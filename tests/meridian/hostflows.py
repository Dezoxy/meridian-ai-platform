"""Workflows for the tests of the second host (S037, R4a), each a factory of the
shape a workload publishes: it is called with the two asynchronous faces and
returns a ``WorkflowDefinition``.

``brief_factory`` has the shape the claim-brief workload will have: gather (two
tool reads), draft (one model call), ask (a tool write, an output, then the
pause) and file (the recorded decision, then a note written through a key that
a second run of the step repeats). ``chain_factory`` is plain relays around a
pause, for the step bound; ``two_pauses_factory`` asks twice at once.

``Dials`` counts how often each step ran and makes a named step fail with a
message the test chooses, so a test can see which steps run again.
"""

import threading
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Never

from agent_framework import Executor, WorkflowContext, handler, response_handler
from hostsupport import APPROVAL_STEP, CLAIM, NOTE_STEP, POLICY
from workflowsupport import Dials as CountingDials
from workflowsupport import StepFailure

from meridian.runtime.agent_framework_host import (
    ResumeMarker,
    WorkflowDefinition,
    WorkflowFactory,
)
from meridian.runtime.hosts import AsyncModelClient, AsyncToolClient


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
class Filing:
    claim_id: str
    brief: str


STATE_TYPES = (Gathered, Drafted, Filing)
BARRIER_SECONDS = 30


@dataclass
class Dials(CountingDials):
    """The counting dials, with the text of the failure a step raises, a switch
    that makes the last step of the brief yield nothing, and a barrier the first
    step waits at, so that two legs can be shown to run at the same time."""

    message: str = ""
    quiet: bool = False
    meet: threading.Barrier | None = None

    def enter(self, step: str) -> None:
        self.counts[step] += 1
        if step == "gather" and self.meet is not None:
            self.meet.wait(timeout=BARRIER_SECONDS)
        if step in self.fail_in:
            raise StepFailure(self.message or step)


class Gather(Executor):
    def __init__(self, dials: Dials, tools: AsyncToolClient) -> None:
        super().__init__(id="gather")
        self._dials, self._tools = dials, tools

    @handler
    async def run(self, run_input: dict, ctx: WorkflowContext[Gathered]) -> None:
        self._dials.enter("gather")
        lookup = {"policy_number": POLICY}
        policy = await self._tools.call("policy_lookup", lookup)
        history = await self._tools.call("claim_history", lookup)
        await ctx.send_message(
            Gathered(
                run_input["claim_id"],
                policy.data.get("found") is True,
                len(history.data.get("entries", [])),
            )
        )


class Draft(Executor):
    def __init__(self, dials: Dials, model: AsyncModelClient) -> None:
        super().__init__(id="draft")
        self._dials, self._model = dials, model

    @handler
    async def run(self, gathered: Gathered, ctx: WorkflowContext[Drafted]) -> None:
        self._dials.enter("draft")
        reply = await self._model.chat(
            [{"role": "user", "content": f"Brief for {gathered.claim_id}"}]
        )
        await ctx.send_message(Drafted(gathered.claim_id, reply.text))


class Ask(Executor):
    def __init__(self, dials: Dials, tools: AsyncToolClient) -> None:
        super().__init__(id="ask")
        self._dials, self._tools = dials, tools

    @handler
    async def ask(self, drafted: Drafted, ctx: WorkflowContext[Never, dict]) -> None:
        self._dials.enter("ask")
        await self._tools.call(
            "request_approval",
            {"claim_id": drafted.claim_id, "reason": "A brief waits."},
            step=APPROVAL_STEP,
        )
        await ctx.yield_output({"brief": drafted.brief})
        await ctx.request_info(drafted, ResumeMarker)

    @response_handler
    async def answered(
        self,
        original_request: Drafted,
        response: ResumeMarker,
        ctx: WorkflowContext[Filing],
    ) -> None:
        self._dials.enter("answered")
        await ctx.send_message(
            Filing(original_request.claim_id, original_request.brief)
        )


class File(Executor):
    def __init__(
        self, dials: Dials, tools: AsyncToolClient, replays: list[bool]
    ) -> None:
        super().__init__(id="file")
        self._dials, self._tools, self._replays = dials, tools, replays

    @handler
    async def run(self, filing: Filing, ctx: WorkflowContext[Never, dict]) -> None:
        self._dials.enter("file")
        await self._tools.call("approval_outcome", {"claim_id": filing.claim_id})
        note = await self._tools.call(
            "add_claim_note",
            {"claim_id": filing.claim_id, "note": "The brief was filed."},
            step=NOTE_STEP,
        )
        self._replays.append(note.replayed)
        # After the write: a failure here is a step that ran twice.
        self._dials.enter("file_done")
        if not self._dials.quiet:
            await ctx.yield_output({"brief": filing.brief, "filed": True})


