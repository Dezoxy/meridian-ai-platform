"""POST /v1/chat in live mode over a fake provider: route, refusal, failure (S010).

No network and no Azure. The fake records which deployment it was asked to call,
so a test can say that a deployment the policy filtered out was never reached.
"""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import get_args

import httpx
import psycopg
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import (
    GLOBAL_DEPLOYMENT_YAML,
    REGISTRY_DIR,
    REPLAY_ENTRY,
    assert_spans_hold_no_exception_and_no_canary,
    audit_events,
    pin_chat_route,
)

from meridian.platform.common import audit
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.gateway import app as gateway_app
from meridian.platform.gateway.app import (
    PROVIDER_FAILED,
    PROVIDER_TIMED_OUT,
    create_app,
)
from meridian.platform.gateway.budget import cost_micro_eur
from meridian.platform.gateway.models import ChatRequest
from meridian.platform.gateway.providers.azure_openai import (
    PROVIDER_TIMEOUT_SECONDS,
    AzureOpenAIProvider,
)
from meridian.platform.gateway.providers.base import (
    ProviderError,
    ProviderErrorKind,
    ProviderReply,
)
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.registry import Registry, load_registry
from meridian.platform.registry.checks import run_checks
from meridian.platform.registry.models import Deployment
from meridian.runtime.app import GATEWAY_TIMEOUT_SECONDS

UNUSED_DSN = "postgresql://model_gateway@db.invalid/meridian"
CLAIM_TEXT = "claimant-secret-text-42"
CANARY = "CANARY-provider-internals-9917"
ANSWER_TEXT = "a fake answer"
PROVIDER_MODEL = "gpt-4o-2024-11-20"  # what a provider says; the registry says gpt-4o
BODY = {"messages": [{"role": "user", "content": CLAIM_TEXT}]}
EU_DEPLOYMENT = "aoai-sdc-gpt-4o"
GLOBAL_DEPLOYMENT = "aoai-sdc-gpt-4o-global"
# Every kind answers 502 or 504 but the content filter's, which is a 400 of its
# own (tested in test_gateway_guardrails.py), and a missing recording's, which
# has its own detail (tested in test_recorded.py).
ERROR_KINDS = tuple(
    k for k in get_args(ProviderErrorKind) if k not in ("filtered", "not-recorded")
)


class CrashingProvider:
    """Raises something that is not a ``ProviderError``, with a canary in it."""

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        raise RuntimeError(f"{CANARY} {request.messages[0].content}")


class FakeProvider:
    """Records the deployment of every call; answers or raises as told."""

    def __init__(self, error: ProviderError | None = None) -> None:
        self.error = error
        self.called: list[str] = []
        self.requests: list[ChatRequest] = []

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        self.called.append(deployment.id)
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ProviderReply(
            text=ANSWER_TEXT,
            finish_reason="length",
            model=PROVIDER_MODEL,
            input_tokens=11,
            output_tokens=7,
        )


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


def live_client(
    provider: FakeProvider,
    database_url: str = UNUSED_DSN,
    registry_dir: Path = REGISTRY_DIR,
    exporter: InMemorySpanExporter | None = None,
) -> TestClient:
    settings = GatewaySettings(
        registry_dir=registry_dir,
        mode="live",
        environment="test",
        database_url=database_url,
    )
    tracing = make_tracer_provider("model-gateway", exporter)
    app = create_app(
        settings, tracer_provider=tracing, providers={"azure-openai": provider}
    )
    return TestClient(app)


def narrowed_registry(update: Callable[[Registry], dict]) -> Registry:
    """The real registry changed in memory, as the registry checks would refuse."""
    registry = load_registry(REGISTRY_DIR)
    return registry.model_copy(update=update(registry))


def chat_span(exporter: InMemorySpanExporter):
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "gateway.chat"]
    return span


