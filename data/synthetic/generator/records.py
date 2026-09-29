"""Building blocks for the scenario builders: dates, amounts and records.

A record is a JSON-shaped dict in the field order of its output file. A claim is
built without its ID and a history entry without its ID; the assembly step
numbers them once the whole dataset exists.
"""

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, NamedTuple

from . import catalogue, narratives, people
from .catalogue import PRODUCTS, Product

Record = dict[str, Any]

LOSS_FIRST = date(2026, 5, 1)
LOSS_LAST = date(2026, 8, 25)
MAX_PROMPT_REPORT_DAYS = 4
# A plain policy started this long before the loss: past the early-loss window,
# and still in force on the loss date (a term is 365 days).
MIN_TENURE_DAYS = 60
MAX_TENURE_DAYS = 300
# Claimed amounts that are not tied to a reason: 400 to 6000, above the deductible.
MIN_OTHER_CLAIMED = 400
MAX_OTHER_CLAIMED = 6_000
MIN_ABOVE_DEDUCTIBLE = 100
AMOUNT_STEP = 10
ONE_YEAR_DAYS = 365


@dataclass(frozen=True)
class Context:
    """The one random source and the registrations, indexed by policy number - 1."""

    rng: random.Random
    registrations: tuple[str, ...]


class Scenario(NamedTuple):
    """A built scenario: records without their ID, and the outcome intended."""

    policy: Record
    history: list[Record]
    claim: Record
    circumstance: str | None
    reason: str
    indicators: tuple[str, ...] = ()
    exclusion: str | None = None
    missing: tuple[str, ...] = ()


# -- dates --------------------------------------------------------------------
def add_year(start: date) -> date:
    """The last day of a one-year term that begins on ``start``."""
    try:
        following = start.replace(year=start.year + 1)
    except ValueError:  # 29 February
        following = date(start.year + 1, 3, 1)
    return following - timedelta(days=1)


def start_of_term_ending(end: date) -> date:
    """The start date of the one-year term whose last day is ``end``.

    The inverse of ``add_year``, so a term built backwards from its end date
    ends exactly there, whether or not it spans a leap day.
    """
    for days in (ONE_YEAR_DAYS - 1, ONE_YEAR_DAYS):
        start = end - timedelta(days=days)
        if add_year(start) == end:
            return start
    raise ValueError(f"no one-year term ends on {end}")


def random_day(rng: random.Random, first: date, last: date) -> date:
    return first + timedelta(days=rng.randint(0, (last - first).days))


def standard_dates(rng: random.Random) -> tuple[date, date, date]:
    """Loss, report and policy start of a plain claim: prompt, well into the term."""
    loss = random_day(rng, LOSS_FIRST, LOSS_LAST)
    reported = loss + timedelta(days=rng.randint(0, MAX_PROMPT_REPORT_DAYS))
    start = loss - timedelta(days=rng.randint(MIN_TENURE_DAYS, MAX_TENURE_DAYS))
    return loss, reported, start


