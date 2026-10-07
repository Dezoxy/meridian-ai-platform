"""0031, first half: an idle transaction is ended, for the database (S068, T-25).

The file sets ``idle_in_transaction_session_timeout`` to 60 s as the database's
default, so a session that took a row lock and then went idle inside its
transaction is ended instead of holding the lock for good. A database-level
setting is not copied by ``CREATE DATABASE ... TEMPLATE``, and the suite's
databases are copies, so these tests read the setting on a database the files
ran on directly (one per module) and show, by reading the catalog, that a copy
does not carry it: the suite's green on a copy is no proof of the setting.

The setting is scoped to the throwaway database of this module, so nothing here
touches a role the other tests read. No test sleeps: the waiting session's own
blocked wait is the clock, bounded by a lock timeout so that a regression ends
in an error and not in a hang. The migration is found by the end of its name.
"""

import secrets
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle, drop_database
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sweepmigrationsupport import CLAIM_ID, INSERT_CLAIM, TENANT

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import apply_migrations, migration_files

SUFFIX = "_idle_timeout_pg_temp.sql"
SETTING = "idle_in_transaction_session_timeout"
# The value of the file, as SHOW prints it and as the catalog stores it.
SHOWN = "1min"
STORED = f"{SETTING}=60s"
# Short enough to see in a test, long enough that the gap between a session's
# BEGIN and its next statement (three workers share a machine) cannot pass it.
SHORT = "500ms"
# A session idle outside a transaction with a value far below any wait of the
# tests: it must still be there when the waiter has the row.
TINY = "100ms"
# The waiter gives up on the lock after this long: a regression is an error.
WAITER_LOCK_TIMEOUT = "5s"
WAITER_RESULT_SECONDS = 15
LOCK_THE_ROW = "SELECT claim_id FROM claims.claims WHERE claim_id = %s FOR UPDATE"
SETTING_ROWS = (
    "SELECT d.datname, s.setconfig FROM pg_db_role_setting AS s "
    "JOIN pg_database AS d ON d.oid = s.setdatabase "
    "WHERE s.setrole = 0 AND d.datname = ANY(%s) ORDER BY d.datname"
)
SERVICE_ROLES_SHOWN = ("claims_api", "model_gateway", "gateway_upkeep")


def migration_name() -> str:
    (name,) = [name for name, _ in migration_files() if name.endswith(SUFFIX)]
    return name


@pytest.fixture(scope="module")
def direct_database(
    db_admin_dsn: str, db_passwords: dict[str, str]
) -> Iterator[DatabaseHandle]:
    """A database the migrations ran on directly, not a copy of the template,
    with one claim to lock."""
    name = f"meridian_test_{secrets.token_hex(4)}"
    handle = DatabaseHandle(name=name, admin_dsn=db_admin_dsn, passwords=db_passwords)
    with psycopg.connect(db_admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(OWNER)
            )
        )
    try:
        with psycopg.connect(
            make_conninfo(db_admin_dsn, dbname=name), autocommit=True
        ) as in_database:
            in_database.execute("CREATE EXTENSION IF NOT EXISTS vector")
        with connect(handle.dsn(OWNER), "test") as conn:
            apply_migrations(conn)
        with connect(handle.dsn("claims_api"), "test") as conn:
            conn.execute(INSERT_CLAIM, (CLAIM_ID, TENANT))
            conn.commit()
        yield handle
    finally:
        drop_database(handle)


def show(db: DatabaseHandle, role: str) -> str:
    with connect(db.dsn(role), "test") as conn:
        ((value,),) = conn.execute(f"SHOW {SETTING}").fetchall()
        return value


