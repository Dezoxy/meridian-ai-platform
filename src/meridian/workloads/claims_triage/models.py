"""The claim submission (mirrors data/synthetic/claims.json) and the answer."""

from datetime import date
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from meridian.platform.common.http import ErrorBody
from meridian.platform.common.wire import NoNul, WireModel
from meridian.runtime.models import RunState

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
    deployment: str
    provider: str
    mode: str


class ProposalSummary(WireModel):
    """The answer's view of the proposal (``TriageProposal`` is in proposal.py);
    ``reason`` is the reason code, ``drafted_by`` is null when no model ran."""

    route: Route
    reason: str
    drafted_by: DraftedBy | None


class ClaimResponse(WireModel):
    claim_id: str
    run_id: UUID
    run_status: RunState
    proposal: ProposalSummary | None


class ClaimErrorBody(ErrorBody):
    """An error answer after the claim was stored: which claim, and which run
    when one was started."""

    claim_id: str
    run_id: UUID | None = None
