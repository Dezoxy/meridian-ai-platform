"""The claim submission mirrors data/synthetic/claims.json."""

from typing import Any, get_args

import pytest
from pydantic import ValidationError
from servicesupport import claim_with_id, synthetic_claims

from meridian.workloads.claims_triage.models import (
    MAX_RUN_DESCRIPTION_CHARS,
    MAX_SUBMISSION_DESCRIPTION_CHARS,
    Claimant,
    ClaimDecision,
    ClaimFacts,
    ClaimMoveRequest,
    ClaimMoveResponse,
    ClaimSubmission,
    Decision,
    DecisionResponse,
    DocumentsArrival,
    DraftedBy,
    Outcome,
    ProposalSummary,
)


@pytest.mark.parametrize("claim", synthetic_claims(), ids=lambda c: c["claim_id"])
def test_every_synthetic_claim_is_a_valid_submission(claim: dict[str, Any]) -> None:
    submission = ClaimSubmission.model_validate(claim)

    assert submission.model_dump(mode="json") == claim


def valid(**overrides: Any) -> dict[str, Any]:
    return claim_with_id("CLM-9001") | overrides


@pytest.mark.parametrize(
    "overrides",
    [
        {"claim_id": "CLM-1"},
        {"claim_id": "clm-0001"},
        {"claim_id": "CLM-00001"},
        {"policy_number": "POL-1"},
        {"policy_number": "XXX-0001"},
        {"reported_on": "13 July"},
        {"loss_date": "2099-01-01"},  # after reported_on
        {"peril": "meteor"},
        {"claimed_amount": 0},
        {"claimed_amount": 1_000_001},
        {"claimed_amount": 12.5},
        {"loss_location": {"city": "", "country": "AT"}},
        {"loss_location": {"city": "x" * 101, "country": "AT"}},
        {"loss_location": {"city": "Linz", "country": "at"}},
        {"loss_location": {"city": "Linz", "country": "AUT"}},
        {"claimant": {"name": "", "email": "a@b.example"}},
        # A blank name would make an empty alternative in the run's name pass.
        {"claimant": {"name": " ", "email": "a@b.example"}},
        {"claimant": {"name": " \t\n", "email": "a@b.example"}},
        {"claimant": {"name": "n" * 201, "email": "a@b.example"}},
        {"claimant": {"name": "N", "email": "no-at-sign"}},
        {"claimant": {"name": "N", "email": "a@b"}},
        {"claimant": {"name": "N", "email": "a b@c.example"}},
        {"claimant": {"name": "N", "email": "a@" + "b" * 250 + ".example"}},
        {"description": ""},
        {"description": "x" * 5001},
        {"documents": ["d"] * 21},
        {"documents": [""]},
        {"documents": ["d" * 101]},
        {"extra_field": 1},
        # PostgreSQL text cannot hold NUL; it must be a 422, not a database error.
        {"description": "before\x00after"},
        {"claimant": {"name": "N\x00", "email": "a@b.example"}},
        {"claimant": {"name": "N", "email": "a\x00@b.example"}},
        {"loss_location": {"city": "Li\x00nz", "country": "AT"}},
        {"documents": ["photo\x00"]},
    ],
)
def test_an_invalid_submission_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ClaimSubmission.model_validate(valid(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"claimed_amount": 1},
        {"claimed_amount": 1_000_000},
        {"description": "x" * 5000},
        {"documents": ["d" * 100] * 20},
        {"documents": []},
        {"loss_date": "2026-07-13", "reported_on": "2026-07-13"},  # the same day
        {"loss_location": {"city": "c" * 100, "country": "AT"}},
        {"claimant": {"name": "n" * 200, "email": "a@b.example"}},
    ],
)
def test_the_limits_themselves_are_accepted(overrides: dict[str, Any]) -> None:
    ClaimSubmission.model_validate(valid(**overrides))


def test_a_claimant_name_is_stripped() -> None:
    claimant = Claimant.model_validate({"name": "  Ana Kovacs\n", "email": "a@b.co"})

    assert claimant.name == "Ana Kovacs"


def test_a_name_of_200_characters_after_the_strip_is_accepted() -> None:
    claimant = Claimant.model_validate({"name": f" {'n' * 200} ", "email": "a@b.co"})

    assert claimant.name == "n" * 200


