"""0007: the triage proposal as a document, and the draft columns made optional."""

import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from psycopg.types.json import Jsonb

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import migration_files

ROLE = "claims_api"
OTHER_SERVICE_ROLES = tuple(role for role in SERVICE_ROLES if role != ROLE)
CLAIM_ID = "CLM-0007"
RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000007")
DOCUMENT = {"route": "adjuster", "reason": "unverified"}
INSERT_WITH_PROPOSAL = (
    "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, route, "
    "reason, proposal) VALUES (%s, %s, %s, 'adjuster', 'unverified', %s)"
)
OLD_INSERT = (
    "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, route, "
    "reason, draft, drafted_by_deployment, drafted_by_provider, drafted_by_mode) "
    "VALUES (%s, %s, %s, 'adjuster', 'r', 'd', 'replay-chat', 'replay', 'replay')"
)


def run(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> list:
    """Run one statement as ``role`` in its own transaction; return the rows."""
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


@pytest.fixture
def claim(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A migrated database of its own that holds the claim the proposals are for."""
    run(
        fresh_database,
        ROLE,
        "INSERT INTO claims.claims (claim_id, tenant, submission) "
        "VALUES (%s, 'development', '{}')",
        (CLAIM_ID,),
    )
    return fresh_database


def test_the_migration_is_the_seventh_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert names[6] == "0007_triage_proposal.sql"
    assert ("0007_triage_proposal.sql",) in recorded


def test_the_proposal_column_is_a_nullable_jsonb(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'claims' AND table_name = 'triage_proposals' "
        "AND column_name = 'proposal'",
    )

    assert rows == [("jsonb", "YES")]


@pytest.mark.parametrize(
    "column",
    [
        "draft",
        "drafted_by_deployment",
        "drafted_by_provider",
        "drafted_by_mode",
    ],
)
def test_the_draft_columns_are_no_longer_required(
    migrated_database: DatabaseHandle, column: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'claims' AND table_name = 'triage_proposals' "
        "AND column_name = %s",
        (column,),
    )

    assert rows == [("YES",)]


def test_the_columns_that_stay_required_stay_required(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'claims' AND table_name = 'triage_proposals' "
        "AND is_nullable = 'NO' ORDER BY column_name",
    )

    assert rows == [
        ("claim_id",),
        ("created_at",),
        ("proposal_id",),
        ("reason",),
        ("route",),
        ("run_id",),
    ]


# ── what the service role can do ────────────────────────────────────────────
def test_claims_api_stores_and_reads_a_proposal_document(claim: DatabaseHandle) -> None:
    run(
        claim,
        ROLE,
        INSERT_WITH_PROPOSAL,
        (uuid.uuid4(), CLAIM_ID, RUN_ID, Jsonb(DOCUMENT)),
    )

    rows = run(
        claim,
        ROLE,
        "SELECT route, reason, proposal, draft FROM claims.triage_proposals",
    )

    assert rows == [("adjuster", "unverified", DOCUMENT, None)]


def test_the_insert_of_the_walking_skeleton_still_works(claim: DatabaseHandle) -> None:
    run(claim, ROLE, OLD_INSERT, (uuid.uuid4(), CLAIM_ID, RUN_ID))

    rows = run(
        claim,
        ROLE,
        "SELECT draft, drafted_by_mode, proposal FROM claims.triage_proposals",
    )

    assert rows == [("d", "replay", None)]


def test_a_row_with_neither_a_proposal_nor_a_draft_is_refused(
    claim: DatabaseHandle,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            claim,
            ROLE,
            "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, "
            "route, reason) VALUES (%s, %s, %s, 'adjuster', 'unverified')",
            (uuid.uuid4(), CLAIM_ID, RUN_ID),
        )


@pytest.mark.parametrize(
    "document",
    [[], [DOCUMENT], "text", 7, True],
    ids=["empty-array", "array", "string", "number", "boolean"],
)
def test_a_proposal_that_is_not_a_json_object_is_refused(
    claim: DatabaseHandle, document: object
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            claim,
            ROLE,
            INSERT_WITH_PROPOSAL,
            (uuid.uuid4(), CLAIM_ID, RUN_ID, Jsonb(document)),
        )


def test_a_json_null_is_not_a_proposal_object(claim: DatabaseHandle) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            claim,
            ROLE,
            INSERT_WITH_PROPOSAL,
            (uuid.uuid4(), CLAIM_ID, RUN_ID, Jsonb(None)),
        )


def test_an_unknown_route_is_still_refused(claim: DatabaseHandle) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            claim,
            ROLE,
            "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, "
            "route, reason, proposal) VALUES (%s, %s, %s, 'approve', 'r', %s)",
            (uuid.uuid4(), CLAIM_ID, RUN_ID, Jsonb(DOCUMENT)),
        )


# ── no grant changed ────────────────────────────────────────────────────────
def test_claims_api_may_still_only_select_and_insert_on_the_table(
    migrated_database: DatabaseHandle,
) -> None:
    held = [
        privilege
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        )
        if run(
            migrated_database,
            OWNER,
            "SELECT has_table_privilege(%s, 'claims.triage_proposals', %s)",
            (ROLE, privilege),
        )[0][0]
    ]

    assert held == ["SELECT", "INSERT"]


@pytest.mark.parametrize("role", OTHER_SERVICE_ROLES)
def test_no_other_service_role_can_read_the_proposal_column(
    migrated_database: DatabaseHandle, role: str
) -> None:
    table = run(
        migrated_database,
        OWNER,
        "SELECT has_table_privilege(%s, 'claims.triage_proposals', 'SELECT'), "
        "has_any_column_privilege(%s, 'claims.triage_proposals', 'SELECT'), "
        "has_column_privilege(%s, 'claims.triage_proposals', 'proposal', 'SELECT')",
        (role, role, role),
    )

    assert table == [(False, False, False)]


@pytest.mark.parametrize("role", OTHER_SERVICE_ROLES)
def test_no_other_service_role_can_read_the_table(
    claim: DatabaseHandle, role: str
) -> None:
    run(
        claim,
        ROLE,
        INSERT_WITH_PROPOSAL,
        (uuid.uuid4(), CLAIM_ID, RUN_ID, Jsonb(DOCUMENT)),
    )

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(claim, role, "SELECT proposal FROM claims.triage_proposals")
