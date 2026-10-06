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
production path. A ``clock`` (a callable giving seconds, as ``TenantRateLimiter``
takes one) makes the caller's time the script's instead: it exists for the tests,
which move a clock by hand and do not sleep, and nothing else passes one; no
request can reach it. ``TIME`` is wall-clock time, where the in-process store
counts on a monotonic clock: a step backwards of the clock on the store's host
keeps entries counted for longer than their window, and the key's expiry (which
refusals do not extend) bounds how long. A key's expiry always runs on the
server's clock, which a hand-moved clock does not move.

Members nobody but the script wrote. The gateway's Redis user may ``ZADD`` to any
key under the prefix without the script, so a window can hold a member the script
did not write (the cluster reviews measured both). A member whose count is not
digits before a colon is read as no tokens (it still counts as one request, for as
long as its score is inside the window). The script writes an entry scored at its
own ``now``, which is the largest score a legitimate member can have, so a member
scored later than ``now`` plus the token window is removed in the same call (a
step backwards of the server's clock by more than a window does the same to real
entries: the tenant is counted less, never locked out). A member scored inside
that margin is kept and counted, so it can make a tenant wait: at most 70 s for
the request window and at most two token windows (120 s) for the token window,
which is the longest wait the script can compute from what it keeps and the
ceiling ``_refusal`` puts on one (a longer wait is not the script's). Both end
with the member's own score passing out of the window.

Rolling update. A pod of the previous image and one of this one share the keys and
each loads its own script, which the server keeps under its own hash, so neither
replaces the other. Both write the same member (a digit string, a colon, 16 hex
digits, scored at the script's ``now`` in milliseconds) and both read the other's
(a count read from digits before a colon, a score read as a number), so the
windows are one whatever the mix. What differs is only how they treat a member
nobody wrote: the new script tolerates and removes it, the old one raises on an
unreadable one and counts a future one, so a plant made during the overlap bites
the old pod's calls for that tenant until a call of the new pod removes it.

Failure. A server that cannot be reached, that answers an error, that answers a
reply the client library cannot read or that is not the script's (a wait over
two token windows is not), or that does not answer in time raises
``RateStoreUnavailable``, whose message holds no address, no credential and
nothing the server sent. What the gateway does with it is the
caller's decision (it refuses the call, ``app.py``); this module never falls back
to anything. The client's timeouts and its no-retry rule are set where the client
is made (``rate_store.py``), not here. A tenant that is not a registry ID and a
negative token count are a caller's programming error and raise ``ValueError``
before the store is called.

Every command the store sends to the server (redis-py 8.1.0, read with MONITOR
on Redis 8.10.2): from the script ``TIME`` (only with no clock),
``ZREMRANGEBYSCORE``, ``ZRANGE``, ``ZADD`` and ``PEXPIRE``; from the client
``EVALSHA`` and, when the server does not know the script, ``SCRIPT LOAD`` and
``EVALSHA`` again (never ``EVAL``); on every new connection ``HELLO 3`` (with
``AUTH`` folded into it when the client has a user and a password; ``AUTH``
alone under protocol 2). A client built with redis-py's defaults also sends
``CLIENT MAINT_NOTIFICATIONS`` (Redis answers an unknown subcommand error, which
MONITOR does not show), ``CLIENT SETINFO LIB-NAME`` and ``CLIENT SETINFO
LIB-VER``; the gateway's (``rate_store_client``) turns all three off, so its
connection sends none of them. Nothing else: no ``SELECT`` for database 0, no
``DEL``, no ``EVAL``. The next contract turns the list into the gateway's access
list: ``CLIENT`` is not on it.
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
    RateStoreUnavailable,
    retry_after,
)
from meridian.platform.registry.models import ENTITY_ID_PATTERN, TenantLimits

DEFAULT_PREFIX = "meridian:rate"

_TENANT_ID = re.compile(ENTITY_ID_PATTERN)
_ADMITTED, _REQUEST_RATE, _TOKEN_RATE = 0, 1, 2
_TOKEN_WINDOW_MS = round(TOKEN_WINDOW_SECONDS * 1000)
# The longest wait the script can compute from what it keeps. It keeps members
# scored up to one token window after its own now (the margin below), and a kept
# one leaves the token window a token window after its score: twice the window
# (the request window's longest, 70 s, is shorter). A larger wait cannot come
# from the script, so it is an answer of an unknown shape.
_LONGEST_WAIT_MS = 2 * _TOKEN_WINDOW_MS
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

-- A member this script did not write may be in the key (the gateway's Redis
-- user can ZADD to it): one whose count is not digits before a colon is read as
-- no tokens, not an error that fails every call of the tenant.
local function tokens_of(member)
  return tonumber(string.match(member, '^(%d+):') or '0')
end

local oldest_to_keep = string.format('%.0f', now - token_window)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', oldest_to_keep)
-- This script scores an entry at its own now, so none is ever later than the
-- now of a call that follows; one scored after now plus the longer window was
-- not written by it (or the clock stepped back by more than a window) and goes,
-- or it would count for as long as it takes the clock to reach it.
local latest_to_keep = string.format('%.0f', now + token_window)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '(' .. latest_to_keep, '+inf')
local entries = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')

local requests, oldest, used = 0, nil, 0
for i = 1, #entries, 2 do
  used = used + tokens_of(entries[i])
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
    used = used - tokens_of(entries[i])
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
        if tokens < 0:  # a negative member would break the script's parse
            raise ValueError("the token count is negative")
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
                    _TOKEN_WINDOW_MS,
                ],
            )
        except (redis.RedisError, ValueError, OverflowError) as exc:
            # RedisError is what the client raises for a connection, a timeout and
            # an error the server answers. A reply it cannot read comes out of
            # its parser as the builtin ValueError (a length or a number that is
            # not one) or OverflowError (a length too big): redis-py 8.1.0's
            # parsers raise nothing else, read and fuzzed. The class name says
            # what kind; the text would carry the address or the bad bytes. Not
            # chained: a logged traceback would print them.
            raise RateStoreUnavailable(
                f"the rate store did not answer ({type(exc).__name__})"
            ) from None
        return self._refusal(reply)

    @staticmethod
    def _refusal(reply: object) -> RateRefusal | None:
        # Exact types: bool is an int in Python, so ``[False, 0]`` would read as
        # "admitted" and a store that answers booleans is not one that admitted.
        if isinstance(reply, list) and len(reply) == 2:
            outcome, wait_ms = reply
            if type(outcome) is int and type(wait_ms) is int:
                if outcome == _ADMITTED:
                    return None
                # A wait the script cannot compute from what it keeps (see
                # _LONGEST_WAIT_MS) is a shape of answer it does not know (a
                # member planted in the future gave one of thousands of years),
                # so it is the store's failure and not a tenant's rate.
                if outcome in _REFUSALS and wait_ms <= _LONGEST_WAIT_MS:
                    return RateRefusal(_REFUSALS[outcome], retry_after(wait_ms / 1000))
        raise RateStoreUnavailable("the rate store gave an answer of an unknown shape")
