"""What the Agent Runtime does with a parent graph whose nodes are compiled
subgraphs (S031, Part 1 of the supervisor's contract), through the real runtime
app and PostgreSQL.

The supervisor of the triage is a parent graph and its workers are subgraphs.
Four things could not be read from the source, and each test below shows one:
that the state of both graphs is plain data under the strict serialisation, how
the step limit counts a subgraph's steps, that a pause called by a node of the
parent between two subgraphs resumes as today's pause does, and that the
checkpoints a subgraph writes under a namespace of its own are deleted with the
run, with a span for every node.
"""

import uuid
from collections import Counter
from collections.abc import Callable
from itertools import pairwise
from typing import Any, TypedDict

import httpx
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from runtimesupport import register
from servicesupport import REGISTRY_DIR, audit_events, owner_rows

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.runtime.app import create_app
from meridian.runtime.model_client import ModelClient
from meridian.runtime.settings import RuntimeSettings
from meridian.runtime.tool_client import ToolClient

CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
APPROVAL: dict[str, Any] = {}  # a resume delivers no value (S069)


class Shared(TypedDict, total=False):
    """The keys the parent and its subgraphs share: plain data only."""

    claim: dict[str, Any]
    trail: list[str]
    output: dict[str, Any]


def make_client(
    db: DatabaseHandle, exporter: InMemorySpanExporter | None = None
) -> TestClient:
    settings = RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url="http://gateway.invalid",
        database_url=db.dsn("agent_runtime"),
        tool_servers={},
    )
    gateway = httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    app = create_app(
        settings,
        tracer_provider=make_tracer_provider("agent-runtime", exporter),
        http_client=gateway,
    )
    return TestClient(app, raise_server_exceptions=False)


def start(client: TestClient) -> httpx.Response:
    return client.post(
        "/runs",
        json={
            "agent": "claims-triage",
            "tenant": "claims-triage",
            "reference": "CLM-0001",
            "input": {"claim": {"n": 7}},
        },
    )


def resume(client: TestClient, run_id: str) -> httpx.Response:
    return client.post(
        f"/runs/{run_id}/resume",
        json={"tenant": "claims-triage", "reference": "CLM-0001", "input": APPROVAL},
    )


def worker(
    name: str,
    nodes: int,
    calls: Counter[str],
    fails: Callable[[str], bool] = lambda node: False,
) -> CompiledStateGraph:
    """A subgraph of ``nodes`` nodes in a line. Each appends its name to the
    trail and writes the trail as the output, and each counts its own runs."""
    graph = StateGraph(Shared)
    previous = START
    for index in range(nodes):
        node = f"{name}{index}"

        def step(state: Shared, node: str = node) -> Shared:
            calls[node] += 1
            if fails(node):
                raise RuntimeError("planted failure")
            trail = [*state.get("trail", []), node]
            return {"trail": trail, "output": {"trail": trail}}

        graph.add_node(node, step)
        graph.add_edge(previous, node)
        previous = node
    graph.add_edge(previous, END)
    return graph.compile()


def line_of(
    first: CompiledStateGraph, plain: int, calls: Counter[str]
) -> Callable[[ModelClient, ToolClient], StateGraph]:
    """A parent whose first node is ``first`` and that continues through
    ``plain`` ordinary nodes."""

    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        graph = StateGraph(Shared)
        graph.add_node("first", first)
        graph.add_edge(START, "first")
        previous = "first"
        for index in range(plain):
            node = f"plain{index}"

            def step(state: Shared, node: str = node) -> Shared:
                calls[node] += 1
                trail = [*state["trail"], node]
                return {"trail": trail, "output": {"trail": trail}}

            graph.add_node(node, step)
            graph.add_edge(previous, node)
            previous = node
        graph.add_edge(previous, END)
        return graph

    return factory


def paused_between(
    calls: Counter[str], fails: Callable[[str], bool] = lambda node: False
) -> Callable[[ModelClient, ToolClient], StateGraph]:
    """The supervisor's shape: a subgraph, a node of the parent that only
    pauses, and a second subgraph."""

    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        def pause(state: Shared) -> Shared:
            calls["pause"] += 1
            interrupt({"request_id": "r-1"})
            return {}

        graph = StateGraph(Shared)
        graph.add_node("before", worker("before", 2, calls))
        graph.add_node("pause", pause)
        graph.add_node("after", worker("after", 2, calls, fails))
        graph.add_edge(START, "before")
        graph.add_edge("before", "pause")
        graph.add_edge("pause", "after")
        graph.add_edge("after", END)
        return graph

    return factory


