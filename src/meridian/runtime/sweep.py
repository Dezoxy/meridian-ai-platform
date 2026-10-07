"""What the scheduled sweep does to the runtime's own tables (S052).

The sweep is a job of the claims-triage workload, with a database role of its
own (``claims_sweep``, migration 0014); the statements here are the ones that
belong to ``runtime.runs`` and the checkpoint tables, so they live beside
them. This module must stay free of LangGraph and of every other agent
framework: the sweep job loads it, and the job needs PostgreSQL and nothing
else. Its checkpoint clean-up is SQL for that reason, not a saver's or a
store's own delete.

Every function runs on the connection the caller passes and leaves the commit
to the caller, so a run's end, its audit event and its checkpoints' removal are
one transaction. Rows and audit events hold identifiers and fixed words only
(T-03, T-25); statements use psycopg placeholders only (T-07).
"""

import uuid
from collections.abc import Collection

import psycopg
from psycopg import sql

from meridian.platform.common.audit import AuditEvent, record_event

# How long a run may stay ``Running`` before a resume may take it over: a leg
# that died, or whose last status write failed twice, leaves the run so. Well
# above the longest a live leg can take (four model calls, each at most the
# 30 s deadline plus one 30 s read timeout, and sixteen tool calls of 10 s: 400 s;
# a test multiplies the constants), so a live leg is not taken over. Not a
# ceiling for one case: model-call response headers that trickle (each wait
# under the read timeout; httpx has no timeout for a whole request). A leg
# that outlives the lease writes nothing over the run (its end matches its own
# claim, ``runs.py``); its tool calls bind until it ends (T-10). The sweep
# ends an unfinished run only after the same time (``runs.py`` reads it from
# here, so the runtime and the sweep cannot disagree).
RUNNING_LEASE_SECONDS = 600
# The statuses of a run something may still be working on or resuming.
SWEPT_STATUSES = ("Running", "AwaitingApproval")
# The event and outcome of a run moved to ``Failed``, as ``runs.py`` writes them
# for a failure (a test keeps the two equal); the sweep's reason word follows.
RUN_FAILED_EVENT = ("run.failed", "failed")
ABANDONED_REASON = "abandoned"
# The three tables of LangGraph's saver (0008) and the second host's own,
# ``workflow_checkpoints`` (0023, S037), named here and not imported: its store
# imports the second framework, and this module must not. Every one has a
# ``thread_id`` text column with an index that leads with it, which is all the
# statements below assume.
CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "workflow_checkpoints",
)
# How many threads of each checkpoint table the listing of leftovers looks at per
# thread it may return: a candidate that belongs to a live run is dropped, so the
# listing looks at more threads than it lists. A multiple, not a fixed number, so
# that a larger ``limit`` still has candidates to fill it.
CANDIDATES_PER_LIMIT = 3
# The one spelling of a uuid that equals ``runs.thread_id::text``: PostgreSQL
# prints a uuid in lower case, hyphenated, with no braces. Text of any other
# shape (a thread of no run, ``orphan-1``, an upper-case copy of a run's ID)
# equals no run's thread, and the listing never casts it.
CANONICAL_UUID = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# The compare-and-set: the run moves only while it is still unfinished and idle
# past the lease, so a resume that claimed it first (and so freshened it) wins.
END_RUN = """
UPDATE runtime.runs SET status = 'Failed', updated_at = now()
WHERE run_id = %(run_id)s AND status = ANY(%(statuses)s)
    AND updated_at < now() - make_interval(secs => %(lease)s)
RETURNING thread_id, agent, tenant, reference
"""
# A thread is a leftover when no run of it is unfinished. The checkpoint tables
# hold the thread as text and ``runtime.runs`` as a uuid. Comparing the run's
# thread cast to text would read every run, so a candidate is cast to a uuid
# instead and looked up in the unique index of ``runs.thread_id``. The cast
# happens only for text in the canonical form (a ``CASE``, which PostgreSQL
# does not evaluate eagerly, so ``orphan-1`` never reaches it); a candidate of
# another form equals no ``r.thread_id::text`` and so has no run. For text in
# the canonical form, equal as a uuid is equal as text, so no second comparison
# of the text is needed. The lookup is a ``LEFT JOIN LATERAL`` with ``LIMIT 1``,
# a probe per candidate: the planner turned the same test written as ``NOT
# EXISTS`` into a hash anti-join that read every run. It is one statement per
# pass.
#
# It reads a bounded number of checkpoint rows, not every row: in each table a
# recursive CTE walks the index on ``thread_id`` (a loose index scan) from
# ``start``, taking the next distinct thread after the last, at most ``walk``
# times, and wraps to the start of the index once if it runs off the end (it
# stops when it comes back to its first thread). Only these candidates meet the
# anti-join on ``runs``, and the ones at or after ``start`` come first, in
# ``thread_id`` order, then the wrapped ones. The sweep draws ``start`` at random
# on every pass, so the sample varies from pass to pass and threads whose delete
# fails every pass cannot fill every pass.
_THREAD_WALK = """
{table}_walk(thread_id, n, first_thread) AS (
    SELECT first.thread_id, 1, first.thread_id
    FROM (SELECT COALESCE(
        (SELECT thread_id FROM runtime.{table}
            WHERE thread_id >= %(start)s ORDER BY thread_id LIMIT 1),
        (SELECT thread_id FROM runtime.{table} ORDER BY thread_id LIMIT 1)
    ) AS thread_id) AS first
    WHERE first.thread_id IS NOT NULL
    UNION ALL
    SELECT nxt.thread_id, w.n + 1, w.first_thread
    FROM {table}_walk AS w
    CROSS JOIN LATERAL (SELECT COALESCE(
        (SELECT thread_id FROM runtime.{table}
            WHERE thread_id > w.thread_id ORDER BY thread_id LIMIT 1),
        (SELECT thread_id FROM runtime.{table} ORDER BY thread_id LIMIT 1)
    ) AS thread_id) AS nxt
    WHERE w.n < %(walk)s AND nxt.thread_id <> w.first_thread
)"""
_LEFTOVER_THREADS = """
WITH RECURSIVE {walks},
held AS (
    {candidates}
)
SELECT held.thread_id FROM held
LEFT JOIN LATERAL (
    SELECT 1 AS live FROM runtime.runs AS r
    WHERE r.thread_id = CASE WHEN held.thread_id ~ '{canonical_uuid}'
        THEN held.thread_id::uuid END
        AND r.status = ANY(%(statuses)s)
    LIMIT 1
) AS run ON true
WHERE run.live IS NULL
ORDER BY held.thread_id < %(start)s, held.thread_id
LIMIT %(limit)s
"""
# The tables are this module's constants; no value of a caller is in the text.
LEFTOVER_THREADS = _LEFTOVER_THREADS.format(
    canonical_uuid=CANONICAL_UUID,
    walks=",".join(_THREAD_WALK.format(table=table) for table in CHECKPOINT_TABLES),
    candidates="\n    UNION ".join(
        f"SELECT thread_id FROM {table}_walk"  # noqa: S608
        for table in CHECKPOINT_TABLES
    ),
)
# One delete per thread and table, so these compare as uuids and use the index of
# ``runs.thread_id``. A thread that is not the canonical text of a uuid cannot be
# any run's (the saver writes ``str(thread_id)``), so its rows go unguarded.
GUARDED_DELETE = """
DELETE FROM runtime.{table}
WHERE thread_id = %(thread)s AND NOT EXISTS (
    SELECT 1 FROM runtime.runs AS r
    WHERE r.thread_id = %(run_thread)s AND r.status = ANY(%(statuses)s)
)
"""
UNGUARDED_DELETE = "DELETE FROM runtime.{table} WHERE thread_id = %(thread)s"


