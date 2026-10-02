"""POST /runs and GET /runs/{id} with stub graphs (no workload needed)."""

import contextlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypedDict, get_args

import httpx
import psycopg
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
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
from meridian.platform.common.env import SettingsError
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.throttle import REFUSAL_AUDIT_SECONDS
from meridian.runtime import app as runtime_app
from meridian.runtime import graphs, runs
from meridian.runtime.app import create_app
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

    status_response = client.get(f"/runs/{run_id}")

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

    response = start(make_client(None))

    assert response.status_code == 503
    assert "hunter2" not in response.text
    assert CLAIM_TEXT not in response.text
    assert node_calls == []


# ── GET /runs/{id} ──────────────────────────────────────────────────────────
def test_an_unknown_run_is_404(fresh_database: DatabaseHandle) -> None:
    response = make_client(fresh_database).get(f"/runs/{uuid.uuid4()}")

    assert response.status_code == 404


def test_a_malformed_run_id_is_422(fresh_database: DatabaseHandle) -> None:
    assert make_client(fresh_database).get("/runs/not-a-uuid").status_code == 422


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

    response = start(make_client(None, exporter=exporter))

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
