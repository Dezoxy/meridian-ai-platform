"""The rate windows of ``ratelimit.py``, kept in Redis so processes share them
(S066, T-45).

The same algorithm as ``TenantRateLimiter``, in one script that Redis runs
atomically: a sorted set per tenant under ``<prefix>:<tenant>``, scored with the
time in milliseconds, whose members carry the token count and a random ID. The
script drops what left the 60 s window, counts the 10 s and 60 s windows, and
either refuses with the same reason and the same retry hint or adds the entry and
sets the key to expire after the token window. A refusal adds nothing. One round
trip, no lock in the process: two pods in a rolling update count into one window.

What is stored is the tenant's registry ID in a key, a time, a token count and a
random ID: no prompt, no personal data, no deployment. Nothing is persisted by
this code; a restart of Redis hands every tenant its windows again.

Time. Left to itself the script reads the server's ``TIME``, so two gateway
processes agree on what a window is whatever their own clocks say: that is the
production path, and a request can reach neither path's choice. A ``clock`` (a
callable giving seconds, as ``TenantRateLimiter`` takes one) makes the caller's
time the script's: it exists for the tests, which move a clock by hand and do not
sleep, and nothing else passes one. A key's expiry always runs on the server's
clock, which a hand-moved clock does not move.

Failure. A server that cannot be reached, that answers an error or that does not
answer in time raises ``RateStoreUnavailable``, whose message holds no address, no
credential and nothing the server sent. What the gateway does with it is the
caller's decision (it refuses the call, ``app.py``); this module never falls back
to anything. The client's timeouts and its no-retry rule are set where the client
is made (``rate_store.py``), not here.

Every command the store sends to the server (redis-py 8.1.0, read with MONITOR
on Redis 8.10.2): from the script ``TIME`` (only with no clock),
``ZREMRANGEBYSCORE``, ``ZRANGE``, ``ZADD`` and ``PEXPIRE``; from the client
``EVALSHA`` and, when the server does not know the script, ``SCRIPT LOAD`` and
``EVALSHA`` again (never ``EVAL``); on every new connection ``HELLO 3`` (with
``AUTH`` folded into it when the client has a user and a password; ``AUTH``
alone under protocol 2), ``CLIENT SETINFO LIB-NAME`` and ``CLIENT SETINFO
LIB-VER``. Nothing else: no ``SELECT`` for database 0, no ``DEL``, no ``EVAL``.
The next contract turns the list into the gateway's access list.
"""

import re
import secrets
from collections.abc import Callable

import redis

from meridian.platform.gateway.ratelimit import (
    REQUEST_WINDOW_SECONDS,
    TOKEN_WINDOW_SECONDS,
    RateRefusal,
    RateRefusalReason,
    _retry_after,
)
from meridian.platform.registry.models import ENTITY_ID_PATTERN, TenantLimits

DEFAULT_PREFIX = "meridian:rate"

_TENANT_ID = re.compile(ENTITY_ID_PATTERN)
_ADMITTED, _REQUEST_RATE, _TOKEN_RATE = 0, 1, 2
_REFUSALS: dict[int, RateRefusalReason] = {
    _REQUEST_RATE: "tenant-request-rate",
    _TOKEN_RATE: "tenant-token-rate",
}

