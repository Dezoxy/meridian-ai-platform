"""Label invariants over the committed inputs and expected outcomes.

These tests read the committed JSON only. They do not import the scenario
builders, so they hold the labels to the contract rather than to the code that
made them.
"""

import re
from collections import Counter
from datetime import date, timedelta

import pytest

CLAIM_FIELDS = [
    "claim_id",
    "policy_number",
    "reported_on",
    "loss_date",
    "peril",
    "claimed_amount",
    "loss_location",
    "claimant",
    "description",
    "documents",
]
OUTCOME_FIELDS = [
    "claim_id",
    "route",
    "reason",
    "recommendation",
    "payable_amount",
    "exclusion",
    "fraud_indicators",
    "missing_documents",
    "citations",
]
POLICY_FIELDS = [
    "policy_number",
    "product",
    "wording_version",
    "holder",
    "start_date",
    "end_date",
    "status",
    "lapsed_on",
    "deductible",
    "sum_insured",
    "limit",
    "insured_object",
]
HISTORY_FIELDS = [
    "history_id",
    "policy_number",
    "loss_date",
    "peril",
    "paid_amount",
    "status",
]
SCENARIO_MIX = {
    "within_threshold": 8,
    "over_threshold": 6,
    "fraud_indicator": 6,
    "excluded": 8,
    "policy_inactive": 6,
    "missing_documents": 6,
}
ROUTE_AND_RECOMMENDATION = {
    "within_threshold": ("auto_approve", "approve"),
    "over_threshold": ("adjuster", "approve"),
    "fraud_indicator": ("adjuster", "approve"),
    "excluded": ("adjuster", "reject"),
    "policy_inactive": ("adjuster", "reject"),
    "missing_documents": ("request_documents", None),
}
AUTO_APPROVAL_LIMIT = 2500
REFERENCE_DATE = date(2026, 9, 1)
LOSS_WINDOW = (date(2026, 5, 1), date(2026, 8, 25))
# One phrase per circumstance exclusion that the description must state.
CIRCUMSTANCE_KEYWORD = {
    "racing": "track day",
    "driving_under_influence": "drinks",
    "unlicensed_driver": "licence",
    "wear_and_tear": "rotten",
    "gradual_leak": "months",
}
PERIL_EXCLUSIONS = ("own_vehicle_damage", "flood", "accidental_damage")
LABEL_WORDS = re.compile(
    r"exclu|reject|approv|fraud|golden|oracle|indicator|threshold", re.IGNORECASE
)


def by_key(records: list[dict], key: str) -> dict[str, dict]:
    return {record[key]: record for record in records}


def day(text: str) -> date:
    return date.fromisoformat(text)


def walk(value):
    """Every leaf of a JSON value."""
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)
    else:
        yield value


def test_the_manifest_names_the_workload_the_set_belongs_to(manifest):
    # `meridian eval run` refuses a set whose manifest names another workload.
    assert list(manifest)[:3] == ["synthetic", "workload", "generator_version"]
    assert manifest["workload"] == "claims-triage"


# -- label invariants ---------------------------------------------------------
def test_the_reason_counts_match_the_scenario_mix(outcomes, manifest):
    assert Counter(o["reason"] for o in outcomes) == SCENARIO_MIX
    assert manifest["reasons"] == SCENARIO_MIX
    assert len(outcomes) == 40


def test_route_and_recommendation_follow_from_the_reason(outcomes):
    for outcome in outcomes:
        expected = ROUTE_AND_RECOMMENDATION[outcome["reason"]]
        assert (outcome["route"], outcome["recommendation"]) == expected, outcome


def test_auto_approve_never_has_a_fraud_indicator_or_a_large_payable(outcomes):
    approved = [o for o in outcomes if o["route"] == "auto_approve"]
    assert approved
    for outcome in approved:
        assert outcome["fraud_indicators"] == []
        assert 0 < outcome["payable_amount"] <= AUTO_APPROVAL_LIMIT


def test_every_reject_routes_to_an_adjuster(outcomes):
    rejects = [o for o in outcomes if o["recommendation"] == "reject"]
    assert rejects
    assert {o["route"] for o in rejects} == {"adjuster"}


def test_request_documents_lists_what_is_missing_and_recommends_nothing(outcomes):
    requests = [o for o in outcomes if o["route"] == "request_documents"]
    assert requests
    for outcome in requests:
        assert outcome["missing_documents"]
        assert outcome["recommendation"] is None
        assert outcome["payable_amount"] is None


