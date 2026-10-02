"""The triage rules (S014): pure, no database.

The first group runs the rules over the 40 claims of the synthetic golden set
and compares them with the generator's oracle, whose answers sit in
``expected-outcomes.json``. The oracle is the reference and the module never
imports it. The second group pins what a missing or wrong model assessment
costs. The third and fourth feed hand-made facts to each step and gap.
"""

import json
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from generator import catalogue
from servicesupport import REPO_ROOT

from meridian.platform.knowledge_mcp.chunking import parse_wording
from meridian.workloads.claims_triage import rules
from meridian.workloads.claims_triage.models import ClaimFacts, Peril, Route
from meridian.workloads.claims_triage.rules import (
    Assessment,
    Decision,
    Facts,
    HistoryEntry,
    PolicyRecord,
    decide,
    fraud_indicators,
    missing_documents,
    needs_assessment,
    payable_amount,
    policy_state,
)
from meridian.workloads.claims_triage.wording import Clause, Terms, select_terms

SYNTHETIC = REPO_ROOT / "data" / "synthetic"

# -- the golden set -----------------------------------------------------------


def load(name: str) -> Any:
    return json.loads((SYNTHETIC / name).read_text(encoding="utf-8"))


CLAIMS: list[dict[str, Any]] = load("claims.json")
POLICIES: dict[str, dict[str, Any]] = {
    p["policy_number"]: p for p in load("policies.json")
}
HISTORY: list[dict[str, Any]] = load("claim-history.json")
EXPECTED: dict[str, dict[str, Any]] = {
    e["claim_id"]: e for e in load("expected-outcomes.json")
}
CLAIM_IDS = [claim["claim_id"] for claim in CLAIMS]


def wording_chunks(product_code: str) -> list[dict[str, Any]]:
    path: Path = SYNTHETIC / "wordings" / f"{product_code}.md"
    return [
        {
            "clause": chunk.clause,
            "section": chunk.section,
            "title": chunk.title,
            "body": chunk.body,
            "keyword_match": False,
        }
        for chunk in parse_wording(path.read_text(encoding="utf-8")).chunks
    ]


def golden_facts(claim_id: str) -> tuple[Facts, dict[str, Any]]:
    """The facts of a golden claim, with the assessment ``not_needed``."""
    record = next(c for c in CLAIMS if c["claim_id"] == claim_id)
    claim = ClaimFacts.model_validate(
        {key: value for key, value in record.items() if key != "claimant"}
    )
    policy_record = POLICIES.get(record["policy_number"])
    if policy_record is None:
        return Facts(claim, None, (), False, None, Assessment("not_needed")), record
    policy = PolicyRecord.model_validate(
        {key: policy_record[key] for key in PolicyRecord.model_fields}
    )
    history = tuple(
        HistoryEntry.model_validate(
            {k: v for k, v in entry.items() if k != "policy_number"}
        )
        for entry in HISTORY
        if entry["policy_number"] == policy.policy_number
    )
    terms = select_terms(claim.peril, wording_chunks(policy.product))
    facts = Facts(claim, policy, history, False, terms, Assessment("not_needed"))
    return facts, record


def circumstance_clause(facts: Facts, expected: dict[str, Any]) -> str | None:
    """The clause of the expected exclusion when it is a circumstance one."""
    code = expected["exclusion"]
    assert facts.policy is not None
    product = catalogue.PRODUCTS[facts.policy.product]
    if code is None:
        return None
    exclusion = next(e for e in product.exclusions if e.code == code)
    if exclusion.kind != catalogue.KIND_CIRCUMSTANCE:
        return None
    return catalogue.exclusion_clause(product, code)


def facts_with_assessment(claim_id: str, *, unavailable: bool) -> Facts:
    facts, _ = golden_facts(claim_id)
    if facts.policy is None:
        return facts
    needed = needs_assessment(facts.claim, facts.policy, facts.terms)
    if unavailable:
        status = "unavailable" if needed else "not_needed"
        return replace(facts, assessment=Assessment(status))
    clause = circumstance_clause(facts, EXPECTED[claim_id])
    if clause is not None:
        return replace(facts, assessment=Assessment("applies", clause))
    status = "none_applies" if needed else "not_needed"
    return replace(facts, assessment=Assessment(status))


