"""The claim-brief workload's four steps through the second host (S037, W1a):
the pause, the two decisions, a decision that is not there, a step that runs
twice, and the failures of the two reads. PostgreSQL, the real tool servers and
a stub gateway; every leg runs in a thread of its own with a time limit.
"""

import dataclasses

import pytest
from claimbriefsupport import (
    FailsOnceAfter,
    Refuses,
    ScriptedTools,
    approval_reasons,
    checkpoint_rows,
    claim_input,
    claim_row,
    notes,
    policy_answer,
    record_decision,
    reply_with,
    resume_leg,
    start_leg,
)
from hostsupport import POLICY, BriefWorld
from servicesupport import GATEWAY_REPLY

from meridian.runtime.agent_framework_host import (
    WorkflowDefinition,
    check_factory,
)
from meridian.runtime.failures import GraphFailure, failure_reason
from meridian.runtime.runs import RunOutcome
from meridian.runtime.tool_client import ToolRefused, ToolUnavailable
from meridian.workloads.claim_brief.workflow import (
    APPROVAL_REASON,
    APPROVAL_STEP,
    FILED_NOTE,
    NOTE_STEP,
    STATE_TYPES,
    build,
)
from meridian.workloads.claims_triage.briefs import BriefOutput

THE_BRIEF = GATEWAY_REPLY["output"]["text"]
EMPTY_HISTORY = {"entries": [], "truncated": False}


# ── the factory ─────────────────────────────────────────────────────────────
def test_the_host_accepts_the_factory_and_its_state_types_are_dataclasses() -> None:
    definition = check_factory(build)

    assert isinstance(definition, WorkflowDefinition)
    assert definition.state_types == STATE_TYPES
    assert STATE_TYPES
    assert all(dataclasses.is_dataclass(cls) for cls in STATE_TYPES)
    # Four steps in a line: gather, draft, ask, file.
    assert len(definition.edges) == 3
    assert definition.edges[0][0] is definition.start


# ── a start ─────────────────────────────────────────────────────────────────
def test_a_start_drafts_one_brief_asks_once_and_pauses_with_the_brief_as_output(
    world: BriefWorld,
) -> None:
    outcome = start_leg(world)

    assert outcome == RunOutcome("AwaitingApproval", {"brief": THE_BRIEF})
    assert BriefOutput.model_validate(outcome.output).brief == THE_BRIEF
    assert len(world.gateway.seen) == 1
    assert approval_reasons(world) == [APPROVAL_REASON]
    assert notes(world) == []
    assert checkpoint_rows(world) > 0


def test_a_start_calls_the_two_reads_the_request_and_nothing_else_in_order(
    world: BriefWorld,
) -> None:
    tools = ScriptedTools(policy_answer(), EMPTY_HISTORY)

    start_leg(world, tools)

    assert tools.calls == [
        ("policy_lookup", {"policy_number": POLICY}, None),
        ("claim_history", {"policy_number": POLICY}, None),
        (
            "request_approval",
            {"claim_id": "CLM-0001", "reason": APPROVAL_REASON},
            APPROVAL_STEP,
        ),
    ]


def test_the_two_writes_have_fixed_step_words_and_the_reads_have_none() -> None:
    # The tool client refuses a step word for a tool with no idempotency key, so
    # the three reads (and the outcome) take none; the writes' words are fixed.
    assert (APPROVAL_STEP, NOTE_STEP) == ("request-approval", "file-note")


# ── a resume after a decision ───────────────────────────────────────────────
def test_a_resume_after_approve_files_one_note_of_fixed_text_and_completes(
    world: BriefWorld,
) -> None:
    start_leg(world)
    record_decision(world, "approve")

    outcome = resume_leg(world)

    assert outcome == RunOutcome("Completed", {"brief": THE_BRIEF, "filed": True})
    assert BriefOutput.model_validate(outcome.output).filed is True
    assert notes(world) == [FILED_NOTE]
    # The brief was drafted once, in the first leg.
    assert len(world.gateway.seen) == 1


def test_a_resume_after_reject_writes_nothing_and_says_it_was_not_filed(
    world: BriefWorld,
) -> None:
    start_leg(world)
    record_decision(world, "reject")

    outcome = resume_leg(world)

    assert outcome == RunOutcome("Completed", {"brief": THE_BRIEF, "filed": False})
    assert BriefOutput.model_validate(outcome.output).filed is False
    assert notes(world) == []


