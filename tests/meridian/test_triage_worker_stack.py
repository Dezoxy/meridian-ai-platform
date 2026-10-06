"""S031's two ends of one limit, through the whole stack (``stacksupport``): a
worker's tool list is enforced by the runtime's tool client and again by the
tool servers, and each end holds when the other is gone.

The first test reads what a real triage sends: every call names the worker the
registry gives its tool. The other two plant the graph (the runtime loads
``build`` from its entry point, which a test replaces before the stack is built)
so that one node calls through the wrong worker: once through the runtime's
view, which refuses it before anything is sent, and once through a raw client
that bypasses the view, which the tool server refuses.
"""

import functools
import uuid
from collections.abc import Callable
from typing import Any

import anyio
import pytest
from dbsupport import DatabaseHandle
from servicesupport import audit_events, owner_rows
from stacksupport import CLAIMS, Stack, build_stack, service_of

from meridian.platform.toolserver.wire import META_REFUSAL, META_RUN, META_WORKER
from meridian.runtime import tool_client
from meridian.runtime.tool_client import ToolClient, ToolRefused
from meridian.workloads.claims_triage import graph as graph_module

# CLM-0011: in force with a candidate exclusion, so the run searches four
# times, asks the model once and is referred to an adjuster.
CLAIM = "CLM-0011"
TOOL_SERVERS = {"policy-mcp", "knowledge-mcp", "claims-mcp"}
# Every tool call of a triage and its decision, with the worker that holds it.
WHOLE_RUN = [
    ("policy_lookup", "intake"),
    ("claim_history", "intake"),
    *[("wording_search", "terms")] * 4,
    ("request_approval", "approvals"),
    ("approval_outcome", "approvals"),
    ("add_claim_note", "approvals"),
]


def tool_spans(stack: Stack, name: str, services: set[str]) -> list[Any]:
    return [
        s
        for s in stack.exporter.get_finished_spans()
        if s.name == name and service_of(s) in services
    ]


def plant_graph(
    monkeypatch: pytest.MonkeyPatch, wrap: Callable[[ToolClient], Any]
) -> None:
    """The workload's graph, built over ``wrap(tools)`` instead of the tools.
    ``wraps`` keeps the factory's module and signature, which the runtime checks
    when it loads a graph."""
    real = graph_module.build

    @functools.wraps(real)
    def build(model: Any, tools: Any) -> Any:
        return real(model, wrap(tools))

    monkeypatch.setattr(graph_module, "build", build)


class WrongWorker:
    """Hands the node that asks for ``intake`` the view of ``terms``."""

    def __init__(self, tools: ToolClient) -> None:
        self._tools = tools

    def for_worker(self, worker: str) -> ToolClient:
        return self._tools.for_worker("terms" if worker == "intake" else worker)


class RawCall:
    """A call as the worker ``terms`` that the runtime's view never sees: the
    SDK client straight to the tool server, with the run ID and a worker key."""

    def __init__(self, tools: ToolClient, worker: str) -> None:
        self._tools = tools
        self._worker = worker

    def call(self, tool: str, arguments: Any, *, step: str | None = None) -> Any:
        tools = self._tools
        spec = tools._registry.tool(tool)
        assert spec is not None
        meta = {META_RUN: str(tools._run_id), META_WORKER: self._worker}
        answer = anyio.run(
            tool_client._exchange,
            tools._servers[spec.server],
            tool,
            dict(arguments),
            meta,
            True,
        )
        assert answer.is_error is True
        raise ToolRefused(tool, answer.meta[META_REFUSAL])


class BypassingView:
    def __init__(self, tools: ToolClient) -> None:
        self._tools = tools

    def for_worker(self, worker: str) -> RawCall:
        return RawCall(self._tools, "terms" if worker == "intake" else worker)