@pytest.mark.parametrize("claim_id", CLAIM_IDS)
def test_the_rules_reproduce_the_oracle_on_every_golden_claim(claim_id: str) -> None:
    facts = facts_with_assessment(claim_id, unavailable=False)
    expected = EXPECTED[claim_id]
    assert facts.policy is not None

    decision = decide(facts)

    assert (
        decision.route,
        decision.reason,
        decision.recommendation,
        decision.payable_amount,
    ) == (
        expected["route"],
        expected["reason"],
        expected["recommendation"],
        expected["payable_amount"],
    )
    assert list(decision.fraud_indicators) == expected["fraud_indicators"]
    assert list(decision.missing_documents) == expected["missing_documents"]
    assert decision.exclusion_clause == (
        catalogue.exclusion_clause(
            catalogue.PRODUCTS[facts.policy.product], expected["exclusion"]
        )
        if expected["exclusion"]
        else None
    )
    assert list(decision.citations) == [c["clause"] for c in expected["citations"]]
    assert {c["wording"] for c in expected["citations"]} <= {facts.policy.product}
    assert decision.gaps == ()


def test_the_golden_set_has_forty_claims_and_an_outcome_for_each() -> None:
    assert len(CLAIM_IDS) == 40
    assert set(CLAIM_IDS) == set(EXPECTED)


def assessment_needed(claim_id: str) -> bool:
    facts, _ = golden_facts(claim_id)
    return needs_assessment(facts.claim, facts.policy, facts.terms)


# What the 40 claims come to when the assessment is unavailable wherever it was
# needed: the 7 within_threshold claims that needed it are unverified, and the 3
# circumstance exclusions the model would have found are not seen, so those
# claims go on to the later steps.
COUNTS_WITHOUT_ASSESSMENT = {
    "excluded": 3,
    "fraud_indicator": 6,
    "missing_documents": 6,
    "over_threshold": 7,
    "policy_inactive": 6,
    "unverified": 7,
    "within_threshold": 5,
}


def test_no_claim_that_needed_the_assessment_is_approved_when_it_is_unavailable() -> (
    None
):
    decisions = {
        claim_id: decide(facts_with_assessment(claim_id, unavailable=True))
        for claim_id in CLAIM_IDS
    }

    reasons = Counter(decision.reason for decision in decisions.values())
    print("reasons with the assessment unavailable:", dict(sorted(reasons.items())))

    approved = {c for c, d in decisions.items() if d.route == "auto_approve"}
    assert not {c for c in approved if assessment_needed(c)}
    assert approved == {"CLM-0005", "CLM-0010", "CLM-0016", "CLM-0019", "CLM-0021"}
    assert dict(sorted(reasons.items())) == COUNTS_WITHOUT_ASSESSMENT


@pytest.mark.parametrize("claim_id", CLAIM_IDS)
def test_a_missing_assessment_only_stops_an_automatic_approval(claim_id: str) -> None:
    golden = decide(facts_with_assessment(claim_id, unavailable=False))
    without = decide(facts_with_assessment(claim_id, unavailable=True))
    needed = assessment_needed(claim_id)

    if not needed:
        assert without == golden
    elif golden.reason == "within_threshold":
        assert (without.route, without.reason) == ("adjuster", "unverified")
        assert without.recommendation is None
        assert without.gaps == ("exclusion_assessment",)
    elif golden.reason == "excluded":
        assert without.reason != "excluded"
        assert without.exclusion_clause is None
    else:
        assert without.route == golden.route
        assert without.reason == golden.reason


@pytest.mark.parametrize("claim_id", ["CLM-0026", "CLM-0031", "CLM-0037", "CLM-0038"])
def test_a_wrong_none_applies_turns_an_excluded_claim_into_an_approval(
    claim_id: str,
) -> None:
    expected = EXPECTED[claim_id]
    facts = facts_with_assessment(claim_id, unavailable=False)
    assert expected["reason"] == "excluded"
    assert facts.assessment.status == "applies"

    wrong = decide(replace(facts, assessment=Assessment("none_applies")))

    assert (wrong.route, wrong.reason) == ("auto_approve", "within_threshold")
    assert wrong.exclusion_clause is None


# -- constants ------------------------------------------------------------------