def test_a_resume_reads_the_decision_through_the_tool_with_the_files_step_words(
    world: BriefWorld,
) -> None:
    start_leg(world)
    tools = ScriptedTools(policy_answer(), EMPTY_HISTORY)

    resume_leg(world, tools)

    assert tools.calls == [
        ("approval_outcome", {"claim_id": "CLM-0001"}, None),
        (
            "add_claim_note",
            {"claim_id": "CLM-0001", "note": FILED_NOTE},
            NOTE_STEP,
        ),
    ]


def test_a_resume_with_no_decision_fails_and_can_be_resumed_once_it_exists(
    world: BriefWorld,
) -> None:
    start_leg(world)

    with pytest.raises(GraphFailure) as raised:
        resume_leg(world)
    after_failure = (notes(world), checkpoint_rows(world) > 0)
    record_decision(world, "approve")
    outcome = resume_leg(world)

    assert raised.value.code == "decision-not-recorded"
    assert failure_reason(raised.value) == "decision-not-recorded"
    assert after_failure == ([], True)
    assert outcome == RunOutcome("Completed", {"brief": THE_BRIEF, "filed": True})
    assert notes(world) == [FILED_NOTE]


@pytest.mark.parametrize("word", ["request_documents", "send_back", "withdrawn"])
def test_a_recorded_word_that_is_not_approve_or_reject_is_no_decision(
    world: BriefWorld, word: str
) -> None:
    start_leg(world)
    record_decision(world, word)

    with pytest.raises(GraphFailure) as raised:
        resume_leg(world)

    assert raised.value.code == "decision-not-recorded"
    assert notes(world) == []


def test_the_resume_s_value_decides_nothing_only_the_recorded_row_does(
    world: BriefWorld,
) -> None:
    start_leg(world)
    record_decision(world, "reject")

    outcome = resume_leg(world, value={"decision": "approve", "outcome": "approve"})

    assert outcome.output == {"brief": THE_BRIEF, "filed": False}
    assert notes(world) == []


# ── a step that runs twice ──────────────────────────────────────────────────
def test_the_file_step_run_twice_writes_one_note_because_its_key_does_not_change(
    world: BriefWorld,
) -> None:
    start_leg(world)
    record_decision(world, "approve")
    failing = FailsOnceAfter(world.tools(), "add_claim_note", RuntimeError("after"))

    with pytest.raises(RuntimeError, match="after"):
        resume_leg(world, failing)
    after_failure = notes(world)
    outcome = resume_leg(world, failing)

    assert after_failure == [FILED_NOTE]
    assert [result.replayed for result in failing.results] == [False, True]
    assert notes(world) == [FILED_NOTE]
    assert outcome == RunOutcome("Completed", {"brief": THE_BRIEF, "filed": True})


# ── the draft's replies ─────────────────────────────────────────────────────
def test_a_reply_of_exactly_the_longest_length_is_the_brief(
    world: BriefWorld,
) -> None:
    longest = "b" * 4000
    reply_with(world, longest)

    outcome = start_leg(world)

    assert outcome == RunOutcome("AwaitingApproval", {"brief": longest})
    assert BriefOutput.model_validate(outcome.output).brief == longest
    assert approval_reasons(world) == [APPROVAL_REASON]


@pytest.mark.parametrize(
    ("text", "finish_reason", "word"),
    [
        ("", "stop", "empty-brief"),
        (" \n\t ", "stop", "empty-brief"),
        ("c" * 4001, "stop", "brief-too-long"),
        ("a brief\x00with a NUL", "stop", "brief-unfit"),
        ("a brief that was cut short", "length", "brief-truncated"),
    ],
    ids=["empty", "blank", "too-long", "NUL", "truncated"],
)
def test_a_reply_that_cannot_be_the_brief_fails_with_its_word_and_asks_nobody(
    world: BriefWorld, text: str, finish_reason: str, word: str
) -> None:
    reply_with(world, text, finish_reason)

    with pytest.raises(GraphFailure) as raised:
        start_leg(world)

    assert raised.value.code == word
    assert failure_reason(raised.value) == word
    assert approval_reasons(world) == []
    assert notes(world) == []


