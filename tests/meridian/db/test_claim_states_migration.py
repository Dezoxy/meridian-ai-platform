"""0009: the claim's state, the adjuster's decisions and the Claims API's audit."""

import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from psycopg.types.json import Jsonb

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

ROLE = "claims_api"
# claims_mcp reads three columns of claims.decisions since 0010
# (test_approval_outcome_migration.py); every other role still sees nothing.
OTHER_ROLES = tuple(role for role in SERVICE_ROLES if role not in (ROLE, "claims_mcp"))
CLAIM_ID = "CLM-0009"
RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000009")
INSUFFICIENT_PRIVILEGE = "42501"
STATES = (
    "submitted",
    "triaging",
    "triage_failed",
    "awaiting_adjuster",
    "documents_requested",
    "approved",
    "rejected",
    "withdrawn",
)
INSERT_CLAIM = (
    "INSERT INTO claims.claims (claim_id, tenant, submission) "
    "VALUES (%s, 'development', '{}')"
)
INSERT_PROPOSAL = (
    "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, route, "
    "reason, proposal, created_at) VALUES (%s, %s, %s, %s, 'r', %s, %s)"
)
INSERT_DECISION = (
    "INSERT INTO claims.decisions (claim_id, run_id, decision) VALUES (%s, %s, %s)"
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
    """A migrated database of its own that holds one claim."""
    run(fresh_database, ROLE, INSERT_CLAIM, (CLAIM_ID,))
    return fresh_database


def refused(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> str:
    """The SQLSTATE of the error ``statement`` raises as ``role``."""
    with pytest.raises(psycopg.Error) as caught:
        run(db, role, statement, params)
    return caught.value.sqlstate or "none"


def test_the_migration_is_the_ninth_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert names[8] == "0009_claim_states.sql"
    assert ("0009_claim_states.sql",) in recorded


# ── the new columns ─────────────────────────────────────────────────────────
def test_a_new_claim_is_submitted_with_no_run(claim: DatabaseHandle) -> None:
    rows = run(
        claim,
        ROLE,
        "SELECT state, run_id, state_changed_at IS NOT NULL FROM claims.claims",
    )

    assert rows == [("submitted", None, True)]


@pytest.mark.parametrize("state", STATES)
def test_every_state_of_the_lifecycle_is_accepted(
    claim: DatabaseHandle, state: str
) -> None:
    run(claim, ROLE, "UPDATE claims.claims SET state = %s", (state,))

    assert run(claim, ROLE, "SELECT state FROM claims.claims") == [(state,)]


@pytest.mark.parametrize("state", ["Approved", "done", "", "awaiting-adjuster"])
def test_a_word_that_is_not_a_state_is_refused(
    claim: DatabaseHandle, state: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        run(claim, ROLE, "UPDATE claims.claims SET state = %s", (state,))


def test_a_state_cannot_be_null(claim: DatabaseHandle) -> None:
    with pytest.raises(psycopg.errors.NotNullViolation):
        run(claim, ROLE, "UPDATE claims.claims SET state = NULL")


# ── what is backfilled ──────────────────────────────────────────────────────
def test_the_claims_that_existed_take_the_state_their_proposal_leads_to(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    assert files[8][0] == "0009_claim_states.sql"
    monkeypatch.setattr(runner, "migration_files", lambda: files[:8])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    runs = {n: uuid.uuid4() for n in range(1, 7)}

    def propose(claim_id: str, number: int, route: str, created: str) -> None:
        run(
            empty_database,
            ROLE,
            INSERT_PROPOSAL,
            (
                uuid.uuid4(),
                claim_id,
                runs[number],
                route,
                Jsonb({"route": route, "reason": "r"}),
                created,
            ),
        )

    for claim_id in ("CLM-0001", "CLM-0002", "CLM-0003", "CLM-0004", "CLM-0005"):
        run(empty_database, ROLE, INSERT_CLAIM, (claim_id,))
    propose("CLM-0001", 1, "adjuster", "2026-09-01T10:00:00Z")
    propose("CLM-0002", 2, "auto_approve", "2026-09-01T10:00:00Z")
    propose("CLM-0003", 3, "request_documents", "2026-09-01T10:00:00Z")
    # CLM-0004 has no proposal. CLM-0005 was triaged twice: the later one counts.
    propose("CLM-0005", 4, "adjuster", "2026-09-01T10:00:00Z")
    propose("CLM-0005", 5, "auto_approve", "2026-09-02T10:00:00Z")
    monkeypatch.setattr(runner, "migration_files", lambda: files[:9])

    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = runner.apply_migrations(conn)

    assert applied == ["0009_claim_states.sql"]
    assert run(
        empty_database,
        OWNER,
        "SELECT claim_id, state, run_id FROM claims.claims ORDER BY claim_id",
    ) == [
        ("CLM-0001", "awaiting_adjuster", runs[1]),
        ("CLM-0002", "approved", runs[2]),
        ("CLM-0003", "documents_requested", runs[3]),
        ("CLM-0004", "triage_failed", None),
        ("CLM-0005", "approved", runs[5]),
    ]


# ── claims.decisions ────────────────────────────────────────────────────────
def test_claims_api_records_and_reads_a_decision(claim: DatabaseHandle) -> None:
    run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, RUN_ID, "approve"))

    assert run(
        claim, ROLE, "SELECT claim_id, run_id, decision FROM claims.decisions"
    ) == [(CLAIM_ID, RUN_ID, "approve")]
    columns = run(
        claim,
        OWNER,
        "SELECT decision_id IS NOT NULL, decided_at IS NOT NULL FROM claims.decisions",
    )
    assert columns == [(True, True)]


@pytest.mark.parametrize("word", ["Approve", "approved", "", "withdraw"])
def test_a_decision_that_is_not_one_of_the_three_words_is_refused(
    claim: DatabaseHandle, word: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, RUN_ID, word))


def test_a_run_is_decided_once(claim: DatabaseHandle) -> None:
    run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, RUN_ID, "approve"))

    with pytest.raises(psycopg.errors.UniqueViolation):
        run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, RUN_ID, "reject"))