def test_the_constants_equal_the_catalogues() -> None:
    assert rules.AUTO_APPROVAL_LIMIT == catalogue.AUTO_APPROVAL_LIMIT
    assert rules.REPORTING_WINDOW_DAYS == catalogue.REPORTING_WINDOW_DAYS
    assert rules.EARLY_LOSS_DAYS == catalogue.EARLY_LOSS_DAYS
    assert rules.FREQUENT_CLAIMS_COUNT == catalogue.FREQUENT_CLAIMS_COUNT
    assert rules.FREQUENT_CLAIMS_WINDOW_DAYS == catalogue.FREQUENT_CLAIMS_WINDOW_DAYS
    assert dict(rules.REQUIRED_DOCUMENTS) == catalogue.REQUIRED_DOCUMENTS


def test_the_vocabularies_cover_the_catalogues() -> None:
    assert set(get_args(rules.FraudIndicator)) == set(catalogue.FRAUD_INDICATORS)
    assert set(get_args(rules.Document)) == set(catalogue.DOCUMENT_ORDER)
    assert set(catalogue.REASONS) <= set(get_args(rules.Reason))
    assert set(get_args(rules.Reason)) - set(catalogue.REASONS) == {
        "policy_not_found",
        "nothing_payable",
        "unverified",
    }
    assert set(get_args(Route)) == set(catalogue.ROUTES)
    assert set(rules.REQUIRED_DOCUMENTS) == set(get_args(Peril))


# -- hand-made facts ------------------------------------------------------------

LOSS = date(2026, 6, 5)


def make_claim(**changes: Any) -> ClaimFacts:
    record: dict[str, Any] = {
        "claim_id": "CLM-0001",
        "policy_number": "POL-0001",
        "reported_on": LOSS + timedelta(days=5),
        "loss_date": LOSS,
        "peril": "storm",
        "claimed_amount": 2000,
        "loss_location": {"city": "Linz", "country": "AT"},
        "description": "A storm took part of the roof.",
        "documents": ("photos",),
    }
    return ClaimFacts.model_validate({**record, **changes})


def make_policy(**changes: Any) -> PolicyRecord:
    record: dict[str, Any] = {
        "policy_number": "POL-0001",
        "product": "HOME-STD",
        "wording_version": "2026-01",
        "start_date": date(2025, 8, 18),
        "end_date": date(2026, 8, 17),
        "status": "active",
        "lapsed_on": None,
        "deductible": 250,
        "limit": 360_000,
        "sum_insured": 360_000,
    }
    return PolicyRecord.model_validate({**record, **changes})


def clause(number: str, title: str = "Title") -> Clause:
    return Clause(number, title, "Text.")


def make_terms(**changes: Any) -> Terms:
    fields: dict[str, Any] = {
        "cover": clause("2.2", "Storm"),
        "documents": clause("5.3", "Documents for storm"),
        "peril_exclusion": None,
        "candidates": (),
        "exclusions_complete": True,
        "deductible": clause("4.1", "Deductible"),
        "limit": clause("4.2", "Limit"),
        "reporting": clause("5.1", "Reporting a claim"),
        "period": clause("6.1", "Period of cover"),
        "lapse": clause("6.2", "Lapse for non-payment"),
    }
    return Terms(**{**fields, **changes})


def make_facts(**changes: Any) -> Facts:
    fields: dict[str, Any] = {
        "claim": make_claim(),
        "policy": make_policy(),
        "history": (),
        "history_truncated": False,
        "terms": make_terms(),
        "assessment": Assessment("not_needed"),
    }
    return Facts(**{**fields, **changes})


def entry(days_before_loss: int, history_id: str = "HIST-0001") -> HistoryEntry:
    return HistoryEntry(
        history_id=history_id,
        loss_date=LOSS - timedelta(days=days_before_loss),
        peril="storm",
        paid_amount=900,
        status="closed",
    )


WEAR = clause("3.3", "Wear and tear")


# -- policy_state ---------------------------------------------------------------


def test_an_active_policy_is_in_force_from_its_start_to_its_end_inclusive() -> None:
    policy = make_policy()

    assert policy_state(policy, policy.start_date) == "in_force"
    assert policy_state(policy, policy.end_date) == "in_force"
    assert policy_state(policy, policy.start_date - timedelta(days=1)) == (
        "outside_period"
    )
    assert policy_state(policy, policy.end_date + timedelta(days=1)) == (
        "outside_period"
    )


def test_a_lapse_on_or_before_the_loss_date_is_lapsed() -> None:
    policy = make_policy(status="lapsed", lapsed_on=LOSS)

    assert policy_state(policy, LOSS) == "lapsed"
    assert policy_state(policy, LOSS + timedelta(days=1)) == "lapsed"


