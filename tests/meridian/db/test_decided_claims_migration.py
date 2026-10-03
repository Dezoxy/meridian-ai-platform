"""0013: the view of decided claims the policy server reads (S053, T-66, T-75)."""

import datetime
import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from psycopg.types.json import Jsonb

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import migration_files

ROLE = "policy_mcp"
OTHER_ROLES = tuple(role for role in SERVICE_ROLES if role != ROLE)
TENANT = "development"
CLAIM = "CLM-0013"
INSUFFICIENT_PRIVILEGE = "42501"
OBJECT_NOT_IN_PREREQUISITE_STATE = "55000"
VIEW_COLUMNS = (
    "claim_id",
    "tenant",
    "policy_number",
    "loss_date",
    "peril",
    "paid_amount",
    "state",
)
SELECT_VIEW = (
    "SELECT claim_id, tenant, policy_number, loss_date, peril, paid_amount, state "
    "FROM claims.decided_claims ORDER BY claim_id"
)
INSERT_CLAIM = (
    "INSERT INTO claims.claims (claim_id, tenant, submission, state) "
    "VALUES (%s, %s, %s, %s)"
)
INSERT_PROPOSAL = (
    "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, route, "
    "reason, proposal, created_at) VALUES (%s, %s, %s, 'adjuster', 'r', %s, %s)"
)


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


def submission(**fields: object) -> Jsonb:
    base = {"policy_number": "POL-0001", "loss_date": "2026-07-13", "peril": "storm"}
    return Jsonb({**base, **fields})


def add_claim(
    db: DatabaseHandle,
    claim_id: str,
    state: str,
    *,
    tenant: str = TENANT,
    body: Jsonb | None = None,
) -> None:
    run(db, OWNER, INSERT_CLAIM, (claim_id, tenant, body or submission(), state))


def add_proposal(
    db: DatabaseHandle, claim_id: str, created_at: str, document: dict | None
) -> None:
    proposal = {"route": "adjuster", "reason": "r", **(document or {})}
    run(
        db,
        OWNER,
        INSERT_PROPOSAL,
        (uuid.uuid4(), claim_id, uuid.uuid4(), Jsonb(proposal), created_at),
    )


def test_the_migration_is_the_thirteenth_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert names[12] == "0013_decided_claims.sql"
    assert ("0013_decided_claims.sql",) in recorded


def test_the_view_has_exactly_the_seven_columns_in_order(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'claims' AND table_name = 'decided_claims' "
        "ORDER BY ordinal_position",
    )

    assert rows == [
        ("claim_id", "text"),
        ("tenant", "text"),
        ("policy_number", "text"),
        ("loss_date", "date"),
        ("peril", "text"),
        ("paid_amount", "integer"),
        ("state", "text"),
    ]


def test_the_view_is_a_security_barrier(migrated_database: DatabaseHandle) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT reloptions FROM pg_class WHERE oid = 'claims.decided_claims'::regclass",
    )

    assert rows == [(["security_barrier=true"],)]


def test_the_view_holds_the_approved_and_the_rejected_claims_and_no_others(
    fresh_database: DatabaseHandle,
) -> None:
    states = (
        "submitted",
        "triaging",
        "triage_failed",
        "awaiting_adjuster",
        "documents_requested",
        "approved",
        "rejected",
        "withdrawn",
    )
    for number, state in enumerate(states, start=1):
        add_claim(fresh_database, f"CLM-{number:04d}", state)

    rows = run(
        fresh_database, ROLE, "SELECT claim_id, state FROM claims.decided_claims"
    )

    assert sorted(rows) == [("CLM-0006", "approved"), ("CLM-0007", "rejected")]


