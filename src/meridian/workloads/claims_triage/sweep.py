"""The scheduled sweep of the claims-triage workload (S052).

``python -m meridian.workloads.claims_triage.sweep`` runs one pass and exits; a
Kubernetes CronJob starts it every five minutes. It finds the work a crash left
behind, in this order, each step safe to repeat and safe beside another pass:

1. claims that waited in a state the system should have moved them out of: a
   claim whose documents are overdue goes to an adjuster (no claim is rejected
   without a person, C-02), and a claim left ``submitted`` or ``triaging`` is
   failed, which a person sees;
2. runs that no process works on and no claim keeps: they end ``Failed``;
3. checkpoints that no unfinished run needs: they are deleted.

It connects as the database role ``claims_sweep`` (``MERIDIAN_DATABASE_URL``),
which can do these things and no others (migration 0013). It starts no triage and
raises no claim's ``triages``. It imports neither the Claims API nor LangGraph,
and needs PostgreSQL only.

Each move is its own transaction, so a failure is the claim's, the run's or the
thread's alone: it is logged by its ID, the exception's class and the sqlstate
(a message could hold claimant text, T-03), and the pass goes on. A pass is
bounded in the number of items it takes. Exit codes: 0 for a clean pass, 1 when
any item or the connection failed, 2 for a setting that is missing or invalid.
"""

import logging
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import NamedTuple
from uuid import UUID

import psycopg

