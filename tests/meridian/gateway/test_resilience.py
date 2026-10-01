"""The circuit breaker and the call deadline (S042): pure, on a fake clock."""

import sys
import threading
from typing import get_args

import pytest
from servicesupport import FakeClock

from meridian.platform.gateway.providers.azure_openai import (
    CONNECT_TIMEOUT_SECONDS,
    PROVIDER_TIMEOUT_SECONDS,
)
from meridian.platform.gateway.providers.base import ProviderErrorKind
from meridian.platform.gateway.resilience import (
    CALL_DEADLINE_SECONDS,
    DEPLOYMENT_FAILURES,
    FAILURE_THRESHOLD,
    MIN_ATTEMPT_SECONDS,
    OPEN_SECONDS,
    CircuitBreaker,
    Deadline,
    Permit,
)
from meridian.runtime.app import GATEWAY_TIMEOUT_SECONDS

KEY = "aoai-sdc-gpt-4o"
OTHER_KEY = "aoai-sdc-gpt-4o-second"
RACERS = 16
RACE_ROUNDS = 40
SWITCH_INTERVAL_SECONDS = 1e-6
STALE_SETTLES = ["success", "failure", "release"]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(clock=clock)


def take(breaker: CircuitBreaker, key: str = KEY) -> Permit:
    permit = breaker.acquire(key)
    assert permit is not None
    return permit


def settle(breaker: CircuitBreaker, how: str, permit: Permit) -> None:
    getattr(breaker, how)(permit)


def fail_times(breaker: CircuitBreaker, key: str, times: int) -> None:
    for _ in range(times):
        breaker.failure(take(breaker, key))


def open_circuit(breaker: CircuitBreaker, key: str = KEY) -> None:
    fail_times(breaker, key, FAILURE_THRESHOLD)
    assert breaker.state(key) == "open"


def to_half_open(breaker: CircuitBreaker, clock: FakeClock) -> Permit:
    """Open the circuit, wait out the cooldown and take the probe."""
    open_circuit(breaker)
    clock.advance(OPEN_SECONDS)
    probe = take(breaker)
    assert breaker.state(KEY) == "half-open"
    return probe


# ── closed ───────────────────────────────────────────────────────────────────
def test_an_unknown_key_is_closed_and_acquirable_with_a_permit_that_is_no_probe(
    breaker: CircuitBreaker,
) -> None:
    assert breaker.state("never-seen") == "closed"
    assert breaker.acquire("never-seen") == Permit("never-seen", None)


def test_failures_below_the_threshold_leave_the_circuit_closed(
    breaker: CircuitBreaker,
) -> None:
    fail_times(breaker, KEY, FAILURE_THRESHOLD - 1)

    assert breaker.state(KEY) == "closed"
    assert breaker.acquire(KEY) is not None


def test_the_threshold_failure_in_a_row_opens_the_circuit(
    breaker: CircuitBreaker,
) -> None:
    fail_times(breaker, KEY, FAILURE_THRESHOLD)

    assert breaker.state(KEY) == "open"
    assert breaker.acquire(KEY) is None


def test_a_success_in_between_resets_the_count(breaker: CircuitBreaker) -> None:
    fail_times(breaker, KEY, FAILURE_THRESHOLD - 1)
    breaker.success(take(breaker))

    fail_times(breaker, KEY, FAILURE_THRESHOLD - 1)

    assert breaker.state(KEY) == "closed"


def test_release_changes_neither_the_count_nor_the_state(
    breaker: CircuitBreaker,
) -> None:
    fail_times(breaker, KEY, FAILURE_THRESHOLD - 1)
    for _ in range(FAILURE_THRESHOLD * 2):
        breaker.release(take(breaker))

    assert breaker.state(KEY) == "closed"
    # The count survived the releases: one more failure opens the circuit.
    breaker.failure(take(breaker))
    assert breaker.state(KEY) == "open"


def test_a_custom_threshold_and_cooldown_are_honoured(clock: FakeClock) -> None:
    small = CircuitBreaker(failure_threshold=1, open_seconds=5.0, clock=clock)

    fail_times(small, KEY, 1)
    assert small.state(KEY) == "open"
    clock.advance(4.9)
    assert small.acquire(KEY) is None
    clock.advance(0.1)
    assert small.acquire(KEY) is not None


