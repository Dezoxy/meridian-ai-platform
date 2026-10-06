"""The claim brief as a Microsoft Agent Framework workflow (S037).

Four steps in a line, run by ``meridian.runtime.agent_framework_host``:

1. **gather** reads the policy and the claim's history (``policy_lookup``, then
   ``claim_history``) and keeps the facts the model may see (``facts.py``).
2. **draft** makes ONE model call for a short brief in plain text and checks the
   reply (``prompt.py``): empty, longer than 4,000 characters, holding a NUL or
   cut at the token cap fails the run with a word of its own, and the reply is
   never cut, stripped or padded.
3. **ask** yields the brief as the run's output (the Claims Triage App reads it
   from the paused leg), asks for an approval (``request_approval``, fixed
   reason) and pauses until an adjuster has decided.
4. **file** reads the recorded decision through ``approval_outcome`` and never
   from the resume: ``approve`` writes one note of fixed text (``add_claim_note``)
   and yields the brief with ``filed: true``; ``reject`` writes nothing and
   yields ``filed: false``; no decision, or any other word, fails the run with
   ``decision-not-recorded`` and leaves it paused-resumable.

It decides nothing about the claim and moves no claim's state: the two writes
are rows keyed by this run. The host re-opens a failed resume at the step that
failed, so ``file`` may run twice; its two writes therefore go through a FIXED
``step`` word (the tool client's idempotency key is made from the run, the tool
and that word), and a second run of the step writes no second row.

The workload is handed the host's two asynchronous clients and nothing else:
every model call goes through the gateway and every tool call through the tool
client. The steps keep the brief in the workflow's state, so it is in the run's
checkpoint rows until the run ends; it is in no log line, span, exception or
tool argument.
"""

from dataclasses import dataclass
from typing import Any, Literal, Never

from agent_framework import Executor, WorkflowContext, handler, response_handler
from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.runtime.agent_framework_host import (
    ResumeMarker,
    WorkflowDefinition,
)
from meridian.runtime.failures import GraphFailure
from meridian.runtime.hosts import AsyncModelClient, AsyncToolClient
from meridian.runtime.model_client import ChatResult
from meridian.workloads.claim_brief.facts import (
    document,
    read_claim,
    read_history,
    read_policy,
)
from meridian.workloads.claim_brief.prompt import (
    BRIEF_DATA_CLASS,
    BRIEF_OUTPUT_TOKENS,
    MAX_BRIEF_CHARS,
    build_messages,
)

# The step words of the two writes. They are fixed: the idempotency key of a
# write is made from the run, the tool and the word, so a step that runs twice
# repeats it. The reads take no word: the tool client refuses one for a tool
# with no idempotency key.
APPROVAL_STEP = "request-approval"
NOTE_STEP = "file-note"
# Fixed texts: a note or a request never holds what the model wrote.
APPROVAL_REASON = "A claim brief was drafted and waits for an adjuster's decision."
FILED_NOTE = "An adjuster approved the claim brief and it was filed."

EMPTY_BRIEF = "empty-brief"
BRIEF_TOO_LONG = "brief-too-long"
BRIEF_UNFIT = "brief-unfit"
BRIEF_TRUNCATED = "brief-truncated"
OUTCOME_UNFIT = "approval-outcome-unfit"
DECISION_NOT_RECORDED = "decision-not-recorded"


@dataclass
class Gathered:
    """What gather passes on: the claim's ID, to address the tools, and the
    document the model is sent (numbers, dates, booleans and closed words)."""

    claim_id: str
    facts: dict[str, Any]


@dataclass
class Drafted:
    """The brief, and the claim it is about. Also the pause's request: the
    answer's handler hands it on to the file step."""

    claim_id: str
    brief: str


# Every dataclass a message or a request carries: a checkpoint is JSON and
# refuses a type that is not listed (the host adds its own response marker).
STATE_TYPES = (Gathered, Drafted)


