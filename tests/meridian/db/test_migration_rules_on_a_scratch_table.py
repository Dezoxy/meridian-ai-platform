"""Why the migration rules hold, on a scratch table (S065).

The rules are in ``src/meridian/platform/migrations/README.md`` and are checked
on the packaged files by ``test_migration_rules.py`` (text only). This file shows
by construction, against PostgreSQL, what a change locks and whether it rewrites
the table: which lock the file's ``ALTER`` keeps through a backfill, which lock a
backfill, a constraint added ``NOT VALID`` and its validation take (read from
``pg_locks``), and which changes rewrite the table (its file on disk, the
``relfilenode``, is a new one).

Every wait below is the server's: a second connection that asks for a lock the
first holds is refused after 200 ms. No test sleeps.
"""

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle

from meridian.platform.common.db import connect

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '200ms'"
CREATE_SCRATCH = "CREATE TABLE public.rule_scratch (id integer PRIMARY KEY, label text)"
FILL_SCRATCH = (
    "INSERT INTO public.rule_scratch (id, label) "
    "SELECT i, 'row' FROM generate_series(1, 4) AS i"
)
ADD_FLAG = "ALTER TABLE public.rule_scratch ADD COLUMN flag integer NOT NULL DEFAULT 0"
BACKFILL_HALF = "UPDATE public.rule_scratch SET flag = 1 WHERE id <= 2"
ADD_CHECK_NOT_VALID = (
    "ALTER TABLE public.rule_scratch "
    "ADD CONSTRAINT rule_scratch_flag_check CHECK (flag >= 0) NOT VALID"
)
VALIDATE_CHECK = (
    "ALTER TABLE public.rule_scratch VALIDATE CONSTRAINT rule_scratch_flag_check"
)
CREATE_PARENT = "CREATE TABLE public.rule_parent (id integer PRIMARY KEY)"
ADD_REFERENCE_NOT_VALID = (
    "ALTER TABLE public.rule_scratch ADD CONSTRAINT rule_scratch_id_fkey "
    "FOREIGN KEY (id) REFERENCES public.rule_parent (id) NOT VALID"
)
READ_ALL = "SELECT id, flag FROM public.rule_scratch ORDER BY id"
# Each change to the scratch table, and whether PostgreSQL rewrites the table
# for it: the table's file on disk (its relfilenode) is a new one when it does.
CREATE_SEQUENCE = "CREATE SEQUENCE public.rule_seq"
FILENODE = "SELECT pg_relation_filenode('public.rule_scratch')"
ALTER_SCRATCH = "ALTER TABLE public.rule_scratch "
REWRITES = {
    "nullable column": (ALTER_SCRATCH + "ADD COLUMN extra integer", False),
    "not null with a constant default": (
        ALTER_SCRATCH + "ADD COLUMN extra integer NOT NULL DEFAULT 0",
        False,
    ),
    "default now(), which is stable": (
        ALTER_SCRATCH + "ADD COLUMN extra timestamptz NOT NULL DEFAULT now()",
        False,
    ),
    "default nextval(), which is volatile": (
        ALTER_SCRATCH
        + "ADD COLUMN extra bigint NOT NULL DEFAULT nextval('public.rule_seq')",
        True,
    ),
    "default gen_random_uuid(), which is volatile": (
        ALTER_SCRATCH + "ADD COLUMN extra uuid NOT NULL DEFAULT gen_random_uuid()",
        True,
    ),
    "generated stored column": (
        ALTER_SCRATCH + "ADD COLUMN extra integer GENERATED ALWAYS AS (id * 2) STORED",
        True,
    ),
    "identity column": (
        ALTER_SCRATCH + "ADD COLUMN extra integer GENERATED ALWAYS AS IDENTITY",
        True,
    ),
    "type change": (ALTER_SCRATCH + "ALTER COLUMN id TYPE bigint", True),
}