def failed_events(db: DatabaseHandle, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return [
        (e["service"], e["event"], e["tool"], e["outcome"], e["reason"])
        for e in audit_events(db, run_id)
        if e["event"] != "model.call"
    ]


def refusal_workers(db: DatabaseHandle, run_id: uuid.UUID) -> list[str | None]:
    return [
        e["worker"]
        for e in audit_events(db, run_id)
        if e["event"] == "tool.call" and e["outcome"] == "refused"
    ]


def test_every_tool_call_of_a_triage_and_its_decision_names_the_worker_that_holds_it(
    fresh_database: DatabaseHandle,
) -> None:
    stack = build_stack(fresh_database)
    posted = stack.post(CLAIMS[CLAIM])
    assert posted.status_code == 201

    decided = stack.decide(CLAIM, "approve")

    assert decided.status_code == 200, decided.text
    runtime = tool_spans(stack, "runtime.tool", {"agent-runtime"})
    servers = tool_spans(stack, "tool.call", TOOL_SERVERS)
    assert [
        (s.attributes["meridian.tool"], s.attributes["meridian.worker"])
        for s in runtime
    ] == WHOLE_RUN
    assert [
        (s.attributes["meridian.tool"], s.attributes["meridian.worker"])
        for s in servers
    ] == WHOLE_RUN
    assert {s.attributes["meridian.tool_outcome"] for s in servers} == {"completed"}
    # The audit log says the same: each row a tool server wrote names the worker.
    rows = [
        e
        for e in audit_events(fresh_database, uuid.UUID(posted.json()["run_id"]))
        if e["event"] == "tool.call"
    ]
    assert [(r["tool"], r["worker"]) for r in rows] == WHOLE_RUN
    assert {(r["outcome"], r["reason"]) for r in rows} == {("completed", None)}
    assert {r["service"] for r in rows} == {"policy-mcp", "knowledge-mcp", "claims-mcp"}


def test_a_node_that_calls_through_the_wrong_worker_is_refused_by_the_runtime_alone(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant_graph(monkeypatch, WrongWorker)
    stack = build_stack(fresh_database)

    response = stack.post(CLAIMS[CLAIM])

    assert response.status_code == 502
    run_id = uuid.UUID(response.json()["run_id"])
    assert failed_events(fresh_database, run_id) == [
        ("agent-runtime", "run.started", None, "started", None),
        (
            "agent-runtime",
            "tool.call",
            "policy_lookup",
            "refused",
            "worker-tool-not-allowed",
        ),
        (
            "agent-runtime",
            "run.failed",
            "policy_lookup",
            "failed",
            "worker-tool-not-allowed",
        ),
        ("claims-api", "claim.triage_failed", None, "triage_failed", "triage-failed"),
    ]
    # The runtime's row names the view's worker: the one the graph asked for.
    assert refusal_workers(fresh_database, run_id) == ["terms"]
    # Nothing reached a tool server: no row of theirs, no span of theirs.
    assert tool_spans(stack, "tool.call", TOOL_SERVERS) == []
    assert owner_rows(
        fresh_database,
        "SELECT status FROM runtime.runs WHERE run_id = %s",
        (run_id,),
    ) == [("Failed",)]


def test_a_call_that_bypasses_the_runtimes_view_is_refused_by_the_tool_server_alone(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant_graph(monkeypatch, BypassingView)
    stack = build_stack(fresh_database)

    response = stack.post(CLAIMS[CLAIM])

    assert response.status_code == 502
    run_id = uuid.UUID(response.json()["run_id"])
    assert failed_events(fresh_database, run_id) == [
        ("agent-runtime", "run.started", None, "started", None),
        (
            "policy-mcp",
            "tool.call",
            "policy_lookup",
            "refused",
            "worker-tool-not-allowed",
        ),
        ("agent-runtime", "run.failed", "policy_lookup", "failed", "tool-refused"),
        ("claims-api", "claim.triage_failed", None, "triage_failed", "triage-failed"),
    ]
    # The row of the tool server names the worker it was sent and accepted as
    # one of the agent's.
    assert refusal_workers(fresh_database, run_id) == ["terms"]
    # The runtime refused nothing: its client was never asked.
    assert tool_spans(stack, "runtime.tool", {"agent-runtime"}) == []
    (span,) = tool_spans(stack, "tool.call", TOOL_SERVERS)
    assert span.attributes["meridian.worker"] == "terms"
    assert span.attributes["meridian.reason"] == "worker-tool-not-allowed"