from meridian.platform.common.db import DATABASE_URL_ENV, connect
from meridian.platform.common.env import SettingsError, require_env
from meridian.platform.common.logredaction import install_log_redaction
from meridian.runtime.sweep import (
    RUNNING_LEASE_SECONDS,
    SWEPT_STATUSES,
    delete_thread_checkpoints,
    end_abandoned_run,
    leftover_threads,
)
from meridian.workloads.claims_triage.lifecycle import (
    DOCUMENTS_DEADLINE_DAYS,
    DOCUMENTS_OVERDUE,
    TRIAGE_ABANDONED,
    TRIAGE_NOT_STARTED,
    Transition,
    move_claim,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "claims-sweep"
# Copies of ``AGENT`` and ``TRIAGE_LEASE_SECONDS`` of ``triaging.py`` (a test
# keeps them equal): importing that module loads FastAPI, httpx and OpenTelemetry,
# and this job needs none of them. The lease is how long a claim may stay
# ``submitted`` or ``triaging`` before another post takes the triage over, twice
# the longest a runtime call lasts.
AGENT = "claims-triage"
TRIAGE_LEASE_SECONDS = 120.0
DOCUMENTS_DEADLINE_ENV = "MERIDIAN_SWEEP_DOCUMENTS_DEADLINE_DAYS"
MIN_DEADLINE_DAYS = 1
MAX_DEADLINE_DAYS = 365
SECONDS_PER_DAY = 24 * 60 * 60
# What one pass takes, oldest first; the rest wait for the next pass. The CronJob
# gives a pass 120 s, and every statement runs under the connection's 10 s limit.
MAX_CLAIMS_PER_PASS = 200
MAX_RUNS_PER_PASS = 100
MAX_THREADS_PER_PASS = 100
EXIT_CLEAN = 0
EXIT_FAILED = 1
EXIT_SETTINGS = 2
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
FAILURE_LOG = "%s %s: the sweep could not finish it: %s (sqlstate %s)"
SUMMARY_LOG = (
    "sweep pass: %d claims referred as overdue, %d claims failed as not started, "
    "%d claims failed as abandoned, %d runs ended, %d threads cleaned, %d failures"
)
# The claim's states the sweep looks at, and the move out of each.
MOVE_FROM_STATE: Mapping[str, Transition] = {
    t.source: t for t in (DOCUMENTS_OVERDUE, TRIAGE_NOT_STARTED, TRIAGE_ABANDONED)
}
# An adjuster's queue holds the claim; the run it names is theirs to resume.
WAITING_STATE = "awaiting_adjuster"
RUNS_ENDED = "runs-ended"
THREADS_CLEANED = "threads-cleaned"
FAILURES = "failures"

STALE_CLAIMS = """
SELECT claim_id, tenant, state, state_changed_at FROM claims.claims
WHERE state = ANY(%(states)s) AND state_changed_at < now() - make_interval(
    secs => CASE WHEN state = %(documents_state)s
        THEN %(documents_seconds)s ELSE %(lease_seconds)s END)
ORDER BY state_changed_at
LIMIT %(limit)s
"""
# A claim keeps the run it names while an adjuster waits on it, and while it has
# moved so lately that the request which moved it may still be ending or resuming
# the run. The listing and the check below say it in the same words.
ABANDONED_RUNS = """
SELECT r.run_id FROM runtime.runs AS r
WHERE r.agent = %(agent)s AND r.status = ANY(%(statuses)s)
    AND r.updated_at < now() - make_interval(secs => %(lease)s)
    AND NOT EXISTS (
        SELECT 1 FROM claims.claims AS c
        WHERE c.run_id = r.run_id AND (
            c.state = %(waiting_state)s
            OR c.state_changed_at > now() - make_interval(secs => %(lease)s)
        )
    )
ORDER BY r.updated_at
LIMIT %(limit)s
"""
LOCK_RUN = "SELECT 1 FROM runtime.runs WHERE run_id = %s FOR UPDATE SKIP LOCKED"
IS_KEPT = """
SELECT EXISTS (
    SELECT 1 FROM claims.claims AS c
    WHERE c.run_id = %(run_id)s AND (
        c.state = %(waiting_state)s
        OR c.state_changed_at > now() - make_interval(secs => %(lease)s)
    )
)
"""


class StaleClaim(NamedTuple):
    claim_id: str
    tenant: str
    state: str
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class PassResult:
    overdue: int
    not_started: int
    abandoned: int
    runs_ended: int
    threads_cleaned: int
    failures: int

    @property
    def moved_claims(self) -> int:
        return self.overdue + self.not_started + self.abandoned


@dataclass(frozen=True, slots=True)
class SweepSettings:
    database_url: str = field(repr=False)
    documents_deadline_days: int


def read_settings(environ: Mapping[str, str]) -> SweepSettings:
    """Read the variables; raise ``SettingsError`` naming the one that is missing
    or invalid, never its value (a database URL can carry a password)."""
    database_url = require_env(environ, DATABASE_URL_ENV)
    raw = environ.get(DOCUMENTS_DEADLINE_ENV)
    days = DOCUMENTS_DEADLINE_DAYS if raw is None else _whole_days(raw)
    return SweepSettings(database_url, days)


def _whole_days(raw: str) -> int:
    # ASCII digits only: ``\d`` and ``int`` accept other scripts' digits.
    if re.fullmatch(r"[0-9]{1,4}", raw):
        days = int(raw)
        if MIN_DEADLINE_DAYS <= days <= MAX_DEADLINE_DAYS:
            return days
    raise SettingsError(
        f"{DOCUMENTS_DEADLINE_ENV} must be a whole number of days "
        f"from {MIN_DEADLINE_DAYS} to {MAX_DEADLINE_DAYS}"
    )


def _attempt(
    conn: psycopg.Connection, kind: str, identifier: str, action: Callable[[], bool]
) -> bool | None:
    """Run ``action`` in a transaction of its own and commit it.

    Returns what ``action`` returned (whether it changed anything), or ``None``
    when the database refused it: the transaction is rolled back, the failure is
    logged by the item's ID, the exception's class and the sqlstate (a message
    can quote what the statement held), and the pass goes on. A connection that
    has broken ends the pass instead: nothing after it could run.
    """
    try:
        changed = action()
        conn.commit()
    except psycopg.Error as exc:
        if conn.broken:
            raise
        conn.rollback()
        logger.error(
            FAILURE_LOG, kind, identifier, type(exc).__name__, exc.sqlstate or "none"
        )
        return None
    return changed


def _tally(tally: Counter[str], key: str, outcome: bool | None) -> None:
    if outcome is None:
        tally[FAILURES] += 1
    elif outcome:
        tally[key] += 1


def _stale_claims(conn: psycopg.Connection, deadline_days: int) -> list[StaleClaim]:
    rows = conn.execute(
        STALE_CLAIMS,
        {
            "states": list(MOVE_FROM_STATE),
            "documents_state": DOCUMENTS_OVERDUE.source,
            "documents_seconds": float(deadline_days * SECONDS_PER_DAY),
            "lease_seconds": TRIAGE_LEASE_SECONDS,
            "limit": MAX_CLAIMS_PER_PASS,
        },
    ).fetchall()
    conn.commit()
    return [StaleClaim(*row) for row in rows]


def _move(conn: psycopg.Connection, claim: StaleClaim) -> bool:
    """Move the claim if it is still as the listing found it: the move is a
    compare-and-set on the moment the pass read, so a claim another request
    changed since is left alone. The claim's run is cleared: no triage is
    waiting on it."""
    moved = move_claim(
        conn,
        MOVE_FROM_STATE[claim.state],
        claim_id=claim.claim_id,
        tenant=claim.tenant,
        changed_at=claim.changed_at,
        service=SERVICE_NAME,
    )
    return moved is not None


def _sweep_claims(
    conn: psycopg.Connection, deadline_days: int, tally: Counter[str]
) -> None:
    for claim in _stale_claims(conn, deadline_days):
        moved = _attempt(conn, "claim", claim.claim_id, partial(_move, conn, claim))
        _tally(tally, MOVE_FROM_STATE[claim.state].trigger, moved)


def list_abandoned_runs(conn: psycopg.Connection) -> list[UUID]:
    """The runs, at most ``MAX_RUNS_PER_PASS``, oldest first, of this workload's
    agent that are unfinished, idle past the lease and kept by no claim. The
    claims are in the listing, not only in the check, so that runs a claim keeps
    cannot fill the bound and starve the runs that are abandoned."""
    rows = conn.execute(
        ABANDONED_RUNS,
        {
            "agent": AGENT,
            "statuses": list(SWEPT_STATUSES),
            "lease": float(RUNNING_LEASE_SECONDS),
            "waiting_state": WAITING_STATE,
            "limit": MAX_RUNS_PER_PASS,
        },
    ).fetchall()
    conn.commit()
    return [run_id for (run_id,) in rows]


def end_run_unless_kept(conn: psycopg.Connection, run_id: UUID) -> bool:
    """End one run in the caller's transaction unless something keeps it.

    The row is locked first, and a row someone else has locked is skipped: a
    resume (``claim_paused_run``) locks the row it claims, so the sweep neither
    waits for it nor takes the run from it. Under the lock the claims are read
    again, so the keep rule holds at the moment of the update and not only when
    the run was listed; the update itself still compares status and age.
    """
    if conn.execute(LOCK_RUN, (run_id,)).fetchone() is None:
        return False
    kept = conn.execute(
        IS_KEPT,
        {
            "run_id": run_id,
            "waiting_state": WAITING_STATE,
            "lease": float(RUNNING_LEASE_SECONDS),
        },
    ).fetchone()
    if kept is not None and kept[0]:
        return False
    return end_abandoned_run(conn, run_id, service=SERVICE_NAME)


def _sweep_runs(conn: psycopg.Connection, tally: Counter[str]) -> None:
    for run_id in list_abandoned_runs(conn):
        ended = _attempt(
            conn, "run", str(run_id), partial(end_run_unless_kept, conn, run_id)
        )
        _tally(tally, RUNS_ENDED, ended)


def _delete(conn: psycopg.Connection, thread_id: str) -> bool:
    return delete_thread_checkpoints(conn, thread_id) > 0


def _sweep_threads(conn: psycopg.Connection, tally: Counter[str]) -> None:
    threads = leftover_threads(conn, limit=MAX_THREADS_PER_PASS)
    conn.commit()
    for thread_id in threads:
        cleaned = _attempt(conn, "thread", thread_id, partial(_delete, conn, thread_id))
        _tally(tally, THREADS_CLEANED, cleaned)


def run_pass(conn: psycopg.Connection, documents_deadline_days: int) -> PassResult:
    """One pass: claims, then abandoned runs, then leftover checkpoints.

    ``conn`` is the sweep role's. Logs one line with the counts. A listing that
    fails raises ``psycopg.Error``; a failure of one item does not.
    """
    tally: Counter[str] = Counter()
    _sweep_claims(conn, documents_deadline_days, tally)
    _sweep_runs(conn, tally)
    _sweep_threads(conn, tally)
    result = PassResult(
        overdue=tally[DOCUMENTS_OVERDUE.trigger],
        not_started=tally[TRIAGE_NOT_STARTED.trigger],
        abandoned=tally[TRIAGE_ABANDONED.trigger],
        runs_ended=tally[RUNS_ENDED],
        threads_cleaned=tally[THREADS_CLEANED],
        failures=tally[FAILURES],
    )
    logger.info(
        SUMMARY_LOG,
        result.overdue,
        result.not_started,
        result.abandoned,
        result.runs_ended,
        result.threads_cleaned,
        result.failures,
    )
    return result


def main(environ: Mapping[str, str] = os.environ) -> int:
    """Run one pass and return the exit code."""
    install_log_redaction()
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
    try:
        settings = read_settings(environ)
    except SettingsError as exc:
        logger.error("the sweep cannot start: %s", exc)
        return EXIT_SETTINGS
    try:
        with connect(settings.database_url, SERVICE_NAME) as conn:
            result = run_pass(conn, settings.documents_deadline_days)
    except psycopg.Error as exc:
        # The class and the sqlstate only: a connection error can quote the host
        # and the user, a statement error the values.
        logger.error(
            "the sweep pass could not run: %s (sqlstate %s)",
            type(exc).__name__,
            exc.sqlstate or "none",
        )
        return EXIT_FAILED
    return EXIT_CLEAN if result.failures == 0 else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