def test_approve_always_has_a_positive_payable_amount(outcomes):
    approvals = [o for o in outcomes if o["recommendation"] == "approve"]
    assert approvals
    for outcome in approvals:
        assert outcome["payable_amount"] > 0


def test_only_approvals_carry_a_payable_amount(outcomes):
    for outcome in outcomes:
        has_amount = outcome["payable_amount"] is not None
        assert has_amount == (outcome["recommendation"] == "approve"), outcome


def test_an_exclusion_is_named_exactly_when_the_reason_is_excluded(outcomes):
    for outcome in outcomes:
        assert (outcome["exclusion"] is not None) == (outcome["reason"] == "excluded")
    named = Counter(o["exclusion"] for o in outcomes if o["exclusion"])
    assert len(named) == 8, "every exclusion code appears once"
    assert set(named.values()) == {1}


def test_missing_documents_are_listed_exactly_when_the_reason_says_so(outcomes):
    for outcome in outcomes:
        listed = bool(outcome["missing_documents"])
        assert listed == (outcome["reason"] == "missing_documents")


def test_the_threshold_separates_within_from_over(outcomes):
    for outcome in outcomes:
        if outcome["reason"] == "within_threshold":
            assert outcome["payable_amount"] <= AUTO_APPROVAL_LIMIT
        if outcome["reason"] == "over_threshold":
            assert outcome["payable_amount"] > AUTO_APPROVAL_LIMIT
    payables = {o["payable_amount"] for o in outcomes}
    assert {AUTO_APPROVAL_LIMIT, AUTO_APPROVAL_LIMIT + 1} <= payables


def test_fraud_cases_are_isolated_two_per_indicator(outcomes):
    fraud = [o for o in outcomes if o["reason"] == "fraud_indicator"]
    counts = Counter(tuple(o["fraud_indicators"]) for o in fraud)
    assert counts == {
        ("early_loss",): 2,
        ("frequent_claims",): 2,
        ("late_report",): 2,
    }


def test_no_other_reason_carries_a_fraud_indicator(outcomes):
    for outcome in outcomes:
        if outcome["reason"] != "fraud_indicator":
            assert outcome["fraud_indicators"] == [], outcome


def test_payable_amount_is_the_capped_claim_less_the_deductible(
    outcomes, claims, policies
):
    claim_of, policy_of = by_key(claims, "claim_id"), by_key(policies, "policy_number")
    for outcome in outcomes:
        if outcome["payable_amount"] is None:
            continue
        claim = claim_of[outcome["claim_id"]]
        policy = policy_of[claim["policy_number"]]
        capped = min(claim["claimed_amount"], policy["limit"])
        assert outcome["payable_amount"] == capped - policy["deductible"]
    assert any(
        claim_of[o["claim_id"]]["claimed_amount"]
        > policy_of[claim_of[o["claim_id"]]["policy_number"]]["limit"]
        for o in outcomes
    ), "one claim should exceed its limit"


def test_the_limit_clause_is_cited_exactly_when_the_limit_caps_the_claim(
    outcomes, claims, policies
):
    claim_of, policy_of = by_key(claims, "claim_id"), by_key(policies, "policy_number")
    capped = []
    for outcome in outcomes:
        claim = claim_of[outcome["claim_id"]]
        limit = policy_of[claim["policy_number"]]["limit"]
        clauses = [c["clause"] for c in outcome["citations"]]
        should_cite = outcome["payable_amount"] is not None and (
            claim["claimed_amount"] > limit
        )
        assert ("4.2" in clauses) == should_cite, outcome["claim_id"]
        if should_cite:
            capped.append(clauses)
            assert clauses.index("4.1") + 1 == clauses.index("4.2")
    assert capped, "one claim should exceed its limit"


def test_fraud_indicators_agree_with_the_raw_data(outcomes, claims, policies, history):
    claim_of, policy_of = by_key(claims, "claim_id"), by_key(policies, "policy_number")
    for outcome in outcomes:
        claim = claim_of[outcome["claim_id"]]
        policy = policy_of[claim["policy_number"]]
        loss, reported = day(claim["loss_date"]), day(claim["reported_on"])
        found = []
        if 0 <= (loss - day(policy["start_date"])).days <= 30:
            found.append("early_loss")
        recent = [
            h
            for h in history
            if h["policy_number"] == policy["policy_number"]
            and loss - timedelta(days=365) <= day(h["loss_date"]) < loss
        ]
        if len(recent) >= 2:
            found.append("frequent_claims")
        if (reported - loss).days > 30:
            found.append("late_report")
        assert outcome["fraud_indicators"] == found, outcome["claim_id"]


