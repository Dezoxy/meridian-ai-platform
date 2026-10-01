"""POST /v1/chat: policy, replay, audit, the span and the error shapes."""

import uuid
from pathlib import Path

import psycopg
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from psycopg.conninfo import make_conninfo
from servicesupport import (
    REGISTRY_DIR,
    assert_spans_hold_no_exception_and_no_canary,
    audit_events,
    database_error,
)

from meridian.platform.common import audit
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.gateway import app as gateway_app
from meridian.platform.gateway.app import create_app
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.registry import load_registry

UNUSED_DSN = "postgresql://model_gateway@db.invalid/meridian"
CLAIM_TEXT = "claimant-secret-text-42"
BODY = {"messages": [{"role": "user", "content": CLAIM_TEXT}]}


def headers(
    tenant: str = "claims-triage",
    agent: str = "claims-triage",
    run_id: uuid.UUID | None = None,
) -> dict[str, str]:
    return {
        "X-Meridian-Tenant": tenant,
        "X-Meridian-Agent": agent,
        "X-Meridian-Run": str(run_id or uuid.uuid4()),
    }


def make_client(
    database_url: str = UNUSED_DSN,
    registry_dir: Path = REGISTRY_DIR,
    exporter: InMemorySpanExporter | None = None,
) -> TestClient:
    settings = GatewaySettings(
        registry_dir=registry_dir,
        mode="replay",
        environment="test",
        database_url=database_url,
    )
    provider = make_tracer_provider("model-gateway", exporter)
    return TestClient(create_app(settings, tracer_provider=provider))


