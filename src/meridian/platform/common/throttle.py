"""The audit throttle of refusals (T-49), shared by the gateway, the Agent
Runtime, the tool servers and the caller check of each (S013, S069).

A refusal flood must leave one audit row per window, not one per request. The
state lives and dies with the process, which is right for one replica (C-01).
The counts of a flood's last window are written by ``refusal_summary``.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

REFUSAL_AUDIT_SECONDS = 60.0
# A flood that goes on claims a row every window, and that row carries the
# count, so a count is handed out only when no row was claimed for two windows:
# that keeps the rows of one key at one per window on average (T-49).
REFUSAL_SUMMARY_SECONDS = 2 * REFUSAL_AUDIT_SECONDS


@dataclass(slots=True)
class _RefusalWindow:
    last_audited_at: float | None = None
    suppressed: int = 0  # refusals since the last row that left no row
    # When a write failed (``release`` of a row, ``restore`` of a summary):
    # ``take_ended`` waits two windows from then, as from a row, so the next
    # refusal of the flood carries the count and a database that is down is not
    # tried with every request. ``due`` never reads it.
    released_at: float | None = None


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

    The refusals suppressed in a flood's last window are carried by no row. A
    caller that wants them written (every service does, through
    ``refusal_summary.write_ended_summaries``) calls ``take_ended`` now and
    then (with ``everything`` at shutdown), writes one row per
    ``(tenant, reason, count)`` it gets back, and calls ``restore`` with that
    count if a write fails. A count waits two windows after the key's last row,
    its last release or its last restore, so the next refusal of a flood still
    carries it in its own row, and a summary that cannot be written is tried
    again after two windows, not with every request. A caller that never calls
    ``take_ended`` sees no change.
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
        stay counted. The release is noted, so ``take_ended`` leaves the count
        to the next refusal's row for two windows."""
        with self._lock:
            window = self._windows.setdefault((tenant, reason), _RefusalWindow())
            window.last_audited_at = None
            window.released_at = self._clock()
            window.suppressed += carried + 1

    def take_ended(
        self, *, everything: bool = False
    ) -> list[tuple[str | None, str, int]]:
        """The counts no row carries yet, as ``(tenant, reason, count)``, and
        each one is handed out once: its count restarts at zero. A key's count
        is handed out when the latest of its last row's claim, its last
        release and its last restore is ``REFUSAL_SUMMARY_SECONDS`` ago (a key
        with none is quiet at once), because until then the next row of its
        flood carries the count, and a summary that failed is not tried with
        every request. With ``everything`` every count above zero is handed
        out, for a shutdown. The window of a row is not changed."""
        with self._lock:
            now = self._clock()
            ended: list[tuple[str | None, str, int]] = []
            for (tenant, reason), window in self._windows.items():
                times = (window.last_audited_at, window.released_at)
                latest = max((t for t in times if t is not None), default=None)
                quiet = latest is None or now - latest >= REFUSAL_SUMMARY_SECONDS
                if window.suppressed > 0 and (everything or quiet):
                    ended.append((tenant, reason, window.suppressed))
                    window.suppressed = 0
            return ended

    def restore(self, tenant: str | None, reason: str, count: int) -> None:
        """A count ``take_ended`` handed out could not be written: count it
        again, so a later ``due`` carries it at once, and ``take_ended`` hands
        it out again after two windows (``everything`` at once). The restore is
        noted like a release. The window of the key's row is not changed."""
        with self._lock:
            window = self._windows.setdefault((tenant, reason), _RefusalWindow())
            window.released_at = self._clock()
            window.suppressed += count
