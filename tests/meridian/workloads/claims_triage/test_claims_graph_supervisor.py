"""The triage graph's factory, its supervisor and its workers (S014, S031): the
graph returned uncompiled, the nodes and branches of the supervisor, what each
worker's builder receives, the tools each worker may call, the spans, the longest
path against the runtime's limit of steps, and how the runtime finds the graph.
"""

import inspect
import uuid
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any, cast

import pytest
from graphsupport import (
    THREAD,
    StubModel,
    StubTools,
    StubWorkerView,
    compiled,
    facts,
    paused,
    planted_state,
    resume,
)
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR
from toolsupport import tracer_of

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.registry import load_registry
from meridian.runtime import runs
from meridian.runtime.failures import GraphFailure, failure_reason
from meridian.runtime.graphs import load_graph_factory
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient, ToolNotAllowed
from meridian.runtime.tracing import NodeSpans
from meridian.workloads.claims_triage import workers
from meridian.workloads.claims_triage.graph import build

# -- the factory --------------------------------------------------------------


def test_the_graph_is_returned_uncompiled() -> None:
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, StubTools()))

    assert not hasattr(graph, "invoke")


def test_the_supervisor_names_its_nodes_and_routes_in_two_branches() -> None:
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, StubTools()))

    assert list(graph.nodes) == [
        "intake",
        "terms",
        "assessor",
        "propose",
        "request_approval",
        "await_decision",
    ]
    # No policy goes straight to the rules, and only an adjuster's claim goes on
    # to ask for an approval.
    assert set(graph.branches) == {"intake", "propose"}


# -- the supervisor and its workers (S031) ------------------------------------