# ── happy path ──────────────────────────────────────────────────────────────
def test_a_chat_call_is_answered_audited_and_traced(
    fresh_database: DatabaseHandle,
) -> None:
    exporter = InMemorySpanExporter()
    client = make_client(fresh_database.dsn("model_gateway"), exporter=exporter)
    run_id = uuid.uuid4()

    response = client.post("/v1/chat", json=BODY, headers=headers(run_id=run_id))

    assert response.status_code == 200
    body = response.json()
    assert uuid.UUID(body["call_id"])
    assert (body["mode"], body["deployment"], body["provider"], body["model"]) == (
        "replay",
        "replay-chat",
        "replay",
        "replay-chat",
    )
    assert body["output"]["text"].startswith("Replay response (simulated;")
    usage = body["usage"]
    assert usage["input_tokens"] == -(-len(CLAIM_TEXT) // 4)
    assert usage["output_tokens"] == -(-len(body["output"]["text"]) // 4)
    assert audit_events(fresh_database, run_id) == [
        {
            "service": "model-gateway",
            "event": "model.call",
            "outcome": "completed",
            "tenant": "claims-triage",
            "agent": "claims-triage",
            "run_id": run_id,
            "reference": None,
            "deployment": "replay-chat",
            "provider": "replay",
            "model": "replay-chat",
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "reason": None,
            "data_class": "personal",
            "sku": None,  # a replay deployment has no sku or region
            "region": None,
            "residency": "eu-region",
            "call_id": uuid.UUID(body["call_id"]),
            "http_status": None,
            "provider_model": "replay-chat",
            "suppressed": None,
            "tool": None,
        }
    ]
    (chat_span,) = [
        s for s in exporter.get_finished_spans() if s.name == "gateway.chat"
    ]
    assert dict(chat_span.attributes) == {
        "meridian.tenant": "claims-triage",
        "meridian.agent": "claims-triage",
        "meridian.run_id": str(run_id),
        "meridian.call_id": body["call_id"],
        "meridian.cost_micro_eur": 0,  # a replay deployment costs nothing
        "meridian.deployment": "replay-chat",
        "meridian.provider": "replay",
        "meridian.mode": "replay",
        "meridian.residency": "eu-region",
        "meridian.data_class": "personal",
        "gen_ai.request.model": "replay-chat",
        "gen_ai.response.model": "replay-chat",
        "gen_ai.usage.input_tokens": usage["input_tokens"],
        "gen_ai.usage.output_tokens": usage["output_tokens"],
        "meridian.attempts": 1,
        "meridian.skipped": 0,
    }
    assert body["output"]["finish_reason"] == "stop"


def test_the_same_request_gets_the_same_text_twice(
    fresh_database: DatabaseHandle,
) -> None:
    client = make_client(fresh_database.dsn("model_gateway"))

    first = client.post("/v1/chat", json=BODY, headers=headers())
    second = client.post("/v1/chat", json=BODY, headers=headers())

    assert first.json()["output"] == second.json()["output"]
    assert first.json()["call_id"] != second.json()["call_id"]


def test_no_span_and_no_audit_row_holds_the_message_content(
    fresh_database: DatabaseHandle,
) -> None:
    exporter = InMemorySpanExporter()
    client = make_client(fresh_database.dsn("model_gateway"), exporter=exporter)
    run_id = uuid.uuid4()

    client.post("/v1/chat", json=BODY, headers=headers(run_id=run_id))

    for span in exporter.get_finished_spans():
        assert CLAIM_TEXT not in " ".join(map(str, span.attributes.values()))
    assert CLAIM_TEXT not in str(audit_events(fresh_database, run_id))


# ── policy ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("tenant", "agent", "reason", "data_class"),
    [
        ("no-such-tenant", "claims-triage", "unknown-tenant", None),
        # agent not in the tenant's list
        ("claims-triage", "some-other-agent", "agent-not-allowed", "personal"),
    ],
)
def test_a_refused_request_answers_403_and_is_audited(
    fresh_database: DatabaseHandle,
    tenant: str,
    agent: str,
    reason: str,
    data_class: str | None,
) -> None:
    client = make_client(fresh_database.dsn("model_gateway"))
    run_id = uuid.uuid4()

    response = client.post(
        "/v1/chat", json=BODY, headers=headers(tenant, agent, run_id)
    )

    assert response.status_code == 403
    assert "output" not in response.text
    (event,) = audit_events(fresh_database, run_id)
    assert (event["service"], event["event"], event["outcome"]) == (
        "model-gateway",
        "model.call",
        "refused",
    )
    assert (event["tenant"], event["agent"]) == (tenant, agent)
    assert event["input_tokens"] is None
    # T-39: a refusal says where the call would have gone (replay always knows).
    assert (event["deployment"], event["provider"], event["model"]) == (
        "replay-chat",
        "replay",
        "replay-chat",
    )
    assert (event["reason"], event["data_class"]) == (reason, data_class)
    assert (event["sku"], event["region"]) == (None, None)


def test_a_data_class_the_replay_deployment_does_not_allow_is_refused(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry checks now refuse a registry whose replay deployment cannot
    # serve every tenant, so this second line of defence is tested on a
    # registry narrowed in memory after it was loaded.
    registry = load_registry(REGISTRY_DIR)
    narrowed = registry.model_copy(
        update={
            "deployments": tuple(
                d.model_copy(update={"data_classes": ("synthetic",)})
                if d.id == "replay-chat"
                else d
                for d in registry.deployments
            )
        }
    )
    monkeypatch.setattr(gateway_app, "load_registry", lambda _: narrowed)
    client = make_client(fresh_database.dsn("model_gateway"))
    refused_run, allowed_run = uuid.uuid4(), uuid.uuid4()

    personal = client.post(  # tenant claims-triage holds personal data
        "/v1/chat", json=BODY, headers=headers("claims-triage", run_id=refused_run)
    )
    synthetic = client.post(  # tenant development holds synthetic data only
        "/v1/chat", json=BODY, headers=headers("development", run_id=allowed_run)
    )

    assert (personal.status_code, synthetic.status_code) == (403, 200)
    assert [e["outcome"] for e in audit_events(fresh_database, refused_run)] == [
        "refused"
    ]
    assert [e["outcome"] for e in audit_events(fresh_database, allowed_run)] == [
        "completed"
    ]


def test_a_refusal_span_carries_the_deployment_provider_and_mode() -> None:
    exporter = InMemorySpanExporter()
    client = make_client(exporter=exporter)

    client.post("/v1/chat", json=BODY, headers=headers("no-such-tenant"))

    (chat_span,) = [
        s for s in exporter.get_finished_spans() if s.name == "gateway.chat"
    ]
    assert (
        dict(chat_span.attributes).items()
        >= {
            "meridian.deployment": "replay-chat",
            "meridian.provider": "replay",
            "meridian.mode": "replay",
        }.items()
    )


def test_a_residency_the_data_class_does_not_allow_is_refused(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry checks already refuse a deployment whose label its classes
    # cannot reach, so this second line of defence is tested on a registry
    # narrowed in memory after it was loaded: personal data may now reach
    # eu-zone only, and the replay deployment is eu-region.
    registry = load_registry(REGISTRY_DIR)
    narrowed = registry.model_copy(
        update={
            "data_classes": tuple(
                c.model_copy(update={"residency": ("eu-zone",)})
                if c.id == "personal"
                else c
                for c in registry.data_classes
            )
        }
    )
    monkeypatch.setattr(gateway_app, "load_registry", lambda _: narrowed)
    client = make_client(fresh_database.dsn("model_gateway"))
    refused_run, allowed_run = uuid.uuid4(), uuid.uuid4()

    personal = client.post(
        "/v1/chat", json=BODY, headers=headers("claims-triage", run_id=refused_run)
    )
    synthetic = client.post(
        "/v1/chat", json=BODY, headers=headers("development", run_id=allowed_run)
    )

    assert (personal.status_code, synthetic.status_code) == (403, 200)
    (event,) = audit_events(fresh_database, refused_run)
    assert (event["outcome"], event["reason"]) == ("refused", "no-allowed-deployment")


# ── the audit write is part of the answer (QA-05) ───────────────────────────
def fail_connect(*_args: object, **_kwargs: object) -> None:
    raise psycopg.OperationalError("connection refused: password=hunter2")


def test_when_the_audit_write_fails_the_call_answers_503_with_no_output(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real database, so the ledger works and the audit write is what fails.
    monkeypatch.setattr(audit, "connect", fail_connect)
    client = make_client(fresh_database.dsn("model_gateway"))

    response = client.post("/v1/chat", json=BODY, headers=headers())

    assert response.status_code == 503
    assert "output" not in response.text
    assert "usage" not in response.text
    assert "hunter2" not in response.text


def test_when_the_audit_write_of_a_refusal_fails_the_answer_is_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit, "connect", fail_connect)
    client = make_client()

    response = client.post("/v1/chat", json=BODY, headers=headers("no-such-tenant"))

    assert response.status_code == 503


def test_an_unreachable_database_is_a_503_too(
    fresh_database: DatabaseHandle,
) -> None:
    nobody_listens = make_conninfo(
        fresh_database.dsn("model_gateway"), host="127.0.0.1", port=1
    )
    client = make_client(nobody_listens)

    response = client.post("/v1/chat", json=BODY, headers=headers())

    assert response.status_code == 503
    assert "output" not in response.text


# ── request validation: no database needed, no content echoed ───────────────
@pytest.mark.parametrize(
    "drop", ["X-Meridian-Tenant", "X-Meridian-Agent", "X-Meridian-Run"]
)
def test_a_missing_header_is_a_422(drop: str) -> None:
    sent = {k: v for k, v in headers().items() if k != drop}

    response = make_client().post("/v1/chat", json=BODY, headers=sent)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("X-Meridian-Tenant", "Claims_Triage"),
        ("X-Meridian-Tenant", "-leading-dash"),
        ("X-Meridian-Tenant", "a" * 65),
        ("X-Meridian-Agent", "has space"),
        ("X-Meridian-Run", "not-a-uuid"),
    ],
)
def test_a_malformed_header_is_a_422(header: str, value: str) -> None:
    response = make_client().post(
        "/v1/chat", json=BODY, headers=headers() | {header: value}
    )

    assert response.status_code == 422


def message(role: str = "user", content: str = "hello") -> dict[str, str]:
    return {"role": role, "content": content}


@pytest.mark.parametrize(
    "body",
    [
        {"messages": []},
        {"messages": [message()] * 51},
        {"messages": [message(role="tool")]},
        {"messages": [message(content="")]},
        {"messages": [message(content="x" * 20_001)]},
        {"messages": [message()], "max_output_tokens": 0},
        {"messages": [message()], "max_output_tokens": 4097},
        {"messages": [message()], "temperature": 0.2},
        {"messages": [{**message(), "name": "extra"}]},
        {},
    ],
)
def test_an_invalid_body_is_a_422(body: dict) -> None:
    response = make_client().post("/v1/chat", json=body, headers=headers())

    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"messages": [message()] * 50, "max_output_tokens": 4096},
        {"messages": [message(content="x" * 20_000)]},
    ],
)
def test_the_limits_themselves_are_accepted(
    fresh_database: DatabaseHandle, body: dict
) -> None:
    client = make_client(fresh_database.dsn("model_gateway"))

    assert client.post("/v1/chat", json=body, headers=headers()).status_code == 200