# -- amounts ------------------------------------------------------------------
def claimed_in(rng: random.Random, low: int, high: int) -> int:
    """A multiple of ten from ``low`` to ``high``, both rounded inwards."""
    first = -(-low // AMOUNT_STEP) * AMOUNT_STEP
    last = high // AMOUNT_STEP * AMOUNT_STEP
    return rng.randrange(first, last + 1, AMOUNT_STEP)


def other_claimed(rng: random.Random, deductible: int) -> int:
    """400 to 6000, and always above the deductible."""
    low = max(MIN_OTHER_CLAIMED, deductible + MIN_ABOVE_DEDUCTIBLE)
    return claimed_in(rng, low, MAX_OTHER_CLAIMED)


# -- records ------------------------------------------------------------------
def make_policy(
    ctx: Context,
    number: int,
    product_code: str,
    start: date,
    *,
    lapsed_on: date | None = None,
    sum_insured: int | None = None,
) -> Record:
    rng = ctx.rng
    product = PRODUCTS[product_code]
    holder = people.make_holder(rng, number)
    if product.sum_insured_range is None:
        sum_insured = None
    elif sum_insured is None:
        low, high, step = product.sum_insured_range
        sum_insured = rng.randrange(low, high + 1, step)
    if product.line == "motor":
        insured_object = people.make_vehicle(rng, ctx.registrations[number - 1])
    else:
        insured_object = people.make_building(rng, holder["address"])
    return {
        "policy_number": f"POL-{number:04d}",
        "product": product.code,
        "wording_version": catalogue.WORDING_VERSION,
        "holder": holder,
        "start_date": start.isoformat(),
        "end_date": add_year(start).isoformat(),
        "status": "active" if lapsed_on is None else "lapsed",
        "lapsed_on": None if lapsed_on is None else lapsed_on.isoformat(),
        "deductible": product.deductible,
        "sum_insured": sum_insured,
        "limit": product.fixed_limit
        if product.fixed_limit is not None
        else sum_insured,
        "insured_object": insured_object,
    }


def make_claim(
    ctx: Context,
    policy: Record,
    peril: str,
    loss: date,
    reported: date,
    claimed: int,
    *,
    documents: tuple[str, ...] | None = None,
    circumstance: str | None = None,
    delayed: bool = False,
) -> Record:
    """A first-notice-of-loss record without its ID: only what a claimant submits."""
    if reported > catalogue.REFERENCE_DATE:
        raise ValueError(
            f"{policy['policy_number']}: reported after the reference date"
        )
    line = PRODUCTS[policy["product"]].line
    holder = policy["holder"]
    if line == "motor":
        vehicle = policy["insured_object"]
        subject = f"my {vehicle['make']} {vehicle['model']}"
        place = people.random_place(ctx.rng)
    else:
        subject = f"my {policy['insured_object']['building_type']}"
        place = {key: holder["address"][key] for key in ("city", "country")}
    provided = catalogue.REQUIRED_DOCUMENTS[peril] if documents is None else documents
    description = narratives.describe(
        ctx.rng,
        line,
        peril,
        subject=subject,
        city=place["city"],
        loss_date=loss,
        circumstance=circumstance,
        delayed=delayed,
    )
    return {
        "policy_number": policy["policy_number"],
        "reported_on": reported.isoformat(),
        "loss_date": loss.isoformat(),
        "peril": peril,
        "claimed_amount": claimed,
        "loss_location": place,
        "claimant": {"name": holder["name"], "email": holder["email"]},
        "description": description,
        "documents": list(provided),
    }


def history_entry(
    rng: random.Random, policy_number: str, product: Product, day: date
) -> Record:
    return {
        "policy_number": policy_number,
        "loss_date": day.isoformat(),
        "peril": rng.choice(product.covered),
        "paid_amount": rng.randrange(200, 4501, AMOUNT_STEP),
        "status": "closed",
    }


def last_history_day(policy: Record) -> date:
    """The last day a closed claim can be dated: in the term, before any lapse."""
    last = date.fromisoformat(policy["end_date"])
    if policy["lapsed_on"] is not None:
        lapse = date.fromisoformat(policy["lapsed_on"])
        last = min(last, lapse - timedelta(days=1))
    return last


def quiet_history(ctx: Context, policy: Record, anchor: date) -> list[Record]:
    """Background history that never makes a claim on ``anchor`` frequent.

    Nothing, a single entry inside the year before ``anchor``, or two entries
    older than that year (the policy is a renewal, so its history can predate
    the current term). ``anchor`` is clamped so that no entry is dated after the
    term ended or on or after the lapse date.
    """
    rng = ctx.rng
    product = PRODUCTS[policy["product"]]
    anchor = min(anchor, last_history_day(policy))
    style = rng.randrange(4)
    if style < 2:
        return []
    if style == 2:
        days_before = [rng.randint(20, catalogue.FREQUENT_CLAIMS_WINDOW_DAYS - 25)]
    else:
        oldest = 3 * catalogue.FREQUENT_CLAIMS_WINDOW_DAYS
        days_before = rng.sample(
            range(catalogue.FREQUENT_CLAIMS_WINDOW_DAYS + 1, oldest), 2
        )
    return [
        history_entry(rng, policy["policy_number"], product, anchor - timedelta(days=n))
        for n in sorted(days_before, reverse=True)
    ]


def burst_history(ctx: Context, policy: Record, loss: date, count: int) -> list[Record]:
    """``count`` closed claims inside the current term, within a year of the loss."""
    rng = ctx.rng
    product = PRODUCTS[policy["product"]]
    start = date.fromisoformat(policy["start_date"])
    term_days = (loss - start).days
    offsets = sorted(rng.sample(range(5, term_days - 4), count))
    return [
        history_entry(rng, policy["policy_number"], product, start + timedelta(days=n))
        for n in offsets
    ]
