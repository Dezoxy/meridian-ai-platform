"""Pure claim rules and the shared result type. No framework is imported here.

Both flows call these functions, so they differ only in orchestration.
"""

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

# spikes/s005-agent-framework/src/claimflow/rules.py -> repository root.
REPO_ROOT = Path(__file__).resolve().parents[4]
DATA_DIR = REPO_ROOT / "data" / "synthetic"

APPROVAL_THRESHOLD_EUR = 2500

Status = Literal["completed", "awaiting_approval"]
Proposal = Literal["approve", "reject", "auto_approve"]
Decision = Literal["approve", "reject"]


class UnknownClaimError(LookupError):
    """The claim id is not in the golden set."""


@dataclass(frozen=True)
class ValidatedClaim:
    claim: dict[str, Any]
    policy: dict[str, Any]
    policy_valid: bool


@dataclass(frozen=True)
class Assessment:
    claim_id: str
    proposal: Proposal
    needs_approval: bool


@dataclass
class AdjusterDecision:
    """The payload of the approval pause. Both frameworks build it from a dict.

    Both run `__post_init__` when they build it, so the value rule is enforced
    by either framework and not only by the type check.
    """

    decision: Decision
    adjuster_id: str

    def __post_init__(self) -> None:
        if not self.adjuster_id.strip():
            raise ValueError("adjuster_id must not be empty")


@dataclass(frozen=True)
class AssessedClaim:
    """What the flow carries into the approval step: the claim and the proposal."""

    validated: ValidatedClaim
    assessment: Assessment


@dataclass(frozen=True)
class RunOutcome:
    claim_id: str
    proposal: Proposal
    outcome: Proposal
    adjuster_id: str | None


@dataclass(frozen=True)
class RunResult:
    """What every flow returns. `outcome` is None while a decision is pending."""

    status: Status
    run_ref: str
    claim_id: str
    proposal: Proposal
    outcome: Proposal | None
    adjuster_id: str | None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def _load(name: str) -> list[dict[str, Any]]:
    with (DATA_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def policy_is_valid(policy: dict[str, Any], loss_date: str) -> bool:
    """Active, and the loss date within the term (both ends inclusive)."""
    if policy["status"] != "active":
        return False
    start = date.fromisoformat(policy["start_date"])
    end = date.fromisoformat(policy["end_date"])
    return start <= date.fromisoformat(loss_date) <= end


def validate(claim_id: str) -> ValidatedClaim:
    """Step 1: load the claim and its policy and check the policy."""
    claims = {claim["claim_id"]: claim for claim in _load("claims.json")}
    if claim_id not in claims:
        raise UnknownClaimError(claim_id)
    claim = claims[claim_id]
    policies = {policy["policy_number"]: policy for policy in _load("policies.json")}
    policy = policies[claim["policy_number"]]
    return ValidatedClaim(claim, policy, policy_is_valid(policy, claim["loss_date"]))


def policy_summary(policy_number: str) -> str:
    """The policy fields both frameworks' `lookup_policy` tool returns, as JSON."""
    for policy in _load("policies.json"):
        if policy["policy_number"] == policy_number:
            keys = ("policy_number", "status", "start_date", "end_date")
            return json.dumps({key: policy[key] for key in keys})
    raise LookupError(policy_number)


def assess(claim_id: str, claimed_amount: int, policy_valid: bool) -> Assessment:
    """Step 2: propose an outcome and say whether a human must decide."""
    if not policy_valid:
        return Assessment(claim_id, "reject", needs_approval=True)
    if claimed_amount > APPROVAL_THRESHOLD_EUR:
        return Assessment(claim_id, "approve", needs_approval=True)
    return Assessment(claim_id, "auto_approve", needs_approval=False)


def assess_validated(validated: ValidatedClaim) -> Assessment:
    claim = validated.claim
    return assess(claim["claim_id"], claim["claimed_amount"], validated.policy_valid)


def auto_outcome(assessed: AssessedClaim) -> RunOutcome:
    """Step 3, no pause: the proposal stands as the outcome."""
    assessment = assessed.assessment
    return RunOutcome(
        assessment.claim_id, assessment.proposal, assessment.proposal, None
    )


def decided_outcome(
    assessed: AssessedClaim, decision: Decision, adjuster_id: str
) -> RunOutcome:
    """Step 3, after the pause: the adjuster's decision is the outcome."""
    assessment = assessed.assessment
    return RunOutcome(assessment.claim_id, assessment.proposal, decision, adjuster_id)


def golden_route(claim_id: str) -> str:
    """The labelled route from the golden set; a cross-check for tests only."""
    for outcome in _load("expected-outcomes.json"):
        if outcome["claim_id"] == claim_id:
            return outcome["route"]
    raise UnknownClaimError(claim_id)