def _run_thread(thread_id: str) -> uuid.UUID | None:
    """The uuid whose canonical text is ``thread_id``, or ``None`` if there is none."""
    try:
        parsed = uuid.UUID(thread_id)
    except ValueError:
        return None
    return parsed if str(parsed) == thread_id else None


def delete_thread_checkpoints(conn: psycopg.Connection, thread_id: str) -> int:
    """Delete the thread's rows in every checkpoint table, unless a run of
    the thread is unfinished (the check is in each ``DELETE``, so it holds when
    the rows go). Returns how many rows went."""
    run_thread = _run_thread(thread_id)
    params: dict[str, object] = {"thread": thread_id}
    template = UNGUARDED_DELETE
    if run_thread is not None:
        template = GUARDED_DELETE
        params |= {"run_thread": run_thread, "statuses": list(SWEPT_STATUSES)}
    deleted = 0
    for table in CHECKPOINT_TABLES:
        statement = sql.SQL(template).format(table=sql.Identifier(table))
        deleted += conn.execute(statement, params).rowcount
    return deleted


def _new_start() -> uuid.UUID:
    """The random start of one pass of the listing (a test replaces this)."""
    return uuid.uuid4()


def leftover_threads(
    conn: psycopg.Connection, *, limit: int, start: uuid.UUID | None = None
) -> list[str]:
    """Up to ``limit`` threads that hold checkpoint rows and have no run in
    ``Running`` or ``AwaitingApproval``: a finished run whose delete failed, or
    a thread with no run at all. The sweep's role reads ``thread_id`` only.

    The listing is a sample from where ``start`` falls in the order of the
    threads' IDs (a random uuid when none is given): the threads after it, then
    the ones before it. It reads at most ``CANDIDATES_PER_LIMIT * limit``
    threads of each table, through its index, so what a pass reads in index
    probes does not grow with the tables. The bound is in probes, not in dead
    index entries: after many deletes and before a vacuum a probe steps over
    them, and one pass read far more buffers (about 512k, then about 14k on the
    next pass). The sample is not uniform: a thread that follows a long gap in
    the ID space is drawn more often. A thread is listed only when fewer than
    ``CANDIDATES_PER_LIMIT * limit`` threads of its table lie between the start
    and it, and a thread of a live run uses up a candidate. A leftover that is
    preceded in ID order by at least that many threads of live runs is
    therefore reached only when the start falls within the last
    ``CANDIDATES_PER_LIMIT * limit`` threads of that run: some start reaches it
    (a thread whose ID is the text of a uuid, as the saver writes, is never out
    of reach), but a given pass need not, and how often one does depends on the
    live runs' threads before it. A thread that is no uuid text may sort after
    every random start (``orphan-1`` does) and is then reached only when fewer
    than ``CANDIDATES_PER_LIMIT * limit`` threads follow the start; that is
    acceptable because the runtime is the only writer of checkpoint threads and
    writes the text of a uuid. A pass may list fewer than ``limit`` when
    candidates belong to a live run; the next pass starts elsewhere.

    A ``limit`` of zero or below lists nothing and runs no statement (a
    negative ``LIMIT`` is a server error)."""
    if limit <= 0:
        return []
    params = {
        "statuses": list(SWEPT_STATUSES),
        "limit": limit,
        "walk": CANDIDATES_PER_LIMIT * limit,
        "start": str(start if start is not None else _new_start()),
    }
    rows = conn.execute(LEFTOVER_THREADS, params).fetchall()
    return [thread for (thread,) in rows]


def end_abandoned_run(
    conn: psycopg.Connection,
    run_id: uuid.UUID,
    *,
    service: str,
    unreferenced_agents: Collection[str] = (),
) -> bool:
    """Move an unfinished run, idle past ``RUNNING_LEASE_SECONDS``, to ``Failed``;
    write its ``run.failed`` event with the reason ``abandoned``; delete its
    thread's checkpoints. Returns ``False``, and changes nothing, when the run is
    not in that state any more (finished, or freshened by a resume).

    The event carries the run's reference (a claim's ID) unless the run's agent is
    one of ``unreferenced_agents``: then it carries none, and the run's own ID
    on the row is what ties it to the run. A claim's trail takes every event of
    the sweep's role that names the claim, and an agent that is not the claim's
    triage must not appear in it as the triage's end (S037).

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
            reference=None if agent in unreferenced_agents else reference,
        ),
    )
    delete_thread_checkpoints(conn, str(thread_id))
    return True
