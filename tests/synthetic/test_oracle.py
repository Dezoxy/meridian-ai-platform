"""The oracle's precedence and boundaries, on hand-built records."""

import pytest
from generator import catalogue
from generator.oracle import derive_outcome

PHOTOS_AND_ESTIMATE = ["photos", "repair_estimate"]


def make_policy(**overrides) -> dict:
    """A motor comprehensive policy in force all of 2026, deductible 300."""
    base = {
        "policy_number": "POL-0001",
        "product": "MOTOR-COMP",
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "status": "active",
        "lapsed_on": None,
        "deductible": 300,
        "sum_insured": 20000,
        "limit": 20000,
    }
    return {**base, **overrides}


def make_claim(**overrides) -> dict:
    """A complete, prompt collision claim with payable 1000."""
    base = {
        "claim_id": "CLM-0001",
        "policy_number": "POL-0001",
        "reported_on": "2026-06-12",
        "loss_date": "2026-06-10",
        "peril": "collision",
        "claimed_amount": 1300,
        "documents": PHOTOS_AND_ESTIMATE,
    }
    return {**base, **overrides}


def make_history(*loss_dates: str, policy_number: str = "POL-0001") -> list[dict]:
    return [
        {"policy_number": policy_number, "loss_date": loss_date}
        for loss_date in loss_dates
    ]


def clauses(outcome: dict) -> list[str]:
    return [citation["clause"] for citation in outcome["citations"]]


# -- precedence ---------------------------------------------------------------
def test_inactive_beats_excluded():
    # Arrange: own-vehicle damage on a liability policy that has already ended
    policy = make_policy(
        product="MOTOR-TPL", deductible=0, sum_insured=None, limit=1_000_000
    )
    policy["end_date"] = "2026-05-31"

    # Act
    outcome = derive_outcome(make_claim(), policy, [])

    # Assert
    assert outcome["reason"] == "policy_inactive"
    assert outcome["exclusion"] is None


def test_excluded_beats_missing_documents():
    # Arrange
    claim = make_claim(documents=[])

    # Act
    outcome = derive_outcome(claim, make_policy(), [], "driving_under_influence")

    # Assert
    assert outcome["reason"] == "excluded"
    assert outcome["exclusion"] == "driving_under_influence"
    assert outcome["missing_documents"] == []
    assert clauses(outcome) == ["3.2"]


def test_missing_documents_beat_fraud_indicators():
    # Arrange: a loss 9 days after the start, with the repair estimate missing
    policy = make_policy(start_date="2026-06-01")
    claim = make_claim(documents=["photos"])

    # Act
    outcome = derive_outcome(claim, policy, [])

    # Assert
    assert outcome["route"] == "request_documents"
    assert outcome["reason"] == "missing_documents"
    assert outcome["recommendation"] is None
    assert outcome["payable_amount"] is None
    assert outcome["missing_documents"] == ["repair_estimate"]
    assert outcome["fraud_indicators"] == ["early_loss"]
    assert clauses(outcome) == ["5.2"]


def test_fraud_indicator_beats_over_threshold():
    # Arrange: reported 51 days late, and a payable amount far above the limit
    claim = make_claim(reported_on="2026-07-31", claimed_amount=9000)

    # Act
    outcome = derive_outcome(claim, make_policy(), [])

    # Assert
    assert outcome["route"] == "adjuster"
    assert outcome["reason"] == "fraud_indicator"
    assert outcome["recommendation"] == "approve"
    assert outcome["payable_amount"] == 8700
    assert outcome["fraud_indicators"] == ["late_report"]
    assert clauses(outcome) == ["2.1", "4.1", "5.1"]


def test_fraud_citations_leave_out_the_reporting_clause_unless_late():
    # Arrange
    policy = make_policy(start_date="2026-06-01")

    # Act
    outcome = derive_outcome(make_claim(), policy, [])

    # Assert
    assert outcome["fraud_indicators"] == ["early_loss"]
    assert clauses(outcome) == ["2.1", "4.1"]


@pytest.mark.parametrize(
    ("claimed", "route", "reason"),
    [
        (2800, "auto_approve", "within_threshold"),
        (2801, "adjuster", "over_threshold"),
    ],
)
def test_the_threshold_is_payable_above_2500(claimed, route, reason):
    # Arrange: deductible 300, so claimed 2800 is payable exactly 2500

    # Act
    outcome = derive_outcome(make_claim(claimed_amount=claimed), make_policy(), [])

    # Assert
    assert outcome["route"] == route
    assert outcome["reason"] == reason
    assert outcome["payable_amount"] == claimed - 300
    assert outcome["recommendation"] == "approve"
    assert clauses(outcome) == ["2.1", "4.1"]


