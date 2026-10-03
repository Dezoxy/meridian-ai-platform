"""The claim submission (mirrors data/synthetic/claims.json) and the answer."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from meridian.platform.common.http import ErrorBody
from meridian.platform.common.wire import NoNul, WireModel
from meridian.runtime.models import RunState
from meridian.workloads.claims_triage.lifecycle import LifecycleState

Peril = Literal[
    "accidental_damage",
    "burglary",
    "burst_pipe",
    "collision",
    "fire",
    "flood",
    "glass",
    "storm",
    "theft",
    "third_party_liability",
]
Route = Literal["adjuster", "auto_approve", "request_documents"]
# What an adjuster decides about a claim the rules routed to one.
Decision = Literal["approve", "reject", "request_documents"]
# The note the graph records for a decision: fixed text keyed by the word, so a
# note holds nothing a caller wrote.
DECISION_NOTES: Mapping[Decision, str] = MappingProxyType(
    {
        "approve": "An adjuster decided to approve the claim.",
        "reject": "An adjuster decided to reject the claim.",
        "request_documents": "An adjuster decided to request more documents.",
    }
)
# A simple local@domain.tld shape; real validation is the mail server's job.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=100), NoNul]
MAX_DOCUMENTS = 20


class LossLocation(WireModel):
    city: ShortText
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]


class Claimant(WireModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=200), NoNul]
    email: Annotated[
        str,
        StringConstraints(min_length=3, max_length=254, pattern=EMAIL_PATTERN),
        NoNul,
    ]


class ClaimFacts(WireModel):
    """The claim without the claimant: what the triage graph is sent. Its name
    and email stay in the Claims API, because the model does not need them
    (data minimisation, T-03)."""

    claim_id: Annotated[str, StringConstraints(pattern=r"^CLM-[0-9]{4}$")]
    policy_number: Annotated[str, StringConstraints(pattern=r"^POL-[0-9]{4}$")]
    reported_on: date
    loss_date: date
    peril: Peril
    claimed_amount: Annotated[int, Field(ge=1, le=1_000_000, strict=True)]
    loss_location: LossLocation
    description: Annotated[str, StringConstraints(min_length=1, max_length=5000), NoNul]
    documents: tuple[ShortText, ...] = Field(max_length=MAX_DOCUMENTS)

    @model_validator(mode="after")
    def _loss_is_not_after_the_report(self) -> Self:
        if self.loss_date > self.reported_on:
            raise ValueError("loss_date is after reported_on")
        return self


class ClaimSubmission(ClaimFacts):
    """What ``POST /claims`` takes and ``claims.claims`` stores."""

    claimant: Claimant


class DraftedBy(WireModel):
    deployment: Annotated[str, NoNul]
    provider: Annotated[str, NoNul]
    mode: Annotated[str, NoNul]
    # The version of the prompt the model was sent (assessment.py). Null only on
    # a proposal stored before S017: the adjuster's page validates stored
    # proposals with ``TriageProposal.model_validate`` (adjuster.py), so rows
    # already on a running cluster must still read. A new proposal always has it.
    prompt: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None


class ProposalSummary(WireModel):
    """The answer's view of the proposal (``TriageProposal`` is in proposal.py);
    ``drafted_by`` is null when no model ran. It carries no reason code: with a
    fresh claim ID per probe, the code tells a caller whether a policy number
    exists, that a policy lapsed and, by bisecting the amount, its deductible and
    limit. The stored proposal keeps the reason."""

    route: Route
    drafted_by: DraftedBy | None


class ClaimResponse(WireModel):
    claim_id: str
    state: LifecycleState
    run_id: UUID
    run_status: RunState
    proposal: ProposalSummary | None


class ClaimDecision(WireModel):
    """What ``POST /claims/{claim_id}/decision`` takes: one decision word and
    nothing else. Strict, so no type is coerced into a word."""

    model_config = ConfigDict(strict=True)

    decision: Decision


class DecisionResponse(WireModel):
    """The claim after the decision, and the run that was resumed on it."""

    claim_id: str
    state: LifecycleState
    run_id: UUID
    run_status: RunState


@dataclass(frozen=True, slots=True)
class DecisionFailure:
    """A decision that did not complete, as a status and a fixed text: the
    JSON route answers it as JSON and the adjuster's page renders it. The run's
    ID is there once the decision was recorded and a resume was tried."""

    status: int
    detail: str
    run_id: UUID | None = None


class ClaimErrorBody(ErrorBody):
    """An error answer after the claim was stored: which claim, and which run
    when one was started."""

    claim_id: str
    run_id: UUID | None = None
