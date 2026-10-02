"""The triage proposal: what the rules decide, and what it may not contradict."""

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from meridian.workloads.claims_triage.proposal import Citation, TriageProposal
from meridian.workloads.claims_triage.rules import AUTO_APPROVAL_LIMIT, Reason

DRAFTED_BY = {"deployment": "replay-chat", "provider": "replay", "mode": "replay"}
CITATION = {"product": "HOME-STD", "wording_version": "2026-01", "clause": "2.1"}


def proposal(**overrides: Any) -> dict[str, Any]:
    """A valid ``within_threshold`` proposal (the model said nothing applies),
    changed by ``overrides``."""
    return {
        "route": "auto_approve",
        "reason": "within_threshold",
        "recommendation": "approve",
        "payable_amount": 1800,
        "exclusion_clause": None,
        "fraud_indicators": [],
        "missing_documents": [],
        "citations": [CITATION],
        "gaps": [],
        "assessment": "none_applies",
        "unavailable_because": None,
        "rationale": "No circumstance exclusion applies to a burst pipe.",
        "drafted_by": DRAFTED_BY,
    } | overrides


# One valid proposal per reason: the changes that turn the base one into it.
VALID_BY_REASON: dict[str, dict[str, Any]] = {
    "within_threshold": {},
    "over_threshold": {
        "route": "adjuster",
        "reason": "over_threshold",
        "payable_amount": AUTO_APPROVAL_LIMIT + 1,
    },
    "fraud_indicator": {
        "route": "adjuster",
        "reason": "fraud_indicator",
        "fraud_indicators": ["early_loss", "late_report"],
    },
    "excluded": {
        "route": "adjuster",
        "reason": "excluded",
        "recommendation": "reject",
        "payable_amount": None,
        "exclusion_clause": "5.2",
        "assessment": "applies",
    },
    "policy_inactive": {
        "route": "adjuster",
        "reason": "policy_inactive",
        "recommendation": "reject",
        "payable_amount": None,
        "assessment": "not_needed",
        "rationale": None,
        "drafted_by": None,
    },
    "missing_documents": {
        "route": "request_documents",
        "reason": "missing_documents",
        "recommendation": None,
        "payable_amount": None,
        "missing_documents": ["photos", "repair_estimate"],
    },
    "policy_not_found": {
        "route": "adjuster",
        "reason": "policy_not_found",
        "recommendation": None,
        "payable_amount": None,
        "citations": [],
        "assessment": "not_needed",
        "rationale": None,
        "drafted_by": None,
    },
    "nothing_payable": {
        "route": "adjuster",
        "reason": "nothing_payable",
        "recommendation": None,
        "payable_amount": None,
    },
    "unverified": {
        "route": "adjuster",
        "reason": "unverified",
        "recommendation": None,
        "gaps": ["exclusion_assessment"],
        "assessment": "unavailable",
        "unavailable_because": "not-json",
        "rationale": None,
    },
}


@pytest.mark.parametrize("reason", VALID_BY_REASON)
def test_a_valid_proposal_for_each_reason_is_accepted_and_round_trips(
    reason: str,
) -> None:
    document = proposal(**VALID_BY_REASON[reason])

    accepted = TriageProposal.model_validate(document)

    assert accepted.reason == reason
    assert accepted.model_dump(mode="json") == document


def test_every_reason_the_rules_can_give_has_a_valid_proposal() -> None:
    assert set(VALID_BY_REASON) == set(get_args(Reason))


def test_the_graphs_proposal_without_an_assessment_is_accepted() -> None:
    """What the one-node graph builds: the model was asked and gave no usable
    answer, so the claim goes to an adjuster, unverified."""
    TriageProposal.model_validate(
        proposal(
            **VALID_BY_REASON["unverified"],
            citations=[],
        )
    )