class ApprovalOutcome(BaseModel):
    """The answer of ``approval_outcome``: the word recorded for this run, none
    while there is none. Strict, so no type is coerced into a word."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: (
        Literal["approve", "reject", "request_documents", "send_back", "withdrawn"]
        | None
    ) = None


def _is_text(text: str) -> bool:
    """Whether ``text`` can be written as UTF-8: a lone surrogate cannot."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def brief_of(reply: ChatResult) -> str:
    """The reply as the brief, unchanged, or a ``GraphFailure`` that says why it
    cannot be one. Nothing of the reply is put in the failure."""
    text = reply.text
    if not text.strip():
        raise GraphFailure(EMPTY_BRIEF)
    if len(text) > MAX_BRIEF_CHARS:
        raise GraphFailure(BRIEF_TOO_LONG)
    if "\x00" in text or not _is_text(text):
        raise GraphFailure(BRIEF_UNFIT)
    if reply.finish_reason != "stop":
        raise GraphFailure(BRIEF_TRUNCATED)
    return text


class Gather(Executor):
    def __init__(self, tools: AsyncToolClient) -> None:
        super().__init__(id="gather")
        self._tools = tools

    @handler
    async def gather(self, run_input: dict, ctx: WorkflowContext[Gathered]) -> None:
        claim = read_claim(run_input)
        number = {"policy_number": claim.policy_number}
        found = await self._tools.call("policy_lookup", number)
        entries = await self._tools.call("claim_history", number)
        facts = document(
            claim,
            read_policy(found.data),
            read_history(entries.data, claim.peril),
        )
        await ctx.send_message(Gathered(claim.claim_id, facts))


class Draft(Executor):
    def __init__(self, model: AsyncModelClient) -> None:
        super().__init__(id="draft")
        self._model = model

    @handler
    async def draft(self, gathered: Gathered, ctx: WorkflowContext[Drafted]) -> None:
        reply = await self._model.chat(
            build_messages(gathered.facts),
            max_output_tokens=BRIEF_OUTPUT_TOKENS,
            data_class=BRIEF_DATA_CLASS,
        )
        await ctx.send_message(Drafted(gathered.claim_id, brief_of(reply)))


class Ask(Executor):
    def __init__(self, tools: AsyncToolClient) -> None:
        super().__init__(id="ask")
        self._tools = tools

    @handler
    async def ask(self, drafted: Drafted, ctx: WorkflowContext[Never, dict]) -> None:
        await ctx.yield_output({"brief": drafted.brief})
        await self._tools.call(
            "request_approval",
            {"claim_id": drafted.claim_id, "reason": APPROVAL_REASON},
            step=APPROVAL_STEP,
        )
        await ctx.request_info(drafted, ResumeMarker)

    @response_handler
    async def answered(
        self,
        original_request: Drafted,
        response: ResumeMarker,
        ctx: WorkflowContext[Drafted],
    ) -> None:
        # The answer carries nothing: the decision is read from its record.
        await ctx.send_message(original_request)


class File(Executor):
    def __init__(self, tools: AsyncToolClient) -> None:
        super().__init__(id="file")
        self._tools = tools

    async def _decision(self, claim_id: str) -> str:
        """The recorded decision: ``approve`` or ``reject``, else the run fails.
        The answer is read from the Claims API's record and nowhere else."""
        answer = await self._tools.call("approval_outcome", {"claim_id": claim_id})
        try:
            outcome = ApprovalOutcome.model_validate(answer.data).outcome
        except ValidationError:
            raise GraphFailure(OUTCOME_UNFIT) from None
        if outcome not in ("approve", "reject"):
            raise GraphFailure(DECISION_NOT_RECORDED)
        return outcome

    @handler
    async def file(self, drafted: Drafted, ctx: WorkflowContext[Never, dict]) -> None:
        filed = await self._decision(drafted.claim_id) == "approve"
        if filed:
            await self._tools.call(
                "add_claim_note",
                {"claim_id": drafted.claim_id, "note": FILED_NOTE},
                step=NOTE_STEP,
            )
        await ctx.yield_output({"brief": drafted.brief, "filed": filed})


def build(model: AsyncModelClient, tools: AsyncToolClient) -> WorkflowDefinition:
    """The workload's entry point: the four steps over the host's two clients.
    It calls nothing; the host builds it again for every leg."""
    gather, draft = Gather(tools), Draft(model)
    ask, file = Ask(tools), File(tools)
    return WorkflowDefinition(
        start=gather,
        edges=((gather, draft), (draft, ask), (ask, file)),
        state_types=STATE_TYPES,
    )
