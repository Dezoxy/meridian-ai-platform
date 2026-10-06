"""The per-tenant rate windows (S011), on a fake clock.

The 26 cases of the first part run against both stores (S066): the process's own
windows and the Redis ones, with the same expectations. The cases that follow are
the Redis store's alone: what two processes share, what the script does with the
server's clock, and how it fails.
"""

import itertools
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from redissupport import RateKeys
from servicesupport import FakeClock

from meridian.platform.gateway.ratelimit import (
    TOKEN_WINDOW_SECONDS,
    RateLimiter,
    RateRefusal,
    TenantRateLimiter,
)
from meridian.platform.gateway.ratelimit_redis import (
    RateStoreUnavailable,
    RedisRateLimiter,
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


@pytest.fixture(params=["in-process", "redis"])
def make_limiter(
    request: pytest.FixtureRequest, clock: FakeClock
) -> Callable[[], RateLimiter]:
    """A way to build a limiter that shares no state with the ones built before:
    the in-process one is new, and the Redis one has a key prefix of its own
    (the clock does not move between the rounds of a racing test)."""
    if request.param == "in-process":
        return lambda: TenantRateLimiter(clock=clock)
    keys: RateKeys = request.getfixturevalue("rate_keys")
    return lambda: RedisRateLimiter(keys.client, prefix=keys.new_prefix(), clock=clock)


@pytest.fixture
def limiter(make_limiter: Callable[[], RateLimiter]) -> RateLimiter:
    return make_limiter()


@pytest.fixture
def stored_entries(
    request: pytest.FixtureRequest, limiter: RateLimiter
) -> Callable[[str], int]:
    """How many entries a limiter holds for a tenant: the in-process deque's
    length, or the cardinality of the tenant's key in Redis."""
    if isinstance(limiter, TenantRateLimiter):
        return lambda tenant: len(limiter._entries[tenant])
    keys: RateKeys = request.getfixturevalue("rate_keys")
    assert isinstance(limiter, RedisRateLimiter)
    return lambda tenant: keys.client.zcard(limiter.key_for(tenant))


def admit_n(
    limiter: RateLimiter,
    count: int,
    *,
    tenant: str = TENANT,
    tokens: int = 1,
    window: TenantLimits | None = None,
) -> list[RateRefusal | None]:
    return [limiter.admit(tenant, window or limits(), tokens) for _ in range(count)]


# ── requests per 10 seconds ─────────────────────────────────────────────────
def test_requests_up_to_the_limit_are_admitted(limiter: RateLimiter) -> None:
    assert admit_n(limiter, REQUESTS) == [None] * REQUESTS


def test_one_request_over_the_limit_is_refused(limiter: RateLimiter) -> None:
    admit_n(limiter, REQUESTS)

    refusal = limiter.admit(TENANT, limits(), 1)

    assert refusal is not None
    assert refusal.reason == "tenant-request-rate"


def test_the_entry_leaves_the_request_window_exactly_at_10_seconds(
    limiter: RateLimiter, clock: FakeClock
) -> None:
    admit_n(limiter, REQUESTS)
    clock.advance(9.75)
    assert limiter.admit(TENANT, limits(), 1) is not None

    clock.advance(0.25)  # now - t == 10.0: no longer "within"

    assert limiter.admit(TENANT, limits(), 1) is None


def test_the_request_retry_hint_counts_to_the_oldest_entry_leaving(
    limiter: RateLimiter, clock: FakeClock
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
    limiter: RateLimiter, clock: FakeClock
) -> None:
    admit_n(limiter, REQUESTS)
    clock.advance(7.0)  # exactly 3.0 s left

    assert limiter.admit(TENANT, limits(), 1) == RateRefusal("tenant-request-rate", 3)


def test_the_request_retry_hint_is_at_least_one_second(
    limiter: RateLimiter, clock: FakeClock
) -> None:
    admit_n(limiter, REQUESTS)
    clock.advance(9.875)  # 0.125 s left: rounded up to 1

    assert limiter.admit(TENANT, limits(), 1) == RateRefusal("tenant-request-rate", 1)


def test_the_request_window_is_checked_before_the_token_window(
    limiter: RateLimiter,
) -> None:
    window = limits(requests=1, tokens=100)
    limiter.admit(TENANT, window, 100)

    refusal = limiter.admit(TENANT, window, 100)

    assert refusal is not None
    assert refusal.reason == "tenant-request-rate"


# ── tokens per minute ───────────────────────────────────────────────────────
def test_tokens_up_to_the_limit_are_admitted(limiter: RateLimiter) -> None:
    assert limiter.admit(TENANT, limits(), 400) is None
    assert limiter.admit(TENANT, limits(), 600) is None  # 1000 in all: at the limit


def test_one_token_over_the_limit_is_refused(limiter: RateLimiter) -> None:
    limiter.admit(TENANT, limits(), 400)
    limiter.admit(TENANT, limits(), 600)

    refusal = limiter.admit(TENANT, limits(), 1)

    assert refusal is not None
    assert refusal.reason == "tenant-token-rate"


def test_the_entry_leaves_the_token_window_exactly_at_60_seconds(
    limiter: RateLimiter, clock: FakeClock
) -> None:
    limiter.admit(TENANT, limits(requests=100), 1000)
    clock.advance(59.75)
    assert limiter.admit(TENANT, limits(requests=100), 1) is not None

    clock.advance(0.25)

    assert limiter.admit(TENANT, limits(requests=100), 1000) is None


def test_the_token_retry_hint_waits_for_enough_entries_to_leave(
    limiter: RateLimiter, clock: FakeClock
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
    limiter: RateLimiter, clock: FakeClock
) -> None:
    window = limits(requests=100)
    limiter.admit(TENANT, window, 600)  # t = 0
    clock.advance(10.0)
    limiter.admit(TENANT, window, 400)  # t = 10
    clock.advance(10.5)  # now = 20.5

    # 100 more fit once the first entry (600) leaves, at 60: 39.5 s -> 40.
    assert limiter.admit(TENANT, window, 100) == RateRefusal("tenant-token-rate", 40)


def test_the_token_retry_hint_is_at_least_one_second(
    limiter: RateLimiter, clock: FakeClock
) -> None:
    window = limits(requests=100)
    limiter.admit(TENANT, window, 1000)
    clock.advance(59.875)

    assert limiter.admit(TENANT, window, 1) == RateRefusal("tenant-token-rate", 1)


# ── a request that can never pass ───────────────────────────────────────────
def test_a_request_larger_than_the_whole_minute_is_refused_without_a_hint(
    limiter: RateLimiter,
) -> None:
    refusal = limiter.admit(TENANT, limits(), TOKENS + 1)

    assert refusal == RateRefusal("tenant-request-too-large", None)


def test_a_request_of_exactly_the_minute_limit_is_admitted(
    limiter: RateLimiter,
) -> None:
    assert limiter.admit(TENANT, limits(), TOKENS) is None


def test_too_large_is_reported_even_when_the_request_window_is_full(
    limiter: RateLimiter,
) -> None:
    admit_n(limiter, REQUESTS)

    refusal = limiter.admit(TENANT, limits(), TOKENS + 1)

    assert refusal == RateRefusal("tenant-request-too-large", None)


# ── refused requests are not recorded ───────────────────────────────────────
def test_a_flood_of_refused_requests_does_not_extend_its_own_lockout(
    limiter: RateLimiter, clock: FakeClock
) -> None:
    admit_n(limiter, REQUESTS)
    for _ in range(50):
        clock.advance(0.125)
        assert limiter.admit(TENANT, limits(), 1) is not None
    clock.advance(3.75)  # 50 x 0.125 + 3.75 = 10: the first window ends

    assert admit_n(limiter, REQUESTS) == [None] * REQUESTS


def test_refused_tokens_are_not_counted_toward_the_minute(
    limiter: RateLimiter, clock: FakeClock
) -> None:
    window = limits(requests=100)
    limiter.admit(TENANT, window, 900)
    for _ in range(20):
        assert limiter.admit(TENANT, window, 500) is not None
    clock.advance(60.0)

    assert limiter.admit(TENANT, window, 1000) is None


def test_too_large_requests_are_not_recorded(limiter: RateLimiter) -> None:
    for _ in range(10):
        limiter.admit(TENANT, limits(), TOKENS + 1)

    assert admit_n(limiter, REQUESTS) == [None] * REQUESTS


# ── tenants, memory ─────────────────────────────────────────────────────────
def test_two_tenants_do_not_share_a_window(limiter: RateLimiter) -> None:
    admit_n(limiter, REQUESTS)
    assert limiter.admit(TENANT, limits(), 1) is not None

    assert admit_n(limiter, REQUESTS, tenant=OTHER_TENANT) == [None] * REQUESTS


def test_each_tenant_is_held_to_the_limits_it_is_asked_with(
    limiter: RateLimiter,
) -> None:
    assert limiter.admit(TENANT, limits(requests=1), 1) is None
    assert limiter.admit(TENANT, limits(requests=1), 1) is not None
    assert limiter.admit(OTHER_TENANT, limits(requests=2), 1) is None
    assert limiter.admit(OTHER_TENANT, limits(requests=2), 1) is None
    assert limiter.admit(OTHER_TENANT, limits(requests=2), 1) is not None


def test_a_tenant_whose_entries_expired_is_not_kept(
    clock: FakeClock,
) -> None:
    # In-process only: any call sweeps every tenant. Redis cannot say the same
    # under a hand-moved clock (a call touches its own key, and a key leaves
    # on the server's clock); its reading is the two tests after the threads.
    limiter = TenantRateLimiter(clock=clock)
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
    limiter: RateLimiter, clock: FakeClock, stored_entries: Callable[[str], int]
) -> None:
    window = limits(requests=1000, tokens=10**6)
    for _ in range(5000):
        limiter.admit(TENANT, window, 1)
        clock.advance(0.125)

    assert stored_entries(TENANT) == 480  # 60 s at one request per 0.125 s


