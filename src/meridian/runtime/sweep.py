"""What the scheduled sweep does to the runtime's own tables (S052).

The sweep is a job of the claims-triage workload, with a database role of its
own (``claims_sweep``, migration 0013); the statements here are the ones that
belong to ``runtime.runs`` and the checkpoint tables, so they live beside
them. This module must stay free of LangGraph: the sweep job loads it, and the
job needs PostgreSQL and nothing else. Its checkpoint clean-up is SQL for that
reason, not the saver's ``delete_thread``.

Every function runs on the connection the caller passes and leaves the commit
to the caller, so a run's end, its audit event and its checkpoints' removal are
one transaction. Rows and audit events hold identifiers and fixed words only
(T-03, T-25); statements use psycopg placeholders only (T-07).
"""

import uuid

import psycopg
from psycopg import sql

from meridian.platform.common.audit import AuditEvent, record_event

# How long a run may stay ``Running`` before a resume may take it over: a leg
# that died, or whose last status write failed twice, leaves the run so. Well
# above the longest a live leg can take (four model calls at the gateway's
# 30 s timeout, sixteen tool calls), so a live leg is not taken over. The sweep
# ends an unfinished run only after the same time (``runs.py`` reads it from
# here, so the runtime and the sweep cannot disagree).
RUNNING_LEASE_SECONDS = 600
# The statuses of a run something may still be working on or resuming.
SWEPT_STATUSES = ("Running", "AwaitingApproval")
# The event and outcome of a run moved to ``Failed``, as ``runs.py`` writes them
# for a failure (a test keeps the two equal); the sweep's reason word follows.
RUN_FAILED_EVENT = ("run.failed", "failed")
ABANDONED_REASON = "abandoned"
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

# The compare-and-set: the run moves only while it is still unfinished and idle
# past the lease, so a resume that claimed it first (and so freshened it) wins.
END_RUN = """
UPDATE runtime.runs SET status = 'Failed', updated_at = now()
WHERE run_id = %(run_id)s AND status = ANY(%(statuses)s)
    AND updated_at < now() - make_interval(secs => %(lease)s)
RETURNING thread_id, agent, tenant, reference
"""
# A thread is a leftover when no run of it is unfinished. The checkpoint tables
# hold the thread as text and ``runtime.runs`` as a uuid.
LEFTOVER_THREADS = """
SELECT held.thread_id FROM (
    SELECT thread_id FROM runtime.checkpoints
    UNION SELECT thread_id FROM runtime.checkpoint_blobs
    UNION SELECT thread_id FROM runtime.checkpoint_writes
) AS held
WHERE NOT EXISTS (
    SELECT 1 FROM runtime.runs AS r
    WHERE r.thread_id::text = held.thread_id AND r.status = ANY(%(statuses)s)
)
ORDER BY held.thread_id
LIMIT %(limit)s
"""
DELETE_THREAD = sql.SQL(
    """
DELETE FROM runtime.{table}
WHERE thread_id = %(thread)s AND NOT EXISTS (
    SELECT 1 FROM runtime.runs AS r
    WHERE r.thread_id::text = %(thread)s AND r.status = ANY(%(statuses)s)
)
"""
)


def delete_thread_checkpoints(conn: psycopg.Connection, thread_id: str) -> int:
    """Delete the thread's rows in the three checkpoint tables, unless a run of
    the thread is unfinished (the check is in each ``DELETE``, so it holds when
    the rows go). Returns how many rows went."""
    deleted = 0
    for table in CHECKPOINT_TABLES:
        cursor = conn.execute(
            DELETE_THREAD.format(table=sql.Identifier(table)),
            {"thread": thread_id, "statuses": list(SWEPT_STATUSES)},
        )
        deleted += cursor.rowcount
    return deleted


def leftover_threads(conn: psycopg.Connection, *, limit: int) -> list[str]:
    """The threads, at most ``limit``, that hold checkpoint rows and have no run
    in ``Running`` or ``AwaitingApproval``: a finished run whose delete failed,
    or a thread with no run at all. The sweep's role reads ``thread_id`` only."""
    rows = conn.execute(
        LEFTOVER_THREADS, {"statuses": list(SWEPT_STATUSES), "limit": limit}
    ).fetchall()
    return [thread for (thread,) in rows]


def end_abandoned_run(
    conn: psycopg.Connection, run_id: uuid.UUID, *, service: str
) -> bool:
    """Move an unfinished run, idle past ``RUNNING_LEASE_SECONDS``, to ``Failed``;
    write its ``run.failed`` event with the reason ``abandoned``; delete its
    thread's checkpoints. Returns ``False``, and changes nothing, when the run is
    not in that state any more (finished, or freshened by a resume).

    The caller decides that nothing keeps the run, and holds its row lock
    (``FOR UPDATE``) while it does; without the lock this statement waits for
    whoever has the row, which a resume does for as long as its claim takes.
    """
    row = conn.execute(
        END_RUN,
        {
            "run_id": run_id,
            "statuses": list(SWEPT_STATUSES),
            "lease": float(RUNNING_LEASE_SECONDS),
        },
    ).fetchone()
    if row is None:
        return False
    thread_id, agent, tenant, reference = row
    event, outcome = RUN_FAILED_EVENT
    record_event(
        conn,
        AuditEvent(
            service=service,
            event=event,
            outcome=outcome,
            reason=ABANDONED_REASON,
            tenant=tenant,
            agent=agent,
            run_id=run_id,
            reference=reference,
        ),
    )
    delete_thread_checkpoints(conn, str(thread_id))
    return True
