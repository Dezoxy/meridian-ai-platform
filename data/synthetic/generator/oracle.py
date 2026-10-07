"""The oracle: the expected outcome of a claim, derived from the rules.

A pure function of the claim, the policy, the claim history and the catalogue.
It reads the same JSON-shaped records the generator writes (dates as ISO
strings) and returns the exact ``expected-outcomes.json`` record. It knows
nothing about how the scenario builders made their data, so the builders are
checked against it and never the other way round.

Precedence, first match wins:

0. no policy has the claim's number (nothing else is decided);
1. the policy was not in force on the loss date;
2. the peril is not covered, or an exclusion applies;
3. a required document is missing;
4. a fraud indicator applies;
5. the payable amount is above the auto-approval limit;
6. otherwise the claim may be approved automatically.

Steps 4 to 6 cite the cover clause of the peril and 4.1, then 4.2 when the limit
caps the payable amount (the claimed amount is above the limit), then 5.1 when
the report was late.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from . import catalogue
from .catalogue import Exclusion, Product

Record = Mapping[str, Any]


def derive_outcome(
    claim: Record,
    policy: Record | None,
    history: Sequence[Record],
    circumstance: str | None = None,
) -> dict[str, Any]:
    """Return the expected-outcomes record for ``claim``.

    ``circumstance`` is the exclusion code the claim's facts fall under, if any.
    It is a fact behind the description and is never part of the claim record.
    ``policy`` is None when no policy has the claim's number: the claim goes to
    an adjuster with nothing else decided, as the triage rules decide it.
    Raises ValueError for records the rules cannot judge.
    """
    if _day(claim, "reported_on") < _day(claim, "loss_date"):
        raise ValueError(f"{claim['claim_id']}: reported before the loss")
    if policy is None:
        return _record(
            claim["claim_id"],
            None,
            [],
            route="adjuster",
            reason="policy_not_found",
            clauses=[],
        )
    case = _Case(
        claim=claim,
        policy=policy,
        product=_product_of(policy),
        indicators=fraud_indicators(claim, policy, history),
        circumstance=circumstance,
    )
    for step in (_inactive, _excluded, _incomplete):
        outcome = step(case)
        if outcome is not None:
            return outcome
    return _payable(case)


@dataclass(frozen=True)
class _Case:
    """One claim with what the steps need to judge it."""

    claim: Record
    policy: Record
    product: Product
    indicators: list[str]
    circumstance: str | None

    def outcome(self, **fields: Any) -> dict[str, Any]:
        return _record(self.claim["claim_id"], self.product, self.indicators, **fields)


def _inactive(case: _Case) -> dict[str, Any] | None:
    """Step 1: the policy was not in force on the loss date."""
    loss_date = _day(case.claim, "loss_date")
    lapsed = _lapse_applies(case.policy, loss_date)
    if not lapsed and _in_period(case.policy, loss_date):
        return None
    return case.outcome(
        route="adjuster",
        reason="policy_inactive",
        recommendation="reject",
        clauses=[catalogue.LAPSE_CLAUSE if lapsed else catalogue.PERIOD_CLAUSE],
    )


def _excluded(case: _Case) -> dict[str, Any] | None:
    """Step 2: the peril is not covered, or an exclusion applies."""
    exclusion = applicable_exclusion(
        case.product, case.claim["peril"], case.circumstance
    )
    if exclusion is None:
        return None
    return case.outcome(
        route="adjuster",
        reason="excluded",
        recommendation="reject",
        exclusion=exclusion.code,
        clauses=[catalogue.exclusion_clause(case.product, exclusion.code)],
    )


def _incomplete(case: _Case) -> dict[str, Any] | None:
    """Step 3: a required document is missing."""
    missing = missing_documents(case.claim)
    if not missing:
        return None
    return case.outcome(
        route="request_documents",
        reason="missing_documents",
        missing=missing,
        clauses=[catalogue.documents_clause(case.product, case.claim["peril"])],
    )


def _payable(case: _Case) -> dict[str, Any]:
    """Steps 4 to 6: covered and complete, so the claim has a payable amount."""
    payable = payable_amount(case.claim, case.policy)
    clauses = [
        catalogue.cover_clause(case.product, case.claim["peril"]),
        catalogue.DEDUCTIBLE_CLAUSE,
    ]
    if case.claim["claimed_amount"] > case.policy["limit"]:
        clauses.append(catalogue.LIMIT_CLAUSE)
    if "late_report" in case.indicators:
        clauses.append(catalogue.REPORTING_CLAUSE)
    if case.indicators:
        route, reason = "adjuster", "fraud_indicator"
    elif payable > catalogue.AUTO_APPROVAL_LIMIT:
        route, reason = "adjuster", "over_threshold"
    else:
        route, reason = "auto_approve", "within_threshold"
    return case.outcome(
        route=route,
        reason=reason,
        recommendation="approve",
        payable=payable,
        clauses=clauses,
    )


def payable_amount(claim: Record, policy: Record) -> int:
    """``min(claimed, limit) - deductible``; ValueError when nothing is payable."""
    payable = min(claim["claimed_amount"], policy["limit"]) - policy["deductible"]
    if payable <= 0:
        raise ValueError(
            f"{claim['claim_id']}: payable amount {payable} is not positive"
        )
    return payable


def fraud_indicators(
    claim: Record, policy: Record, history: Sequence[Record]
) -> list[str]:
    """Every indicator that applies, in catalogue order."""
    loss_date = _day(claim, "loss_date")
    found = []
    if 0 <= (loss_date - _day(policy, "start_date")).days <= catalogue.EARLY_LOSS_DAYS:
        found.append("early_loss")
    if _recent_claims(policy, history, loss_date) >= catalogue.FREQUENT_CLAIMS_COUNT:
        found.append("frequent_claims")
    delay = (_day(claim, "reported_on") - loss_date).days
    if delay > catalogue.REPORTING_WINDOW_DAYS:
        found.append("late_report")
    return found


def _recent_claims(policy: Record, history: Sequence[Record], loss_date: date) -> int:
    """History entries of this policy dated in the 365 days before the loss."""
    earliest = loss_date - timedelta(days=catalogue.FREQUENT_CLAIMS_WINDOW_DAYS)
    return sum(
        1
        for entry in history
        if entry["policy_number"] == policy["policy_number"]
        and earliest <= _day(entry, "loss_date") < loss_date
    )


def applicable_exclusion(
    product: Product, peril: str, circumstance: str | None
) -> Exclusion | None:
    """The exclusion that removes cover for this peril and circumstance, if any."""
    if circumstance is not None and circumstance not in catalogue.EXCLUSION_CODES:
        raise ValueError(f"unknown circumstance {circumstance!r}")
    if peril not in catalogue.LINE_PERILS[product.line]:
        raise ValueError(
            f"{product.code}: {peril} is not a peril of the {product.line} line"
        )
    for exclusion in product.exclusions:
        if exclusion.kind == catalogue.KIND_PERIL and peril in exclusion.perils:
            return exclusion
    for exclusion in product.exclusions:
        if (
            exclusion.kind == catalogue.KIND_CIRCUMSTANCE
            and exclusion.code == circumstance
            and peril in exclusion.perils
        ):
            return exclusion
    return None


def missing_documents(claim: Record) -> list[str]:
    """Required documents the claim did not provide, in catalogue order."""
    provided = claim["documents"]
    required = catalogue.REQUIRED_DOCUMENTS[claim["peril"]]
    return [document for document in required if document not in provided]


def _lapse_applies(policy: Record, loss_date: date) -> bool:
    if policy["status"] != "lapsed":
        return False
    if policy["lapsed_on"] is None:
        raise ValueError(f"{policy['policy_number']}: lapsed without a lapse date")
    return _day(policy, "lapsed_on") <= loss_date


def _in_period(policy: Record, loss_date: date) -> bool:
    return _day(policy, "start_date") <= loss_date <= _day(policy, "end_date")


def _product_of(policy: Record) -> Product:
    try:
        return catalogue.PRODUCTS[policy["product"]]
    except KeyError:
        raise ValueError(f"unknown product {policy['product']!r}") from None


def _day(record: Record, key: str) -> date:
    return date.fromisoformat(record[key])


def _record(
    claim_id: str,
    product: Product | None,
    indicators: list[str],
    *,
    route: str,
    reason: str,
    clauses: list[str],
    recommendation: str | None = None,
    payable: int | None = None,
    exclusion: str | None = None,
    missing: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "route": route,
        "reason": reason,
        "recommendation": recommendation,
        "payable_amount": payable,
        "exclusion": exclusion,
        "fraud_indicators": list(indicators),
        "missing_documents": list(missing or []),
        "citations": [
            {"wording": product.code, "clause": clause}
            for clause in clauses
            if product is not None
        ],
    }
