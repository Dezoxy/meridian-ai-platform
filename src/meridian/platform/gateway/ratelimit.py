"""The two rate windows a tenant is held to before a call is made (S011, T-45).

Pure and free of I/O: sliding windows of requests (10 s) and tokens (60 s) per
tenant, on an injectable clock. They are Azure OpenAI's own windows, so a tenant
that would exhaust a deployment's rate limit is refused here, with a retry hint,
instead of causing a provider 429 that counts against the deployment's circuit
for every other tenant. The state lives and dies with the process, which is
right for one replica (C-01); the day and month budgets that must survive a
restart are in PostgreSQL (``budget.py``).

A refused request records nothing, so a flood cannot extend its own lockout.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from meridian.platform.registry.models import TenantLimits

REQUEST_WINDOW_SECONDS = 10.0
TOKEN_WINDOW_SECONDS = 60.0
MIN_RETRY_SECONDS = 1
REFUSAL_AUDIT_SECONDS = 60.0

RateRefusalReason = Literal[
    "tenant-request-rate", "tenant-token-rate", "tenant-request-too-large"
]


@dataclass(frozen=True, slots=True)
class RateRefusal:
    reason: RateRefusalReason
    retry_after_seconds: int | None  # None for tenant-request-too-large


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


@dataclass(slots=True)
class _RefusalWindow:
    last_audited_at: float | None = None
    suppressed: int = 0  # refusals since the last row that left no row


class RefusalAuditThrottle:
    """Says whether a refusal is due a row in the audit log, so a flood leaves
    one row per window and not one per request (T-49), and how many refusals
    the row stands in for.

    The key is a tenant and a reason. The tenant is a registry ID, or ``None``
    for a request whose tenant is unknown: the header's value is caller-chosen
    and is never a key, so the map is bounded by tenants times reasons. A
    refusal inside the window records nothing but its count, so a flood cannot
    keep its own row away.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[tuple[str | None, str], _RefusalWindow] = {}

    def due(self, tenant: str | None, reason: str) -> int | None:
        """``None``: a row was written for this key inside the window, and this
        refusal is counted as suppressed. Otherwise the number suppressed since
        the last row, to be written on the row. The window is not started
        yet, and this refusal is counted as suppressed until ``mark`` says its
        row was written, so a write that fails loses nothing: the next refusal
        is due again and its row carries this one."""
        with self._lock:
            window = self._windows.setdefault((tenant, reason), _RefusalWindow())
            now = self._clock()
            last = window.last_audited_at
            inside = last is not None and now - last < REFUSAL_AUDIT_SECONDS
            carried = window.suppressed
            window.suppressed += 1
            return None if inside else carried

    def mark(self, tenant: str | None, reason: str) -> None:
        """The row was written: the window starts now and the count is zero."""
        with self._lock:
            window = self._windows.setdefault((tenant, reason), _RefusalWindow())
            window.last_audited_at = self._clock()
            window.suppressed = 0