def test_a_lapse_after_the_loss_date_is_judged_by_the_period() -> None:
    policy = make_policy(status="lapsed", lapsed_on=LOSS + timedelta(days=1))

    assert policy_state(policy, LOSS) == "in_force"
    assert policy_state(policy, date(2025, 8, 1)) == "outside_period"


def test_a_lapsed_policy_without_a_lapse_date_is_lapsed() -> None:
    policy = make_policy(status="lapsed", lapsed_on=None)

    assert policy_state(policy, LOSS) == "lapsed"


def test_a_lapse_before_the_period_still_makes_a_later_loss_lapsed() -> None:
    policy = make_policy(status="lapsed", lapsed_on=date(2025, 9, 1))

    assert policy_state(policy, date(2025, 8, 1)) == "outside_period"
    assert policy_state(policy, LOSS) == "lapsed"


def test_a_lapse_date_on_an_active_policy_is_ignored() -> None:
    policy = make_policy(status="active", lapsed_on=date(2025, 9, 1))

    assert policy_state(policy, LOSS) == "in_force"


# -- fraud_indicators ---------------------------------------------------------


@pytest.mark.parametrize(
    ("days_after_start", "expected"),
    [
        (-1, ()),
        (0, ("early_loss",)),
        (30, ("early_loss",)),
        (31, ()),
    ],
)
def test_early_loss_is_a_loss_in_the_first_thirty_days_of_the_policy(
    days_after_start: int, expected: tuple[str, ...]
) -> None:
    policy = make_policy(start_date=LOSS - timedelta(days=days_after_start))
    claim = make_claim(reported_on=LOSS)

    assert fraud_indicators(claim, policy, ()) == expected


@pytest.mark.parametrize(
    ("days_before", "expected"),
    [
        ([0], ()),
        ([1], ()),
        ([1, 0], ()),
        ([1, 365], ("frequent_claims",)),
        ([1, 366], ()),
        ([365, 366, 400], ()),
        ([1, 2, 3], ("frequent_claims",)),
    ],
)
def test_frequent_claims_are_two_entries_in_the_365_days_before_the_loss(
    days_before: list[int], expected: tuple[str, ...]
) -> None:
    history = tuple(entry(d, f"HIST-{i:04d}") for i, d in enumerate(days_before))

    assert fraud_indicators(make_claim(reported_on=LOSS), make_policy(), history) == (
        expected
    )


@pytest.mark.parametrize(
    ("delay_days", "expected"),
    [(0, ()), (30, ()), (31, ("late_report",))],
)
def test_a_late_report_is_one_made_more_than_thirty_days_after_the_loss(
    delay_days: int, expected: tuple[str, ...]
) -> None:
    claim = make_claim(reported_on=LOSS + timedelta(days=delay_days))

    assert fraud_indicators(claim, make_policy(), ()) == expected


def test_indicators_come_in_the_catalogues_order() -> None:
    policy = make_policy(start_date=LOSS - timedelta(days=3))
    claim = make_claim(reported_on=LOSS + timedelta(days=40))

    assert fraud_indicators(claim, policy, (entry(1), entry(2, "HIST-0002"))) == (
        "early_loss",
        "frequent_claims",
        "late_report",
    )


# -- missing_documents and payable_amount ---------------------------------------


def test_missing_documents_follow_the_catalogue_order_and_ignore_extras() -> None:
    claim = make_claim(peril="collision", documents=("police_report",))
    assert missing_documents(claim) == ("photos", "repair_estimate")

    claim = make_claim(peril="collision", documents=("repair_estimate", "other"))
    assert missing_documents(claim) == ("photos",)

    claim = make_claim(peril="burglary", documents=("photos", "police_report"))
    assert missing_documents(claim) == ()


@pytest.mark.parametrize(
    ("claimed", "limit", "deductible", "expected"),
    [
        (2000, 360_000, 250, 1750),
        (5000, 3000, 250, 2750),
        (3000, 3000, 250, 2750),
        (250, 360_000, 250, 0),
        (100, 360_000, 250, -150),
    ],
)
def test_the_payable_amount_is_the_capped_claim_less_the_deductible(
    claimed: int, limit: int, deductible: int, expected: int
) -> None:
    claim = make_claim(claimed_amount=claimed)
    policy = make_policy(limit=limit, deductible=deductible)

    assert payable_amount(claim, policy) == expected


# -- needs_assessment -----------------------------------------------------------


