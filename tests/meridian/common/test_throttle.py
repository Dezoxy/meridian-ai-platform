"""The audit throttle of refusals (T-49): pure, on a fake clock.

Moved from the gateway's rate-limit tests (S013) when the throttle moved to
``platform/common``, because the tool servers use it too.

``due`` starts the window at once, so refusals that overlap at the start of a
window cannot all be due; ``release`` ends it again when the row could not be
written, and the count carries what was lost.
"""

import sys
import threading
from collections.abc import Callable, Iterator

import pytest
from servicesupport import FakeClock

from meridian.platform.common.throttle import (
    REFUSAL_AUDIT_SECONDS,
    RefusalAuditThrottle,
)

TENANT = "claims-triage"
OTHER_TENANT = "evaluation"
RACERS = 16
ROUNDS = 50
OVERLAPPING = 8


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fast_switching() -> Iterator[None]:
    """Make the interpreter switch threads about a million times a second, so a
    missing lock shows up within a few rounds and not once in a million."""
    before = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(before)


def run_threads(call: Callable[[], None], threads: int = RACERS) -> None:
    start = threading.Barrier(threads)

    def wait_then_call() -> None:
        start.wait(timeout=30)
        call()

    racers = [threading.Thread(target=wait_then_call) for _ in range(threads)]
    for thread in racers:
        thread.start()
    for thread in racers:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in racers)


REASON = "tenant-request-rate"


def test_the_first_refusal_of_a_key_is_due_with_a_count_of_zero(
    clock: FakeClock,
) -> None:
    throttle = RefusalAuditThrottle(clock=clock)

    assert throttle.due(TENANT, REASON) == 0


def test_once_a_refusal_is_due_the_rest_of_the_window_is_suppressed(
    clock: FakeClock,
) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    assert throttle.due(TENANT, REASON) == 0

    answers = [throttle.due(TENANT, REASON) for _ in range(19)]

    assert answers == [None] * 19


def test_the_next_row_after_the_window_carries_the_count_of_the_suppressed(
    clock: FakeClock,
) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    assert throttle.due(TENANT, REASON) == 0
    for _ in range(19):
        assert throttle.due(TENANT, REASON) is None

    clock.advance(REFUSAL_AUDIT_SECONDS)

    assert throttle.due(TENANT, REASON) == 19
    assert throttle.due(TENANT, REASON) is None  # a new window, a new count
    clock.advance(REFUSAL_AUDIT_SECONDS)
    assert throttle.due(TENANT, REASON) == 1


def test_a_refusal_just_inside_the_window_is_suppressed_and_one_at_its_end_is_due(
    clock: FakeClock,
) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    throttle.due(TENANT, REASON)

    clock.advance(REFUSAL_AUDIT_SECONDS - 0.5)
    inside = throttle.due(TENANT, REASON)
    clock.advance(0.5)
    at_the_end = throttle.due(TENANT, REASON)

    assert (inside, at_the_end) == (None, 1)


def test_a_released_window_makes_the_next_refusal_due_with_the_loss(
    clock: FakeClock,
) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    carried = throttle.due(TENANT, REASON)
    assert carried == 0  # the row cannot be written

    throttle.release(TENANT, REASON, carried)

    assert throttle.due(TENANT, REASON) == 1  # its row stands for both


def test_a_release_carries_what_the_failed_row_carried(clock: FakeClock) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    throttle.due(TENANT, REASON)
    for _ in range(4):
        throttle.due(TENANT, REASON)  # four suppressed
    clock.advance(REFUSAL_AUDIT_SECONDS)
    carried = throttle.due(TENANT, REASON)
    assert carried == 4

    throttle.release(TENANT, REASON, carried)

    # the four it carried and the refusal whose row failed
    assert throttle.due(TENANT, REASON) == 5


def test_a_second_failure_keeps_adding_to_the_loss(clock: FakeClock) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    for expected in (0, 1, 2):
        carried = throttle.due(TENANT, REASON)
        assert carried == expected
        throttle.release(TENANT, REASON, carried)

    assert throttle.due(TENANT, REASON) == 3


