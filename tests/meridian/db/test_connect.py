"""The connection every service opens carries its timeouts (item 4) and its
isolation level (S013)."""

import psycopg
from dbsupport import OWNER, DatabaseHandle
from psycopg import sql

from meridian.platform.common.db import (
    CONNECT_TIMEOUT_SECONDS,
    STATEMENT_TIMEOUT_MS,
    connect,
)

READ_COMMITTED = ("read committed",)


def test_the_connection_has_a_connect_timeout_and_a_statement_timeout(
    migrated_database: DatabaseHandle,
) -> None:
    with connect(migrated_database.dsn(OWNER), "test") as conn:
        shown = conn.execute("SHOW statement_timeout").fetchone()
        parameters = conn.info.get_parameters()

    assert shown == ("10s",)
    assert STATEMENT_TIMEOUT_MS == 10_000
    assert parameters["connect_timeout"] == str(CONNECT_TIMEOUT_SECONDS) == "5"


def test_the_connection_reads_committed(migrated_database: DatabaseHandle) -> None:
    with connect(migrated_database.dsn(OWNER), "test") as conn:
        shown = conn.execute("SHOW transaction_isolation").fetchone()

    assert shown == READ_COMMITTED


def test_the_connection_reads_committed_in_a_database_that_says_otherwise(
    fresh_database: DatabaseHandle,
) -> None:
    with psycopg.connect(fresh_database.admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL(
                "ALTER DATABASE {} SET default_transaction_isolation = 'serializable'"
            ).format(sql.Identifier(fresh_database.name))
        )
    dsn = fresh_database.dsn(OWNER)

    with psycopg.connect(dsn) as plain:
        assert plain.execute("SHOW transaction_isolation").fetchone() == (
            "serializable",
        )
    with connect(dsn, "test") as conn:
        isolation = conn.execute("SHOW transaction_isolation").fetchone()
        timeout = conn.execute("SHOW statement_timeout").fetchone()

    assert isolation == READ_COMMITTED
    assert timeout == ("10s",)
