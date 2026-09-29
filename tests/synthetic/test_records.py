"""Date arithmetic and the quiet-history rules of the record builders."""

import random
from datetime import date, timedelta

import pytest
from generator import builders, catalogue, records

FIRST_DAY = date(2026, 1, 1)
LAST_DAY = date(2030, 12, 31)
FUZZ_SEEDS = range(200)


def make_rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 (seeded, not secret)


def days(first: date, last: date):
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


# -- one-year terms -----------------------------------------------------------
@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2026, 1, 1), date(2026, 12, 31)),
        (date(2026, 3, 1), date(2027, 2, 28)),
        (date(2027, 3, 1), date(2028, 2, 29)),
        (date(2028, 1, 1), date(2028, 12, 31)),
        (date(2028, 2, 29), date(2029, 2, 28)),
    ],
)
def test_add_year_gives_the_last_day_of_a_one_year_term(start, end):
    assert records.add_year(start) == end


def test_a_term_built_backwards_from_its_end_ends_there_across_leap_days():
    for start in days(FIRST_DAY, LAST_DAY):
        end = records.add_year(start)
        assert records.add_year(records.start_of_term_ending(end)) == end, end


def test_no_term_ends_on_the_day_before_a_leap_day():
    with pytest.raises(ValueError, match="no one-year term"):
        records.start_of_term_ending(date(2028, 2, 28))


def test_an_expired_next_day_loss_is_exactly_one_day_after_the_end_date():
    for seed in FUZZ_SEEDS:
        rng = make_rng(seed)
        loss, start, lapsed_on = builders._inactive_dates(rng, "expired_next_day")
        assert records.add_year(start) + timedelta(days=1) == loss, seed
        assert lapsed_on is None


# -- quiet history ------------------------------------------------------------
@pytest.mark.parametrize(
    ("end_date", "lapsed_on", "last_day"),
    [
        ("2026-11-30", None, date(2026, 11, 30)),  # in force at the reference date
        ("2026-08-09", None, date(2026, 8, 9)),  # the term ended before it
        ("2026-11-30", "2026-04-10", date(2026, 4, 9)),  # lapsed: the day before
        ("2026-03-01", "2026-04-10", date(2026, 3, 1)),  # the term ended first
    ],
)
def test_quiet_history_stays_inside_the_term_and_before_any_lapse(
    end_date, lapsed_on, last_day
):
    # Arrange
    policy = {
        "policy_number": "POL-0001",
        "product": "MOTOR-COMP",
        "end_date": end_date,
        "lapsed_on": lapsed_on,
    }
    anchor = catalogue.REFERENCE_DATE

    # Act
    dated = [
        date.fromisoformat(entry["loss_date"])
        for seed in FUZZ_SEEDS
        for entry in records.quiet_history(
            records.Context(make_rng(seed), ()), policy, anchor
        )
    ]

    # Assert
    assert records.last_history_day(policy) == last_day
    assert dated, "the fuzz should have produced some history"
    assert max(dated) <= last_day
