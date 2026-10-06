"""The golden cases added after the first forty: the fraud indicators on their
boundaries and a claim on a policy number no policy has.

The first forty are frozen while the recording of the model's answers stands:
that recording is keyed by the text of their descriptions, and a new one costs
money. These tests read the committed JSON and, for other seeds, the generator.
"""

import hashlib
import json
from collections import Counter
from datetime import date, timedelta

import pytest
from generator import catalogue, scenarios
from generator.scenarios import build_dataset

FROZEN_CLAIMS = 40
FROZEN_POLICIES = 50
FROZEN_HISTORY = 44
EXTRA_CLAIMS = 7
# The SHA-256 of the canonical JSON of what belongs to the first forty claims,
# taken from the committed files before the extra scenarios were added: their
# claims, labels, policies (POL-0001 to POL-0050) and history (HIST-0001 to
# HIST-0044), which the extra scenarios only follow.
FROZEN_DIGEST = "fb6ef3227204a0752692d3822f0b9245faf40c3bec93076abbbff78d25ccadb3"
OTHER_SEEDS = (1, 2, 3, 7, 11)
EXTRA_INDICATORS = {
    ("early_loss",): 1,
    ("frequent_claims",): 1,
    ("late_report",): 1,
    (): 3,
}


def day(text: str) -> date:
    return date.fromisoformat(text)


def frozen_digest(claims, outcomes, policies, history) -> str:
    frozen = {
        "claims": claims[:FROZEN_CLAIMS],
        "outcomes": outcomes[:FROZEN_CLAIMS],
        "policies": policies[:FROZEN_POLICIES],
        "history": history[:FROZEN_HISTORY],
    }
    text = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def measured(claim, policy, history) -> dict:
    """What a claim shows of each indicator, read from the raw records: the
    days from the policy's start to the loss, from the loss to the report, the
    entries of its policy dated in the 365 days before the loss, and how far
    back its oldest entry is."""
    loss = day(claim["loss_date"])
    earliest = loss - timedelta(days=catalogue.FREQUENT_CLAIMS_WINDOW_DAYS)
    own = [h for h in history if h["policy_number"] == claim["policy_number"]]
    ago = [(loss - day(h["loss_date"])).days for h in own]
    return {
        "since_start": (loss - day(policy["start_date"])).days,
        "delay": (day(claim["reported_on"]) - loss).days,
        "in_window": sum(1 for h in own if earliest <= day(h["loss_date"]) < loss),
        "entries": len(own),
        "oldest": max(ago, default=0),
    }


def extras(claims, outcomes, policies, history) -> list[dict]:
    """The claims after the first forty, each with its label, its policy and
    its numbers; both are None for a claim on no policy."""
    policy_of = {p["policy_number"]: p for p in policies}
    rows = []
    for claim, outcome in zip(
        claims[FROZEN_CLAIMS:], outcomes[FROZEN_CLAIMS:], strict=True
    ):
        policy = policy_of.get(claim["policy_number"])
        numbers = None if policy is None else measured(claim, policy, history)
        rows.append(
            {"claim": claim, "outcome": outcome, "policy": policy, "numbers": numbers}
        )
    return rows


def on_a_policy(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["policy"] is not None]


@pytest.fixture(scope="module")
def committed_extras(claims, outcomes, policies, history) -> list[dict]:
    return extras(claims, outcomes, policies, history)


# -- the first forty do not move ----------------------------------------------
def test_the_first_forty_claims_and_what_belongs_to_them_keep_their_bytes(
    claims, outcomes, policies, history
):
    # Arrange: the digest was taken before any extra scenario existed. A recorded
    # model answer is keyed by the description of one of these claims.

    # Act
    digest = frozen_digest(claims, outcomes, policies, history)

    # Assert
    assert digest == FROZEN_DIGEST, (
        "one of the first forty claims, or its policy, history or label, changed: "
        "a recorded answer of the model is keyed by these claims' descriptions, so "
        "a paid recording (make eval-record, with the owner's yes) would be needed "
        "to replay the evaluation; add scenarios to EXTRA_PLAN in generator/plan.py, "
        "drawn from the second stream, and leave the first forty alone"
    )


