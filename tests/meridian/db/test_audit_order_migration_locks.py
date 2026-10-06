"""0017 and the traffic it meets: no deadlock, and a busy table fails fast (S065).

0017 rewrites ``audit.events``. It takes ACCESS EXCLUSIVE on the table with its
first statements and holds it to the commit, so that the two ways the review
found it deadlocking with ordinary traffic are gone:

- a reader of ``audit.claim_trail`` holds ACCESS SHARE on the view and waits for
  the table, while a file that then replaced the view would wait for the reader:
  the view now has a file of its own (0019);
- a transaction that read the table and then inserts waits behind the file's
  SHARE lock (``CREATE INDEX``) while the file's ``CLUSTER`` waits for the
  reader's ACCESS SHARE: the file now asks for ACCESS EXCLUSIVE at the start.

A file that cannot get its lock gives up after its ``lock_timeout`` and leaves
nothing behind. Each wait below is on the server: a second connection, a
``lock_timeout`` or a statement timeout on every connection that could wait, and
a poll of ``pg_locks`` (a bounded count of queries, not a pause) to know that a
connection is waiting before the test goes on. A regression ends in an error,
not a hang.
"""

import queue
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from psycopg import sql

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

NAME = "0017_audit_order.sql"
VIEW_NAME = "0019_audit_trail_seq.sql"
LAST_BEFORE = "0016"
CLAIM_ID = "CLM-0171"
TENANT = "development"
# How the tests wait for a connection to be waiting: this many queries of
# pg_locks, one after the other, and then the test fails.
MAX_POLLS = 200_000
RESULT_SECONDS = 30
INSERT_CLAIM = (
    "INSERT INTO claims.claims (claim_id, tenant, submission) VALUES (%s, %s, '{}')"
)
INSERT_TRAIL_EVENT = (
    "INSERT INTO audit.events (service, event, outcome, tenant, reference) "
    "VALUES ('claims-api', 'claim.received', 'ok', %s, %s)"
)
READ_TRAIL = "SELECT claim_id FROM audit.claim_trail"
LATE_ROW = (
    "INSERT INTO audit.events (service, event, outcome) VALUES ('test', 'late', 'ok')"
)
WHAT_IS_LEFT = """
SELECT
    (SELECT count(*) FROM pg_attribute
     WHERE attrelid = 'audit.events'::regclass AND attname = 'seq'
        AND NOT attisdropped),
    (SELECT count(*) FROM pg_class WHERE oid = to_regclass('audit.events_seq')),
    (SELECT count(*) FROM pg_class
     WHERE oid = to_regclass('audit.events_order_tmp_idx')),
    (SELECT count(*) FROM public.meridian_migrations WHERE name = %s),
    (SELECT prosecdef FROM pg_proc WHERE oid = 'audit.stamp_event()'::regprocedure),
    (SELECT count(*) FROM pg_index WHERE indrelid = 'audit.events'::regclass),
    pg_relation_filenode('audit.events')
"""