def test_an_assessment_is_needed_for_a_covered_peril_in_force_with_a_candidate() -> (
    None
):
    terms = make_terms(candidates=(WEAR,))

    assert needs_assessment(make_claim(), make_policy(), terms) is True


@pytest.mark.parametrize(
    ("policy", "terms"),
    [
        (None, None),
        (make_policy(status="lapsed", lapsed_on=LOSS), make_terms(candidates=(WEAR,))),
        (make_policy(end_date=date(2026, 1, 1)), make_terms(candidates=(WEAR,))),
        (make_policy(), make_terms(candidates=(WEAR,), cover=None)),
        (make_policy(), make_terms(candidates=(WEAR,), peril_exclusion=clause("3.1"))),
        (make_policy(), make_terms(candidates=())),
    ],
)
def test_no_assessment_is_needed_otherwise(
    policy: PolicyRecord | None, terms: Terms | None
) -> None:
    assert needs_assessment(make_claim(), policy, terms) is False


# -- decide: one test per step ----------------------------------------------------


def test_step_1_no_policy_goes_to_the_adjuster_with_nothing_else() -> None:
    facts = make_facts(policy=None, terms=None, history=(entry(1),))

    assert decide(facts) == Decision(
        route="adjuster",
        reason="policy_not_found",
        recommendation=None,
        payable_amount=None,
        exclusion_clause=None,
        fraud_indicators=(),
        missing_documents=(),
        citations=(),
        gaps=(),
    )


def test_a_policy_without_terms_is_a_bug_of_the_caller() -> None:
    with pytest.raises(ValueError, match="terms"):
        decide(make_facts(terms=None))


def test_step_2_a_lapsed_policy_is_rejected_citing_the_lapse_clause() -> None:
    policy = make_policy(status="lapsed", lapsed_on=LOSS - timedelta(days=10))

    decision = decide(make_facts(policy=policy))

    assert decision == Decision(
        route="adjuster",
        reason="policy_inactive",
        recommendation="reject",
        payable_amount=None,
        exclusion_clause=None,
        fraud_indicators=(),
        missing_documents=(),
        citations=("6.2",),
        gaps=(),
    )


def test_step_2_a_lapsed_policy_without_a_lapse_date_cites_the_lapse_clause() -> None:
    policy = make_policy(status="lapsed", lapsed_on=None)

    decision = decide(make_facts(policy=policy))

    assert (decision.reason, decision.citations) == ("policy_inactive", ("6.2",))


def test_step_2_a_loss_outside_the_period_cites_the_period_clause() -> None:
    policy = make_policy(start_date=LOSS + timedelta(days=1))

    decision = decide(make_facts(policy=policy))

    assert (decision.reason, decision.recommendation, decision.citations) == (
        "policy_inactive",
        "reject",
        ("6.1",),
    )


def test_step_2_a_lapse_after_the_loss_does_not_stop_the_claim() -> None:
    policy = make_policy(status="lapsed", lapsed_on=LOSS + timedelta(days=20))

    decision = decide(make_facts(policy=policy))

    assert (decision.route, decision.reason) == ("auto_approve", "within_threshold")


def test_step_2_leaves_out_a_clause_that_was_not_retrieved() -> None:
    policy = make_policy(status="lapsed", lapsed_on=LOSS)

    decision = decide(make_facts(policy=policy, terms=make_terms(lapse=None)))

    assert decision.citations == ()


def test_step_2_fills_the_fraud_indicators_and_lists_no_gaps() -> None:
    policy = make_policy(
        status="lapsed", lapsed_on=LOSS, start_date=LOSS - timedelta(days=5)
    )
    facts = make_facts(
        policy=policy,
        terms=make_terms(exclusions_complete=False),
        history_truncated=True,
    )

    decision = decide(facts)

    assert decision.fraud_indicators == ("early_loss",)
    assert decision.gaps == ()


def test_step_3_a_peril_exclusion_is_rejected_citing_its_clause() -> None:
    terms = make_terms(cover=None, peril_exclusion=clause("3.1", "Flood"))

    decision = decide(make_facts(policy=make_policy(), terms=terms))

    assert decision == Decision(
        route="adjuster",
        reason="excluded",
        recommendation="reject",
        payable_amount=None,
        exclusion_clause="3.1",
        fraud_indicators=(),
        missing_documents=(),
        citations=("3.1",),
        gaps=(),
    )


