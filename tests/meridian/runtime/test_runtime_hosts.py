"""The Agent Runtime on two hosts (S037, R1): the agent's registry entry says which
framework runs it, the service refuses at start an entry point that is not what
its host runs, and a finished run's checkpoints are forgotten by its own host,
after the run is recorded as ended.

Both agents of these tests are the registry's own, or a ``claim-brief`` planted
into a scratch copy with ``host: agent-framework`` (see ``hostsupport``).
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

import httpx
import pytest
from agent_framework.exceptions import WorkflowCheckpointException
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from hostflows import Dials, chain_factory, loop_factory, yield_factory
from hostsupport import AGENT as BRIEF_AGENT
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from runtimesupport import FakeEntryPoint, register, register_agents
from servicesupport import GATEWAY_REPLY, REGISTRY_DIR, audit_events, owner_rows

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.runtime import SERVICE_NAME
from meridian.runtime.agent_framework_host import AgentFrameworkHost
from meridian.runtime.app import create_app
from meridian.runtime.graphs import GraphLoadError
from meridian.runtime.hosts import Host
from meridian.runtime.langgraph_host import LangGraphHost
from meridian.runtime.model_client import ModelClient
from meridian.runtime.runs import RunIdentity
from meridian.runtime.settings import RuntimeSettings
from meridian.runtime.tool_client import ToolClient
from meridian.runtime.workflow_checkpoints import PostgresCheckpointStore

CLAIM_TEXT = "claimant-secret-text-42"
TRIAGE_AGENT = "claims-triage"
TENANT = "claims-triage"
CLAIM = "CLM-0001"
BRIEF_MODULE = "meridian.workloads.claim_brief.workflow"
DELETE_LOG = "its checkpoints were not deleted"
LANGGRAPH_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
SECOND_HOST_TABLES = ("workflow_checkpoints",)


class State(TypedDict, total=False):
    output: Any


def graph_of(node: Callable[[State], State]) -> StateGraph:
    graph = StateGraph(State)
    graph.add_node("work", node)
    graph.add_edge(START, "work")
    graph.add_edge("work", END)
    return graph


def langgraph_done(model: ModelClient, tools: ToolClient) -> StateGraph:
    return graph_of(lambda state: {"output": {"host": "langgraph"}})


def langgraph_pausing(model: ModelClient, tools: ToolClient) -> StateGraph:
    return graph_of(lambda state: {"output": {"answer": interrupt("approve?")}})


def langgraph_looping(model: ModelClient, tools: ToolClient) -> StateGraph:
    graph = StateGraph(State)
    graph.add_node("work", lambda state: {})
    graph.add_edge(START, "work")
    graph.add_conditional_edges("work", lambda state: "work", ["work"])
    return graph


def a_workflow(model: Any, tools: Any) -> Any:
    return yield_factory({"host": "agent-framework"})(model, tools)


@dataclass(frozen=True)
class Case:
    """One host, its agent, and two workloads for it: one that ends in a leg and
    one that pauses once and ends on its resume."""

    agent: str
    tables: tuple[str, ...]
    entries: Callable[[Callable[..., Any]], list[FakeEntryPoint]]
    done: Callable[..., Any]
    pausing: Callable[..., Any]


CASES = {
    "langgraph": Case(
        TRIAGE_AGENT,
        LANGGRAPH_TABLES,
        lambda factory: [FakeEntryPoint(factory, TRIAGE_AGENT)],
        langgraph_done,
        langgraph_pausing,
    ),
    "agent-framework": Case(
        BRIEF_AGENT,
        SECOND_HOST_TABLES,
        lambda factory: [FakeEntryPoint(factory, BRIEF_AGENT, module=BRIEF_MODULE)],
        a_workflow,
        chain_factory(Dials(), before=1, after=0),
    ),
}


def make_client(
    db: DatabaseHandle | None,
    registry_dir: Path = REGISTRY_DIR,
    exporter: InMemorySpanExporter | None = None,
) -> TestClient:
    dsn = db.dsn("agent_runtime") if db else "postgresql://agent_runtime@db.invalid/x"
    settings = RuntimeSettings(
        registry_dir=registry_dir,
        gateway_url="http://gateway.invalid",
        database_url=dsn,
        tool_servers={},
    )
    gateway = httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=GATEWAY_REPLY)
        ),
    )
    app = create_app(
        settings,
        tracer_provider=make_tracer_provider(SERVICE_NAME, exporter),
        http_client=gateway,
    )
    return TestClient(app, raise_server_exceptions=False)


def service_of(
    monkeypatch: pytest.MonkeyPatch,
    plant: Callable[..., Path],
    db: DatabaseHandle | None,
    *entries: FakeEntryPoint,
) -> TestClient:
    """The runtime over a scratch copy of the registry, which holds
    ``claim-brief`` on the second host, with ``entries`` published (the
    registry's own agent keeps its real entry point when none is given)."""
    directory = plant()
    register_agents(monkeypatch, *entries)
    return make_client(db, directory)


def start(client: TestClient, agent: str) -> httpx.Response:
    return client.post(
        "/runs",
        json={"agent": agent, "tenant": TENANT, "reference": CLAIM, "input": {}},
    )


def resume(client: TestClient, run_id: str) -> httpx.Response:
    return client.post(
        f"/runs/{run_id}/resume",
        json={"tenant": TENANT, "reference": CLAIM, "input": {}},
    )


def started_service(
    monkeypatch: pytest.MonkeyPatch,
    plant: Callable[..., Path],
    db: DatabaseHandle,
    case: Case,
    factory: Callable[..., Any],
) -> TestClient:
    """The runtime with ``factory`` published for the case's agent; for the
    second host's agent in the scratch registry, for the first's in the real."""
    entries = case.entries(factory)
    if case.agent == BRIEF_AGENT:
        return service_of(monkeypatch, plant, db, *entries)
    register_agents(monkeypatch, *entries)
    return make_client(db)


def rows_held(db: DatabaseHandle, case: Case, run_id: str) -> dict[str, int]:
    """The rows of the run's thread in each checkpoint table of its host."""
    ((thread,),) = owner_rows(
        db, "SELECT thread_id FROM runtime.runs WHERE run_id = %s", (run_id,)
    )
    return {
        table: owner_rows(
            db,
            f"SELECT count(*) FROM runtime.{table} WHERE thread_id = %s",  # noqa: S608
            (str(thread),),
        )[0][0]
        for table in case.tables
    }


def stored_status(db: DatabaseHandle, run_id: str) -> str:
    return owner_rows(
        db, "SELECT status FROM runtime.runs WHERE run_id = %s", (run_id,)
    )[0][0]


# ── what the service refuses to start with ──────────────────────────────────
def test_a_langgraph_agent_whose_entry_point_returns_a_workflow_definition_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(monkeypatch, chain_factory(Dials(), before=0, after=0))

    with pytest.raises(GraphLoadError) as refused:
        make_client(None)

    text = str(refused.value)
    assert TRIAGE_AGENT in text
    assert "langgraph" in text
    assert "WorkflowDefinition" in text


def test_an_agent_framework_agent_whose_entry_point_returns_a_graph_is_refused(
    monkeypatch: pytest.MonkeyPatch, plant: Callable[..., Path]
) -> None:
    entry = FakeEntryPoint(langgraph_done, BRIEF_AGENT, module=BRIEF_MODULE)

    with pytest.raises(GraphLoadError) as refused:
        service_of(monkeypatch, plant, None, entry)

    text = str(refused.value)
    assert BRIEF_AGENT in text
    assert "agent-framework" in text
    assert "StateGraph" in text


def test_a_factory_that_cannot_be_called_is_refused_with_its_class_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(model: ModelClient, tools: ToolClient) -> StateGraph:
        raise RuntimeError(CLAIM_TEXT)

    register(monkeypatch, broken)

    with pytest.raises(GraphLoadError) as refused:
        make_client(None)

    assert "RuntimeError" in str(refused.value)
    assert CLAIM_TEXT not in str(refused.value)
    assert refused.value.__cause__ is None


def test_the_service_starts_with_an_agent_of_each_host(
    monkeypatch: pytest.MonkeyPatch, plant: Callable[..., Path]
) -> None:
    client = service_of(
        monkeypatch,
        plant,
        None,
        FakeEntryPoint(langgraph_done, TRIAGE_AGENT),
        FakeEntryPoint(a_workflow, BRIEF_AGENT, module=BRIEF_MODULE),
    )

    assert client.app is not None


def test_the_service_starts_with_the_real_triage_graph_checked_as_a_langgraph_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(None)

    assert client.app is not None


# ── a run of each host, from start to the end ───────────────────────────────
@pytest.mark.parametrize("host", CASES)
def test_a_run_that_ends_in_its_first_leg_leaves_no_checkpoint_of_its_host(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    plant: Callable[..., Path],
    host: str,
) -> None:
    case = CASES[host]
    client = started_service(monkeypatch, plant, fresh_database, case, case.done)

    response = start(client, case.agent)

    body = response.json()
    assert (response.status_code, body["status"]) == (200, "Completed")
    assert body["output"] == {"host": host}
    assert rows_held(fresh_database, case, body["run_id"]) == {
        table: 0 for table in case.tables
    }


@pytest.mark.parametrize("host", CASES)
def test_a_paused_run_keeps_its_checkpoints_until_its_resume_ends_it(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    plant: Callable[..., Path],
    host: str,
) -> None:
    case = CASES[host]
    client = started_service(monkeypatch, plant, fresh_database, case, case.pausing)

    paused = start(client, case.agent)
    run_id = paused.json()["run_id"]
    held_while_paused = rows_held(fresh_database, case, run_id)
    finished = resume(client, run_id)

    assert (paused.status_code, paused.json()["status"]) == (200, "AwaitingApproval")
    assert all(count > 0 for count in held_while_paused.values()), held_while_paused
    assert (finished.status_code, finished.json()["status"]) == (200, "Completed")
    assert rows_held(fresh_database, case, run_id) == {
        table: 0 for table in case.tables
    }


def test_the_second_host_forgets_a_run_only_after_the_run_is_recorded_as_ended(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    plant: Callable[..., Path],
) -> None:
    seen: list[str] = []
    real_forget = AgentFrameworkHost.forget

    def spying(self: AgentFrameworkHost, identity: RunIdentity) -> None:
        seen.append(stored_status(fresh_database, str(identity.run_id)))
        real_forget(self, identity)

    monkeypatch.setattr(AgentFrameworkHost, "forget", spying)
    case = CASES["agent-framework"]
    client = started_service(monkeypatch, plant, fresh_database, case, case.done)

    response = start(client, case.agent)

    assert response.json()["status"] == "Completed"
    assert seen == ["Completed"]


def test_a_forget_of_the_second_host_that_fails_is_logged_and_changes_no_answer(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    plant: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def refused(self: PostgresCheckpointStore) -> int:
        raise WorkflowCheckpointException(f"password=hunter2 {CLAIM_TEXT}")

    monkeypatch.setattr(PostgresCheckpointStore, "forget", refused)
    case = CASES["agent-framework"]
    client = started_service(monkeypatch, plant, fresh_database, case, case.done)

    with caplog.at_level(logging.ERROR):
        response = start(client, case.agent)

    body = response.json()
    assert (response.status_code, body["status"]) == (200, "Completed")
    assert stored_status(fresh_database, body["run_id"]) == "Completed"
    assert caplog.text.count(DELETE_LOG) == 1
    assert "WorkflowCheckpointException" in caplog.text
    assert "sqlstate none" in caplog.text
    assert body["run_id"] in caplog.text
    assert "hunter2" not in caplog.text
    assert CLAIM_TEXT not in caplog.text
    run_events = {
        e["event"] for e in audit_events(fresh_database, uuid.UUID(body["run_id"]))
    }
    assert "run.completed" in run_events


# ── the step limit: one word for each host, as before ───────────────────────
def test_a_langgraph_run_over_its_step_limit_fails_as_unexpected_as_it_always_did(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, langgraph_looping)

    response = start(make_client(fresh_database), TRIAGE_AGENT)

    body = response.json()
    assert (response.status_code, body["status"]) == (502, "Failed")
    failed = [
        e
        for e in audit_events(fresh_database, uuid.UUID(body["run_id"]))
        if e["event"] == "run.failed"
    ]
    assert [e["reason"] for e in failed] == ["unexpected"]


def test_a_run_of_the_second_host_over_its_step_limit_fails_as_step_limit(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    plant: Callable[..., Path],
) -> None:
    case = CASES["agent-framework"]
    client = started_service(
        monkeypatch, plant, fresh_database, case, loop_factory(Dials())
    )

    response = start(client, case.agent)

    body = response.json()
    assert (response.status_code, body["status"]) == (502, "Failed")
    failed = [
        e
        for e in audit_events(fresh_database, uuid.UUID(body["run_id"]))
        if e["event"] == "run.failed"
    ]
    assert [e["reason"] for e in failed] == ["step-limit"]


# ── the first host, behind the protocol ─────────────────────────────────────
IDENTITY = RunIdentity(
    run_id=uuid.uuid4(),
    thread_id=uuid.uuid4(),
    agent=TRIAGE_AGENT,
    tenant=TENANT,
    reference=CLAIM,
)


def test_the_langgraph_host_is_a_host() -> None:
    host = LangGraphHost(langgraph_done, MemorySaver(), lambda: MemorySaver())

    assert isinstance(host, Host)


def test_the_langgraph_host_pauses_resumes_and_forgets_a_thread() -> None:
    saver = MemorySaver()
    host = LangGraphHost(langgraph_pausing, saver, lambda: saver)
    clients: Any = (None, None)

    paused = host.start(IDENTITY, *clients, make_tracer(), {})
    held = list(saver.list(None))
    ended = host.resume(IDENTITY, *clients, make_tracer(), {"approved": True})
    host.forget(IDENTITY)

    assert (paused.status, paused.output) == ("AwaitingApproval", None)
    assert held
    assert (ended.status, ended.output) == ("Completed", {"answer": {"approved": True}})
    assert list(saver.list(None)) == []


def make_tracer() -> Any:
    return make_tracer_provider(SERVICE_NAME, InMemorySpanExporter()).get_tracer("test")
