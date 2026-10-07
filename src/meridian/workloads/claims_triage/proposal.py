"""The triage proposal: what the rules decided, with the model's one fact.

The graph writes it to ``output`` and the Claims API validates and stores it.
The validator refuses a proposal that contradicts itself, so a bug in the
graph is a failed run, not a stored decision (T-28, C-02): the route follows
from the reason, and an automatic approval needs every condition the rules
need.
"""

from typing import Annotated, Final, Literal, Self

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
# Why the assessment is unavailable: one word, never the model's text. The last
# three are the guardrails' (S047): the claimant's text holds special-category
# data or addresses the model, or the provider's content filter refused it.
UnavailableBecause = Literal[
    "truncated",
    "not-json",
    "not-the-format",
    "unknown-clause",
    "unsure",
    "too-long",
    "special-data",
    "injection-suspected",
    "filtered",
]
# The one word the screen and the assessment share; a test pins it to the Literal.
INJECTION_SUSPECTED: Final = "injection-suspected"
# The model said something about the exclusions, in its own words.
ASSESSED = ("none_applies", "applies")
RestsOn = Literal["model", "rules", "neither"]
# What the adjuster's pages say of a recommendation, in one place (S070): the
# sentence beside it on the claim's page and the marker in the queue's column.
# Fixed words, no stored value; neither names the claimant or the prompt.
RESTS_ON_NOTES: Final = {
    "model": (
        "This recommendation rests on a model's reading of the policy's "
        "exclusion clauses and is not the rules' alone; check it against the "
        "policy's wording."
    ),
    "rules": "The rules decided this recommendation; no model was asked.",
}
RESTS_ON_MARKS: Final = {"model": "model reading", "rules": "rules only"}


def recommendation_rests_on(
    recommendation: str | None, assessment: str | None
) -> RestsOn:
    """What a stored recommendation rests on, from the two fields that say so:
    ``model`` when the model answered about the exclusions, ``rules`` when no
    assessment was needed, ``neither`` when there is no recommendation to mark
    or the assessment was unavailable. Any other value, such as a field a row
    stored before this change lacks, is ``neither``: it never raises."""
    if recommendation not in ("approve", "reject"):
        return "neither"
    if assessment in ASSESSED:
        return "model"
    return "rules" if assessment == "not_needed" else "neither"


class Citation(WireModel):
    """A clause of the policy's wording that the decision rests on."""

    product: Annotated[str, StringConstraints(min_length=1, max_length=32), NoNul]
    wording_version: Annotated[
        str, StringConstraints(min_length=1, max_length=16), NoNul
    ]
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
    unavailable_because: UnavailableBecause | None
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
        # An unavailable assessment may follow a call or not: a user message
        # over the gateway's limit is never sent, a guardrail stops the call
        # and a filtered one gets no answer. A filtered one that was a refused
        # prompt has no drafter; one whose completion the filter withheld has,
        # as the provider ran (S069).
        if self.assessment == "not_needed" and self.drafted_by is not None:
            raise ValueError("drafted_by is empty when no assessment was needed")
        if self.assessment in ASSESSED and self.drafted_by is None:
            raise ValueError("an assessment the model answered needs a drafted_by")
        if (self.unavailable_because is not None) != (self.assessment == "unavailable"):
            raise ValueError(
                "unavailable_because is set exactly when the assessment is unavailable"
            )
        if self.rationale is not None and self.assessment not in ASSESSED:
            raise ValueError("a rationale needs an assessment the model answered")
