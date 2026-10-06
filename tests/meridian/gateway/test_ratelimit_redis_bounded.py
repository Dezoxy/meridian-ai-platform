"""What a planted window cannot do to the script, and what one call may read (S066).

H3a, from the third security review (F1 and F2). The gateway's Redis user may
``ZADD`` into any ``meridian:rate:*`` key without the script, so a holder of its
credential can leave members the script did not write. ``test_ratelimit_redis_
planted.py`` pins what the script does with a member it cannot read or one from
the future. These pin two more:

* A count too big for a float (309 digits or more is ``inf`` in Lua) made the sum
  ``inf``, the walk ``nan`` and the script end in its own error reply: a 503 for
  that tenant for as long as the member stayed. A count is now clamped as it is
  read, and a plant is a 429.
* A window of hundreds of thousands of members made one call slow enough to
  outlast the gateway's read timeout, and the next tenant's call answered BUSY.
  One call now reads a bounded number of members, whatever the key holds.

The tests plant with the test Redis's own client, as the credential's holder
would, and call the limiter as the gateway does. None sleeps or reads a wall
clock: time is a hand-moved clock, and what the script touched is counted by
wrapping the script's own text, not timed.
"""

import itertools
import sys
import threading
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
import redis
from redissupport import RateKeys
from servicesupport import FakeClock

from meridian.platform.gateway import ratelimit_redis
from meridian.platform.gateway.ratelimit import (
    REQUEST_WINDOW_SECONDS,
    TOKEN_WINDOW_SECONDS,
    RateRefusal,
)
from meridian.platform.gateway.ratelimit_redis import RedisRateLimiter
from meridian.platform.registry.models import TenantLimits

TENANT = "claims-triage"
OTHER_TENANT = "evaluation"
TOKENS = 1000
REQUESTS = 10
RACERS = 16
ROUNDS = 20
# The most the script reads for a limit of R requests: the 120 s the script keeps
# (a token window each side of now) is twelve request windows, and each admits at
# most R. The test works it out from the windows, not from the module.
WINDOWS_KEPT = round(2 * TOKEN_WINDOW_SECONDS / REQUEST_WINDOW_SECONDS)
CAP = WINDOWS_KEPT * REQUESTS
NOW_MS = 1000 * 1000  # the hand clock reads 1000 s