# ── threads ─────────────────────────────────────────────────────────────────
@pytest.mark.usefixtures("fast_switching")
def test_racing_threads_are_admitted_exactly_up_to_the_limit(
    make_limiter: Callable[[], RateLimiter],
) -> None:
    allowed = 5
    for _ in range(ROUNDS):
        limiter = make_limiter()
        results: list[RateRefusal | None] = []

        def call() -> None:
            results.append(limiter.admit(TENANT, limits(requests=allowed), 1))  # noqa: B023

        run_threads(call)

        assert len(results) == RACERS
        assert results.count(None) == allowed
        assert all(r.reason == "tenant-request-rate" for r in results if r)


@pytest.mark.usefixtures("fast_switching")
def test_racing_threads_cannot_pass_the_token_limit(
    make_limiter: Callable[[], RateLimiter],
) -> None:
    window = limits(requests=1000, tokens=400)
    for _ in range(ROUNDS):
        limiter = make_limiter()
        results: list[RateRefusal | None] = []

        def call() -> None:
            results.append(limiter.admit(TENANT, window, 100))  # noqa: B023

        run_threads(call)

        assert results.count(None) == 4


# ── the Redis store alone (S066) ────────────────────────────────────────────
def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def bare_client(port: int) -> redis.Redis:
    """A client as ``rate_store_client`` makes one, without TLS: short timeouts
    and no retry (the client of the gateway is tested in
    ``test_gateway_rate_store_settings.py``)."""
    return redis.Redis(
        host="127.0.0.1",
        port=port,
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
        retry=Retry(NoBackoff(), 0),
    )


