"""Probe 1: the runtime's synchronous clients inside the framework's async steps.

A leg is a plain thread that runs ``asyncio.run(workflow.run(...))``. The steps
are coroutines on that loop; ``ToolClient.call`` and ``ModelClient.chat`` block
their thread. Three ways to call them from a step: (a) directly with no kept
transport, (b) directly with a kept ``ToolTransport``, (c) through
``asyncio.to_thread``.
"""

import asyncio
from typing import Any

import anyio
import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from s037probe.flow import CLAIM, CallMode, Deps, StepFailure, build_workflow
from s037probe.support import (
    GATEWAY_REPLY,
    Kept,
    StandIn,
    a_valid_answer,
    application_log,
    gateway_client,
    model_client,
    run_in_plain_thread,
    tool_client,
    tracer_of,
)

from meridian.runtime.failures import failure_reason
from meridian.runtime.model_client import ModelCallLimitError
from meridian.runtime.tool_client import ToolCallLimit, ToolUnavailable
from meridian.runtime.tool_transport import ToolTransport

# How long a handler waits to learn whether the loop above could run; a
# watchdog, never asserted on.
LOOP_PROBE_SECONDS = 1.0


def leg(deps: Deps) -> Any:
    """What the runtime's leg does: a new loop in a plain thread."""
    workflow = build_workflow(deps)
    return run_in_plain_thread(lambda: asyncio.run(workflow.run(CLAIM)))


def servers_of(target: Any) -> dict[str, Any]:
    return {"policy-mcp": target, "claims-mcp": target}


def test_a_direct_call_without_a_kept_transport_fails_in_the_nested_loop(
    exporter: InMemorySpanExporter, stand_in: StandIn, stand_in_url: str
) -> None:
    http, _ = gateway_client()
    for target in (stand_in.server, stand_in_url):
        tools = tool_client(servers_of(target), exporter)
        deps = Deps(tools, model_client(http), tracer_of(exporter), mode="direct")

        with application_log() as records, pytest.raises(ToolUnavailable) as raised:
            leg(deps)

        # The SDK's RuntimeError never reaches the step: the client turns every
        # exception into ToolUnavailable and logs the class name of the cause.
        assert raised.value.__cause__ is None
        logged = [r.getMessage() for r in records if r.levelname == "ERROR"]
        assert "tool call failed: RuntimeError" in logged
        assert failure_reason(raised.value) == "tool-unavailable"
        assert deps.counts["gather"] == 1
        assert deps.counts["draft"] == 0
        outcomes = [
            s.attributes.get("meridian.tool_outcome")
            for s in exporter.get_finished_spans()
            if s.name == "runtime.tool"
        ]
        assert outcomes[-1] == "unavailable"
        exporter.clear()


def tool_calls_that_saw_a_running_loop(
    mode: CallMode,
    exporter: InMemorySpanExporter,
    stand_in: StandIn,
    url: str,
) -> tuple[list[bool], Any]:
    """Run the leg over HTTP with a kept transport; for each tool call, whether
    a task on the loop above the client could run while the call was served."""
    holder: dict[str, Deps] = {}
    saw: list[bool] = []

    def answer(name: str, arguments: Any) -> Any:
        saw.append(holder["deps"].released.wait(timeout=LOOP_PROBE_SECONDS))
        return a_valid_answer(name, arguments)

    stand_in.answer = answer
    http, _ = gateway_client()
    transport = ToolTransport()
    try:
        tools = tool_client(servers_of(url), exporter, transport=transport)
        deps = Deps(tools, model_client(http), tracer_of(exporter), mode=mode)
        deps.watch_loop = True
        holder["deps"] = deps
        return saw, leg(deps)
    finally:
        transport.close()


def test_the_sdks_own_error_for_a_nested_loop_is_a_runtime_error() -> None:
    async def step() -> None:
        anyio.run(anyio.sleep, 0)

    with pytest.raises(RuntimeError, match="Already running"):
        asyncio.run(step())


def test_a_direct_call_with_a_kept_transport_works_but_blocks_the_loop(
    exporter: InMemorySpanExporter, stand_in: StandIn, stand_in_url: str
) -> None:
    saw, result = tool_calls_that_saw_a_running_loop(
        "direct", exporter, stand_in, stand_in_url
    )

    # It works: the leg reaches the pause with every call answered.
    assert len(result.get_request_info_events()) == 1
    # The task that would set the event never ran while the client held the
    # loop's thread, in any of the three tool calls before the pause (two reads
    # and one write; the draft step's model call is not a tool call).
    assert saw == [False, False, False]


