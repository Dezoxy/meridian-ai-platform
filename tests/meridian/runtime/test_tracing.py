"""One span per graph node, current while the node runs."""

from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.runtime.tracing import NodeSpans

RUN_ID = uuid4()


class State(TypedDict, total=False):
    output: dict
    note: str


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


def run(exporter: InMemorySpanExporter, nodes: dict, payload: object, thread: str):
    provider = make_tracer_provider("agent-runtime", exporter)
    graph = StateGraph(State)
    previous = START
    for name, fn in nodes.items():
        graph.add_node(name, fn)
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, END)
    compiled = graph.compile(checkpointer=MemorySaver())
    handler = NodeSpans(provider.get_tracer("t"), run_id=RUN_ID, agent="claims-triage")
    config = {"configurable": {"thread_id": thread}, "callbacks": [handler]}
    return compiled, config, provider


def spans_named(exporter: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


def test_each_node_gets_a_span_with_its_identifiers(
    exporter: InMemorySpanExporter,
) -> None:
    compiled, config, _ = run(
        exporter,
        {"first": lambda s: {"note": "a"}, "second": lambda s: {"output": {}}},
        {},
        "t1",
    )

    compiled.invoke({}, config)

    names = [s.name for s in exporter.get_finished_spans()]
    assert names == ["langgraph.node first", "langgraph.node second"]
    first = spans_named(exporter, "langgraph.node first")[0]
    assert dict(first.attributes) == {
        "meridian.node": "first",
        "meridian.run_id": str(RUN_ID),
        "meridian.agent": "claims-triage",
    }


def test_the_node_span_is_current_inside_the_node(
    exporter: InMemorySpanExporter,
) -> None:
    provider_holder: dict = {}

    def node(_: State) -> State:
        tracer = provider_holder["provider"].get_tracer("inner")
        with tracer.start_as_current_span("inside"):
            pass
        return {"output": {}}

    compiled, config, provider = run(exporter, {"work": node}, {}, "t2")
    provider_holder["provider"] = provider

    compiled.invoke({}, config)

    (node_span,) = spans_named(exporter, "langgraph.node work")
    (inside,) = spans_named(exporter, "inside")
    assert inside.parent.span_id == node_span.context.span_id
    assert inside.context.trace_id == node_span.context.trace_id


def test_a_pause_for_approval_is_not_an_error(exporter: InMemorySpanExporter) -> None:
    def ask(_: State) -> State:
        return {"note": interrupt("approve?")}

    compiled, config, _ = run(exporter, {"ask": ask}, {}, "t3")

    compiled.invoke({}, config)
    assert compiled.get_state(config).interrupts
    compiled.invoke(Command(resume="yes"), config)

    spans = spans_named(exporter, "langgraph.node ask")
    assert len(spans) == 2
    assert all(s.status.status_code is StatusCode.UNSET for s in spans)


def test_a_failing_node_marks_its_span_as_an_error_without_the_message(
    exporter: InMemorySpanExporter,
) -> None:
    def boom(_: State) -> State:
        raise RuntimeError("claimant secret text")

    compiled, config, _ = run(exporter, {"boom": boom}, {}, "t4")

    with pytest.raises(RuntimeError):
        compiled.invoke({}, config)

    (span,) = spans_named(exporter, "langgraph.node boom")
    assert span.status.status_code is StatusCode.ERROR
    assert "claimant secret text" not in str(span.status.description)


def test_the_graph_itself_has_no_node_span(exporter: InMemorySpanExporter) -> None:
    compiled, config, _ = run(exporter, {"only": lambda s: {"output": {}}}, {}, "t5")

    compiled.invoke({}, config)

    assert [s.name for s in exporter.get_finished_spans()] == ["langgraph.node only"]