def test_the_extra_scenarios_leave_the_first_forty_as_they_were_without_them(
    monkeypatch,
):
    # Arrange: the extra scenarios draw from a stream of their own, so building
    # them or not leaves the first forty as they were
    with_extra = build_dataset(catalogue.DEFAULT_SEED)
    monkeypatch.setattr(scenarios, "build_extra", lambda seed, first: [])

    # Act
    without = build_dataset(catalogue.DEFAULT_SEED)

    # Assert
    assert len(without.claims) == FROZEN_CLAIMS
    assert with_extra.claims[:FROZEN_CLAIMS] == without.claims
    assert with_extra.outcomes[:FROZEN_CLAIMS] == without.outcomes
    assert with_extra.history[:FROZEN_HISTORY] == without.history
    assert with_extra.policies[:FROZEN_POLICIES] == without.policies


def test_the_extra_claims_follow_the_forty_in_number_and_in_every_file(
    claims, outcomes, policies, history, manifest
):
    # Assert
    assert [c["claim_id"] for c in claims] == [
        f"CLM-{n:04d}" for n in range(1, FROZEN_CLAIMS + EXTRA_CLAIMS + 1)
    ]
    assert [o["claim_id"] for o in outcomes] == [c["claim_id"] for c in claims]
    assert [h["history_id"] for h in history] == [
        f"HIST-{n:04d}" for n in range(1, len(history) + 1)
    ]
    assert {h["policy_number"] for h in history[FROZEN_HISTORY:]} <= {
        p["policy_number"] for p in policies[FROZEN_POLICIES:]
    }
    assert manifest["counts"]["claims"] == len(claims)


# -- the boundaries -------------------------------------------------------------
def test_a_loss_on_the_30th_day_after_the_start_is_early_and_the_31st_is_not(
    committed_extras,
):
    # Arrange
    rows = [
        r
        for r in on_a_policy(committed_extras)
        if r["numbers"]["since_start"] in (30, 31)
    ]

    # Act
    by_day = {r["numbers"]["since_start"]: r["outcome"] for r in rows}

    # Assert
    assert len(rows) == 2
    assert by_day[30]["fraud_indicators"] == ["early_loss"]
    assert by_day[30]["reason"] == "fraud_indicator"
    assert by_day[31]["fraud_indicators"] == []
    assert by_day[31]["reason"] == "within_threshold"


def test_a_report_on_the_31st_day_is_late_and_the_30th_is_not(committed_extras):
    # Arrange
    rows = [
        r for r in on_a_policy(committed_extras) if r["numbers"]["delay"] in (30, 31)
    ]

    # Act
    by_delay = {r["numbers"]["delay"]: r["outcome"] for r in rows}

    # Assert
    assert len(rows) == 2
    assert by_delay[31]["fraud_indicators"] == ["late_report"]
    assert by_delay[31]["reason"] == "fraud_indicator"
    assert by_delay[30]["fraud_indicators"] == []
    assert by_delay[30]["reason"] == "within_threshold"


def test_two_earlier_claims_with_the_older_365_days_back_are_frequent_366_are_not(
    committed_extras,
):
    # Arrange
    rows = [
        r
        for r in on_a_policy(committed_extras)
        if r["numbers"]["entries"] == 2 and r["numbers"]["oldest"] in (365, 366)
    ]

    # Act
    by_oldest = {r["numbers"]["oldest"]: r for r in rows}

    # Assert
    assert sorted(by_oldest) == [365, 366]
    assert by_oldest[365]["numbers"]["in_window"] == catalogue.FREQUENT_CLAIMS_COUNT
    assert by_oldest[365]["outcome"]["fraud_indicators"] == ["frequent_claims"]
    assert by_oldest[366]["numbers"]["in_window"] == (
        catalogue.FREQUENT_CLAIMS_COUNT - 1
    )
    assert by_oldest[366]["outcome"]["fraud_indicators"] == []
    assert by_oldest[366]["outcome"]["reason"] == "within_threshold"


