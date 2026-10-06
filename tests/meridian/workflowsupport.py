"""A small workflow in the second agent framework, for the tests of its
checkpoint store (S037, R3b).

It has the shape the claim-brief workload will have, without tools or a model:
``draft`` (the start) -> ``ask`` (a request for a person, which pauses the run)
-> ``file`` (reads the answer's effect and yields the output). It keeps two
dataclasses of its own in its messages and requests (``Drafted``, ``Filing``)
and the response marker (``Marker``), which a store must be told about.

``Dials`` counts how often each step ran and makes a named step fail, so a test
can see which steps run again after a failure. A leg is ``asyncio.run`` of a
coroutine, as the runtime's leg will be: a new event loop each time.
"""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Never

from agent_framework import (
    CheckpointStorage,
    Executor,
    Workflow,
    WorkflowBuilder,
    WorkflowCheckpoint,
    WorkflowContext,
    WorkflowRunResult,
    handler,
    response_handler,
)

WORKFLOW_NAME = "claim-brief"
CLAIM = "CLM-0001"
BRIEF = "a synthetic brief"
LEG_SECONDS = 60


@dataclass
class Drafted:
    claim_id: str
    brief: str


@dataclass
class Filing:
    claim_id: str
    brief: str


@dataclass
class Marker:
    """The response type of the request: it carries nothing."""


CHECKPOINT_TYPES = [Drafted, Filing, Marker]


class StepFailure(Exception):
    """Raised by a step the test told to fail."""


@dataclass
class Dials:
    counts: Counter[str] = field(default_factory=Counter)
    fail_in: set[str] = field(default_factory=set)

    def enter(self, step: str) -> None:
        self.counts[step] += 1
        if step in self.fail_in:
            raise StepFailure(step)


class Draft(Executor):
    def __init__(self, dials: Dials) -> None:
        super().__init__(id="draft")
        self._dials = dials

    @handler
    async def run(self, claim_id: str, ctx: WorkflowContext[Drafted]) -> None:
        self._dials.enter("draft")
        await ctx.send_message(Drafted(claim_id, BRIEF))


class Ask(Executor):
    def __init__(self, dials: Dials) -> None:
        super().__init__(id="ask")
        self._dials = dials

    @handler
    async def ask(self, drafted: Drafted, ctx: WorkflowContext) -> None:
        self._dials.enter("ask")
        await ctx.request_info(drafted, Marker)

    @response_handler
    async def answered(
        self,
        original_request: Drafted,
        response: Marker,
        ctx: WorkflowContext[Filing],
    ) -> None:
        self._dials.enter("answered")
        await ctx.send_message(
            Filing(original_request.claim_id, original_request.brief)
        )


class File(Executor):
    def __init__(self, dials: Dials) -> None:
        super().__init__(id="file")
        self._dials = dials

    @handler
    async def run(self, filing: Filing, ctx: WorkflowContext[Never, dict]) -> None:
        self._dials.enter("file")
        await ctx.yield_output({"brief": filing.brief, "filed": True})


def build_workflow(dials: Dials, store: CheckpointStorage) -> Workflow:
    """A workflow built from nothing for one leg, as the host will build it."""
    draft, ask, file = Draft(dials), Ask(dials), File(dials)
    return (
        WorkflowBuilder(
            name=WORKFLOW_NAME, start_executor=draft, checkpoint_storage=store
        )
        .add_edge(draft, ask)
        .add_edge(ask, file)
        .build()
    )


def leg[Result](work: Callable[[], Awaitable[Result]]) -> Result:
    """One leg: a new event loop, which gives up (and fails the test) when the
    work does not finish."""

    async def bounded() -> Result:
        return await asyncio.wait_for(work(), timeout=LEG_SECONDS)

    return asyncio.run(bounded())


def start(dials: Dials, store: CheckpointStorage) -> WorkflowRunResult:
    workflow = build_workflow(dials, store)
    return leg(lambda: workflow.run(CLAIM))


async def latest_of(store: CheckpointStorage) -> WorkflowCheckpoint:
    found = await store.get_latest(workflow_name=WORKFLOW_NAME)
    assert found is not None
    return found


def answer_latest(dials: Dials, store: CheckpointStorage) -> WorkflowRunResult:
    """Resume as the host will: a new workflow, the latest checkpoint's pending
    requests answered with a marker of the host's own."""
    workflow = build_workflow(dials, store)

    async def go() -> WorkflowRunResult:
        latest = await latest_of(store)
        return await workflow.run(
            responses={rid: Marker() for rid in latest.pending_request_info_events},
            checkpoint_id=latest.checkpoint_id,
        )

    return leg(go)


def restore_latest(dials: Dials, store: CheckpointStorage) -> WorkflowRunResult:
    """Resume a run whose answer is in flight: restore the latest checkpoint
    with no response."""
    workflow = build_workflow(dials, store)

    async def go() -> WorkflowRunResult:
        latest = await latest_of(store)
        return await workflow.run(checkpoint_id=latest.checkpoint_id)

    return leg(go)


class MemoryStore:
    """The six members of ``CheckpointStorage`` over a list, with no copy and no
    codec: what the framework saves is what a test reads back."""

    def __init__(self) -> None:
        self.saved: list[WorkflowCheckpoint] = []

    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        self.saved.append(checkpoint)
        return checkpoint.checkpoint_id

    async def load(self, checkpoint_id: str) -> WorkflowCheckpoint:
        return next(c for c in self.saved if c.checkpoint_id == checkpoint_id)

    async def list_checkpoints(self, *, workflow_name: str) -> list[WorkflowCheckpoint]:
        return [c for c in self.saved if c.workflow_name == workflow_name]

    async def list_checkpoint_ids(self, *, workflow_name: str) -> list[str]:
        return [c.checkpoint_id for c in self.saved if c.workflow_name == workflow_name]

    async def get_latest(self, *, workflow_name: str) -> WorkflowCheckpoint | None:
        found = [c for c in self.saved if c.workflow_name == workflow_name]
        return found[-1] if found else None

    async def delete(self, checkpoint_id: str) -> bool:
        before = len(self.saved)
        self.saved = [c for c in self.saved if c.checkpoint_id != checkpoint_id]
        return len(self.saved) < before


def describe(result: WorkflowRunResult) -> dict[str, Any]:
    """What a leg's result says: paused or not, and the outputs."""
    return {
        "paused": bool(result.get_request_info_events()),
        "outputs": result.get_outputs(),
    }