def scratch_table(db: DatabaseHandle, *, with_flag: bool) -> None:
    """A table of four rows, with the column already committed (the first file
    of the rule) or without it."""
    with connect(db.dsn(OWNER), "test-set-up") as conn:
        conn.execute(CREATE_SCRATCH)
        conn.execute(FILL_SCRATCH)
        if with_flag:
            conn.execute(ADD_FLAG)
        conn.commit()


def held_modes(
    conn: psycopg.Connection, table: str = "public.rule_scratch"
) -> set[str]:
    """The lock modes this connection's transaction holds on ``table``."""
    rows = conn.execute(
        "SELECT mode FROM pg_locks WHERE pid = pg_backend_pid() "
        "AND locktype = 'relation' AND relation = %s::regclass",
        (table,),
    ).fetchall()
    return {mode for (mode,) in rows}


def test_the_lock_of_an_add_column_is_still_held_after_the_backfill_that_follows(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=False)

    with (
        connect(empty_database.dsn(OWNER), "test-migration") as migration,
        connect(empty_database.dsn(OWNER), "test-reader") as reader,
    ):
        migration.execute(ADD_FLAG)
        migration.execute(BACKFILL_HALF)
        reader.execute(LOCK_TIMEOUT)

        held = held_modes(migration)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            reader.execute(READ_ALL)

        migration.rollback()

    # The UPDATE's own lock is there beside the ALTER's, which it did not end.
    assert held == {"AccessExclusiveLock", "RowExclusiveLock"}


@pytest.mark.parametrize("change", REWRITES)
def test_only_some_changes_rewrite_the_table(
    empty_database: DatabaseHandle, change: str
) -> None:
    statement, rewrites = REWRITES[change]
    scratch_table(empty_database, with_flag=False)

    with connect(empty_database.dsn(OWNER), "test-migration") as migration:
        migration.execute(CREATE_SEQUENCE)
        ((before,),) = migration.execute(FILENODE).fetchall()
        migration.execute(statement)
        ((after,),) = migration.execute(FILENODE).fetchall()
        migration.rollback()

    assert (after != before) is rewrites


def test_a_backfill_update_holds_row_exclusive_on_the_table_and_nothing_stronger(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)

    with connect(empty_database.dsn(OWNER), "test-backfill") as backfill:
        backfill.execute(BACKFILL_HALF)

        held = held_modes(backfill)
        backfill.rollback()

    assert held == {"RowExclusiveLock"}


def test_a_backfill_after_the_column_is_committed_blocks_no_reader_and_no_other_row(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)

    with (
        connect(empty_database.dsn(OWNER), "test-backfill") as backfill,
        connect(empty_database.dsn(OWNER), "test-other") as other,
    ):
        backfill.execute(BACKFILL_HALF)
        other.execute(LOCK_TIMEOUT)

        seen = other.execute(READ_ALL).fetchall()
        written = other.execute(
            "UPDATE public.rule_scratch SET label = 'new' WHERE id = 3"
        )
        backfill.rollback()
        other.rollback()

    assert seen == [(1, 0), (2, 0), (3, 0), (4, 0)]
    assert written.rowcount == 1


def test_a_writer_of_a_row_the_backfill_has_changed_waits_for_that_row_only(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)

    with (
        connect(empty_database.dsn(OWNER), "test-backfill") as backfill,
        connect(empty_database.dsn(OWNER), "test-other") as other,
    ):
        backfill.execute(BACKFILL_HALF)
        other.execute(LOCK_TIMEOUT)

        with pytest.raises(psycopg.errors.LockNotAvailable):
            other.execute("UPDATE public.rule_scratch SET label = 'new' WHERE id = 1")

        backfill.rollback()


def test_the_lock_of_a_check_added_not_valid_is_still_held_after_it_is_validated(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)

    with (
        connect(empty_database.dsn(OWNER), "test-migration") as migration,
        connect(empty_database.dsn(OWNER), "test-reader") as reader,
    ):
        migration.execute(ADD_CHECK_NOT_VALID)
        migration.execute(VALIDATE_CHECK)
        reader.execute(LOCK_TIMEOUT)

        held = held_modes(migration)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            reader.execute(READ_ALL)

        migration.rollback()

    # The validation's own lock is there beside the ALTER's, which it did not end.
    assert held == {"AccessExclusiveLock", "ShareUpdateExclusiveLock"}


