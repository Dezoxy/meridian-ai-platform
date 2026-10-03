"""The claim submission (mirrors data/synthetic/claims.json) and the answer."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, Strict, StringConstraints, model_validator

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
# The words recorded for a paused run in claims.decisions: the adjuster's three
# decisions and the two that end a run without one (S048, T-74).
Outcome = Literal["approve", "reject", "request_documents", "send_back", "withdrawn"]
# The note the graph records for an outcome: fixed text keyed by the word, so a
# note holds nothing a caller wrote.
DECISION_NOTES: Mapping[Outcome, str] = MappingProxyType(
    {
        "approve": "An adjuster decided to approve the claim.",
        "reject": "An adjuster decided to reject the claim.",
        "request_documents": "An adjuster decided to request more documents.",
        "send_back": "An adjuster sent the claim back to triage.",
        "withdrawn": "The claimant withdrew the claim.",
    }
)
# A simple local@domain.tld shape; real validation is the mail server's job.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=100), NoNul]
MAX_DOCUMENTS = 20
# What a claimant may submit, and what the run is sent. The run's copy of the
# description has the claimant's name and identifiers replaced by placeholders
# that can be longer than what they replace (``[name]`` for a three-letter part,
# ``[email]`` for a six-character address), so its bound is wider: a copy that
# no longer validates would fail every node of the graph, for good.
MAX_SUBMISSION_DESCRIPTION_CHARS = 5000
MAX_RUN_DESCRIPTION_CHARS = 3 * MAX_SUBMISSION_DESCRIPTION_CHARS


class LossLocation(WireModel):
    city: ShortText
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]


class Claimant(WireModel):
    # Stripped, so a blank name is no name: the run's copy of the description
    # replaces the name, and an empty one would match at every boundary.
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
        NoNul,
    ]
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
    description: Annotated[
        str,
        StringConstraints(min_length=1, max_length=MAX_RUN_DESCRIPTION_CHARS),
        NoNul,
    ]
    documents: tuple[ShortText, ...] = Field(max_length=MAX_DOCUMENTS)

    @model_validator(mode="after")
    def _loss_is_not_after_the_report(self) -> Self:
        if self.loss_date > self.reported_on:
            raise ValueError("loss_date is after reported_on")
        return self


class ClaimSubmission(ClaimFacts):
    """What ``POST /claims`` takes and ``claims.claims`` stores. Its description
    is held to the submission's limit; ``ClaimFacts`` takes the wider one."""

    description: Annotated[
        str,
        StringConstraints(min_length=1, max_length=MAX_SUBMISSION_DESCRIPTION_CHARS),
        NoNul,
    ]
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
    """The claim after the decision, and the run that was resumed on it: none
    when the claim was referred to an adjuster with no paused run (S048)."""

    claim_id: str
    state: LifecycleState
    run_id: UUID | None = None
    run_status: RunState | None = None


class DocumentsArrival(WireModel):
    """What ``POST /claims/{claim_id}/documents`` takes: the names of the
    documents that arrived, and nothing else (T-38). A name is strict, so no
    type is coerced into one; the list itself is not, because a JSON array
    reaches a strict tuple field as a list and would be refused."""

    documents: tuple[Annotated[ShortText, Strict()], ...] = Field(
        min_length=1, max_length=MAX_DOCUMENTS
    )


class ClaimMoveResponse(WireModel):
    """The answer of the routes that move a claim (triage again, withdrawal,
    documents): the claim's state, and the run and the proposal when a triage
    ran. The proposal is its route and the deployment asked, never its reason
    (T-65)."""

    claim_id: str
    state: LifecycleState
    run_id: UUID | None = None
    run_status: RunState | None = None
    proposal: ProposalSummary | None = None


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