def test_a_422_does_not_echo_what_was_sent() -> None:
    body = {"messages": [message(content=CLAIM_TEXT)], "temperature": CLAIM_TEXT}

    response = make_client().post("/v1/chat", json=body, headers=headers())

    assert response.status_code == 422
    assert CLAIM_TEXT not in response.text


def test_the_default_output_limit_is_1024_and_is_accepted(
    fresh_database: DatabaseHandle,
) -> None:
    client = make_client(fresh_database.dsn("model_gateway"))

    assert client.post("/v1/chat", json=BODY, headers=headers()).status_code == 200


def test_a_nul_byte_in_a_message_is_a_422_not_an_outage() -> None:
    body = {"messages": [message(content="before\x00after")]}

    response = make_client().post("/v1/chat", json=body, headers=headers())

    assert response.status_code == 422
    assert "before" not in response.text


# ── a database error inside the span never reaches the span (T-03) ──────────
def test_a_database_error_leaves_no_exception_event_and_no_message_in_a_span(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise database_error(CLAIM_TEXT)

    monkeypatch.setattr(audit, "connect", refuse)
    exporter = InMemorySpanExporter()
    client = make_client(fresh_database.dsn("model_gateway"), exporter=exporter)

    response = client.post("/v1/chat", json=BODY, headers=headers())

    assert response.status_code == 503
    assert CLAIM_TEXT not in response.text
    assert_spans_hold_no_exception_and_no_canary(exporter, CLAIM_TEXT)
    (chat_span,) = [
        s for s in exporter.get_finished_spans() if s.name == "gateway.chat"
    ]
    assert chat_span.status.description == "AuditUnavailable"


# ── request body limit: 4 MiB ───────────────────────────────────────────────
def test_a_body_over_4_mib_is_413() -> None:
    response = make_client().post(
        "/v1/chat",
        content=b"x" * (4 * 1024 * 1024 + 1),
        headers=headers() | {"Content-Type": "application/json"},
    )

    assert response.status_code == 413


# ── lifecycle ───────────────────────────────────────────────────────────────
def test_healthz_and_the_lifespan_need_no_database() -> None:
    with make_client() as client:
        response = client.get("/healthz")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})
