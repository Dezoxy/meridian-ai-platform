"""The second host's spans, its trace context, its telemetry and its isolation
between runs (S037, R4a).

The framework emits spans of its own through the global tracer provider, and
the platform never sets one: the host makes one span per step from the
framework's events. Each case reads what an in-memory exporter received.
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from agent_framework import WorkflowEvent
from agent_framework._workflows._events import WorkflowErrorDetails
from dbsupport import DatabaseHandle
from hostflows import Dials, brief_factory
from hostsupport import (
    AGENT,
    BriefWorld,
    Gateway,
    in_leg_thread,
    in_leg_threads,
    make_world,
)
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR, REPO_ROOT, owner_rows
from toolsupport import add_run, tracer_of

from meridian.platform.common.telemetry import configure_propagation
from meridian.runtime.agent_framework_host import AgentFrameworkHost, StepSpans
from meridian.runtime.runs import RunIdentity, RunOutcome
from meridian.runtime.tool_client import ToolCallLimit

CANARY = "canary-claim-text-c35a"
START_INPUT = {"claim_id": "CLM-0001"}
STEPS = ["gather", "draft", "ask"]
PROCESS_SECONDS = 120
TESTS_DIR = Path(__file__).resolve().parents[1]
DSN_VARIABLE = "HOST_TEST_DSN"
# A leg of the second host in a process of its own, with nothing configured:
# prints the global tracer and meter providers before and after.
PROBE = """
import json, sys, os, uuid
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import httpx
from opentelemetry import metrics, trace
from hostflows import yield_factory
from meridian.platform.registry import load_registry
from meridian.runtime.agent_framework_host import AgentFrameworkHost
from meridian.runtime.model_client import ModelClient
from meridian.runtime.runs import RunIdentity
from meridian.runtime.tool_client import ToolClient

def providers():
    return [type(trace.get_tracer_provider()).__name__,
            type(metrics.get_meter_provider()).__name__]

before = providers()
run_id, thread_id = uuid.uuid4(), uuid.uuid4()
tracer = trace.get_tracer("probe")
tools = ToolClient({}, registry=load_registry(Path(sys.argv[2])), agent="claims-triage",
    run_id=run_id, tracer=tracer, on_refusal=lambda t: None,
    on_worker_refusal=lambda t, r, w: None, max_calls=1)
model = ModelClient(httpx.Client(base_url="http://gateway.invalid"),
    tenant="claims-triage", agent="claims-triage", run_id=run_id, max_calls=1)