def test_step_3_comes_before_step_4_and_lists_no_gaps() -> None:
    terms = make_terms(
        cover=None, peril_exclusion=clause("3.1"), exclusions_complete=False
    )

    decision = decide(make_facts(terms=terms, history_truncated=True))

    assert (decision.reason, decision.gaps) == ("excluded", ())


def test_step_4_no_cover_clause_is_unverified_with_its_own_gap() -> None:
    facts = make_facts(
        terms=make_terms(cover=None, exclusions_complete=False),
        history_truncated=True,
    )

    decision = decide(facts)

    assert decision == Decision(
        route="adjuster",
        reason="unverified",
        recommendation=None,
        payable_amount=None,
        exclusion_clause=None,
        fraud_indicators=(),
        missing_documents=(),
        citations=(),
        gaps=("cover_clause",),
    )


def test_step_4_fills_the_fraud_indicators() -> None:
    claim = make_claim(reported_on=LOSS + timedelta(days=45))

    decision = decide(make_facts(claim=claim, terms=make_terms(cover=None)))

    assert decision.fraud_indicators == ("late_report",)


def test_step_5_an_applying_exclusion_is_rejected_citing_its_clause() -> None:
    facts = make_facts(
        terms=make_terms(candidates=(clause("3.2"), WEAR)),
        assessment=Assessment("applies", "3.3"),
    )

    assert decide(facts) == Decision(
        route="adjuster",
        reason="excluded",
        recommendation="reject",
        payable_amount=None,
        exclusion_clause="3.3",
        fraud_indicators=(),
        missing_documents=(),
        citations=("3.3",),
        gaps=(),
    )


def test_step_5_a_clause_outside_the_candidates_is_a_value_error() -> None:
    facts = make_facts(
        terms=make_terms(candidates=(WEAR,)),
        assessment=Assessment("applies", "3.4"),
    )

    with pytest.raises(ValueError, match=r"3\.4"):
        decide(facts)


def test_step_5_an_applying_clause_without_any_candidate_is_a_value_error() -> None:
    facts = make_facts(assessment=Assessment("applies", "3.3"))

    with pytest.raises(ValueError, match=r"3\.3"):
        decide(facts)


def test_step_5_lists_the_gaps_of_the_decision() -> None:
    facts = make_facts(
        terms=make_terms(candidates=(WEAR,), exclusions_complete=False),
        assessment=Assessment("applies", "3.3"),
        history_truncated=True,
    )

    decision = decide(facts)

    assert decision.reason == "excluded"
    assert decision.gaps == ("exclusion_clauses", "claim_history")


def test_an_assessment_is_not_read_when_an_earlier_step_decides() -> None:
    lapsed = make_policy(status="lapsed", lapsed_on=LOSS)
    bogus = Assessment("applies", "3.9")

    assert decide(make_facts(policy=lapsed, assessment=bogus)).reason == (
        "policy_inactive"
    )
    peril = make_terms(peril_exclusion=clause("3.1"))
    assert decide(make_facts(terms=peril, assessment=bogus)).reason == "excluded"
    no_cover = make_terms(cover=None)
    assert decide(make_facts(terms=no_cover, assessment=bogus)).reason == "unverified"


def test_step_6_missing_documents_request_them_citing_the_documents_clause() -> None:
    claim = make_claim(peril="collision", documents=())
    terms = make_terms(documents=clause("5.2", "Documents for collision"))

    decision = decide(make_facts(claim=claim, terms=terms))

    assert decision == Decision(
        route="request_documents",
        reason="missing_documents",
        recommendation=None,
        payable_amount=None,
        exclusion_clause=None,
        fraud_indicators=(),
        missing_documents=("photos", "repair_estimate"),
        citations=("5.2",),
        gaps=(),
    )


def test_step_6_leaves_out_a_documents_clause_that_was_not_retrieved() -> None:
    claim = make_claim(documents=())

    decision = decide(make_facts(claim=claim, terms=make_terms(documents=None)))

    assert (decision.reason, decision.citations) == ("missing_documents", ())


def test_step_6_still_requests_documents_when_facts_are_missing() -> None:
    claim = make_claim(documents=())
    facts = make_facts(
        claim=claim,
        terms=make_terms(candidates=(WEAR,), exclusions_complete=False),
        assessment=Assessment("unavailable"),
        history_truncated=True,
    )

    decision = decide(facts)

    assert decision.route == "request_documents"
    assert decision.gaps == (
        "exclusion_clauses",
        "exclusion_assessment",
        "claim_history",
    )


