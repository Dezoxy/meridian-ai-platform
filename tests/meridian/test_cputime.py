"""``cputime.growth``: what it answers, and how a noisy measurement is retaken.

A measurement that fails the limit is taken again, up to ``ATTEMPTS`` times,
and the least growth answers (S074, H4): noise from a busy machine does not
repeat, a quadratic call measures sixteen every time. The retry tests script
``best_time`` (the thread's CPU time of a call) so they depend on no clock; the
two tests of real calls prove the helper still tells the two shapes apart.
"""

from collections.abc import Callable

import cputime
import pytest
from cputime import ATTEMPTS, MAX_GROWTH, MIN_SMALL_SECONDS, growth, measure_growth

# Scripted times are whole multiples of one half second, so a ratio of two of
# them is exact in floating point and compares with the limit without rounding.
SMALL = 0.5


def script(monkeypatch: pytest.MonkeyPatch, ratios: list[float]) -> list[float]:
    """Make ``best_time`` answer, in order, the small and the large time of one
    attempt per ratio; the times of the attempts not yet taken stay in the list
    that is returned."""
    left = [time for ratio in ratios for time in (SMALL, SMALL * ratio)]

    def best_time(call: Callable[[int], object], argument: int) -> float:
        return left.pop(0)

    monkeypatch.setattr(cputime, "best_time", best_time)
    return left


def nothing(_: int) -> None:
    return None


def test_a_measurement_under_the_limit_is_taken_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left = script(monkeypatch, [4.0, 4.0])

    grown, attempts = measure_growth(nothing, 1, 4)

    assert (grown, attempts) == (4.0, 1)
    assert len(left) == 2


def test_a_measurement_over_the_limit_is_retaken_and_the_least_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script(monkeypatch, [MAX_GROWTH + 1, 4.0])

    grown, attempts = measure_growth(nothing, 1, 4)

    assert (grown, attempts) == (4.0, 2)


def test_a_measurement_at_the_limit_is_retaken_because_callers_require_less(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script(monkeypatch, [MAX_GROWTH, 4.0])

    grown, attempts = measure_growth(nothing, 1, 4)

    assert (grown, attempts) == (4.0, 2)


def test_the_retaking_is_bounded_and_the_least_of_the_attempts_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ratios = [16.0 - attempt for attempt in range(ATTEMPTS)]
    left = script(monkeypatch, [*ratios, 2.0])

    grown, attempts = measure_growth(nothing, 1, 4)

    assert (grown, attempts) == (min(ratios), ATTEMPTS)
    assert min(ratios) >= MAX_GROWTH
    assert left == [SMALL, SMALL * 2.0], "an attempt past the bound was taken"


def test_growth_answers_the_least_measurement_as_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script(monkeypatch, [MAX_GROWTH + 4, 5.0])

    assert growth(nothing, 1, 4) == 5.0


def test_a_small_run_shorter_than_the_floor_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cputime, "best_time", lambda call, argument: 0.0)

    with pytest.raises(AssertionError, match="too little to time"):
        measure_growth(nothing, 1, 4)


def test_a_small_run_as_long_as_the_floor_is_timed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter([MIN_SMALL_SECONDS * 2, MIN_SMALL_SECONDS * 8])
    monkeypatch.setattr(cputime, "best_time", lambda call, argument: next(times))

    assert measure_growth(nothing, 1, 4) == (4.0, 1)


def linear(size: int) -> None:
    total = 0
    for step in range(size):
        total += step


def quadratic(size: int) -> None:
    for _ in range(size):
        for _ in range(size):
            pass


def test_a_linear_call_measures_under_the_limit() -> None:
    grown, _ = measure_growth(linear, 50_000, 200_000)

    assert grown < MAX_GROWTH


def test_a_quadratic_call_measures_over_the_limit_on_every_attempt() -> None:
    grown, attempts = measure_growth(quadratic, 400, 1_600)

    assert grown > MAX_GROWTH
    assert attempts == ATTEMPTS
