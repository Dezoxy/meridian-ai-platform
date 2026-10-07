"""The pause of the triage graph (S015, S048): the approval request, the decision
read from the record on resume (never from the resume value), the note, and the
answers that do not fit.
"""

import logging
from typing import Any, cast

import pytest
from graphsupport import (
    CANARY,
    NOTE_ID,
    REQUEST_ID,
    STATE_KEYS,
    THREAD,
    StubModel,
    StubTools,
    assert_plain,
    compiled,
    expected_proposal,
    facts,
    golden_model,
    paused,
    resume,
)
from langgraph.checkpoint.memory import MemorySaver
from pydantic import ValidationError
from servicesupport import REGISTRY_DIR

from meridian.platform.registry import load_registry
from meridian.runtime.failures import GraphFailure, failure_reason
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient, ToolRefused
from meridian.workloads.claims_triage.graph import build
from meridian.workloads.claims_triage.models import DECISION_NOTES
from meridian.workloads.claims_triage.proposal import TriageProposal
from meridian.workloads.claims_triage.workers import ApprovalOutcome

# -- the pause ----------------------------------------------------------------

DECISIONS = ("approve", "reject", "request_documents")
# What the Claims API may record for a run: the decisions, and the two words that
# end a run without one (S048).
OUTCOMES = (*DECISIONS, "send_back", "withdrawn")
# A claim per way to reach the adjuster route: a rule, an inactive policy, and
# no policy at all.
ADJUSTER_CLAIMS = [
    pytest.param("CLM-0004", {}, "over_threshold", id="over-threshold"),
    pytest.param("CLM-0002", {}, "policy_inactive", id="policy-inactive"),
    pytest.param(
        "CLM-0001", {"policy_number": "POL-9999"}, "policy_not_found", id="no-policy"
    ),
]


def approval_request(reason: str, claim_id: str = "CLM-0004") -> tuple[Any, ...]:
    return (
        "request_approval",
        {"claim_id": claim_id, "reason": reason},
        "request-approval",
    )


def decision_note(decision: str, claim_id: str = "CLM-0004") -> tuple[Any, ...]:
    return (
        "add_claim_note",
        {"claim_id": claim_id, "note": DECISION_NOTES[decision]},
        "decision-note",
    )


@pytest.mark.parametrize(("claim_id", "changes", "reason"), ADJUSTER_CLAIMS)
def test_a_claim_routed_to_an_adjuster_requests_approval_and_pauses(
    claim_id: str, changes: dict[str, Any], reason: str
) -> None:
    graph, tools = paused(claim_id, **changes)

    snapshot = graph.get_state(THREAD)
    assert snapshot.next == ("await_decision",)
    (pending,) = snapshot.interrupts
    assert pending.value == {"request_id": REQUEST_ID}
    assert tools.writes == [approval_request(reason, claim_id)]
    proposal = TriageProposal.model_validate(snapshot.values["output"])
    assert (proposal.route, proposal.reason) == ("adjuster", reason)
    assert snapshot.values["request_id"] == REQUEST_ID
    assert snapshot.values["decision"] is None


@pytest.mark.parametrize("decision", OUTCOMES)
def test_a_decision_is_noted_with_its_fixed_text_and_the_run_completes(
    decision: str,
) -> None:
    graph, tools = paused()
    output = graph.get_state(THREAD).values["output"]
    tools.recorded = decision

    result = resume(graph, {})

    assert "__interrupt__" not in result
    snapshot = graph.get_state(THREAD)
    assert snapshot.next == ()
    assert snapshot.interrupts == ()
    assert tools.calls[-1] == ("approval_outcome", {"claim_id": "CLM-0004"})
    assert tools.names().count("approval_outcome") == 1
    assert tools.writes == [approval_request("over_threshold"), decision_note(decision)]
    assert snapshot.values["decision"] == decision
    assert snapshot.values["request_id"] == REQUEST_ID
    assert snapshot.values["output"] == output