def test_a_refusal_between_due_and_release_is_not_lost(clock: FakeClock) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    carried = throttle.due(TENANT, REASON)
    assert throttle.due(TENANT, REASON) is None  # arrives while the write runs

    throttle.release(TENANT, REASON, carried)

    # the refusal whose row failed and the one that arrived meanwhile
    assert throttle.due(TENANT, REASON) == 2


def test_a_released_window_is_closed_again_by_the_next_due(clock: FakeClock) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    carried = throttle.due(TENANT, REASON)
    assert carried == 0
    throttle.release(TENANT, REASON, carried)
    assert throttle.due(TENANT, REASON) == 1

    assert throttle.due(TENANT, REASON) is None


def test_a_refusal_inside_the_window_does_not_extend_it(clock: FakeClock) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    throttle.due(TENANT, REASON)
    clock.advance(REFUSAL_AUDIT_SECONDS - 1)
    throttle.due(TENANT, REASON)  # a flood must not keep its own row away

    clock.advance(1)

    assert throttle.due(TENANT, REASON) == 1


def test_each_tenant_and_each_reason_has_its_own_window(clock: FakeClock) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    throttle.due(TENANT, REASON)

    assert throttle.due(OTHER_TENANT, REASON) == 0
    assert throttle.due(TENANT, "tenant-token-rate") == 0
    assert throttle.due(TENANT, REASON) is None


def test_a_refusal_with_no_tenant_is_a_key_of_its_own(clock: FakeClock) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    assert throttle.due(None, "unknown-tenant") == 0

    assert throttle.due(None, "unknown-tenant") is None
    assert throttle.due(TENANT, "unknown-tenant") == 0


@pytest.mark.usefixtures("fast_switching")
def test_eight_threads_refusing_at_the_start_of_a_window_leave_one_due_answer(
    clock: FakeClock,
) -> None:
    """The window starts in ``due``, not after the row is written: refusals that
    overlap at the start of a window must not all be due."""
    for _ in range(ROUNDS):
        throttle = RefusalAuditThrottle(clock=clock)
        answers: list[int | None] = []

        def call() -> None:
            answers.append(throttle.due(TENANT, REASON))  # noqa: B023

        run_threads(call, OVERLAPPING)
        clock.advance(REFUSAL_AUDIT_SECONDS)

        assert len(answers) == OVERLAPPING
        assert [a for a in answers if a is not None] == [0]
        assert throttle.due(TENANT, REASON) == OVERLAPPING - 1


@pytest.mark.usefixtures("fast_switching")
def test_racing_refusals_inside_a_window_are_all_counted(clock: FakeClock) -> None:
    for _ in range(ROUNDS):
        throttle = RefusalAuditThrottle(clock=clock)
        assert throttle.due(TENANT, REASON) == 0
        answers: list[int | None] = []

        def call() -> None:
            answers.append(throttle.due(TENANT, REASON))  # noqa: B023

        run_threads(call)
        clock.advance(REFUSAL_AUDIT_SECONDS)

        assert answers == [None] * RACERS
        assert throttle.due(TENANT, REASON) == RACERS


@pytest.mark.parametrize("second", ["due", "release"])
def test_a_caller_waits_while_another_is_inside_the_throttle(
    clock: FakeClock, second: str
) -> None:
    """Both methods hold the one lock. Under the interpreter lock alone the
    counting cannot be torn (``due`` has no point where a thread can be switched
    out), so this holds a caller inside with a clock that blocks and shows that
    nobody else gets in."""
    inside, release = threading.Event(), threading.Event()
    entered, done = threading.Event(), threading.Event()
    blocked = [True]

    def blocking_clock() -> float:
        if blocked:
            blocked.clear()
            inside.set()
            assert release.wait(timeout=30)
        return clock()

    throttle = RefusalAuditThrottle(clock=blocking_clock)
    first = threading.Thread(target=throttle.due, args=(TENANT, REASON))
    first.start()
    assert inside.wait(timeout=30)

    def other() -> None:
        entered.set()
        if second == "due":
            throttle.due(TENANT, REASON)
        else:
            throttle.release(TENANT, REASON, 0)
        done.set()

    waiting = threading.Thread(target=other)
    waiting.start()
    assert entered.wait(timeout=30)
    waited = not done.wait(timeout=0.25)
    release.set()
    first.join(timeout=30)
    waiting.join(timeout=30)

    assert waited, "a second caller got in while the first was inside"
    assert done.is_set()