@pytest.fixture
def two_stores(
    rate_keys: RateKeys, clock: FakeClock
) -> tuple[RedisRateLimiter, RedisRateLimiter]:
    """Two processes of the gateway: each its own client and store object, both
    on one prefix, so what the first admits the second must count."""
    prefix = rate_keys.new_prefix()
    first = RedisRateLimiter(rate_keys.new_client(), prefix=prefix, clock=clock)
    second = RedisRateLimiter(rate_keys.new_client(), prefix=prefix, clock=clock)
    return first, second


def test_two_stores_share_a_tenants_request_window(
    two_stores: tuple[RedisRateLimiter, RedisRateLimiter],
) -> None:
    first, second = two_stores
    admit_n(first, REQUESTS - 1)
    assert second.admit(TENANT, limits(), 1) is None  # the last one that fits

    refusal = first.admit(TENANT, limits(), 1)

    assert refusal is not None
    assert refusal.reason == "tenant-request-rate"


def test_two_stores_share_a_tenants_token_window(
    two_stores: tuple[RedisRateLimiter, RedisRateLimiter],
) -> None:
    first, second = two_stores
    assert first.admit(TENANT, limits(), 600) is None

    assert second.admit(TENANT, limits(), 400) is None  # 1000 in all
    refusal = first.admit(TENANT, limits(), 1)

    assert refusal is not None
    assert refusal.reason == "tenant-token-rate"


def test_the_second_store_gets_the_hint_the_first_stores_entries_make(
    two_stores: tuple[RedisRateLimiter, RedisRateLimiter], clock: FakeClock
) -> None:
    first, second = two_stores
    admit_n(first, REQUESTS)
    clock.advance(7.0)  # exactly 3.0 s left

    refusal = second.admit(TENANT, limits(), 1)

    assert refusal == RateRefusal("tenant-request-rate", 3)