def test_a_reply_is_kept_as_it_came_not_cut_stripped_or_padded(
    world: BriefWorld,
) -> None:
    reply_with(world, "  A brief with spaces.\n")

    outcome = start_leg(world)

    assert outcome.output == {"brief": "  A brief with spaces.\n"}


# ── the two reads fail the leg before any model call ────────────────────────
@pytest.mark.parametrize("tool", ["policy_lookup", "claim_history"])
@pytest.mark.parametrize(
    ("error", "word"),
    [
        (ToolRefused("tool", "unknown"), "tool-refused"),
        (ToolUnavailable("tool"), "tool-unavailable"),
    ],
    ids=["refused", "unavailable"],
)
def test_a_read_that_is_refused_or_unavailable_fails_the_leg_before_the_model(
    world: BriefWorld, tool: str, error: Exception, word: str
) -> None:
    tools = Refuses(world.tools(), tool, error)

    with pytest.raises(type(error)) as raised:
        start_leg(world, tools)

    assert failure_reason(raised.value) == word
    assert world.gateway.seen == []
    assert approval_reasons(world) == []
    assert tools.called[-1] == tool


def test_a_run_the_servers_no_longer_know_as_running_fails_at_the_first_read(
    world: BriefWorld,
) -> None:
    world.set_run_status("Completed")

    with pytest.raises(Exception) as raised:
        start_leg(world)

    assert failure_reason(raised.value) == "tool-refused"
    assert world.gateway.seen == []


@pytest.mark.parametrize(
    "answer",
    [
        {"found": "yes"},
        {"found": True},
        {"found": True, "policy": {"status": "active"}},
        {"found": True, "policy": policy_answer(status="expired")["policy"]},
        {"found": True, "policy": policy_answer(deductible=-1)["policy"]},
        {"found": True, "policy": policy_answer(limit=True)["policy"]},
    ],
)
def test_a_policy_answer_that_does_not_fit_fails_with_a_word_and_repeats_nothing(
    world: BriefWorld, answer: dict
) -> None:
    tools = ScriptedTools(answer, EMPTY_HISTORY)

    with pytest.raises(GraphFailure) as raised:
        start_leg(world, tools)

    assert raised.value.code == "policy-record-unfit"
    assert world.gateway.seen == []


@pytest.mark.parametrize(
    "answer",
    [
        {"entries": [], "truncated": "no"},
        {"entries": [{"peril": "fire"}], "truncated": False},
        {"entries": [{"peril": "fire", "paid_amount": -1}], "truncated": False},
        {"entries": "none", "truncated": False},
        {"truncated": False},
    ],
)
def test_a_history_answer_that_does_not_fit_fails_with_a_word_before_the_model(
    world: BriefWorld, answer: dict
) -> None:
    tools = ScriptedTools(policy_answer(), answer)

    with pytest.raises(GraphFailure) as raised:
        start_leg(world, tools)

    assert raised.value.code == "history-unfit"
    assert world.gateway.seen == []


@pytest.mark.parametrize(
    "run_input",
    [
        {},
        {"claim": "CLM-0001"},
        claim_input(peril="earthquake"),
        claim_input(claimed_amount=0),
        claim_input(policy_number="not-a-policy"),
        {"claim": {"claim_id": "CLM-0001"}},
    ],
)
def test_an_input_that_is_not_a_claim_fails_with_a_word_before_any_call(
    world: BriefWorld, run_input: dict
) -> None:
    tools = ScriptedTools(policy_answer(), EMPTY_HISTORY)

    with pytest.raises(GraphFailure) as raised:
        start_leg(world, tools, run_input)

    assert raised.value.code == "claim-unfit"
    assert tools.calls == []
    assert world.gateway.seen == []


# ── what the workload never does ────────────────────────────────────────────
def test_a_whole_brief_leaves_the_claim_s_row_as_it_was(world: BriefWorld) -> None:
    before = claim_row(world)
    start_leg(world)
    record_decision(world, "approve")
    resume_leg(world)

    assert claim_row(world) == before
    assert notes(world) == [FILED_NOTE]


def test_the_fixed_texts_name_no_claim_no_person_and_no_model_text() -> None:
    assert FILED_NOTE.strip() == FILED_NOTE
    assert 0 < len(FILED_NOTE) <= 2000
    assert 0 < len(APPROVAL_REASON) <= 1000
    for text in (FILED_NOTE, APPROVAL_REASON):
        assert "CLM-" not in text
        assert "POL-" not in text
