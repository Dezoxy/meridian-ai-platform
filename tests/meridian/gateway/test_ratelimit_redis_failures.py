"""What the Redis store does with a store that misbehaves, and with a call it
must not send (S066, T-45).

A server that answers bytes the client library cannot read, or reads wrongly,
is the same refusal as one that is down: ``RateStoreUnavailable``, never a bare
``ValueError`` (a 500 with no audit row) and never an admission. The servers
here are a socket and a canned reply; no Redis is needed.
"""

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import pytest
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from meridian.platform.gateway.ratelimit import (
    TOKEN_WINDOW_SECONDS,
    RateStoreUnavailable,
)
from meridian.platform.gateway.ratelimit_redis import RedisRateLimiter
from meridian.platform.registry.models import TenantLimits

TENANT = "claims-triage"
LIMITS = TenantLimits(
    requests_per_10_seconds=5,
    tokens_per_minute=1000,
    tokens_per_day=10**6,
    cost_per_month_eur=Decimal(1),
)
HELLO_REPLY = b"%1\r\n+proto\r\n:3\r\n"
CONNECTION_SECONDS = 2
CLIENT_SECONDS = 0.5


@contextmanager
def store_that_answers(reply: bytes) -> Iterator[int]:
    """A port whose server answers the handshake and then every command with
    ``reply``, whatever it is."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def serve() -> None:
        connection, _ = listener.accept()
        connection.settimeout(CONNECTION_SECONDS)
        try:
            with connection:
                while data := connection.recv(65536):
                    connection.sendall(HELLO_REPLY if b"HELLO" in data else reply)
        except OSError:
            return

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()  # the served connection ends by its own timeout


def limiter_on(port: int) -> RedisRateLimiter:
    client = redis.Redis(
        host="127.0.0.1",
        port=port,
        socket_timeout=CLIENT_SECONDS,
        socket_connect_timeout=CLIENT_SECONDS,
        retry=Retry(NoBackoff(), 0),
        driver_info=None,
    )
    return RedisRateLimiter(client, clock=lambda: 1.0)


# What redis-py 8.1.0's parser raises out of a reply it cannot read: ValueError
# (a length or a number that is not one) and OverflowError (a length too big).
UNREADABLE = {
    "bulk-length-not-a-number": b"$abc\r\n",
    "array-length-not-a-number": b"*x\r\n",
    "integer-not-a-number": b":9x\r\n",
    "double-not-a-number": b",nope\r\n",
    "map-length-not-a-number": b"%x\r\n",
    "bulk-length-too-big": b"$999999999999999999999999\r\nabc\r\n",
    "blob-error-length-not-a-number": b"!x\r\n",
}


@pytest.mark.parametrize("reply", UNREADABLE.values(), ids=UNREADABLE.keys())
def test_a_reply_the_client_cannot_read_is_the_stores_one_exception(
    reply: bytes,
) -> None:
    with store_that_answers(reply) as port, pytest.raises(RateStoreUnavailable) as e:
        limiter_on(port).admit(TENANT, LIMITS, 1)

    assert str(port) not in str(e.value)
    assert e.value.__cause__ is None


# Readable, and not what the script returns: a pair of integers.
WRONG_SHAPE = {
    "ok": b"+OK\r\n",
    "nil": b"_\r\n",
    "one-integer": b":0\r\n",
    "short-array": b"*1\r\n:0\r\n",
    "empty-array": b"*0\r\n",
    "three-integers": b"*3\r\n:0\r\n:0\r\n:0\r\n",
    "strings": b"*2\r\n$1\r\n0\r\n$1\r\n0\r\n",
    "double-wait": b"*2\r\n:0\r\n,1.5\r\n",
    "an-unknown-outcome": b"*2\r\n:7\r\n:0\r\n",
    # bool is an int in Python: a store that answers false for "admitted" is
    # not one that admitted.
    "false-for-admitted": b"*2\r\n#f\r\n:0\r\n",
    "true-for-a-refusal": b"*2\r\n#t\r\n:5\r\n",
    "false-for-the-wait": b"*2\r\n:0\r\n#f\r\n",
}


@pytest.mark.parametrize("reply", WRONG_SHAPE.values(), ids=WRONG_SHAPE.keys())
def test_a_reply_of_another_shape_is_never_an_admission(reply: bytes) -> None:
    with store_that_answers(reply) as port, pytest.raises(RateStoreUnavailable):
        limiter_on(port).admit(TENANT, LIMITS, 1)


def test_the_replies_of_the_script_are_still_read() -> None:
    with store_that_answers(b"*2\r\n:0\r\n:0\r\n") as port:
        assert limiter_on(port).admit(TENANT, LIMITS, 1) is None
    with store_that_answers(b"*2\r\n:1\r\n:2500\r\n") as port:
        refusal = limiter_on(port).admit(TENANT, LIMITS, 1)

    assert refusal is not None
    assert (refusal.reason, refusal.retry_after_seconds) == ("tenant-request-rate", 3)


# ── a wait no legitimate window can give ────────────────────────────────────
# The script keeps members scored up to one token window ahead, and a kept one
# leaves the token window a token window after its score: twice the window.
CEILING_MS = 2 * round(TOKEN_WINDOW_SECONDS * 1000)


@pytest.mark.parametrize("outcome", [1, 2], ids=["request-window", "token-window"])
def test_a_wait_of_exactly_the_ceiling_is_still_the_tenants_rate(
    outcome: int,
) -> None:
    reply = f"*2\r\n:{outcome}\r\n:{CEILING_MS}\r\n".encode()

    with store_that_answers(reply) as port:
        refusal = limiter_on(port).admit(TENANT, LIMITS, 1)

    assert refusal is not None
    assert refusal.retry_after_seconds == 120


@pytest.mark.parametrize("outcome", [1, 2], ids=["request-window", "token-window"])
@pytest.mark.parametrize(
    "wait_ms",
    [CEILING_MS + 1, 3_600_000, 98_208_712_268_000],
    ids=["a-millisecond-over", "an-hour", "thousands-of-years"],
)
def test_a_wait_over_the_ceiling_is_the_stores_failure_not_a_rate(
    outcome: int, wait_ms: int
) -> None:
    # Nothing the script can compute from what it keeps is longer: a larger
    # number is a shape of answer it does not know, so the call is refused as the
    # store's failure (a 503 and an audit row), not as a 429 that tells the
    # caller to come back in a year.
    reply = f"*2\r\n:{outcome}\r\n:{wait_ms}\r\n".encode()

    with store_that_answers(reply) as port, pytest.raises(RateStoreUnavailable) as e:
        limiter_on(port).admit(TENANT, LIMITS, 1)

    assert "unknown shape" in str(e.value)
    assert str(wait_ms) not in str(e.value)


# ── a call it must not send ─────────────────────────────────────────────────
def test_a_negative_token_count_is_refused_before_the_store_is_called() -> None:
    # Nothing listens: had the store been called the exception would be
    # RateStoreUnavailable, not the caller's programming error.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    with pytest.raises(ValueError, match="negative"):
        limiter_on(port).admit(TENANT, LIMITS, -1)


def test_zero_tokens_are_still_a_request() -> None:
    with store_that_answers(b"*2\r\n:0\r\n:0\r\n") as port:
        assert limiter_on(port).admit(TENANT, LIMITS, 0) is None
