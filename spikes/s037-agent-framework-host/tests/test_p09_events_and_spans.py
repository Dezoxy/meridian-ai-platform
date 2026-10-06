"""Probe 9: events and spans. What a host can make spans from, whether the
framework takes a tracer provider, and what it does with none."""

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_framework import Workflow, WorkflowBuilder
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from s037probe.flow import CLAIM, StepFailure, build_workflow
from s037probe.rig import leg, plain_deps
from s037probe.support import tracer_of

SPIKE_ROOT = Path(__file__).resolve().parents[1]
PROCESS_SECONDS = 120


async def events_of(workflow: Workflow) -> list[Any]:
    stream = workflow.run(CLAIM, stream=True)
    return [event async for event in stream]


def test_a_stream_gives_an_invoked_and_a_completed_event_for_each_executor() -> None:
    deps = plain_deps()
    workflow = build_workflow(deps)

    events = leg(lambda: events_of(workflow))

    steps = [
        (e.type, e.executor_id)
        for e in events
        if e.type in ("executor_invoked", "executor_completed")
    ]
    assert steps == [
        ("executor_invoked", "gather"),
        ("executor_completed", "gather"),
        ("executor_invoked", "draft"),
        ("executor_completed", "draft"),
        ("executor_invoked", "ask"),
        ("executor_completed", "ask"),
    ]
    # The brackets of a step and of a round are events too, in order.
    kinds = [e.type for e in events]
    assert kinds[:3] == ["started", "status", "superstep_started"]
    assert kinds.count("superstep_started") == kinds.count("superstep_completed") == 3
    assert "request_info" in kinds


def test_an_event_carries_the_payload_and_no_time_so_a_host_stamps_it_on_arrival() -> (
    None
):
    workflow = build_workflow(plain_deps())

    events = leg(lambda: events_of(workflow))

    invoked = next(e for e in events if e.type == "executor_invoked")
    completed = next(e for e in events if e.type == "executor_completed")
    # ``data`` is the message the step received and the messages it sent: claim
    # content. A span must never take it.
    assert invoked.data == CLAIM
    assert [type(m).__name__ for m in completed.data] == ["Gathered"]
    assert not any(hasattr(e, name) for e in events for name in ("timestamp", "time"))


def test_a_failing_step_is_an_event_with_details_and_then_the_exception() -> None:
    deps = plain_deps()
    deps.fail_in.add("draft")
    workflow = build_workflow(deps)
    seen: list[Any] = []

    async def go() -> None:
        stream = workflow.run(CLAIM, stream=True)
        async for event in stream:
            seen.append(event)

    try:
        leg(go)
    except StepFailure:
        failed = True
    else:
        failed = False

    kinds = [e.type for e in seen]
    assert failed
    assert "executor_failed" in kinds
    assert kinds[-2:] == ["failed", "status"]
    details = next(e for e in seen if e.type == "failed").details
    assert details.error_type == "StepFailure"


def test_spans_made_from_events_are_siblings_of_the_clients_spans_not_parents() -> None:
    exporter = InMemorySpanExporter()
    tracer = tracer_of(exporter)
    deps = plain_deps(exporter)
    deps.tracer = None  # the steps open no span of their own
    workflow = build_workflow(deps)

    async def go() -> None:
        with tracer.start_as_current_span("agent.run"):
            open_spans: dict[str, Any] = {}
            stream = workflow.run(CLAIM, stream=True)
            async for event in stream:
                if event.type == "executor_invoked":
                    span = tracer.start_span(f"maf.executor {event.executor_id}")
                    span.set_attribute("meridian.node", event.executor_id)
                    open_spans[event.executor_id] = span
                elif event.type == "executor_completed":
                    open_spans.pop(event.executor_id).end()

    leg(go)

    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert names.count("agent.run") == 1
    nodes = [s for s in spans if s.name.startswith("maf.executor")]
    assert [s.attributes["meridian.node"] for s in nodes] == ["gather", "draft", "ask"]
    root = next(s for s in spans if s.name == "agent.run")
    # The node spans are children of the leg's span, as the host makes them.
    assert {s.parent.span_id for s in nodes} == {root.context.span_id}
    # The client's spans are children of the leg's span too: the node span the
    # host started on the event is not current in the step, so a call the step
    # makes does not nest under it (LangGraph's NodeSpans makes its span current).
    tools = [s for s in spans if s.name == "runtime.tool"]
    assert len(tools) == 3
    assert {s.parent.span_id for s in tools} == {root.context.span_id}


def test_the_framework_takes_no_tracer_provider_as_an_argument() -> None:
    names = set()
    for function in (WorkflowBuilder.__init__, Workflow.__init__, Workflow.run):
        names |= set(inspect.signature(function).parameters)

    assert not {n for n in names if "provider" in n or "tracer" in n}


def probe_process(mode: str) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, "-m", "s037probe.probe_telemetry", mode],
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
        check=True,
        cwd=SPIKE_ROOT,
        env={**os.environ, "PYTHONPATH": str(SPIKE_ROOT / "src")},
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_with_no_global_provider_the_framework_makes_no_real_span_and_sets_none() -> (
    None
):
    seen = probe_process("none")

    # No provider anywhere in the process: the API's proxy provider is still the
    # global one after a full run, the clients and the store included, and the
    # step saw an invalid (non-recording) current span in every step.
    assert seen["provider"] == "ProxyTracerProvider"
    assert seen["valid_in_steps"] == [False] * 3
    assert seen["spans"] == []


def test_with_a_global_provider_the_framework_uses_it_and_spans_carry_no_payload() -> (
    None
):
    seen = probe_process("global")

    assert seen["provider"] == "TracerProvider"
    assert seen["valid_in_steps"] == [True] * 3
    framework = [s for s in seen["spans"] if not s.startswith("tools/call")]
    assert framework == [
        "edge_group.process InternalEdgeGroup",
        "edge_group.process SingleEdgeGroup",
        "executor.process ask",
        "executor.process draft",
        "executor.process gather",
        "message.send",
        "workflow.build",
        "workflow.run",
    ]
    # Not the framework's: the MCP SDK's client makes a span of its own for a
    # call, named after the raw tool name, once a global provider exists.
    assert sorted(set(seen["spans"]) - set(framework)) == [
        "tools/call claim_history",
        "tools/call policy_lookup",
        "tools/call request_approval",
    ]
    assert seen["executor_ids"] == ["ask", "draft", "gather"]
    assert seen["payload_in_attributes"] is False