def test_each_tool_is_called_through_the_worker_that_holds_it() -> None:
    triage = load_registry(REGISTRY_DIR).agent("claims-triage")
    assert triage is not None
    graph, tools = paused()
    tools.recorded = "withdrawn"

    resume(graph, {})

    # The whole path: the two reads, four searches, the request, the outcome
    # and the note, each through its worker, which the registry says holds it.
    assert [worker for worker, _ in tools.workers] == [
        "intake",
        "intake",
        "terms",
        "terms",
        "terms",
        "terms",
        "approvals",
        "approvals",
        "approvals",
    ]
    for worker, tool in tools.workers:
        holder = triage.worker(worker)
        assert holder is not None
        assert tool in holder.tools


def test_the_five_notes_are_different_fixed_texts() -> None:
    assert set(DECISION_NOTES) == set(OUTCOMES)
    assert len(set(DECISION_NOTES.values())) == 5
    assert all(1 <= len(note) <= 2000 for note in DECISION_NOTES.values())


@pytest.mark.parametrize(
    ("word", "note"),
    [
        ("send_back", "An adjuster sent the claim back to triage."),
        ("withdrawn", "The claimant withdrew the claim."),
    ],
)
def test_a_run_ended_without_a_decision_writes_its_fixed_note_and_completes(
    word: str, note: str
) -> None:
    graph, tools = paused()
    tools.recorded = word

    result = resume(graph, {})

    assert "__interrupt__" not in result
    snapshot = graph.get_state(THREAD)
    assert (snapshot.next, snapshot.interrupts) == ((), ())
    assert tools.writes == [
        approval_request("over_threshold"),
        (
            "add_claim_note",
            {"claim_id": "CLM-0004", "note": note},
            "decision-note",
        ),
    ]
    assert snapshot.values["decision"] == word


@pytest.mark.parametrize("word", OUTCOMES)
def test_an_approval_outcome_takes_each_of_the_five_words(word: str) -> None:
    assert ApprovalOutcome.model_validate({"outcome": word}).outcome == word


@pytest.mark.parametrize("word", ["cancelled", "send-back", "Withdrawn", ""])
def test_an_approval_outcome_refuses_a_sixth_word(word: str) -> None:
    with pytest.raises(ValidationError):
        ApprovalOutcome.model_validate({"outcome": word})


def test_a_graph_compiled_anew_resumes_the_run_from_the_checkpoint() -> None:
    saver = MemorySaver()
    paused(saver=saver)
    tools = StubTools(recorded="approve")
    graph = compiled(StubModel(), tools, saver)

    resume(graph, {})

    assert tools.writes == [decision_note("approve")]
    assert tools.names() == ["approval_outcome"]
    assert graph.get_state(THREAD).values["decision"] == "approve"


@pytest.mark.parametrize(
    ("claim_id", "route"),
    [
        ("CLM-0011", "auto_approve"),
        ("CLM-0005", "auto_approve"),
        ("CLM-0003", "request_documents"),
    ],
)
def test_the_other_routes_complete_without_a_write_or_a_pause(
    claim_id: str, route: str
) -> None:
    model = golden_model(claim_id)
    tools = StubTools()
    graph = compiled(model, tools)

    result = graph.invoke({"claim": facts(claim_id)}, THREAD)

    assert "__interrupt__" not in result
    assert result["output"]["route"] == route
    assert (result["request_id"], result["decision"]) == (None, None)
    snapshot = graph.get_state(THREAD)
    assert (snapshot.next, snapshot.interrupts) == ((), ())
    assert tools.writes == []


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({"decision": "reject"}, id="another-word"),
        pytest.param({"decision": "approve"}, id="the-same-word"),
        pytest.param({"decision": "escalate"}, id="unknown-word"),
        pytest.param({"decision": "approve", "note": CANARY}, id="forged-note"),
        pytest.param({}, id="empty-dict"),
        pytest.param("reject", id="bare-string"),
        pytest.param(["reject"], id="list"),
        pytest.param(5, id="number"),
        pytest.param(None, id="null"),
    ],
)
def test_the_run_reads_the_record_and_never_the_resume_value(value: Any) -> None:
    """T-31: the Claims API records the decision, the run only reads it. Whatever
    the resume carries, the note and the state follow the record."""
    graph, tools = paused()
    tools.recorded = "approve"

    resume(graph, value)

    assert tools.writes == [
        approval_request("over_threshold"),
        decision_note("approve"),
    ]
    assert graph.get_state(THREAD).values["decision"] == "approve"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({}, id="no-outcome"),
        pytest.param({"outcome": None}, id="null-outcome"),
    ],
)
def test_a_run_with_no_recorded_decision_fails_before_any_write(
    answer: dict[str, Any],
) -> None:
    tools = StubTools(answers={"approval_outcome": answer})
    graph, _ = paused(tools=tools)

    with pytest.raises(GraphFailure) as raised:
        resume(graph, {"decision": "approve"})

    assert raised.value.code == "decision-not-recorded"
    assert tools.writes == [approval_request("over_threshold")]
    snapshot = graph.get_state(THREAD)
    # What the runtime reads to keep the run AwaitingApproval.
    assert len(snapshot.interrupts) == 1
    assert snapshot.values["decision"] is None


