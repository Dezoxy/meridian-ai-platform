"""The audit throttle of refusals (T-49), shared by the gateway and the tool
servers (S013).

A refusal flood must leave one audit row per window, not one per request. The
state lives and dies with the process, which is right for one replica (C-01).
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

REFUSAL_AUDIT_SECONDS = 60.0


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

    A caller does ``carried = due(...)`` and, when that is not ``None``, writes
    the row and calls ``release`` if the write fails.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[tuple[str | None, str], _RefusalWindow] = {}

    def due(self, tenant: str | None, reason: str) -> int | None:
        """``None``: a row was claimed for this key inside the window, and this
        refusal is counted as suppressed. Otherwise this caller owns the row
        of a new window, which starts now so that refusals overlapping with
        this one are suppressed, and the answer is the number of refusals the
        row stands in for, besides this one. The count restarts at zero."""
        with self._lock:
            window = self._windows.setdefault((tenant, reason), _RefusalWindow())
            now = self._clock()
            last = window.last_audited_at
            if last is not None and now - last < REFUSAL_AUDIT_SECONDS:
                window.suppressed += 1
                return None
            carried = window.suppressed
            window.last_audited_at = now
            window.suppressed = 0
            return carried

    def release(self, tenant: str | None, reason: str, carried: int) -> None:
        """The row ``due`` promised could not be written: end the window, so
        the next refusal is due again, and count the refusal that had no row
        and the ``carried`` it was to stand in for. Refusals that arrived since
        stay counted."""
        with self._lock:
            window = self._windows.setdefault((tenant, reason), _RefusalWindow())
            window.last_audited_at = None
            window.suppressed += carried + 1
