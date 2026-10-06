"""A window that somebody else wrote to cannot lock a tenant out for ever (S066, T-45).

The gateway's Redis user may ``ZADD`` into any ``meridian:rate:*`` key without
going through the script, so a holder of its credential (or a bug) can leave a
member the script did not write: one it cannot read, or one scored in the
future. The cluster reviews (security M1 and L1, infra S1) measured both: the
first made the script raise on every call of that tenant (a 503 for that tenant
alone, until the store restarted), the second a 429 whose wait was thousands of
years. Neither may outlive the call that finds it. The tests plant the member
with the test Redis's own client, as the credential's holder would, and call the
limiter as the gateway does.
"""

from decimal import Decimal

import pytest
from redissupport import RateKeys
from servicesupport import FakeClock

from meridian.platform.gateway.ratelimit import (
    TOKEN_WINDOW_SECONDS,
    RateRefusal,
    RateStoreUnavailable,
)
from meridian.platform.gateway.ratelimit_redis import RedisRateLimiter
from meridian.platform.registry.models import TenantLimits

TENANT = "claims-triage"
OTHER_TENANT = "evaluation"
TOKENS = 1000
REQUESTS = 3
# The hand clock reads 1000 s: the script's "now" is 1,000,000 ms, so the future
# starts just after the longer window from there.
FUTURE_BOUND_MS = round(1000 * 1000 + TOKEN_WINDOW_SECONDS * 1000)


def limits(requests: int = 10, tokens: int = TOKENS) -> TenantLimits:
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


def members_of(rate_keys: RateKeys, store: RedisRateLimiter, tenant: str) -> list:
    return rate_keys.client.zrange(store.key_for(tenant), 0, -1, withscores=True)


# ── a member the script cannot read ─────────────────────────────────────────
UNREADABLE_MEMBERS = {
    "no-colon": "junk",
    "text-for-the-count": "abc:def",
    "a-negative-count": "-5:negative",
    "digits-then-text": "12abc:x",
    "no-count": ":x",
    "empty": "",
}


@pytest.mark.parametrize(
    "member", UNREADABLE_MEMBERS.values(), ids=UNREADABLE_MEMBERS.keys()
)
def test_a_member_with_no_token_count_is_read_as_no_tokens_and_the_tenant_goes_on(
    rate_keys: RateKeys, store: RedisRateLimiter, clock: FakeClock, member: str
) -> None:
    rate_keys.client.zadd(store.key_for(TENANT), {member: clock() * 1000})

    # The whole minute's tokens are still free: the planted member counts none.
    refusal = store.admit(TENANT, limits(), TOKENS)

    assert refusal is None


@pytest.mark.parametrize(
    "member", UNREADABLE_MEMBERS.values(), ids=UNREADABLE_MEMBERS.keys()
)
def test_a_member_with_no_token_count_still_counts_as_one_request(
    rate_keys: RateKeys, store: RedisRateLimiter, clock: FakeClock, member: str
) -> None:
    rate_keys.client.zadd(store.key_for(TENANT), {member: clock() * 1000})

    refusal = store.admit(TENANT, limits(requests=1), 1)

    # What the member says is only that something happened now: it is read as a
    # request, as the window's other members are, and it leaves with them.
    assert refusal == RateRefusal("tenant-request-rate", 10)


def test_the_token_hint_walks_past_a_member_with_no_token_count(
    rate_keys: RateKeys, store: RedisRateLimiter, clock: FakeClock
) -> None:
    window = limits(requests=100, tokens=100)
    rate_keys.client.zadd(store.key_for(TENANT), {"junk": clock() * 1000})
    clock.advance(5.0)
    assert store.admit(TENANT, window, 100) is None

    refusal = store.admit(TENANT, window, 100)

    # The planted member holds no tokens, so the real entry is the one that
    # must leave, 60 s after it was written: not an error from the walk.
    assert refusal == RateRefusal("tenant-token-rate", 60)


# ── a member scored in the future ───────────────────────────────────────────
FUTURE_SCORES = {
    "far-in-the-future": 99999999999999,
    "infinity": "+inf",
    "one-millisecond-past-the-longer-window": FUTURE_BOUND_MS + 1,
}


@pytest.mark.parametrize("score", FUTURE_SCORES.values(), ids=FUTURE_SCORES.keys())
def test_members_scored_in_the_future_do_not_lock_the_tenant_out(
    rate_keys: RateKeys, store: RedisRateLimiter, score: float | str
) -> None:
    key = store.key_for(TENANT)
    # As many as the request limit: counted, they would refuse the next call
    # with a wait of years.
    rate_keys.client.zadd(key, {f"0:planted{n}": score for n in range(REQUESTS)})

    refusal = store.admit(TENANT, limits(requests=REQUESTS), 1)

    assert refusal is None
    assert rate_keys.client.zcard(key) == 1  # the planted members are gone