class Receipts:
    """The arguments each worker's builder was called with, in call order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.received: dict[str, tuple[Any, ...]] = {}
        for name in BUILDERS:
            self._wrap(monkeypatch, name)

    def _wrap(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        real = getattr(workers, name)

        def recording(*args: Any, **kwargs: Any) -> Any:
            assert not kwargs
            self.received[name] = args
            return real(*args)

        monkeypatch.setattr(workers, name, recording)


BUILDERS = (
    "build_intake",
    "build_terms",
    "build_assessor",
    "build_request_approval",
    "build_outcome",
)


def test_each_builder_receives_only_what_its_worker_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipts = Receipts(monkeypatch)
    model, tools = StubModel(), StubTools()

    build(cast(ModelClient, model), cast(ToolClient, tools))

    received = receipts.received
    assert set(received) == set(BUILDERS)
    assert received["build_assessor"] == (model,)
    for builder, worker in [
        ("build_intake", "intake"),
        ("build_terms", "terms"),
        ("build_request_approval", "approvals"),
        ("build_outcome", "approvals"),
    ]:
        (view,) = received[builder]
        assert isinstance(view, StubWorkerView)
        assert view is not tools
        assert view._worker == worker
    # Nobody gets the whole tool client, and only the assessor gets the model:
    # the supervisor holds neither.
    assert [name for name, args in received.items() if tools in args] == []
    assert [name for name, args in received.items() if model in args] == [
        "build_assessor"
    ]


def test_the_assessors_builder_takes_the_model_and_no_tool_client() -> None:
    parameters = inspect.signature(workers.build_assessor).parameters

    assert [(p.name, p.annotation) for p in parameters.values()] == [
        ("model", ModelClient)
    ]
    for builder in BUILDERS:
        if builder != "build_assessor":
            signature = inspect.signature(getattr(workers, builder))
            (parameter,) = signature.parameters.values()
            assert parameter.annotation is ToolClient


def test_the_supervisor_and_its_workers_call_no_tool_but_through_a_view() -> None:
    # StubTools.call, as the real client for an agent with workers, refuses a call
    # with no worker: a whole run, with a pause and a decision, makes none.
    graph, tools = paused()
    tools.recorded = "approve"

    resume(graph, {})

    assert len(tools.workers) == 9


def real_client(worker: str) -> ToolClient:
    """The runtime's own client for the claims-triage agent, with no server: a
    call a view refuses is refused before anything is sent."""
    client = ToolClient(
        {},
        registry=load_registry(REGISTRY_DIR),
        agent="claims-triage",
        run_id=uuid.uuid4(),
        tracer=tracer_of(InMemorySpanExporter()),
        on_refusal=lambda tool: None,
        on_worker_refusal=lambda tool, reason, worker: None,
        max_calls=16,
    )
    return client.for_worker(worker)


PLANTED = [
    pytest.param(workers.build_intake, "terms", (), id="intake-over-the-terms-view"),
    pytest.param(
        workers.build_terms, "intake", ("policy", "chunks"), id="terms-over-intake"
    ),
    pytest.param(
        workers.build_request_approval,
        "intake",
        ("output",),
        id="approvals-request-over-intake",
    ),
    pytest.param(workers.build_outcome, "terms", (), id="approvals-outcome-over-terms"),
]


@pytest.mark.parametrize(("builder", "wrong_view", "keys"), PLANTED)
def test_a_worker_that_calls_a_tool_of_another_worker_is_refused(
    builder: Callable[[ToolClient], CompiledStateGraph],
    wrong_view: str,
    keys: tuple[str, ...],
) -> None:
    """Each worker's first call is a tool its own worker holds. Built over the
    view of another worker, the node makes a call that is not on that worker's
    list, and the view refuses it with the reason the runtime records."""
    worker = builder(real_client(wrong_view))

    with pytest.raises(ToolNotAllowed) as raised:
        worker.invoke(planted_state(*keys))

    assert failure_reason(raised.value) == "worker-tool-not-allowed"


def test_a_note_with_no_decision_in_the_state_is_a_failure_and_writes_nothing() -> None:
    """``read_outcome`` sets the decision or raises, so the note's node never
    sees none; a state that lacks it is a bug of the graph and writes no note."""
    tools = StubTools()
    outcome = workers.build_outcome(cast(ToolClient, tools.for_worker("approvals")))
    state = {"claim": facts("CLM-0004"), "decision": None}

    with pytest.raises(GraphFailure) as raised:
        outcome.nodes["write_note"].invoke(state)

    assert raised.value.code == "missing-decision"
    assert tools.writes == []


def test_the_pause_node_returns_only_the_key_its_worker_wrote() -> None:
    graph, tools = paused()
    tools.recorded = "withdrawn"
    (pending,) = graph.get_state(THREAD).interrupts

    updates = list(
        graph.stream(Command(resume={pending.id: {}}), THREAD, stream_mode="updates")
    )

    assert updates == [{"await_decision": {"decision": "withdrawn"}}]


def test_the_spans_of_a_workers_nodes_name_it_and_the_supervisors_do_not() -> None:
    exporter = InMemorySpanExporter()
    provider = make_tracer_provider("agent-runtime", exporter)
    handler = NodeSpans(
        provider.get_tracer("test"), run_id=uuid.uuid4(), agent="claims-triage"
    )
    tools = StubTools(recorded="approve")
    graph = compiled(StubModel(), tools)
    config = {**THREAD, "callbacks": [handler]}
    graph.invoke({"claim": facts("CLM-0004")}, config)
    (pending,) = graph.get_state(THREAD).interrupts

    graph.invoke(Command(resume={pending.id: {}}), config)

    worker_of = {
        span.attributes["meridian.node"]: span.attributes.get("meridian.worker")
        for span in exporter.get_finished_spans()
    }
    assert worker_of == {
        "intake": "intake",
        "lookup_policy": "intake",
        "load_history": "intake",
        "terms": "terms",
        "retrieve_terms": "terms",
        "assessor": "assessor",
        "assess": "assessor",
        "propose": None,
        "request_approval": "approvals",
        "await_decision": None,
        "read_outcome": "approvals",
        "write_note": "approvals",
    }


def inner_steps(worker: CompiledStateGraph) -> int:
    return len([name for name in worker.nodes if name != "__start__"])


def test_the_longest_path_fits_the_runtimes_limit_of_steps() -> None:
    """Every graph, the parent and each subgraph, counts its own steps for each
    leg against the one limit (``test_runtime_subgraphs.py``). The parent's first
    leg is five nodes and the step that pauses; the resumed leg is the one node
    that pauses and runs the outcome worker; no worker has more than two nodes."""
    tools, model = StubTools(), StubModel()
    graph = compiled(model, tools)
    first_leg = [
        next(iter(update))
        for update in graph.stream(
            {"claim": facts("CLM-0004")}, THREAD, stream_mode="updates"
        )
    ]
    (pending,) = graph.get_state(THREAD).interrupts
    tools.recorded = "approve"
    resumed = [
        next(iter(update))
        for update in graph.stream(
            Command(resume={pending.id: {}}), THREAD, stream_mode="updates"
        )
    ]
    views = {name: tools.for_worker(name) for name in ("intake", "terms", "approvals")}
    longest_worker = max(
        inner_steps(worker)
        for worker in (
            workers.build_intake(cast(ToolClient, views["intake"])),
            workers.build_terms(cast(ToolClient, views["terms"])),
            workers.build_assessor(cast(ModelClient, model)),
            workers.build_request_approval(cast(ToolClient, views["approvals"])),
            workers.build_outcome(cast(ToolClient, views["approvals"])),
        )
    )

    assert first_leg == [
        "intake",
        "terms",
        "assessor",
        "propose",
        "request_approval",
        "__interrupt__",
    ]
    assert resumed == ["await_decision"]
    assert (longest_worker, len(first_leg), len(resumed)) == (2, 6, 1)
    assert max(longest_worker, len(first_leg), len(resumed)) < runs.RECURSION_LIMIT


def test_the_entry_point_is_installed_from_the_meridian_distribution() -> None:
    (entry,) = [
        e for e in entry_points(group="meridian.graphs") if e.name == "claims-triage"
    ]

    assert entry.value == "meridian.workloads.claims_triage.graph:build"
    assert entry.dist is not None and entry.dist.name == "meridian"


def test_the_runtime_loads_the_workload_graph_through_the_registry() -> None:
    factory = load_graph_factory("claims-triage", load_registry(REGISTRY_DIR))

    assert factory is build
