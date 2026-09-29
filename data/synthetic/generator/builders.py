"""Scenario builders: policies, history and claims made outcome first.

Each builder receives a plan entry and a policy number and creates the facts
that should produce the intended reason. It never decides the outcome; the
assembly step runs the oracle on what was built and stops on a mismatch.
"""

import random
from datetime import date, timedelta

from . import catalogue
from .catalogue import PRODUCTS
from .plan import ScenarioSpec
from .records import (
    LOSS_FIRST,
    LOSS_LAST,
    MAX_OTHER_CLAIMED,
    MAX_PROMPT_REPORT_DAYS,
    MAX_TENURE_DAYS,
    MIN_ABOVE_DEDUCTIBLE,
    MIN_OTHER_CLAIMED,
    MIN_TENURE_DAYS,
    Context,
    Record,
    Scenario,
    add_year,
    burst_history,
    claimed_in,
    make_claim,
    make_policy,
    other_claimed,
    quiet_history,
    random_day,
    standard_dates,
    start_of_term_ending,
)

MIN_WITHIN_PAYABLE = 300
MIN_OVER_PAYABLE = catalogue.AUTO_APPROVAL_LIMIT + 1
MAX_OVER_PAYABLE = 25_000
LARGE_FRAUD_MIN_PAYABLE = 2_600


def build_within(ctx: Context, spec: ScenarioSpec, number: int) -> Scenario:
    rng = ctx.rng
    deductible = PRODUCTS[spec.product].deductible
    loss, reported, start = standard_dates(rng)
    lapsed_on = None
    if spec.variant == "lapsed_later":
        lapsed_on = min(
            catalogue.REFERENCE_DATE, loss + timedelta(days=rng.randint(10, 40))
        )
    policy = make_policy(ctx, number, spec.product, start, lapsed_on=lapsed_on)
    if spec.variant == "boundary":
        claimed = catalogue.AUTO_APPROVAL_LIMIT + deductible
    else:
        claimed = claimed_in(
            rng,
            MIN_WITHIN_PAYABLE + deductible,
            catalogue.AUTO_APPROVAL_LIMIT + deductible,
        )
    claim = make_claim(ctx, policy, spec.peril, loss, reported, claimed)
    history = quiet_history(ctx, policy, loss)
    return Scenario(policy, history, claim, None, "within_threshold")


def build_over(ctx: Context, spec: ScenarioSpec, number: int) -> Scenario:
    rng = ctx.rng
    product = PRODUCTS[spec.product]
    loss, reported, start = standard_dates(rng)
    sum_insured = None
    if spec.variant == "clipped":
        sum_insured = rng.randrange(9_000, 20_001, 500)
    policy = make_policy(ctx, number, spec.product, start, sum_insured=sum_insured)
    if spec.variant == "boundary":
        claimed = MIN_OVER_PAYABLE + product.deductible
    elif spec.variant == "clipped":
        claimed = policy["limit"] + rng.randrange(500, 3_001, 100)
    else:
        top = min(policy["limit"], MAX_OVER_PAYABLE)
        claimed = claimed_in(rng, MIN_OVER_PAYABLE + product.deductible, top)
    claim = make_claim(ctx, policy, spec.peril, loss, reported, claimed)
    history = quiet_history(ctx, policy, loss)
    return Scenario(policy, history, claim, None, "over_threshold")


def build_fraud(ctx: Context, spec: ScenarioSpec, number: int) -> Scenario:
    rng = ctx.rng
    deductible = PRODUCTS[spec.product].deductible
    loss, reported, start = standard_dates(rng)
    if spec.variant == "early_loss":
        start = loss - timedelta(days=rng.randint(3, catalogue.EARLY_LOSS_DAYS - 5))
    elif spec.variant == "frequent_claims":
        start = loss - timedelta(days=rng.randint(200, MAX_TENURE_DAYS))
    elif spec.variant == "late_report":
        delay = rng.randint(catalogue.REPORTING_WINDOW_DAYS + 1, 50)
        latest = min(LOSS_LAST, catalogue.REFERENCE_DATE - timedelta(days=delay))
        loss = random_day(rng, LOSS_FIRST, latest)
        reported = loss + timedelta(days=delay)
        start = loss - timedelta(days=rng.randint(MIN_TENURE_DAYS, MAX_TENURE_DAYS))
    else:
        raise ValueError(f"unknown fraud variant {spec.variant!r}")
    if spec.large:
        claimed = claimed_in(
            rng, LARGE_FRAUD_MIN_PAYABLE + deductible, MAX_OTHER_CLAIMED
        )
    else:
        top = catalogue.AUTO_APPROVAL_LIMIT
        claimed = claimed_in(
            rng, max(MIN_OTHER_CLAIMED, deductible + MIN_ABOVE_DEDUCTIBLE), top
        )
    policy = make_policy(ctx, number, spec.product, start)
    claim = make_claim(
        ctx,
        policy,
        spec.peril,
        loss,
        reported,
        claimed,
        delayed=spec.variant == "late_report",
    )
    if spec.variant == "early_loss":
        history = []
    elif spec.variant == "frequent_claims":
        history = burst_history(ctx, policy, loss, rng.choice((2, 3)))
    else:
        history = quiet_history(ctx, policy, loss)
    return Scenario(policy, history, claim, None, "fraud_indicator", (spec.variant,))