def paused_inside(
    calls: Counter[str], fails: Callable[[str], bool] = lambda node: False
) -> Callable[[ModelClient, ToolClient], StateGraph]:
    """The pause is the first node of the second subgraph."""

    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        def pause(state: Shared) -> Shared:
            calls["pause"] += 1
            interrupt({"request_id": "r-1"})
            return {}

        inner = StateGraph(Shared)
        inner.add_node("pause", pause)
        previous = "pause"
        inner.add_edge(START, "pause")
        for index in range(2):
            node = f"after{index}"

            def step(state: Shared, node: str = node) -> Shared:
                calls[node] += 1
                if fails(node):
                    raise RuntimeError("planted failure")
                trail = [*state.get("trail", []), node]
                return {"trail": trail, "output": {"trail": trail}}

            inner.add_node(node, step)
            inner.add_edge(previous, node)
            previous = node
        inner.add_edge(previous, END)
        graph = StateGraph(Shared)
        graph.add_node("before", worker("before", 2, calls))
        graph.add_node("after", inner.compile())
        graph.add_edge(START, "before")
        graph.add_edge("before", "after")
        graph.add_edge("after", END)
        return graph

    return factory


def paused_then_invoking(
    calls: Counter[str], fails: Callable[[str], bool] = lambda node: False
) -> Callable[[ModelClient, ToolClient], StateGraph]:
    """The pause is a node of the parent that then runs the second subgraph
    itself, as one call."""

    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        after = worker("after", 2, calls, fails)

        def decide(state: Shared) -> Shared:
            calls["pause"] += 1
            interrupt({"request_id": "r-1"})
            return after.invoke(state)

        graph = StateGraph(Shared)
        graph.add_node("before", worker("before", 2, calls))
        graph.add_node("decide", decide)
        graph.add_edge(START, "before")
        graph.add_edge("before", "decide")
        graph.add_edge("decide", END)
        return graph

    return factory


def checkpoint_counts(db: DatabaseHandle, thread_id: uuid.UUID) -> dict[str, int]:
    return {
        table: owner_rows(
            db,
            f"SELECT count(*) FROM runtime.{table} WHERE thread_id = %s",  # noqa: S608
            (str(thread_id),),
        )[0][0]
        for table in CHECKPOINT_TABLES
    }


def namespaced_checkpoints(db: DatabaseHandle, thread_id: uuid.UUID) -> int:
    """The checkpoints of the thread that a subgraph wrote under its own
    namespace (the parent's own are under the empty one)."""
    return owner_rows(
        db,
        "SELECT count(*) FROM runtime.checkpoints "
        "WHERE thread_id = %s AND checkpoint_ns <> ''",
        (str(thread_id),),
    )[0][0]


def the_only_thread(db: DatabaseHandle) -> uuid.UUID:
    return owner_rows(db, "SELECT thread_id FROM runtime.runs")[0][0]


def failure_reason_of(db: DatabaseHandle, run_id: str) -> str | None:
    events = audit_events(db, uuid.UUID(run_id))
    failed = [e for e in events if e["event"] == "run.failed"]
    return failed[0]["reason"] if failed else None