def run(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> list:
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def apply_through(
    db: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, last_number: str
) -> list[str]:
    """Apply the migrations numbered up to ``last_number`` that are not yet
    applied (the runner sees only that prefix of the packaged files)."""
    files = [(n, t) for n, t in migration_files() if n[:4] <= last_number]
    monkeypatch.setattr(runner, "migration_files", lambda: files)
    with connect(db.dsn(OWNER), "test") as conn:
        return runner.apply_migrations(conn)


def text_of(name: str) -> str:
    return dict(migration_files())[name]


@pytest.fixture
def database_before_0017(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> DatabaseHandle:
    """0001 to 0016 applied and one claim with an event the trail shows, in a
    database whose sessions find a deadlock after 100 ms instead of a second,
    so that a regression fails quickly."""
    with psycopg.connect(empty_database.admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("ALTER DATABASE {} SET deadlock_timeout = '100ms'").format(
                sql.Identifier(empty_database.name)
            )
        )
    apply_through(empty_database, monkeypatch, LAST_BEFORE)
    run(empty_database, "claims_api", INSERT_CLAIM, (CLAIM_ID, TENANT))
    run(empty_database, "claims_api", INSERT_TRAIL_EVENT, (TENANT, CLAIM_ID))
    return empty_database


def wait_until_waiting(db: DatabaseHandle, pid: int) -> None:
    """Return when the backend ``pid`` waits for a lock; fail if it never does.

    The one synchronisation the tests need: the other side must be blocked
    before the test acts, or the interleaving under test might not happen.
    """
    with psycopg.connect(db.admin_dsn, autocommit=True) as admin:
        for _ in range(MAX_POLLS):
            waiting = admin.execute(
                "SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted", (pid,)
            ).fetchone()
            if waiting:
                return
    pytest.fail(f"backend {pid} never waited for a lock in {MAX_POLLS} polls")


def in_thread(
    pool: ThreadPoolExecutor, work: Callable[[Callable[[int], None]], Any]
) -> tuple[Any, int]:
    """Start ``work`` in the pool; return its future and the backend PID of the
    connection it announced (``work`` calls its argument with the PID before it
    runs the statement that may wait)."""
    pids: queue.Queue[int] = queue.Queue()
    future = pool.submit(work, pids.put)
    try:
        return future, pids.get(timeout=RESULT_SECONDS)
    except queue.Empty:
        if future.done():
            future.result()  # raises the work's own error
        pytest.fail("the work did not announce its connection")


# ── what the file takes, and when ───────────────────────────────────────────
def test_the_first_statements_of_0017_set_a_lock_timeout_and_lock_the_table() -> None:
    statements = [
        " ".join(statement.split())
        for statement in "\n".join(
            line for line in text_of(NAME).splitlines() if not line.startswith("--")
        ).split(";")
        if statement.strip()
    ]

    assert statements[0].startswith("SET LOCAL lock_timeout = '")
    assert statements[1] == "LOCK TABLE audit.events IN ACCESS EXCLUSIVE MODE"


def test_0017_holds_access_exclusive_on_the_audit_table_and_touches_no_other_table(
    database_before_0017: DatabaseHandle,
) -> None:
    held_query = (
        "SELECT c.oid::regclass::text, l.mode FROM pg_locks AS l "
        "JOIN pg_class AS c ON c.oid = l.relation "
        "WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation' "
        "AND c.relkind IN ('r', 'v') "
        "AND c.relnamespace IN ('audit'::regnamespace, 'claims'::regnamespace, "
        "'runtime'::regnamespace)"
    )

    with connect(database_before_0017.dsn(OWNER), "test-migration") as migration:
        migration.execute(text_of(NAME))
        held = migration.execute(held_query).fetchall()
        migration.rollback()

    tables = {table for table, _ in held}
    assert ("audit.events", "AccessExclusiveLock") in held
    # The trail view and the tables behind it are as 0017 found them: it takes
    # no lock on them, so a reader of the trail is held up by the audit table
    # alone.
    assert tables == {"audit.events"}


# ── deadlock 1: a reader of the trail, then the view's replacement ─────────
def test_a_reader_of_the_trail_blocked_behind_0017_does_not_deadlock_with_0019(
    database_before_0017: DatabaseHandle,
) -> None:
    db = database_before_0017

    def read_the_trail(announce: Callable[[int], None]) -> list:
        with connect(db.dsn("claims_api"), "test-reader") as reader:
            reader.execute("SET lock_timeout = '20s'")
            ((pid,),) = reader.execute("SELECT pg_backend_pid()").fetchall()
            announce(pid)
            rows = reader.execute(READ_TRAIL).fetchall()
            reader.commit()
            return rows

    with (
        connect(db.dsn(OWNER), "test-migration") as migration,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        migration.execute("LOCK TABLE audit.events IN ACCESS EXCLUSIVE MODE")
        reader, reader_pid = in_thread(pool, read_the_trail)
        wait_until_waiting(db, reader_pid)

        # The reader holds ACCESS SHARE on the view and waits for the table. A
        # file that replaced the view here would wait for the reader.
        migration.execute(text_of(NAME))
        migration.commit()
        seen = reader.result(timeout=RESULT_SECONDS)
        migration.execute(text_of(VIEW_NAME))
        migration.commit()

    assert seen == [(CLAIM_ID,)]
    columns = run(
        db,
        OWNER,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'audit' AND table_name = 'claim_trail' "
        "AND column_name = 'seq'",
    )
    assert columns == [("seq",)]


# ── deadlock 2: a transaction that read the table and then inserts ─────────
def test_a_transaction_that_read_the_audit_table_and_then_inserts_is_not_deadlocked(
    database_before_0017: DatabaseHandle,
) -> None:
    db = database_before_0017

    def apply_0017(announce: Callable[[int], None]) -> None:
        with connect(db.dsn(OWNER), "test-migration") as migration:
            ((pid,),) = migration.execute("SELECT pg_backend_pid()").fetchall()
            announce(pid)
            migration.execute(text_of(NAME))
            migration.commit()

    with (
        connect(db.dsn(OWNER), "test-reader") as reader,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        reader.execute("SELECT count(*) FROM audit.events").fetchall()
        migration, migration_pid = in_thread(pool, apply_0017)
        wait_until_waiting(db, migration_pid)

        # The migration waits for this transaction's ACCESS SHARE. This insert
        # queues behind the migration's request for the table, so PostgreSQL
        # lets the insert go first (it holds a lock the migration waits for);
        # the file's own lock can no longer be upgraded into a deadlock.
        reader.execute(LATE_ROW)
        reader.commit()
        migration.result(timeout=RESULT_SECONDS)

    ((seq,),) = run(db, OWNER, "SELECT seq FROM audit.events WHERE event = 'late'")
    assert seq is not None


# ── a busy table: the file gives up, leaves nothing, and is run again ──────
def test_0017_fails_on_its_lock_timeout_when_a_transaction_holds_the_table_open(
    database_before_0017: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = database_before_0017
    before = run(db, OWNER, WHAT_IS_LEFT, (NAME,))

    with connect(db.dsn(OWNER), "test-reader") as reader:
        reader.execute("SELECT count(*) FROM audit.events").fetchall()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            apply_through(db, monkeypatch, "0017")
        after_failure = run(db, OWNER, WHAT_IS_LEFT, (NAME,))
        reader.rollback()

    applied = apply_through(db, monkeypatch, "0017")

    # Nothing is left of the failed run: no column, no sequence, no index, no
    # ledger row, the old trigger function, the old file on disk.
    assert before[0][:5] == (0, 0, 0, 0, False)
    assert after_failure == before
    assert applied == [NAME]
    after_rerun = run(db, OWNER, WHAT_IS_LEFT, (NAME,))
    assert after_rerun[0][:5] == (1, 1, 0, 1, True)
    assert after_rerun[0][6] != before[0][6]
