"""0023: the second host's checkpoint table (S037, R3b)."""

import secrets

import psycopg
import pytest
from dbsupport import (
    JOB_ROLES,
    OWNER,
    SERVICE_ROLES,
    UPKEEP_ROLE,
    DatabaseHandle,
)

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import migration_files

TABLE = "workflow_checkpoints"
NAME = "0023_workflow_checkpoints.sql"
INSUFFICIENT_PRIVILEGE = "42501"
# Every role that has a login and no right on the table. The runtime writes it
# and the sweep removes from it; nobody else touches it.
OTHER_ROLES = (
    *(role for role in SERVICE_ROLES if role not in ("agent_runtime", "claims_sweep")),
    UPKEEP_ROLE,
    *JOB_ROLES,
)
INSERT = (
    "INSERT INTO runtime.workflow_checkpoints "
    "(thread_id, checkpoint_id, workflow_name, checkpointed_at, body) "
    "VALUES (%s, %s, 'claim-brief', %s, '{}')"
)
# The same instant for two rows, so only the sequence column can order them.
AN_INSTANT = "2026-10-06T12:00:00+00:00"


def run(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> list:
    """Run one statement as ``role`` in its own transaction; return the rows."""
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def thread_of(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


# ── the table ───────────────────────────────────────────────────────────────
def test_the_migration_is_in_the_ledger_after_the_job_roles(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]

    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert NAME in names
    assert names.index(NAME) > names.index("0022_job_roles.sql")
    assert [name for (name,) in recorded] == names


def test_the_table_has_these_columns_and_the_sequence_is_an_identity(
    migrated_database: DatabaseHandle,
) -> None:
    columns = run(
        migrated_database,
        OWNER,
        "SELECT column_name, data_type, is_nullable, is_identity, "
        "identity_generation FROM information_schema.columns "
        "WHERE table_schema = 'runtime' AND table_name = %s "
        "ORDER BY ordinal_position",
        (TABLE,),
    )

    assert columns == [
        ("seq", "bigint", "NO", "YES", "ALWAYS"),
        ("thread_id", "text", "NO", "NO", None),
        ("checkpoint_id", "text", "NO", "NO", None),
        ("workflow_name", "text", "NO", "NO", None),
        ("checkpointed_at", "timestamp with time zone", "NO", "NO", None),
        ("body", "jsonb", "NO", "NO", None),
    ]


def test_the_key_is_the_thread_and_the_checkpoint_and_no_other_index_exists(
    migrated_database: DatabaseHandle,
) -> None:
    keys = run(
        migrated_database,
        OWNER,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'runtime.workflow_checkpoints'::regclass",
    )
    indexes = run(
        migrated_database,
        OWNER,
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'runtime' AND tablename = %s",
        (TABLE,),
    )

    assert keys == [("PRIMARY KEY (thread_id, checkpoint_id)",)]
    # The key's index leads with thread_id: it serves the sweep's walk over
    # threads and a run's own lookups, so a second index would only duplicate it.
    assert len(indexes) == 1
    assert "(thread_id, checkpoint_id)" in indexes[0][0]


def test_the_table_name_is_outside_the_pattern_the_langgraph_comparison_selects(
    migrated_database: DatabaseHandle,
) -> None:
    langgraph_like = run(
        migrated_database,
        OWNER,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'runtime' AND table_name LIKE 'checkpoint%%' "
        "ORDER BY 1",
    )

    assert not TABLE.startswith("checkpoint")
    assert langgraph_like == [
        ("checkpoint_blobs",),
        ("checkpoint_writes",),
        ("checkpoints",),
    ]


# ── who may touch it ────────────────────────────────────────────────────────
def test_the_runtime_inserts_reads_updates_and_deletes_and_needs_no_sequence_right(
    migrated_database: DatabaseHandle,
) -> None:
    thread = thread_of("runtime")

    inserted = run(
        migrated_database, "agent_runtime", INSERT, (thread, "c1", AN_INSTANT)
    )
    updated = run(
        migrated_database,
        "agent_runtime",
        "UPDATE runtime.workflow_checkpoints SET workflow_name = 'other' "
        "WHERE thread_id = %s RETURNING 1",
        (thread,),
    )
    selected = run(
        migrated_database,
        "agent_runtime",
        "SELECT thread_id, body FROM runtime.workflow_checkpoints WHERE thread_id = %s",
        (thread,),
    )
    deleted = run(
        migrated_database,
        "agent_runtime",
        "DELETE FROM runtime.workflow_checkpoints WHERE thread_id = %s RETURNING 1",
        (thread,),
    )

    assert (inserted, updated, selected, deleted) == (
        [],
        [(1,)],
        [(thread, {})],
        [(1,)],
    )


def test_two_checkpoints_of_one_instant_are_told_apart_by_the_sequence(
    migrated_database: DatabaseHandle,
) -> None:
    thread = thread_of("order")
    for checkpoint in ("b-first", "a-second"):
        run(
            migrated_database, "agent_runtime", INSERT, (thread, checkpoint, AN_INSTANT)
        )

    ordered = run(
        migrated_database,
        "agent_runtime",
        "SELECT checkpoint_id FROM runtime.workflow_checkpoints "
        "WHERE thread_id = %s ORDER BY checkpointed_at, seq",
        (thread,),
    )

    assert ordered == [("b-first",), ("a-second",)]


def test_the_sweep_reads_the_thread_and_deletes_and_cannot_read_a_body_or_write(
    migrated_database: DatabaseHandle,
) -> None:
    thread = thread_of("sweep")
    run(migrated_database, OWNER, INSERT, (thread, "c1", AN_INSTANT))
    refused = (
        ("SELECT body FROM runtime.workflow_checkpoints", ()),
        ("SELECT * FROM runtime.workflow_checkpoints", ()),
        ("SELECT checkpoint_id FROM runtime.workflow_checkpoints", ()),
        (INSERT, (thread, "c9", AN_INSTANT)),
        ("UPDATE runtime.workflow_checkpoints SET workflow_name = 'x'", ()),
    )

    seen = run(
        migrated_database,
        "claims_sweep",
        "SELECT thread_id FROM runtime.workflow_checkpoints WHERE thread_id = %s",
        (thread,),
    )
    for statement, params in refused:
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
            run(migrated_database, "claims_sweep", statement, params)
        assert caught.value.sqlstate == INSUFFICIENT_PRIVILEGE
    deleted = run(
        migrated_database,
        "claims_sweep",
        "DELETE FROM runtime.workflow_checkpoints WHERE thread_id = %s RETURNING 1",
        (thread,),
    )

    assert seen == [(thread,)]
    assert deleted == [(1,)]


@pytest.mark.parametrize("role", OTHER_ROLES)
def test_no_other_role_can_read_write_or_delete_the_table(
    migrated_database: DatabaseHandle, role: str
) -> None:
    thread = thread_of("refused")
    # A row to be refused sight of, so a refusal is not an empty table.
    run(migrated_database, OWNER, INSERT, (thread, "c1", AN_INSTANT))
    statements = {
        "select": ("SELECT thread_id FROM runtime.workflow_checkpoints", ()),
        "insert": (INSERT, (thread, "c2", AN_INSTANT)),
        "delete": ("DELETE FROM runtime.workflow_checkpoints", ()),
    }

    for statement, params in statements.values():
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as refused:
            run(migrated_database, role, statement, params)
        assert refused.value.sqlstate == INSUFFICIENT_PRIVILEGE


def test_only_the_runtime_the_sweep_and_the_owner_hold_a_privilege_on_the_table(
    migrated_database: DatabaseHandle,
) -> None:
    holders = run(
        migrated_database,
        OWNER,
        "SELECT DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
        "ELSE pg_get_userbyid(a.grantee) END "
        "FROM pg_class c, aclexplode(c.relacl) a "
        "WHERE c.oid = 'runtime.workflow_checkpoints'::regclass ORDER BY 1",
    )
    sweep_columns = run(
        migrated_database,
        OWNER,
        "SELECT a.attname FROM pg_attribute a "
        "WHERE a.attrelid = 'runtime.workflow_checkpoints'::regclass "
        "AND a.attnum > 0 AND NOT a.attisdropped "
        "AND has_column_privilege('claims_sweep', a.attrelid, a.attnum, 'SELECT') "
        "ORDER BY 1",
    )

    assert holders == [("agent_runtime",), ("claims_sweep",), (OWNER,)]
    # The sweep's SELECT is the one column it finds a thread by.
    assert sweep_columns == [("thread_id",)]
