"""The five services refuse a caller the registry does not map (S055).

In process: ``as_caller`` sets the TLS extension a verified certificate would.
The check, its audit and the tenant and agent rule are proved here; the
certificate itself is proved with real TLS in ``common/test_peercert.py``.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from callersupport import CALLER_HEADER, PREFIX, as_caller, as_caller_named_by_header
from dbsupport import DatabaseHandle
from servicesupport import REGISTRY_DIR, owner_rows
from starlette.testclient import TestClient
from starlette.types import ASGIApp
from toolsupport import HOSTS, add_run

from meridian.platform.common.env import SettingsError
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.gateway import app as gateway_module
from meridian.platform.gateway.app import create_app as create_gateway
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.knowledge_mcp.app import create_app as create_knowledge
from meridian.platform.knowledge_mcp.settings import KnowledgeServerSettings
from meridian.platform.policy_mcp.app import create_app as create_policy
from meridian.platform.toolserver.settings import ToolServerSettings
from meridian.runtime import app as runtime_module
from meridian.runtime.app import create_app as create_runtime
from meridian.runtime.settings import RuntimeSettings
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app as create_claims_mcp,
)

REFUSED = {"detail": "request refused"}
UNUSED_DSN = "postgresql://role@db.invalid/meridian"
MCP_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
NAME_REASON = "caller-name-not-allowed"
CHAT = {"messages": [{"role": "user", "content": "hello"}]}
EMBED = {"inputs": ["a sentence"]}


# One app of each service, built with the prefix over the DSN it is given.
def gateway_app(dsn: str) -> ASGIApp:
    return create_gateway(
        GatewaySettings(
            registry_dir=REGISTRY_DIR,
            mode="replay",
            environment="test",
            database_url=dsn,
            identity_prefix=PREFIX,
        ),
        tracer_provider=make_tracer_provider("model-gateway"),
    )


def runtime_app(dsn: str) -> ASGIApp:
    return create_runtime(
        RuntimeSettings(
            registry_dir=REGISTRY_DIR,
            gateway_url="http://gateway.invalid",
            database_url=dsn,
            identity_prefix=PREFIX,
        ),
        tracer_provider=make_tracer_provider("agent-runtime"),
    )


def tool_settings(dsn: str) -> ToolServerSettings:
    return ToolServerSettings(
        registry_dir=REGISTRY_DIR,
        database_url=dsn,
        allowed_hosts=HOSTS,
        identity_prefix=PREFIX,
    )


def policy_app(dsn: str) -> ASGIApp:
    provider = make_tracer_provider("policy-mcp")
    return create_policy(tool_settings(dsn), tracer_provider=provider).app


def claims_mcp_app(dsn: str) -> ASGIApp:
    provider = make_tracer_provider("claims-mcp")
    return create_claims_mcp(tool_settings(dsn), tracer_provider=provider).app


def knowledge_app(dsn: str) -> ASGIApp:
    settings = KnowledgeServerSettings(
        **dict(tool_settings(dsn)), gateway_url="http://gateway.invalid"
    )
    http = httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    provider = make_tracer_provider("knowledge-mcp")
    return create_knowledge(settings, http=http, tracer_provider=provider).app


def a_tool_call(client: TestClient) -> httpx.Response:
    return client.post("/mcp", json=MCP_LIST, headers=MCP_HEADERS)


def an_embedding_call(client: TestClient) -> httpx.Response:
    return client.post("/v1/embeddings", json=EMBED, headers=headers())


def a_run_read(client: TestClient) -> httpx.Response:
    return client.get(
        f"/runs/{uuid.uuid4()}",
        params={"tenant": "claims-triage", "reference": "CLM-0001"},
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


@dataclass(frozen=True)
class Service:
    """A service, how to build it, a call that reaches its routes and what the
    allowed caller gets for it (the runtime's read is a 404: the run is not
    there), then two services of the registry that may not call it."""

    role: str
    build: Callable[[str], ASGIApp]
    call: Callable[[TestClient], httpx.Response]
    allowed: str
    answer: int
    forbidden: tuple[str, str]


SERVICES = {
    "model-gateway": Service(
        "model_gateway",
        gateway_app,
        an_embedding_call,
        "agent-runtime",
        200,
        ("claims-api", "policy-mcp"),
    ),
    "agent-runtime": Service(
        "agent_runtime",
        runtime_app,
        a_run_read,
        "claims-api",
        404,
        ("model-gateway", "knowledge-mcp"),
    ),
    "policy-mcp": Service(
        "policy_mcp",
        policy_app,
        a_tool_call,
        "agent-runtime",
        200,
        ("claims-api", "knowledge-mcp"),
    ),
    "claims-mcp": Service(
        "claims_mcp",
        claims_mcp_app,
        a_tool_call,
        "agent-runtime",
        200,
        ("claims-api", "knowledge-mcp"),
    ),
    "knowledge-mcp": Service(
        "knowledge_mcp",
        knowledge_app,
        a_tool_call,
        "agent-runtime",
        200,
        ("claims-api", "policy-mcp"),
    ),
}


def client_of(
    service: str, caller: str | None, db: DatabaseHandle | None
) -> TestClient:
    """A client of the service, called by ``caller``; with no database the
    audit cannot be written."""
    spec = SERVICES[service]
    dsn = UNUSED_DSN if db is None else db.dsn(spec.role)
    return TestClient(
        as_caller(spec.build(dsn), caller),
        base_url=f"http://{HOSTS[0]}",
        raise_server_exceptions=False,
    )


@contextmanager
def session(
    service: str, db: DatabaseHandle | None
) -> Iterator[Callable[[str | None], httpx.Response]]:
    """One running app of the service, and a way to make its own call as a
    given caller (nobody for ``None``). The app's lifespan runs, as a tool
    server's session manager needs; its audit throttle lives as long as the
    session."""
    spec = SERVICES[service]
    dsn = UNUSED_DSN if db is None else db.dsn(spec.role)
    app = as_caller_named_by_header(spec.build(dsn))
    with TestClient(
        app, base_url=f"http://{HOSTS[0]}", raise_server_exceptions=False
    ) as client:

        def call(caller: str | None) -> httpx.Response:
            if caller is None:
                client.headers.pop(CALLER_HEADER, None)
            else:
                client.headers[CALLER_HEADER] = caller
            return spec.call(client)

        yield call


def caller_rows(db: DatabaseHandle) -> list[tuple]:
    """The audit rows of refused callers, oldest first: service, event,
    outcome, reason, caller (the reference column), suppressed."""
    return owner_rows(
        db,
        "SELECT service, event, outcome, reason, reference, suppressed "
        "FROM audit.events WHERE reason LIKE 'caller-%%' ORDER BY recorded_at",
    )


# ── the five: who may call ──────────────────────────────────────────────────
@pytest.mark.parametrize("service", SERVICES)
def test_no_identity_is_401_and_a_service_that_may_not_call_is_403(
    service: str, fresh_database: DatabaseHandle
) -> None:
    with session(service, fresh_database) as call:
        anonymous = call(None)
        unknown = call("stranger")
        refused = [call(caller) for caller in SERVICES[service].forbidden]

    assert (anonymous.status_code, anonymous.json()) == (401, REFUSED)
    assert (unknown.status_code, unknown.json()) == (403, REFUSED)
    assert [(r.status_code, r.json()) for r in refused] == [(403, REFUSED)] * 2


@pytest.mark.parametrize("service", SERVICES)
def test_the_allowed_caller_passes_and_the_health_probe_needs_no_identity(
    service: str, fresh_database: DatabaseHandle
) -> None:
    spec = SERVICES[service]

    with session(service, fresh_database) as call:
        passed = call(spec.allowed)
    with client_of(service, None, fresh_database) as client:
        probe = client.get("/healthz")

    assert passed.status_code == spec.answer
    assert passed.json() != REFUSED
    assert (probe.status_code, probe.json()) == (200, {"status": "ok"})


@pytest.mark.parametrize("service", SERVICES)
def test_a_refusal_is_audited_with_its_reason_and_the_caller_once_per_window(
    service: str, fresh_database: DatabaseHandle
) -> None:
    forbidden = SERVICES[service].forbidden[0]

    with session(service, fresh_database) as call:
        for _ in range(3):
            call(forbidden)
        call(None)
        call("stranger")

    event = event_of(service)
    assert [r[:6] for r in caller_rows(fresh_database)] == [
        (service, event, "refused", "caller-not-allowed", forbidden, 0),
        (service, event, "refused", "caller-no-identity", None, 0),
        (service, event, "refused", "caller-unknown-service", "stranger", 0),
    ]


def event_of(service: str) -> str:
    """The event the service gives its other refusals."""
    return {"model-gateway": "model.call", "agent-runtime": "run.refused"}.get(
        service, "tool.call"
    )


@pytest.mark.parametrize("service", SERVICES)
def test_a_refusal_whose_audit_cannot_be_written_is_still_refused(
    service: str,
) -> None:
    with session(service, None) as call:
        anonymous = call(None)
        forbidden = call(SERVICES[service].forbidden[0])

    assert (anonymous.status_code, forbidden.status_code) == (401, 403)


@pytest.mark.parametrize(
    ("module", "build"),
    [(gateway_module, gateway_app), (runtime_module, runtime_app)],
)
def test_a_service_the_registry_does_not_hold_stops_the_start(
    monkeypatch: pytest.MonkeyPatch, module: Any, build: Callable[[str], ASGIApp]
) -> None:
    monkeypatch.setattr(module, "SERVICE_NAME", "ghost-service")

    with pytest.raises(SettingsError, match="ghost-service"):
        build(UNUSED_DSN)


# ── the gateway: the tenant and the agent a caller may name ─────────────────
@pytest.mark.parametrize(
    ("tenant", "agent"),
    [
        ("evaluation", "claims-triage"),  # a tenant the runtime does not name
        ("claims-triage", "knowledge-ingestion"),  # an agent it does not name
        ("evaluation", "evaluation-judge"),
    ],
)
@pytest.mark.parametrize(
    ("route", "body"), [("/v1/chat", CHAT), ("/v1/embeddings", EMBED)]
)
def test_the_runtime_naming_a_tenant_or_agent_outside_its_lists_is_403_and_audited(
    route: str,
    body: dict[str, Any],
    tenant: str,
    agent: str,
    fresh_database: DatabaseHandle,
) -> None:
    client = client_of("model-gateway", "agent-runtime", fresh_database)
    run_id = uuid.uuid4()

    answer = client.post(route, json=body, headers=headers(tenant, agent, run_id))

    assert (answer.status_code, answer.json()) == (403, REFUSED)
    rows = owner_rows(
        fresh_database,
        "SELECT service, event, outcome, reason, tenant, agent, reference, "
        "suppressed, deployment FROM audit.events WHERE run_id = %s",
        (run_id,),
    )
    # Refused before the route was decided: no deployment in the row.
    assert rows == [
        (
            "model-gateway",
            "model.call",
            "refused",
            NAME_REASON,
            tenant,
            agent,
            "agent-runtime",
            0,
            None,
        )
    ]


@pytest.mark.parametrize(
    ("route", "body"), [("/v1/chat", CHAT), ("/v1/embeddings", EMBED)]
)
def test_the_runtime_naming_what_it_lists_is_answered_as_before(
    route: str, body: dict[str, Any], fresh_database: DatabaseHandle
) -> None:
    client = client_of("model-gateway", "agent-runtime", fresh_database)

    answer = client.post(route, json=body, headers=headers())

    assert answer.status_code == 200
    assert caller_rows(fresh_database) == []


@pytest.mark.parametrize(
    ("caller", "tenant", "agent", "status"),
    [
        ("knowledge-mcp", "claims-triage", "claims-triage", 200),
        ("knowledge-mcp", "claims-triage", "knowledge-ingestion", 403),
        ("knowledge-mcp", "evaluation", "claims-triage", 403),
        ("meridian-ingest", "claims-triage", "knowledge-ingestion", 200),
        ("meridian-ingest", "claims-triage", "claims-triage", 403),
        ("meridian-ingest", "evaluation", "knowledge-ingestion", 403),
    ],
)
def test_embeddings_from_the_knowledge_server_and_the_ingestion_job_name_their_own(
    caller: str, tenant: str, agent: str, status: int, fresh_database: DatabaseHandle
) -> None:
    client = client_of("model-gateway", caller, fresh_database)

    answer = client.post("/v1/embeddings", json=EMBED, headers=headers(tenant, agent))

    assert answer.status_code == status


def test_a_name_refusal_leaves_one_row_per_tenant_and_reason_per_window(
    fresh_database: DatabaseHandle,
) -> None:
    client = client_of("model-gateway", "agent-runtime", fresh_database)

    for _ in range(4):
        client.post("/v1/chat", json=CHAT, headers=headers("evaluation"))

    (row,) = caller_rows(fresh_database)
    assert (row[3], row[5]) == (NAME_REASON, 0)


def test_a_tenant_header_the_registry_does_not_hold_never_becomes_a_throttle_key(
    fresh_database: DatabaseHandle,
) -> None:
    client = client_of("model-gateway", "agent-runtime", fresh_database)

    for index in range(5):
        client.post("/v1/chat", json=CHAT, headers=headers(f"made-up-{index}"))

    # All five share the one window of tenants the registry does not know.
    assert len(caller_rows(fresh_database)) == 1


# ── the runtime: the tenant and the agent of a run ──────────────────────────
RUN = {
    "agent": "claims-triage",
    "tenant": "claims-triage",
    "reference": "CLM-0001",
    "input": {"claim": {"n": 1}},
}


@pytest.mark.parametrize(
    ("tenant", "agent"),
    [
        # Each is one the registry lets run, so only the caller's lists refuse.
        ("evaluation", "claims-triage"),
        ("claims-triage", "knowledge-ingestion"),  # a job agent, refused later too
    ],
)
def test_the_claims_api_naming_another_tenant_or_agent_for_a_run_is_403_and_audited(
    tenant: str, agent: str, fresh_database: DatabaseHandle
) -> None:
    client = client_of("agent-runtime", "claims-api", fresh_database)

    answer = client.post("/runs", json={**RUN, "tenant": tenant, "agent": agent})

    assert (answer.status_code, answer.json()) == (403, REFUSED)
    assert owner_rows(
        fresh_database,
        "SELECT service, event, outcome, reason, tenant, agent, reference "
        "FROM audit.events",
    ) == [
        (
            "agent-runtime",
            "run.refused",
            "refused",
            NAME_REASON,
            tenant,
            agent,
            "CLM-0001",
        )
    ]
    assert owner_rows(fresh_database, "SELECT 1 FROM runtime.runs") == []


def test_the_claims_api_naming_what_it_lists_gets_past_the_name_rule(
    fresh_database: DatabaseHandle,
) -> None:
    client = client_of("agent-runtime", "claims-api", fresh_database)

    answer = client.post("/runs", json={**RUN, "input": {"claim": {"n": 1}}})

    # Past the name rule it is an ordinary run request: the audit holds no
    # caller refusal, whatever became of the run.
    assert answer.json() != REFUSED
    assert caller_rows(fresh_database) == []


def resume(client: TestClient, run_id: uuid.UUID, tenant: str) -> httpx.Response:
    return client.post(
        f"/runs/{run_id}/resume",
        json={"tenant": tenant, "reference": "CLM-0001", "input": {}},
    )


def test_resuming_a_run_of_a_tenant_the_caller_may_not_name_does_not_say_it_exists(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = add_run(
        fresh_database,
        status="AwaitingApproval",
        tenant="evaluation",
        agent="claims-triage",
    )
    client = client_of("agent-runtime", "claims-api", fresh_database)

    existing = resume(client, run_id, "evaluation")
    absent = resume(client, uuid.uuid4(), "evaluation")

    # The same answer: no answer tells that a run of that tenant exists (T-10).
    assert (existing.status_code, existing.json()) == (403, REFUSED)
    assert (absent.status_code, absent.json()) == (403, REFUSED)
    assert (
        owner_rows(
            fresh_database,
            "SELECT event, tenant, agent, run_id FROM audit.events WHERE reason = %s",
            (NAME_REASON,),
        )
        == [("run.refused", "evaluation", None, None)] * 2
    )


def test_resuming_a_run_of_an_agent_the_caller_may_not_name_is_403_and_audited(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = add_run(
        fresh_database,
        status="AwaitingApproval",
        tenant="claims-triage",
        agent="knowledge-ingestion",
    )
    client = client_of("agent-runtime", "claims-api", fresh_database)

    answer = resume(client, run_id, "claims-triage")

    assert (answer.status_code, answer.json()) == (403, REFUSED)
    assert owner_rows(
        fresh_database,
        "SELECT event, reason, tenant, agent, run_id FROM audit.events "
        "WHERE run_id = %s",
        (run_id,),
    ) == [("run.refused", NAME_REASON, "claims-triage", "knowledge-ingestion", run_id)]


def test_resuming_what_the_caller_may_name_gets_the_ordinary_answer(
    fresh_database: DatabaseHandle,
) -> None:
    client = client_of("agent-runtime", "claims-api", fresh_database)

    answer = resume(client, uuid.uuid4(), "claims-triage")

    assert (answer.status_code, answer.json()) == (404, {"detail": "no such run"})