# -- policy in force ----------------------------------------------------------
def test_a_lapsed_policy_cites_the_lapse_clause():
    # Arrange: lapsed on the day of the loss
    policy = make_policy(status="lapsed", lapsed_on="2026-06-10")

    # Act
    outcome = derive_outcome(make_claim(), policy, [])

    # Assert
    assert outcome["reason"] == "policy_inactive"
    assert outcome["recommendation"] == "reject"
    assert outcome["route"] == "adjuster"
    assert outcome["payable_amount"] is None
    assert outcome["citations"] == [{"wording": "MOTOR-COMP", "clause": "6.2"}]


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [
        ("2025-01-01", "2025-12-31"),  # expired before the loss
        ("2026-06-11", "2027-06-10"),  # starts the day after the loss
        ("2025-06-10", "2026-06-09"),  # ended the day before the loss
    ],
)
def test_an_expired_or_unstarted_policy_cites_the_period_clause(start_date, end_date):
    # Arrange
    policy = make_policy(start_date=start_date, end_date=end_date)

    # Act
    outcome = derive_outcome(make_claim(), policy, [])

    # Assert
    assert outcome["reason"] == "policy_inactive"
    assert clauses(outcome) == ["6.1"]


def test_a_policy_that_lapsed_after_the_loss_was_in_force():
    # Arrange
    policy = make_policy(status="lapsed", lapsed_on="2026-06-11")

    # Act
    outcome = derive_outcome(make_claim(), policy, [])

    # Assert
    assert outcome["reason"] == "within_threshold"


def test_an_expired_and_lapsed_policy_cites_the_lapse():
    # Arrange
    policy = make_policy(status="lapsed", lapsed_on="2026-03-01", end_date="2026-05-31")

    # Act
    outcome = derive_outcome(make_claim(), policy, [])

    # Assert
    assert clauses(outcome) == ["6.2"]


def test_a_lapsed_policy_without_a_lapse_date_is_rejected_as_malformed():
    with pytest.raises(ValueError, match="lapse date"):
        derive_outcome(make_claim(), make_policy(status="lapsed"), [])


# -- payable amount -----------------------------------------------------------
@pytest.mark.parametrize("claimed", [300, 200])
def test_a_payable_amount_of_zero_or_less_raises(claimed):
    with pytest.raises(ValueError, match="not positive"):
        derive_outcome(make_claim(claimed_amount=claimed), make_policy(), [])


def test_the_limit_caps_the_payable_amount():
    # Arrange: claimed above the sum insured
    claim = make_claim(claimed_amount=30000)

    # Act
    outcome = derive_outcome(claim, make_policy(), [])

    # Assert
    assert outcome["payable_amount"] == 20000 - 300
    assert outcome["reason"] == "over_threshold"
    assert clauses(outcome) == ["2.1", "4.1", "4.2"]


@pytest.mark.parametrize(
    ("claimed", "expected"),
    [(20000, ["2.1", "4.1"]), (20001, ["2.1", "4.1", "4.2"])],
)
def test_the_limit_clause_is_cited_only_when_the_claim_exceeds_the_limit(
    claimed, expected
):
    # Arrange: a claim exactly at the limit is not capped

    # Act
    outcome = derive_outcome(make_claim(claimed_amount=claimed), make_policy(), [])

    # Assert
    assert clauses(outcome) == expected


def test_the_limit_clause_comes_before_the_reporting_clause():
    # Arrange: a late report on a claim above the limit
    claim = make_claim(claimed_amount=30000, reported_on="2026-07-31")

    # Act
    outcome = derive_outcome(claim, make_policy(), [])

    # Assert
    assert outcome["reason"] == "fraud_indicator"
    assert clauses(outcome) == ["2.1", "4.1", "4.2", "5.1"]


def test_a_claim_that_stops_before_payment_does_not_cite_the_limit():
    # Arrange: above the limit, but a document is missing
    claim = make_claim(claimed_amount=30000, documents=["photos"])

    # Act
    outcome = derive_outcome(claim, make_policy(), [])

    # Assert
    assert outcome["reason"] == "missing_documents"
    assert clauses(outcome) == ["5.2"]