def test_policy_inactive_matches_the_policy_dates(outcomes, claims, policies):
    claim_of, policy_of = by_key(claims, "claim_id"), by_key(policies, "policy_number")
    for outcome in outcomes:
        claim = claim_of[outcome["claim_id"]]
        policy = policy_of[claim["policy_number"]]
        loss = day(claim["loss_date"])
        lapsed = policy["status"] == "lapsed" and day(policy["lapsed_on"]) <= loss
        in_force = day(policy["start_date"]) <= loss <= day(policy["end_date"])
        inactive = lapsed or not in_force
        assert inactive == (outcome["reason"] == "policy_inactive"), outcome
        if inactive:
            clause = "6.2" if lapsed else "6.1"
            cited = [citation["clause"] for citation in outcome["citations"]]
            assert cited == [clause], outcome
    causes = Counter(
        "lapsed" if o["citations"][0]["clause"] == "6.2" else "period"
        for o in outcomes
        if o["reason"] == "policy_inactive"
    )
    assert set(causes) == {"lapsed", "period"}, "expired and lapsed both appear"


def test_one_lapsed_policy_was_still_in_force_at_the_loss(claims, policies, outcomes):
    claim_of, policy_of = by_key(claims, "claim_id"), by_key(policies, "policy_number")
    traps = [
        o
        for o in outcomes
        if o["reason"] != "policy_inactive"
        and policy_of[claim_of[o["claim_id"]]["policy_number"]]["status"] == "lapsed"
    ]
    assert traps


def test_the_variants_the_readme_promises_exist(claims, policies, outcomes):
    claim_of, policy_of = by_key(claims, "claim_id"), by_key(policies, "policy_number")
    rows = [
        (
            o,
            claim_of[o["claim_id"]],
            policy_of[claim_of[o["claim_id"]]["policy_number"]],
        )
        for o in outcomes
    ]
    inactive = [(c, p) for o, c, p in rows if o["reason"] == "policy_inactive"]

    def lapsed(claim, policy):
        return policy["status"] == "lapsed" and day(policy["lapsed_on"]) <= day(
            claim["loss_date"]
        )

    lapsed_claims = [(c, p) for c, p in inactive if lapsed(c, p)]
    not_started = [
        (c, p) for c, p in inactive if day(c["loss_date"]) < day(p["start_date"])
    ]
    expired = [
        (c, p)
        for c, p in inactive
        if not lapsed(c, p) and day(c["loss_date"]) > day(p["end_date"])
    ]
    assert (len(expired), len(lapsed_claims), len(not_started)) == (3, 2, 1)
    assert any(day(p["lapsed_on"]) == day(c["loss_date"]) for c, p in lapsed_claims)
    one_day = timedelta(days=1)
    assert any(day(c["loss_date"]) == day(p["end_date"]) + one_day for c, p in expired)

    fraud = [
        o["payable_amount"] for o, _, _ in rows if o["reason"] == "fraud_indicator"
    ]
    assert any(amount > AUTO_APPROVAL_LIMIT for amount in fraud)
    assert any(amount <= AUTO_APPROVAL_LIMIT for amount in fraud)
    payables = {o["payable_amount"] for o, _, _ in rows}
    assert {AUTO_APPROVAL_LIMIT, AUTO_APPROVAL_LIMIT + 1} <= payables
    assert any(
        c["claimed_amount"] > p["limit"] for o, c, p in rows if o["payable_amount"]
    )
    assert any(
        p["status"] == "lapsed"
        and day(p["lapsed_on"]) > day(c["loss_date"])
        and o["reason"] != "policy_inactive"
        for o, c, p in rows
    ), "a policy that lapsed after the loss was still in force"


# -- inputs carry no labels ---------------------------------------------------
def test_claims_carry_no_label_fields(claims, outcomes):
    label_keys = set().union(*(o.keys() for o in outcomes)) - {"claim_id"}
    for claim in claims:
        assert not label_keys & claim.keys(), claim["claim_id"]
        assert list(claim) == CLAIM_FIELDS


def test_claims_and_outcomes_have_the_same_claim_ids(claims, outcomes):
    claim_ids = [c["claim_id"] for c in claims]
    assert claim_ids == [o["claim_id"] for o in outcomes]
    assert claim_ids == [f"CLM-{n:04d}" for n in range(1, 41)]


def test_descriptions_do_not_use_label_vocabulary(claims):
    for claim in claims:
        assert not LABEL_WORDS.search(claim["description"]), claim["claim_id"]


