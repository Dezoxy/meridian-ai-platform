"""The triage rules: a claim's route, decided from facts.

Pure: no I/O. The model never decides a route (C-02, T-30). It supplies one
fact, the assessment of whether a circumstance exclusion applies, and these
rules do the rest. The rules are the workload's own; the synthetic generator's
oracle (``data/synthetic/generator/oracle.py``) is the tests' reference and is
never imported here.

A fact that is missing or uncertain can only stop an automatic approval. It
never stops a request for documents or a route to the adjuster.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType
from typing import Literal

from meridian.platform.common.wire import WireModel

from .models import ClaimFacts, Peril, Route
from .wording import Clause, Terms

# The catalogue's values (data/synthetic/generator/catalogue.py); a test keeps
# the two equal.
AUTO_APPROVAL_LIMIT = 2500  # payable euros; up to and including this may auto-approve
REPORTING_WINDOW_DAYS = 30
EARLY_LOSS_DAYS = 30
FREQUENT_CLAIMS_COUNT = 2
FREQUENT_CLAIMS_WINDOW_DAYS = 365

Document = Literal["police_report", "photos", "repair_estimate", "accident_statement"]
FraudIndicator = Literal["early_loss", "frequent_claims", "late_report"]
Reason = Literal[
    "within_threshold",
    "over_threshold",
    "fraud_indicator",
    "excluded",
    "policy_inactive",
    "missing_documents",
    "policy_not_found",
    "nothing_payable",
    "unverified",
]
Gap = Literal[
    "cover_clause",
    "exclusion_clauses",
    "deductible_clause",
    "limit_clause",
    "exclusion_assessment",
    "claim_history",
    # A clause the decision cites and the search did not return: the decision
    # stands, and the citation is not invented.
    "period_clause",
    "lapse_clause",
    "documents_clause",
    "reporting_clause",
]
PolicyState = Literal["in_force", "lapsed", "outside_period"]

# Per peril, in the catalogue's document order.
REQUIRED_DOCUMENTS: Mapping[Peril, tuple[Document, ...]] = MappingProxyType(
    {
        "collision": ("photos", "repair_estimate"),
        "theft": ("police_report",),
        "fire": ("photos",),
        "glass": ("photos",),
        "storm": ("photos",),
        "flood": ("photos",),
        "burst_pipe": ("photos", "repair_estimate"),
        "burglary": ("police_report", "photos"),
        "accidental_damage": ("photos",),
        "third_party_liability": ("accident_statement",),
    }
)


class PolicyRecord(WireModel):
    """The ``policy`` object of ``policy_lookup``'s answer."""

    policy_number: str
    product: str
    wording_version: str
    start_date: date
    end_date: date
    status: Literal["active", "lapsed"]
    lapsed_on: date | None = None
    deductible: int
    limit: int
    sum_insured: int | None = None


class HistoryEntry(WireModel):
    """One of ``claim_history``'s ``entries``."""

    history_id: str
    loss_date: date
    peril: str
    paid_amount: int
    status: str


@dataclass(frozen=True, slots=True)
class Assessment:
    """The model's one input: whether a circumstance exclusion applies.

    ``clause`` is set exactly when ``status`` is ``applies``."""

    status: Literal["not_needed", "none_applies", "applies", "unavailable"]
    clause: str | None = None

    def __post_init__(self) -> None:
        if (self.status == "applies") != (self.clause is not None):
            raise ValueError("an assessment has a clause exactly when it applies")


@dataclass(frozen=True, slots=True)
class Facts:
    """Everything the rules need. ``terms`` is None only when ``policy`` is."""

    claim: ClaimFacts
    policy: PolicyRecord | None
    history: tuple[HistoryEntry, ...]
    history_truncated: bool
    terms: Terms | None
    assessment: Assessment


@dataclass(frozen=True, slots=True)
class Decision:
    route: Route
    reason: Reason
    recommendation: Literal["approve", "reject"] | None
    payable_amount: int | None
    exclusion_clause: str | None
    fraud_indicators: tuple[FraudIndicator, ...]
    missing_documents: tuple[Document, ...]
    citations: tuple[str, ...]  # clause numbers, only clauses in the terms
    gaps: tuple[Gap, ...]


