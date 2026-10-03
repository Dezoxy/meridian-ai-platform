"""0010: the claims tool server reads a run's recorded decision (S015)."""

import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import migration_files

ROLE = "claims_mcp"
OTHER_ROLES = tuple(role for role in SERVICE_ROLES if role not in (ROLE, "claims_api"))
CLAIM_ID = "CLM-0010"
RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000010")
INSUFFICIENT_PRIVILEGE = "42501"
INSERT_CLAIM = (
    "INSERT INTO claims.claims (claim_id, tenant, submission) "
    "VALUES (%s, 'development', '{}')"
)
INSERT_DECISION = (
    "INSERT INTO claims.decisions (claim_id, run_id, decision) VALUES (%s, %s, %s)"
)
READABLE = ("claim_id", "run_id", "decision")
UNREADABLE = ("decision_id", "decided_at")


def run(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> list:
    """Run one statement as ``role`` in its own transaction; return the rows."""
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def refused(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> str:
    """The SQLSTATE of the error ``statement`` raises as ``role``."""
    with pytest.raises(psycopg.Error) as caught:
        run(db, role, statement, params)
    return caught.value.sqlstate or "none"


@pytest.fixture
def decided(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A migrated database of its own that holds one claim and its decision."""
    run(fresh_database, "claims_api", INSERT_CLAIM, (CLAIM_ID,))
    run(fresh_database, "claims_api", INSERT_DECISION, (CLAIM_ID, RUN_ID, "reject"))
    return fresh_database


def test_the_migration_is_the_tenth_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert names[9] == "0010_approval_outcome.sql"
    assert ("0010_approval_outcome.sql",) in recorded


def test_claims_mcp_reads_a_decision_by_run_and_claim(decided: DatabaseHandle) -> None:
    rows = run(
        decided,
        ROLE,
        "SELECT decision FROM claims.decisions WHERE run_id = %s AND claim_id = %s",
        (RUN_ID, CLAIM_ID),
    )

    assert rows == [("reject",)]


@pytest.mark.parametrize("column", READABLE)
def test_claims_mcp_may_select_each_of_the_three_columns(
    migrated_database: DatabaseHandle, column: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_column_privilege(%s, 'claims.decisions', %s, 'SELECT')",
        (ROLE, column),
    )

    assert rows == [(True,)]


@pytest.mark.parametrize("column", UNREADABLE)
def test_claims_mcp_may_select_nothing_else_of_a_decision(
    decided: DatabaseHandle, column: str
) -> None:
    assert (
        refused(decided, ROLE, f"SELECT {column} FROM claims.decisions")  # noqa: S608
        == INSUFFICIENT_PRIVILEGE
    )


def test_claims_mcp_may_not_select_every_column(decided: DatabaseHandle) -> None:
    assert (
        refused(decided, ROLE, "SELECT * FROM claims.decisions")
        == INSUFFICIENT_PRIVILEGE
    )


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO claims.decisions (claim_id, run_id, decision) "
        "VALUES ('CLM-0010', gen_random_uuid(), 'approve')",
        "UPDATE claims.decisions SET decision = 'approve'",
        "DELETE FROM claims.decisions",
        "TRUNCATE claims.decisions",
    ],
)
def test_claims_mcp_may_not_insert_update_or_delete_a_decision(
    decided: DatabaseHandle, statement: str
) -> None:
    assert refused(decided, ROLE, statement) == INSUFFICIENT_PRIVILEGE
    assert run(decided, OWNER, "SELECT decision FROM claims.decisions") == [("reject",)]


@pytest.mark.parametrize("role", OTHER_ROLES)
def test_no_other_tool_or_runtime_role_has_any_privilege_on_decisions(
    migrated_database: DatabaseHandle, role: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_table_privilege(%s, 'claims.decisions', 'SELECT'), "
        "has_any_column_privilege(%s, 'claims.decisions', "
        "'SELECT, INSERT, UPDATE, REFERENCES')",
        (role, role),
    )

    assert rows == [(False, False)]


def test_claims_mcp_holds_no_table_level_privilege_on_decisions(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_table_privilege(%s, 'claims.decisions', 'SELECT'), "
        "has_table_privilege(%s, 'claims.decisions', "
        "'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')",
        (ROLE, ROLE),
    )

    assert rows == [(False, False)]


def test_the_claim_id_of_a_decision_is_indexed(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'claims' "
        "AND tablename = 'decisions' AND indexname = 'decisions_claim_id_idx'",
    )

    assert len(rows) == 1
    assert "(claim_id)" in rows[0][0]