# ── open ─────────────────────────────────────────────────────────────────────
def test_an_open_circuit_refuses_until_the_cooldown_has_passed(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    open_circuit(breaker)

    clock.advance(OPEN_SECONDS - 0.001)
    assert breaker.acquire(KEY) is None
    assert breaker.state(KEY) == "open"

    clock.advance(0.001)
    assert breaker.acquire(KEY) is not None
    assert breaker.state(KEY) == "half-open"


def test_after_the_cooldown_exactly_one_acquire_is_the_probe(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    open_circuit(breaker)
    clock.advance(OPEN_SECONDS)

    first, second, third = (breaker.acquire(KEY) for _ in range(3))

    assert first is not None
    assert first.probe is not None
    assert (second, third) == (None, None)


# ── half-open: the probe decides ─────────────────────────────────────────────
def test_a_probe_that_succeeds_closes_the_circuit_and_resets_the_count(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    probe = to_half_open(breaker, clock)

    breaker.success(probe)

    assert breaker.state(KEY) == "closed"
    breaker.release(take(breaker))
    fail_times(breaker, KEY, FAILURE_THRESHOLD - 1)
    assert breaker.state(KEY) == "closed"


def test_a_probe_that_fails_opens_again_with_a_fresh_cooldown(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    probe = to_half_open(breaker, clock)

    breaker.failure(probe)

    assert breaker.state(KEY) == "open"
    clock.advance(OPEN_SECONDS - 0.001)
    assert breaker.acquire(KEY) is None
    clock.advance(0.001)
    assert breaker.acquire(KEY) is not None


def test_a_probe_that_is_released_leaves_the_circuit_half_open_for_the_next(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    probe = to_half_open(breaker, clock)
    assert breaker.acquire(KEY) is None  # the probe is in flight

    breaker.release(probe)

    assert breaker.state(KEY) == "half-open"
    next_probe = take(breaker)  # the next caller is the new probe
    assert next_probe.probe is not None
    assert next_probe.probe != probe.probe
    assert breaker.acquire(KEY) is None


# ── a permit settles only what it was taken for ──────────────────────────────
@pytest.mark.parametrize("how", STALE_SETTLES)
def test_a_stale_permit_changes_nothing_on_an_open_circuit(
    breaker: CircuitBreaker, clock: FakeClock, how: str
) -> None:
    stale = take(breaker)  # a call that began while the circuit was closed
    open_circuit(breaker)
    clock.advance(OPEN_SECONDS - 10.0)

    settle(breaker, how, stale)

    assert breaker.state(KEY) == "open"
    clock.advance(10.0 - 0.001)
    assert breaker.acquire(KEY) is None  # the cooldown still ends at t0 + 30
    clock.advance(0.001)
    assert breaker.acquire(KEY) is not None


def test_a_stale_failure_does_not_extend_the_cooldown(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    stale = take(breaker)
    open_circuit(breaker)  # opened at t0
    clock.advance(20.0)

    breaker.failure(stale)
    clock.advance(10.0)

    assert breaker.acquire(KEY) is not None  # t0 + 30: the probe


@pytest.mark.parametrize("how", STALE_SETTLES)
@pytest.mark.parametrize("probe_ends", ["success", "failure", "release"])
def test_a_stale_permit_on_a_half_open_circuit_changes_nothing_and_the_probe_decides(
    breaker: CircuitBreaker, clock: FakeClock, how: str, probe_ends: str
) -> None:
    stale = take(breaker)
    probe = to_half_open(breaker, clock)

    settle(breaker, how, stale)

    # Neither closed (success), reopened (failure) nor freed (release).
    assert breaker.state(KEY) == "half-open"
    assert breaker.acquire(KEY) is None
    settle(breaker, probe_ends, probe)
    expected = {"success": "closed", "failure": "open", "release": "half-open"}
    assert breaker.state(KEY) == expected[probe_ends]
    if probe_ends == "failure":
        clock.advance(OPEN_SECONDS - 0.001)  # a fresh cooldown, from the probe
        assert breaker.acquire(KEY) is None
        clock.advance(0.001)
        assert breaker.acquire(KEY) is not None
    if probe_ends == "release":
        assert breaker.acquire(KEY) is not None  # the next caller probes


@pytest.mark.parametrize("second", STALE_SETTLES)
def test_settling_a_probe_permit_twice_does_not_free_or_settle_a_newer_probe(
    breaker: CircuitBreaker, clock: FakeClock, second: str
) -> None:
    old = to_half_open(breaker, clock)
    breaker.release(old)
    newer = take(breaker)
    assert newer.probe != old.probe

    settle(breaker, second, old)  # the old probe's late, second settle

    assert breaker.state(KEY) == "half-open"
    assert breaker.acquire(KEY) is None  # the newer probe is still in flight
    breaker.success(newer)
    assert breaker.state(KEY) == "closed"


def test_a_probe_permit_settled_after_its_own_success_counts_for_nothing(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    probe = to_half_open(breaker, clock)
    breaker.success(probe)

    for _ in range(FAILURE_THRESHOLD * 2):
        breaker.failure(probe)

    assert breaker.state(KEY) == "closed"


# ── independence and concurrency ─────────────────────────────────────────────
def test_the_circuits_of_two_keys_are_independent(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    open_circuit(breaker, KEY)

    assert breaker.state(OTHER_KEY) == "closed"
    other = take(breaker, OTHER_KEY)
    assert breaker.acquire(KEY) is None
    breaker.failure(other)
    assert breaker.state(OTHER_KEY) == "closed"  # its own count is 1


def race_for_the_probe(clock: FakeClock) -> list[Permit | None]:
    breaker = CircuitBreaker(clock=clock)
    open_circuit(breaker)
    clock.advance(OPEN_SECONDS)
    barrier = threading.Barrier(RACERS)
    results: list[Permit | None] = []
    results_lock = threading.Lock()

    def race() -> None:
        barrier.wait()
        granted = breaker.acquire(KEY)
        with results_lock:
            results.append(granted)

    threads = [threading.Thread(target=race) for _ in range(RACERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_of_sixteen_racing_threads_exactly_one_gets_the_probe_in_every_round(
    clock: FakeClock,
) -> None:
    # A thread switch every microsecond makes a missing lock show: with the lock
    # removed from ``acquire`` this test fails (several threads get the probe).
    previous = sys.getswitchinterval()
    sys.setswitchinterval(SWITCH_INTERVAL_SECONDS)
    try:
        rounds = [race_for_the_probe(clock) for _ in range(RACE_ROUNDS)]
    finally:
        sys.setswitchinterval(previous)

    for results in rounds:
        assert len(results) == RACERS
        assert sum(permit is not None for permit in results) == 1


# ── the deadline ─────────────────────────────────────────────────────────────
def test_the_deadline_counts_down_with_the_clock_and_stops_at_zero(
    clock: FakeClock,
) -> None:
    deadline = Deadline(25.0, clock)
    assert deadline.remaining() == 25.0

    clock.advance(10.0)
    assert deadline.remaining() == 15.0

    clock.advance(15.0)
    assert deadline.remaining() == 0.0

    clock.advance(100.0)
    assert deadline.remaining() == 0.0


# ── the constants ────────────────────────────────────────────────────────────
def test_the_time_limits_nest_inside_the_runtimes_timeout_to_the_gateway() -> None:
    assert MIN_ATTEMPT_SECONDS < CALL_DEADLINE_SECONDS < GATEWAY_TIMEOUT_SECONDS
    # An attempt given the whole deadline can still use its connect and answer
    # limits in full.
    assert CONNECT_TIMEOUT_SECONDS + PROVIDER_TIMEOUT_SECONDS <= CALL_DEADLINE_SECONDS


def test_every_provider_error_kind_is_deliberately_counted_or_not() -> None:
    # The request or the credential is at fault, not the deployment.
    uncounted = {"rejected", "auth"}

    assert DEPLOYMENT_FAILURES.isdisjoint(uncounted)
    assert DEPLOYMENT_FAILURES | uncounted == set(get_args(ProviderErrorKind))
