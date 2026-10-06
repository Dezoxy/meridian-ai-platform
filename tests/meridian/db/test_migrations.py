"""The migration runner against a real PostgreSQL."""

import hashlib
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

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
# "0017_b.sql" in the fullwidth digits U+FF10 to U+FF19, which ``\d`` matches.
FULLWIDTH_0017 = "".join(chr(0xFF10 + int(digit)) for digit in "0017") + "_b.sql"


def test_migration_files_are_numbered_and_sorted() -> None:
    names = [name for name, _ in migration_files()]

    assert names[0] == "0001_schemas.sql"
    assert names == sorted(names)
    assert all(re.fullmatch(r"[0-9]{4}_[a-z0-9_]+\.sql", name) for name in names)
    numbers = [name[:4] for name in names]
    assert len(numbers) == len(set(numbers)), "two migrations share a number"


def _package_with(
    monkeypatch: pytest.MonkeyPatch, directory: Path, names: list[str]
) -> None:
    """Make the runner read ``directory``, holding an empty file per name, as
    the migrations package."""
    for name in names:
        (directory / name).write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(
        runner, "resources", SimpleNamespace(files=lambda package: directory)
    )


@pytest.mark.parametrize(
    "name",
    [
        "0019_Add_Col.sql",  # an upper-case letter
        "0019-add.sql",  # a hyphen where the underscore goes
        "19_x.sql",  # too few digits
        "00170_x.sql",  # too many digits
        "0019_.sql",  # no words after the number
        "0019_add.SQL",  # an upper-case extension
        FULLWIDTH_0017,  # fullwidth digits, which \d matches
    ],
)
def test_a_sql_file_whose_name_does_not_match_is_refused_naming_it(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _package_with(monkeypatch, tmp_path, ["0001_a.sql", name])

    with pytest.raises(MigrationError) as raised:
        migration_files()

    assert name in str(raised.value)


def test_a_file_that_is_not_sql_is_not_a_migration_and_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _package_with(monkeypatch, tmp_path, ["0001_a.sql", "README.md", "notes.txt"])

    assert [name for name, _ in migration_files()] == ["0001_a.sql"]


def test_a_refused_name_is_refused_before_anything_is_applied(
    empty_database: DatabaseHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _package_with(monkeypatch, tmp_path, ["0001_a.sql", "0002-b.sql"])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(MigrationError, match=r"0002-b\.sql"):
            apply_migrations(conn)

        ledger = conn.execute(
            "SELECT to_regclass('public.meridian_migrations')"
        ).fetchone()

    assert ledger == (None,)


def test_every_sql_file_in_the_package_has_a_name_the_runner_accepts() -> None:
    # The refusal is read on the directory itself, not through the runner, so a
    # name the runner skipped would show here.
    entries = [
        entry.name
        for entry in resources.files(runner.__package__).iterdir()
        if entry.name.lower().endswith(".sql")
    ]

    assert entries
    assert sorted(entries) == [name for name, _ in migration_files()]


def test_two_files_with_one_number_are_refused_naming_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = [("0017_a.sql", "SELECT 1;"), ("0017_b.sql", "SELECT 2;")]
    monkeypatch.setattr(runner, "_packaged_files", lambda: files)

    with pytest.raises(MigrationError) as raised:
        migration_files()

    message = str(raised.value)
    assert "0017" in message
    assert "0017_a.sql" in message
    assert "0017_b.sql" in message


def test_a_gap_in_the_numbers_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    files = [("0001_a.sql", "SELECT 1;"), ("0003_b.sql", "SELECT 2;")]
    monkeypatch.setattr(runner, "_packaged_files", lambda: files)

    assert migration_files() == files


def test_a_refused_tree_applies_nothing_and_creates_no_ledger(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [
        ("0017_a.sql", "CREATE SCHEMA first_of_two;"),
        ("0017_b.sql", "CREATE SCHEMA second_of_two;"),
    ]
    monkeypatch.setattr(runner, "_packaged_files", lambda: files)
    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(MigrationError, match="0017"):
            apply_migrations(conn)

        ledger = conn.execute(
            "SELECT to_regclass('public.meridian_migrations')"
        ).fetchone()
        schemas = conn.execute(
            "SELECT count(*) FROM pg_namespace "
            "WHERE nspname IN ('first_of_two', 'second_of_two')"
        ).fetchone()

    assert ledger == (None,)
    assert schemas == (0,)


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
