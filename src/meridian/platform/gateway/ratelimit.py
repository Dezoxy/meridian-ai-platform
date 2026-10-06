"""The two rate windows a tenant is held to before a call is made (S011, T-45).

Pure and free of I/O: sliding windows of requests (10 s) and tokens (60 s) per
tenant, on an injectable clock. They are Azure OpenAI's own windows, so a tenant
that would exhaust a deployment's rate limit is refused here, with a retry hint,
instead of causing a provider 429 that counts against the deployment's circuit
for every other tenant. The state lives and dies with the process, which is
right for one replica (C-01); the day and month budgets that must survive a
restart are in PostgreSQL (``budget.py``).

A refused request records nothing, so a flood cannot extend its own lockout.

Two stores keep the windows behind one method (S066, T-45): ``TenantRateLimiter``
here, the process's own, and ``RedisRateLimiter`` in ``ratelimit_redis.py``,
which shares them between processes. Both answer the same refusals with the
same hints; ``RateLimiter`` is what a caller may rely on. A gateway given the
address of the shared store (``rate_store.py``) uses it and nothing else: one
that cannot be reached is a refusal of the call, ``rate-store-unavailable``,
never a fall back to the process's own windows. Both stores are implemented and
tested against a real Redis, on loopback and without TLS; the shared one has
not run on a cluster.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from meridian.platform.registry.models import TenantLimits

REQUEST_WINDOW_SECONDS = 10.0
TOKEN_WINDOW_SECONDS = 60.0
MIN_RETRY_SECONDS = 1

RateRefusalReason = Literal[
    "tenant-request-rate", "tenant-token-rate", "tenant-request-too-large"
]
# Not an answer of a limiter: ``RedisRateLimiter.admit`` raises when its store
# gives none, and the gateway refuses the call with this word (S066).
RateStoreRefusalReason = Literal["rate-store-unavailable"]


@dataclass(frozen=True, slots=True)
class RateRefusal:
    reason: RateRefusalReason
    retry_after_seconds: int | None  # None for tenant-request-too-large


class RateLimiter(Protocol):
    """What the gateway asks of a store of rate windows: one method."""

    def admit(
        self, tenant: str, limits: TenantLimits, tokens: int
    ) -> RateRefusal | None:
        """Record the request and return ``None``, or say why it must wait."""
        ...


def _retry_after(seconds: float) -> int:
    """Whole seconds, rounded up and at least one."""
    return max(MIN_RETRY_SECONDS, math.ceil(seconds))


class TenantRateLimiter:
    """One sliding window of ``(time, tokens)`` entries per tenant, all under
    one lock: the endpoint is sync and runs in a thread pool.

    An entry is "within" a window while ``now - t`` is less than the window, so
    it leaves exactly at its window's length. Memory per tenant is bounded by
    what the limits admit in 60 s, and a tenant with no entry left is dropped.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, deque[tuple[float, int]]] = {}

    def admit(
        self, tenant: str, limits: TenantLimits, tokens: int
    ) -> RateRefusal | None:
        """Record the request and return ``None``, or say why it must wait."""
        # _refusal's AssertionError relies on this guard: a request that passes
        # it fits an empty token window.
        if tokens > limits.tokens_per_minute:
            return RateRefusal("tenant-request-too-large", None)
        with self._lock:
            now = self._clock()
            self._drop_expired(now)
            entries = self._entries.get(tenant, deque())
            refusal = self._refusal(entries, now, limits, tokens)
            if refusal is None:
                self._entries.setdefault(tenant, entries).append((now, tokens))
            return refusal

    def _drop_expired(self, now: float) -> None:
        for tenant, entries in list(self._entries.items()):
            while entries and now - entries[0][0] >= TOKEN_WINDOW_SECONDS:
                entries.popleft()
            if not entries:
                del self._entries[tenant]

    @staticmethod
    def _refusal(
        entries: deque[tuple[float, int]],
        now: float,
        limits: TenantLimits,
        tokens: int,
    ) -> RateRefusal | None:
        recent = [t for t, _ in entries if now - t < REQUEST_WINDOW_SECONDS]
        if len(recent) >= limits.requests_per_10_seconds:
            wait = recent[0] + REQUEST_WINDOW_SECONDS - now
            return RateRefusal("tenant-request-rate", _retry_after(wait))
        used = sum(n for _, n in entries)
        if used + tokens <= limits.tokens_per_minute:
            return None
        for t, n in entries:  # oldest first: how many must leave for this to fit
            used -= n
            if used + tokens <= limits.tokens_per_minute:
                wait = t + TOKEN_WINDOW_SECONDS - now
                return RateRefusal("tenant-token-rate", _retry_after(wait))
        raise AssertionError("unreachable: tokens fits an empty window")