def test_a_missing_document_claim_with_no_payable_amount_does_not_raise():
    # Arrange: claimed equals the deductible, but the claim stops at step 3
    claim = make_claim(claimed_amount=300, documents=[])

    # Act
    outcome = derive_outcome(claim, make_policy(), [])

    # Assert
    assert outcome["reason"] == "missing_documents"
    assert outcome["missing_documents"] == PHOTOS_AND_ESTIMATE


# -- the in-force side of the date boundaries ---------------------------------
def test_a_loss_on_the_start_date_is_in_force():
    # Arrange: the term starts on the loss date, which is also an early loss
    policy = make_policy(start_date="2026-06-10", end_date="2027-06-09")
    claim = make_claim(loss_date="2026-06-10", reported_on="2026-06-10")

    # Act
    outcome = derive_outcome(claim, policy, [])

    # Assert
    assert outcome["reason"] == "fraud_indicator"
    assert outcome["fraud_indicators"] == ["early_loss"]
    assert clauses(outcome) == ["2.1", "4.1"]


def test_a_loss_on_the_end_date_is_in_force():
    # Arrange: the last day of the term
    claim = make_claim(loss_date="2026-12-31", reported_on="2026-12-31")

    # Act
    outcome = derive_outcome(claim, make_policy(end_date="2026-12-31"), [])

    # Assert
    assert outcome["reason"] == "within_threshold"
    assert outcome["route"] == "auto_approve"


def test_a_history_entry_on_the_loss_date_does_not_count():
    # Arrange: one entry 200 days before the loss, one on the loss date itself
    entries = make_history("2025-11-22", "2026-06-10")

    # Act
    outcome = derive_outcome(make_claim(), make_policy(), entries)

    # Assert
    assert outcome["fraud_indicators"] == []
    assert outcome["reason"] == "within_threshold"


# -- fraud indicator boundaries -----------------------------------------------
@pytest.mark.parametrize(
    ("start_date", "expected"),
    [
        ("2026-05-11", ["early_loss"]),  # loss on day 30 of the term
        ("2026-05-10", []),  # loss on day 31
        ("2026-06-10", ["early_loss"]),  # loss on the start date
    ],
)
def test_early_loss_covers_the_first_30_days(start_date, expected):
    # Arrange
    policy = make_policy(start_date=start_date, end_date="2027-05-10")

    # Act
    outcome = derive_outcome(make_claim(), policy, [])

    # Assert
    assert outcome["fraud_indicators"] == expected


@pytest.mark.parametrize(
    ("reported_on", "expected"),
    [("2026-07-10", []), ("2026-07-11", ["late_report"])],
)
def test_a_report_is_late_after_30_days(reported_on, expected):
    # Arrange: the loss is on 10 June; 30 days later is 10 July

    # Act
    outcome = derive_outcome(make_claim(reported_on=reported_on), make_policy(), [])

    # Assert
    assert outcome["fraud_indicators"] == expected


@pytest.mark.parametrize(
    ("loss_dates", "expected"),
    [
        (("2025-06-10", "2025-12-01"), ["frequent_claims"]),  # 365 days counts
        (("2025-06-09", "2025-12-01"), []),  # 366 days does not
        (("2026-06-01",), []),  # one entry is not frequent
        (("2025-12-01", "2026-03-01", "2026-05-01"), ["frequent_claims"]),
        (("2026-06-10", "2026-06-11"), []),  # not before the loss
    ],
)
def test_frequent_claims_need_two_entries_in_the_365_days_before_the_loss(
    loss_dates, expected
):
    # Arrange
    entries = make_history(*loss_dates)

    # Act
    outcome = derive_outcome(make_claim(), make_policy(), entries)

    # Assert
    assert outcome["fraud_indicators"] == expected


@pytest.mark.parametrize("status", ["submitted", "awaiting_adjuster", "approved"])
def test_a_history_entry_counts_by_its_dates_whatever_its_status(status):
    # S067: a claim still open is a history entry of the platform's run, as a
    # decided one is. The oracle counts by policy and loss date and reads no
    # status, so it needs no change.
    # Arrange
    entries = [
        {**entry, "status": status}
        for entry in make_history("2026-03-01", "2026-04-01", "2026-06-10")
    ]

    # Act
    outcome = derive_outcome(make_claim(), make_policy(), entries)

    # Assert: the third entry has the loss's own date and is not counted
    assert outcome["fraud_indicators"] == ["frequent_claims"]
    assert (
        derive_outcome(make_claim(), make_policy(), entries[2:])["fraud_indicators"]
        == []
    )


