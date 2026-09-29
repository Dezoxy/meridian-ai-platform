"""The pure rules both flows call. No framework is imported here."""

import pytest
from claimflow import rules
from support import (
    AUTO_APPROVE_CLAIM,
    BOUNDARY_CLAIM,
    LAPSED_POLICY_CLAIM,
    OUT_OF_DATE_CLAIM,
    OVER_THRESHOLD_CLAIM,
)


def _policy(**overrides: str) -> dict:
    policy = {"status": "active", "start_date": "2026-01-01", "end_date": "2026-12-31"}
    return {**policy, **overrides}


def test_threshold_is_2500_euro() -> None:
    assert rules.APPROVAL_THRESHOLD_EUR == 2500


@pytest.mark.parametrize(
    ("loss_date", "expected"),
    [
        ("2025-12-31", False),
        ("2026-01-01", True),  # start date is inclusive
        ("2026-12-31", True),  # end date is inclusive
        ("2027-01-01", False),
    ],
)
def test_policy_term_bounds_are_inclusive(loss_date: str, expected: bool) -> None:
    assert rules.policy_is_valid(_policy(), loss_date) is expected


@pytest.mark.parametrize("status", ["lapsed", "cancelled", "ACTIVE", ""])
def test_only_status_active_makes_a_policy_valid(status: str) -> None:
    assert rules.policy_is_valid(_policy(status=status), "2026-06-01") is False


def test_amount_at_the_threshold_is_auto_approved() -> None:
    assessment = rules.assess("CLM-X", claimed_amount=2500, policy_valid=True)
    assert (assessment.proposal, assessment.needs_approval) == ("auto_approve", False)


def test_amount_one_euro_over_the_threshold_needs_approval() -> None:
    assessment = rules.assess("CLM-X", claimed_amount=2501, policy_valid=True)
    assert (assessment.proposal, assessment.needs_approval) == ("approve", True)


def test_invalid_policy_is_rejected_whatever_the_amount() -> None:
    small = rules.assess("CLM-X", claimed_amount=1, policy_valid=False)
    large = rules.assess("CLM-X", claimed_amount=99999, policy_valid=False)
    assert (small.proposal, small.needs_approval) == ("reject", True)
    assert (large.proposal, large.needs_approval) == ("reject", True)


def test_validate_loads_the_claim_and_its_policy() -> None:
    validated = rules.validate(OVER_THRESHOLD_CLAIM)
    assert validated.claim["claim_id"] == OVER_THRESHOLD_CLAIM
    assert validated.policy["policy_number"] == validated.claim["policy_number"]
    assert validated.policy_valid is True


def test_validate_unknown_claim_raises() -> None:
    with pytest.raises(rules.UnknownClaimError):
        rules.validate("CLM-9999")


@pytest.mark.parametrize(
    ("claim_id", "valid", "proposal"),
    [
        (AUTO_APPROVE_CLAIM, True, "auto_approve"),
        (OVER_THRESHOLD_CLAIM, True, "approve"),
        (BOUNDARY_CLAIM, True, "approve"),
        (LAPSED_POLICY_CLAIM, False, "reject"),
        (OUT_OF_DATE_CLAIM, False, "reject"),
    ],
)
def test_golden_set_picks_behave_as_the_tests_assume(
    claim_id: str, valid: bool, proposal: str
) -> None:
    validated = rules.validate(claim_id)
    assessment = rules.assess_validated(validated)
    assert validated.policy_valid is valid
    assert assessment.proposal == proposal


def test_auto_approve_pick_is_labelled_auto_approve_in_the_golden_set() -> None:
    # Cross-check only: the decision comes from the rules, not from the label.
    assert rules.golden_route(AUTO_APPROVE_CLAIM) == "auto_approve"


def test_out_of_date_pick_has_an_active_policy() -> None:
    # CLM-0014 exercises the term rule independently of the status rule.
    assert rules.validate(OUT_OF_DATE_CLAIM).policy["status"] == "active"