def limits(requests: int = REQUESTS, tokens: int = TOKENS) -> TenantLimits:
    return TenantLimits(
        requests_per_10_seconds=requests,
        tokens_per_minute=tokens,
        tokens_per_day=10**9,
        cost_per_month_eur=Decimal(1),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(rate_keys: RateKeys, clock: FakeClock) -> RedisRateLimiter:
    return RedisRateLimiter(
        rate_keys.client, prefix=rate_keys.new_prefix(), clock=clock
    )


@pytest.fixture
def fast_switching() -> Iterator[None]:
    before = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(before)


def plant(
    rate_keys: RateKeys, key: str, members: dict[str, float], chunk: int = 10_000
) -> None:
    """ZADD as the credential's holder would, in chunks a server takes at once."""
    items = list(members.items())
    for start in range(0, len(items), chunk):
        rate_keys.client.zadd(key, dict(items[start : start + chunk]))


# ── F1: a count too big for a float ─────────────────────────────────────────
HUGE_COUNTS = {
    "309-digits-is-inf-in-lua": "9" * 309,
    "400-digits": "9" * 400,
    "a-thousand-digits": "7" * 1000,
    "a-float-that-rounds": "9" * 30,
    "one-over-the-limit": str(TOKENS + 1),
}


@pytest.mark.parametrize("count", HUGE_COUNTS.values(), ids=HUGE_COUNTS.keys())
def test_a_planted_count_too_big_for_a_float_is_a_429_and_never_a_503(
    rate_keys: RateKeys, store: RedisRateLimiter, clock: FakeClock, count: str
) -> None:
    plant(rate_keys, store.key_for(TENANT), {f"{count}:x": clock() * 1000})

    # The member fills the minute's tokens until it leaves, 60 s after its score:
    # a rate refusal with that wait, not an error out of the script's own sum.
    refusal = store.admit(TENANT, limits(), 1)

    assert refusal == RateRefusal("tenant-token-rate", 60)


def test_a_planted_huge_count_at_the_edge_of_the_margin_waits_at_most_two_windows(
    rate_keys: RateKeys, store: RedisRateLimiter
) -> None:
    edge = NOW_MS + round(TOKEN_WINDOW_SECONDS * 1000)
    plant(rate_keys, store.key_for(TENANT), {f"{'9' * 400}:edge": edge})

    refusal = store.admit(TENANT, limits(), 1)

    assert refusal == RateRefusal("tenant-token-rate", 120)


def test_a_planted_huge_count_leaves_with_its_score_and_the_tenant_goes_on(
    rate_keys: RateKeys, store: RedisRateLimiter, clock: FakeClock
) -> None:
    plant(rate_keys, store.key_for(TENANT), {f"{'9' * 400}:x": clock() * 1000})
    assert store.admit(TENANT, limits(), 1) is not None

    clock.advance(TOKEN_WINDOW_SECONDS)

    assert store.admit(TENANT, limits(), 1) is None


def test_a_planted_huge_count_never_changes_what_another_tenant_sees(
    rate_keys: RateKeys, store: RedisRateLimiter, clock: FakeClock
) -> None:
    assert store.admit(OTHER_TENANT, limits(), 100) is None
    before = rate_keys.client.zrange(store.key_for(OTHER_TENANT), 0, -1)
    plant(rate_keys, store.key_for(TENANT), {f"{'9' * 400}:x": clock() * 1000})

    assert store.admit(TENANT, limits(), 1) is not None

    after = rate_keys.client.zrange(store.key_for(OTHER_TENANT), 0, -1)
    assert after == before
    assert store.admit(OTHER_TENANT, limits(), 100) is None


# What the script's pattern reads as a count: digits, then a colon, nothing else.
# A sign, an exponent, a name for a number, a hexadecimal, a space or a point
# stops the match, so the member is read as no tokens: counted as a request (as
# every unreadable member is) and no more. None of these is a number to Lua's sum.
ODD_COUNTS = {
    "nan": "nan:x",
    "inf": "inf:x",
    "negative": "-1:x",
    "positive-sign": "+5:x",
    "exponent": "1e5:x",
    "exponent-to-infinity": "1e999:x",
    "hexadecimal": "0x10:x",
    "leading-space": " 5:x",
    "decimal-point": "5.5:x",
    "arabic-indic-digits": "١٢:x",
}


@pytest.mark.parametrize("member", ODD_COUNTS.values(), ids=ODD_COUNTS.keys())
def test_a_count_that_is_not_plain_digits_is_read_as_no_tokens(
    rate_keys: RateKeys, store: RedisRateLimiter, clock: FakeClock, member: str
) -> None:
    plant(rate_keys, store.key_for(TENANT), {member: clock() * 1000})

    # Read as 100000 (the exponent), or as anything that counts, the minute's
    # whole budget would no longer be free.
    refusal = store.admit(TENANT, limits(), TOKENS)

    assert refusal is None


def test_a_nan_count_cannot_get_past_the_clamp_whichever_way_it_is_written(
    rate_keys: RateKeys,
) -> None:
    # The pattern keeps a nan out, so this is a guard on the clamp's argument
    # order: Lua 5.1's math.min keeps its first argument against a nan, and
    # returns the nan when the nan is first. The script puts the bound first.
    reply = rate_keys.client.eval(
        "local n = 0/0; return {math.min(5, n) == 5 and 1 or 0,"
        " math.min(n, 5) == 5 and 1 or 0}",
        0,
    )

    assert reply == [1, 0]


def run_threads(call: Callable[[], None]) -> list[BaseException]:
    errors: list[BaseException] = []
    start = threading.Barrier(RACERS)

    def wait_then_call() -> None:
        try:
            start.wait(timeout=30)
            call()
        except BaseException as exc:  # reported to the test
            errors.append(exc)

    threads = [threading.Thread(target=wait_then_call) for _ in range(RACERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return errors


RACED_WINDOWS = {
    "clean": (None, 10),
    "a-planted-count-of-400": ("400", 6),
    "a-planted-count-that-is-inf": ("9" * 400, 0),
    "a-planted-count-that-rounds": ("9" * 30, 0),
}


@pytest.mark.usefixtures("fast_switching")
@pytest.mark.parametrize(
    ("planted", "allowed"), RACED_WINDOWS.values(), ids=RACED_WINDOWS.keys()
)
def test_racing_threads_on_two_stores_are_admitted_exactly_what_a_planted_window_allows(
    rate_keys: RateKeys, clock: FakeClock, planted: str | None, allowed: int
) -> None:
    # A thousand tokens a minute, a hundred a call: ten in a clean window.
    window = limits(requests=1000, tokens=1000)
    clients = [rate_keys.new_client(), rate_keys.new_client()]
    for _ in range(ROUNDS):
        prefix = rate_keys.new_prefix()
        stores = [RedisRateLimiter(c, prefix=prefix, clock=clock) for c in clients]
        if planted is not None:
            plant(rate_keys, stores[0].key_for(TENANT), {f"{planted}:p": NOW_MS})
        turns = itertools.count()
        results: list[RateRefusal | None] = []

        def call() -> None:
            store = stores[next(turns) % 2]  # noqa: B023
            results.append(store.admit(TENANT, window, 100))  # noqa: B023

        errors = run_threads(call)

        assert errors == []  # no call was a 503 (RateStoreUnavailable)
        assert len(results) == RACERS
        assert results.count(None) == allowed


# ── F2: what one call may read ──────────────────────────────────────────────
# The script's own text, run with a wrapper that counts the members its ZRANGE
# hands back, so a test compares what was touched and not how long it took.
_COUNTING = """
local real_redis = redis
local touched = 0
local redis = {
  call = function(command, ...)
    local reply = real_redis.call(command, ...)
    if command == 'ZRANGE' then touched = touched + #reply / 2 end
    return reply
  end,
  error_reply = real_redis.error_reply,
}
local function body()
%s
end
local reply = body()
reply[#reply + 1] = touched
return reply
"""


class CountingScript:
    """Stands in for the limiter's script: runs its text through the wrapper above
    on the same server with the same arguments, keeps the count, and gives the
    limiter the two-element reply it expects."""

    def __init__(self, client: redis.Redis) -> None:
        self._client = client
        self.touched: list[int] = []

    def __call__(self, keys: list[str], args: list[object]) -> list[int]:
        text = _COUNTING % ratelimit_redis._SCRIPT
        *reply, touched = self._client.eval(text, len(keys), *keys, *args)
        self.touched.append(int(touched))
        return reply


def recent(count: int, age_seconds: float) -> dict[str, float]:
    """Members of no tokens inside the request window, all ``age_seconds`` old, so
    that a walk reads the age off them and a refusal without one cannot."""
    return {f"0:p{n}": NOW_MS - age_seconds * 1000 for n in range(count)}


@pytest.mark.parametrize("planted", [CAP + 1, 1_000, 20_000])
def test_a_planted_window_is_read_up_to_the_bound_and_one_more_and_no_further(
    rate_keys: RateKeys, store: RedisRateLimiter, planted: int
) -> None:
    counting = CountingScript(rate_keys.client)
    store._script = counting  # type: ignore[assignment]
    plant(rate_keys, store.key_for(TENANT), recent(planted, 5))

    store.admit(TENANT, limits(), 1)

    assert counting.touched == [CAP + 1]


def test_what_the_script_touches_does_not_grow_with_the_plant(
    rate_keys: RateKeys, store: RedisRateLimiter
) -> None:
    counting = CountingScript(rate_keys.client)
    store._script = counting  # type: ignore[assignment]
    small, large = 2_000, 40_000

    plant(rate_keys, store.key_for(TENANT), recent(small, 5))
    store.admit(TENANT, limits(), 1)
    plant(rate_keys, store.key_for(OTHER_TENANT), recent(large, 5))
    store.admit(OTHER_TENANT, limits(), 1)

    assert counting.touched[0] == counting.touched[1]


@pytest.mark.parametrize("planted", [CAP + 1, 10 * CAP, 24_000])
def test_a_window_over_the_bound_is_a_request_refusal_with_a_bounded_wait(
    rate_keys: RateKeys, store: RedisRateLimiter, planted: int
) -> None:
    # The oldest member is 5 s old, so a walk would say 5 s. The answer without
    # one is the request window's own length.
    plant(rate_keys, store.key_for(TENANT), recent(planted, 5))

    refusal = store.admit(TENANT, limits(), 1)

    assert refusal == RateRefusal("tenant-request-rate", 10)


def test_a_window_exactly_at_the_bound_is_still_walked(
    rate_keys: RateKeys, store: RedisRateLimiter
) -> None:
    plant(rate_keys, store.key_for(TENANT), recent(CAP, 5))

    refusal = store.admit(TENANT, limits(), 1)

    # Walked: the wait is read off the oldest member (5 s old, so 5 s left).
    assert refusal == RateRefusal("tenant-request-rate", 5)


def test_a_window_at_the_bound_with_nothing_recent_is_admitted(
    rate_keys: RateKeys, store: RedisRateLimiter
) -> None:
    plant(rate_keys, store.key_for(TENANT), recent(CAP, 30))

    refusal = store.admit(TENANT, limits(), 1)

    assert refusal is None


def test_a_window_over_the_bound_is_refused_though_nothing_in_it_is_recent(
    rate_keys: RateKeys, store: RedisRateLimiter
) -> None:
    # More than the script itself can have written: fail closed for a request
    # window's wait, and the members leave with their scores.
    plant(rate_keys, store.key_for(TENANT), recent(CAP + 1, 30))

    refusal = store.admit(TENANT, limits(), 1)

    assert refusal == RateRefusal("tenant-request-rate", 10)


def test_another_tenant_is_admitted_right_after_a_plant_far_over_the_bound(
    rate_keys: RateKeys, store: RedisRateLimiter
) -> None:
    plant(rate_keys, store.key_for(TENANT), recent(24_000, 5))
    assert store.admit(TENANT, limits(), 1) is not None

    admitted = store.admit(OTHER_TENANT, limits(), 1)

    assert admitted is None


def test_a_refusal_for_a_window_over_the_bound_removes_and_adds_nothing(
    rate_keys: RateKeys, store: RedisRateLimiter
) -> None:
    key = store.key_for(TENANT)
    plant(rate_keys, key, recent(CAP + 50, 5))

    store.admit(TENANT, limits(), 1)

    assert rate_keys.client.zcard(key) == CAP + 50


def test_the_plant_leaves_with_its_scores_and_the_tenant_is_admitted_again(
    rate_keys: RateKeys, store: RedisRateLimiter, clock: FakeClock
) -> None:
    plant(rate_keys, store.key_for(TENANT), recent(5 * CAP, 5))
    assert store.admit(TENANT, limits(), 1) is not None

    clock.advance(TOKEN_WINDOW_SECONDS)

    assert store.admit(TENANT, limits(), 1) is None


# ── the bound is never reached by what the gateway writes itself ────────────
def hammer(
    store: RedisRateLimiter,
    rate_keys: RateKeys,
    clock: FakeClock,
    seconds: float,
    peak: list[int],
) -> None:
    """Call every quarter second, as a tenant that is at its limit all the time,
    and record the biggest window the script's own writes ever left."""
    window = limits(requests=REQUESTS, tokens=10**9)
    for _ in range(round(seconds * 4)):
        store.admit(TENANT, window, 1)
        peak.append(rate_keys.client.zcard(store.key_for(TENANT)))
        clock.advance(0.25)


def test_a_tenant_at_its_limit_all_the_time_never_leaves_more_than_six_windows(
    rate_keys: RateKeys, store: RedisRateLimiter, clock: FakeClock
) -> None:
    peak: list[int] = []

    hammer(store, rate_keys, clock, 150, peak)

    # Six request windows of at most R, on a clock that only goes forward: half
    # of what the script reads.
    assert max(peak) <= (WINDOWS_KEPT // 2) * REQUESTS


def test_a_clock_that_steps_back_a_minute_never_leaves_more_than_the_bound(
    rate_keys: RateKeys, store: RedisRateLimiter, clock: FakeClock
) -> None:
    peak: list[int] = []
    hammer(store, rate_keys, clock, 150, peak)

    # Just under a token window back: the entries the clock had already written
    # are in the future, and still inside what the script keeps.
    clock.advance(-(TOKEN_WINDOW_SECONDS - 1))
    hammer(store, rate_keys, clock, 150, peak)

    assert max(peak) <= CAP