def test_a_decision_on_an_unknown_claim_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        run(fresh_database, ROLE, INSERT_DECISION, ("CLM-9999", RUN_ID, "approve"))


def test_the_decisions_table_has_no_free_text_and_no_adjuster(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'claims' AND table_name = 'decisions' "
        "ORDER BY column_name",
    )

    assert rows == [
        ("claim_id",),
        ("decided_at",),
        ("decision",),
        ("decision_id",),
        ("run_id",),
    ]


# ── what claims_api may and may not do ──────────────────────────────────────
def test_claims_api_may_update_the_three_columns(claim: DatabaseHandle) -> None:
    run(
        claim,
        ROLE,
        "UPDATE claims.claims SET state = 'triaging', "
        "state_changed_at = clock_timestamp(), run_id = %s",
        (RUN_ID,),
    )

    assert run(claim, ROLE, "SELECT state, run_id FROM claims.claims") == [
        ("triaging", RUN_ID)
    ]


@pytest.mark.parametrize(
    "assignment",
    [
        "submission = '{}'",
        "tenant = 'other'",
        "claim_id = 'CLM-0010'",
        "received_at = now()",
    ],
)
def test_claims_api_may_update_nothing_else_of_a_claim(
    claim: DatabaseHandle, assignment: str
) -> None:
    state = refused(claim, ROLE, f"UPDATE claims.claims SET {assignment}")  # noqa: S608

    assert state == INSUFFICIENT_PRIVILEGE


def test_claims_api_may_still_not_delete_a_claim(claim: DatabaseHandle) -> None:
    assert refused(claim, ROLE, "DELETE FROM claims.claims") == INSUFFICIENT_PRIVILEGE


def test_claims_api_may_lock_a_claim_for_update(claim: DatabaseHandle) -> None:
    rows = run(
        claim,
        ROLE,
        "SELECT state FROM claims.claims WHERE claim_id = %s FOR UPDATE",
        (CLAIM_ID,),
    )

    assert rows == [("submitted",)]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE claims.decisions SET decision = 'reject'",
        "DELETE FROM claims.decisions",
        "TRUNCATE claims.decisions",
    ],
)
def test_claims_api_may_neither_change_nor_delete_a_decision(
    claim: DatabaseHandle, statement: str
) -> None:
    run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, RUN_ID, "approve"))

    assert refused(claim, ROLE, statement) == INSUFFICIENT_PRIVILEGE


def test_claims_api_appends_to_the_audit_log_and_cannot_read_it(
    fresh_database: DatabaseHandle,
) -> None:
    run(
        fresh_database,
        ROLE,
        "INSERT INTO audit.events (service, event, outcome) "
        "VALUES ('claims-api', 'claim.triaging', 'triaging')",
    )

    assert run(
        fresh_database,
        OWNER,
        "SELECT service, event, db_role FROM audit.events",
    ) == [("claims-api", "claim.triaging", ROLE)]
    assert (
        refused(fresh_database, ROLE, "SELECT count(*) FROM audit.events")
        == INSUFFICIENT_PRIVILEGE
    )


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit.events SET outcome = 'x'", "DELETE FROM audit.events"],
)
def test_claims_api_cannot_change_the_audit_log(
    fresh_database: DatabaseHandle, statement: str
) -> None:
    run(
        fresh_database,
        ROLE,
        "INSERT INTO audit.events (service, event, outcome) "
        "VALUES ('claims-api', 'claim.triaging', 'triaging')",
    )

    assert refused(fresh_database, ROLE, statement) == INSUFFICIENT_PRIVILEGE


# ── what every other role sees ──────────────────────────────────────────────
@pytest.mark.parametrize("role", OTHER_ROLES)
def test_no_other_service_role_has_any_privilege_on_decisions(
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


def test_the_policy_server_cannot_read_a_decision(claim: DatabaseHandle) -> None:
    run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, RUN_ID, "approve"))

    assert (
        refused(claim, "policy_mcp", "SELECT * FROM claims.decisions")
        == INSUFFICIENT_PRIVILEGE
    )
    assert (
        refused(claim, "policy_mcp", "SELECT decision FROM claims.decisions")
        == INSUFFICIENT_PRIVILEGE
    )


@pytest.mark.parametrize("role", ["policy_mcp", "claims_mcp"])
@pytest.mark.parametrize("column", ["state", "state_changed_at", "run_id"])
def test_the_tool_servers_cannot_read_the_new_claim_columns(
    migrated_database: DatabaseHandle, role: str, column: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_column_privilege(%s, 'claims.claims', %s, 'SELECT')",
        (role, column),
    )

    assert rows == [(False,)]
