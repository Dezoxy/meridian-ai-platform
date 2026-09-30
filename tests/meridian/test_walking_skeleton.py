"""S009's done-when: a claim crosses the API, the runtime and the gateway.

Three apps run in one process, each with its own tracer provider exporting to
one in-memory exporter. The gateway sits behind a Starlette ``TestClient``, the
runtime's HTTP client is that client, and the claims app's HTTP client is a
``TestClient`` of the runtime, so the real HTTP contracts, the real graph (found
through its entry point), the real SQL and the real grants are exercised.
"""

import uuid

import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR, claim_with_id, owner_rows, synthetic_claims

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.gateway.app import create_app as create_gateway
from meridian.platform.gateway.settings import GatewaySettings
from meridian.runtime.app import create_app as create_runtime
from meridian.runtime.settings import RuntimeSettings
from meridian.workloads.claims_triage.app import create_app as create_claims_api
from meridian.workloads.claims_triage.settings import ClaimsSettings

CLAIM = synthetic_claims()[0]  # CLM-0001
SERVICES = {"claims-api", "agent-runtime", "model-gateway"}


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def claims_client(
    fresh_database: DatabaseHandle, exporter: InMemorySpanExporter
) -> TestClient:
    gateway = create_gateway(
        GatewaySettings(
            registry_dir=REGISTRY_DIR,
            mode="replay",
            environment="test",
            database_url=fresh_database.dsn("model_gateway"),
        ),
        tracer_provider=make_tracer_provider("model-gateway", exporter),
    )
    runtime = create_runtime(
        RuntimeSettings(
            registry_dir=REGISTRY_DIR,
            gateway_url="http://gateway.test",
            database_url=fresh_database.dsn("agent_runtime"),
        ),
        tracer_provider=make_tracer_provider("agent-runtime", exporter),
        http_client=TestClient(gateway),
    )
    claims = create_claims_api(
        ClaimsSettings(
            runtime_url="http://runtime.test",
            database_url=fresh_database.dsn("claims_api"),
        ),
        tracer_provider=make_tracer_provider("claims-api", exporter),
        http_client=TestClient(runtime),
    )
    return TestClient(claims)


def service_of(span: ReadableSpan) -> str:
    return str(span.resource.attributes["service.name"])


def ancestors(span: ReadableSpan, by_id: dict[int, ReadableSpan]) -> list[str]:
    """Span names from ``span``'s parent up to the root."""
    names: list[str] = []
    parent = span.parent
    while parent is not None and parent.span_id in by_id:
        span = by_id[parent.span_id]
        names.append(span.name)
        parent = span.parent
    return names


def test_a_claim_crosses_api_runtime_and_gateway_in_one_trace(
    claims_client: TestClient,
    fresh_database: DatabaseHandle,
    exporter: InMemorySpanExporter,
) -> None:
    response = claims_client.post("/claims", json=CLAIM)

    assert response.status_code == 201
    body = response.json()
    run_id = uuid.UUID(body["run_id"])
    assert body["claim_id"] == "CLM-0001"
    assert body["run_status"] == "Completed"
    assert body["proposal"]["route"] == "adjuster"
    assert body["proposal"]["drafted_by"] == {
        "deployment": "replay-chat",
        "provider": "replay",
        "mode": "replay",
    }

    # ── the rows ────────────────────────────────────────────────────────────
    assert owner_rows(fresh_database, "SELECT claim_id, tenant FROM claims.claims") == [
        ("CLM-0001", "claims-triage")
    ]
    assert owner_rows(
        fresh_database,
        "SELECT claim_id, run_id, route, drafted_by_deployment, drafted_by_provider "
        "FROM claims.triage_proposals",
    ) == [("CLM-0001", run_id, "adjuster", "replay-chat", "replay")]
    assert owner_rows(
        fresh_database, "SELECT run_id, status, reference FROM runtime.runs"
    ) == [(run_id, "Completed", "CLM-0001")]
    events = owner_rows(
        fresh_database,
        "SELECT service, event, outcome FROM audit.events WHERE run_id = %s",
        (run_id,),
    )
    assert sorted(events) == [
        ("agent-runtime", "run.completed", "completed"),
        ("agent-runtime", "run.started", "started"),
        ("model-gateway", "model.call", "completed"),
    ]

    # ── one trace across the three services ─────────────────────────────────
    spans = exporter.get_finished_spans()
    assert {service_of(s) for s in spans} == SERVICES
    assert len({s.context.trace_id for s in spans}) == 1
    by_id = {s.context.span_id: s for s in spans}
    (node,) = [s for s in spans if s.name == "langgraph.node draft_proposal"]
    (chat,) = [s for s in spans if s.name == "gateway.chat"]
    assert service_of(node) == "agent-runtime"
    assert service_of(chat) == "model-gateway"
    # The gateway's span descends from the node that made the call, and the
    # node from the run and the claim: the trace is a chain, not a bag.
    above_chat = ancestors(chat, by_id)
    for expected in ("langgraph.node draft_proposal", "runtime.run", "claims.submit"):
        assert expected in above_chat
    assert above_chat.index("langgraph.node draft_proposal") < above_chat.index(
        "runtime.run"
    )
    assert above_chat.index("runtime.run") < above_chat.index("claims.submit")

    # ── no claimant content anywhere (T-03) ─────────────────────────────────
    secrets = (
        CLAIM["claimant"]["name"],
        CLAIM["claimant"]["email"],
        CLAIM["description"],
    )
    span_values = [
        str(value)
        for span in spans
        for value in (
            *span.attributes.values(),
            span.status.description or "",
            *(v for event in span.events for v in event.attributes.values()),
        )
    ]
    audit_values = [
        str(value)
        for row in owner_rows(fresh_database, "SELECT * FROM audit.events")
        for value in row
    ]
    run_values = [
        str(value)
        for row in owner_rows(fresh_database, "SELECT * FROM runtime.runs")
        for value in row
    ]
    for secret in secrets:
        assert not any(secret in value for value in span_values)
        assert not any(secret in value for value in audit_values)
        assert not any(secret in value for value in run_values)


def test_a_second_post_of_the_same_claim_is_a_conflict_and_adds_no_second_trace(
    claims_client: TestClient, exporter: InMemorySpanExporter
) -> None:
    assert claims_client.post("/claims", json=CLAIM).status_code == 201
    exporter.clear()

    second = claims_client.post("/claims", json=CLAIM)

    assert second.status_code == 409
    assert {service_of(s) for s in exporter.get_finished_spans()} == {"claims-api"}


def test_another_synthetic_claim_takes_the_same_path(
    claims_client: TestClient,
) -> None:
    response = claims_client.post("/claims", json=claim_with_id("CLM-0002", index=1))

    assert response.status_code == 201
    assert response.json()["proposal"]["route"] == "adjuster"