@pytest.mark.parametrize(
    ("member", "window", "reason", "hint"),
    [
        ("0:edge", limits(requests=1), "tenant-request-rate", 70),
        ("1000:edge", limits(), "tenant-token-rate", 120),
    ],
    ids=["request-window", "token-window"],
)
def test_a_member_kept_at_the_edge_of_the_margin_is_a_429_within_the_ceiling(
    rate_keys: RateKeys,
    store: RedisRateLimiter,
    member: str,
    window: TenantLimits,
    reason: str,
    hint: int,
) -> None:
    # The longest waits the script can compute from what it keeps: the member at
    # now + 60 s leaves the request window 70 s from now, the token window 120 s.
    # Neither is a 503: the ceiling is what the script itself can answer.
    rate_keys.client.zadd(store.key_for(TENANT), {member: FUTURE_BOUND_MS})

    refusal = store.admit(TENANT, window, 1)

    assert refusal is not None
    assert (refusal.reason, refusal.retry_after_seconds) == (reason, hint)
    assert hint <= 2 * TOKEN_WINDOW_SECONDS


def test_a_member_exactly_the_longer_window_ahead_is_kept(
    rate_keys: RateKeys, store: RedisRateLimiter
) -> None:
    key = store.key_for(TENANT)
    rate_keys.client.zadd(key, {"0:edge": FUTURE_BOUND_MS})

    assert store.admit(TENANT, limits(), 1) is None

    assert rate_keys.client.zcard(key) == 2  # the edge member and the new one


def test_the_server_clock_removes_the_future_members_too(
    rate_keys: RateKeys,
) -> None:
    production = RedisRateLimiter(rate_keys.client, prefix=rate_keys.new_prefix())
    key = production.key_for(TENANT)
    rate_keys.client.zadd(key, {f"0:planted{n}": 99999999999999 for n in range(3)})

    refusal = production.admit(TENANT, limits(requests=3), 1)

    assert refusal is None
    assert rate_keys.client.zcard(key) == 1


def test_a_tenant_with_both_kinds_of_plant_is_admitted_on_the_next_two_calls(
    rate_keys: RateKeys, store: RedisRateLimiter, clock: FakeClock
) -> None:
    key = store.key_for(TENANT)
    rate_keys.client.zadd(key, {"junk": clock() * 1000})
    rate_keys.client.zadd(key, {"0:future": 99999999999999})

    first = store.admit(TENANT, limits(), 1)
    second = store.admit(TENANT, limits(), 1)

    assert (first, second) == (None, None)


# ── nobody else is affected ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("member", "score", "count"),
    # One unreadable member is read as a request, so more would be counted
    # honestly; as many future members as the limit would refuse the call if
    # they were counted.
    [("junk", 1000 * 1000, 1), ("0:future", 99999999999999, REQUESTS)],
    ids=["unreadable", "future"],
)
def test_another_tenant_never_notices_a_plant_in_a_tenants_window(
    rate_keys: RateKeys,
    store: RedisRateLimiter,
    member: str,
    score: float,
    count: int,
) -> None:
    window = limits(requests=REQUESTS)
    assert store.admit(OTHER_TENANT, window, 1) is None
    before = members_of(rate_keys, store, OTHER_TENANT)
    plant = {f"{member}{n}": score for n in range(count)}
    rate_keys.client.zadd(store.key_for(TENANT), plant)

    admitted = store.admit(TENANT, window, 1)

    assert admitted is None
    assert members_of(rate_keys, store, OTHER_TENANT) == before
    assert store.admit(OTHER_TENANT, window, 1) is None


def test_a_planted_key_that_is_not_a_window_is_still_the_stores_failure(
    rate_keys: RateKeys, store: RedisRateLimiter
) -> None:
    # The tolerance is for members, not for a key of another type: that is
    # still an error the server answers, and still no admission.
    rate_keys.client.set(store.key_for(TENANT), "not a sorted set")

    with pytest.raises(RateStoreUnavailable):
        store.admit(TENANT, limits(), 1)


# ── what two scripts share during a rolling update ──────────────────────────
def test_every_member_the_script_writes_is_a_count_and_a_random_id_at_a_time(
    rate_keys: RateKeys, store: RedisRateLimiter, clock: FakeClock
) -> None:
    # The format the previous script reads too (see the module's docstring): a
    # digit string, a colon, 16 hex digits, scored at the script's now in ms.
    store.admit(TENANT, limits(), 7)
    clock.advance(0.5)
    store.admit(TENANT, limits(), 0)

    members = members_of(rate_keys, store, TENANT)

    assert [score for _, score in members] == [1_000_000, 1_000_500]
    assert [m.decode().split(":")[0] for m, _ in members] == ["7", "0"]
    assert all(len(m.decode().split(":")[1]) == 16 for m, _ in members)
    assert all(
        set(m.decode().split(":")[1]) <= set("0123456789abcdef") for m, _ in members
    )