def policy_state(policy: PolicyRecord, loss_date: date) -> PolicyState:
    """A lapse on or before the loss date (or one without a date) makes the
    policy lapsed; a lapse after the loss leaves the period to decide."""
    if policy.status == "lapsed" and (
        policy.lapsed_on is None or policy.lapsed_on <= loss_date
    ):
        return "lapsed"
    if policy.start_date <= loss_date <= policy.end_date:
        return "in_force"
    return "outside_period"


def fraud_indicators(
    claim: ClaimFacts, policy: PolicyRecord, history: tuple[HistoryEntry, ...]
) -> tuple[FraudIndicator, ...]:
    """Every indicator that applies, in the catalogue's order. ``history`` is
    this policy's."""
    found: list[FraudIndicator] = []
    if 0 <= (claim.loss_date - policy.start_date).days <= EARLY_LOSS_DAYS:
        found.append("early_loss")
    earliest = claim.loss_date - timedelta(days=FREQUENT_CLAIMS_WINDOW_DAYS)
    recent = sum(1 for e in history if earliest <= e.loss_date < claim.loss_date)
    if recent >= FREQUENT_CLAIMS_COUNT:
        found.append("frequent_claims")
    if (claim.reported_on - claim.loss_date).days > REPORTING_WINDOW_DAYS:
        found.append("late_report")
    return tuple(found)


def missing_documents(claim: ClaimFacts) -> tuple[Document, ...]:
    """Required documents the claim did not provide, in the catalogue's order."""
    return tuple(
        document
        for document in REQUIRED_DOCUMENTS[claim.peril]
        if document not in claim.documents
    )


def payable_amount(claim: ClaimFacts, policy: PolicyRecord) -> int:
    """``min(claimed, limit) - deductible``; zero or negative when nothing is
    payable."""
    return min(claim.claimed_amount, policy.limit) - policy.deductible


def needs_assessment(
    claim: ClaimFacts, policy: PolicyRecord | None, terms: Terms | None
) -> bool:
    """Whether the model must say if a circumstance exclusion applies: the
    policy is in force, the cover clause was found, no peril exclusion was
    found and a circumstance exclusion is a candidate."""
    return (
        policy is not None
        and terms is not None
        and policy_state(policy, claim.loss_date) == "in_force"
        and terms.cover is not None
        and terms.peril_exclusion is None
        and len(terms.candidates) > 0
    )


def reads_exclusion_count(
    claim: ClaimFacts, policy: PolicyRecord | None, terms: Terms | None
) -> bool:
    """Whether ``decide`` reaches the gaps, where ``exclusions_complete`` is read:
    the policy is in force, no peril exclusion was found and the cover clause
    was. A claim decided before that (no policy, a lapsed or expired one, a peril
    the product does not cover, no cover clause) never reads it. The graph fails a
    run whose wording the table has no count for only when this is true (S067);
    a test keeps it equal to the paths of ``decide``."""
    return (
        policy is not None
        and terms is not None
        and policy_state(policy, claim.loss_date) == "in_force"
        and terms.peril_exclusion is None
        and terms.cover is not None
    )


def _cite(*clauses: Clause | None) -> tuple[str, ...]:
    """The numbers of the clauses that were retrieved; none is invented."""
    return tuple(c.clause for c in clauses if c is not None)


def _gaps(facts: Facts, policy: PolicyRecord, terms: Terms) -> tuple[Gap, ...]:
    claim = facts.claim
    assessment = facts.assessment.status
    above_limit = claim.claimed_amount > policy.limit
    candidates: list[tuple[bool, Gap]] = [
        (not terms.exclusions_complete, "exclusion_clauses"),
        (terms.deductible is None, "deductible_clause"),
        (above_limit and terms.limit is None, "limit_clause"),
        (
            assessment == "unavailable"
            or (assessment == "not_needed" and needs_assessment(claim, policy, terms)),
            "exclusion_assessment",
        ),
        (facts.history_truncated, "claim_history"),
    ]
    return tuple(gap for present, gap in candidates if present)