def test_two_prefixes_on_one_server_do_not_share_a_window(
    rate_keys: RateKeys, clock: FakeClock
) -> None:
    first = RedisRateLimiter(
        rate_keys.client, prefix=rate_keys.new_prefix(), clock=clock
    )
    second = RedisRateLimiter(
        rate_keys.client, prefix=rate_keys.new_prefix(), clock=clock
    )
    admit_n(first, REQUESTS)
    assert first.admit(TENANT, limits(), 1) is not None

    assert admit_n(second, REQUESTS) == [None] * REQUESTS


@pytest.mark.usefixtures("fast_switching")
def test_racing_threads_on_two_stores_are_admitted_exactly_up_to_the_limit(
    rate_keys: RateKeys, clock: FakeClock
) -> None:
    allowed = 5
    clients = [rate_keys.new_client(), rate_keys.new_client()]
    for _ in range(ROUNDS):
        prefix = rate_keys.new_prefix()
        stores = [RedisRateLimiter(c, prefix=prefix, clock=clock) for c in clients]
        turns = itertools.count()
        results: list[RateRefusal | None] = []

        def call() -> None:
            store = stores[next(turns) % 2]  # noqa: B023
            results.append(store.admit(TENANT, limits(requests=allowed), 1))  # noqa: B023

        run_threads(call)

        assert len(results) == RACERS
        assert results.count(None) == allowed


@pytest.mark.usefixtures("fast_switching")
def test_racing_threads_on_two_stores_cannot_pass_the_token_limit(
    rate_keys: RateKeys, clock: FakeClock
) -> None:
    window = limits(requests=1000, tokens=400)
    clients = [rate_keys.new_client(), rate_keys.new_client()]
    for _ in range(ROUNDS):
        prefix = rate_keys.new_prefix()
        stores = [RedisRateLimiter(c, prefix=prefix, clock=clock) for c in clients]
        turns = itertools.count()
        results: list[RateRefusal | None] = []

        def call() -> None:
            store = stores[next(turns) % 2]  # noqa: B023
            results.append(store.admit(TENANT, window, 100))  # noqa: B023

        run_threads(call)

        assert results.count(None) == 4


# Without a clock the script reads the server's TIME. A test cannot wait for it
# to move, so it writes with a hand-set time and reads with none.
def test_an_entry_written_with_a_time_far_in_the_past_is_gone_at_the_next_call(
    rate_keys: RateKeys,
) -> None:
    prefix = rate_keys.new_prefix()
    past = RedisRateLimiter(rate_keys.client, prefix=prefix, clock=lambda: 0.5)
    server = RedisRateLimiter(rate_keys.client, prefix=prefix)
    assert past.admit(TENANT, limits(requests=1), 1) is None

    refusal = server.admit(TENANT, limits(requests=1), 1)

    assert refusal is None
    assert rate_keys.client.zcard(server.key_for(TENANT)) == 1


def test_an_entry_written_at_the_servers_present_is_counted(
    rate_keys: RateKeys,
) -> None:
    prefix = rate_keys.new_prefix()
    first = RedisRateLimiter(rate_keys.client, prefix=prefix)
    second = RedisRateLimiter(rate_keys.client, prefix=prefix)
    assert first.admit(TENANT, limits(requests=1), 1) is None

    refusal = second.admit(TENANT, limits(requests=1), 1)

    assert refusal is not None
    assert refusal.reason == "tenant-request-rate"


def test_a_key_is_set_to_expire_about_the_token_window_after_an_admission(
    rate_keys: RateKeys, clock: FakeClock
) -> None:
    store = RedisRateLimiter(
        rate_keys.client, prefix=rate_keys.new_prefix(), clock=clock
    )

    store.admit(TENANT, limits(), 1)

    millis = rate_keys.client.pttl(store.key_for(TENANT))
    assert 50_000 < millis <= TOKEN_WINDOW_SECONDS * 1000  # the server's clock


def test_a_refusal_adds_no_member(rate_keys: RateKeys, clock: FakeClock) -> None:
    store = RedisRateLimiter(
        rate_keys.client, prefix=rate_keys.new_prefix(), clock=clock
    )
    window = limits(requests=2, tokens=100)
    store.admit(TENANT, window, 60)
    store.admit(TENANT, window, 40)
    key = store.key_for(TENANT)

    by_requests = store.admit(TENANT, window, 1)
    clock.advance(10.0)  # both entries leave the request window, not the token one
    by_tokens = store.admit(TENANT, window, 1)

    assert by_requests is not None
    assert by_requests.reason == "tenant-request-rate"
    assert by_tokens is not None
    assert by_tokens.reason == "tenant-token-rate"
    assert rate_keys.client.zcard(key) == 2


