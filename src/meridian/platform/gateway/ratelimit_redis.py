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

Two more things a member can be, and what the script does with each (S066, H3a).
A count of digits too big for a float (309 digits or more are ``inf`` in Lua)
is read as one more than the token limit: that fills the window, a 429 until the
member leaves, and never the error the sum of an ``inf`` ended in. Every count
the pattern accepts is a finite whole number or that ``inf``; a sign, an
exponent, a point, a name or any non-digit stops the match, so the member reads
as no tokens. The sums stay whole numbers for the limits the registry accepts
(a tenant's two limits are capped at ``MAX_RATE_LIMIT``, 10**9); a token limit
near 10**15 and more would lose whole numbers, so the walk that ends the token
refusal may not come back to zero, and then the script refuses for the whole
token window, the longest wait it knows, and never ends in an error reply. And a
window holding more members than the script can have written
(``_ENTRIES_PER_REQUEST_LIMIT`` times the request limit) is not read to its end:
one call reads that many and one more, and the one more is answered as a
refusal for the request window, with that window's wait and no walk, so what one
tenant's key holds does not slow the calls of the others (they run one at a
time on the server). That refusal changes nothing in the key.

What a holder of the gateway's credential can still do, with those two closed: it
can fill a tenant's window with members inside what the script keeps, a 429 for
that tenant alone (a wait of at most 70 s on the request window, 120 s on the
token window, or the request window's own length over the read bound) until the
members leave, so about every two minutes it must plant again; it can empty a
window, which hands the tenant its limits again; and it can load and run a
script of its own that loops, which the store's probe answers by restarting the
store. None of this reaches the ledger in PostgreSQL, whose budgets are the hard
limits: these windows are flood control. One cost stays outside the bound: the
two removals take every member scored outside what is kept in one call, so a
plant of many such members costs one slow call before it is gone.

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
_REQUEST_WINDOW_MS = round(REQUEST_WINDOW_SECONDS * 1000)
_TOKEN_WINDOW_MS = round(TOKEN_WINDOW_SECONDS * 1000)
# What one call reads, per unit of the request limit R: twelve R, and one more.
# That is a margin of two over the most the script can have written, which is six
# R whatever the clock did. The script writes an entry only while fewer than R
# entries scored after now - 10 s are there, and those scored in the future count
# too (now - score is under the window for them), so after a write at most R sit
# after ten seconds ago and any 10 s span of scores holds at most R. Each call
# drops what is a token window old, so what is kept lies in the minute before the
# newest write: R in the last ten seconds and five more spans of R, six R, with
# the clock stepping either way (the tests walk it both ways and never see more).
# An earlier reading took the span the script keeps (a token window each side of
# now, twelve request windows) for the bound, and six of the twelve for a clock
# stepping back: it cannot, which is why twelve is a margin and not the bound. It
# is left at twelve; a bound of seven (six and one for the boundary) would be safe
# too. The read must exceed what a legitimate tenant keeps, or the script would
# refuse the tenant it should admit.
_ENTRIES_PER_REQUEST_LIMIT = 2 * _TOKEN_WINDOW_MS // _REQUEST_WINDOW_MS
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
local most_entries = tonumber(ARGV[8]) * max_requests

-- A member this script did not write may be in the key (the gateway's Redis
-- user can ZADD to it): one whose count is not digits before a colon is read as
-- no tokens, not an error that fails every call of the tenant. A count of digits
-- can still be too big for a float (309 digits are inf, and inf - inf is nan, so
-- the walk below would end in its error reply): it is read as at most one more
-- than the limit, which is already more than the window holds, and a sum of
-- those stays a whole number. The bound goes first: math.min keeps its first
-- argument against a nan.
local function tokens_of(member)
  return math.min(max_tokens + 1, tonumber(string.match(member, '^(%d+):') or '0'))
end

local oldest_to_keep = string.format('%.0f', now - token_window)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', oldest_to_keep)
-- This script scores an entry at its own now, so none is ever later than the
-- now of a call that follows; one scored after now plus the longer window was
-- not written by it (or the clock stepped back by more than a window) and goes,
-- or it would count for as long as it takes the clock to reach it.
local latest_to_keep = string.format('%.0f', now + token_window)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '(' .. latest_to_keep, '+inf')
-- Read the oldest entries, as many as the script can have written and one more
-- (the call's cost must not depend on what somebody planted: every tenant's
-- calls wait behind this one). The one more says the window holds what the
-- script never wrote, and the answer is the request window's own wait, without
-- a walk. The reply is flat (a score after each member): two elements an entry.
local entries = redis.call('ZRANGE', KEYS[1], 0, most_entries, 'WITHSCORES')
if #entries > 2 * most_entries then
  return {1, request_window}
end

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
  -- Not reached while the sums are whole numbers: the walk ends at zero, and
  -- zero plus the tokens is within the limit (the call is not too large). A sum
  -- past 2^53 (a limit near 10^15 and more) loses whole numbers and the walk may
  -- not come back to zero. A window the script cannot account for is refused for
  -- the whole token window, the longest it knows, and not answered with an error
  -- that would be a 503 for every call of the tenant.
  return {2, token_window}
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
                    _REQUEST_WINDOW_MS,
                    _TOKEN_WINDOW_MS,
                    _ENTRIES_PER_REQUEST_LIMIT,
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