def test_history_of_another_policy_is_ignored():
    # Arrange
    entries = make_history("2026-03-01", "2026-04-01", policy_number="POL-0002")

    # Act
    outcome = derive_outcome(make_claim(), make_policy(), entries)

    # Assert
    assert outcome["fraud_indicators"] == []


def test_indicators_are_listed_in_catalogue_order_for_every_route():
    # Arrange: all three indicators, on a policy that has ended
    policy = make_policy(start_date="2026-06-01", end_date="2026-06-09")
    claim = make_claim(reported_on="2026-07-20")
    entries = make_history("2026-01-01", "2026-02-01")

    # Act
    outcome = derive_outcome(claim, policy, entries)

    # Assert
    assert outcome["reason"] == "policy_inactive"
    assert outcome["fraud_indicators"] == list(catalogue.FRAUD_INDICATORS)


# -- exclusions and malformed input -------------------------------------------
def test_a_peril_exclusion_needs_no_circumstance():
    # Arrange: flood is not covered by Home Standard
    policy = make_policy(product="HOME-STD", deductible=250)
    claim = make_claim(peril="flood", documents=["photos"])

    # Act
    outcome = derive_outcome(claim, policy, [])

    # Assert
    assert outcome["reason"] == "excluded"
    assert outcome["exclusion"] == "flood"
    assert clauses(outcome) == ["3.1"]
    assert outcome["citations"][0]["wording"] == "HOME-STD"


def test_a_circumstance_excludes_only_the_perils_it_names():
    # Arrange: racing excludes collision on Motor Comprehensive, not theft
    theft = make_claim(peril="theft", documents=["police_report"])

    # Act
    collision_outcome = derive_outcome(make_claim(), make_policy(), [], "racing")
    theft_outcome = derive_outcome(theft, make_policy(), [], "racing")

    # Assert
    assert collision_outcome["exclusion"] == "racing"
    assert theft_outcome["exclusion"] is None
    assert theft_outcome["reason"] == "within_threshold"


def test_a_circumstance_the_product_does_not_exclude_is_ignored():
    # Arrange: Motor Comprehensive has no wear-and-tear exclusion

    # Act
    outcome = derive_outcome(make_claim(), make_policy(), [], "wear_and_tear")

    # Assert
    assert outcome["exclusion"] is None


# -- a policy number no policy has --------------------------------------------
def test_a_claim_on_no_policy_goes_to_an_adjuster_with_nothing_else_decided():
    # Arrange: a claim that would otherwise hold every indicator and miss a document
    claim = make_claim(reported_on="2026-08-10", documents=[])

    # Act
    outcome = derive_outcome(claim, None, make_history("2026-05-01", "2026-05-02"))

    # Assert
    assert outcome == {
        "claim_id": "CLM-0001",
        "route": "adjuster",
        "reason": "policy_not_found",
        "recommendation": None,
        "payable_amount": None,
        "exclusion": None,
        "fraud_indicators": [],
        "missing_documents": [],
        "citations": [],
    }


def test_a_claim_on_no_policy_reported_before_the_loss_still_raises():
    with pytest.raises(ValueError, match="before the loss"):
        derive_outcome(make_claim(reported_on="2026-06-09"), None, [])


def test_an_unknown_circumstance_raises():
    with pytest.raises(ValueError, match="unknown circumstance"):
        derive_outcome(make_claim(), make_policy(), [], "sunspots")


def test_an_unknown_circumstance_raises_even_for_a_peril_exclusion():
    # Arrange: flood is excluded by Home Standard, so the peril exclusion would
    # otherwise return before the circumstance was ever looked at
    policy = make_policy(product="HOME-STD", deductible=250)
    claim = make_claim(peril="flood", documents=["photos"])

    # Act and assert
    with pytest.raises(ValueError, match="unknown circumstance"):
        derive_outcome(claim, policy, [], "sunspots")


def test_a_peril_of_another_line_raises():
    with pytest.raises(ValueError, match="not a peril"):
        derive_outcome(make_claim(peril="flood"), make_policy(), [])


def test_a_claim_reported_before_the_loss_raises():
    with pytest.raises(ValueError, match="before the loss"):
        derive_outcome(make_claim(reported_on="2026-06-09"), make_policy(), [])


def test_the_oracle_does_not_change_its_inputs():
    # Arrange
    claim, policy, entries = make_claim(), make_policy(), make_history("2026-01-01")
    before = (dict(claim), dict(policy), [dict(entry) for entry in entries])

    # Act
    derive_outcome(claim, policy, entries)

    # Assert
    assert (claim, policy, entries) == before
