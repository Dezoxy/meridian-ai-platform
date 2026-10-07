"""Time a call by the CPU time of its thread, and the growth of that time.

A test that shows a call is linear compares its time on a small input with its
time on one four times larger: linear growth is 4, quadratic 16. The limit sits
between them, at twice the linear growth, so that timer noise does not fail a
linear run.

A measurement that fails the limit is taken again, up to ``ATTEMPTS`` times, and
the least growth is the answer (S074). On a busy machine a thread's CPU time is
not steady: the thread resumes after a preemption with cold caches, and a
neighbour on the same core slows it, so every run of a window of some tens of
milliseconds can take twice as long as in a quiet one, and the least of five
runs inside that window is no help. A linear call measured 8.52 against 8 that
way, at a load of 10. The slow window ends; a quadratic call measures sixteen in
every window, so taking it again never lets one through (and a measurement at
twice the limit is not taken again).

What the retry costs in what the tests catch: the least of up to four attempts
passes growth the single attempt mostly failed, so a test now catches growth of
roughly n^1.7 (10.6 times for four times the input) and worse, not n^1.5 (8
times): at a load of 9 to 12, 87 % of n^1.5 runs passed with the retry (25 %
without), 2.5 % of n^1.7 runs, and no quadratic run (S074 review).
"""

import time
from collections.abc import Callable

RUNS = 5
MAX_GROWTH = 8
ATTEMPTS = 4
# A growth this far over the limit is not noise: a real quadratic call measures
# sixteen, twice the limit, so it fails at the first attempt, not the fourth.
GIVE_UP_GROWTH = 2 * MAX_GROWTH
# The small run must take far longer than the clock can tell apart, or a ratio
# of two tiny numbers wanders. The smallest small run of the callers is about
# 0.3 ms (measured at a load of 10, S074); this sits at a third of that, so that
# a faster machine does not turn the floor into a test of its speed.
MIN_SMALL_SECONDS = 1e-4
MIN_CLOCK_TICKS = 100


def best_time[T](call: Callable[[T], object], argument: T) -> float:
    """The least CPU time of ``RUNS`` runs of ``call(argument)``.

    The thread's CPU time, not the wall clock: under parallel workers (S054)
    the wall clock also counts the time a test waited for a CPU, which
    measured a linear run at 9 to 11 times.
    """
    best = float("inf")
    for _ in range(RUNS):
        started = time.thread_time()
        call(argument)
        best = min(best, time.thread_time() - started)
    return best


def measure_growth[T](
    call: Callable[[T], object], small: T, large: T
) -> tuple[float, int]:
    """The least growth of ``ATTEMPTS`` measurements at most, and how many were
    taken: one when the first is under ``MAX_GROWTH``."""
    floor = max(
        MIN_SMALL_SECONDS,
        MIN_CLOCK_TICKS * time.get_clock_info("thread_time").resolution,
    )
    least = float("inf")
    for attempt in range(1, ATTEMPTS + 1):
        small_time = best_time(call, small)
        assert small_time > floor, (
            f"the small input takes {small_time:.2e} s of CPU, too little to "
            f"time (the floor is {floor:.2e} s): use a longer one"
        )
        least = min(least, best_time(call, large) / small_time)
        if least < MAX_GROWTH or least >= GIVE_UP_GROWTH:
            return least, attempt
    return least, ATTEMPTS


def growth[T](call: Callable[[T], object], small: T, large: T) -> float:
    """How many times longer ``call`` takes on ``large`` than on ``small``: the
    least of the measurements ``measure_growth`` takes."""
    return measure_growth(call, small, large)[0]