def build_excluded(ctx: Context, spec: ScenarioSpec, number: int) -> Scenario:
    rng = ctx.rng
    product = PRODUCTS[spec.product]
    exclusion = {e.code: e for e in product.exclusions}[spec.variant]
    circumstance = (
        spec.variant if exclusion.kind == catalogue.KIND_CIRCUMSTANCE else None
    )
    loss, reported, start = standard_dates(rng)
    policy = make_policy(ctx, number, spec.product, start)
    claimed = other_claimed(rng, product.deductible)
    claim = make_claim(
        ctx, policy, spec.peril, loss, reported, claimed, circumstance=circumstance
    )
    history = quiet_history(ctx, policy, loss)
    return Scenario(
        policy, history, claim, circumstance, "excluded", exclusion=spec.variant
    )


def _inactive_dates(rng: random.Random, variant: str) -> tuple[date, date, date | None]:
    """Loss date, policy start and lapse date for a policy that is not in force."""
    if variant == "not_started":
        # The policy must start on or before the reference date.
        wait = rng.randint(5, 40)
        latest = min(LOSS_LAST, catalogue.REFERENCE_DATE - timedelta(days=wait))
        loss = random_day(rng, LOSS_FIRST, latest)
        return loss, loss + timedelta(days=wait), None
    loss = random_day(rng, LOSS_FIRST, LOSS_LAST)
    if variant in ("expired", "expired_next_day"):
        days_after_end = 1 if variant == "expired_next_day" else rng.randint(10, 90)
        end = loss - timedelta(days=days_after_end)
        return loss, start_of_term_ending(end), None
    if variant in ("lapsed", "lapsed_same_day"):
        start = loss - timedelta(days=rng.randint(90, MAX_TENURE_DAYS))
        days_before = 0 if variant == "lapsed_same_day" else rng.randint(5, 60)
        return loss, start, loss - timedelta(days=days_before)
    raise ValueError(f"unknown inactive variant {variant!r}")


def build_inactive(ctx: Context, spec: ScenarioSpec, number: int) -> Scenario:
    rng = ctx.rng
    product = PRODUCTS[spec.product]
    loss, start, lapsed_on = _inactive_dates(rng, spec.variant)
    reported = loss + timedelta(days=rng.randint(0, MAX_PROMPT_REPORT_DAYS))
    policy = make_policy(ctx, number, spec.product, start, lapsed_on=lapsed_on)
    claim = make_claim(
        ctx, policy, spec.peril, loss, reported, other_claimed(rng, product.deductible)
    )
    history = quiet_history(ctx, policy, loss)
    return Scenario(policy, history, claim, None, "policy_inactive")


def build_missing(ctx: Context, spec: ScenarioSpec, number: int) -> Scenario:
    rng = ctx.rng
    product = PRODUCTS[spec.product]
    loss, reported, start = standard_dates(rng)
    policy = make_policy(ctx, number, spec.product, start)
    provided = tuple(
        document
        for document in catalogue.REQUIRED_DOCUMENTS[spec.peril]
        if document not in spec.missing
    )
    claim = make_claim(
        ctx,
        policy,
        spec.peril,
        loss,
        reported,
        other_claimed(rng, product.deductible),
        documents=provided,
    )
    history = quiet_history(ctx, policy, loss)
    return Scenario(
        policy, history, claim, None, "missing_documents", missing=spec.missing
    )


BUILDERS = {
    "within_threshold": build_within,
    "over_threshold": build_over,
    "fraud_indicator": build_fraud,
    "excluded": build_excluded,
    "policy_inactive": build_inactive,
    "missing_documents": build_missing,
}


def build_background(
    ctx: Context, number: int, index: int
) -> tuple[Record, list[Record]]:
    """A policy without a claim, and its history."""
    rng = ctx.rng
    product_code = tuple(PRODUCTS)[index % len(PRODUCTS)]
    start = random_day(rng, date(2025, 5, 1), date(2026, 7, 15))
    lapsed_on = None
    if rng.randrange(5) == 0:
        last = min(add_year(start), catalogue.REFERENCE_DATE)
        lapsed_on = start + timedelta(
            days=rng.randint(20, max(20, (last - start).days))
        )
    policy = make_policy(ctx, number, product_code, start, lapsed_on=lapsed_on)
    return policy, quiet_history(ctx, policy, catalogue.REFERENCE_DATE)