def test_a_tenant_visited_after_its_entries_expired_keeps_only_the_new_entry(
    rate_keys: RateKeys, clock: FakeClock
) -> None:
    # The Redis reading of "a tenant whose entries expired is not kept": a call
    # touches its own key, and the key itself leaves by its expiry, on the
    # server's clock, which a hand-moved clock does not move.
    store = RedisRateLimiter(
        rate_keys.client, prefix=rate_keys.new_prefix(), clock=clock
    )
    store.admit(TENANT, limits(), 1)
    clock.advance(TOKEN_WINDOW_SECONDS - 0.25)
    store.admit(TENANT, limits(), 1)
    assert rate_keys.client.zcard(store.key_for(TENANT)) == 2

    clock.advance(0.25)
    store.admit(TENANT, limits(), 1)

    assert rate_keys.client.zcard(store.key_for(TENANT)) == 2  # the first is gone


def test_the_script_is_sent_again_when_the_server_lost_its_script_cache(
    rate_keys: RateKeys, clock: FakeClock
) -> None:
    store = RedisRateLimiter(
        rate_keys.client, prefix=rate_keys.new_prefix(), clock=clock
    )
    store.admit(TENANT, limits(), 1)

    rate_keys.client.script_flush()

    assert store.admit(TENANT, limits(), 1) is None
    assert rate_keys.client.zcard(store.key_for(TENANT)) == 2


@pytest.mark.parametrize(
    "tenant",
    [
        "",
        "Claims",
        "claims:triage",
        "claims triage",
        "claims-triage\n",
        "-claims",
        "a*",
    ],
)
def test_a_tenant_that_is_not_a_registry_id_never_reaches_a_key(
    rate_keys: RateKeys, clock: FakeClock, tenant: str
) -> None:
    prefix = rate_keys.new_prefix()
    store = RedisRateLimiter(rate_keys.client, prefix=prefix, clock=clock)

    with pytest.raises(ValueError) as raised:
        store.admit(tenant, limits(), 1)

    assert str(raised.value) == "the tenant is not a registry ID"
    assert rate_keys.keys(prefix) == []


def test_a_registry_id_with_digits_and_hyphens_is_a_tenant(
    rate_keys: RateKeys, clock: FakeClock
) -> None:
    store = RedisRateLimiter(
        rate_keys.client, prefix=rate_keys.new_prefix(), clock=clock
    )

    assert store.admit("claims-triage-2", limits(), 1) is None


# ── how the Redis store fails ───────────────────────────────────────────────
def test_an_unreachable_store_raises_the_one_exception_without_its_address() -> None:
    port = closed_port()
    store = RedisRateLimiter(bare_client(port))

    with pytest.raises(RateStoreUnavailable) as raised:
        store.admit(TENANT, limits(), 1)

    assert "127.0.0.1" not in str(raised.value)
    assert str(port) not in str(raised.value)
    assert raised.value.__cause__ is None


def test_a_store_that_does_not_answer_raises_the_one_exception() -> None:
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()  # takes the connection and never replies
        port = silent.getsockname()[1]
        store = RedisRateLimiter(bare_client(port))

        with pytest.raises(RateStoreUnavailable) as raised:
            store.admit(TENANT, limits(), 1)

    assert str(port) not in str(raised.value)


def test_an_error_the_server_answers_is_the_same_exception_without_its_text(
    rate_keys: RateKeys, clock: FakeClock
) -> None:
    store = RedisRateLimiter(
        rate_keys.client, prefix=rate_keys.new_prefix(), clock=clock
    )
    rate_keys.client.set(store.key_for(TENANT), "not a sorted set")

    with pytest.raises(RateStoreUnavailable) as raised:
        store.admit(TENANT, limits(), 1)

    assert "WRONGTYPE" not in str(raised.value)
    assert store.key_for(TENANT) not in str(raised.value)


def test_a_request_too_large_is_answered_before_the_server_is_called() -> None:
    store = RedisRateLimiter(bare_client(closed_port()))

    refusal = store.admit(TENANT, limits(), TOKENS + 1)

    assert refusal == RateRefusal("tenant-request-too-large", None)
