"""The Agent Runtime's host for Microsoft Agent Framework (S037).

It satisfies ``meridian.runtime.hosts.Host``: the runtime's neutral code starts a
leg, resumes it and forgets the run's checkpoints, and this module does each in
the second framework. With ``runtime.workflow_checkpoints`` it is the only
module of the runtime that imports the framework.

**What a workload publishes.** A factory called with the two asynchronous faces
of the run's clients (``AsyncModelClient``, ``AsyncToolClient``) that returns a
``WorkflowDefinition``: the first step, the edges between steps and the types
the workflow keeps in its messages and requests. A frozen dataclass and not a
callable that receives a builder, because the framework's builder cannot be made
before it knows the first step, and everything else it takes (the workflow's
name, the checkpoint store, the step bound) is the host's: a workload cannot set
or see them. The host also owns ``ResumeMarker``, the one response type a
workload's response handler is typed on.

**A leg** is ``asyncio.run`` of one coroutine, in the thread the runtime gives
it: a new event loop, a new workflow object and a new checkpoint store each
time. The framework is told to run the workflow as a stream, and the host reads
its events for the step spans, then its final result for the pause and the
output.

* The output is the last value the workflow yielded in this leg; it must be an
  object or absent.
* A resume reads the LATEST checkpoint of the run's thread and takes one of
  three ways (see ``_plan_resume``); it never names a checkpoint a caller gave,
  because naming the pause's own checkpoint answers the pause a second time.
* The step bound is ten steps a leg (``RECURSION_LIMIT``): the framework counts
  across legs, so a resume's bound is the restored count plus ten.
* A save the store refused fails the leg: the framework only logs one.
* The framework's own telemetry stays off. It reads the global tracer provider,
  and the platform sets none. The step spans are made here from its events.

Nothing here logs an exception's text: the framework's and the store's can quote
claim content, so a log line has the run, a class name or a fixed word.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NoReturn, cast

from agent_framework import (
    Executor,
    Workflow,
    WorkflowBuilder,
    WorkflowEvent,
    WorkflowRunResult,
)
from agent_framework.exceptions import (
    WorkflowCheckpointException,
    WorkflowConvergenceException,
)
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from meridian.platform.common.telemetry import set_span_attributes
from meridian.runtime.failures import GraphFailure
from meridian.runtime.hosts import AsyncModelClient, AsyncToolClient
from meridian.runtime.model_client import ModelClient
from meridian.runtime.runs import (
    NO_PENDING_PAUSE,
    RECURSION_LIMIT,
    SEVERAL_PENDING_PAUSES,
    RunIdentity,
    RunOutcome,
)
from meridian.runtime.tool_client import ToolClient
from meridian.runtime.workflow_checkpoints import PostgresCheckpointStore

logger = logging.getLogger(__name__)

# The failure words this host adds to the runtime's own (``failure_reason``
# passes a ``GraphFailure``'s code on as it is).
STEP_LIMIT = "step-limit"
CHECKPOINT_NOT_SAVED = "checkpoint-not-saved"
CHECKPOINT_NOT_READ = "checkpoint-not-read"
SPAN_PREFIX = "agent_framework.node"
# A class name, as the framework reports a failed step's: anything else is not
# put on a span.
CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
UNKNOWN_CLASS = "unknown"


@dataclass
class ResumeMarker:
    """The response a pause is answered with. It carries nothing: the workload
    reads the recorded decision through a tool, and the value the runtime's
    resume carries is ignored, as the pause of a LangGraph graph ignores it."""


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """What a workload publishes for this host.

    ``edges`` are plain edges from one step to the next (the only kind this
    host builds). ``state_types`` are every dataclass the workflow keeps in a
    message or a request: a checkpoint is JSON, and a type that is not listed
    here (and is not ``ResumeMarker``) is refused when it is saved, so the leg
    fails at its first checkpoint.
    """

    start: Executor
    edges: tuple[tuple[Executor, Executor], ...]
    state_types: tuple[type, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.start, Executor):
            raise TypeError("a definition's start must be an Executor")
        for edge in self.edges:
            if not (len(edge) == 2 and all(isinstance(end, Executor) for end in edge)):
                raise TypeError("a definition's edges are pairs of Executors")
        for cls in self.state_types:
            if not (isinstance(cls, type) and hasattr(cls, "__dataclass_fields__")):
                raise TypeError("a definition's state types are dataclasses")


type WorkflowFactory = Callable[[AsyncModelClient, AsyncToolClient], WorkflowDefinition]


class _NoClient:
    """Stands where a client would, while a factory is only being checked."""

    def chat(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError("the check of a factory makes no call")

    call = chat


def check_definition(value: object) -> WorkflowDefinition:
    """``value`` when it is a ``WorkflowDefinition``, else a ``TypeError`` that
    names its type: a factory of the other framework returns a graph builder, a
    mistake returns nothing."""
    if not isinstance(value, WorkflowDefinition):
        raise TypeError(
            "an agent-framework agent's factory must return a "
            f"WorkflowDefinition, not {type(value).__name__}"
        )
    return value


def check_factory(factory: object) -> WorkflowDefinition:
    """Check an agent's factory without running a leg: call it with faces over
    clients that refuse every call, check what it returns, and have the
    framework build a workflow from it (which checks the edges). Raises
    ``TypeError`` with a clear sentence for anything this host cannot run: a
    factory of the other framework, a factory that returns nothing, a workflow
    the framework does not accept. The wiring calls it when the service starts.

    What it cannot see is a type a request carries that ``state_types`` omits:
    that is refused when the first checkpoint holding it is saved.
    """
    if not callable(factory):
        raise TypeError("an agent-framework agent's entry point must be a factory")
    faces = (
        AsyncModelClient(cast(ModelClient, _NoClient())),
        AsyncToolClient(cast(ToolClient, _NoClient())),
    )
    try:
        value = factory(*faces)
    except Exception as error:
        raise TypeError(
            "the entry point could not be called with the second host's two "
            f"clients: {type(error).__name__}"
        ) from error
    definition = check_definition(value)
    try:
        _build(definition, "check", None, RECURSION_LIMIT)
    except Exception as error:
        raise TypeError(f"the workflow definition does not build: {error}") from error
    return definition


def _build(
    definition: WorkflowDefinition,
    name: str,
    store: PostgresCheckpointStore | None,
    max_iterations: int,
) -> Workflow:
    builder = WorkflowBuilder(
        max_iterations=max_iterations,
        name=name,
        start_executor=definition.start,
        checkpoint_storage=store,
    )
    for source, target in definition.edges:
        builder.add_edge(source, target)
    return builder.build()


class StepSpans:
    """One span per step, made from the framework's events.

    The span is not current while the step runs (it is started on an event, not
    around the step), so the tool and model spans a step makes are its siblings
    under the leg's span, not its children. Events carry no time: a span starts
    and ends when its event arrives.
    An event's ``data`` is claim content and never reaches a span: a failed
    step's span has the class name the framework reports, and nothing else.
    """

    def __init__(self, tracer: Tracer, identity: RunIdentity) -> None:
        self._tracer = tracer
        self._run_id = str(identity.run_id)
        self._agent = identity.agent
        self._open: dict[str, Span] = {}

    def observe(self, event: WorkflowEvent[Any]) -> None:
        step = str(event.executor_id)
        if event.type == "executor_invoked":
            self._start(step)
        elif event.type == "executor_completed":
            self._end(step, None)
        elif event.type == "executor_failed":
            self._end(step, _class_name(getattr(event.details, "error_type", None)))

    def close(self) -> None:
        """End every span still open: a leg that raised or paused left them."""
        for step in list(self._open):
            self._end(step, None)

    def _start(self, step: str) -> None:
        self._end(step, None)  # a step is open once; never leak a second span
        span = self._tracer.start_span(f"{SPAN_PREFIX} {step}")
        set_span_attributes(
            span,
            {
                "meridian.node": step,
                "meridian.run_id": self._run_id,
                "meridian.agent": self._agent,
            },
        )
        self._open[step] = span

    def _end(self, step: str, error: str | None) -> None:
        span = self._open.pop(step, None)
        if span is None:
            return
        if error is not None:
            span.set_status(Status(StatusCode.ERROR, error))
        span.end()


def _class_name(value: object) -> str:
    if isinstance(value, str) and CLASS_NAME.fullmatch(value):
        return value
    return UNKNOWN_CLASS


@dataclass(frozen=True, slots=True)
class _Plan:
    """How a leg runs: the step bound, and for a resume the checkpoint to restore
    (always the latest) and the responses to give."""

    max_iterations: int
    checkpoint_id: str | None = None
    responses: dict[str, ResumeMarker] | None = None


@dataclass(slots=True)
class _Leg:
    definition: WorkflowDefinition
    store: PostgresCheckpointStore
    identity: RunIdentity
    spans: StepSpans
    plan: _Plan = field(default_factory=lambda: _Plan(RECURSION_LIMIT))


async def _plan_resume(leg: _Leg) -> _Plan:
    """Read the latest checkpoint of the run's thread and decide how to resume.

    * one pending request: answer it with a ``ResumeMarker``;
    * no pending request but messages in flight (the step after the answer
      failed last time, and the framework left the answer in flight): restore
      the checkpoint with no response, and only the failed step runs again;
    * neither, or no checkpoint: ``no-pending-pause``; several pending requests:
      ``several-pending-pauses``. Both raise before anything runs.
    """
    failed = False
    try:
        latest = await leg.store.get_latest(workflow_name=leg.identity.agent)
    except WorkflowCheckpointException:
        failed = True
    if failed:
        logger.error(
            "run %s: its checkpoints could not be read: %s",
            leg.identity.run_id,
            WorkflowCheckpointException.__name__,
        )
        raise GraphFailure(CHECKPOINT_NOT_READ)
    if latest is None:
        raise GraphFailure(NO_PENDING_PAUSE)
    pending = latest.pending_request_info_events
    bound = latest.iteration_count + RECURSION_LIMIT
    if len(pending) > 1:
        raise GraphFailure(SEVERAL_PENDING_PAUSES)
    if len(pending) == 1:
        responses = {request_id: ResumeMarker() for request_id in pending}
        return _Plan(bound, latest.checkpoint_id, responses)
    if any(latest.messages.values()):
        return _Plan(bound, latest.checkpoint_id)
    raise GraphFailure(NO_PENDING_PAUSE)


def _require_saved(leg: _Leg) -> None:
    """Fail the leg when the store refused or lost a save. The framework only
    logs one, so a pause that was never saved would look like a pause."""
    failures = leg.store.failures
    if failures:
        logger.error(
            "run %s: its checkpoints were not saved: %s",
            leg.identity.run_id,
            ", ".join(sorted(set(failures))),
        )
        raise GraphFailure(CHECKPOINT_NOT_SAVED)


async def _run(leg: _Leg, run_input: dict[str, Any] | None) -> RunOutcome:
    try:
        if run_input is None:
            leg.plan = await _plan_resume(leg)
        workflow = _build(
            leg.definition,
            leg.identity.agent,
            leg.store,
            leg.plan.max_iterations,
        )
        stream = workflow.run(
            run_input,
            stream=True,
            responses=leg.plan.responses,
            checkpoint_id=leg.plan.checkpoint_id,
        )
        async for event in stream:
            leg.spans.observe(event)
            if event.type == "superstep_started":
                # The previous step's checkpoint has been saved, or refused, by
                # now: a leg that cannot save stops before it does more.
                _require_saved(leg)
        result = await stream.get_final_response()
    except WorkflowConvergenceException:
        raise GraphFailure(STEP_LIMIT) from None
    finally:
        leg.spans.close()
    _require_saved(leg)
    return _outcome(result)


def _outcome(result: WorkflowRunResult) -> RunOutcome:
    outputs = result.get_outputs()
    output = outputs[-1] if outputs else None
    if output is not None and not isinstance(output, dict):
        raise TypeError("a workflow's output must be an object")
    paused = bool(result.get_request_info_events())
    return RunOutcome("AwaitingApproval" if paused else "Completed", output)


class AgentFrameworkHost:
    """The host of one agent whose workload is a Microsoft Agent Framework
    workflow. ``factory`` is the workload's entry point (see
    ``check_factory``); ``dsn`` is the runtime role's, for the checkpoint store."""

    def __init__(self, factory: WorkflowFactory, *, dsn: str) -> None:
        self._factory = factory
        self._dsn = dsn

    def start(
        self,
        identity: RunIdentity,
        model: ModelClient,
        tools: ToolClient,
        tracer: Tracer,
        run_input: dict[str, Any],
    ) -> RunOutcome:
        return self._leg(identity, model, tools, tracer, run_input)

    def resume(
        self,
        identity: RunIdentity,
        model: ModelClient,
        tools: ToolClient,
        tracer: Tracer,
        value: dict[str, Any],
    ) -> RunOutcome:
        """``value`` is ignored: the pause is answered with a ``ResumeMarker``."""
        return self._leg(identity, model, tools, tracer, None)

    def forget(self, identity: RunIdentity) -> None:
        store = PostgresCheckpointStore(self._dsn, identity.thread_id, ())
        asyncio.run(store.forget())

    def _leg(
        self,
        identity: RunIdentity,
        model: ModelClient,
        tools: ToolClient,
        tracer: Tracer,
        run_input: dict[str, Any] | None,
    ) -> RunOutcome:
        """One leg: ``run_input`` is None for a resume."""
        definition = check_definition(
            self._factory(AsyncModelClient(model), AsyncToolClient(tools))
        )
        store = PostgresCheckpointStore(
            self._dsn, identity.thread_id, (*definition.state_types, ResumeMarker)
        )
        leg = _Leg(definition, store, identity, StepSpans(tracer, identity))
        return asyncio.run(_run(leg, run_input))
