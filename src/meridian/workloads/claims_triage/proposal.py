"""The triage proposal: what the rules decided, with the model's one fact.

The graph writes it to ``output`` and the Claims API validates and stores it.
The validator refuses a proposal that contradicts itself, so a bug in the
graph is a failed run, not a stored decision (T-28, C-02): the route follows
from the reason, and an automatic approval needs every condition the rules
need.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from meridian.platform.common.wire import NoNul, WireModel

from .models import DraftedBy, Route
from .rules import AUTO_APPROVAL_LIMIT, Document, FraudIndicator, Gap, Reason

CLAUSE_PATTERN = r"^[1-9][0-9]?\.[1-9][0-9]?$"
MAX_CITATIONS = 10
MAX_RATIONALE_CHARS = 600
MAX_PAYABLE_AMOUNT = 1_000_000_000

Clause = Annotated[str, StringConstraints(pattern=CLAUSE_PATTERN)]
Euros = Annotated[int, Field(ge=1, le=MAX_PAYABLE_AMOUNT, strict=True)]
# The model's words: free text, so no NUL byte reaches PostgreSQL.
Rationale = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_RATIONALE_CHARS), NoNul
]
AssessmentStatus = Literal["not_needed", "none_applies", "applies", "unavailable"]
# The model said something about the exclusions, in its own words.
ASSESSED = ("none_applies", "applies")


class Citation(WireModel):
    """A clause of the policy's wording that the decision rests on."""

    product: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    wording_version: Annotated[str, StringConstraints(min_length=1, max_length=16)]
    clause: Clause


def _route_of(reason: Reason) -> Route:
    if reason == "within_threshold":
        return "auto_approve"
    if reason == "missing_documents":
        return "request_documents"
    return "adjuster"


class TriageProposal(WireModel):
    route: Route
    reason: Reason
    recommendation: Literal["approve", "reject"] | None
    payable_amount: Euros | None
    exclusion_clause: Clause | None
    fraud_indicators: tuple[FraudIndicator, ...]
    missing_documents: tuple[Document, ...]
    citations: tuple[Citation, ...] = Field(max_length=MAX_CITATIONS)
    gaps: tuple[Gap, ...]
    assessment: AssessmentStatus
    rationale: Rationale | None
    drafted_by: DraftedBy | None

    @model_validator(mode="after")
    def _does_not_contradict_itself(self) -> Self:
        if self.route != _route_of(self.reason):
            raise ValueError(f"the reason {self.reason} does not give this route")
        if self.route == "auto_approve":
            self._check_auto_approval()
        if (self.reason == "excluded") != (self.exclusion_clause is not None):
            raise ValueError("a proposal is excluded exactly when it has a clause")
        if self.reason == "excluded" and self.recommendation != "reject":
            raise ValueError("an exclusion recommends rejecting")
        if self.reason == "missing_documents" and not self.missing_documents:
            raise ValueError("missing_documents needs a missing document")
        if self.reason == "unverified" and not self.gaps:
            raise ValueError("unverified needs a gap")
        self._check_model_call()
        return self

    def _check_auto_approval(self) -> None:
        conditions = {
            "a recommendation to approve": self.recommendation == "approve",
            "a payable amount within the limit": (
                self.payable_amount is not None
                and self.payable_amount <= AUTO_APPROVAL_LIMIT
            ),
            "no fraud indicator": not self.fraud_indicators,
            "no missing document": not self.missing_documents,
            "no gap": not self.gaps,
            "no exclusion": self.exclusion_clause is None,
            "an assessment that found no exclusion": self.assessment
            in ("not_needed", "none_applies"),
            "a citation": bool(self.citations),
        }
        unmet = [name for name, met in conditions.items() if not met]
        if unmet:
            raise ValueError(f"an automatic approval needs {', '.join(unmet)}")

    def _check_model_call(self) -> None:
        if (self.drafted_by is None) != (self.assessment == "not_needed"):
            raise ValueError(
                "drafted_by is empty exactly when no assessment was needed"
            )
        if self.rationale is not None and self.assessment not in ASSESSED:
            raise ValueError("a rationale needs an assessment the model answered")