@contextmanager
def forgotten_transaction(
    db: DatabaseHandle,
) -> Iterator[tuple[psycopg.Connection, list]]:
    """Session A takes the claim's row lock, with its own value shortened, and
    then does nothing; session B asks for the same row and waits for it. Yields
    A and what B got, once B has it (so A has been ended by then)."""
    owner = db.dsn(OWNER)
    forgetful = connect(owner, "test-forgetful")
    try:
        forgetful.execute(f"SET {SETTING} = '{SHORT}'")
        forgetful.commit()
        forgetful.execute(LOCK_THE_ROW, (CLAIM_ID,))

        def wait_for_the_row() -> list:
            with connect(owner, "test-waiter") as waiter:
                waiter.execute(f"SET lock_timeout = '{WAITER_LOCK_TIMEOUT}'")
                rows = waiter.execute(LOCK_THE_ROW, (CLAIM_ID,)).fetchall()
                waiter.rollback()
                return rows

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(wait_for_the_row)
            got = future.result(timeout=WAITER_RESULT_SECONDS)
        yield forgetful, got
    finally:
        forgetful.close()


def test_a_new_session_of_the_database_shows_the_setting_as_one_minute(
    direct_database: DatabaseHandle,
) -> None:
    shown = show(direct_database, OWNER)

    assert shown == SHOWN


@pytest.mark.parametrize("role", SERVICE_ROLES_SHOWN)
def test_a_session_of_every_role_gets_the_database_default(
    direct_database: DatabaseHandle, role: str
) -> None:
    shown = show(direct_database, role)

    assert shown == SHOWN


def test_the_default_is_a_database_setting_and_not_a_role_s(
    direct_database: DatabaseHandle,
) -> None:
    with psycopg.connect(direct_database.admin_dsn, autocommit=True) as admin:
        rows = admin.execute(SETTING_ROWS, ([direct_database.name],)).fetchall()

    assert rows == [(direct_database.name, [STORED])]


def test_a_session_can_change_its_own_value_so_the_setting_ends_the_forgotten_only(
    direct_database: DatabaseHandle,
) -> None:
    with connect(direct_database.dsn(OWNER), "test") as conn:
        conn.execute(f"SET {SETTING} = 0")
        ((value,),) = conn.execute(f"SHOW {SETTING}").fetchall()

    assert value == "0"


def test_a_session_that_holds_a_row_lock_and_goes_idle_is_ended(
    direct_database: DatabaseHandle,
) -> None:
    with forgotten_transaction(direct_database) as (forgetful, _):
        with pytest.raises(psycopg.errors.IdleInTransactionSessionTimeout):
            forgetful.execute("SELECT 1")

        ended = forgetful.broken or forgetful.closed

    assert ended


def test_the_session_that_waited_for_the_row_gets_it(
    direct_database: DatabaseHandle,
) -> None:
    with forgotten_transaction(direct_database) as (_, got):
        pass

    assert got == [(CLAIM_ID,)]


def test_a_session_idle_outside_a_transaction_is_not_ended(
    direct_database: DatabaseHandle,
) -> None:
    quiet = psycopg.connect(direct_database.dsn(OWNER), autocommit=True)
    try:
        quiet.execute(f"SET {SETTING} = '{TINY}'")
        quiet.execute("SELECT 1")

        # By the time the waiter has the row, the forgotten session's value of
        # half a second has run out: far longer than the quiet one's 100 ms.
        with forgotten_transaction(direct_database):
            still_there = quiet.execute("SELECT 1").fetchall()
    finally:
        quiet.close()

    assert still_there == [(1,)]


def test_a_copy_of_the_template_does_not_carry_the_setting(
    fresh_database: DatabaseHandle, db_template: str
) -> None:
    # The template was built by the migrations directly, so it has the setting;
    # CREATE DATABASE ... TEMPLATE copies files, not pg_db_role_setting, so the
    # copy every other test of the suite uses has none. A green test on a copy
    # says nothing about this file's setting.
    with psycopg.connect(fresh_database.admin_dsn, autocommit=True) as admin:
        rows = admin.execute(
            SETTING_ROWS, ([db_template, fresh_database.name],)
        ).fetchall()

    assert rows == [(db_template, [STORED])]


def test_the_migration_is_in_the_package_and_names_the_setting() -> None:
    text = dict(migration_files())[migration_name()]

    assert SETTING in text
