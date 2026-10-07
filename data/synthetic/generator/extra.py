"""Scenarios drawn from a second random stream, so the first forty stay frozen.

The golden set's first forty claims were drawn from one ``random.Random(seed)``.
A recorded model answer is keyed by the text of those claims, so a scenario
added to that stream would move the text of claims after it and lose answers
that only a paid recording brings back. The scenarios here are drawn from a
second stream, seeded from the same seed and a fixed label, once the first
stream is spent: nothing the first forty depend on can move. Their policies
take the numbers after the last of the first set, so no policy, history entry
or claim of the first set changes its ID either.

None of these claims is one the triage model is asked about. A claim on a
policy in force asks the model only when a circumstance exclusion of the
product names the peril, and none of the perils in ``EXTRA_PLAN`` is one; an
unknown policy has no cover to read. So no recorded answer is needed, and the
oracle's proposal is the expectation.
"""

import random
from datetime import date, timedelta

from . import catalogue
from .catalogue import PRODUCTS
from .plan import EXTRA_PLAN, FIRST_EXTRA_POLICY, UNKNOWN_POLICY, ExtraSpec
from .records import (
    LOSS_FIRST,
    LOSS_LAST,
    MAX_TENURE_DAYS,
    MIN_ABOVE_DEDUCTIBLE,
    MIN_OTHER_CLAIMED,
    MIN_TENURE_DAYS,
    Context,
    Scenario,
    claimed_in,
    history_entry,
    make_claim,
    make_policy,
    other_claimed,
    quiet_history,
    random_day,
    standard_dates,
)

STREAM_LABEL = "extra-scenarios"
# A policy number no policy has is drawn from this range; the policies have
# POL-0001 to POL-0056.
UNKNOWN_POLICY_FIRST, UNKNOWN_POLICY_LAST = 9000, 9999
FIRST_REGISTRATION, LAST_REGISTRATION = 1000, 9999
MIN_FREQUENT_TENURE_DAYS = 200


def stream(seed: int) -> random.Random:
    """The second stream. A string seed is hashed the same on every run and
    every machine, whatever ``PYTHONHASHSEED`` is."""
    return random.Random(f"{seed}:{STREAM_LABEL}")


def _new_registrations(
    rng: random.Random, taken: tuple[str, ...], count: int
) -> tuple[str, ...]:
    """Registrations in the invented format that no earlier policy has."""
    free = [
        f"SYN-{number:04d}"
        for number in range(FIRST_REGISTRATION, LAST_REGISTRATION + 1)
        if f"SYN-{number:04d}" not in taken
    ]
    return tuple(rng.sample(free, count))


def build_extra(seed: int, first: Context) -> list[Scenario]:
    """The scenarios of ``EXTRA_PLAN`` for ``seed``, in a shuffled order.

    ``first`` is the context of the first stream, spent by now; only its
    registrations are read, so that the new policies share none with it.
    """
    rng = stream(seed)
    with_policy = sum(1 for spec in EXTRA_PLAN if spec.kind != UNKNOWN_POLICY)
    registrations = first.registrations + _new_registrations(
        rng, first.registrations, with_policy
    )
    ctx = Context(rng, registrations)
    plan = rng.sample(EXTRA_PLAN, len(EXTRA_PLAN))
    first_number = FIRST_EXTRA_POLICY
    numbers = iter(
        rng.sample(range(first_number, first_number + with_policy), with_policy)
    )
    return [
        build_unknown_policy(ctx, spec)
        if spec.kind == UNKNOWN_POLICY
        else build_boundary(ctx, spec, next(numbers))
        for spec in plan
    ]


def _boundary_dates(ctx: Context, spec: ExtraSpec) -> tuple[date, date, date]:
    """Loss date, report date and policy start, with the indicator of ``spec``
    on its boundary (flagged) or one day off it."""
    rng = ctx.rng
    loss, reported, start = standard_dates(rng)
    if spec.kind == "early_loss":
        days = catalogue.EARLY_LOSS_DAYS + (0 if spec.flagged else 1)
        start = loss - timedelta(days=days)
    elif spec.kind == "late_report":
        delay = catalogue.REPORTING_WINDOW_DAYS + (1 if spec.flagged else 0)
        latest = min(LOSS_LAST, catalogue.REFERENCE_DATE - timedelta(days=delay))
        loss = random_day(rng, LOSS_FIRST, latest)
        reported = loss + timedelta(days=delay)
        start = loss - timedelta(days=rng.randint(MIN_TENURE_DAYS, MAX_TENURE_DAYS))
    else:
        start = loss - timedelta(
            days=rng.randint(MIN_FREQUENT_TENURE_DAYS, MAX_TENURE_DAYS)
        )
    return loss, reported, start


def build_boundary(ctx: Context, spec: ExtraSpec, number: int) -> Scenario:
    """A claim with one indicator on its boundary, or one day off it.

    Early loss: the loss is 30 days after the start (flagged) or 31. Late
    report: the report is 31 days after the loss (flagged) or 30. Frequent
    claims: two earlier claims, the older exactly 365 days before the loss
    (flagged) or 366, which leaves one inside the window. The payable amount
    never passes the auto-approval limit, so the one flagged indicator is the
    only reason to refer the claim, and a claim one day off goes through.
    """
    rng = ctx.rng
    product = PRODUCTS[spec.product]
    loss, reported, start = _boundary_dates(ctx, spec)
    claimed = claimed_in(
        rng,
        max(MIN_OTHER_CLAIMED, product.deductible + MIN_ABOVE_DEDUCTIBLE),
        catalogue.AUTO_APPROVAL_LIMIT,
    )
    policy = make_policy(ctx, number, spec.product, start)
    claim = make_claim(
        ctx,
        policy,
        spec.peril,
        loss,
        reported,
        claimed,
        delayed=spec.kind == "late_report",
    )
    if spec.kind == "early_loss":
        history = []
    elif spec.kind == "frequent_claims":
        oldest = catalogue.FREQUENT_CLAIMS_WINDOW_DAYS + (0 if spec.flagged else 1)
        history = [
            history_entry(
                rng, policy["policy_number"], product, loss - timedelta(days=n)
            )
            for n in (oldest, 1)
        ]
    else:
        history = quiet_history(ctx, policy, loss)
    if not spec.flagged:
        return Scenario(policy, history, claim, None, "within_threshold")
    return Scenario(policy, history, claim, None, "fraud_indicator", (spec.kind,))


def build_unknown_policy(ctx: Context, spec: ExtraSpec) -> Scenario:
    """A claim whose policy number no policy has.

    The claimant is a stand-in holder, made and then dropped: only the claim is
    kept. The stand-in is on a home product, which needs no vehicle
    registration for a number outside the policies'.
    """
    rng = ctx.rng
    product = PRODUCTS[spec.product]
    if product.line != "home":
        raise ValueError("the stand-in holder of an unknown policy is a home policy")
    number = rng.randint(UNKNOWN_POLICY_FIRST, UNKNOWN_POLICY_LAST)
    loss, reported, start = standard_dates(rng)
    stand_in = make_policy(ctx, number, spec.product, start)
    claim = make_claim(
        ctx,
        stand_in,
        spec.peril,
        loss,
        reported,
        other_claimed(rng, product.deductible),
    )
    return Scenario(None, [], claim, None, "policy_not_found")