def test_the_run_s_description_may_be_three_times_the_submission_s() -> None:
    """``ClaimSubmission`` is a ``ClaimFacts``, so the field was one shared
    constraint. The run's copy may outgrow the submission's text (a three-letter
    name part becomes ``[name]``), so ``ClaimFacts`` takes three times as much and
    the submission is narrowed again."""
    assert MAX_RUN_DESCRIPTION_CHARS == 3 * MAX_SUBMISSION_DESCRIPTION_CHARS
    assert MAX_SUBMISSION_DESCRIPTION_CHARS == 5000
    ClaimSubmission.model_validate(valid(description="x" * 5000))
    with pytest.raises(ValidationError):
        ClaimSubmission.model_validate(valid(description="x" * 5001))
    facts = {k: v for k, v in valid().items() if k != "claimant"}
    ClaimFacts.model_validate(facts | {"description": "x" * MAX_RUN_DESCRIPTION_CHARS})
    with pytest.raises(ValidationError):
        ClaimFacts.model_validate(
            facts | {"description": "x" * (MAX_RUN_DESCRIPTION_CHARS + 1)}
        )


def test_a_submission_is_frozen() -> None:
    submission = ClaimSubmission.model_validate(valid())

    with pytest.raises(ValidationError):
        submission.claimed_amount = 1


DRAFTED_BY = {"deployment": "replay-chat", "provider": "replay", "mode": "replay"}
SUMMARY = {"route": "adjuster", "drafted_by": DRAFTED_BY}


def test_a_proposal_summary_takes_a_route_and_a_drafted_by() -> None:
    summary = ProposalSummary.model_validate(SUMMARY)

    assert summary.drafted_by is not None
    assert summary.drafted_by.provider == "replay"


PROMPT = "a" * 64


def test_a_drafted_by_takes_a_prompt_version() -> None:
    drafted_by = DraftedBy.model_validate(DRAFTED_BY | {"prompt": PROMPT})

    assert drafted_by.prompt == PROMPT


def test_a_drafted_by_without_a_prompt_reads_as_a_proposal_stored_before_s017() -> None:
    drafted_by = DraftedBy.model_validate(DRAFTED_BY)

    assert drafted_by.prompt is None


@pytest.mark.parametrize(
    "prompt",
    ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "", " " + "a" * 63, "a" * 64 + "\n"],
    ids=["short", "long", "uppercase", "not-hex", "empty", "space", "newline"],
)
def test_a_drafted_by_refuses_a_prompt_that_is_not_64_lowercase_hex(
    prompt: str,
) -> None:
    with pytest.raises(ValidationError):
        DraftedBy.model_validate(DRAFTED_BY | {"prompt": prompt})


def test_a_proposal_summary_has_no_reason() -> None:
    # The reason code would tell a caller whether a policy exists, lapsed, and
    # (by bisecting the amount) its deductible and limit.
    assert "reason" not in ProposalSummary.model_fields
    with pytest.raises(ValidationError):
        ProposalSummary.model_validate(SUMMARY | {"reason": "unverified"})


def test_a_proposal_summary_may_say_that_no_model_was_called() -> None:
    summary = ProposalSummary.model_validate(SUMMARY | {"drafted_by": None})

    assert summary.drafted_by is None