def brief_factory(
    dials: Dials,
    replays: list[bool] | None = None,
    state_types: tuple[type, ...] = STATE_TYPES,
) -> WorkflowFactory:
    kept = [] if replays is None else replays

    def factory(model: AsyncModelClient, tools: AsyncToolClient) -> WorkflowDefinition:
        gather, draft = Gather(dials, tools), Draft(dials, model)
        ask, file = Ask(dials, tools), File(dials, tools, kept)
        return WorkflowDefinition(
            start=gather,
            edges=((gather, draft), (draft, ask), (ask, file)),
            state_types=state_types,
        )

    return factory


# ── relays around a pause, for the step bound ───────────────────────────────
class Relay(Executor):
    def __init__(self, name: str, dials: Dials) -> None:
        super().__init__(id=name)
        self._dials = dials

    @handler
    async def run(self, message: dict, ctx: WorkflowContext[dict]) -> None:
        self._dials.enter(self.id)
        await ctx.send_message({"n": message.get("n", 0) + 1})


class Pause(Executor):
    def __init__(self, dials: Dials) -> None:
        super().__init__(id="pause")
        self._dials = dials

    @handler
    async def ask(self, message: dict, ctx: WorkflowContext) -> None:
        self._dials.enter("pause")
        await ctx.request_info(Drafted(CLAIM, "waiting"), ResumeMarker)

    @response_handler
    async def answered(
        self,
        original_request: Drafted,
        response: ResumeMarker,
        ctx: WorkflowContext[dict],
    ) -> None:
        self._dials.enter("answered")
        await ctx.send_message({"n": 0})


class Finish(Executor):
    def __init__(self, dials: Dials) -> None:
        super().__init__(id="finish")
        self._dials = dials

    @handler
    async def run(self, message: dict, ctx: WorkflowContext[Never, dict]) -> None:
        self._dials.enter("finish")
        await ctx.yield_output({"done": True})


def chain_factory(
    dials: Dials, *, before: int, after: int, loop_after: bool = False
) -> WorkflowFactory:
    """``before`` relays, the pause, then ``after`` relays and the end; with
    ``loop_after`` the pause is followed by one relay that feeds itself and never
    ends."""

    def factory(model: AsyncModelClient, tools: AsyncToolClient) -> WorkflowDefinition:
        first: list[Executor] = [Relay(f"before-{i}", dials) for i in range(before)]
        pause = Pause(dials)
        if loop_after:
            tail: list[Executor] = [Relay("loop", dials)]
        else:
            tail = [Relay(f"after-{i}", dials) for i in range(after)]
            tail.append(Finish(dials))
        line = [*first, pause, *tail]
        edges = tuple(pairwise(line))
        if loop_after:
            edges = (*edges, (tail[0], tail[0]))
        return WorkflowDefinition(start=line[0], edges=edges, state_types=(Drafted,))

    return factory


def loop_factory(dials: Dials) -> WorkflowFactory:
    """One relay that feeds itself: a workflow that never converges."""

    def factory(model: AsyncModelClient, tools: AsyncToolClient) -> WorkflowDefinition:
        node = Relay("loop", dials)
        return WorkflowDefinition(start=node, edges=((node, node),))

    return factory


class Yielder(Executor):
    def __init__(self, values: tuple[Any, ...]) -> None:
        super().__init__(id="yielder")
        self._values = values

    @handler
    async def run(self, message: dict, ctx: WorkflowContext[Never, Any]) -> None:
        for value in self._values:
            await ctx.yield_output(value)


def yield_factory(*values: Any) -> WorkflowFactory:
    """One step that yields each of ``values`` in turn and ends."""

    def factory(model: AsyncModelClient, tools: AsyncToolClient) -> WorkflowDefinition:
        return WorkflowDefinition(start=Yielder(values), edges=())

    return factory


class Raiser(Executor):
    def __init__(self, error: Exception) -> None:
        super().__init__(id="raiser")
        self._error = error

    @handler
    async def run(self, message: dict, ctx: WorkflowContext) -> None:
        raise self._error


def raising_factory(error: Exception) -> WorkflowFactory:
    """One step that raises ``error``."""

    def factory(model: AsyncModelClient, tools: AsyncToolClient) -> WorkflowDefinition:
        return WorkflowDefinition(start=Raiser(error), edges=())

    return factory


class TwoPauses(Executor):
    def __init__(self) -> None:
        super().__init__(id="two-pauses")

    @handler
    async def ask(self, message: dict, ctx: WorkflowContext) -> None:
        await ctx.request_info(Drafted(CLAIM, "first"), ResumeMarker)
        await ctx.request_info(Drafted(CLAIM, "second"), ResumeMarker)

    @response_handler
    async def answered(
        self,
        original_request: Drafted,
        response: ResumeMarker,
        ctx: WorkflowContext,
    ) -> None:
        return None


def two_pauses_factory() -> WorkflowFactory:
    def factory(model: AsyncModelClient, tools: AsyncToolClient) -> WorkflowDefinition:
        return WorkflowDefinition(start=TwoPauses(), edges=(), state_types=(Drafted,))

    return factory
