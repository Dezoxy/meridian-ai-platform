"""The scheduled sweep of the claims-triage workload (S052).

``python -m meridian.workloads.claims_triage.sweep`` runs one pass and exits; a
Kubernetes CronJob starts it every five minutes. It finds the work a crash left
behind, in this order, each step safe to repeat and safe beside another pass:

1. claims that waited in a state the system should have moved them out of: a
   claim whose documents are overdue goes to an adjuster (no claim is rejected
   without a person, C-02), and a claim left ``submitted`` or ``triaging`` is
   failed, which a person sees;
2. runs that no process works on and no claim or claim brief keeps: they end
   ``Failed``;
3. checkpoints that no unfinished run needs: they are deleted.

It connects as the database role ``claims_sweep`` (``MERIDIAN_DATABASE_URL``),
which can do these things and no others (migration 0014). It starts no triage and
raises no claim's ``triages``. It imports neither the Claims API nor LangGraph,
and needs PostgreSQL only.

Each move is its own transaction, so a failure is the claim's, the run's or the
thread's alone: it is logged by its ID, the exception's class and the sqlstate
(a message could hold claimant text, T-03), and the pass goes on. A pass is
bounded in the number of items it takes. Exit codes: 0 for a clean pass, 1 when
any item or the connection failed, 2 for a setting that is missing or invalid.
After a pass it sends the counts as gauges (``sweep_meters``), when the
collector's address is set; that changes neither the exit code nor the summary.
"""

