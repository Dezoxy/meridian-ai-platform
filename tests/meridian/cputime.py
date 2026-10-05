"""Time a call by the CPU time of its thread, and the growth of that time.

A test that shows a call is linear compares its time on a small input with its
time on one four times larger: linear growth is 4, quadratic 16. The limit sits
between them, at twice the linear growth, so that timer noise does not fail a
linear run.
"""

import time
from collections.abc import Callable

RUNS = 5
MAX_GROWTH = 8
# The small run must take far longer than the clock can tell apart, or a ratio
# of two tiny numbers wanders.
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


def growth[T](call: Callable[[T], object], small: T, large: T) -> float:
    """How many times longer ``call`` takes on ``large`` than on ``small``."""
    small_time = best_time(call, small)
    floor = MIN_CLOCK_TICKS * time.get_clock_info("thread_time").resolution
    assert small_time > floor, (
        f"the small input takes {small_time:.2e} s of CPU, too little to time "
        f"(clock resolution times {MIN_CLOCK_TICKS} is {floor:.2e} s): "
        "use a longer one"
    )
    return best_time(call, large) / small_time