def _decision(
    route: Route,
    reason: Reason,
    indicators: tuple[FraudIndicator, ...] = (),
    *,
    recommendation: Literal["approve", "reject"] | None = None,
    payable: int | None = None,
    exclusion: str | None = None,
    missing: tuple[Document, ...] = (),
    citations: tuple[str, ...] = (),
    gaps: tuple[Gap, ...] = (),
) -> Decision:
    return Decision(
        route=route,
        reason=reason,
        recommendation=recommendation,
        payable_amount=payable,
        exclusion_clause=exclusion,
        fraud_indicators=indicators,
        missing_documents=missing,
        citations=citations,
        gaps=gaps,
    )


def _applying_exclusion(assessment: Assessment, terms: Terms) -> str | None:
    """The clause the assessment says applies; a clause that is not one of the
    candidates is a bug in the caller. The message holds no value: the clause
    came from the model."""
    if assessment.status != "applies":
        return None
    if assessment.clause not in [c.clause for c in terms.candidates]:
        raise ValueError("the assessed clause is not one of the candidate exclusions")
    return assessment.clause


def decide(facts: Facts) -> Decision:
    """The decision for ``facts``: the first rule that matches wins."""
    policy, terms, claim = facts.policy, facts.terms, facts.claim
    if policy is None:
        return _decision("adjuster", "policy_not_found")
    if terms is None:
        raise ValueError("a policy was found but its terms were not given")
    indicators = fraud_indicators(claim, policy, facts.history)

    state = policy_state(policy, claim.loss_date)
    if state != "in_force":
        clause, gap = (
            (terms.lapse, "lapse_clause")
            if state == "lapsed"
            else (terms.period, "period_clause")
        )
        return _decision(
            "adjuster",
            "policy_inactive",
            indicators,
            recommendation="reject",
            citations=_cite(clause),
            gaps=() if clause is not None else (gap,),
        )
    peril_exclusion = terms.peril_exclusion
    if peril_exclusion is not None:
        return _decision(
            "adjuster",
            "excluded",
            indicators,
            recommendation="reject",
            exclusion=peril_exclusion.clause,
            citations=_cite(peril_exclusion),
        )
    if terms.cover is None:
        return _decision("adjuster", "unverified", indicators, gaps=("cover_clause",))

    gaps = _gaps(facts, policy, terms)
    applying = _applying_exclusion(facts.assessment, terms)
    if applying is not None:
        return _decision(
            "adjuster",
            "excluded",
            indicators,
            recommendation="reject",
            exclusion=applying,
            citations=(applying,),
            gaps=gaps,
        )
    missing = missing_documents(claim)
    if missing:
        return _decision(
            "request_documents",
            "missing_documents",
            indicators,
            missing=missing,
            citations=_cite(terms.documents),
            gaps=gaps if terms.documents is not None else (*gaps, "documents_clause"),
        )
    payable = payable_amount(claim, policy)
    if payable <= 0:
        return _decision(
            "adjuster",
            "nothing_payable",
            indicators,
            citations=_cite(terms.cover, terms.deductible),
            gaps=gaps,
        )
    return _payable_decision(facts, policy, terms, indicators, payable, gaps)


def _payable_decision(
    facts: Facts,
    policy: PolicyRecord,
    terms: Terms,
    indicators: tuple[FraudIndicator, ...],
    payable: int,
    gaps: tuple[Gap, ...],
) -> Decision:
    """Step 8: a covered claim with a positive payable amount. The rules
    recommend approving only when nothing is missing: a claim whose exclusions
    nobody checked keeps its route and reason but has no recommendation."""
    if "late_report" in indicators and terms.reporting is None:
        gaps = (*gaps, "reporting_clause")
    citations = _cite(
        terms.cover,
        terms.deductible,
        terms.limit if facts.claim.claimed_amount > policy.limit else None,
        terms.reporting if "late_report" in indicators else None,
    )

    def decision(route: Route, reason: Reason) -> Decision:
        return _decision(
            route,
            reason,
            indicators,
            recommendation=None if gaps else "approve",
            payable=payable,
            citations=citations,
            gaps=gaps,
        )

    if indicators:
        return decision("adjuster", "fraud_indicator")
    if payable > AUTO_APPROVAL_LIMIT:
        return decision("adjuster", "over_threshold")
    if gaps:
        return decision("adjuster", "unverified")
    return decision("auto_approve", "within_threshold")
