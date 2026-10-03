"""POST /runs, POST /runs/{id}/resume and GET /runs/{id} with stub graphs (no
workload needed)."""

import contextlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, TypedDict, get_args

import httpx
import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from servicesupport import (
    GATEWAY_REPLY,
    REGISTRY_DIR,
    TESTS_ROOT,
    assert_spans_hold_no_exception_and_no_canary,
    audit_events,
    database_error,
    owner_rows,
)
from toolsupport import POLICY, policy_server, seed_world

import meridian.runtime as meridian_runtime
from meridian.platform.common import audit
from meridian.platform.common.db import connect
from meridian.platform.common.env import SettingsError
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.throttle import REFUSAL_AUDIT_SECONDS
from meridian.runtime import app as runtime_app
from meridian.runtime import graphs, runs
from meridian.runtime.app import create_app
from meridian.runtime.checkpoints import open_saver
from meridian.runtime.failures import GraphFailure
from meridian.runtime.graphs import GraphLoadError
from meridian.runtime.model_client import ModelClient
from meridian.runtime.models import RunState
from meridian.runtime.settings import RuntimeSettings
from meridian.runtime.tool_client import (
    ToolClient,
    ToolNotAllowed,
    ToolRefused,
    ToolUnavailable,
)

CLAIM_TEXT = "claimant-secret-text-42"


class State(TypedDict, total=False):
    claim: dict
    output: Any


class FakeEntryPoint:
    name = "claims-triage"
    value = "meridian.workloads.claims_triage.graph:build"

    class dist:
        name = "meridian"

    def __init__(
        self, factory: Callable[[ModelClient, ToolClient], StateGraph]
    ) -> None:
        self.factory = factory

    def load(self) -> Callable[[ModelClient, ToolClient], StateGraph]:
        return self.factory


def graph_of(node: Callable[[State], State]) -> StateGraph:
    graph = StateGraph(State)
    graph.add_node("work", node)
    graph.add_edge(START, "work")
    graph.add_edge("work", END)
    return graph


def ok_factory(model: ModelClient, tools: ToolClient) -> StateGraph:
    def work(state: State) -> State:
        reply = model.chat([{"role": "user", "content": "hi"}])
        return {"output": {"text": reply.text, "echo": state["claim"]["n"]}}

    return graph_of(work)


def register(monkeypatch: pytest.MonkeyPatch, factory: Callable) -> None:
    """Publish ``factory`` as the claims-triage graph for the next ``make_client``.

    The stand-in graphs live under tests/, outside the meridian package, so the
    loader's package-directory check is pointed at tests/ for these tests; the
    check itself is tested in test_graphs.py.
    """
    entry = FakeEntryPoint(factory)
    monkeypatch.setattr(graphs, "entry_points", lambda *, group: [entry])
    monkeypatch.setattr(graphs, "TRUSTED_ROOT", TESTS_ROOT)