@pytest.mark.parametrize(
    "broken",
    [
        {"route": "auto_reject"},
        {"extra": 1},
        {"drafted_by": {"deployment": "d", "provider": "p"}},
        # PostgreSQL text cannot hold NUL: a DraftedBy is written to a column.
        {"drafted_by": DRAFTED_BY | {"deployment": "eu\x00chat"}},
        {"drafted_by": DRAFTED_BY | {"provider": "re\x00play"}},
        {"drafted_by": DRAFTED_BY | {"mode": "re\x00play"}},
    ],
)
def test_a_summary_with_an_unknown_route_or_field_is_refused(
    broken: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ProposalSummary.model_validate(SUMMARY | broken)


def test_a_summary_leaves_out_the_drafted_by_field_only_by_saying_null() -> None:
    with pytest.raises(ValidationError):
        ProposalSummary.model_validate({"route": "adjuster"})


def test_the_facts_are_the_submission_without_the_claimant() -> None:
    claim = valid()
    facts = {k: v for k, v in claim.items() if k != "claimant"}

    assert ClaimFacts.model_validate(facts).model_dump(mode="json") == facts
    assert "claimant" not in ClaimFacts.model_fields
    with pytest.raises(ValidationError):  # the graph is never sent the claimant
        ClaimFacts.model_validate(claim)


def test_the_submission_adds_only_the_claimant_to_the_facts() -> None:
    assert set(ClaimSubmission.model_fields) - set(ClaimFacts.model_fields) == {
        "claimant"
    }


# -- S048: the documents that arrive, and the answers that may have no run ----
@pytest.mark.parametrize(
    "documents",
    [["a"], ["x" * 100], [f"d{n}" for n in range(20)], ["same", "same"]],
    ids=["one", "name-of-100", "twenty", "a-repeated-name"],
)
def test_documents_arriving_take_one_to_twenty_names(documents: list[str]) -> None:
    arrival = DocumentsArrival.model_validate({"documents": documents})

    assert arrival.documents == tuple(documents)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"documents": []}, id="empty"),
        pytest.param({"documents": [f"d{n}" for n in range(21)]}, id="twenty-one"),
        pytest.param({"documents": ["x" * 101]}, id="name-of-101"),
        pytest.param({"documents": [""]}, id="empty-name"),
        pytest.param({"documents": ["a\x00b"]}, id="nul"),
        pytest.param({"documents": [1]}, id="number"),
        pytest.param({"documents": [None]}, id="null-name"),
        pytest.param({"documents": [b"a"]}, id="bytes-under-strict"),
        pytest.param({"documents": "a"}, id="a-bare-string"),
        pytest.param({}, id="no-documents"),
        pytest.param({"documents": ["a"], "claim_id": "CLM-0001"}, id="extra-field"),
    ],
)
def test_documents_arriving_that_do_not_fit_are_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DocumentsArrival.model_validate(body)


def test_documents_arriving_are_read_from_a_json_array() -> None:
    arrival = DocumentsArrival.model_validate_json('{"documents": ["a", "b"]}')

    assert arrival.documents == ("a", "b")


def test_a_claim_move_request_is_an_empty_json_object() -> None:
    assert ClaimMoveRequest.model_validate_json("{}") == ClaimMoveRequest()
    assert not ClaimMoveRequest.model_fields


@pytest.mark.parametrize("body", ['{"state": "approved"}', "[]", "null", '"{}"'])
def test_a_claim_move_request_with_anything_else_is_refused(body: str) -> None:
    with pytest.raises(ValidationError):
        ClaimMoveRequest.model_validate_json(body)


def test_a_move_response_may_carry_the_state_alone() -> None:
    answer = ClaimMoveResponse.model_validate(
        {"claim_id": "CLM-0001", "state": "withdrawn"}
    )

    assert (answer.run_id, answer.run_status, answer.proposal) == (None, None, None)


def test_a_move_response_carries_the_run_and_the_proposal_when_a_triage_ran() -> None:
    run_id = "00000000-0000-4000-8000-000000000001"

    answer = ClaimMoveResponse.model_validate(
        {
            "claim_id": "CLM-0001",
            "state": "awaiting_adjuster",
            "run_id": run_id,
            "run_status": "AwaitingApproval",
            "proposal": SUMMARY,
        }
    )

    assert str(answer.run_id) == run_id
    assert answer.proposal == ProposalSummary.model_validate(SUMMARY)


@pytest.mark.parametrize(
    "broken",
    [
        {"state": "done"},
        {"run_status": "Dancing"},
        {"proposal": {**SUMMARY, "reason": "over_threshold"}},  # T-65
        {"unknown": 1},
    ],
)
def test_a_move_response_that_does_not_fit_is_refused(broken: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ClaimMoveResponse.model_validate(
            {"claim_id": "CLM-0001", "state": "withdrawn"} | broken
        )


def test_a_decision_response_may_have_no_run() -> None:
    answer = DecisionResponse.model_validate(
        {"claim_id": "CLM-0001", "state": "approved"}
    )

    assert (answer.run_id, answer.run_status) == (None, None)
    assert answer.model_dump(mode="json") == {
        "claim_id": "CLM-0001",
        "state": "approved",
        "run_id": None,
        "run_status": None,
    }


def test_the_decision_route_s_body_still_has_three_words_and_the_outcome_five() -> None:
    assert set(get_args(Decision)) == {"approve", "reject", "request_documents"}
    assert set(get_args(Outcome)) == set(get_args(Decision)) | {
        "send_back",
        "withdrawn",
    }
    for word in ("send_back", "withdrawn"):
        with pytest.raises(ValidationError):
            ClaimDecision.model_validate({"decision": word})