def test_a_run_that_found_no_decision_completes_when_one_is_recorded() -> None:
    graph, tools = paused()
    with pytest.raises(GraphFailure):
        resume(graph, {})
    tools.recorded = "request_documents"

    resume(graph, {})

    assert tools.writes == [
        approval_request("over_threshold"),
        decision_note("request_documents"),
    ]
    assert graph.get_state(THREAD).values["decision"] == "request_documents"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"outcome": "escalate"}, id="unknown-word"),
        pytest.param({"outcome": "APPROVE"}, id="capitals"),
        pytest.param({"outcome": " approve"}, id="padded"),
        pytest.param({"outcome": ["approve"]}, id="list-word"),
        pytest.param({"outcome": 1}, id="number-word"),
        pytest.param({"outcome": "approve", "note": "ok"}, id="extra-field"),
        pytest.param({"Outcome": "approve"}, id="other-key"),
        pytest.param({"decision": "approve"}, id="another-key"),
        pytest.param({"outcome": f"{CANARY}-word", CANARY: CANARY}, id="canary"),
    ],
)
def test_an_outcome_answer_that_does_not_fit_fails_the_run_before_any_write(
    answer: dict[str, Any],
) -> None:
    tools = StubTools(answers={"approval_outcome": answer})
    graph, _ = paused(tools=tools)

    with pytest.raises(GraphFailure) as raised:
        resume(graph, {})

    assert raised.value.code == "approval-outcome-unfit"
    assert CANARY not in str(raised.value)
    assert raised.value.__cause__ is None
    assert tools.writes == [approval_request("over_threshold")]


def test_a_rerun_of_the_approval_request_sends_the_same_payload_and_step() -> None:
    """T-23: the idempotency key is the run, the tool and the step, so a node
    that runs again must send the same payload under the same step and be
    answered with the stored request."""
    tools = StubTools()
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, tools))
    state = {
        "claim": facts("CLM-0004"),
        "output": expected_proposal(reason="over_threshold"),
    }
    node = graph.nodes["request_approval"].runnable

    first, second = node.invoke(state), node.invoke(state)

    # The worker's subgraph answers with its whole state: the key it wrote is
    # the same both times.
    assert first["request_id"] == second["request_id"] == REQUEST_ID
    assert tools.writes == [approval_request("over_threshold")] * 2


def test_a_run_replayed_from_the_checkpoint_before_the_request_sends_it_again() -> None:
    graph, tools = paused()
    (before,) = [
        s for s in graph.get_state_history(THREAD) if s.next == ("request_approval",)
    ]

    graph.invoke(None, before.config)

    assert tools.writes == [approval_request("over_threshold")] * 2
    assert graph.get_state(THREAD).next == ("await_decision",)


