"""The scenario plan: 40 claims, each with the outcome it is built to produce.

The plan is the balance of the golden set and is written down here, not drawn
at random: eight claims within the threshold, six over it, six with one fraud
indicator each, eight excluded (every exclusion code once), six on inactive
policies and six with missing documents, spread over all four products. The
generator shuffles the plan with its seed, so claim numbers carry no hint of
the outcome.

``variant`` says how a builder shapes the facts:

- within_threshold: "" | "boundary" (payable exactly 2500) | "lapsed_later"
  (a policy that lapsed after the loss, so it was in force on the loss date);
- over_threshold: "" | "boundary" (payable exactly 2501) | "clipped" (claimed
  more than the sum insured, so the limit caps the payable amount);
- fraud_indicator: the one indicator: early_loss | frequent_claims | late_report;
- excluded: the exclusion code;
- policy_inactive: expired | expired_next_day | lapsed | lapsed_same_day |
  not_started;
- missing_documents: no variant; ``missing`` lists the documents left out.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ScenarioSpec:
    reason: str
    product: str
    peril: str
    variant: str = ""
    large: bool = False  # fraud claims: payable above the auto-approval limit
    missing: tuple[str, ...] = ()


def _spec(reason: str, *rows: tuple) -> tuple[ScenarioSpec, ...]:
    return tuple(ScenarioSpec(reason, *row) for row in rows)


PLAN = (
    *_spec(
        "within_threshold",
        ("MOTOR-COMP", "glass"),
        ("MOTOR-COMP", "collision", "boundary"),
        ("MOTOR-COMP", "storm", "lapsed_later"),
        ("MOTOR-TPL", "third_party_liability"),
        ("HOME-STD", "burst_pipe"),
        ("HOME-STD", "burglary"),
        ("HOME-PLUS", "accidental_damage"),
        ("HOME-PLUS", "flood"),
    ),
    *_spec(
        "over_threshold",
        ("MOTOR-COMP", "collision"),
        ("MOTOR-COMP", "theft", "clipped"),
        ("MOTOR-TPL", "third_party_liability", "boundary"),
        ("HOME-STD", "fire"),
        ("HOME-PLUS", "flood"),
        ("HOME-PLUS", "burglary"),
    ),
    *_spec(
        "fraud_indicator",
        ("MOTOR-COMP", "theft", "early_loss", True),
        ("HOME-PLUS", "fire", "early_loss"),
        ("MOTOR-COMP", "glass", "frequent_claims"),
        ("HOME-STD", "burglary", "frequent_claims", True),
        ("HOME-STD", "storm", "late_report", True),
        ("MOTOR-TPL", "third_party_liability", "late_report"),
    ),
    *_spec(
        "excluded",
        ("MOTOR-TPL", "collision", "own_vehicle_damage"),
        ("MOTOR-TPL", "third_party_liability", "racing"),
        ("MOTOR-COMP", "collision", "driving_under_influence"),
        ("MOTOR-COMP", "collision", "unlicensed_driver"),
        ("HOME-STD", "flood", "flood"),
        ("HOME-STD", "accidental_damage", "accidental_damage"),
        ("HOME-STD", "burst_pipe", "gradual_leak"),
        ("HOME-PLUS", "storm", "wear_and_tear"),
    ),
    *_spec(
        "policy_inactive",
        ("MOTOR-COMP", "collision", "expired_next_day"),
        ("HOME-STD", "fire", "expired"),
        ("MOTOR-TPL", "third_party_liability", "expired"),
        ("HOME-PLUS", "burst_pipe", "lapsed_same_day"),
        ("MOTOR-COMP", "glass", "lapsed"),
        ("HOME-PLUS", "storm", "not_started"),
    ),
    *_spec(
        "missing_documents",
        ("MOTOR-COMP", "collision", "", False, ("repair_estimate",)),
        ("MOTOR-COMP", "theft", "", False, ("police_report",)),
        ("MOTOR-TPL", "third_party_liability", "", False, ("accident_statement",)),
        ("HOME-STD", "burglary", "", False, ("police_report",)),
        ("HOME-PLUS", "burst_pipe", "", False, ("photos", "repair_estimate")),
        ("HOME-PLUS", "flood", "", False, ("photos",)),
    ),
)
POLICY_COUNT = 50
