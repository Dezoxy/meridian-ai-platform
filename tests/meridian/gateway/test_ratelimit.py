"""The per-tenant rate windows (S011): pure, on a fake clock."""

import sys
import threading
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
from servicesupport import FakeClock

from meridian.platform.gateway.ratelimit import (
    REFUSAL_AUDIT_SECONDS,
    TOKEN_WINDOW_SECONDS,
    RateRefusal,
    RefusalAuditThrottle,
    TenantRateLimiter,
)
from meridian.platform.registry.models import TenantLimits

TENANT = "claims-triage"
OTHER_TENANT = "evaluation"
REQUESTS = 3
TOKENS = 1000
RACERS = 16
ROUNDS = 50


def limits(
    requests: int = REQUESTS, tokens: int = TOKENS, day: int = 10**9
) -> TenantLimits:
    return TenantLimits(
        requests_per_10_seconds=requests,
        tokens_per_minute=tokens,
        tokens_per_day=day,
        cost_per_month_eur=Decimal(1),
    )


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


def run_threads(call: Callable[[], None]) -> None:
    start = threading.Barrier(RACERS)

    def wait_then_call() -> None:
        start.wait(timeout=30)
        call()

    threads = [threading.Thread(target=wait_then_call) for _ in range(RACERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)


@pytest.fixture
def limiter(clock: FakeClock) -> TenantRateLimiter:
    return TenantRateLimiter(clock=clock)


def admit_n(
    limiter: TenantRateLimiter,
    count: int,
    *,
    tenant: str = TENANT,
    tokens: int = 1,
    window: TenantLimits | None = None,
) -> list[RateRefusal | None]:
    return [limiter.admit(tenant, window or limits(), tokens) for _ in range(count)]


# ── requests per 10 seconds ─────────────────────────────────────────────────
def test_requests_up_to_the_limit_are_admitted(limiter: TenantRateLimiter) -> None:
    assert admit_n(limiter, REQUESTS) == [None] * REQUESTS


def test_one_request_over_the_limit_is_refused(limiter: TenantRateLimiter) -> None:
    admit_n(limiter, REQUESTS)

    refusal = limiter.admit(TENANT, limits(), 1)

    assert refusal is not None
    assert refusal.reason == "tenant-request-rate"


def test_the_entry_leaves_the_request_window_exactly_at_10_seconds(
    limiter: TenantRateLimiter, clock: FakeClock
) -> None:
    admit_n(limiter, REQUESTS)
    clock.advance(9.75)
    assert limiter.admit(TENANT, limits(), 1) is not None

    clock.advance(0.25)  # now - t == 10.0: no longer "within"

    assert limiter.admit(TENANT, limits(), 1) is None


def test_the_request_retry_hint_counts_to_the_oldest_entry_leaving(
    limiter: TenantRateLimiter, clock: FakeClock
) -> None:
    limiter.admit(TENANT, limits(), 1)  # t = 0
    clock.advance(2.5)
    limiter.admit(TENANT, limits(), 1)  # t = 2.5
    clock.advance(1.0)
    limiter.admit(TENANT, limits(), 1)  # t = 3.5
    clock.advance(1.0)  # now = 4.5; the oldest leaves at 10.0: 5.5 s -> 6

    refusal = limiter.admit(TENANT, limits(), 1)

    assert refusal == RateRefusal("tenant-request-rate", 6)


def test_a_whole_second_left_is_a_hint_of_that_second(
    limiter: TenantRateLimiter, clock: FakeClock
) -> None:
    admit_n(limiter, REQUESTS)
    clock.advance(7.0)  # exactly 3.0 s left

    assert limiter.admit(TENANT, limits(), 1) == RateRefusal("tenant-request-rate", 3)


def test_the_request_retry_hint_is_at_least_one_second(
    limiter: TenantRateLimiter, clock: FakeClock
) -> None:
    admit_n(limiter, REQUESTS)
    clock.advance(9.875)  # 0.125 s left: rounded up to 1

    assert limiter.admit(TENANT, limits(), 1) == RateRefusal("tenant-request-rate", 1)


def test_the_request_window_is_checked_before_the_token_window(
    limiter: TenantRateLimiter,
) -> None:
    window = limits(requests=1, tokens=100)
    limiter.admit(TENANT, window, 100)

    refusal = limiter.admit(TENANT, window, 100)

    assert refusal is not None
    assert refusal.reason == "tenant-request-rate"


# ── tokens per minute ───────────────────────────────────────────────────────
def test_tokens_up_to_the_limit_are_admitted(limiter: TenantRateLimiter) -> None:
    assert limiter.admit(TENANT, limits(), 400) is None
    assert limiter.admit(TENANT, limits(), 600) is None  # 1000 in all: at the limit


def test_one_token_over_the_limit_is_refused(limiter: TenantRateLimiter) -> None:
    limiter.admit(TENANT, limits(), 400)
    limiter.admit(TENANT, limits(), 600)

    refusal = limiter.admit(TENANT, limits(), 1)

    assert refusal is not None
    assert refusal.reason == "tenant-token-rate"


def test_the_entry_leaves_the_token_window_exactly_at_60_seconds(
    limiter: TenantRateLimiter, clock: FakeClock
) -> None:
    limiter.admit(TENANT, limits(requests=100), 1000)
    clock.advance(59.75)
    assert limiter.admit(TENANT, limits(requests=100), 1) is not None

    clock.advance(0.25)

    assert limiter.admit(TENANT, limits(requests=100), 1000) is None


def test_the_token_retry_hint_waits_for_enough_entries_to_leave(
    limiter: TenantRateLimiter, clock: FakeClock
) -> None:
    window = limits(requests=100)
    limiter.admit(TENANT, window, 300)  # t = 0
    clock.advance(20.0)
    limiter.admit(TENANT, window, 300)  # t = 20
    clock.advance(20.0)
    limiter.admit(TENANT, window, 400)  # t = 40, the window is full (1000)
    clock.advance(5.0)  # now = 45

    # 700 more need 700 of 1000 out: the entries of 300 and 300 are not
    # enough (400 left + 700 > 1000), the third (400) must leave too, at 100.
    refusal = limiter.admit(TENANT, window, 700)
    # 600 more need only the first two out: the second leaves at 80 -> 35 s.
    smaller = limiter.admit(TENANT, window, 600)

    assert refusal == RateRefusal("tenant-token-rate", 55)
    assert smaller == RateRefusal("tenant-token-rate", 35)


def test_a_token_hint_with_one_leaving_entry_is_that_entrys_time(
    limiter: TenantRateLimiter, clock: FakeClock
) -> None:
    window = limits(requests=100)
    limiter.admit(TENANT, window, 600)  # t = 0
    clock.advance(10.0)
    limiter.admit(TENANT, window, 400)  # t = 10
    clock.advance(10.5)  # now = 20.5

    # 100 more fit once the first entry (600) leaves, at 60: 39.5 s -> 40.
    assert limiter.admit(TENANT, window, 100) == RateRefusal("tenant-token-rate", 40)


def test_the_token_retry_hint_is_at_least_one_second(
    limiter: TenantRateLimiter, clock: FakeClock
) -> None:
    window = limits(requests=100)
    limiter.admit(TENANT, window, 1000)
    clock.advance(59.875)

    assert limiter.admit(TENANT, window, 1) == RateRefusal("tenant-token-rate", 1)


# ── a request that can never pass ───────────────────────────────────────────
def test_a_request_larger_than_the_whole_minute_is_refused_without_a_hint(
    limiter: TenantRateLimiter,
) -> None:
    refusal = limiter.admit(TENANT, limits(), TOKENS + 1)

    assert refusal == RateRefusal("tenant-request-too-large", None)


def test_a_request_of_exactly_the_minute_limit_is_admitted(
    limiter: TenantRateLimiter,
) -> None:
    assert limiter.admit(TENANT, limits(), TOKENS) is None


def test_too_large_is_reported_even_when_the_request_window_is_full(
    limiter: TenantRateLimiter,
) -> None:
    admit_n(limiter, REQUESTS)

    refusal = limiter.admit(TENANT, limits(), TOKENS + 1)

    assert refusal == RateRefusal("tenant-request-too-large", None)


# ── refused requests are not recorded ───────────────────────────────────────
def test_a_flood_of_refused_requests_does_not_extend_its_own_lockout(
    limiter: TenantRateLimiter, clock: FakeClock
) -> None:
    admit_n(limiter, REQUESTS)
    for _ in range(50):
        clock.advance(0.125)
        assert limiter.admit(TENANT, limits(), 1) is not None
    clock.advance(3.75)  # 50 x 0.125 + 3.75 = 10: the first window ends

    assert admit_n(limiter, REQUESTS) == [None] * REQUESTS


def test_refused_tokens_are_not_counted_toward_the_minute(
    limiter: TenantRateLimiter, clock: FakeClock
) -> None:
    window = limits(requests=100)
    limiter.admit(TENANT, window, 900)
    for _ in range(20):
        assert limiter.admit(TENANT, window, 500) is not None
    clock.advance(60.0)

    assert limiter.admit(TENANT, window, 1000) is None


def test_too_large_requests_are_not_recorded(limiter: TenantRateLimiter) -> None:
    for _ in range(10):
        limiter.admit(TENANT, limits(), TOKENS + 1)

    assert admit_n(limiter, REQUESTS) == [None] * REQUESTS


# ── tenants, memory ─────────────────────────────────────────────────────────
def test_two_tenants_do_not_share_a_window(limiter: TenantRateLimiter) -> None:
    admit_n(limiter, REQUESTS)
    assert limiter.admit(TENANT, limits(), 1) is not None

    assert admit_n(limiter, REQUESTS, tenant=OTHER_TENANT) == [None] * REQUESTS


def test_each_tenant_is_held_to_the_limits_it_is_asked_with(
    limiter: TenantRateLimiter,
) -> None:
    assert limiter.admit(TENANT, limits(requests=1), 1) is None
    assert limiter.admit(TENANT, limits(requests=1), 1) is not None
    assert limiter.admit(OTHER_TENANT, limits(requests=2), 1) is None
    assert limiter.admit(OTHER_TENANT, limits(requests=2), 1) is None
    assert limiter.admit(OTHER_TENANT, limits(requests=2), 1) is not None


def test_a_tenant_whose_entries_expired_is_not_kept(
    limiter: TenantRateLimiter, clock: FakeClock
) -> None:
    limiter.admit(TENANT, limits(), 1)
    limiter.admit(OTHER_TENANT, limits(), 1)
    assert set(limiter._entries) == {TENANT, OTHER_TENANT}

    clock.advance(TOKEN_WINDOW_SECONDS - 0.25)
    limiter.admit(OTHER_TENANT, limits(), 1)  # sweeps; TENANT's entry is 59.75 old
    assert set(limiter._entries) == {TENANT, OTHER_TENANT}

    clock.advance(0.25)
    limiter.admit(OTHER_TENANT, limits(), 1)

    assert set(limiter._entries) == {OTHER_TENANT}


def test_memory_per_tenant_is_bounded_by_what_a_minute_admits(
    limiter: TenantRateLimiter, clock: FakeClock
) -> None:
    window = limits(requests=1000, tokens=10**6)
    for _ in range(5000):
        limiter.admit(TENANT, window, 1)
        clock.advance(0.125)

    assert len(limiter._entries[TENANT]) == 480  # 60 s at one request per 0.125 s


# ── threads ─────────────────────────────────────────────────────────────────
@pytest.mark.usefixtures("fast_switching")
def test_racing_threads_are_admitted_exactly_up_to_the_limit(clock: FakeClock) -> None:
    allowed = 5
    for _ in range(ROUNDS):
        limiter = TenantRateLimiter(clock=clock)
        results: list[RateRefusal | None] = []

        def call() -> None:
            results.append(limiter.admit(TENANT, limits(requests=allowed), 1))  # noqa: B023

        run_threads(call)

        assert len(results) == RACERS
        assert results.count(None) == allowed
        assert all(r.reason == "tenant-request-rate" for r in results if r)


@pytest.mark.usefixtures("fast_switching")
def test_racing_threads_cannot_pass_the_token_limit(clock: FakeClock) -> None:
    window = limits(requests=1000, tokens=400)
    for _ in range(ROUNDS):
        limiter = TenantRateLimiter(clock=clock)
        results: list[RateRefusal | None] = []

        def call() -> None:
            results.append(limiter.admit(TENANT, window, 100))  # noqa: B023

        run_threads(call)

        assert results.count(None) == 4


# ── the audit throttle of refusals (T-49) ───────────────────────────────────
REASON = "tenant-request-rate"


def test_the_first_refusal_of_a_key_is_due_with_a_count_of_zero(
    clock: FakeClock,
) -> None:
    throttle = RefusalAuditThrottle(clock=clock)

    assert throttle.due(TENANT, REASON) == 0


def test_after_the_row_is_marked_the_rest_of_the_window_is_suppressed(
    clock: FakeClock,
) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    assert throttle.due(TENANT, REASON) == 0
    throttle.mark(TENANT, REASON)

    answers = [throttle.due(TENANT, REASON) for _ in range(19)]

    assert answers == [None] * 19


def test_the_next_row_after_the_window_carries_the_count_of_the_suppressed(
    clock: FakeClock,
) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    assert throttle.due(TENANT, REASON) == 0
    throttle.mark(TENANT, REASON)
    for _ in range(19):
        assert throttle.due(TENANT, REASON) is None

    clock.advance(REFUSAL_AUDIT_SECONDS)

    assert throttle.due(TENANT, REASON) == 19
    throttle.mark(TENANT, REASON)
    assert throttle.due(TENANT, REASON) is None  # a new window, a new count
    clock.advance(REFUSAL_AUDIT_SECONDS)
    assert throttle.due(TENANT, REASON) == 1


def test_a_refusal_just_inside_the_window_is_suppressed_and_one_at_its_end_is_due(
    clock: FakeClock,
) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    throttle.due(TENANT, REASON)
    throttle.mark(TENANT, REASON)

    clock.advance(REFUSAL_AUDIT_SECONDS - 0.5)
    inside = throttle.due(TENANT, REASON)
    clock.advance(0.5)
    at_the_end = throttle.due(TENANT, REASON)

    assert (inside, at_the_end) == (None, 1)


def test_a_row_that_was_never_written_leaves_the_next_refusal_due_with_the_loss(
    clock: FakeClock,
) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    assert throttle.due(TENANT, REASON) == 0  # the write failed: no mark

    assert throttle.due(TENANT, REASON) == 1
    assert throttle.due(TENANT, REASON) == 2  # and again, until a row is marked


def test_a_refusal_inside_the_window_does_not_extend_it(clock: FakeClock) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    throttle.due(TENANT, REASON)
    throttle.mark(TENANT, REASON)
    clock.advance(REFUSAL_AUDIT_SECONDS - 1)
    throttle.due(TENANT, REASON)  # a flood must not keep its own row away

    clock.advance(1)

    assert throttle.due(TENANT, REASON) == 1


def test_each_tenant_and_each_reason_has_its_own_window(clock: FakeClock) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    throttle.due(TENANT, REASON)
    throttle.mark(TENANT, REASON)

    assert throttle.due(OTHER_TENANT, REASON) == 0
    assert throttle.due(TENANT, "tenant-token-rate") == 0
    assert throttle.due(TENANT, REASON) is None


def test_a_refusal_with_no_tenant_is_a_key_of_its_own(clock: FakeClock) -> None:
    throttle = RefusalAuditThrottle(clock=clock)
    assert throttle.due(None, "unknown-tenant") == 0
    throttle.mark(None, "unknown-tenant")

    assert throttle.due(None, "unknown-tenant") is None
    assert throttle.due(TENANT, "unknown-tenant") == 0


@pytest.mark.usefixtures("fast_switching")
def test_racing_refusals_inside_a_window_are_all_counted(clock: FakeClock) -> None:
    for _ in range(ROUNDS):
        throttle = RefusalAuditThrottle(clock=clock)
        assert throttle.due(TENANT, REASON) == 0
        throttle.mark(TENANT, REASON)
        answers: list[int | None] = []

        def call() -> None:
            answers.append(throttle.due(TENANT, REASON))  # noqa: B023

        run_threads(call)
        clock.advance(REFUSAL_AUDIT_SECONDS)

        assert answers == [None] * RACERS
        assert throttle.due(TENANT, REASON) == RACERS


@pytest.mark.parametrize("second", ["due", "mark"])
def test_a_caller_waits_while_another_is_inside_the_throttle(
    clock: FakeClock, second: str
) -> None:
    """Both methods hold the one lock. Under the interpreter lock alone the
    counting cannot be torn (``due`` has no point where a thread can be switched
    out), so this holds a caller inside with a clock that blocks and shows that
    nobody else gets in."""
    inside, release, done = threading.Event(), threading.Event(), threading.Event()
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
        getattr(throttle, second)(TENANT, REASON)
        done.set()

    waiting = threading.Thread(target=other)
    waiting.start()
    waited = not done.wait(timeout=0.25)
    release.set()
    first.join(timeout=30)
    waiting.join(timeout=30)

    assert waited, "a second caller got in while the first was inside"
    assert done.is_set()