def test_each_boundary_claim_has_its_own_indicator_or_none_and_a_small_payable(
    committed_extras,
):
    # Arrange
    rows = on_a_policy(committed_extras)

    # Act
    indicators = Counter(tuple(r["outcome"]["fraud_indicators"]) for r in rows)

    # Assert
    assert indicators == EXTRA_INDICATORS
    for row in rows:
        outcome = row["outcome"]
        assert 0 < outcome["payable_amount"] <= catalogue.AUTO_APPROVAL_LIMIT
        assert outcome["route"] == (
            "adjuster" if outcome["fraud_indicators"] else "auto_approve"
        )


def test_no_extra_claim_asks_the_model_about_a_circumstance(committed_extras):
    # Arrange: the model is asked only when a circumstance exclusion of the
    # product names the peril, so no recorded answer exists for an extra claim
    def candidate_perils(product_code: str) -> set[str]:
        return {
            peril
            for exclusion in catalogue.PRODUCTS[product_code].exclusions
            if exclusion.kind == catalogue.KIND_CIRCUMSTANCE
            for peril in exclusion.perils
        }

    # Act
    asked = [
        r["claim"]["claim_id"]
        for r in on_a_policy(committed_extras)
        if r["claim"]["peril"] in candidate_perils(r["policy"]["product"])
    ]

    # Assert
    assert asked == []


def test_the_claim_number_of_an_extra_claim_does_not_reveal_its_scenario(
    committed_extras,
):
    # Arrange
    reasons = [r["outcome"]["reason"] for r in committed_extras]
    policy_numbers = [r["claim"]["policy_number"] for r in committed_extras]

    # Assert: neither the reasons nor the policy numbers run in the order of the
    # plan (each indicator's pair, then the unknown policy) or of the numbers
    assert reasons != sorted(reasons)
    assert policy_numbers != sorted(policy_numbers)
    assert len({n for n in policy_numbers}) == EXTRA_CLAIMS


# -- an unknown policy ------------------------------------------------------------
def test_one_claim_names_a_policy_no_policy_has_and_goes_to_an_adjuster_unjudged(
    committed_extras, policies
):
    # Arrange
    unknown = [r for r in committed_extras if r["policy"] is None]

    # Act
    outcome = unknown[0]["outcome"]

    # Assert
    assert len(unknown) == 1
    assert unknown[0]["claim"]["policy_number"] not in {
        p["policy_number"] for p in policies
    }
    assert outcome == {
        "claim_id": unknown[0]["claim"]["claim_id"],
        "route": "adjuster",
        "reason": "policy_not_found",
        "recommendation": None,
        "payable_amount": None,
        "exclusion": None,
        "fraud_indicators": [],
        "missing_documents": [],
        "citations": [],
    }


def test_policy_not_found_is_a_reason_the_manifest_counts(manifest):
    # Assert
    assert "policy_not_found" in catalogue.REASONS
    assert list(manifest["reasons"]) == list(catalogue.REASONS)
    assert manifest["reasons"]["policy_not_found"] == 1
    assert sum(manifest["reasons"].values()) == FROZEN_CLAIMS + EXTRA_CLAIMS


# -- the generator, for other seeds -----------------------------------------------
@pytest.mark.parametrize("seed", OTHER_SEEDS)
def test_every_seed_builds_the_seven_claims_each_on_its_boundary(seed):
    # Arrange
    dataset = build_dataset(seed)

    # Act
    rows = extras(dataset.claims, dataset.outcomes, dataset.policies, dataset.history)
    flagged = Counter(
        tuple(r["outcome"]["fraud_indicators"]) for r in on_a_policy(rows)
    )

    # Assert
    assert len(rows) == EXTRA_CLAIMS
    assert flagged == EXTRA_INDICATORS
    assert [r["outcome"]["reason"] for r in rows].count("policy_not_found") == 1
    assert {30, 31} <= {r["numbers"]["since_start"] for r in on_a_policy(rows)}
    assert {30, 31} <= {r["numbers"]["delay"] for r in on_a_policy(rows)}
    assert {365, 366} <= {r["numbers"]["oldest"] for r in on_a_policy(rows)}