def test_the_claim_number_reveals_neither_the_outcome_nor_the_policy(claims, outcomes):
    reasons = [o["reason"] for o in outcomes]
    assert reasons != sorted(reasons), "claims are not grouped by outcome"
    same_number = [
        c
        for c in claims
        if c["policy_number"].split("-")[1] == c["claim_id"].split("-")[1]
    ]
    assert len(same_number) < len(claims) // 2


def test_descriptions_state_the_circumstance_of_a_circumstance_exclusion(
    claims, outcomes
):
    claim_of = by_key(claims, "claim_id")
    stated = set()
    for outcome in outcomes:
        code = outcome["exclusion"]
        if code not in CIRCUMSTANCE_KEYWORD:
            continue
        description = claim_of[outcome["claim_id"]]["description"]
        assert CIRCUMSTANCE_KEYWORD[code] in description.lower(), outcome["claim_id"]
        stated.add(code)
    assert stated == set(CIRCUMSTANCE_KEYWORD)


def test_other_claims_do_not_mention_a_circumstance(claims, outcomes):
    claim_of = by_key(claims, "claim_id")
    for outcome in outcomes:
        if outcome["exclusion"] in CIRCUMSTANCE_KEYWORD:
            continue
        text = claim_of[outcome["claim_id"]]["description"].lower()
        for keyword in CIRCUMSTANCE_KEYWORD.values():
            assert keyword not in text, (outcome["claim_id"], keyword)


def test_descriptions_are_two_to_four_sentences(claims):
    for claim in claims:
        sentences = re.findall(r"[.!?](?:\s|$)", claim["description"])
        assert 2 <= len(sentences) <= 4, claim["claim_id"]


def test_a_late_report_gives_a_reason_for_the_delay(claims, outcomes):
    claim_of = by_key(claims, "claim_id")
    late = [o for o in outcomes if "late_report" in o["fraud_indicators"]]
    assert len(late) == 2
    for outcome in late:
        description = claim_of[outcome["claim_id"]]["description"].lower()
        assert re.search(r"delay|earlier|so quickly|could not|not realise", description)


# -- contact data -------------------------------------------------------------
def test_contact_data_is_synthetic(policies, claims):
    emails = [p["holder"]["email"] for p in policies]
    emails += [c["claimant"]["email"] for c in claims]
    assert all(email.endswith("@example.com") for email in emails)
    assert len({p["holder"]["email"] for p in policies}) == len(policies)


def test_registrations_use_an_invented_format(policies):
    registrations = [
        p["insured_object"]["registration"]
        for p in policies
        if "registration" in p["insured_object"]
    ]
    assert registrations
    assert all(re.fullmatch(r"SYN-\d{4}", value) for value in registrations)
    assert len(set(registrations)) == len(registrations)


def test_no_phone_numbers_bank_accounts_or_birth_dates_appear(policies, claims):
    text = "\n".join(str(walk_value) for walk_value in walk([policies, claims]))
    assert not re.search(r"\+\d{5,}|\b\d{3}[ -]\d{3}[ -]\d{3,}\b", text)
    assert not re.search(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", text)
    for record in policies:
        assert "date_of_birth" not in record["holder"]
        assert "phone" not in record["holder"]


def test_the_claimant_is_the_policy_holder(claims, policies):
    policy_of = by_key(policies, "policy_number")
    for claim in claims:
        holder = policy_of[claim["policy_number"]]["holder"]
        assert claim["claimant"] == {"name": holder["name"], "email": holder["email"]}


# -- shape --------------------------------------------------------------------
def test_records_keep_the_contract_field_order(policies, history, outcomes):
    assert all(list(p) == POLICY_FIELDS for p in policies)
    assert all(list(h) == HISTORY_FIELDS for h in history)
    assert all(list(o) == OUTCOME_FIELDS for o in outcomes)


def test_lists_are_sorted_by_id(policies, history, claims, outcomes):
    for records, key in (
        (policies, "policy_number"),
        (history, "history_id"),
        (claims, "claim_id"),
        (outcomes, "claim_id"),
    ):
        ids = [r[key] for r in records]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)


def test_there_are_no_floats_anywhere(policies, history, claims, outcomes, manifest):
    for value in walk([policies, history, claims, outcomes, manifest]):
        assert not isinstance(value, float), value