def test_step_6_comes_before_a_payable_amount_that_is_not_positive() -> None:
    claim = make_claim(documents=(), claimed_amount=100)

    assert decide(make_facts(claim=claim)).reason == "missing_documents"


@pytest.mark.parametrize("claimed", [250, 100])
def test_step_7_nothing_payable_goes_to_the_adjuster_without_a_recommendation(
    claimed: int,
) -> None:
    decision = decide(make_facts(claim=make_claim(claimed_amount=claimed)))

    assert decision == Decision(
        route="adjuster",
        reason="nothing_payable",
        recommendation=None,
        payable_amount=None,
        exclusion_clause=None,
        fraud_indicators=(),
        missing_documents=(),
        citations=("2.2", "4.1"),
        gaps=(),
    )


def test_step_7_still_lists_the_gaps() -> None:
    facts = make_facts(
        claim=make_claim(claimed_amount=250),
        terms=make_terms(deductible=None),
        history_truncated=True,
    )

    decision = decide(facts)

    assert decision.citations == ("2.2",)
    assert decision.gaps == ("deductible_clause", "claim_history")


@pytest.mark.parametrize(
    ("claimed", "route", "reason"),
    [
        (2750, "auto_approve", "within_threshold"),
        (2751, "adjuster", "over_threshold"),
    ],
)
def test_step_8_a_payable_amount_of_2500_approves_and_2501_does_not(
    claimed: int, route: str, reason: str
) -> None:
    decision = decide(make_facts(claim=make_claim(claimed_amount=claimed)))

    assert (decision.route, decision.reason, decision.recommendation) == (
        route,
        reason,
        "approve",
    )
    assert decision.payable_amount == claimed - 250
    assert decision.citations == ("2.2", "4.1")


def test_step_8_a_fraud_indicator_goes_to_the_adjuster_with_an_approval() -> None:
    claim = make_claim(reported_on=LOSS + timedelta(days=31))

    decision = decide(make_facts(claim=claim))

    assert (decision.route, decision.reason, decision.recommendation) == (
        "adjuster",
        "fraud_indicator",
        "approve",
    )
    assert decision.fraud_indicators == ("late_report",)
    assert decision.payable_amount == 1750
    assert decision.citations == ("2.2", "4.1", "5.1")


def test_step_8_a_fraud_indicator_outranks_a_payable_amount_over_the_limit() -> None:
    claim = make_claim(claimed_amount=9000, reported_on=LOSS + timedelta(days=31))

    assert decide(make_facts(claim=claim)).reason == "fraud_indicator"


def test_step_8_the_limit_clause_is_cited_when_the_claim_exceeds_the_limit() -> None:
    claim = make_claim(claimed_amount=5000)
    policy = make_policy(limit=2000)

    decision = decide(make_facts(claim=claim, policy=policy))

    assert decision.payable_amount == 1750
    assert decision.citations == ("2.2", "4.1", "4.2")
    assert decision.reason == "within_threshold"


def test_step_8_a_claim_equal_to_the_limit_does_not_cite_the_limit_clause() -> None:
    decision = decide(
        make_facts(
            claim=make_claim(claimed_amount=2000), policy=make_policy(limit=2000)
        )
    )

    assert decision.citations == ("2.2", "4.1")


def test_step_8_cites_cover_deductible_limit_then_reporting() -> None:
    claim = make_claim(claimed_amount=5000, reported_on=LOSS + timedelta(days=60))

    decision = decide(make_facts(claim=claim, policy=make_policy(limit=2000)))

    assert decision.citations == ("2.2", "4.1", "4.2", "5.1")


def test_step_8_leaves_out_clauses_that_were_not_retrieved() -> None:
    claim = make_claim(claimed_amount=5000, reported_on=LOSS + timedelta(days=60))
    terms = make_terms(deductible=None, limit=None, reporting=None)

    decision = decide(
        make_facts(claim=claim, policy=make_policy(limit=2000), terms=terms)
    )

    assert decision.citations == ("2.2",)


def test_step_8_over_threshold_still_lists_the_gaps() -> None:
    facts = make_facts(claim=make_claim(claimed_amount=9000), history_truncated=True)

    decision = decide(facts)

    assert (decision.reason, decision.gaps) == ("over_threshold", ("claim_history",))


def test_step_8_a_fraud_indicator_still_lists_the_gaps() -> None:
    claim = make_claim(reported_on=LOSS + timedelta(days=31))

    decision = decide(make_facts(claim=claim, history_truncated=True))

    assert (decision.reason, decision.gaps) == ("fraud_indicator", ("claim_history",))