def test_a_peril_exclusion_needs_no_model_call() -> None:
    """The wording excludes the peril itself: excluded, and nothing to assess."""
    TriageProposal.model_validate(
        proposal(
            **VALID_BY_REASON["excluded"]
            | {"assessment": "not_needed", "rationale": None, "drafted_by": None}
        )
    )


def test_a_proposal_for_a_claim_without_a_model_call_has_no_drafted_by() -> None:
    accepted = TriageProposal.model_validate(
        proposal(assessment="not_needed", rationale=None, drafted_by=None)
    )

    assert accepted.drafted_by is None


# ── the reason fixes the route ──────────────────────────────────────────────
@pytest.mark.parametrize(
    ("reason", "route"),
    [
        ("within_threshold", "adjuster"),
        ("within_threshold", "request_documents"),
        ("missing_documents", "adjuster"),
        ("missing_documents", "auto_approve"),
        ("over_threshold", "auto_approve"),
        ("over_threshold", "request_documents"),
        ("excluded", "request_documents"),
        ("unverified", "auto_approve"),
    ],
)
def test_a_route_the_reason_does_not_give_is_refused(reason: str, route: str) -> None:
    base = VALID_BY_REASON[reason]

    with pytest.raises(ValidationError):
        TriageProposal.model_validate(proposal(**base | {"route": route}))