def test_money_is_a_whole_number_of_euros(policies, history, claims, outcomes):
    for policy in policies:
        for key in ("deductible", "limit"):
            assert isinstance(policy[key], int)
        assert policy["sum_insured"] is None or isinstance(policy["sum_insured"], int)
    assert all(isinstance(h["paid_amount"], int) for h in history)
    assert all(isinstance(c["claimed_amount"], int) for c in claims)
    assert all(
        o["payable_amount"] is None or isinstance(o["payable_amount"], int)
        for o in outcomes
    )


def test_claims_fall_in_the_reporting_period(claims):
    for claim in claims:
        loss, reported = day(claim["loss_date"]), day(claim["reported_on"])
        assert LOSS_WINDOW[0] <= loss <= LOSS_WINDOW[1]
        assert loss <= reported <= REFERENCE_DATE


def test_no_policy_starts_or_lapses_after_the_reference_date(policies):
    for policy in policies:
        assert day(policy["start_date"]) <= REFERENCE_DATE
        if policy["lapsed_on"] is not None:
            assert (
                day(policy["start_date"]) <= day(policy["lapsed_on"]) <= REFERENCE_DATE
            )


def test_history_is_never_dated_after_the_term_or_on_or_after_a_lapse(
    history, policies
):
    # History may predate the current term (the policy is a renewal), but a
    # closed claim cannot be dated after the term ended or once cover lapsed.
    policy_of = by_key(policies, "policy_number")
    for entry in history:
        policy = policy_of[entry["policy_number"]]
        dated = day(entry["loss_date"])
        assert dated <= day(policy["end_date"]), entry["history_id"]
        assert dated <= REFERENCE_DATE, entry["history_id"]
        if policy["lapsed_on"] is not None:
            assert dated < day(policy["lapsed_on"]), entry["history_id"]


def test_claimed_amounts_exceed_the_deductible(claims, policies):
    policy_of = by_key(policies, "policy_number")
    for claim in claims:
        policy = policy_of[claim["policy_number"]]
        assert claim["claimed_amount"] > policy["deductible"]


def test_documents_are_in_catalogue_order(claims):
    order = ["police_report", "photos", "repair_estimate", "accident_statement"]
    for claim in claims:
        assert claim["documents"] == sorted(claim["documents"], key=order.index)


def test_fifty_policies_forty_with_a_claim_and_history_of_closed_claims(
    policies, claims, history
):
    assert len(policies) == 50
    claimed = [c["policy_number"] for c in claims]
    assert len(set(claimed)) == 40
    assert set(claimed) <= {p["policy_number"] for p in policies}
    assert all(h["status"] == "closed" for h in history)
    assert {h["policy_number"] for h in history} <= {
        p["policy_number"] for p in policies
    }


def test_every_product_appears_in_the_claims(claims, policies):
    policy_of = by_key(policies, "policy_number")
    counts = Counter(policy_of[c["policy_number"]]["product"] for c in claims)
    assert set(counts) == {"MOTOR-TPL", "MOTOR-COMP", "HOME-STD", "HOME-PLUS"}
    assert min(counts.values()) >= 5


@pytest.mark.parametrize(
    ("product", "deductible", "limit"),
    [
        ("MOTOR-TPL", 0, 1_000_000),
        ("MOTOR-COMP", 300, None),
        ("HOME-STD", 250, None),
        ("HOME-PLUS", 150, None),
    ],
)
def test_policies_follow_their_product(policies, product, deductible, limit):
    own = [p for p in policies if p["product"] == product]
    assert own
    for policy in own:
        assert policy["deductible"] == deductible
        assert policy["wording_version"] == "2026-01"
        assert policy["limit"] == (limit if limit else policy["sum_insured"])
        assert day(policy["end_date"]) == day(policy["start_date"]).replace(
            year=day(policy["start_date"]).year + 1
        ) - timedelta(days=1)


def test_sum_insured_ranges_and_insured_objects(policies):
    for policy in policies:
        product, sum_insured = policy["product"], policy["sum_insured"]
        insured = policy["insured_object"]
        if product == "MOTOR-TPL":
            assert sum_insured is None
        elif product == "MOTOR-COMP":
            assert 8_000 <= sum_insured <= 45_000
        else:
            assert 80_000 <= sum_insured <= 400_000
        if product.startswith("MOTOR"):
            assert list(insured) == ["make", "model", "year", "registration"]
        else:
            assert list(insured) == ["address", "building_type"]
            assert insured["building_type"] in ("apartment", "house")
        assert policy["status"] in ("active", "lapsed")
        assert (policy["status"] == "lapsed") == (policy["lapsed_on"] is not None)