identity = RunIdentity(run_id, thread_id, "claims-triage", "claims-triage", "CLM-0001")
dsn = os.environ["HOST_TEST_DSN"]
host = AgentFrameworkHost(yield_factory({"done": True}), dsn=dsn)
outcome = host.start(identity, model, tools, tracer, {})
host.forget(identity)
print(json.dumps({"before": before, "after": providers(), "status": outcome.status}))
"""


@pytest.fixture
def world(fresh_database: DatabaseHandle, plant: Callable[..., Path]) -> BriefWorld:
    return make_world(fresh_database, plant)


def host_over(world: BriefWorld, dials: Dials) -> AgentFrameworkHost:
    return AgentFrameworkHost(brief_factory(dials), dsn=world.dsn())


def start(host: AgentFrameworkHost, world: BriefWorld) -> RunOutcome:
    return in_leg_thread(
        lambda: host.start(
            world.identity,
            world.model(),
            world.tools(),
            tracer_of(world.exporter),
            START_INPUT,
        )
    )


def resume(host: AgentFrameworkHost, world: BriefWorld) -> RunOutcome:
    return in_leg_thread(
        lambda: host.resume(
            world.identity, world.model(), world.tools(), tracer_of(world.exporter), {}
        )
    )


def leg_span(world: BriefWorld, work: Callable[[], Any]) -> ReadableSpan:
    """Run ``work`` under a current span named ``leg`` and return that span, as
    the runtime's leg runs under its request's span."""
    configure_propagation()
    tracer = tracer_of(world.exporter)
    with tracer.start_as_current_span("leg"):
        work()
    return next(s for s in world.exporter.get_finished_spans() if s.name == "leg")


def spans_named(world: BriefWorld, prefix: str) -> list[ReadableSpan]:
    return [s for s in world.exporter.get_finished_spans() if s.name.startswith(prefix)]


def everything_spans_hold(world: BriefWorld) -> str:
    spans = world.exporter.get_finished_spans()
    assert spans, "no span was finished, so nothing was checked"
    return " ".join(
        [
            *(s.status.description or "" for s in spans),
            *(s.name for s in spans),
            *(str(v) for s in spans for v in (s.attributes or {}).values()),
            *(str(v) for s in spans for e in s.events for v in e.attributes.values()),
            *(e.name for s in spans for e in s.events),
        ]
    )


# ── one span per step ───────────────────────────────────────────────────────
def test_each_step_has_a_span_named_and_attributed_as_a_langgraph_node_s_is(
    world: BriefWorld,
) -> None:
    host = host_over(world, Dials())

    leg = leg_span(world, lambda: start(host, world))

    steps = spans_named(world, "agent_framework.node ")
    assert [s.name for s in steps] == [f"agent_framework.node {n}" for n in STEPS]
    for step, name in zip(steps, STEPS, strict=True):
        # The same three attributes a LangGraph node's span has.
        assert dict(step.attributes or {}) == {
            "meridian.node": name,
            "meridian.run_id": str(world.run_id),
            "meridian.agent": AGENT,
        }
        assert step.status.status_code == trace.StatusCode.UNSET
        assert step.parent is not None
        assert step.parent.span_id == leg.context.span_id


def test_the_clients_spans_are_siblings_of_the_steps_not_their_children(
    world: BriefWorld,
) -> None:
    host = host_over(world, Dials())

    leg = leg_span(world, lambda: start(host, world))

    tools = [s for s in world.exporter.get_finished_spans() if s.name == "runtime.tool"]
    assert len(tools) == 3  # two reads and the approval request
    assert {s.parent.span_id for s in tools if s.parent} == {leg.context.span_id}


def test_a_resumed_leg_makes_spans_for_the_steps_it_runs(world: BriefWorld) -> None:
    host = host_over(world, Dials())
    start(host, world)
    world.exporter.clear()

    leg = leg_span(world, lambda: resume(host, world))

    steps = spans_named(world, "agent_framework.node ")
    assert [s.name for s in steps] == [
        "agent_framework.node ask",
        "agent_framework.node file",
    ]
    assert {s.parent.span_id for s in steps if s.parent} == {leg.context.span_id}


def test_a_failed_step_s_span_is_an_error_with_the_class_name_and_no_content(
    world: BriefWorld,
) -> None:
    world.gateway.reply = {
        **world.gateway.reply,
        "output": {"text": CANARY, "finish_reason": "stop"},
    }
    dials = Dials(fail_in={"ask"}, message=f"the brief {CANARY} was refused")
    host = host_over(world, dials)

    def failing_start() -> None:
        with pytest.raises(Exception, match=CANARY):
            start(host, world)

    leg_span(world, failing_start)

    by_name = {s.name: s for s in spans_named(world, "agent_framework.node ")}
    assert by_name["agent_framework.node ask"].status.status_code == (
        trace.StatusCode.ERROR
    )
    assert by_name["agent_framework.node ask"].status.description == "StepFailure"
    assert by_name["agent_framework.node draft"].status.status_code == (
        trace.StatusCode.UNSET
    )
    # The brief was in the messages the events carried, and the failure's text
    # quoted it: neither is on any span the leg made.
    assert CANARY not in everything_spans_hold(world)


def test_a_step_that_a_client_failed_has_a_span_marked_with_the_clients_class(
    world: BriefWorld,
) -> None:
    host = host_over(world, Dials())

    def limited_start() -> None:
        with pytest.raises(ToolCallLimit):
            in_leg_thread(
                lambda: host.start(
                    world.identity,
                    world.model(),
                    world.tools(max_calls=1),
                    tracer_of(world.exporter),
                    START_INPUT,
                )
            )

    leg_span(world, limited_start)

    (gather,) = spans_named(world, "agent_framework.node gather")
    assert gather.status.status_code == trace.StatusCode.ERROR
    assert gather.status.description == "ToolCallLimit"


def test_every_span_of_a_leg_that_paused_is_ended_and_exported(
    world: BriefWorld,
) -> None:
    leg_span(world, lambda: start(host_over(world, Dials()), world))

    # A span that is not ended is not exported: the pause's own step is here.
    assert "agent_framework.node ask" in {
        s.name for s in world.exporter.get_finished_spans()
    }


def span_maker() -> tuple[StepSpans, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    identity = RunIdentity(
        uuid.uuid4(), uuid.uuid4(), AGENT, "claims-triage", "CLM-0001"
    )
    return StepSpans(tracer_of(exporter), identity), exporter


def test_a_span_left_open_when_a_leg_ends_is_ended_by_close() -> None:
    spans, exporter = span_maker()

    spans.observe(WorkflowEvent.executor_invoked("ask", {"claim": CANARY}))
    assert exporter.get_finished_spans() == ()
    spans.close()

    (span,) = exporter.get_finished_spans()
    assert span.name == "agent_framework.node ask"
    assert span.status.status_code == trace.StatusCode.UNSET
    assert CANARY not in str(span.attributes)


def test_a_step_invoked_again_before_it_completed_leaves_no_span_open() -> None:
    spans, exporter = span_maker()

    spans.observe(WorkflowEvent.executor_invoked("ask"))
    spans.observe(WorkflowEvent.executor_invoked("ask"))
    spans.observe(WorkflowEvent.executor_completed("ask"))

    assert len(exporter.get_finished_spans()) == 2


def test_an_error_type_that_is_not_a_class_name_never_reaches_a_span() -> None:
    spans, exporter = span_maker()
    details = WorkflowErrorDetails(error_type=f"the brief {CANARY}", message=CANARY)

    spans.observe(WorkflowEvent.executor_invoked("ask"))
    spans.observe(WorkflowEvent.executor_failed("ask", details))

    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.status.description == "unknown"
    assert CANARY not in str(span.status) + str(span.attributes)


# ── the trace context ───────────────────────────────────────────────────────
def test_the_legs_trace_reaches_the_gateway_and_both_tool_servers(
    world: BriefWorld,
) -> None:
    host = host_over(world, Dials())

    leg = leg_span(world, lambda: start(host, world))

    trace_id = leg.context.trace_id
    carried = trace.get_current_span(
        propagate.extract({"traceparent": world.gateway.seen[0].headers["traceparent"]})
    ).get_span_context()
    assert carried.trace_id == trace_id
    servers = {
        s.resource.attributes["service.name"]
        for s in world.exporter.get_finished_spans()
        if s.context.trace_id == trace_id
    }
    assert {"policy-mcp", "claims-mcp"} <= servers


# ── the framework's own telemetry stays off ─────────────────────────────────
def test_a_leg_sets_no_global_provider_in_a_process_with_none_configured(
    world: BriefWorld,
) -> None:
    done = subprocess.run(
        [sys.executable, "-c", PROBE, str(TESTS_DIR), str(REGISTRY_DIR)],
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
        check=False,
        cwd=REPO_ROOT,
        env={**os.environ, DSN_VARIABLE: world.dsn()},
    )

    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen["status"] == "Completed"
    # The API's own no-op providers, before and after: the framework, which
    # reads the global provider and never sets one, made no real span.
    assert seen["before"] == seen["after"]
    assert seen["after"][0] == "ProxyTracerProvider"
    assert "Proxy" in seen["after"][1]


# ── two legs at once ────────────────────────────────────────────────────────
def test_two_legs_of_two_runs_at_once_do_not_see_each_other(
    world: BriefWorld,
) -> None:
    other = BriefWorld(
        **{
            **vars(world),
            "run_id": add_run(world.db, agent=AGENT, tenant=world.identity.tenant),
            "thread_id": uuid.uuid4(),
            "gateway": Gateway(),
            "exporter": world.exporter,
        }
    )
    for owner, text in ((world, "first run's brief"), (other, "second run's brief")):
        owner.gateway.reply = {
            **owner.gateway.reply,
            "output": {"text": text, "finish_reason": "stop"},
        }
    # Both legs wait in their first step until the other is there too.
    meet = threading.Barrier(2)
    dials = (Dials(meet=meet), Dials(meet=meet))

    def leg_of(owner: BriefWorld, own: Dials) -> Callable[[], RunOutcome]:
        host = host_over(owner, own)
        return lambda: host.start(
            owner.identity,
            owner.model(),
            owner.tools(),
            tracer_of(owner.exporter),
            START_INPUT,
        )

    first, second = in_leg_threads(leg_of(world, dials[0]), leg_of(other, dials[1]))

    assert first == RunOutcome("AwaitingApproval", {"brief": "first run's brief"})
    assert second == RunOutcome("AwaitingApproval", {"brief": "second run's brief"})
    rows = owner_rows(
        world.db,
        "SELECT thread_id, count(*), bool_and(workflow_name = %s) "
        "FROM runtime.workflow_checkpoints GROUP BY thread_id ORDER BY thread_id",
        (AGENT,),
    )
    assert {row[0] for row in rows} == {str(world.thread_id), str(other.thread_id)}
    assert all(row[1] == 4 and row[2] for row in rows)
    # Each gateway saw only its own run's calls.
    assert {r.headers["X-Meridian-Run"] for r in world.gateway.seen} == {
        str(world.run_id)
    }
    assert {r.headers["X-Meridian-Run"] for r in other.gateway.seen} == {
        str(other.run_id)
    }