@pytest.fixture
def global_first_registry(plant: Callable[..., Path]) -> Path:
    """A registry that passes the checks, whose chat route is the global
    deployment (synthetic data only) followed by the EU one."""
    directory = plant(
        ("models.yaml", REPLAY_ENTRY, GLOBAL_DEPLOYMENT_YAML + REPLAY_ENTRY)
    )
    pin_chat_route(directory, GLOBAL_DEPLOYMENT, EU_DEPLOYMENT)
    assert run_checks(load_registry(directory)) == ()
    return directory


@pytest.fixture
def one_candidate_registry(plant: Callable[..., Path]) -> Path:
    """A registry whose chat route lists the one EU deployment, however many the
    real route lists (S042 walks the candidates, so a failure test needs one)."""
    directory = pin_chat_route(plant(), EU_DEPLOYMENT)
    assert run_checks(load_registry(directory)) == ()
    return directory


# ── happy path ──────────────────────────────────────────────────────────────
def test_a_live_call_reaches_the_routed_deployment_and_is_audited_and_traced(
    fresh_database: DatabaseHandle,
) -> None:
    exporter = InMemorySpanExporter()
    provider = FakeProvider()
    client = live_client(
        provider, fresh_database.dsn("model_gateway"), exporter=exporter
    )
    run_id = uuid.uuid4()

    response = client.post("/v1/chat", json=BODY, headers=headers(run_id=run_id))

    assert response.status_code == 200
    body = response.json()
    assert uuid.UUID(body["call_id"])
    assert (body["mode"], body["deployment"], body["provider"]) == (
        "live",
        EU_DEPLOYMENT,
        "azure-openai",
    )
    # The registry's model name; the provider's own string goes to the span only.
    assert body["model"] == "gpt-4o"
    assert body["output"] == {"text": ANSWER_TEXT, "finish_reason": "length"}
    assert body["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert provider.called == [EU_DEPLOYMENT]
    registry = load_registry(REGISTRY_DIR)
    deployment = registry.deployment(EU_DEPLOYMENT)
    assert deployment is not None
    expected_cost = cost_micro_eur(deployment.price, registry.exchange, 11, 7)
    assert audit_events(fresh_database, run_id) == [
        {
            "service": "model-gateway",
            "event": "model.call",
            "outcome": "completed",
            "tenant": "claims-triage",
            "agent": "claims-triage",
            "run_id": run_id,
            "reference": None,
            "deployment": EU_DEPLOYMENT,
            "provider": "azure-openai",
            "model": "gpt-4o",
            "input_tokens": 11,
            "output_tokens": 7,
            "reason": None,
            "data_class": "personal",
            "sku": "Standard",
            "region": "swedencentral",
            "residency": "eu-region",
            "call_id": uuid.UUID(body["call_id"]),
            "http_status": None,
            "provider_model": PROVIDER_MODEL,
            "suppressed": None,
            "tool": None,
            "worker": None,  # the gateway's rows name no worker (S031)
        }
    ]
    assert dict(chat_span(exporter).attributes) == {
        "meridian.tenant": "claims-triage",
        "meridian.agent": "claims-triage",
        "meridian.run_id": str(run_id),
        "meridian.call_id": body["call_id"],
        "meridian.mode": "live",
        "meridian.deployment": EU_DEPLOYMENT,
        "meridian.provider": "azure-openai",
        "gen_ai.request.model": "gpt-4o",
        "meridian.sku": "Standard",
        "meridian.region": "swedencentral",
        "meridian.residency": "eu-region",
        "meridian.data_class": "personal",
        "gen_ai.response.model": PROVIDER_MODEL,
        "gen_ai.usage.input_tokens": 11,
        "gen_ai.usage.output_tokens": 7,
        "meridian.cost_micro_eur": expected_cost,
        "meridian.attempts": 1,
        "meridian.skipped": 0,
    }


def test_the_provider_gets_the_request_and_no_span_or_row_holds_the_text(
    fresh_database: DatabaseHandle,
) -> None:
    exporter = InMemorySpanExporter()
    provider = FakeProvider()
    client = live_client(
        provider, fresh_database.dsn("model_gateway"), exporter=exporter
    )
    run_id = uuid.uuid4()

    client.post("/v1/chat", json=BODY, headers=headers(run_id=run_id))

    (request,) = provider.requests
    assert request.messages[0].content == CLAIM_TEXT
    assert_spans_hold_no_exception_and_no_canary(exporter, CLAIM_TEXT)
    assert_spans_hold_no_exception_and_no_canary(exporter, ANSWER_TEXT)
    assert CLAIM_TEXT not in str(audit_events(fresh_database, run_id))
    assert ANSWER_TEXT not in str(audit_events(fresh_database, run_id))


# ── T-44: the filter runs before any provider call ─────────────────────────
@pytest.mark.parametrize(
    ("tenant", "reached", "residency", "data_class"),
    [
        ("claims-triage", EU_DEPLOYMENT, "eu-region", "personal"),
        ("development", GLOBAL_DEPLOYMENT, "global", "synthetic"),
    ],
)
def test_a_global_deployment_ahead_of_an_eu_one_is_reached_by_synthetic_data_only(
    fresh_database: DatabaseHandle,
    global_first_registry: Path,
    tenant: str,
    reached: str,
    residency: str,
    data_class: str,
) -> None:
    provider = FakeProvider()
    client = live_client(
        provider,
        fresh_database.dsn("model_gateway"),
        registry_dir=global_first_registry,
    )
    run_id = uuid.uuid4()

    response = client.post(
        "/v1/chat", json=BODY, headers=headers(tenant, run_id=run_id)
    )

    assert response.status_code == 200
    assert response.json()["deployment"] == reached
    # The deployment the policy filtered out was never asked.
    assert provider.called == [reached]
    (event,) = audit_events(fresh_database, run_id)
    assert (event["deployment"], event["residency"], event["data_class"]) == (
        reached,
        residency,
        data_class,
    )


# ── refusals ────────────────────────────────────────────────────────────────
def test_a_tenant_no_candidate_may_reach_is_refused_without_a_provider_call(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry checks refuse a route nobody can use, so the registry is
    # narrowed in memory after it was loaded: every chat candidate, however
    # many the route lists.
    candidates = load_registry(REGISTRY_DIR).route("chat").candidates
    narrowed = narrowed_registry(
        lambda r: {
            "deployments": tuple(
                d.model_copy(update={"data_classes": ("synthetic",)})
                if d.id in candidates
                else d
                for d in r.deployments
            )
        }
    )
    monkeypatch.setattr(gateway_app, "load_registry", lambda _: narrowed)
    exporter = InMemorySpanExporter()
    provider = FakeProvider()
    client = live_client(
        provider, fresh_database.dsn("model_gateway"), exporter=exporter
    )
    run_id = uuid.uuid4()

    response = client.post("/v1/chat", json=BODY, headers=headers(run_id=run_id))

    assert response.status_code == 403
    assert response.json() == {"detail": "request refused"}
    assert provider.called == []
    (event,) = audit_events(fresh_database, run_id)
    assert (event["outcome"], event["reason"], event["data_class"]) == (
        "refused",
        "no-allowed-deployment",
        "personal",
    )
    # A live refusal names no deployment: nothing was chosen.
    assert [
        event[name]
        for name in ("deployment", "provider", "model", "sku", "region", "residency")
    ] == [None] * 6
    assert dict(chat_span(exporter).attributes)["meridian.refusal"] == (
        "no-allowed-deployment"
    )


def test_a_chat_purpose_without_a_route_is_refused(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrowed = narrowed_registry(
        lambda r: {"routes": tuple(x for x in r.routes if x.purpose != "chat")}
    )
    monkeypatch.setattr(gateway_app, "load_registry", lambda _: narrowed)
    provider = FakeProvider()
    client = live_client(provider, fresh_database.dsn("model_gateway"))
    run_id = uuid.uuid4()

    response = client.post("/v1/chat", json=BODY, headers=headers(run_id=run_id))

    assert response.status_code == 403
    assert provider.called == []
    (event,) = audit_events(fresh_database, run_id)
    assert event["reason"] == "no-route"


@pytest.mark.parametrize(
    ("tenant", "agent", "reason", "data_class"),
    [
        ("no-such-tenant", "claims-triage", "unknown-tenant", None),
        ("claims-triage", "some-other-agent", "agent-not-allowed", "personal"),
    ],
)
def test_live_mode_refuses_an_unknown_tenant_and_an_agent_not_allowed(
    fresh_database: DatabaseHandle,
    tenant: str,
    agent: str,
    reason: str,
    data_class: str | None,
) -> None:
    exporter = InMemorySpanExporter()
    provider = FakeProvider()
    client = live_client(
        provider, fresh_database.dsn("model_gateway"), exporter=exporter
    )
    run_id = uuid.uuid4()

    response = client.post(
        "/v1/chat", json=BODY, headers=headers(tenant, agent, run_id)
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "request refused"}
    assert provider.called == []
    (event,) = audit_events(fresh_database, run_id)
    assert (event["outcome"], event["reason"], event["data_class"]) == (
        "refused",
        reason,
        data_class,
    )
    assert event["deployment"] is None
    assert dict(chat_span(exporter).attributes)["meridian.refusal"] == reason


# ── a provider failure ──────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", ERROR_KINDS)
def test_a_provider_error_answers_a_generic_502_or_504_and_is_audited(
    fresh_database: DatabaseHandle,
    one_candidate_registry: Path,
    kind: ProviderErrorKind,
) -> None:
    exporter = InMemorySpanExporter()
    provider = FakeProvider(error=ProviderError(kind, 500))
    client = live_client(
        provider,
        fresh_database.dsn("model_gateway"),
        registry_dir=one_candidate_registry,
        exporter=exporter,
    )
    run_id = uuid.uuid4()

    response = client.post("/v1/chat", json=BODY, headers=headers(run_id=run_id))

    timed_out = kind == "timeout"
    assert response.status_code == (504 if timed_out else 502)
    assert response.json() == {
        "detail": PROVIDER_TIMED_OUT if timed_out else PROVIDER_FAILED
    }
    (event,) = audit_events(fresh_database, run_id)
    assert (event["event"], event["outcome"], event["reason"]) == (
        "model.call",
        "failed",
        kind,
    )
    assert (
        event["deployment"],
        event["provider"],
        event["model"],
        event["data_class"],
        event["sku"],
        event["region"],
        event["residency"],
    ) == (
        EU_DEPLOYMENT,
        "azure-openai",
        "gpt-4o",
        "personal",
        "Standard",
        "swedencentral",
        "eu-region",
    )
    assert (event["input_tokens"], event["output_tokens"]) == (None, None)
    assert dict(chat_span(exporter).attributes)["error.type"] == kind
    assert_spans_hold_no_exception_and_no_canary(exporter, CLAIM_TEXT)


# ── a provider call that fails in a way no one planned ──────────────────────
def test_an_unexpected_provider_exception_is_audited_as_internal_and_answers_500(
    fresh_database: DatabaseHandle, one_candidate_registry: Path
) -> None:
    exporter = InMemorySpanExporter()
    client = live_client(
        CrashingProvider(),
        fresh_database.dsn("model_gateway"),
        registry_dir=one_candidate_registry,
        exporter=exporter,
    )
    run_id = uuid.uuid4()

    response = client.post("/v1/chat", json=BODY, headers=headers(run_id=run_id))

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    (event,) = audit_events(fresh_database, run_id)
    assert (event["event"], event["outcome"], event["reason"]) == (
        "model.call",
        "failed",
        "internal",
    )
    assert (
        event["deployment"],
        event["provider"],
        event["model"],
        event["data_class"],
        event["sku"],
        event["region"],
        event["residency"],
    ) == (
        EU_DEPLOYMENT,
        "azure-openai",
        "gpt-4o",
        "personal",
        "Standard",
        "swedencentral",
        "eu-region",
    )
    assert (event["input_tokens"], event["output_tokens"]) == (None, None)
    assert dict(chat_span(exporter).attributes)["error.type"] == "internal"
    for canary in (CANARY, CLAIM_TEXT):
        assert canary not in response.text
        assert canary not in str(event)
        assert_spans_hold_no_exception_and_no_canary(exporter, canary)


def test_when_the_audit_of_an_unexpected_failure_fails_the_answer_is_503(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    one_candidate_registry: Path,
) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(audit, "connect", fail_connect)
    client = live_client(
        CrashingProvider(),
        fresh_database.dsn("model_gateway"),
        registry_dir=one_candidate_registry,
    )

    response = client.post("/v1/chat", json=BODY, headers=headers())

    assert response.status_code == 503
    assert CANARY not in response.text


def test_a_malformed_provider_reply_through_the_real_adapter_is_a_502_and_one_row(
    fresh_database: DatabaseHandle, one_candidate_registry: Path
) -> None:
    # A 200 whose first choice has no message: before the fix the adapter raised
    # AttributeError and the call left no audit row.
    malformed = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": PROVIDER_MODEL,
        "choices": [{"index": 0, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    provider = AzureOpenAIProvider(
        {"sdc": "https://oai-meridian-sdc-a1b2c3.openai.azure.com"},
        lambda: "fake-entra-token",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=malformed)
            )
        ),
    )
    exporter = InMemorySpanExporter()
    client = live_client(
        provider,
        fresh_database.dsn("model_gateway"),
        registry_dir=one_candidate_registry,
        exporter=exporter,
    )
    run_id = uuid.uuid4()

    response = client.post("/v1/chat", json=BODY, headers=headers(run_id=run_id))

    assert response.status_code == 502
    assert response.json() == {"detail": PROVIDER_FAILED}
    (event,) = audit_events(fresh_database, run_id)
    assert (event["outcome"], event["reason"]) == ("failed", "bad-response")
    assert event["deployment"] == EU_DEPLOYMENT
    assert dict(chat_span(exporter).attributes)["error.type"] == "bad-response"


def test_when_the_audit_write_of_a_failed_call_fails_the_answer_is_503(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    one_candidate_registry: Path,
) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise psycopg.OperationalError("connection refused: password=hunter2")

    monkeypatch.setattr(audit, "connect", fail_connect)
    provider = FakeProvider(error=ProviderError("unavailable", 503))
    client = live_client(
        provider,
        fresh_database.dsn("model_gateway"),
        registry_dir=one_candidate_registry,
    )

    response = client.post("/v1/chat", json=BODY, headers=headers())

    assert response.status_code == 503
    assert provider.called == [EU_DEPLOYMENT]
    assert "hunter2" not in response.text
    assert "provider" not in response.text


def test_when_the_audit_write_of_a_completed_call_fails_the_answer_has_no_output(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(audit, "connect", fail_connect)
    provider = FakeProvider()
    client = live_client(provider, fresh_database.dsn("model_gateway"))

    response = client.post("/v1/chat", json=BODY, headers=headers())

    assert response.status_code == 503
    assert provider.called == [EU_DEPLOYMENT]  # the audit, not the ledger, failed
    assert ANSWER_TEXT not in response.text


# ── timeouts ────────────────────────────────────────────────────────────────
def test_the_provider_timeout_is_below_the_runtimes_timeout_to_the_gateway() -> None:
    assert PROVIDER_TIMEOUT_SECONDS < GATEWAY_TIMEOUT_SECONDS
