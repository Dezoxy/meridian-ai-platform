"""Apply the numbered SQL files once each, each in its own transaction.

Applied files are recorded with their SHA-256 in ``public.meridian_migrations``.
A file that changed after it was applied is refused: a migration is history,
so a change goes in a new file.
"""

import hashlib
import re
from importlib import resources

import psycopg
from psycopg.pq import TransactionStatus

MIGRATION_NAME = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")
# Arbitrary constant: serialises concurrent runners on the same database.
LOCK_KEY = 7_009_001

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS public.meridian_migrations (
    name text PRIMARY KEY,
    sha256 text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(Exception):
    """An applied migration no longer matches the file in the package, or the
    connection was not in a state the runner can rely on."""


def migration_files() -> list[tuple[str, str]]:
    """The packaged ``(name, SQL text)`` pairs, in name order."""
    found = [
        (entry.name, entry.read_text(encoding="utf-8"))
        for entry in resources.files(__package__).iterdir()
        if MIGRATION_NAME.fullmatch(entry.name)
    ]
    return sorted(found)


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply every migration not yet applied; return the names applied.

    Raises ``MigrationError`` when an applied file's checksum changed or the
    connection has a transaction open (``conn.transaction()`` would then open
    a savepoint, and the advisory lock would not serialise the runners).
    """
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise MigrationError("the connection has an open transaction; pass an idle one")
    with conn.transaction():
        # The lock comes first: two runners on an empty database would
        # otherwise race in CREATE TABLE IF NOT EXISTS and one would fail.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        conn.execute(CREATE_TABLE)
    applied: list[str] = []
    for name, text in migration_files():
        checksum = _checksum(text)
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            row = conn.execute(
                "SELECT sha256 FROM public.meridian_migrations WHERE name = %s",
                (name,),
            ).fetchone()
            if row is not None:
                if row[0] != checksum:
                    raise MigrationError(
                        f"{name} was applied with a different checksum; an applied "
                        "migration must not change, add a new file instead"
                    )
                continue
            # No parameters, so the file may hold several statements.
            conn.execute(text)
            conn.execute(
                "INSERT INTO public.meridian_migrations (name, sha256) VALUES (%s, %s)",
                (name, checksum),
            )
            applied.append(name)
    return applied
