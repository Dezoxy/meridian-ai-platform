"""The probe workflow: the shape of the design's ``claim-brief`` agent.

gather (two tool reads) -> draft (one model call) -> ask (a tool write, then the
pause) -> file (the recorded decision through a tool, then a note). Every step
calls the runtime's real ``ToolClient`` and ``ModelClient``, which are
synchronous; ``Deps.sync`` is the one place that says how a step calls them.
"""

import asyncio
import threading
from collections import Counter
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Literal, Never

from agent_framework import (
    CheckpointStorage,
    Executor,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
    response_handler,
)
from opentelemetry import trace
from opentelemetry.trace import Tracer

WORKFLOW_NAME = "claim-brief"
CallMode = Literal["direct", "thread"]
POLICY = "POL-0049"
CLAIM = "CLM-0001"


@dataclass
class Gathered:
    claim_id: str
    policy_found: bool
    history_entries: int


@dataclass
class Drafted:
    claim_id: str
    brief: str


@dataclass
class AskRequest:
    claim_id: str
    brief: str


@dataclass
class Marker:
    """The host's own response type: it carries nothing, because the workload
    reads the recorded decision through a tool and ignores the resume's value."""


@dataclass
class Filing:
    claim_id: str
    brief: str


CHECKPOINT_TYPES = [Gathered, Drafted, AskRequest, Marker, Filing]


class StepFailure(Exception):
    """Raised by a step the test told to fail."""


@dataclass
class Deps:
    """What the steps are handed: the two runtime clients and the probes' dials."""

    tools: Any
    model: Any
    tracer: Tracer | None = None
    mode: CallMode = "thread"
    counts: Counter[str] = field(default_factory=Counter)
    fail_in: set[str] = field(default_factory=set)
    # How the model client is called, when it differs from the tool client's.
    model_mode: CallMode | None = None
    # A probe's dial: with ``watch_loop`` each client call is preceded by a task
    # on the loop that sets a fresh ``released`` event, so a handler that waits
    # for it learns whether the loop could run while the client's thread was busy.
    watch_loop: bool = False
    released: threading.Event = field(default_factory=threading.Event)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    span_valid_in_step: list[bool] = field(default_factory=list)

    async def sync(
        self,
        fn: Callable[..., Any],
        *args: Any,
        via: CallMode | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call a synchronous client method from a coroutine: on the loop's own
        thread ("direct") or through ``asyncio.to_thread`` ("thread")."""
        if self.watch_loop:
            self.released = threading.Event()
            self._tasks.append(asyncio.create_task(_release(self.released)))
        if (via or self.mode) == "thread":
            return await asyncio.to_thread(fn, *args, **kwargs)
        return fn(*args, **kwargs)

    def enter(self, step: str) -> None:
        self.counts[step] += 1
        # Whether the framework made a real span for this step (probe 9).
        context = trace.get_current_span().get_span_context()
        self.span_valid_in_step.append(context.is_valid)
        if step in self.fail_in:
            raise StepFailure(step)


async def _release(event: threading.Event) -> None:
    await asyncio.sleep(0)
    event.set()


def _span(deps: Deps, name: str) -> Any:
    if deps.tracer is None:
        return nullcontext()
    return deps.tracer.start_as_current_span(name)


class Gather(Executor):
    def __init__(self, deps: Deps) -> None:
        super().__init__(id="gather")
        self._deps = deps

    @handler
    async def run(self, claim_id: str, ctx: WorkflowContext[Gathered]) -> None:
        deps = self._deps
        deps.enter("gather")
        with _span(deps, "step.gather"):
            policy = await deps.sync(
                deps.tools.call, "policy_lookup", {"policy_number": POLICY}
            )
            history = await deps.sync(
                deps.tools.call, "claim_history", {"policy_number": POLICY}
            )
        await ctx.send_message(
            Gathered(
                claim_id,
                policy.data.get("found") is True,
                len(history.data.get("entries", [])),
            )
        )


class Draft(Executor):
    def __init__(self, deps: Deps) -> None:
        super().__init__(id="draft")
        self._deps = deps

    @handler
    async def run(self, gathered: Gathered, ctx: WorkflowContext[Drafted]) -> None:
        deps = self._deps
        deps.enter("draft")
        with _span(deps, "step.draft"):
            reply = await deps.sync(
                deps.model.chat,
                [{"role": "user", "content": f"Brief for {gathered.claim_id}"}],
                via=deps.model_mode,
            )
        await ctx.send_message(Drafted(gathered.claim_id, reply.text))


class Ask(Executor):
    """The pause. ``@response_handler`` registers on the class, so the step that
    asks is a class and the step that reads the answer is its method."""

    def __init__(self, deps: Deps) -> None:
        super().__init__(id="ask")
        self._deps = deps

    @handler
    async def ask(self, drafted: Drafted, ctx: WorkflowContext) -> None:
        deps = self._deps
        deps.enter("ask")
        with _span(deps, "step.ask"):
            await deps.sync(
                deps.tools.call,
                "request_approval",
                {"claim_id": drafted.claim_id, "reason": "brief"},
                step="ask",
            )
        await ctx.request_info(AskRequest(drafted.claim_id, drafted.brief), Marker)

    @response_handler
    async def answered(
        self,
        original_request: AskRequest,
        response: Marker,
        ctx: WorkflowContext[Filing],
    ) -> None:
        deps = self._deps
        deps.enter("answered")
        await ctx.send_message(
            Filing(original_request.claim_id, original_request.brief)
        )


class File(Executor):
    def __init__(self, deps: Deps) -> None:
        super().__init__(id="file")
        self._deps = deps

    @handler
    async def run(self, filing: Filing, ctx: WorkflowContext[Never, dict]) -> None:
        deps = self._deps
        deps.enter("file")
        with _span(deps, "step.file"):
            await deps.sync(
                deps.tools.call, "approval_outcome", {"claim_id": filing.claim_id}
            )
            await deps.sync(
                deps.tools.call,
                "add_claim_note",
                {"claim_id": filing.claim_id, "note": "Brief filed."},
                step="file",
            )
        await ctx.yield_output({"brief": filing.brief, "filed": True})


def build_workflow(
    deps: Deps,
    storage: CheckpointStorage | None = None,
    *,
    name: str = WORKFLOW_NAME,
    max_iterations: int | None = None,
) -> Workflow:
    gather, draft, ask, file = Gather(deps), Draft(deps), Ask(deps), File(deps)
    extra: dict[str, Any] = {}
    if max_iterations is not None:
        extra["max_iterations"] = max_iterations
    return (
        WorkflowBuilder(
            name=name, start_executor=gather, checkpoint_storage=storage, **extra
        )
        .add_edge(gather, draft)
        .add_edge(draft, ask)
        .add_edge(ask, file)
        .build()
    )
