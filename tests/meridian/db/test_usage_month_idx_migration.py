"""0029: an index on ``gateway.usage (month)`` for the ledger's expiry in batches
(S068, T-25).

The batch function of 0030 removes the usage rows of the months before a cutoff,
oldest month first, and asks whether any is left; the table had no index on
``month``, so every call scanned it whole. The index is on ``month`` alone: a
second key would buy an order nothing needs and cost every model call an insert
into a random page of the index. The file holds the index and nothing else. The
migration is found by the end of its name, never by its number.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from psycopg.conninfo import make_conninfo
from sweepmigrationsupport import run

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

SUFFIX = "_usage_month_idx.sql"
INDEX = "usage_month_idx"
INDEX_COLUMNS = (
    "SELECT a.attname FROM pg_index AS i "
    "JOIN pg_class AS c ON c.oid = i.indexrelid "
    "CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, pos) "
    "JOIN pg_attribute AS a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
    "WHERE c.relname = %s ORDER BY k.pos"
)


def migration_index() -> int:
    (index,) = [
        position
        for position, (name, _) in enumerate(migration_files())
        if name.endswith(SUFFIX)
    ]
    return index


def migration_name() -> str:
    return migration_files()[migration_index()][0]


def migration_text() -> str:
    return dict(migration_files())[migration_name()]


def apply_everything_before(
    db: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apply the files before this one as the owner."""
    before = migration_files()[: migration_index()]
    monkeypatch.setattr(runner, "migration_files", lambda: before)
    with connect(db.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)


@contextmanager
def explain_with_scans_off(db: DatabaseHandle) -> Iterator[psycopg.Connection]:
    """A session of the owner whose planner may not pick a sequential scan, so
    that a table of a few rows shows which indexes could serve a query."""
    with connect(db.dsn(OWNER), "test") as conn:
        conn.execute("SET enable_seqscan = off")
        conn.execute("SET enable_bitmapscan = off")
        yield conn


def test_the_migration_is_recorded(migrated_database: DatabaseHandle) -> None:
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert (migration_name(),) in recorded


def test_the_index_is_a_plain_one_on_month_alone(
    migrated_database: DatabaseHandle,
) -> None:
    columns = run(migrated_database, OWNER, INDEX_COLUMNS, (INDEX,))
    partial_or_unique = run(
        migrated_database,
        OWNER,
        "SELECT i.indpred IS NOT NULL OR i.indisunique FROM pg_index AS i "
        "JOIN pg_class AS c ON c.oid = i.indexrelid WHERE c.relname = %s",
        (INDEX,),
    )

    assert columns == [("month",)]
    assert partial_or_unique == [(False,)]


def test_the_index_holds_the_order_the_batch_asks_for_so_no_sort_is_needed(
    migrated_database: DatabaseHandle,
) -> None:
    with explain_with_scans_off(migrated_database) as conn:
        plan = "\n".join(
            row[0]
            for row in conn.execute(
                "EXPLAIN SELECT attempt_id FROM gateway.usage "
                "WHERE month < current_date ORDER BY month LIMIT 100"
            )
        )

    assert INDEX in plan
    assert "Sort" not in plan


def test_the_file_is_only_the_index_and_its_guard() -> None:
    text = re.sub(r"--[^\n]*", "", migration_text())

    statements = [
        statement.strip().split(None, 2)[0:2]
        for statement in re.sub(r"\$\$.*?\$\$", "$$", text, flags=re.S).split(";")
        if statement.strip()
    ]

    assert statements == [
        ["SET", "LOCAL"],
        ["DO", "$$"],
        ["CREATE", "INDEX"],
    ]


def test_the_file_sets_its_lock_timeout_before_it_names_the_table() -> None:
    text = re.sub(r"--[^\n]*", "", migration_text())

    assert text.index("lock_timeout") < text.index("gateway.usage")


def test_the_file_takes_share_on_the_table_and_no_exclusive_lock(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)
    text = migration_text()

    with connect(empty_database.dsn(OWNER), "test") as conn:
        conn.execute(text)
        modes = {
            mode
            for (mode,) in conn.execute(
                "SELECT l.mode FROM pg_locks AS l "
                "WHERE l.locktype = 'relation' AND l.pid = pg_backend_pid() "
                "AND l.relation = 'gateway.usage'::regclass"
            )
        }
        conn.rollback()

    assert modes == {"ShareLock"}


def test_a_migration_run_by_a_role_that_does_not_own_the_schema_is_refused(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)

    with connect(empty_database.dsn("model_gateway"), "test") as conn:
        with pytest.raises(
            psycopg.Error, match="must be run by the owner of the schema gateway"
        ):
            conn.execute(migration_text())
        conn.rollback()

    assert run(empty_database, OWNER, f"SELECT to_regclass('gateway.{INDEX}')") == [
        (None,)
    ]


def test_a_superuser_is_refused_too(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)
    admin_dsn = make_conninfo(empty_database.admin_dsn, dbname=empty_database.name)

    with psycopg.connect(admin_dsn) as conn:
        with pytest.raises(
            psycopg.Error, match="must be run by the owner of the schema gateway"
        ):
            conn.execute(migration_text())
        conn.rollback()
