"""0017 fails closed under a short statement timeout, and runs under the default (S068).

0017 rewrites ``audit.events`` twice (``CLUSTER`` and an ``ADD COLUMN`` with a
volatile default) and holds ACCESS EXCLUSIVE on it to the commit. Above about
five to six million rows a rewrite no longer fits in the runner's ten seconds a
statement: the runner cannot apply the file, the transaction rolls back and the
table is as it was. No database of this project is in that state (the README,
"What cannot follow the rule"), so the test shows the failure with the timeout
turned down instead of the table turned up.

The numbers, and why the outcome does not depend on the machine's speed. The
table holds ``PLANTED_ROWS`` rows and the connection's statement timeout is
``SHORT_TIMEOUT`` for the whole run of the file. The file builds an index over
the rows, rewrites the heap in that order and rewrites it again to add the
column: three passes over about twenty megabytes written to disk, which no
machine finishes in ten milliseconds, so the cancellation is certain and not a
race. The default arm gives the same file the runner's ten seconds, which is
many times what 0017 needs for this table (the header measured a rewrite of 1.5
million rows in under five seconds): it cannot fail for want of time on a slow
machine. Neither arm sleeps or reads a clock.
"""

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

NAME = "0017_audit_order.sql"
LAST_BEFORE = "0016"
PLANTED_ROWS = 200_000
# Rows are written in batches: the insert trigger stamps each row in plpgsql,
# and one statement of all of them would meet the runner's own ten seconds.
BATCH_ROWS = 50_000
SHORT_TIMEOUT = "10ms"
QUERY_CANCELED = "57014"
PLANT = (
    "INSERT INTO audit.events (service, event, outcome, tenant) "
    "SELECT 'test', 'planted-' || n, 'ok', 'development' "
    "FROM generate_series(1, %s) AS n"
)
WHAT_IS_THERE = """
SELECT
    (SELECT count(*) FROM audit.events),
    (SELECT count(*) FROM pg_attribute
     WHERE attrelid = 'audit.events'::regclass AND attname = 'seq'
        AND NOT attisdropped),
    (SELECT count(*) FROM pg_class WHERE oid = to_regclass('audit.events_seq')),
    (SELECT count(*) FROM pg_class
     WHERE oid = to_regclass('audit.events_order_tmp_idx')),
    (SELECT count(*) FROM public.meridian_migrations WHERE name = %s),
    (SELECT prosecdef FROM pg_proc WHERE oid = 'audit.stamp_event()'::regprocedure),
    (SELECT array_agg(tgname::text || tgenabled::text ORDER BY tgname)
     FROM pg_trigger
     WHERE tgrelid = 'audit.events'::regclass AND NOT tgisinternal),
    pg_relation_filenode('audit.events')
"""


def apply_through(
    db: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    last_number: str,
    conn: psycopg.Connection | None = None,
) -> list[str]:
    """Apply the migrations numbered up to ``last_number`` that are not yet
    applied, on ``conn`` or on a connection of the runner's own kind."""
    files = [(n, t) for n, t in migration_files() if n[:4] <= last_number]
    monkeypatch.setattr(runner, "migration_files", lambda: files)
    if conn is not None:
        return runner.apply_migrations(conn)
    with connect(db.dsn(OWNER), "test") as fresh:
        return runner.apply_migrations(fresh)


def what_is_there(db: DatabaseHandle) -> tuple:
    with connect(db.dsn(OWNER), "test") as conn:
        (row,) = conn.execute(WHAT_IS_THERE, (NAME,)).fetchall()
        return row


@pytest.fixture
def database_with_many_audit_rows(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> DatabaseHandle:
    """0001 to 0016 applied and ``PLANTED_ROWS`` audit rows of a synthetic
    tenant written, 0017 not applied."""
    apply_through(empty_database, monkeypatch, LAST_BEFORE)
    with connect(empty_database.dsn(OWNER), "test") as conn:
        for _ in range(PLANTED_ROWS // BATCH_ROWS):
            conn.execute(PLANT, (BATCH_ROWS,))
            conn.commit()
    return empty_database


def test_0017_is_cancelled_by_a_short_statement_timeout_and_leaves_the_table_as_it_was(
    database_with_many_audit_rows: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = database_with_many_audit_rows
    before = what_is_there(db)

    with connect(db.dsn(OWNER), "test-migration") as conn:
        conn.execute(f"SET statement_timeout = '{SHORT_TIMEOUT}'")
        conn.commit()
        with pytest.raises(psycopg.errors.QueryCanceled) as raised:
            apply_through(db, monkeypatch, "0017", conn)

    after = what_is_there(db)
    assert raised.value.sqlstate == QUERY_CANCELED
    # What the failed run left: the same rows, no column, no sequence, no
    # temporary index, no ledger row, the old trigger function, the same
    # triggers on the same table file (the rewrite was rolled back).
    assert before[:6] == (PLANTED_ROWS, 0, 0, 0, 0, False)
    assert before[6]
    assert after == before


def test_0017_applies_to_the_same_table_under_the_default_statement_timeout(
    database_with_many_audit_rows: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = database_with_many_audit_rows
    before = what_is_there(db)

    applied = apply_through(db, monkeypatch, "0017")

    after = what_is_there(db)
    assert applied == [NAME]
    assert after[:6] == (PLANTED_ROWS, 1, 1, 0, 1, True)
    assert after[6] == before[6]
    assert after[7] != before[7]