# KEYS[1]: the tenant's key. ARGV: the time in ms or '' for the server's, the
# request limit, the token limit, this request's tokens, its member, and the two
# windows in ms. Returns {outcome, wait in ms}: 0 admitted, 1 the request window
# is full, 2 the token window is. An entry is inside a window while now - t is
# less than the window: ZREMRANGEBYSCORE's inclusive bound drops what is exactly
# a window old. ZRANGE returns the entries oldest first, as the in-process
# deque holds them, and the retry hint walks them the same way.
_SCRIPT = """
local now
if ARGV[1] == '' then
  local server = redis.call('TIME')
  now = tonumber(server[1]) * 1000 + math.floor(tonumber(server[2]) / 1000)
else
  now = tonumber(ARGV[1])
end
local max_requests = tonumber(ARGV[2])
local max_tokens = tonumber(ARGV[3])
local tokens = tonumber(ARGV[4])
local request_window = tonumber(ARGV[6])
local token_window = tonumber(ARGV[7])

local oldest_to_keep = string.format('%.0f', now - token_window)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', oldest_to_keep)
local entries = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')

local requests, oldest, used = 0, nil, 0
for i = 1, #entries, 2 do
  used = used + tonumber(string.match(entries[i], '^(%d+):'))
  if now - tonumber(entries[i + 1]) < request_window then
    requests = requests + 1
    oldest = oldest or tonumber(entries[i + 1])
  end
end

if requests >= max_requests then
  return {1, (oldest or now) + request_window - now}
end
if used + tokens > max_tokens then
  for i = 1, #entries, 2 do
    used = used - tonumber(string.match(entries[i], '^(%d+):'))
    if used + tokens <= max_tokens then
      return {2, tonumber(entries[i + 1]) + token_window - now}
    end
  end
  return redis.error_reply('tokens fit an empty window')
end

redis.call('ZADD', KEYS[1], now, ARGV[5])
redis.call('PEXPIRE', KEYS[1], token_window)
return {0, 0}
"""


class RateStoreUnavailable(Exception):
    """The store gave no answer: it could not be reached, it answered an error
    or it did not answer in time. The message names none of the details."""


class RedisRateLimiter:
    """``RateLimiter`` on a Redis client; thread-safe, as the client is.

    ``client`` is used as it is: its address, TLS, credentials, timeouts and
    retry rule are the caller's (the gateway's is ``rate_store_client``, which
    sets a connect and a read timeout and no retry). Nothing here retries, and a
    client built with retries would run a script a second time after a read
    timeout that may already have run it.
    """

    def __init__(
        self,
        client: redis.Redis,
        *,
        prefix: str = DEFAULT_PREFIX,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._prefix = prefix
        self._clock = clock
        self._script = client.register_script(_SCRIPT)

    def key_for(self, tenant: str) -> str:
        """The tenant's key; a ``ValueError`` unless it is a registry ID, so no
        text a request carries ever becomes part of a key."""
        if not _TENANT_ID.fullmatch(tenant):
            raise ValueError("the tenant is not a registry ID")
        return f"{self._prefix}:{tenant}"

    def admit(
        self, tenant: str, limits: TenantLimits, tokens: int
    ) -> RateRefusal | None:
        """Record the request and return ``None``, or say why it must wait."""
        key = self.key_for(tenant)
        if tokens > limits.tokens_per_minute:
            return RateRefusal("tenant-request-too-large", None)
        now = "" if self._clock is None else round(self._clock() * 1000)
        try:
            reply = self._script(
                keys=[key],
                args=[
                    now,
                    limits.requests_per_10_seconds,
                    limits.tokens_per_minute,
                    tokens,
                    f"{tokens}:{secrets.token_hex(8)}",
                    round(REQUEST_WINDOW_SECONDS * 1000),
                    round(TOKEN_WINDOW_SECONDS * 1000),
                ],
            )
        except redis.RedisError as exc:
            # The class name says what kind; the text would carry the address.
            # Not chained: a logged traceback would print it.
            raise RateStoreUnavailable(
                f"the rate store did not answer ({type(exc).__name__})"
            ) from None
        return self._refusal(reply)

    @staticmethod
    def _refusal(reply: object) -> RateRefusal | None:
        match reply:
            case [int(outcome), int(_)] if outcome == _ADMITTED:
                return None
            case [int(outcome), int(wait_ms)] if outcome in _REFUSALS:
                return RateRefusal(_REFUSALS[outcome], _retry_after(wait_ms / 1000))
        raise RateStoreUnavailable("the rate store gave an answer of an unknown shape")
