"""The migration runner against a real PostgreSQL."""

import hashlib
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import (
    MigrationError,
    apply_migrations,
    migration_files,
)

SCHEMAS = ("audit", "claims", "gateway", "knowledge", "policy", "runtime")


def test_migration_files_are_numbered_and_sorted() -> None:
    names = [name for name, _ in migration_files()]

    assert names[0] == "0001_schemas.sql"
    assert names == sorted(names)
    assert all(re.fullmatch(r"\d{4}_[a-z0-9_]+\.sql", name) for name in names)


def test_migrations_apply_in_name_order_and_are_recorded(
    empty_database: DatabaseHandle,
) -> None:
    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = apply_migrations(conn)
        recorded = conn.execute(
            "SELECT name, sha256 FROM public.meridian_migrations ORDER BY name"
        ).fetchall()
        schemas = {
            row[0]
            for row in conn.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)",
                (list(SCHEMAS),),
            )
        }

    expected = [name for name, _ in migration_files()]
    assert applied == expected
    assert [name for name, _ in recorded] == expected
    for (name, text), (_, sha256) in zip(migration_files(), recorded, strict=True):
        assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest(), name
    assert schemas == set(SCHEMAS)


def test_a_second_run_applies_nothing(empty_database: DatabaseHandle) -> None:
    with connect(empty_database.dsn(OWNER), "test") as conn:
        apply_migrations(conn)

        second = apply_migrations(conn)

    assert second == []


def test_a_changed_checksum_is_refused(empty_database: DatabaseHandle) -> None:
    with connect(empty_database.dsn(OWNER), "test") as conn:
        apply_migrations(conn)
        conn.execute("UPDATE public.meridian_migrations SET sha256 = repeat('0', 64)")
        conn.commit()

        with pytest.raises(MigrationError, match=r"0001_schemas\.sql.*checksum"):
            apply_migrations(conn)


def test_a_missing_service_role_fails_clearly_and_leaves_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    name, text = migration_files()[0]  # 0001: the one that checks the roles
    guard = "ARRAY['claims_api'"
    assert guard in text
    broken = text.replace(guard, "ARRAY['role_that_does_not_exist'")
    monkeypatch.setattr(runner, "migration_files", lambda: [(name, broken)])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(
            psycopg.Error,
            match="required role role_that_does_not_exist does not exist",
        ):
            apply_migrations(conn)

        leftovers = conn.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname = ANY(%s)",
            (list(SCHEMAS),),
        ).fetchone()
        recorded = conn.execute("SELECT count(*) FROM public.meridian_migrations")

        assert leftovers == (0,)
        assert recorded.fetchone() == (0,)


def test_a_connection_with_an_open_transaction_is_refused(
    empty_database: DatabaseHandle,
) -> None:
    # Inside a transaction, conn.transaction() would only open a savepoint and
    # the advisory lock would outlive nothing it was meant to protect.
    with connect(empty_database.dsn(OWNER), "test") as conn:
        conn.execute("SELECT 1")

        with pytest.raises(MigrationError, match="transaction"):
            apply_migrations(conn)


def test_concurrent_runners_on_an_empty_database_apply_each_file_once(
    empty_database: DatabaseHandle,
) -> None:
    runners = 8
    start = threading.Barrier(runners)

    def migrate() -> list[str]:
        with connect(empty_database.dsn(OWNER), "test-concurrent") as conn:
            start.wait(timeout=30)
            return apply_migrations(conn)

    with ThreadPoolExecutor(max_workers=runners) as pool:
        results = [f.result() for f in [pool.submit(migrate) for _ in range(runners)]]

    expected = [name for name, _ in migration_files()]
    assert sorted(name for applied in results for name in applied) == expected
    # Not "one runner applied them all": the lock is taken per file, so a
    # second runner may win it for a later file. That assertion held only
    # while one thread kept winning, and failed under load (S054).
    with connect(empty_database.dsn(OWNER), "test-concurrent") as conn:
        recorded = conn.execute(
            "SELECT name FROM public.meridian_migrations ORDER BY name"
        ).fetchall()
    assert [name for (name,) in recorded] == expected


def test_a_failing_migration_is_rolled_back_whole(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [
        ("0001_ok.sql", "CREATE SCHEMA kept;"),
        ("0002_bad.sql", "CREATE SCHEMA half; SELECT 1 / 0;"),
    ]
    monkeypatch.setattr(runner, "migration_files", lambda: files)
    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(psycopg.errors.DivisionByZero):
            apply_migrations(conn)

        schemas = {
            row[0]
            for row in conn.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname IN ('kept', 'half')"
            )
        }
        recorded = conn.execute(
            "SELECT name FROM public.meridian_migrations ORDER BY name"
        ).fetchall()

    assert schemas == {"kept"}  # the first file committed on its own
    assert recorded == [("0001_ok.sql",)]