def test_a_row_carries_what_the_submission_says(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(
        fresh_database,
        CLAIM,
        "rejected",
        body=submission(policy_number="POL-0042", loss_date="2026-05-01", peril="fire"),
    )

    assert run(fresh_database, ROLE, SELECT_VIEW) == [
        (CLAIM, TENANT, "POL-0042", datetime.date(2026, 5, 1), "fire", 0, "rejected")
    ]


def test_an_approved_claim_carries_its_latest_proposal_s_amount(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "approved")
    # The older proposal holds the larger amount, so an order by amount or by
    # insertion would answer 900.
    add_proposal(fresh_database, CLAIM, "2026-09-02T10:00:00Z", {"payable_amount": 900})
    add_proposal(fresh_database, CLAIM, "2026-09-03T10:00:00Z", {"payable_amount": 450})

    rows = run(fresh_database, ROLE, "SELECT paid_amount FROM claims.decided_claims")

    assert rows == [(450,)]


def test_a_proposal_inserted_first_but_created_last_is_the_latest(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "approved")
    add_proposal(fresh_database, CLAIM, "2026-09-03T10:00:00Z", {"payable_amount": 450})
    add_proposal(fresh_database, CLAIM, "2026-09-02T10:00:00Z", {"payable_amount": 900})

    rows = run(fresh_database, ROLE, "SELECT paid_amount FROM claims.decided_claims")

    assert rows == [(450,)]


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"payable_amount": None},
        {"payable_amount": "450"},
        {"payable_amount": [450]},
        {"payable_amount": -5},
        {"payable_amount": 1_000_000_001},
    ],
    ids=["missing", "null", "string", "array", "negative", "over-cap"],
)
def test_an_approved_claim_whose_proposal_has_no_usable_amount_is_paid_nothing(
    fresh_database: DatabaseHandle, document: dict
) -> None:
    add_claim(fresh_database, CLAIM, "approved")
    add_proposal(fresh_database, CLAIM, "2026-09-02T10:00:00Z", document)

    rows = run(fresh_database, ROLE, "SELECT paid_amount FROM claims.decided_claims")

    assert rows == [(0,)]


def test_a_fractional_amount_is_rounded_to_a_whole_one(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "approved")
    add_proposal(
        fresh_database, CLAIM, "2026-09-02T10:00:00Z", {"payable_amount": 12.5}
    )

    rows = run(fresh_database, ROLE, "SELECT paid_amount FROM claims.decided_claims")

    assert rows == [(13,)]


def test_an_approved_claim_at_the_cap_keeps_its_amount(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "approved")
    add_proposal(
        fresh_database,
        CLAIM,
        "2026-09-02T10:00:00Z",
        {"payable_amount": 1_000_000_000},
    )

    rows = run(fresh_database, ROLE, "SELECT paid_amount FROM claims.decided_claims")

    assert rows == [(1_000_000_000,)]


def test_an_approved_claim_without_a_proposal_is_paid_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "approved")

    rows = run(fresh_database, ROLE, "SELECT paid_amount FROM claims.decided_claims")

    assert rows == [(0,)]


def test_a_rejected_claim_is_paid_nothing_whatever_its_proposal_says(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "rejected")
    add_proposal(fresh_database, CLAIM, "2026-09-02T10:00:00Z", {"payable_amount": 700})

    rows = run(fresh_database, ROLE, "SELECT paid_amount FROM claims.decided_claims")

    assert rows == [(0,)]


@pytest.mark.parametrize(
    "body",
    [
        Jsonb({}),
        Jsonb({"loss_date": "not a date"}),
        Jsonb({"loss_date": "2026-02-31"}),
        Jsonb({"loss_date": "today"}),
        Jsonb({"loss_date": 20260713}),
    ],
    ids=["empty", "text", "impossible", "keyword", "number"],
)
def test_a_submission_without_a_usable_loss_date_does_not_break_the_view(
    fresh_database: DatabaseHandle, body: Jsonb
) -> None:
    add_claim(fresh_database, "CLM-0001", "approved", body=body)
    add_claim(fresh_database, "CLM-0002", "approved")

    rows = run(
        fresh_database,
        ROLE,
        "SELECT claim_id, loss_date FROM claims.decided_claims ORDER BY claim_id",
    )

    assert [claim_id for claim_id, _ in rows] == ["CLM-0001", "CLM-0002"]
    assert rows[0][1] is None
    assert rows[1][1] is not None