def test_a_resume_that_failed_after_its_note_sends_the_same_note_again() -> None:
    """The node that paused runs from its start when the run resumes. The outcome
    worker it runs continues from its own checkpoint (the read of the outcome
    is not made again, see ``test_runtime_subgraphs.py``), so a resume that
    failed after sending the note sends it again: the same one."""
    tools = StubTools(answers={"add_claim_note": {}}, recorded="reject")
    graph, _ = paused(tools=tools)
    with pytest.raises(GraphFailure):
        resume(graph, {})
    del tools.answers["add_claim_note"]

    resume(graph, {})

    assert tools.writes[1:] == [decision_note("reject")] * 2
    assert tools.names().count("approval_outcome") == 1
    assert graph.get_state(THREAD).values["decision"] == "reject"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({}, id="empty"),
        pytest.param({"request_id": REQUEST_ID}, id="no-replayed"),
        pytest.param({"request_id": "not-a-uuid", "replayed": False}, id="not-a-uuid"),
        pytest.param({"request_id": REQUEST_ID, "replayed": "yes"}, id="not-a-bool"),
        pytest.param(
            {"request_id": REQUEST_ID, "replayed": False, "more": 1}, id="extra-field"
        ),
        pytest.param({"request_id": f"{CANARY}-id", "replayed": CANARY}, id="canary"),
    ],
)
def test_an_approval_answer_that_does_not_fit_fails_the_run(
    answer: dict[str, Any],
) -> None:
    tools = StubTools(answers={"request_approval": answer})

    with pytest.raises(GraphFailure) as raised:
        paused(tools=tools)

    assert raised.value.code == "approval-request-unfit"
    assert CANARY not in str(raised.value)
    assert len(tools.writes) == 1


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({}, id="empty"),
        pytest.param({"note_id": NOTE_ID}, id="no-replayed"),
        pytest.param({"note_id": "not-a-uuid", "replayed": False}, id="not-a-uuid"),
        pytest.param({"note_id": NOTE_ID, "replayed": 3}, id="not-a-bool"),
        pytest.param({"request_id": NOTE_ID, "replayed": False}, id="other-key"),
        pytest.param({"note_id": f"{CANARY}-id", "replayed": CANARY}, id="canary"),
    ],
)
def test_a_note_answer_that_does_not_fit_fails_the_run(
    answer: dict[str, Any],
) -> None:
    tools = StubTools(answers={"add_claim_note": answer}, recorded="approve")
    graph, _ = paused(tools=tools)

    with pytest.raises(GraphFailure) as raised:
        resume(graph, {})

    assert raised.value.code == "decision-note-unfit"
    assert CANARY not in str(raised.value)
    assert [w[0] for w in tools.writes] == ["request_approval", "add_claim_note"]


@pytest.mark.parametrize("tool", ["request_approval", "add_claim_note"])
def test_a_write_tool_error_fails_the_run_unchanged(tool: str) -> None:
    error = ToolRefused(tool, "idempotency-conflict")
    tools = StubTools(errors={tool: error}, recorded="approve")

    with pytest.raises(ToolRefused) as raised:
        graph, _ = paused(tools=tools)
        resume(graph, {})

    assert raised.value is error


def test_the_state_after_a_resumed_run_holds_only_plain_data() -> None:
    graph, tools = paused()
    tools.recorded = "request_documents"
    resume(graph, {})

    values = graph.get_state(THREAD).values

    assert set(values) == STATE_KEYS
    assert (values["request_id"], values["decision"]) == (
        REQUEST_ID,
        "request_documents",
    )
    assert_plain(values)


def test_no_log_line_and_no_error_of_the_pause_holds_claim_text_or_a_tool_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    description = f"A tree fell on the car. {CANARY}-claim"
    unfit = {"request_id": f"{CANARY}-id", "replayed": f"{CANARY}-flag"}

    graph, _ = paused(tools=StubTools(recorded="approve"), description=description)
    resume(graph, {})
    with pytest.raises(GraphFailure) as bad_request:
        paused(tools=StubTools(answers={"request_approval": unfit}))
    graph, _ = paused(
        tools=StubTools(answers={"add_claim_note": unfit}, recorded="approve")
    )
    with pytest.raises(GraphFailure) as bad_note:
        resume(graph, {})
    graph, _ = paused(
        tools=StubTools(answers={"approval_outcome": {"outcome": f"{CANARY}-word"}})
    )
    with pytest.raises(GraphFailure) as bad_decision:
        resume(graph, {f"{CANARY}-key": f"{CANARY}-word"})

    assert [failure_reason(e.value) for e in (bad_request, bad_note, bad_decision)] == [
        "approval-request-unfit",
        "decision-note-unfit",
        "approval-outcome-unfit",
    ]
    assert not any(
        CANARY in str(e.value) for e in (bad_request, bad_note, bad_decision)
    )
    assert not any(CANARY in record.getMessage() for record in caplog.records)
