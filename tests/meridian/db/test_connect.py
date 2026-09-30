"""The connection every service opens carries its timeouts (item 4)."""

from dbsupport import OWNER, DatabaseHandle

from meridian.platform.common.db import (
    CONNECT_TIMEOUT_SECONDS,
    STATEMENT_TIMEOUT_MS,
    connect,
)


def test_the_connection_has_a_connect_timeout_and_a_statement_timeout(
    migrated_database: DatabaseHandle,
) -> None:
    with connect(migrated_database.dsn(OWNER), "test") as conn:
        shown = conn.execute("SHOW statement_timeout").fetchone()
        parameters = conn.info.get_parameters()

    assert shown == ("10s",)
    assert STATEMENT_TIMEOUT_MS == 10_000
    assert parameters["connect_timeout"] == str(CONNECT_TIMEOUT_SECONDS) == "5"