def test_policy_mcp_may_select_the_view_and_still_not_read_the_submission(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "approved")

    viewed = run(fresh_database, ROLE, "SELECT claim_id FROM claims.decided_claims")
    privileges = run(
        fresh_database,
        OWNER,
        "SELECT has_table_privilege('policy_mcp', 'claims.decided_claims', 'SELECT'), "
        "has_column_privilege('policy_mcp', 'claims.claims', 'submission', 'SELECT')",
    )

    assert viewed == [(CLAIM,)]
    assert privileges == [(True, False)]
    assert (
        refused(fresh_database, ROLE, "SELECT submission FROM claims.claims")
        == INSUFFICIENT_PRIVILEGE
    )


def test_policy_mcp_has_no_new_privilege_on_the_claims_it_could_not_read(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_table_privilege('policy_mcp', 'claims.claims', 'SELECT'), "
        "has_column_privilege('policy_mcp', 'claims.claims', 'state', 'SELECT'), "
        "has_table_privilege('policy_mcp', 'claims.triage_proposals', 'SELECT')",
    )

    assert rows == [(False, False, False)]


@pytest.mark.parametrize("role", OTHER_ROLES)
def test_no_other_service_role_has_any_privilege_on_the_view(
    migrated_database: DatabaseHandle, role: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_table_privilege(%s, 'claims.decided_claims', "
        "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'), "
        "has_any_column_privilege(%s, 'claims.decided_claims', "
        "'SELECT, INSERT, UPDATE, REFERENCES')",
        (role, role),
    )

    assert rows == [(False, False)]


def test_the_view_is_granted_to_policy_mcp_alone_and_to_public_not_at_all(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
        "WHERE table_schema = 'claims' AND table_name = 'decided_claims' "
        "AND grantee <> %s ORDER BY grantee, privilege_type",
        (OWNER,),
    )

    assert rows == [("policy_mcp", "SELECT")]


def test_claims_api_has_no_privilege_on_the_policy_schema(
    migrated_database: DatabaseHandle,
) -> None:
    # The owner's decision: the Claims API does not write the policy store; a
    # decided claim reaches the history through the view instead.
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_schema_privilege('claims_api', 'policy', 'USAGE, CREATE'), "
        "has_table_privilege('claims_api', 'policy.claim_history', "
        "'SELECT, INSERT, UPDATE, DELETE')",
    )

    assert rows == [(False, False)]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO claims.decided_claims (claim_id, tenant, policy_number, "
        "loss_date, peril, paid_amount, state) VALUES "
        "('CLM-0099', 'development', 'POL-0001', '2026-07-13', 'storm', 1, 'approved')",
        "UPDATE claims.decided_claims SET paid_amount = 1",
        "DELETE FROM claims.decided_claims",
    ],
)
def test_policy_mcp_cannot_write_through_the_view(
    fresh_database: DatabaseHandle, statement: str
) -> None:
    add_claim(fresh_database, CLAIM, "approved")

    # The view joins a table, so PostgreSQL may refuse the write as "cannot
    # insert into view" before it checks a privilege; either way nothing passes.
    sqlstate = refused(fresh_database, ROLE, statement)

    assert sqlstate in {INSUFFICIENT_PRIVILEGE, OBJECT_NOT_IN_PREREQUISITE_STATE}
    assert run(fresh_database, OWNER, "SELECT count(*) FROM claims.claims") == [(1,)]


def test_the_partial_index_covers_the_tool_s_lookup(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index AS i "
        "WHERE i.indrelid = 'claims.claims'::regclass AND i.indpred IS NOT NULL "
        "AND pg_get_expr(i.indpred, i.indrelid) LIKE '%%approved%%'",
    )

    assert len(rows) == 1
    definition = rows[0][0]
    assert "(tenant, policy_number)" in definition
    assert "'approved'" in definition
    assert "'rejected'" in definition