def test_a_check_added_not_valid_takes_access_exclusive_for_the_catalog_change(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)

    with connect(empty_database.dsn(OWNER), "test-not-valid-file") as not_valid_file:
        not_valid_file.execute(ADD_CHECK_NOT_VALID)

        held = held_modes(not_valid_file)
        not_valid_file.rollback()

    assert held == {"AccessExclusiveLock"}


def test_a_foreign_key_added_not_valid_takes_share_row_exclusive_on_both_tables(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)

    with connect(empty_database.dsn(OWNER), "test-not-valid-file") as not_valid_file:
        not_valid_file.execute(CREATE_PARENT)
        not_valid_file.commit()
        not_valid_file.execute(ADD_REFERENCE_NOT_VALID)

        child = held_modes(not_valid_file)
        parent = held_modes(not_valid_file, "public.rule_parent")
        not_valid_file.rollback()

    # The strongest lock each holds is SHARE ROW EXCLUSIVE: it blocks writers
    # and other DDL, not readers, and is not an ACCESS EXCLUSIVE lock.
    stronger = {"ExclusiveLock", "AccessExclusiveLock"}
    assert "ShareRowExclusiveLock" in child
    assert "ShareRowExclusiveLock" in parent
    assert not (child | parent) & stronger


def test_a_check_added_not_valid_refuses_a_violating_write_at_once(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)
    with connect(empty_database.dsn(OWNER), "test-not-valid-file") as not_valid_file:
        not_valid_file.execute(ADD_CHECK_NOT_VALID)
        not_valid_file.commit()

    with connect(empty_database.dsn(OWNER), "test-writer") as writer:
        with pytest.raises(psycopg.errors.CheckViolation):
            writer.execute("INSERT INTO public.rule_scratch (id, flag) VALUES (9, -1)")
        writer.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            writer.execute("UPDATE public.rule_scratch SET flag = -1 WHERE id = 1")
        writer.rollback()


def test_validating_holds_a_lock_that_blocks_other_ddl_and_vacuum_only(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)
    with connect(empty_database.dsn(OWNER), "test-not-valid-file") as not_valid_file:
        not_valid_file.execute(ADD_CHECK_NOT_VALID)
        not_valid_file.commit()

    with (
        connect(empty_database.dsn(OWNER), "test-validate") as validate,
        connect(empty_database.dsn(OWNER), "test-ddl") as ddl,
        connect(empty_database.dsn(OWNER), "test-vacuum") as vacuum,
    ):
        validate.execute(VALIDATE_CHECK)
        held = held_modes(validate)
        ddl.execute(LOCK_TIMEOUT)
        vacuum.autocommit = True
        vacuum.execute("SET lock_timeout = '200ms'")

        with pytest.raises(psycopg.errors.LockNotAvailable):
            ddl.execute("ALTER TABLE public.rule_scratch ADD COLUMN extra integer")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            vacuum.execute("VACUUM public.rule_scratch")

        validate.rollback()
        ddl.rollback()

    assert held == {"ShareUpdateExclusiveLock"}


def test_validating_a_constraint_added_not_valid_blocks_no_reader_and_no_writer(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)
    with connect(empty_database.dsn(OWNER), "test-not-valid-file") as not_valid_file:
        not_valid_file.execute(ADD_CHECK_NOT_VALID)
        not_valid_file.commit()

    with (
        connect(empty_database.dsn(OWNER), "test-validate") as validate,
        connect(empty_database.dsn(OWNER), "test-other") as other,
    ):
        validate.execute(VALIDATE_CHECK)
        other.execute(LOCK_TIMEOUT)

        seen = other.execute(READ_ALL).fetchall()
        written = other.execute(
            "UPDATE public.rule_scratch SET label = 'new' WHERE id = 3"
        )
        validate.rollback()
        other.rollback()

    assert seen == [(1, 0), (2, 0), (3, 0), (4, 0)]
    assert written.rowcount == 1