# ── auto_approve ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "change",
    [
        {"recommendation": None},
        {"recommendation": "reject"},
        {"payable_amount": None},
        {"payable_amount": AUTO_APPROVAL_LIMIT + 1},
        {"fraud_indicators": ["late_report"]},
        {"missing_documents": ["photos"]},
        {"gaps": ["claim_history"]},
        {"exclusion_clause": "5.2"},
        {"assessment": "applies"},
        {"citations": []},
    ],
    ids=[
        "no-recommendation",
        "reject",
        "no-amount",
        "amount-over-the-limit",
        "fraud-indicator",
        "missing-document",
        "gap",
        "exclusion",
        "assessment-applies",
        "no-citation",
    ],
)
def test_an_auto_approval_that_breaks_a_condition_is_refused(
    change: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        TriageProposal.model_validate(proposal(**change))


@pytest.mark.parametrize("drafted_by", [DRAFTED_BY, None], ids=["called", "too-long"])
@pytest.mark.parametrize(
    "because", ["truncated", "not-json", "not-the-format", "unknown-clause", "unsure"]
)
def test_an_unavailable_assessment_alone_refuses_an_auto_approval(
    because: str, drafted_by: dict[str, str] | None
) -> None:
    """Everything else of the proposal is valid: no rationale, no gap, a
    recommendation to approve, a citation. Only the assessment is wrong."""
    document = proposal(
        assessment="unavailable",
        unavailable_because=because,
        rationale=None,
        drafted_by=drafted_by,
    )

    with pytest.raises(ValidationError, match="assessment that found no exclusion"):
        TriageProposal.model_validate(document)


def test_an_auto_approval_needs_only_that_assessment_of_the_two_it_accepts() -> None:
    for assessment, rationale, drafted_by in (
        ("none_applies", "Nothing applies.", DRAFTED_BY),
        ("not_needed", None, None),
    ):
        TriageProposal.model_validate(
            proposal(assessment=assessment, rationale=rationale, drafted_by=drafted_by)
        )


def test_an_auto_approval_at_the_limit_is_accepted_and_one_above_is_refused() -> None:
    TriageProposal.model_validate(proposal(payable_amount=AUTO_APPROVAL_LIMIT))
    with pytest.raises(ValidationError):
        TriageProposal.model_validate(proposal(payable_amount=AUTO_APPROVAL_LIMIT + 1))


def test_an_auto_approval_without_a_model_call_is_accepted() -> None:
    TriageProposal.model_validate(
        proposal(assessment="not_needed", rationale=None, drafted_by=None)
    )


# ── excluded ────────────────────────────────────────────────────────────────
def excluded(**overrides: Any) -> dict[str, Any]:
    return proposal(**VALID_BY_REASON["excluded"] | overrides)


@pytest.mark.parametrize(
    "change",
    [
        {"exclusion_clause": None},
        {"recommendation": "approve"},
        {"recommendation": None},
    ],
    ids=["reason-without-clause", "approve", "no-recommendation"],
)
def test_an_exclusion_that_contradicts_itself_is_refused(
    change: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        TriageProposal.model_validate(excluded(**change))


def test_an_exclusion_clause_under_another_reason_is_refused() -> None:
    with pytest.raises(ValidationError):
        TriageProposal.model_validate(
            proposal(**VALID_BY_REASON["over_threshold"] | {"exclusion_clause": "5.2"})
        )


# ── missing documents and gaps ──────────────────────────────────────────────
def test_missing_documents_as_a_reason_needs_a_missing_document() -> None:
    document = proposal(**VALID_BY_REASON["missing_documents"])
    TriageProposal.model_validate(document)

    with pytest.raises(ValidationError):
        TriageProposal.model_validate(document | {"missing_documents": []})


def test_unverified_needs_a_gap() -> None:
    document = proposal(**VALID_BY_REASON["unverified"])
    TriageProposal.model_validate(document)

    with pytest.raises(ValidationError):
        TriageProposal.model_validate(document | {"gaps": []})


# ── the assessment, the model's words and the model call ────────────────────
@pytest.mark.parametrize(
    "change",
    [
        {"drafted_by": None},  # the model was asked
        {"assessment": "not_needed"},  # no call, yet a drafted_by and a rationale
        {"assessment": "not_needed", "rationale": None},  # no call, yet a drafted_by
        {"assessment": "applies", "drafted_by": None, "exclusion_clause": "5.2"},
    ],
    ids=[
        "answered-without-model-call",
        "not-needed-with-both",
        "not-needed-with-drafted-by",
        "applies-without-call",
    ],
)
def test_drafted_by_is_none_when_no_assessment_was_needed_and_set_when_answered(
    change: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        TriageProposal.model_validate(proposal(**change))


@pytest.mark.parametrize("drafted_by", [DRAFTED_BY, None], ids=["called", "too-long"])
def test_an_unavailable_assessment_has_a_drafted_by_or_none_when_no_call_was_made(
    drafted_by: dict[str, str] | None,
) -> None:
    accepted = TriageProposal.model_validate(
        proposal(
            **VALID_BY_REASON["unverified"]
            | {
                "unavailable_because": "too-long" if drafted_by is None else "unsure",
                "drafted_by": drafted_by,
            }
        )
    )

    assert (accepted.drafted_by is None) == (drafted_by is None)


@pytest.mark.parametrize(
    "because",
    ["truncated", "not-json", "not-the-format", "unknown-clause", "unsure", "too-long"],
)
def test_an_unavailable_assessment_carries_one_of_the_six_reason_words(
    because: str,
) -> None:
    document = proposal(
        **VALID_BY_REASON["unverified"] | {"unavailable_because": because}
    )

    accepted = TriageProposal.model_validate(document)

    assert accepted.model_dump(mode="json")["unavailable_because"] == because


@pytest.mark.parametrize(
    "change",
    [
        {"unavailable_because": None},  # unavailable, and no word for it
        {"unavailable_because": "because"},
        {"unavailable_because": ""},
        {"assessment": "none_applies"},  # a word, yet the model answered
        {"assessment": "applies", "exclusion_clause": "5.2", "rationale": "Racing."},
        {"assessment": "not_needed", "drafted_by": None},
    ],
    ids=[
        "unavailable-without-a-word",
        "unknown-word",
        "empty-word",
        "word-with-none-applies",
        "word-with-applies",
        "word-with-not-needed",
    ],
)
def test_the_reason_word_is_set_exactly_when_the_assessment_is_unavailable(
    change: dict[str, Any],
) -> None:
    document = proposal(**VALID_BY_REASON["unverified"]) | change

    with pytest.raises(ValidationError):
        TriageProposal.model_validate(document)


@pytest.mark.parametrize("assessment", ["not_needed", "unavailable"])
def test_a_rationale_without_an_answer_from_the_model_is_refused(
    assessment: str,
) -> None:
    drafted_by = None if assessment == "not_needed" else DRAFTED_BY
    base = {
        "route": "adjuster",
        "reason": "unverified",
        "recommendation": None,
        "gaps": ["claim_history"],
        "assessment": assessment,
        "unavailable_because": "unsure" if assessment == "unavailable" else None,
        "drafted_by": drafted_by,
    }
    TriageProposal.model_validate(proposal(**base, rationale=None))

    with pytest.raises(ValidationError):
        TriageProposal.model_validate(proposal(**base, rationale="the model's words"))


# ── the field limits ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "change",
    [
        {"route": "auto_reject"},
        {"reason": "because"},
        {"recommendation": "refer"},
        {"payable_amount": 0},
        {"payable_amount": 1_000_000_001},
        {"payable_amount": 12.5},
        {"fraud_indicators": ["witness"]},
        {"missing_documents": ["passport"]},
        {"gaps": ["mood"]},
        {"assessment": "maybe"},
        {"rationale": ""},
        {"rationale": "x" * 601},
        {"rationale": "before\x00after"},
        {"citations": [CITATION] * 11},
        {"citations": [{**CITATION, "product": ""}]},
        {"citations": [{**CITATION, "product": "p" * 33}]},
        {"citations": [{**CITATION, "wording_version": ""}]},
        {"citations": [{**CITATION, "wording_version": "v" * 17}]},
        {"citations": [{**CITATION, "clause": "0.1"}]},
        {"citations": [{**CITATION, "clause": "2"}]},
        {"citations": [{**CITATION, "clause": "100.1"}]},
        {"citations": [{**CITATION, "clause": "2.1\n"}]},
        {"citations": [{**CITATION, "extra": 1}]},
        {"drafted_by": {"deployment": "d", "provider": "p"}},
        {"citations": [{**CITATION, "product": "HOME\x00STD"}]},
        {"citations": [{**CITATION, "wording_version": "2026\x00-01"}]},
        {"drafted_by": {**DRAFTED_BY, "deployment": "eu\x00chat"}},
        {"drafted_by": {**DRAFTED_BY, "provider": "azure\x00openai"}},
        {"drafted_by": {**DRAFTED_BY, "mode": "li\x00ve"}},
        {"unavailable_because": "because"},
        {"extra": 1},
    ],
)
def test_a_value_outside_its_limits_is_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TriageProposal.model_validate(proposal(**change))


@pytest.mark.parametrize(
    "change",
    [
        {"payable_amount": 1},
        {"rationale": "x" * 600},
        {"citations": [CITATION] * 10},
        {"citations": [{**CITATION, "product": "p" * 32, "wording_version": "v" * 16}]},
        {"citations": [{**CITATION, "clause": "99.99"}]},
    ],
)
def test_the_limits_themselves_are_accepted(change: dict[str, Any]) -> None:
    TriageProposal.model_validate(proposal(**change))


def test_a_field_may_not_be_left_out() -> None:
    document = proposal()
    del document["gaps"]

    with pytest.raises(ValidationError):
        TriageProposal.model_validate(document)


def test_a_proposal_is_frozen() -> None:
    accepted = TriageProposal.model_validate(proposal())

    with pytest.raises(ValidationError):
        accepted.route = "adjuster"


def test_a_citation_is_frozen_and_takes_no_unknown_field() -> None:
    citation = Citation.model_validate(CITATION)

    with pytest.raises(ValidationError):
        citation.clause = "9.9"
    with pytest.raises(ValidationError):
        Citation.model_validate(CITATION | {"extra": 1})
