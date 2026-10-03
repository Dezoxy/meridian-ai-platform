"""0008: the runtime's LangGraph checkpoint tables (S015)."""

import secrets
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from langgraph.checkpoint.postgres.base import MIGRATIONS
from psycopg import sql

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import migration_files
from meridian.runtime.checkpoints import open_saver

TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
# The library's own ledger: only its setup() uses it, so 0008 does not create it.
LIBRARY_LEDGER = "checkpoint_migrations"
# Since 0013 claims_sweep reads thread_id and DELETEs on the three tables.
OTHER_ROLES = tuple(
    role for role in SERVICE_ROLES if role not in ("agent_runtime", "claims_sweep")
)
INSUFFICIENT_PRIVILEGE = "42501"

# One row per table, with only the columns that have no default.
INSERTS = {
    "checkpoints": (
        "INSERT INTO runtime.checkpoints (thread_id, checkpoint_id, checkpoint) "
        "VALUES (%s, 'c1', '{}')"
    ),
    "checkpoint_blobs": (
        "INSERT INTO runtime.checkpoint_blobs (thread_id, channel, version, type) "
        "VALUES (%s, 'ch', 'v1', 'msgpack')"
    ),
    "checkpoint_writes": (
        "INSERT INTO runtime.checkpoint_writes "
        "(thread_id, checkpoint_id, task_id, idx, channel, blob) "
        "VALUES (%s, 'c1', 't1', 0, 'ch', '\\x00')"
    ),
}
# A column of each table that UPDATE can change without touching a key.
UPDATES = {
    "checkpoints": "UPDATE runtime.checkpoints SET type = 'x' WHERE thread_id = %s",
    "checkpoint_blobs": "UPDATE runtime.checkpoint_blobs SET type = 'x' "
    "WHERE thread_id = %s",
    "checkpoint_writes": "UPDATE runtime.checkpoint_writes SET type = 'x' "
    "WHERE thread_id = %s",
}


def run(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> list:
    """Run one statement as ``role`` in its own transaction; return the rows."""
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


# ── the shape equals the library's ──────────────────────────────────────────
@contextmanager
def library_schema(db: DatabaseHandle) -> Iterator[str]:
    """A scratch schema holding the tables the library's MIGRATIONS create.

    Run as the owner with ``search_path`` on the schema, in autocommit so that
    the library's ``CREATE INDEX CONCURRENTLY`` works, as ``setup()`` runs them.
    """
    schema = f"scratch_ckpt_{secrets.token_hex(4)}"
    with psycopg.connect(db.dsn(OWNER), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            conn.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
            )
            for statement in MIGRATIONS:
                conn.execute(statement)
            yield schema
        finally:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


COLUMNS = (
    "SELECT table_name, column_name, data_type, is_nullable, column_default "
    "FROM information_schema.columns WHERE table_schema = %s "
    "AND table_name = ANY(%s) ORDER BY table_name, column_name"
)
PRIMARY_KEYS = (
    "SELECT conrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint "
    "WHERE contype = 'p' AND connamespace = %s::regnamespace ORDER BY 1"
)
# Each index as its table, uniqueness and indexed columns in order; the primary
# keys' own indexes are left to the constraint query above.
INDEXES = """
SELECT c.relname, i.indisunique,
       array_agg(a.attname ORDER BY k.ord) AS indexed_columns
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE n.nspname = %s AND NOT i.indisprimary AND c.relname = ANY(%s)
GROUP BY c.relname, i.indexrelid, i.indisunique
ORDER BY 1, 3
"""


def shape(db: DatabaseHandle, schema: str) -> dict[str, list]:
    """Columns, primary keys and indexes of the checkpoint tables in ``schema``,
    without the library's own ledger and without the schema's name."""
    with connect(db.dsn(OWNER), "test") as conn:
        tables = [
            name
            for (name,) in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name LIKE 'checkpoint%%' "
                "AND table_name <> %s ORDER BY 1",
                (schema, LIBRARY_LEDGER),
            )
        ]
        columns = conn.execute(COLUMNS, (schema, tables)).fetchall()
        keys = conn.execute(PRIMARY_KEYS, (schema,)).fetchall()
        indexes = conn.execute(INDEXES, (schema, tables)).fetchall()
    return {
        "tables": tables,
        "columns": columns,
        "primary keys": [
            (name, definition)
            for table, definition in keys
            if (name := table.removeprefix(f"{schema}.")) in tables
        ],
        "indexes": indexes,
    }