import logging
import os
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
from meridian.platform.common.logformat import configure_logging
from meridian.platform.common.logredaction import install_log_redaction
from meridian.runtime.sweep import (
    RUNNING_LEASE_SECONDS,
    SWEPT_STATUSES,
    delete_thread_checkpoints,
    end_abandoned_run,
    leftover_threads,
    lock_run,
)
from meridian.workloads.claims_triage.lifecycle import (
    AGENT,
    BRIEF_AGENT,
    DOCUMENTS_DEADLINE_ENV,
    DOCUMENTS_OVERDUE,
    TRIAGE_ABANDONED,
    TRIAGE_LEASE_SECONDS,
    TRIAGE_NOT_STARTED,
    Transition,
    deadline_days_of,
    deadline_seconds,
    move_claim,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "claims-sweep"
# What one pass takes: at most this many claims from each listing (one per move),
# runs and threads; the rest wait for the next pass. The CronJob gives a pass
# 120 s, and every statement runs under the connection's 10 s limit. A listing is
# a random sample of the stale items, not the oldest: every stale item is due, so
# nothing needs the order, and an item that fails every pass (a row the audit
# table refuses, say) would otherwise keep its place at the head of the listing
# and block everything behind it for good. The sample finds the others.
MAX_CLAIMS_PER_LISTING = 100
MAX_RUNS_PER_PASS = 100
MAX_THREADS_PER_PASS = 100
EXIT_CLEAN = 0
EXIT_FAILED = 1
EXIT_SETTINGS = 2
FAILURE_LOG = "%s %s: the sweep could not finish it: %s (sqlstate %s)"
SUMMARY_LOG = (
    "sweep pass: %d claims referred as overdue, %d claims failed as not started, "
    "%d claims failed as abandoned, %d runs ended, %d threads cleaned, %d failures"
)
# The claims' moves, in the order of their listings. A claim stranded in a state
# the system should have left is moved before one whose documents are overdue, so
# a backlog of the second cannot hold back the first (each listing has its own
# bound as well).
MOVES = (TRIAGE_NOT_STARTED, TRIAGE_ABANDONED, DOCUMENTS_OVERDUE)
# An adjuster's queue holds the claim; the run it names is theirs to resume.
WAITING_STATE = "awaiting_adjuster"
# The runs the sweep ends when abandoned: a closed tuple of the two agents, never
# a pattern. A brief waits for the adjuster's decision in its own state.
SWEPT_AGENTS = (AGENT, BRIEF_AGENT)
WAITING_BRIEF_STATE = "awaiting_decision"
RUNS_ENDED = "runs-ended"
THREADS_CLEANED = "threads-cleaned"
FAILURES = "failures"

STALE_CLAIMS = """
SELECT claim_id, tenant, state_changed_at FROM claims.claims
WHERE state = %(state)s
    AND state_changed_at < now() - make_interval(secs => %(seconds)s)
ORDER BY random()
LIMIT %(limit)s
"""
# A claim keeps the run it names while an adjuster waits on it, and while it has
# moved so lately that the request which moved it may still be ending or resuming
# the run. A claim brief (S037) keeps its run the same way: while it waits for its
# decision, and while it has changed within the lease. Only a claim or a brief of
# the run's own tenant keeps it, as ``audit.claim_trail`` links a claim to a run.
# The listing and the check below say it in the same words. The sweep reads a
# brief's run, tenant, state and time, never its text (migration 0024).
ABANDONED_RUNS = """
SELECT r.run_id FROM runtime.runs AS r
WHERE r.agent = ANY(%(agents)s) AND r.status = ANY(%(statuses)s)
    AND r.updated_at < now() - make_interval(secs => %(lease)s)
    AND NOT EXISTS (
        SELECT 1 FROM claims.claims AS c
        WHERE c.run_id = r.run_id AND c.tenant = r.tenant AND (
            c.state = %(waiting_state)s
            OR c.state_changed_at > now() - make_interval(secs => %(lease)s)
        )
    )
    AND NOT EXISTS (
        SELECT 1 FROM claims.briefs AS b
        WHERE b.run_id = r.run_id AND b.tenant = r.tenant AND (
            b.state = %(waiting_brief_state)s
            OR b.state_changed_at > now() - make_interval(secs => %(lease)s)
        )
    )
ORDER BY random()
LIMIT %(limit)s
"""
IS_KEPT = """
SELECT EXISTS (
    SELECT 1 FROM claims.claims AS c
    WHERE c.run_id = %(run_id)s AND c.tenant = %(tenant)s AND (
        c.state = %(waiting_state)s
        OR c.state_changed_at > now() - make_interval(secs => %(lease)s)
    )
) OR EXISTS (
    SELECT 1 FROM claims.briefs AS b
    WHERE b.run_id = %(run_id)s AND b.tenant = %(tenant)s AND (
        b.state = %(waiting_brief_state)s
        OR b.state_changed_at > now() - make_interval(secs => %(lease)s)
    )
)
"""


class StaleClaim(NamedTuple):
    claim_id: str
    tenant: str
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
    days = deadline_days_of(environ.get(DOCUMENTS_DEADLINE_ENV))
    return SweepSettings(database_url, days)


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


def _step(
    conn: psycopg.Connection, name: str, tally: Counter[str], step: Callable[[], None]
) -> None:
    """Run one listing and the moves it leads to; a database error that ends it
    is logged by the step's name, the class and the sqlstate, counted, and rolled
    back, and the next step runs: a claims listing that fails must not leave the
    runs and the checkpoints unswept (T-63). A broken connection ends the pass."""
    try:
        step()
    except psycopg.Error as exc:
        if conn.broken:
            raise
        conn.rollback()
        logger.error(
            FAILURE_LOG, "step", name, type(exc).__name__, exc.sqlstate or "none"
        )
        tally[FAILURES] += 1


def stale_claims(
    conn: psycopg.Connection, transition: Transition, seconds: float
) -> list[StaleClaim]:
    """A random sample, at most ``MAX_CLAIMS_PER_LISTING``, of the claims that
    have been in the transition's source state for longer than ``seconds``."""
    rows = conn.execute(
        STALE_CLAIMS,
        {
            "state": transition.source,
            "seconds": seconds,
            "limit": MAX_CLAIMS_PER_LISTING,
        },
    ).fetchall()
    conn.commit()
    return [StaleClaim(*row) for row in rows]


def _move(conn: psycopg.Connection, transition: Transition, claim: StaleClaim) -> bool:
    """Move the claim if it is still as the listing found it: the move is a
    compare-and-set on the moment the pass read, so a claim another request
    changed since is left alone. The claim's run is cleared: no triage is
    waiting on it."""
    moved = move_claim(
        conn,
        transition,
        claim_id=claim.claim_id,
        tenant=claim.tenant,
        changed_at=claim.changed_at,
        service=SERVICE_NAME,
    )
    return moved is not None


def _sweep_transition(
    conn: psycopg.Connection,
    transition: Transition,
    seconds: float,
    tally: Counter[str],
) -> None:
    for claim in stale_claims(conn, transition, seconds):
        moved = _attempt(
            conn, "claim", claim.claim_id, partial(_move, conn, transition, claim)
        )
        _tally(tally, transition.trigger, moved)


def _sweep_claims(
    conn: psycopg.Connection, deadline_days: int, tally: Counter[str]
) -> None:
    for transition in MOVES:
        seconds = (
            deadline_seconds(deadline_days)
            if transition is DOCUMENTS_OVERDUE
            else TRIAGE_LEASE_SECONDS
        )
        step = partial(_sweep_transition, conn, transition, seconds, tally)
        _step(conn, transition.trigger, tally, step)


def list_abandoned_runs(conn: psycopg.Connection) -> list[UUID]:
    """A random sample, at most ``MAX_RUNS_PER_PASS``, of the runs of this
    workload's two agents (triage and the claim brief) that are unfinished, idle
    past the lease and kept by no claim and no brief. The claims and the briefs
    are in the listing, not only in the check, so that runs they keep
    cannot fill the bound and starve the runs that are abandoned."""
    rows = conn.execute(
        ABANDONED_RUNS,
        {
            "agents": list(SWEPT_AGENTS),
            "statuses": list(SWEPT_STATUSES),
            "lease": float(RUNNING_LEASE_SECONDS),
            "waiting_state": WAITING_STATE,
            "waiting_brief_state": WAITING_BRIEF_STATE,
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
    tenant = lock_run(conn, run_id)
    if tenant is None:
        return False
    kept = conn.execute(
        IS_KEPT,
        {
            "run_id": run_id,
            "tenant": tenant,
            "waiting_state": WAITING_STATE,
            "waiting_brief_state": WAITING_BRIEF_STATE,
            "lease": float(RUNNING_LEASE_SECONDS),
        },
    ).fetchone()
    if kept is not None and kept[0]:
        return False
    # A brief run's event names no claim: the claim's trail takes every event of
    # this role that does, and the adjuster's page would read it as the triage's.
    return end_abandoned_run(
        conn, run_id, service=SERVICE_NAME, unreferenced_agents=(BRIEF_AGENT,)
    )


def _sweep_runs(conn: psycopg.Connection, tally: Counter[str]) -> None:
    _step(conn, "runs", tally, partial(_end_listed_runs, conn, tally))


def _end_listed_runs(conn: psycopg.Connection, tally: Counter[str]) -> None:
    for run_id in list_abandoned_runs(conn):
        ended = _attempt(
            conn, "run", str(run_id), partial(end_run_unless_kept, conn, run_id)
        )
        _tally(tally, RUNS_ENDED, ended)


def _delete(conn: psycopg.Connection, thread_id: str) -> bool:
    return delete_thread_checkpoints(conn, thread_id) > 0


def _sweep_threads(conn: psycopg.Connection, tally: Counter[str]) -> None:
    _step(conn, "threads", tally, partial(_clean_listed_threads, conn, tally))


def _clean_listed_threads(conn: psycopg.Connection, tally: Counter[str]) -> None:
    threads = leftover_threads(conn, limit=MAX_THREADS_PER_PASS)
    conn.commit()
    for thread_id in threads:
        cleaned = _attempt(conn, "thread", thread_id, partial(_delete, conn, thread_id))
        _tally(tally, THREADS_CLEANED, cleaned)


def run_pass(conn: psycopg.Connection, documents_deadline_days: int) -> PassResult:
    """One pass: claims, then abandoned runs, then leftover checkpoints.

    ``conn`` is the sweep role's. Logs one line with the counts. A failure of one
    item, or of one step's listing, is logged and counted and does not stop the
    others; only a broken connection raises ``psycopg.Error``.
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
    configure_logging(SERVICE_NAME)
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
    except Exception as exc:  # whatever else, so that no traceback is printed
        # The class only: the message, and the traceback a crash would print to
        # stderr, are outside the redacted log line and can hold anything.
        logger.error("the sweep pass could not run: %s", type(exc).__name__)
        return EXIT_FAILED
    # Imported here, not at the top: its provider module loads a web stack that
    # this job otherwise does not (a test holds that). The import and the call
    # are one guard: neither may change the pass's exit code or print a traceback.
    try:
        from meridian.workloads.claims_triage.sweep_meters import report_pass

        report_pass(result)
    except Exception as exc:
        logger.warning("the sweep's metrics were not sent: %s", type(exc).__name__)
    return EXIT_CLEAN if result.failures == 0 else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
