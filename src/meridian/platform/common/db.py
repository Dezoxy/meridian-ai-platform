"""PostgreSQL connections shared by the services."""

import psycopg

# Every service reads its own role's DSN from this variable. The migration
# command uses its own (MERIDIAN_MIGRATIONS_DATABASE_URL), for the owner role.
DATABASE_URL_ENV = "MERIDIAN_DATABASE_URL"


CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 10_000


def connect(dsn: str, application_name: str) -> psycopg.Connection:
    """Open a connection that commits only when the caller says so.

    A dead server fails the connect after 5 seconds and no statement runs
    longer than 10 (this includes the migration runner's lock wait).
    """
    return psycopg.connect(
        dsn,
        autocommit=False,
        application_name=application_name,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
    )