def test_the_migration_is_recorded_in_the_ledger(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]

    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert "0008_checkpoints.sql" in names
    assert [name for (name,) in recorded] == names


def test_the_tables_are_the_ones_the_library_creates(
    migrated_database: DatabaseHandle,
) -> None:
    with library_schema(migrated_database) as scratch:
        library = shape(migrated_database, scratch)

    ours = shape(migrated_database, "runtime")

    # The comparison has something to compare: every table, key and index.
    assert library["tables"] == sorted(TABLES)
    assert len(library["primary keys"]) == len(library["indexes"]) == len(TABLES)
    assert ours == library


def test_the_comparison_notices_a_table_that_differs_from_the_library(
    migrated_database: DatabaseHandle,
) -> None:
    drop_column = sql.SQL("ALTER TABLE {}.checkpoint_writes DROP COLUMN task_path")
    with (
        library_schema(migrated_database) as scratch,
        psycopg.connect(migrated_database.dsn(OWNER), autocommit=True) as conn,
    ):
        conn.execute(drop_column.format(sql.Identifier(scratch)))
        changed = shape(migrated_database, scratch)

    assert changed != shape(migrated_database, "runtime")


def test_the_library_ledger_table_is_not_created(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT count(*) FROM pg_class WHERE relname = %s",
        (LIBRARY_LEDGER,),
    )

    assert rows == [(0,)]


# ── who may touch the tables ────────────────────────────────────────────────
@pytest.mark.parametrize("table", TABLES)
def test_the_runtime_reads_writes_updates_and_deletes_each_table(
    migrated_database: DatabaseHandle, table: str
) -> None:
    thread = f"privilege-{secrets.token_hex(4)}"

    inserted = run(migrated_database, "agent_runtime", INSERTS[table], (thread,))
    updated = run(
        migrated_database, "agent_runtime", UPDATES[table] + " RETURNING 1", (thread,)
    )
    selected = run(
        migrated_database,
        "agent_runtime",
        f"SELECT thread_id FROM runtime.{table} WHERE thread_id = %s",  # noqa: S608
        (thread,),
    )
    deleted = run(
        migrated_database,
        "agent_runtime",
        f"DELETE FROM runtime.{table} WHERE thread_id = %s RETURNING 1",  # noqa: S608
        (thread,),
    )

    assert (inserted, updated, selected, deleted) == (
        [],
        [(1,)],
        [(thread,)],
        [(1,)],
    )


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("role", OTHER_ROLES)
def test_no_other_role_can_read_or_write_a_checkpoint_table(
    migrated_database: DatabaseHandle, role: str, table: str
) -> None:
    thread = f"refused-{secrets.token_hex(4)}"
    # A row to be refused sight of, so a refusal is not an empty table.
    run(migrated_database, OWNER, INSERTS[table], (thread,))
    statements = {
        "select": f"SELECT thread_id FROM runtime.{table}",  # noqa: S608
        "insert": INSERTS[table],
    }

    for name, statement in statements.items():
        params = (thread,) if name == "insert" else ()
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as refused:
            run(migrated_database, role, statement, params)
        assert refused.value.sqlstate == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("table", TABLES)
def test_only_the_runtime_the_sweep_and_the_owner_hold_a_privilege_on_a_table(
    migrated_database: DatabaseHandle, table: str
) -> None:
    holders = run(
        migrated_database,
        OWNER,
        "SELECT DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
        "ELSE pg_get_userbyid(a.grantee) END "
        "FROM pg_class c, aclexplode(c.relacl) a "
        "WHERE c.oid = %s::regclass ORDER BY 1",
        (f"runtime.{table}",),
    )

    # 0013 gave claims_sweep DELETE, to remove a thread's rows.
    assert holders == [("agent_runtime",), ("claims_sweep",), (OWNER,)]


def test_the_runtime_cannot_create_the_libraries_ledger_table(
    migrated_database: DatabaseHandle,
) -> None:
    with (
        open_saver(migrated_database.dsn("agent_runtime")) as saver,
        pytest.raises(psycopg.errors.InsufficientPrivilege) as refused,
    ):
        saver.setup()

    assert refused.value.sqlstate == INSUFFICIENT_PRIVILEGE