class Gateway:
    """A stand-in gateway that remembers the requests it got."""

    def __init__(self, status: int = 200, raises: Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.raises = raises
        self.client = httpx.Client(
            base_url="http://gateway.invalid", transport=httpx.MockTransport(self)
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        body = GATEWAY_REPLY if self.status == 200 else {}
        return httpx.Response(self.status, json=body)


def make_client(
    db: DatabaseHandle | None,
    gateway: Gateway | None = None,
    exporter: InMemorySpanExporter | None = None,
    checkpointer: MemorySaver | None = None,
    tool_servers: Mapping[str, Any] | None = None,
    settings_servers: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    registry_dir: Path = REGISTRY_DIR,
) -> TestClient:
    dsn = db.dsn("agent_runtime") if db else "postgresql://agent_runtime@db.invalid/x"
    settings = RuntimeSettings(
        registry_dir=registry_dir,
        gateway_url="http://gateway.invalid",
        database_url=dsn,
        tool_servers=settings_servers or {},
    )
    app = create_app(
        settings,
        tracer_provider=make_tracer_provider("agent-runtime", exporter),
        http_client=(gateway or Gateway()).client,
        checkpointer=checkpointer,
        tool_servers=tool_servers,
        clock=clock,
    )
    return TestClient(app, raise_server_exceptions=False)


def start(client: TestClient, **overrides: Any) -> httpx.Response:
    body = {
        "agent": "claims-triage",
        "tenant": "claims-triage",
        "reference": "CLM-0001",
        "input": {"claim": {"n": 7, "text": CLAIM_TEXT}},
    } | overrides
    return client.post("/runs", json=body)


def read(client: TestClient, run_id: object, **overrides: str) -> httpx.Response:
    """GET a run's status as the tenant and reference ``start`` used."""
    params = {"tenant": "claims-triage", "reference": "CLM-0001"} | overrides
    return client.get(f"/runs/{run_id}", params=params)


def run_rows(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(
        db,
        "SELECT run_id, thread_id, agent, tenant, reference, status FROM runtime.runs",
    )


# ── the happy path ──────────────────────────────────────────────────────────
def test_a_run_completes_is_stored_audited_and_readable(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, ok_factory)
    gateway = Gateway()
    exporter = InMemorySpanExporter()
    client = make_client(fresh_database, gateway, exporter)

    response = start(client)

    assert response.status_code == 200
    body = response.json()
    run_id = uuid.UUID(body["run_id"])
    assert body["status"] == "Completed"
    assert body["output"] == {"text": "drafted", "echo": 7}
    ((row_run, thread, agent, tenant, reference, status),) = run_rows(fresh_database)
    assert (row_run, agent, tenant, reference, status) == (
        run_id,
        "claims-triage",
        "claims-triage",
        "CLM-0001",
        "Completed",
    )
    assert thread != run_id  # the caller never sees the thread
    events = audit_events(fresh_database, run_id)
    assert {e["service"] for e in events} == {"agent-runtime"}
    assert {(e["event"], e["outcome"]) for e in events} == {
        ("run.started", "started"),
        ("run.completed", "completed"),
    }
    assert {(e["tenant"], e["agent"], e["reference"]) for e in events} == {
        ("claims-triage", "claims-triage", "CLM-0001")
    }
    # A reason belongs to a failure, not to a run that went well.
    assert {(e["reason"], e["tool"]) for e in events} == {(None, None)}
    (request,) = gateway.requests
    assert request.headers["X-Meridian-Run"] == str(run_id)
    assert request.headers["X-Meridian-Tenant"] == "claims-triage"
    assert request.headers["X-Meridian-Agent"] == "claims-triage"
    (run_span,) = [s for s in exporter.get_finished_spans() if s.name == "runtime.run"]
    assert dict(run_span.attributes) == {
        "meridian.run_id": str(run_id),
        "meridian.agent": "claims-triage",
        "meridian.tenant": "claims-triage",
        "meridian.run_status": "Completed",
    }
    node_spans = [
        s for s in exporter.get_finished_spans() if s.name == "langgraph.node work"
    ]
    assert len(node_spans) == 1
    assert node_spans[0].context.trace_id == run_span.context.trace_id

    status_response = read(client, run_id)

    assert status_response.status_code == 200
    status_body = status_response.json()
    assert {
        k: status_body[k] for k in ("run_id", "agent", "tenant", "reference", "status")
    } == {
        "run_id": str(run_id),
        "agent": "claims-triage",
        "tenant": "claims-triage",
        "reference": "CLM-0001",
        "status": "Completed",
    }
    assert status_body["created_at"] <= status_body["updated_at"]


def test_no_audit_row_and_no_run_row_holds_the_input(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, ok_factory)

    response = start(make_client(fresh_database))

    run_id = uuid.UUID(response.json()["run_id"])
    assert CLAIM_TEXT not in str(audit_events(fresh_database, run_id))
    assert CLAIM_TEXT not in str(run_rows(fresh_database))


def test_a_graph_that_pauses_leaves_the_run_awaiting_approval(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def pausing(model: ModelClient, tools: ToolClient) -> StateGraph:
        return graph_of(lambda state: {"output": {"answer": interrupt("approve?")}})

    register(monkeypatch, pausing)

    response = start(make_client(fresh_database))

    assert response.status_code == 200
    assert response.json()["status"] == "AwaitingApproval"
    assert response.json()["output"] is None
    assert run_rows(fresh_database)[0][5] == "AwaitingApproval"
    run_id = uuid.UUID(response.json()["run_id"])
    assert "run.awaiting_approval" in {
        e["event"] for e in audit_events(fresh_database, run_id)
    }


# ── failures ────────────────────────────────────────────────────────────────
def test_a_graph_that_raises_answers_502_and_is_marked_failed(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            raise RuntimeError(f"boom {CLAIM_TEXT}")

        return graph_of(work)

    register(monkeypatch, failing)

    response = start(make_client(fresh_database))

    assert response.status_code == 502
    body = response.json()
    assert body == {"run_id": body["run_id"], "status": "Failed", "output": None}
    assert CLAIM_TEXT not in response.text
    assert "boom" not in response.text
    run_id = uuid.UUID(body["run_id"])
    assert run_rows(fresh_database)[0][5] == "Failed"
    events = {(e["event"], e["outcome"]) for e in audit_events(fresh_database, run_id)}
    assert events == {("run.started", "started"), ("run.failed", "failed")}
    assert CLAIM_TEXT not in str(audit_events(fresh_database, run_id))


def failed_row(db: DatabaseHandle, response: httpx.Response) -> dict:
    """The ``run.failed`` audit row of the run ``response`` answered for."""
    events = audit_events(db, uuid.UUID(response.json()["run_id"]))
    (row,) = [e for e in events if e["event"] == "run.failed"]
    return row


def test_a_failed_run_says_why_in_its_audit_row_and_its_log(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def failing(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            raise RuntimeError(f"boom {CLAIM_TEXT}")

        return graph_of(work)

    register(monkeypatch, failing)

    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        response = start(make_client(fresh_database))

    row = failed_row(fresh_database, response)
    assert (row["reason"], row["tool"]) == ("unexpected", None)
    assert "unexpected" in caplog.text
    assert response.json()["run_id"] in caplog.text
    # Not the exception's text: it could hold claim text.
    assert "boom" not in caplog.text
    assert CLAIM_TEXT not in caplog.text


def test_a_graph_failure_names_its_code_in_the_audit_row_and_the_log(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def failing(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            raise GraphFailure("some-code")

        return graph_of(work)

    register(monkeypatch, failing)

    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        response = start(make_client(fresh_database))

    assert response.status_code == 502
    assert response.json()["status"] == "Failed"
    row = failed_row(fresh_database, response)
    assert (row["reason"], row["tool"]) == ("some-code", None)
    assert "some-code" in caplog.text
    assert response.json()["run_id"] in caplog.text
    assert "some-code" not in response.text  # the caller is not told


def test_a_gateway_refusal_fails_the_run(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, ok_factory)

    response = start(make_client(fresh_database, Gateway(status=403)))

    assert response.status_code == 502
    assert response.json()["status"] == "Failed"
    assert failed_row(fresh_database, response)["reason"] == "model-error"


def test_an_output_that_is_not_an_object_fails_the_run(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(
        monkeypatch, lambda model, tools: graph_of(lambda s: {"output": "a string"})
    )

    response = start(make_client(fresh_database))

    assert response.status_code == 502


def test_a_graph_that_writes_no_output_completes_with_null(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, lambda model, tools: graph_of(lambda s: {}))

    response = start(make_client(fresh_database))

    assert (response.status_code, response.json()["output"]) == (200, None)


def test_the_recursion_limit_stops_a_looping_graph(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def looping(model: ModelClient, tools: ToolClient) -> StateGraph:
        graph = StateGraph(State)
        graph.add_node("spin", lambda s: {})
        graph.add_edge(START, "spin")
        graph.add_edge("spin", "spin")
        return graph

    register(monkeypatch, looping)

    response = start(make_client(fresh_database))

    assert response.status_code == 502
    assert response.json()["status"] == "Failed"


def calling_factory(calls: int) -> Callable[[ModelClient, ToolClient], StateGraph]:
    """A graph whose one node calls the model ``calls`` times: the recursion
    limit counts steps, so it does not bound these."""

    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            for _ in range(calls):
                model.chat([{"role": "user", "content": "hi"}])
            return {"output": {"calls": calls}}

        return graph_of(work)

    return factory


def test_a_run_may_call_the_model_as_often_as_the_limit_allows(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, calling_factory(runs.MAX_MODEL_CALLS_PER_RUN))
    gateway = Gateway()

    response = start(make_client(fresh_database, gateway))

    assert response.json()["status"] == "Completed"
    assert len(gateway.requests) == runs.MAX_MODEL_CALLS_PER_RUN


def test_a_graph_that_calls_the_model_past_the_limit_ends_failed(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, calling_factory(runs.MAX_MODEL_CALLS_PER_RUN + 1))
    gateway = Gateway()

    response = start(make_client(fresh_database, gateway))

    assert response.status_code == 502
    assert response.json()["status"] == "Failed"
    assert len(gateway.requests) == runs.MAX_MODEL_CALLS_PER_RUN
    ((_, _, _, _, _, status),) = run_rows(fresh_database)
    assert status == "Failed"
    row = failed_row(fresh_database, response)
    assert (row["reason"], row["tool"]) == ("model-call-limit", None)


def test_a_graph_that_calls_tools_past_the_limit_ends_failed_with_that_reason(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            # No server is configured, so each call is unavailable; the call
            # past the limit is the one that raises the limit.
            for _ in range(runs.MAX_TOOL_CALLS_PER_RUN + 1):
                with contextlib.suppress(ToolUnavailable):
                    tools.call("policy_lookup", {"policy_number": POLICY})
            return {"output": {}}

        return graph_of(work)

    register(monkeypatch, factory)

    response = start(make_client(fresh_database))

    assert response.status_code == 502
    row = failed_row(fresh_database, response)
    assert (row["reason"], row["tool"]) == ("tool-call-limit", "policy_lookup")


# ── authorisation and loading ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "overrides",
    [{"tenant": "no-such-tenant"}, {"agent": "rogue-agent"}],
)
def test_an_unknown_tenant_or_a_disallowed_agent_is_403_and_leaves_no_row(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
) -> None:
    register(monkeypatch, ok_factory)

    response = start(make_client(fresh_database), **overrides)

    assert response.status_code == 403
    assert run_rows(fresh_database) == []


def test_a_graph_that_cannot_be_loaded_stops_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(graphs, "entry_points", lambda *, group: [])

    with pytest.raises(GraphLoadError, match="no graph"):
        make_client(None)


def test_the_runtime_starts_with_the_real_registry_and_loads_graph_agents_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(monkeypatch, ok_factory)
    loaded: list[str] = []
    real = runtime_app.load_graph_factory

    def recording(agent_id: str, registry: Any) -> Any:
        loaded.append(agent_id)
        return real(agent_id, registry)

    monkeypatch.setattr(runtime_app, "load_graph_factory", recording)

    make_client(None)

    # knowledge-ingestion is in the registry as a job: it has no graph to find.
    assert loaded == ["claims-triage"]


def test_a_graph_agent_without_a_published_graph_still_stops_the_start(
    plant: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, ok_factory)  # publishes claims-triage only
    directory = plant(
        (
            "agents.yaml",
            "  - id: knowledge-ingestion\n",
            "  - id: ghost-graph\n    description: Has no graph.\n    tools: []\n"
            "  - id: knowledge-ingestion\n",
        )
    )

    with pytest.raises(GraphLoadError, match=r"no graph is published.*ghost-graph"):
        make_client(None, registry_dir=directory)


def test_a_run_for_a_job_agent_is_refused_audited_and_runs_no_graph(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_calls: list[str] = []

    def counting(model: ModelClient, tools: ToolClient) -> StateGraph:
        return graph_of(lambda state: node_calls.append("ran") or {"output": {}})

    register(monkeypatch, counting)
    gateway = Gateway()
    client = make_client(fresh_database, gateway)

    # The tenant lists the job agent, so the tenant check passes; the kind is
    # what stops the run.
    response = start(client, agent="knowledge-ingestion")

    assert response.status_code == 403
    assert response.json() == {"detail": "request refused"}
    assert node_calls == []
    assert gateway.requests == []
    assert run_rows(fresh_database) == []
    assert owner_rows(
        fresh_database,
        "SELECT service, event, outcome, reason, tenant, agent, run_id, reference "
        "FROM audit.events",
    ) == [
        (
            "agent-runtime",
            "run.refused",
            "refused",
            "not-a-graph-agent",
            "claims-triage",
            "knowledge-ingestion",
            None,
            "CLM-0001",
        )
    ]


def test_the_graph_factory_is_resolved_once_at_start(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, ok_factory)
    client = make_client(fresh_database)
    monkeypatch.setattr(graphs, "entry_points", lambda *, group: [])  # gone now

    assert start(client).status_code == 200


def test_a_refusal_is_audited_as_run_refused_and_leaves_no_run_row(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, ok_factory)

    response = start(make_client(fresh_database), tenant="no-such-tenant")

    assert response.status_code == 403
    assert response.json() == {"detail": "request refused"}
    rows = owner_rows(
        fresh_database,
        "SELECT service, event, outcome, tenant, agent, run_id, reference, db_role "
        "FROM audit.events",
    )
    assert rows == [
        (
            "agent-runtime",
            "run.refused",
            "refused",
            "no-such-tenant",
            "claims-triage",
            None,
            "CLM-0001",
            "agent_runtime",
        )
    ]
    assert run_rows(fresh_database) == []


def test_when_the_audit_write_of_a_refusal_fails_the_answer_is_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(monkeypatch, ok_factory)

    def no_database(*_a: object, **_k: object) -> None:
        raise database_error(CLAIM_TEXT)

    monkeypatch.setattr(audit, "connect", no_database)

    response = start(make_client(None), tenant="no-such-tenant")

    assert response.status_code == 503
    assert CLAIM_TEXT not in response.text


# ── database failure: nothing runs ──────────────────────────────────────────
def test_when_the_run_row_cannot_be_written_no_graph_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_calls: list[str] = []

    def counting(model: ModelClient, tools: ToolClient) -> StateGraph:
        return graph_of(lambda state: node_calls.append("ran") or {"output": {}})

    def no_database(*_a: object, **_k: object) -> None:
        raise psycopg.OperationalError(f"password=hunter2 {CLAIM_TEXT}")

    register(monkeypatch, counting)
    monkeypatch.setattr(runs, "connect", no_database)

    response = start(make_client(None, checkpointer=MemorySaver()))

    assert response.status_code == 503
    assert "hunter2" not in response.text
    assert CLAIM_TEXT not in response.text
    assert node_calls == []


def test_when_the_saver_cannot_connect_no_run_starts(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_calls: list[str] = []

    def counting(model: ModelClient, tools: ToolClient) -> StateGraph:
        return graph_of(lambda state: node_calls.append("ran") or {"output": {}})

    @contextlib.contextmanager
    def refused(dsn: str) -> Iterator[None]:
        raise psycopg.OperationalError(f"password=hunter2 {CLAIM_TEXT}")
        yield

    register(monkeypatch, counting)
    monkeypatch.setattr(runtime_app, "open_saver", refused)
    exporter = InMemorySpanExporter()

    response = start(make_client(fresh_database, exporter=exporter))

    # The answer start_run's own OperationalError gets, with no row and no run.
    assert response.status_code == 503
    assert "hunter2" not in response.text
    assert CLAIM_TEXT not in response.text
    assert node_calls == []
    assert run_rows(fresh_database) == []
    assert_spans_hold_no_exception_and_no_canary(exporter, CLAIM_TEXT)
    (run_span,) = [s for s in exporter.get_finished_spans() if s.name == "runtime.run"]
    assert run_span.status.status_code is StatusCode.ERROR


# ── GET /runs/{id} ──────────────────────────────────────────────────────────
def test_an_unknown_run_is_404(fresh_database: DatabaseHandle) -> None:
    response = read(make_client(fresh_database), uuid.uuid4())

    assert response.status_code == 404


def test_a_malformed_run_id_is_422(fresh_database: DatabaseHandle) -> None:
    assert read(make_client(fresh_database), "not-a-uuid").status_code == 422


def test_a_read_with_the_runs_tenant_and_reference_is_200(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, ok_factory)
    client = make_client(fresh_database)
    run_id = start(client).json()["run_id"]

    response = read(client, run_id)

    assert response.status_code == 200
    assert response.json()["run_id"] == run_id


@pytest.mark.parametrize(
    "overrides",
    [{"tenant": "evaluation"}, {"reference": "CLM-0002"}, {"run_id": "unknown"}],
    ids=["wrong-tenant", "wrong-reference", "unknown-run"],
)
def test_a_read_naming_the_wrong_tenant_reference_or_run_gets_one_404_body(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
) -> None:
    register(monkeypatch, ok_factory)
    client = make_client(fresh_database)
    run_id = start(client).json()["run_id"]
    fields = dict(overrides)
    asked = uuid.uuid4() if fields.pop("run_id", None) else run_id

    response = read(client, asked, **fields)

    # No answer says that a run ID exists under another tenant (T-10): the body
    # is the unknown run's, and the resume's.
    assert response.status_code == 404
    assert response.json() == {"detail": "no such run"}
    assert response.json() == read(client, uuid.uuid4()).json()
    assert response.text == read(client, uuid.uuid4()).text


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"tenant": "claims-triage"},
        {"reference": "CLM-0001"},
        {"tenant": "claims-triage", "reference": "not valid!"},
    ],
    ids=["neither", "no-reference", "no-tenant", "malformed-reference"],
)
def test_a_read_without_a_valid_tenant_and_reference_is_422(
    fresh_database: DatabaseHandle, params: dict[str, str]
) -> None:
    response = make_client(fresh_database).get(f"/runs/{uuid.uuid4()}", params=params)

    assert response.status_code == 422


# ── request validation ──────────────────────────────────────────────────────
def input_of_size(total: int) -> dict[str, str]:
    """A JSON object whose compact serialisation is exactly ``total`` bytes."""
    overhead = len(json.dumps({"k": ""}, separators=(",", ":")))
    return {"k": "x" * (total - overhead)}


@pytest.mark.parametrize(
    "overrides",
    [
        {"extra": 1},
        {"reference": "has space"},
        {"reference": "x" * 65},
        {"reference": ""},
        {"agent": "Bad_Agent"},
        {"tenant": "t" * 65},
        {"input": []},
        {"input": "text"},
        {"input": input_of_size(32 * 1024 + 1)},
    ],
)
def test_an_invalid_run_request_is_422_and_echoes_nothing(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    register(monkeypatch, ok_factory)

    response = start(make_client(None), **overrides)

    assert response.status_code == 422
    assert "xxxxxxxx" not in response.text


def test_an_input_of_exactly_32_kib_is_accepted(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, lambda model, tools: graph_of(lambda s: {"output": {}}))

    response = start(make_client(fresh_database), input=input_of_size(32 * 1024))

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("text", "unit_bytes"), [("\U0001f600", 4), ("\u4e2d", 3), ("\u00e9", 2)]
)
def test_the_input_size_counts_utf8_bytes_not_escapes(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    unit_bytes: int,
) -> None:
    register(monkeypatch, lambda model, tools: graph_of(lambda s: {"output": {}}))
    overhead = len(json.dumps({"k": ""}, separators=(",", ":")))
    exact = {"k": text * ((32 * 1024 - overhead) // unit_bytes)}
    padding = 32 * 1024 - overhead - len(exact["k"].encode("utf-8"))
    exact["k"] += "x" * padding  # exactly 32 KiB of UTF-8
    over = {"k": exact["k"] + "x"}
    client = make_client(fresh_database)

    accepted = start(client, input=exact)
    refused = start(client, input=over)

    assert len(
        json.dumps(exact, ensure_ascii=False, separators=(",", ":")).encode()
    ) == (32 * 1024)
    assert accepted.status_code == 200
    assert refused.status_code == 422


# ── span hygiene (T-03) ─────────────────────────────────────────────────────
def test_a_database_error_in_the_run_span_leaves_no_message_in_any_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(monkeypatch, ok_factory)

    def refuse(*_a: object, **_k: object) -> None:
        raise database_error(CLAIM_TEXT)

    monkeypatch.setattr(runs, "connect", refuse)
    exporter = InMemorySpanExporter()

    # A saver is injected: the PostgreSQL one would be refused first (below).
    response = start(make_client(None, exporter=exporter, checkpointer=MemorySaver()))

    assert response.status_code == 500
    assert CLAIM_TEXT not in response.text
    assert_spans_hold_no_exception_and_no_canary(exporter, CLAIM_TEXT)
    (run_span,) = [s for s in exporter.get_finished_spans() if s.name == "runtime.run"]
    assert run_span.status.status_code is StatusCode.ERROR
    assert run_span.status.description == "NotNullViolation"


def test_a_failed_run_marks_its_span_as_an_error_with_the_class_name_only(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            raise RuntimeError(f"boom {CLAIM_TEXT}")

        return graph_of(work)

    register(monkeypatch, failing)
    exporter = InMemorySpanExporter()

    start(make_client(fresh_database, exporter=exporter))

    (run_span,) = [s for s in exporter.get_finished_spans() if s.name == "runtime.run"]
    assert run_span.status.status_code is StatusCode.ERROR
    assert run_span.status.description == "RuntimeError"
    assert run_span.attributes["meridian.run_status"] == "Failed"
    assert_spans_hold_no_exception_and_no_canary(exporter, CLAIM_TEXT)


# ── a run never stays Running without a trace of why ────────────────────────
class FlakyFinish:
    """Stands in for runs.finish_run: fails ``failures`` times, then works."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.real = runs.finish_run

    def __call__(
        self, dsn: str, identity: runs.RunIdentity, status: str, **why: str | None
    ) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise psycopg.OperationalError(f"down {CLAIM_TEXT}")
        self.real(dsn, identity, status, **why)


def test_the_retry_of_finish_run_keeps_the_reason_of_a_failed_run(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            raise GraphFailure("a-code")

        return graph_of(work)

    register(monkeypatch, failing)
    flaky = FlakyFinish(failures=1)
    monkeypatch.setattr(runs, "finish_run", flaky)

    response = start(make_client(fresh_database))

    assert (response.status_code, flaky.calls) == (502, 2)
    assert failed_row(fresh_database, response)["reason"] == "a-code"


def test_finish_run_takes_a_reason_for_a_failed_run_only() -> None:
    identity = runs.RunIdentity(
        run_id=uuid.uuid4(),
        thread_id=uuid.uuid4(),
        agent="claims-triage",
        tenant="claims-triage",
        reference="CLM-0001",
    )

    with pytest.raises(ValueError, match="Failed"):
        runs.finish_run("postgresql://nowhere.invalid/x", identity, "Completed", "r")
    with pytest.raises(ValueError, match="Failed"):
        runs.finish_run(
            "postgresql://nowhere.invalid/x", identity, "Completed", tool="t"
        )


def test_finish_run_is_retried_once(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, ok_factory)
    flaky = FlakyFinish(failures=1)
    monkeypatch.setattr(runs, "finish_run", flaky)

    response = start(make_client(fresh_database))

    assert response.status_code == 200
    assert flaky.calls == 2
    assert run_rows(fresh_database)[0][5] == "Completed"


def test_when_finish_run_keeps_failing_the_answer_is_503_with_the_run_id(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    register(monkeypatch, ok_factory)
    flaky = FlakyFinish(failures=99)
    monkeypatch.setattr(runs, "finish_run", flaky)

    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        response = start(make_client(fresh_database))

    assert response.status_code == 503
    assert flaky.calls == 2
    body = response.json()
    assert set(body) == {"detail", "run_id"}
    assert uuid.UUID(body["run_id"])
    assert CLAIM_TEXT not in response.text
    assert body["run_id"] in caplog.text
    assert "Completed" in caplog.text  # the status it should have had
    assert CLAIM_TEXT not in caplog.text
    assert run_rows(fresh_database)[0][5] == "Running"


# ── failure status: a timeout is a 504, anything else a 502 ─────────────────
def test_a_gateway_timeout_fails_the_run_with_504(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, ok_factory)
    gateway = Gateway(raises=httpx.ReadTimeout("slow"))

    response = start(make_client(fresh_database, gateway))

    assert response.status_code == 504
    assert response.json()["status"] == "Failed"
    assert run_rows(fresh_database)[0][5] == "Failed"
    assert failed_row(fresh_database, response)["reason"] == "model-timeout"


def test_the_log_of_a_refused_gateway_call_names_the_status_code(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    register(monkeypatch, ok_factory)

    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        start(make_client(fresh_database, Gateway(status=403)))

    assert "ModelCallError" in caplog.text
    assert "403" in caplog.text
    assert "model-error" in caplog.text


# ── checkpoints hold claimant data, so they are dropped with the run ────────
def test_the_checkpoint_is_deleted_when_a_run_completes(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, ok_factory)
    saver = MemorySaver()

    response = start(make_client(fresh_database, checkpointer=saver))

    assert response.json()["status"] == "Completed"
    assert list(saver.list(None)) == []


def test_the_checkpoint_is_deleted_when_a_run_fails(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            raise RuntimeError("boom")

        return graph_of(work)

    register(monkeypatch, failing)
    saver = MemorySaver()

    response = start(make_client(fresh_database, checkpointer=saver))

    assert response.json()["status"] == "Failed"
    assert list(saver.list(None)) == []


def test_the_checkpoint_is_kept_while_the_run_awaits_approval(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(
        monkeypatch,
        lambda model, tools: graph_of(
            lambda s: {"output": {"a": interrupt("approve?")}}
        ),
    )
    saver = MemorySaver()

    response = start(make_client(fresh_database, checkpointer=saver))

    assert response.json()["status"] == "AwaitingApproval"
    assert list(saver.list(None)) != []


# ── the PostgreSQL saver (no injected checkpointer) ─────────────────────────
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def pausing_factory(model: ModelClient, tools: ToolClient) -> StateGraph:
    return graph_of(lambda state: {"output": {"answer": interrupt("approve?")}})


def checkpoint_counts(db: DatabaseHandle, thread_id: uuid.UUID) -> dict[str, int]:
    """The rows of one thread in each checkpoint table, read as the owner."""
    return {
        table: owner_rows(
            db,
            f"SELECT count(*) FROM runtime.{table} WHERE thread_id = %s",  # noqa: S608
            (str(thread_id),),
        )[0][0]
        for table in CHECKPOINT_TABLES
    }


def record_puts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count the checkpoints the PostgreSQL saver writes, so that a test of
    their deletion can tell a deletion from nothing ever having been written."""
    puts: list[str] = []
    real_put = PostgresSaver.put

    def recording_put(self: PostgresSaver, *args: Any, **kwargs: Any) -> Any:
        puts.append("put")
        return real_put(self, *args, **kwargs)

    monkeypatch.setattr(PostgresSaver, "put", recording_put)
    return puts


def the_only_thread(db: DatabaseHandle) -> uuid.UUID:
    ((_, thread_id, *_),) = run_rows(db)
    return thread_id


def test_a_run_that_completes_leaves_no_checkpoint_row(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, ok_factory)
    puts = record_puts(monkeypatch)

    response = start(make_client(fresh_database))

    assert response.json()["status"] == "Completed"
    assert puts, "the run never wrote a checkpoint, so none could be deleted"
    assert checkpoint_counts(fresh_database, the_only_thread(fresh_database)) == {
        table: 0 for table in CHECKPOINT_TABLES
    }


def test_a_run_that_fails_leaves_no_checkpoint_row(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            raise RuntimeError("boom")

        return graph_of(work)

    register(monkeypatch, failing)
    puts = record_puts(monkeypatch)

    response = start(make_client(fresh_database))

    assert response.json()["status"] == "Failed"
    assert puts, "the run never wrote a checkpoint, so none could be deleted"
    assert checkpoint_counts(fresh_database, the_only_thread(fresh_database)) == {
        table: 0 for table in CHECKPOINT_TABLES
    }


def test_a_run_that_awaits_approval_keeps_its_checkpoint_rows(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, pausing_factory)

    response = start(make_client(fresh_database))

    assert response.json()["status"] == "AwaitingApproval"
    counts = checkpoint_counts(fresh_database, the_only_thread(fresh_database))
    assert all(count > 0 for count in counts.values()), counts


def test_a_second_app_on_the_same_database_sees_the_paused_thread(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, pausing_factory)
    paused = start(make_client(fresh_database))
    thread_id = the_only_thread(fresh_database)
    register(monkeypatch, ok_factory)
    second = make_client(fresh_database)  # another app on the same database

    finished = start(second)
    with open_saver(fresh_database.dsn("agent_runtime")) as saver:
        graph = pausing_factory(None, None).compile(checkpointer=saver)
        snapshot = graph.get_state({"configurable": {"thread_id": str(thread_id)}})

    assert paused.json()["status"] == "AwaitingApproval"
    # The second app's own run ended and cleaned up after itself only.
    assert finished.json()["status"] == "Completed"
    assert [item.value for item in snapshot.interrupts] == ["approve?"]
    assert snapshot.next == ("work",)


class DeleteRefused(PostgresSaver):
    """A saver whose delete fails, as a database that went away would."""

    def delete_thread(self, thread_id: str) -> None:
        raise psycopg.errors.QueryCanceled(f"password=hunter2 {CLAIM_TEXT}")


def test_a_failed_delete_is_logged_and_changes_no_answer(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    @contextlib.contextmanager
    def refusing_delete(dsn: str) -> Iterator[PostgresSaver]:
        with open_saver(dsn) as real:
            yield DeleteRefused(real.conn)

    register(monkeypatch, ok_factory)
    monkeypatch.setattr(runtime_app, "open_saver", refusing_delete)

    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        response = start(make_client(fresh_database))

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "Completed"
    ((run_id, _, _, _, _, status),) = run_rows(fresh_database)
    assert (str(run_id), status) == (body["run_id"], "Completed")
    assert body["run_id"] in caplog.text
    assert "QueryCanceled" in caplog.text
    assert "57014" in caplog.text
    assert "hunter2" not in caplog.text
    assert CLAIM_TEXT not in caplog.text
    assert "run.completed" in {e["event"] for e in audit_events(fresh_database, run_id)}


DELETE_LOG = "its checkpoints were not deleted"


class DeleteFailsOnce(PostgresSaver):
    """A saver whose deletes fail with the errors in ``failures``, shared by
    every instance and spent in order, and work once none is left."""

    def __init__(self, conn: psycopg.Connection, failures: list[Exception]) -> None:
        super().__init__(conn)
        self.failures = failures

    def delete_thread(self, thread_id: str) -> None:
        if self.failures:
            raise self.failures.pop(0)
        super().delete_thread(thread_id)


def failing_delete_scope(
    monkeypatch: pytest.MonkeyPatch, failures: list[Exception]
) -> list[psycopg.Connection]:
    """Make the app's savers fail their next deletes as ``failures`` says; the
    connection of each saver it opens is returned, in order."""
    opened: list[psycopg.Connection] = []

    @contextlib.contextmanager
    def failing(dsn: str) -> Iterator[PostgresSaver]:
        with open_saver(dsn) as real:
            opened.append(real.conn)
            yield DeleteFailsOnce(real.conn, failures)

    monkeypatch.setattr(runtime_app, "open_saver", failing)
    return opened


def test_a_failed_delete_is_retried_once_on_a_fresh_saver(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    register(monkeypatch, ok_factory)
    opened = failing_delete_scope(
        monkeypatch, [psycopg.errors.QueryCanceled(f"password=hunter2 {CLAIM_TEXT}")]
    )

    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        response = start(make_client(fresh_database))

    assert (response.status_code, response.json()["status"]) == (200, "Completed")
    assert len(opened) == 2  # the run's saver, and the retry's own
    assert opened[0] is not opened[1]
    assert checkpoint_counts(fresh_database, the_only_thread(fresh_database)) == {
        table: 0 for table in CHECKPOINT_TABLES
    }
    assert caplog.text.count(DELETE_LOG) <= 1
    assert "hunter2" not in caplog.text
    assert CLAIM_TEXT not in caplog.text


def test_when_both_deletes_fail_each_is_logged_and_the_answer_is_unchanged(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    register(monkeypatch, ok_factory)
    failing_delete_scope(
        monkeypatch,
        [
            psycopg.errors.QueryCanceled(f"password=hunter2 {CLAIM_TEXT}"),
            psycopg.errors.AdminShutdown(f"password=hunter2 {CLAIM_TEXT}"),
        ],
    )

    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        response = start(make_client(fresh_database))

    assert (response.status_code, response.json()["status"]) == (200, "Completed")
    assert run_rows(fresh_database)[0][5] == "Completed"
    assert caplog.text.count(DELETE_LOG) == 2
    assert "QueryCanceled" in caplog.text
    assert "57014" in caplog.text
    assert "AdminShutdown" in caplog.text
    assert "57P01" in caplog.text
    assert response.json()["run_id"] in caplog.text
    assert "hunter2" not in caplog.text
    assert CLAIM_TEXT not in caplog.text
    # What the two attempts could not drop is still there.
    counts = checkpoint_counts(fresh_database, the_only_thread(fresh_database))
    assert all(count > 0 for count in counts.values()), counts


def test_a_delete_that_fails_with_an_error_of_another_library_is_caught(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    register(monkeypatch, ok_factory)
    failing_delete_scope(
        monkeypatch,
        [RuntimeError(f"boom {CLAIM_TEXT}"), ValueError(f"boom {CLAIM_TEXT}")],
    )

    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        response = start(make_client(fresh_database))

    assert (response.status_code, response.json()["status"]) == (200, "Completed")
    assert "RuntimeError" in caplog.text
    assert "ValueError" in caplog.text
    assert "sqlstate none" in caplog.text
    assert "boom" not in caplog.text
    assert CLAIM_TEXT not in caplog.text


def test_the_retry_of_a_delete_uses_the_injected_checkpointer_again(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailsOnce(MemorySaver):
        deletes = 0

        def delete_thread(self, thread_id: str) -> None:
            self.deletes += 1
            if self.deletes == 1:
                raise RuntimeError("down")
            super().delete_thread(thread_id)

    register(monkeypatch, ok_factory)
    saver = FailsOnce()

    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        response = start(make_client(fresh_database, checkpointer=saver))

    assert response.json()["status"] == "Completed"
    assert saver.deletes == 2
    assert list(saver.list(None)) == []


def test_the_final_status_is_recorded_before_the_checkpoints_are_deleted(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    class Spying(PostgresSaver):
        def delete_thread(self, thread_id: str) -> None:
            seen.append(run_rows(fresh_database)[0][5])
            super().delete_thread(thread_id)

    @contextlib.contextmanager
    def spying(dsn: str) -> Iterator[PostgresSaver]:
        with open_saver(dsn) as real:
            yield Spying(real.conn)

    register(monkeypatch, ok_factory)
    monkeypatch.setattr(runtime_app, "open_saver", spying)

    response = start(make_client(fresh_database))

    assert response.json()["status"] == "Completed"
    assert seen == ["Completed"]


# ── resuming a paused run (S015) ────────────────────────────────────────────
APPROVAL = {"approved": True}
BEFORE = {"stage": "before"}


def resumable(
    pauses: int = 1, after: Callable[[Any], None] | None = None
) -> Callable[[ModelClient, ToolClient], StateGraph]:
    """A graph that writes an output, then pauses ``pauses`` times in a second
    node; once resumed through them all it calls ``after`` with the last resume
    value and puts that value in its output. A node restarts from its top on a
    resume, so ``after`` runs once per finished run, not once per pause."""

    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        def before(state: State) -> State:
            return {"output": BEFORE}

        def decide(state: State) -> State:
            answer = None
            for number in range(pauses):
                answer = interrupt(f"approve {number}")
            if after is not None:
                after(answer)
            return {"output": {"stage": "after", "answer": answer}}

        graph = StateGraph(State)
        graph.add_node("before", before)
        graph.add_node("decide", decide)
        graph.add_edge(START, "before")
        graph.add_edge("before", "decide")
        graph.add_edge("decide", END)
        return graph

    return factory


def resume(client: TestClient, run_id: str, **overrides: Any) -> httpx.Response:
    body = {
        "tenant": "claims-triage",
        "reference": "CLM-0001",
        "input": APPROVAL,
    } | overrides
    return client.post(f"/runs/{run_id}/resume", json=body)


def paused_run(client: TestClient) -> str:
    response = start(client)
    assert response.json()["status"] == "AwaitingApproval"
    return response.json()["run_id"]


def audit_count(db: DatabaseHandle) -> int:
    return owner_rows(db, "SELECT count(*) FROM audit.events")[0][0]


def event_names(db: DatabaseHandle, run_id: str) -> list[str]:
    return [e["event"] for e in audit_events(db, uuid.UUID(run_id))]


def test_a_paused_run_answers_with_the_output_so_far_and_resumes_to_completion(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable())
    client = make_client(fresh_database)

    paused = start(client)

    assert paused.status_code == 200
    assert paused.json()["status"] == "AwaitingApproval"
    assert paused.json()["output"] == BEFORE
    run_id = paused.json()["run_id"]
    thread = the_only_thread(fresh_database)

    resumed = resume(client, run_id)

    assert resumed.status_code == 200
    assert resumed.json() == {
        "run_id": run_id,
        "status": "Completed",
        "output": {"stage": "after", "answer": APPROVAL},
    }
    assert run_rows(fresh_database)[0][5] == "Completed"
    assert read(client, run_id).json()["status"] == "Completed"
    assert checkpoint_counts(fresh_database, thread) == {
        table: 0 for table in CHECKPOINT_TABLES
    }
    events = audit_events(fresh_database, uuid.UUID(run_id))
    assert [(e["event"], e["outcome"]) for e in events] == [
        ("run.started", "started"),
        ("run.awaiting_approval", "paused"),
        ("run.resumed", "resumed"),
        ("run.completed", "completed"),
    ]
    assert {(e["tenant"], e["agent"], e["reference"]) for e in events} == {
        ("claims-triage", "claims-triage", "CLM-0001")
    }
    assert {(e["reason"], e["tool"]) for e in events} == {(None, None)}


def test_a_resume_runs_under_a_span_with_the_attributes_of_a_run_span(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable())
    exporter = InMemorySpanExporter()
    client = make_client(fresh_database, exporter=exporter)
    run_id = paused_run(client)

    resume(client, run_id)

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "runtime.resume"]
    assert dict(span.attributes) == {
        "meridian.run_id": run_id,
        "meridian.agent": "claims-triage",
        "meridian.tenant": "claims-triage",
        "meridian.run_status": "Completed",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant": "evaluation"},
        {"reference": "CLM-0002"},
        {"run_id": str(uuid.uuid4())},
    ],
    ids=["wrong-tenant", "wrong-reference", "unknown-run"],
)
def test_a_resume_naming_the_wrong_tenant_reference_or_run_is_404_and_changes_nothing(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
) -> None:
    register(monkeypatch, resumable())
    client = make_client(fresh_database)
    run_id = paused_run(client)
    thread = the_only_thread(fresh_database)
    rows_before = run_rows(fresh_database)
    audit_before = audit_count(fresh_database)
    fields = dict(overrides)  # the parametrized dict is shared between runs
    asked = fields.pop("run_id", run_id)

    response = resume(client, asked, **fields)

    assert response.status_code == 404
    # The answer a GET of a run that does not exist gets: no answer tells a
    # caller that the run ID exists under another tenant (T-10).
    assert response.json() == read(client, uuid.uuid4()).json()
    assert response.json() == {"detail": "no such run"}
    assert run_rows(fresh_database) == rows_before
    assert audit_count(fresh_database) == audit_before
    counts = checkpoint_counts(fresh_database, thread)
    assert all(count > 0 for count in counts.values()), counts


def test_a_run_resumed_twice_runs_its_graph_once_and_the_second_answer_has_no_output(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    resumed_parts: list[Any] = []
    register(monkeypatch, resumable(after=resumed_parts.append))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    first = resume(client, run_id)
    audit_before = audit_count(fresh_database)

    second = resume(client, run_id)

    assert first.json()["status"] == "Completed"
    assert second.status_code == 200
    assert second.json() == {"run_id": run_id, "status": "Completed", "output": None}
    assert resumed_parts == [APPROVAL]
    assert audit_count(fresh_database) == audit_before


def test_a_failed_run_resumed_answers_its_status_and_runs_nothing(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_calls: list[str] = []

    def failing(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            node_calls.append("ran")
            raise RuntimeError("boom")

        return graph_of(work)

    register(monkeypatch, failing)
    client = make_client(fresh_database)
    failed = start(client)
    audit_before = audit_count(fresh_database)

    response = resume(client, failed.json()["run_id"])

    assert failed.status_code == 502
    assert response.status_code == 200
    assert response.json() == {
        "run_id": failed.json()["run_id"],
        "status": "Failed",
        "output": None,
    }
    assert node_calls == ["ran"]
    assert audit_count(fresh_database) == audit_before


def test_two_resumes_at_the_same_time_run_the_resumed_part_once_and_both_answer_200(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    resumed_parts: list[Any] = []

    def slowly(answer: Any) -> None:
        resumed_parts.append(answer)
        time.sleep(0.3)  # long enough for the other request to arrive meanwhile

    register(monkeypatch, resumable(after=slowly))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    start_together = threading.Barrier(2)
    answers: list[httpx.Response] = []

    def send() -> None:
        own = TestClient(client.app, raise_server_exceptions=False)
        start_together.wait()
        answers.append(resume(own, run_id))

    racers = [threading.Thread(target=send) for _ in range(2)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()

    assert [a.status_code for a in answers] == [200, 200]
    assert resumed_parts == [APPROVAL]
    assert sorted(a.json()["output"] is None for a in answers) == [False, True]
    assert run_rows(fresh_database)[0][5] == "Completed"
    assert event_names(fresh_database, run_id) == [
        "run.started",
        "run.awaiting_approval",
        "run.resumed",
        "run.completed",
    ]


def test_a_resume_that_loses_the_claim_answers_the_current_status_and_runs_nothing(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    resumed_parts: list[Any] = []
    register(monkeypatch, resumable(after=resumed_parts.append))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    real = runs.claim_paused_run

    def another_request_wins_first(*args: Any) -> runs.RunIdentity | None:
        assert real(*args) is not None  # the winner, between the read and the claim
        return real(*args)

    monkeypatch.setattr(runs, "claim_paused_run", another_request_wins_first)

    response = resume(client, run_id)

    assert response.status_code == 200
    assert response.json() == {"run_id": run_id, "status": "Running", "output": None}
    assert resumed_parts == []
    assert event_names(fresh_database, run_id) == [
        "run.started",
        "run.awaiting_approval",
        "run.resumed",
    ]


def test_only_one_of_many_claims_of_a_paused_run_succeeds(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable())
    run_id = paused_run(make_client(fresh_database))
    dsn = fresh_database.dsn("agent_runtime")
    claimants = 8
    start_together = threading.Barrier(claimants)
    claimed: list[runs.RunIdentity | None] = []

    def claim() -> None:
        start_together.wait()
        claimed.append(
            runs.claim_paused_run(dsn, uuid.UUID(run_id), "claims-triage", "CLM-0001")
        )

    racers = [threading.Thread(target=claim) for _ in range(claimants)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()

    winners = [identity for identity in claimed if identity is not None]
    assert len(claimed) == claimants
    assert len(winners) == 1
    assert winners[0].run_id == uuid.UUID(run_id)
    assert winners[0].thread_id == the_only_thread(fresh_database)
    assert event_names(fresh_database, run_id).count("run.resumed") == 1


def test_a_tenant_the_registry_no_longer_allows_is_refused_and_the_run_stays_paused(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    plant: Callable[..., Path],
) -> None:
    register(monkeypatch, resumable())
    run_id = paused_run(make_client(fresh_database))
    directory = plant(
        (
            "tenants.yaml",
            "    agents: [claims-triage, knowledge-ingestion]\n",
            "    agents: [knowledge-ingestion]\n",
        )
    )
    narrowed = make_client(fresh_database, registry_dir=directory)

    response = resume(narrowed, run_id)

    assert response.status_code == 403
    assert response.json() == {"detail": "request refused"}
    assert run_rows(fresh_database)[0][5] == "AwaitingApproval"
    refusal = audit_events(fresh_database, uuid.UUID(run_id))[-1]
    assert (refusal["event"], refusal["outcome"], refusal["reason"]) == (
        "run.refused",
        "refused",
        None,
    )
    assert (refusal["tenant"], refusal["agent"], refusal["reference"]) == (
        "claims-triage",
        "claims-triage",
        "CLM-0001",
    )
    assert event_names(fresh_database, run_id)[:2] == [
        "run.started",
        "run.awaiting_approval",
    ]


def test_a_resumed_graph_that_raises_leaves_the_run_paused_with_502(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def raising(answer: Any) -> None:
        raise RuntimeError(f"boom {CLAIM_TEXT}")

    register(monkeypatch, resumable(after=raising))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    thread = the_only_thread(fresh_database)

    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        response = resume(client, run_id)

    assert response.status_code == 502
    assert response.json() == {
        "run_id": run_id,
        "status": "AwaitingApproval",
        "output": None,
    }
    assert CLAIM_TEXT not in response.text
    assert run_rows(fresh_database)[0][5] == "AwaitingApproval"
    events = audit_events(fresh_database, uuid.UUID(run_id))
    assert [(e["event"], e["outcome"]) for e in events] == [
        ("run.started", "started"),
        ("run.awaiting_approval", "paused"),
        ("run.resumed", "resumed"),
        ("run.resume_failed", "paused"),
    ]
    assert (events[-1]["reason"], events[-1]["tool"]) == ("unexpected", None)
    assert CLAIM_TEXT not in str(events)
    assert run_id in caplog.text
    assert CLAIM_TEXT not in caplog.text
    counts = checkpoint_counts(fresh_database, thread)
    assert all(count > 0 for count in counts.values()), counts


class ToolThatIsDown:
    """What a resumed node does after its pause: it fails while the tool is
    down, and once it is up its work is recorded in ``done``."""

    def __init__(self) -> None:
        self.up = False
        self.done: list[Any] = []

    def __call__(self, answer: Any) -> None:
        if not self.up:
            raise ToolUnavailable("policy_lookup")
        self.done.append(answer)


def test_a_resumed_leg_whose_tool_fails_leaves_the_run_paused_and_resumable(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tool = ToolThatIsDown()
    register(monkeypatch, resumable(after=tool))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    thread = the_only_thread(fresh_database)

    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        failed = resume(client, run_id)

    assert failed.status_code == 502
    assert failed.json() == {
        "run_id": run_id,
        "status": "AwaitingApproval",
        "output": None,
    }
    assert run_rows(fresh_database)[0][5] == "AwaitingApproval"
    assert read(client, run_id).json()["status"] == "AwaitingApproval"
    events = audit_events(fresh_database, uuid.UUID(run_id))
    assert [(e["event"], e["outcome"]) for e in events] == [
        ("run.started", "started"),
        ("run.awaiting_approval", "paused"),
        ("run.resumed", "resumed"),
        ("run.resume_failed", "paused"),
    ]
    assert (events[-1]["reason"], events[-1]["tool"]) == (
        "tool-unavailable",
        "policy_lookup",
    )
    assert {e["event"] for e in events} & {"run.failed", "run.completed"} == set()
    counts = checkpoint_counts(fresh_database, thread)
    assert all(count > 0 for count in counts.values()), counts

    tool.up = True
    second = resume(client, run_id)

    assert second.status_code == 200
    assert second.json() == {
        "run_id": run_id,
        "status": "Completed",
        "output": {"stage": "after", "answer": APPROVAL},
    }
    assert tool.done == [APPROVAL]  # the node's work happened once
    assert run_rows(fresh_database)[0][5] == "Completed"
    assert event_names(fresh_database, run_id) == [
        "run.started",
        "run.awaiting_approval",
        "run.resumed",
        "run.resume_failed",
        "run.resumed",
        "run.completed",
    ]
    assert checkpoint_counts(fresh_database, thread) == {
        table: 0 for table in CHECKPOINT_TABLES
    }


def test_a_resumed_leg_that_fails_again_after_a_failed_one_is_paused_again(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = ToolThatIsDown()
    register(monkeypatch, resumable(after=tool))
    client = make_client(fresh_database)
    run_id = paused_run(client)

    first = resume(client, run_id)
    second = resume(client, run_id)

    assert [first.status_code, second.status_code] == [502, 502]
    assert second.json()["status"] == "AwaitingApproval"
    assert event_names(fresh_database, run_id).count("run.resume_failed") == 2


def test_the_vocabulary_of_a_failed_resume_stays_out_of_the_states_table() -> None:
    assert runs.RESUME_FAILED_EVENT == ("run.resume_failed", "paused")
    assert runs.RESUME_FAILED_EVENT not in runs.AUDIT_FOR_STATE.values()
    assert "run.resume_failed" not in {e for e, _ in runs.AUDIT_FOR_STATE.values()}


def test_when_a_failed_resume_cannot_be_recorded_the_answer_is_503_and_the_thread_stays(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable(after=ToolThatIsDown()))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    thread = the_only_thread(fresh_database)
    attempts: list[str] = []

    def down(*args: Any, **kwargs: Any) -> None:
        attempts.append("tried")
        raise psycopg.OperationalError(f"down {CLAIM_TEXT}")

    monkeypatch.setattr(runs, "pause_after_failed_resume", down)

    response = resume(client, run_id)

    assert response.status_code == 503
    assert response.json() == {"detail": response.json()["detail"], "run_id": run_id}
    assert attempts == ["tried", "tried"]
    assert CLAIM_TEXT not in response.text
    assert run_rows(fresh_database)[0][5] == "Running"
    counts = checkpoint_counts(fresh_database, thread)
    assert all(count > 0 for count in counts.values()), counts


def test_the_retry_of_recording_a_failed_resume_keeps_its_reason_and_tool(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable(after=ToolThatIsDown()))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    real = runs.pause_after_failed_resume
    calls: list[str] = []

    def flaky(*args: Any, **kwargs: Any) -> None:
        calls.append("tried")
        if len(calls) == 1:
            raise psycopg.OperationalError("down")
        real(*args, **kwargs)

    monkeypatch.setattr(runs, "pause_after_failed_resume", flaky)

    response = resume(client, run_id)

    assert response.status_code == 502
    assert response.json()["status"] == "AwaitingApproval"
    assert len(calls) == 2
    events = audit_events(fresh_database, uuid.UUID(run_id))
    assert (events[-1]["event"], events[-1]["tool"]) == (
        "run.resume_failed",
        "policy_lookup",
    )
    assert [e["event"] for e in events].count("run.resume_failed") == 1


def test_a_failed_resume_is_logged_as_the_resumed_leg_and_a_failed_start_as_the_first(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def raising(answer: Any) -> None:
        raise RuntimeError("boom")

    register(monkeypatch, resumable(after=raising))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        resume(client, run_id)
    resumed_log = caplog.text
    caplog.clear()

    def failing(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            raise RuntimeError("boom")

        return graph_of(work)

    register(monkeypatch, failing)
    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        start(make_client(fresh_database))

    assert "resumed leg" in resumed_log
    assert "first leg" not in resumed_log
    assert "first leg" in caplog.text
    assert "resumed leg" not in caplog.text


def test_a_resumed_gateway_timeout_leaves_the_run_paused_with_504(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def asking(model: ModelClient, tools: ToolClient) -> StateGraph:
        def decide(state: State) -> State:
            interrupt("approve?")
            model.chat([{"role": "user", "content": "hi"}])
            return {"output": {}}

        return graph_of(decide)

    register(monkeypatch, asking)
    gateway = Gateway(raises=httpx.ReadTimeout("slow"))
    client = make_client(fresh_database, gateway)
    run_id = paused_run(client)

    response = resume(client, run_id)

    assert response.status_code == 504
    assert response.json()["status"] == "AwaitingApproval"
    assert run_rows(fresh_database)[0][5] == "AwaitingApproval"
    assert len(gateway.requests) == 1  # the first leg asked nothing
    reason = audit_events(fresh_database, uuid.UUID(run_id))[-1]
    assert (reason["event"], reason["reason"]) == ("run.resume_failed", "model-timeout")


LOOKS_LIKE_AN_INTERRUPT_ID = "a" * 32  # LangGraph reads such keys as a map


@pytest.mark.parametrize(
    "value",
    [{}, {LOOKS_LIKE_AN_INTERRUPT_ID: "not the pause's id"}],
    ids=["empty", "hex-key"],
)
def test_the_pause_reads_the_resume_value_verbatim_whatever_its_keys(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, value: dict
) -> None:
    register(monkeypatch, resumable())
    client = make_client(fresh_database)
    run_id = paused_run(client)

    response = resume(client, run_id, input=value)

    assert response.json()["status"] == "Completed"
    assert response.json()["output"] == {"stage": "after", "answer": value}


def test_a_thread_whose_checkpoints_are_gone_fails_the_resume_before_any_node_runs(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_calls: list[str] = []

    def counting(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            node_calls.append("ran")
            return {"output": {"answer": interrupt("approve?")}}

        return graph_of(work)

    register(monkeypatch, counting)
    client = make_client(fresh_database)
    run_id = paused_run(client)
    thread = the_only_thread(fresh_database)
    with open_saver(fresh_database.dsn("agent_runtime")) as saver:
        saver.delete_thread(str(thread))  # out of band

    response = resume(client, run_id)

    assert response.status_code == 502
    assert response.json() == {"run_id": run_id, "status": "Failed", "output": None}
    assert node_calls == ["ran"]  # the first leg's only
    assert run_rows(fresh_database)[0][5] == "Failed"
    events = audit_events(fresh_database, uuid.UUID(run_id))
    assert [e["event"] for e in events][-2:] == ["run.resumed", "run.failed"]
    assert (events[-1]["reason"], events[-1]["tool"]) == ("no-pending-pause", None)


def test_a_thread_with_two_pending_pauses_fails_the_resume_before_any_node_runs(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_calls: list[str] = []

    def parallel(model: ModelClient, tools: ToolClient) -> StateGraph:
        def pausing(name: str) -> Callable[[State], State]:
            def node(state: State) -> State:
                node_calls.append(name)
                interrupt(f"approve {name}")
                return {}

            return node

        graph = StateGraph(State)
        for name in ("left", "right"):
            graph.add_node(name, pausing(name))
            graph.add_edge(START, name)
            graph.add_edge(name, END)
        return graph

    register(monkeypatch, parallel)
    client = make_client(fresh_database)
    run_id = paused_run(client)
    calls_of_the_first_leg = len(node_calls)
    thread = the_only_thread(fresh_database)

    response = resume(client, run_id)

    assert response.status_code == 502
    assert response.json()["status"] == "Failed"
    assert len(node_calls) == calls_of_the_first_leg
    events = audit_events(fresh_database, uuid.UUID(run_id))
    assert events[-1]["event"] == "run.failed"
    assert events[-1]["reason"] == "several-pending-pauses"
    # Nothing to resume, so nothing to keep: the thread is deleted.
    assert checkpoint_counts(fresh_database, thread) == {
        table: 0 for table in CHECKPOINT_TABLES
    }


def test_a_run_that_pauses_again_keeps_its_checkpoints_and_resumes_again(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable(pauses=2))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    thread = the_only_thread(fresh_database)

    again = resume(client, run_id, input={"step": 1})
    kept = checkpoint_counts(fresh_database, thread)
    status_between = run_rows(fresh_database)[0][5]
    last = resume(client, run_id, input={"step": 2})

    assert again.status_code == 200
    assert again.json() == {
        "run_id": run_id,
        "status": "AwaitingApproval",
        "output": BEFORE,
    }
    assert status_between == "AwaitingApproval"
    assert all(count > 0 for count in kept.values()), kept
    assert last.json()["status"] == "Completed"
    assert last.json()["output"] == {"stage": "after", "answer": {"step": 2}}
    assert event_names(fresh_database, run_id) == [
        "run.started",
        "run.awaiting_approval",
        "run.resumed",
        "run.awaiting_approval",
        "run.resumed",
        "run.completed",
    ]
    assert checkpoint_counts(fresh_database, thread) == {
        table: 0 for table in CHECKPOINT_TABLES
    }


def test_when_a_resumed_status_cannot_be_recorded_the_answer_is_503_with_the_run_id(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable())
    client = make_client(fresh_database)
    run_id = paused_run(client)
    flaky = FlakyFinish(failures=99)
    monkeypatch.setattr(runs, "finish_run", flaky)

    response = resume(client, run_id)

    assert response.status_code == 503
    assert response.json() == {"detail": response.json()["detail"], "run_id": run_id}
    assert flaky.calls == 2
    assert CLAIM_TEXT not in response.text
    assert run_rows(fresh_database)[0][5] == "Running"


def test_a_second_app_on_the_same_database_resumes_a_run_the_first_paused(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable())
    run_id = paused_run(make_client(fresh_database))
    second = make_client(fresh_database)  # a new create_app: nothing shared

    response = resume(second, run_id)

    assert response.status_code == 200
    assert response.json()["status"] == "Completed"
    assert response.json()["output"] == {"stage": "after", "answer": APPROVAL}


# ── a Running run nobody is running any more is taken over (S015) ───────────
def make_running(
    db: DatabaseHandle, run_id: str, *, idle_seconds: int, status: str = "Running"
) -> None:
    """As the owner: set a run's status and move its ``updated_at`` back by
    ``idle_seconds``, as a leg that died or whose last write failed leaves it."""
    with connect(db.dsn(OWNER), "test-write") as conn:
        conn.execute(
            "UPDATE runtime.runs SET status = %s, "
            "updated_at = now() - make_interval(secs => %s) WHERE run_id = %s",
            (status, float(idle_seconds), uuid.UUID(run_id)),
        )


PAST_THE_LEASE = runs.RUNNING_LEASE_SECONDS + 60
INSIDE_THE_LEASE = runs.RUNNING_LEASE_SECONDS - 60


def test_the_lease_of_a_running_run_is_ten_minutes() -> None:
    assert runs.RUNNING_LEASE_SECONDS == 600
    assert runs.STALE_RUNNING_REASON == "stale-running"


def test_a_stale_running_run_with_a_pending_pause_is_resumed_and_completes(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    resumed_parts: list[Any] = []
    register(monkeypatch, resumable(after=resumed_parts.append))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    thread = the_only_thread(fresh_database)
    make_running(fresh_database, run_id, idle_seconds=PAST_THE_LEASE)

    response = resume(client, run_id)

    assert response.status_code == 200
    assert response.json() == {
        "run_id": run_id,
        "status": "Completed",
        "output": {"stage": "after", "answer": APPROVAL},
    }
    assert resumed_parts == [APPROVAL]
    events = audit_events(fresh_database, uuid.UUID(run_id))
    assert [(e["event"], e["outcome"], e["reason"]) for e in events] == [
        ("run.started", "started", None),
        ("run.awaiting_approval", "paused", None),
        ("run.resumed", "resumed", "stale-running"),
        ("run.completed", "completed", None),
    ]
    assert checkpoint_counts(fresh_database, thread) == {
        table: 0 for table in CHECKPOINT_TABLES
    }


def test_a_paused_run_is_claimed_without_the_takeover_reason(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable())
    client = make_client(fresh_database)
    run_id = paused_run(client)
    # Idle for longer than the lease, but paused: an ordinary claim.
    make_running(
        fresh_database, run_id, idle_seconds=PAST_THE_LEASE, status="AwaitingApproval"
    )

    resume(client, run_id)

    resumed = [
        e
        for e in audit_events(fresh_database, uuid.UUID(run_id))
        if e["event"] == "run.resumed"
    ]
    assert [e["reason"] for e in resumed] == [None]


def test_a_running_run_inside_the_lease_is_not_taken_and_nothing_runs(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    resumed_parts: list[Any] = []
    register(monkeypatch, resumable(after=resumed_parts.append))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    make_running(fresh_database, run_id, idle_seconds=INSIDE_THE_LEASE)
    audit_before = audit_count(fresh_database)

    response = resume(client, run_id)

    assert response.status_code == 200
    assert response.json() == {"run_id": run_id, "status": "Running", "output": None}
    assert resumed_parts == []
    assert run_rows(fresh_database)[0][5] == "Running"
    assert audit_count(fresh_database) == audit_before


def test_a_stale_running_run_with_no_pending_pause_ends_failed_and_its_thread_goes(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first leg that finished and then could neither record its status nor
    delete its checkpoint: the thread holds the claim and no pause."""

    @contextlib.contextmanager
    def refusing_delete(dsn: str) -> Iterator[PostgresSaver]:
        with open_saver(dsn) as real:
            yield DeleteRefused(real.conn)

    register(monkeypatch, ok_factory)
    real_finish = runs.finish_run
    with monkeypatch.context() as broken:
        broken.setattr(runs, "finish_run", FlakyFinish(failures=99))
        broken.setattr(runtime_app, "open_saver", refusing_delete)
        first = start(make_client(fresh_database))
    assert first.status_code == 503
    run_id = first.json()["run_id"]
    thread = the_only_thread(fresh_database)
    assert runs.finish_run is real_finish
    assert all(c > 0 for c in checkpoint_counts(fresh_database, thread).values())
    make_running(fresh_database, run_id, idle_seconds=PAST_THE_LEASE)

    response = resume(make_client(fresh_database), run_id)

    assert response.status_code == 502
    assert response.json() == {"run_id": run_id, "status": "Failed", "output": None}
    assert run_rows(fresh_database)[0][5] == "Failed"
    events = audit_events(fresh_database, uuid.UUID(run_id))
    assert [(e["event"], e["reason"]) for e in events] == [
        ("run.started", None),
        ("run.resumed", "stale-running"),
        ("run.failed", "no-pending-pause"),
    ]
    assert checkpoint_counts(fresh_database, thread) == {
        table: 0 for table in CHECKPOINT_TABLES
    }


def test_two_takeovers_of_a_stale_running_run_at_the_same_time_run_one_leg(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    resumed_parts: list[Any] = []

    def slowly(answer: Any) -> None:
        resumed_parts.append(answer)
        time.sleep(0.3)  # long enough for the other request to arrive meanwhile

    register(monkeypatch, resumable(after=slowly))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    make_running(fresh_database, run_id, idle_seconds=PAST_THE_LEASE)
    start_together = threading.Barrier(2)
    answers: list[httpx.Response] = []

    def send() -> None:
        own = TestClient(client.app, raise_server_exceptions=False)
        start_together.wait()
        answers.append(resume(own, run_id))

    racers = [threading.Thread(target=send) for _ in range(2)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()

    assert [a.status_code for a in answers] == [200, 200]
    assert resumed_parts == [APPROVAL]
    assert sorted(a.json()["output"] is None for a in answers) == [False, True]
    assert event_names(fresh_database, run_id) == [
        "run.started",
        "run.awaiting_approval",
        "run.resumed",
        "run.completed",
    ]


def test_a_stale_running_run_of_another_tenant_or_reference_is_not_claimed(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable())
    run_id = paused_run(make_client(fresh_database))
    make_running(fresh_database, run_id, idle_seconds=PAST_THE_LEASE)
    dsn = fresh_database.dsn("agent_runtime")
    asked = uuid.UUID(run_id)

    wrong_tenant = runs.claim_paused_run(dsn, asked, "evaluation", "CLM-0001")
    wrong_reference = runs.claim_paused_run(dsn, asked, "claims-triage", "CLM-0002")

    assert (wrong_tenant, wrong_reference) == (None, None)
    assert event_names(fresh_database, run_id).count("run.resumed") == 0


def test_a_resumed_leg_that_fails_after_a_takeover_leaves_the_run_paused(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable(after=ToolThatIsDown()))
    client = make_client(fresh_database)
    run_id = paused_run(client)
    make_running(fresh_database, run_id, idle_seconds=PAST_THE_LEASE)

    response = resume(client, run_id)

    assert response.status_code == 502
    assert response.json()["status"] == "AwaitingApproval"
    assert event_names(fresh_database, run_id)[-2:] == [
        "run.resumed",
        "run.resume_failed",
    ]


def test_a_resume_input_of_exactly_32_kib_is_accepted_and_one_byte_more_is_not(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable())
    client = make_client(fresh_database)
    run_id = paused_run(client)

    over = resume(client, run_id, input=input_of_size(32 * 1024 + 1))
    exact = resume(client, run_id, input=input_of_size(32 * 1024))

    assert over.status_code == 422
    assert "xxxxxxxx" not in over.text
    assert exact.status_code == 200
    assert exact.json()["status"] == "Completed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"extra": 1},
        {"reference": "has space"},
        {"reference": ""},
        {"tenant": "t" * 65},
        {"input": []},
        {"input": "text"},
    ],
)
def test_an_invalid_resume_request_is_422(overrides: dict[str, Any]) -> None:
    response = resume(make_client(None), str(uuid.uuid4()), **overrides)

    assert response.status_code == 422


def test_a_malformed_run_id_in_a_resume_is_422() -> None:
    assert resume(make_client(None), "not-a-uuid").status_code == 422


def test_the_resume_has_an_audit_event_of_its_own_beside_the_states() -> None:
    assert runs.AUDIT_FOR_STATE["Running"] == ("run.running", "running")
    assert runs.RESUMED_EVENT == ("run.resumed", "resumed")


# ── startup refusals ────────────────────────────────────────────────────────
def test_the_runtime_refuses_to_start_when_langsmith_tracing_was_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        meridian_runtime, "LANGSMITH_REQUESTED_BY", ("LANGSMITH_TRACING",)
    )

    with pytest.raises(SettingsError, match="LANGSMITH_TRACING"):
        make_client(None)


def test_the_runtime_refuses_to_start_when_strict_msgpack_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_app, "strict_msgpack_enabled", lambda: False)

    with pytest.raises(SettingsError, match="msgpack"):
        make_client(None)


# ── request size, health, lifecycle ─────────────────────────────────────────
def test_a_body_over_64_kib_is_413() -> None:
    response = make_client(None).post(
        "/runs",
        content=b"x" * (64 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413


def test_healthz_and_the_lifespan_need_no_database() -> None:
    with make_client(None) as client:
        response = client.get("/healthz")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_every_run_state_has_an_audit_event_so_finish_run_cannot_fail_on_a_lookup() -> (
    None
):
    assert set(runs.AUDIT_FOR_STATE) == set(get_args(RunState))


def test_an_input_with_a_lone_surrogate_is_422_and_echoes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(monkeypatch, ok_factory)
    raw = (
        '{"agent":"claims-triage","tenant":"claims-triage","reference":"CLM-0001",'
        '"input":{"k":"\\ud800"}}'
    )

    response = make_client(None).post(
        "/runs", content=raw, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert "ud800" not in response.text
    assert "\ud800" not in response.text


def test_the_gateway_client_ignores_proxy_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict[str, Any]] = []

    class SpyClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(runtime_app.httpx, "Client", SpyClient)

    create_app(
        RuntimeSettings(
            registry_dir=REGISTRY_DIR,
            gateway_url="http://gateway.invalid",
            database_url="postgresql://agent_runtime@db.invalid/x",
        )
    )

    (kwargs,) = built
    assert kwargs["trust_env"] is False
    assert kwargs["base_url"] == "http://gateway.invalid"


# ── tools (S013) ────────────────────────────────────────────────────────────
def policy_node(policy_number: str, *, catch: bool = False) -> Callable:
    """A factory whose one node asks the policy server for a policy."""

    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            try:
                found = tools.call("policy_lookup", {"policy_number": policy_number})
            except ToolRefused as refusal:
                if not catch:
                    raise
                return {"output": {"refused": refusal.reason}}
            return {"output": {"policy": found.data["policy"]["policy_number"]}}

        return graph_of(work)

    return factory


def test_a_node_calls_a_tool_and_the_run_is_audited_end_to_end(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = seed_world(fresh_database)
    register(monkeypatch, policy_node(POLICY))
    # The injected target replaces the settings' address for the same server.
    client = make_client(
        fresh_database,
        tool_servers={"policy-mcp": policy_server(world)},
        settings_servers={"policy-mcp": "http://nowhere.invalid"},
    )

    response = start(client)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "Completed"
    assert body["output"] == {"policy": POLICY}
    events = audit_events(fresh_database, uuid.UUID(body["run_id"]))
    assert [(e["service"], e["event"], e["outcome"]) for e in events] == [
        ("agent-runtime", "run.started", "started"),
        ("policy-mcp", "tool.call", "completed"),
        ("agent-runtime", "run.completed", "completed"),
    ]
    assert events[1]["tool"] == "policy_lookup"


def test_a_tool_refusal_the_node_does_not_catch_fails_the_run_with_502(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = seed_world(fresh_database)
    register(monkeypatch, policy_node("POL-0001"))  # not the claim's policy
    client = make_client(
        fresh_database, tool_servers={"policy-mcp": policy_server(world)}
    )

    response = start(client)

    assert response.status_code == 502
    body = response.json()
    assert body == {"run_id": body["run_id"], "status": "Failed", "output": None}
    events = audit_events(fresh_database, uuid.UUID(body["run_id"]))
    assert [(e["service"], e["event"], e["outcome"], e["reason"]) for e in events] == [
        ("agent-runtime", "run.started", "started", None),
        ("policy-mcp", "tool.call", "refused", "outside-claim"),
        ("agent-runtime", "run.failed", "failed", "tool-refused"),
    ]
    assert failed_row(fresh_database, response)["tool"] == "policy_lookup"


def test_the_log_of_a_refused_tool_call_names_the_tool_and_the_refusal(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = seed_world(fresh_database)
    register(monkeypatch, policy_node("POL-0001"))  # not the claim's policy
    client = make_client(
        fresh_database, tool_servers={"policy-mcp": policy_server(world)}
    )

    with caplog.at_level(logging.ERROR, logger=runtime_app.__name__):
        start(client)

    assert "tool-refused" in caplog.text
    assert "policy_lookup" in caplog.text
    assert "outside-claim" in caplog.text
    assert "POL-0001" not in caplog.text


def test_a_node_that_catches_the_refusal_completes_the_run(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = seed_world(fresh_database)
    register(monkeypatch, policy_node("POL-0001", catch=True))
    client = make_client(
        fresh_database, tool_servers={"policy-mcp": policy_server(world)}
    )

    response = start(client)

    assert response.status_code == 200
    assert response.json()["output"] == {"refused": "outside-claim"}


def test_a_tool_the_agent_may_not_call_is_audited_by_the_runtime_and_fails_the_run(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        return graph_of(lambda state: {"output": tools.call("no_such_tool", {}).data})

    register(monkeypatch, factory)

    response = start(make_client(fresh_database))

    assert response.status_code == 502
    events = audit_events(fresh_database, uuid.UUID(response.json()["run_id"]))
    assert [
        (e["service"], e["event"], e["outcome"], e["reason"], e["tool"]) for e in events
    ] == [
        ("agent-runtime", "run.started", "started", None, None),
        ("agent-runtime", "tool.call", "refused", "tool-not-allowed", None),
        ("agent-runtime", "run.failed", "failed", "tool-not-allowed", None),
    ]


def test_a_failed_audit_write_of_a_tool_refusal_fails_the_run(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            try:
                tools.call("no_such_tool", {})
            except ToolNotAllowed:
                return {"output": {"swallowed": True}}  # must not be reached
            return {}

        return graph_of(work)

    register(monkeypatch, factory)
    real = audit.write_audit

    def no_tool_rows(dsn: str, event: audit.AuditEvent) -> None:
        if event.event == "tool.call":
            raise audit.AuditUnavailable("down")
        real(dsn, event)

    monkeypatch.setattr(runtime_app, "write_audit", no_tool_rows)

    response = start(make_client(fresh_database))

    assert response.status_code == 502
    assert response.json()["output"] is None


# ── the runtime's own refusals are throttled (T-49) ─────────────────────────
class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def refusing(times: int) -> Callable:
    """A factory whose node asks for a tool the registry does not have, ``times``
    times, and carries on after each refusal."""

    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            for _ in range(times):
                with contextlib.suppress(ToolNotAllowed):
                    tools.call("no_such_tool", {})
            return {"output": {"asked": times}}

        return graph_of(work)

    return factory


def tool_rows(db: DatabaseHandle, response: httpx.Response) -> list[dict]:
    events = audit_events(db, uuid.UUID(response.json()["run_id"]))
    return [e for e in events if e["event"] == "tool.call"]


# The most tool calls a run may make: a refused call counts toward the limit.
MANY_REFUSALS = runs.MAX_TOOL_CALLS_PER_RUN


def test_all_the_refused_calls_a_run_may_make_leave_one_row(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, refusing(MANY_REFUSALS))

    response = start(make_client(fresh_database, clock=Clock()))

    assert response.json()["status"] == "Completed"
    (row,) = tool_rows(fresh_database, response)
    assert (row["outcome"], row["reason"], row["suppressed"]) == (
        "refused",
        "tool-not-allowed",
        0,
    )


def test_after_the_window_the_next_row_counts_the_refusals_it_stands_for(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, refusing(MANY_REFUSALS))
    clock = Clock()
    client = make_client(fresh_database, clock=clock)
    start(client)
    clock.now += REFUSAL_AUDIT_SECONDS
    register(monkeypatch, refusing(1))  # the same app: its throttle lives on

    second = start(client)

    (row,) = tool_rows(fresh_database, second)
    assert row["suppressed"] == MANY_REFUSALS - 1


def test_inside_the_window_a_later_run_leaves_no_row(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, refusing(MANY_REFUSALS))
    clock = Clock()
    client = make_client(fresh_database, clock=clock)
    start(client)
    clock.now += REFUSAL_AUDIT_SECONDS - 0.5
    register(monkeypatch, refusing(1))

    second = start(client)

    assert second.json()["status"] == "Completed"  # the refusal still raised
    assert tool_rows(fresh_database, second) == []


def test_a_failed_audit_write_fails_the_run_and_the_next_refusal_is_due(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, refusing(1))
    real = audit.write_audit
    down = [True]

    def flaky(dsn: str, event: audit.AuditEvent) -> None:
        if event.event == "tool.call" and down[0]:
            raise audit.AuditUnavailable("down")
        real(dsn, event)

    monkeypatch.setattr(runtime_app, "write_audit", flaky)
    client = make_client(fresh_database, clock=Clock())  # the clock never moves

    failed = start(client)
    down[0] = False
    next_run = start(client)

    assert failed.status_code == 502
    assert tool_rows(fresh_database, failed) == []
    assert next_run.json()["status"] == "Completed"
    (row,) = tool_rows(fresh_database, next_run)
    assert row["suppressed"] == 1  # the refusal whose row could not be written


# ── SDK setup happens at start (S013) ───────────────────────────────────────
def test_the_app_prepares_the_sdk_when_it_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runtime_app, "prepare_sdk", lambda: calls.append("prepared"))

    make_client(None)

    assert calls == ["prepared"]