# ── 1. state ────────────────────────────────────────────────────────────────
def test_a_run_with_a_subgraph_node_completes_and_its_output_is_read_as_today(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: Counter[str] = Counter()
    register(monkeypatch, line_of(worker("sub", 2, calls), 1, calls))

    response = start(make_client(fresh_database))

    assert response.status_code == 200
    assert response.json()["status"] == "Completed"
    assert response.json()["output"] == {"trail": ["sub0", "sub1", "plain0"]}


def test_a_value_of_a_subgraph_that_is_not_plain_data_fails_the_run(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class NotPlain:
        pass

    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        sub = StateGraph(Shared)
        sub.add_node("make", lambda state: {"claim": NotPlain()})
        sub.add_edge(START, "make")
        sub.add_edge("make", END)
        graph = StateGraph(Shared)
        graph.add_node("first", sub.compile())
        graph.add_edge(START, "first")
        graph.add_edge("first", END)
        return graph

    register(monkeypatch, factory)

    response = start(make_client(fresh_database))

    assert response.status_code == 502
    assert response.json()["status"] == "Failed"
    assert "(TypeError;" in caplog.text


# ── 2. steps ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("subgraph_nodes", "plain_nodes", "completes"),
    [
        # Nine steps in a line are the most one graph may take (limit ten).
        (9, 0, True),
        (10, 0, False),
        # The parent's steps and the subgraph's are counted apart: nine and
        # nine are eighteen in all, and the run completes.
        (9, 8, True),
        (1, 9, False),
        (10, 1, False),
    ],
)
def test_each_graph_counts_its_own_steps_against_the_limit_of_ten(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    subgraph_nodes: int,
    plain_nodes: int,
    completes: bool,
) -> None:
    calls: Counter[str] = Counter()
    register(
        monkeypatch, line_of(worker("sub", subgraph_nodes, calls), plain_nodes, calls)
    )

    response = start(make_client(fresh_database))

    expected = "Completed" if completes else "Failed"
    assert response.json()["status"] == expected
    if not completes:
        assert failure_reason_of(fresh_database, response.json()["run_id"]) == (
            "unexpected"
        )


# ── 3. the pause and the resume ─────────────────────────────────────────────
def test_a_pause_between_two_subgraphs_resumes_without_rerunning_the_first(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: Counter[str] = Counter()
    register(monkeypatch, paused_between(calls))
    client = make_client(fresh_database)

    paused = start(client)

    assert paused.json()["status"] == "AwaitingApproval"
    assert paused.json()["output"] == {"trail": ["before0", "before1"]}
    assert calls == {"before0": 1, "before1": 1, "pause": 1}

    resumed = resume(client, paused.json()["run_id"])

    assert resumed.json()["status"] == "Completed"
    assert resumed.json()["output"] == {
        "trail": ["before0", "before1", "after0", "after1"]
    }
    # The subgraph before the pause ran once; the node that pauses ran again
    # from its first line (it ran once to pause and once more after the
    # resume); the subgraph after it ran once.
    assert calls == {
        "before0": 1,
        "before1": 1,
        "pause": 2,
        "after0": 1,
        "after1": 1,
    }


def test_a_pause_node_of_its_own_before_a_failing_subgraph_cannot_be_resumed_twice(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the triage's pause and the work after it share one node (S031). A node
    that only pauses has finished, and its interrupt is consumed, once the resume
    passes it. A subgraph node after it that then fails leaves the thread with no
    pending pause, so the second resume is the runtime's ``no-pending-pause`` and
    ends the run Failed, where today's ``await_decision`` leaves it paused. The
    supervisor's ``await_decision`` therefore pauses and then runs the outcome
    worker itself (the direct shape in the test below)."""
    calls: Counter[str] = Counter()
    register(monkeypatch, paused_between(calls, lambda node: node == "after0"))
    client = make_client(fresh_database)
    run_id = start(client).json()["run_id"]

    first = resume(client, run_id)

    assert first.status_code == 502
    assert first.json()["status"] == "AwaitingApproval"

    second = resume(client, run_id)

    assert second.status_code == 502
    assert second.json()["status"] == "Failed"
    assert failure_reason_of(fresh_database, run_id) == "no-pending-pause"
    assert calls == {"before0": 1, "before1": 1, "pause": 2, "after0": 1}


@pytest.mark.parametrize(
    ("shape", "pauses_run"),
    [
        # The pause is the first node of the second subgraph: the second resume
        # does not enter ``interrupt`` again (the pause ran twice, not three
        # times), for the subgraph continues from its own checkpoint.
        (paused_inside, 2),
        # The pause is a node of the parent that runs the second subgraph
        # itself: every resume runs that node from its first line, as today's
        # ``await_decision`` does.
        (paused_then_invoking, 3),
    ],
)
def test_a_pause_held_with_the_work_after_it_can_be_resumed_after_a_failed_leg(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    shape: Callable,
    pauses_run: int,
) -> None:
    calls: Counter[str] = Counter()
    broken = {"now": True}
    register(monkeypatch, shape(calls, lambda node: broken["now"]))
    client = make_client(fresh_database)
    run_id = start(client).json()["run_id"]

    failed = resume(client, run_id)

    assert failed.status_code == 502
    assert failed.json()["status"] == "AwaitingApproval"

    broken["now"] = False
    again = resume(client, run_id)

    assert again.status_code == 200
    assert again.json()["status"] == "Completed"
    assert calls == {
        "before0": 1,
        "before1": 1,
        "pause": pauses_run,
        "after0": 2,
        "after1": 1,
    }


def test_an_invoked_subgraph_continues_at_the_node_that_failed_not_at_its_first(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The triage's case: the note (the last node of the outcome worker) fails
    after the read of the outcome (its first) succeeded. The second resume runs
    the node that pauses again from its first line (three runs of it), but the
    invoked worker does not run its first node again: it continues from the
    checkpoint it wrote under a namespace of its own."""
    calls: Counter[str] = Counter()
    broken = {"now": True}
    register(
        monkeypatch,
        paused_then_invoking(calls, lambda node: node == "after1" and broken["now"]),
    )
    client = make_client(fresh_database)
    run_id = start(client).json()["run_id"]

    failed = resume(client, run_id)

    assert failed.status_code == 502
    assert failed.json()["status"] == "AwaitingApproval"

    broken["now"] = False
    again = resume(client, run_id)

    assert again.status_code == 200
    assert again.json()["status"] == "Completed"
    assert calls == {
        "before0": 1,
        "before1": 1,
        "pause": 3,
        "after0": 1,
        "after1": 2,
    }


def test_the_limit_of_steps_is_counted_for_each_leg_of_a_run(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        def pause(state: Shared) -> Shared:
            interrupt({"request_id": "r-1"})
            return {}

        def plain(state: Shared) -> Shared:
            return {"output": {}}

        graph = StateGraph(Shared)
        names = [f"first{i}" for i in range(6)] + ["pause"]
        names += [f"last{i}" for i in range(6)]
        for name in names:
            graph.add_node(name, pause if name == "pause" else plain)
        graph.add_edge(START, names[0])
        for left, right in pairwise(names):
            graph.add_edge(left, right)
        graph.add_edge(names[-1], END)
        return graph

    register(monkeypatch, factory)
    client = make_client(fresh_database)
    paused = start(client)

    resumed = resume(client, paused.json()["run_id"])

    assert paused.json()["status"] == "AwaitingApproval"
    assert resumed.json()["status"] == "Completed"


# ── 4. checkpoints and spans ────────────────────────────────────────────────
@pytest.mark.parametrize("shape", [paused_between, paused_then_invoking])
def test_the_checkpoints_of_a_subgraph_are_kept_while_paused_and_deleted_with_the_run(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, shape: Callable
) -> None:
    calls: Counter[str] = Counter()
    register(monkeypatch, shape(calls))
    client = make_client(fresh_database)
    paused = start(client)
    thread = the_only_thread(fresh_database)

    while_paused = checkpoint_counts(fresh_database, thread)
    own_namespace = namespaced_checkpoints(fresh_database, thread)
    resumed = resume(client, paused.json()["run_id"])

    assert all(count > 0 for count in while_paused.values()), while_paused
    assert own_namespace > 0
    assert resumed.json()["status"] == "Completed"
    assert checkpoint_counts(fresh_database, thread) == {
        table: 0 for table in CHECKPOINT_TABLES
    }


@pytest.mark.parametrize(
    ("shape", "carrier", "inner"),
    [
        (paused_between, "after", ["after0", "after1"]),
        (paused_then_invoking, "decide", ["after0", "after1"]),
    ],
)
def test_the_nodes_of_a_subgraph_have_spans_under_the_span_of_the_node_that_runs_it(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    shape: Callable,
    carrier: str,
    inner: list[str],
) -> None:
    calls: Counter[str] = Counter()
    register(monkeypatch, shape(calls))
    exporter = InMemorySpanExporter()
    client = make_client(fresh_database, exporter)
    paused = start(client)

    resumed = resume(client, paused.json()["run_id"])

    assert resumed.json()["status"] == "Completed"
    spans = {
        s.name: s
        for s in exporter.get_finished_spans()
        if s.name.startswith("langgraph.node ")
    }
    outer = spans[f"langgraph.node {carrier}"]
    assert outer.parent is not None
    assert [
        spans[f"langgraph.node {name}"].parent.span_id  # type: ignore[union-attr]
        for name in inner
    ] == [outer.context.span_id] * len(inner)
    assert outer.attributes["meridian.node"] == carrier
