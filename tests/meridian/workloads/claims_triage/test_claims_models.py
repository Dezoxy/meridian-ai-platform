"""The claim submission mirrors data/synthetic/claims.json."""

from typing import Any

import pytest
from pydantic import ValidationError
from servicesupport import claim_with_id, synthetic_claims

from meridian.workloads.claims_triage.models import (
    ClaimFacts,
    ClaimSubmission,
    TriageProposal,
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


def test_a_submission_is_frozen() -> None:
    submission = ClaimSubmission.model_validate(valid())

    with pytest.raises(ValidationError):
        submission.claimed_amount = 1


PROPOSAL = {
    "route": "adjuster",
    "reason": "because",
    "draft": "text",
    "drafted_by": {"deployment": "replay-chat", "provider": "replay", "mode": "replay"},
}


def test_a_triage_proposal_takes_the_graph_output() -> None:
    assert TriageProposal.model_validate(PROPOSAL).drafted_by.provider == "replay"


@pytest.mark.parametrize(
    "broken",
    [
        {"route": "auto_reject"},
        {"reason": ""},
        {"draft": ""},
        {"draft": "x" * 2001},
        {"extra": 1},
        {"drafted_by": {"deployment": "d", "provider": "p"}},
    ],
)
def test_a_proposal_the_graph_should_not_produce_is_refused(
    broken: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        TriageProposal.model_validate(PROPOSAL | broken)


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