# -- decide: one test per gap -------------------------------------------------------


def assert_unverified(decision: Decision, *gaps: str) -> None:
    assert (decision.route, decision.reason, decision.recommendation) == (
        "adjuster",
        "unverified",
        None,
    )
    assert decision.payable_amount == 1750
    assert decision.gaps == gaps


def test_gap_exclusion_clauses_when_the_exclusions_are_incomplete() -> None:
    decision = decide(make_facts(terms=make_terms(exclusions_complete=False)))

    assert_unverified(decision, "exclusion_clauses")


def test_gap_exclusion_clauses_when_no_section_three_clause_was_retrieved() -> None:
    chunks = [
        {"clause": "2.2", "section": "Cover", "title": "Storm", "body": "Cover."},
        {"clause": "4.1", "section": "Amounts", "title": "Deductible", "body": "EUR."},
        {"clause": "4.2", "section": "Amounts", "title": "Limit", "body": "EUR."},
    ]
    terms = select_terms("storm", chunks)

    decision = decide(make_facts(terms=terms))

    assert_unverified(decision, "exclusion_clauses")
    assert decision.citations == ("2.2", "4.1")


def test_gap_deductible_clause_when_it_was_not_retrieved() -> None:
    decision = decide(make_facts(terms=make_terms(deductible=None)))

    assert_unverified(decision, "deductible_clause")
    assert decision.citations == ("2.2",)


def test_gap_limit_clause_only_when_the_claim_is_above_the_limit() -> None:
    terms = make_terms(limit=None)
    policy = make_policy(limit=2000)

    above = decide(
        make_facts(claim=make_claim(claimed_amount=2001), policy=policy, terms=terms)
    )
    at = decide(
        make_facts(claim=make_claim(claimed_amount=2000), policy=policy, terms=terms)
    )

    assert above.gaps == ("limit_clause",)
    assert above.reason == "unverified"
    assert at.gaps == ()
    assert at.reason == "within_threshold"


def test_gap_exclusion_assessment_when_it_is_unavailable() -> None:
    facts = make_facts(
        terms=make_terms(candidates=(WEAR,)), assessment=Assessment("unavailable")
    )

    assert_unverified(decide(facts), "exclusion_assessment")


def test_gap_exclusion_assessment_when_it_was_not_made_though_needed() -> None:
    facts = make_facts(
        terms=make_terms(candidates=(WEAR,)), assessment=Assessment("not_needed")
    )

    assert_unverified(decide(facts), "exclusion_assessment")


def test_no_assessment_gap_when_the_assessment_found_nothing() -> None:
    facts = make_facts(
        terms=make_terms(candidates=(WEAR,)), assessment=Assessment("none_applies")
    )

    decision = decide(facts)

    assert decision.gaps == ()
    assert (decision.route, decision.reason) == ("auto_approve", "within_threshold")


def test_no_assessment_gap_when_none_is_needed() -> None:
    decision = decide(make_facts(assessment=Assessment("not_needed")))

    assert decision.gaps == ()


def test_gap_claim_history_when_it_is_truncated() -> None:
    decision = decide(make_facts(history_truncated=True))

    assert_unverified(decision, "claim_history")


def test_all_gaps_are_listed_in_order() -> None:
    claim = make_claim(claimed_amount=5000)
    facts = make_facts(
        claim=claim,
        policy=make_policy(limit=2000),
        terms=make_terms(
            candidates=(WEAR,), exclusions_complete=False, deductible=None, limit=None
        ),
        assessment=Assessment("unavailable"),
        history_truncated=True,
    )

    decision = decide(facts)

    assert decision.gaps == (
        "exclusion_clauses",
        "deductible_clause",
        "limit_clause",
        "exclusion_assessment",
        "claim_history",
    )
    assert decision.reason == "unverified"


def test_a_decision_is_frozen() -> None:
    decision = decide(make_facts())

    with pytest.raises(AttributeError):
        decision.route = "adjuster"  # type: ignore[misc]


# -- the assessment's shape -----------------------------------------------------------


def test_a_clause_is_set_exactly_when_the_assessment_applies() -> None:
    assert Assessment("applies", "3.3").clause == "3.3"
    for status in ("not_needed", "none_applies", "unavailable"):
        assert Assessment(status).clause is None  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="clause"):
            Assessment(status, "3.3")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="clause"):
        Assessment("applies")