def test_a_call_through_a_worker_thread_does_not_block_the_loop(
    exporter: InMemorySpanExporter, stand_in: StandIn, stand_in_url: str
) -> None:
    saw, result = tool_calls_that_saw_a_running_loop(
        "thread", exporter, stand_in, stand_in_url
    )

    assert len(result.get_request_info_events()) == 1
    assert saw == [True, True, True]


def test_a_direct_model_call_blocks_the_loop_and_one_through_a_thread_does_not(
    exporter: InMemorySpanExporter, stand_in: StandIn
) -> None:
    for mode, expected in (("direct", False), ("thread", True)):
        holder: dict[str, Deps] = {}
        saw: list[bool] = []

        def handler(
            request: httpx.Request,
            holder: dict[str, Deps] = holder,
            saw: list[bool] = saw,
        ) -> httpx.Response:
            saw.append(holder["deps"].released.wait(timeout=LOOP_PROBE_SECONDS))
            return httpx.Response(200, json=GATEWAY_REPLY)

        http, _ = gateway_client(handler)
        # The tools go through a thread in both runs: the model call is the one
        # whose way varies.
        tools = tool_client(servers_of(stand_in.server), exporter)
        deps = Deps(tools, model_client(http), tracer_of(exporter), mode="thread")
        deps.model_mode = mode
        deps.watch_loop = True
        holder["deps"] = deps

        leg(deps)

        assert saw == [expected]


def test_every_way_of_calling_gives_the_step_the_clients_own_exception_type(
    exporter: InMemorySpanExporter, kept: Kept
) -> None:
    http, _ = gateway_client()
    for mode in ("direct", "thread"):
        tools = tool_client(
            kept.servers, exporter, max_calls=1, transport=kept.transport
        )
        deps = Deps(tools, model_client(http), tracer_of(exporter), mode=mode)

        with pytest.raises(ToolCallLimit) as raised:
            leg(deps)

        # Re-raised from workflow.run with its own type: not wrapped in an
        # exception group, not turned into an event the host must look for.
        assert type(raised.value) is ToolCallLimit
        assert not isinstance(raised.value, BaseExceptionGroup)
        assert failure_reason(raised.value) == "tool-call-limit"
        assert deps.counts["gather"] == 1
        assert deps.counts["draft"] == 0


def test_the_model_call_limit_reaches_the_host_with_its_type(
    exporter: InMemorySpanExporter, kept: Kept
) -> None:
    http, seen = gateway_client()
    for mode in ("direct", "thread"):
        tools = tool_client(kept.servers, exporter, transport=kept.transport)
        deps = Deps(
            tools, model_client(http, max_calls=0), tracer_of(exporter), mode=mode
        )

        with pytest.raises(ModelCallLimitError) as raised:
            leg(deps)

        assert failure_reason(raised.value) == "model-call-limit"
        assert deps.counts["draft"] == 1
    assert seen == []


def test_an_exception_of_a_step_reaches_the_host_with_its_type_and_no_event(
    exporter: InMemorySpanExporter, stand_in: StandIn
) -> None:
    http, _ = gateway_client()
    tools = tool_client(servers_of(stand_in.server), exporter)
    deps = Deps(tools, model_client(http), tracer_of(exporter), mode="thread")
    deps.fail_in.add("draft")

    with pytest.raises(StepFailure) as raised:
        leg(deps)

    assert type(raised.value) is StepFailure
    assert not isinstance(raised.value, BaseExceptionGroup)
    assert failure_reason(raised.value) == "unexpected"


def test_the_clients_trace_context_follows_a_call_through_a_worker_thread(
    exporter: InMemorySpanExporter, stand_in: StandIn, kept: Kept
) -> None:
    http, requests = gateway_client()
    for mode in ("direct", "thread"):
        exporter.clear()
        requests.clear()
        stand_in.calls.clear()
        tools = tool_client(kept.servers, exporter, transport=kept.transport)
        deps = Deps(tools, model_client(http), tracer_of(exporter), mode=mode)

        leg(deps)

        spans = exporter.get_finished_spans()
        step = {s.name: s for s in spans if s.name.startswith("step.")}
        tool_spans = [s for s in spans if s.name == "runtime.tool"]
        # The client's span is a child of the step's, in both ways of calling.
        gather_children = [
            s
            for s in tool_spans
            if s.parent.span_id == step["step.gather"].context.span_id
        ]
        assert len(gather_children) == 2
        # The model client has no span of its own: it injects the current
        # context, which is the step's when the thread copied the context.
        (request,) = requests
        traceparent = request.headers["traceparent"]
        assert traceparent.split("-")[2] == format(
            step["step.draft"].context.span_id, "016x"
        )
        # The tool call carries the context of its own span in its _meta.
        first = stand_in.calls[0].meta["traceparent"]
        assert first.split("-")[2] == format(gather_children[0].context.span_id, "016x")
