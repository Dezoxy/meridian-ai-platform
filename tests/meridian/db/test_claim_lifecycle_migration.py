"""0012: the triage counter, the two outcome words and the documents (S048)."""

import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from psycopg.types.json import Jsonb

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

ROLE = "claims_api"
OTHER_ROLES = tuple(role for role in SERVICE_ROLES if role != ROLE)
CLAIM_ID = "CLM-0012"
RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000012")
OTHER_RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000013")
INSUFFICIENT_PRIVILEGE = "42501"
OUTCOMES = ("approve", "reject", "request_documents", "send_back", "withdrawn")
WORDS_THAT_END_A_RUN = ("send_back", "withdrawn")
DECISIONS = ("approve", "reject", "request_documents")
INSERT_CLAIM = (
    "INSERT INTO claims.claims (claim_id, tenant, submission) "
    "VALUES (%s, 'development', '{}')"
)
INSERT_PROPOSAL = (
    "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, route, "
    "reason, proposal, created_at) VALUES (%s, %s, %s, 'adjuster', 'r', %s, %s)"
)
INSERT_DECISION = (
    "INSERT INTO claims.decisions (claim_id, run_id, decision) VALUES (%s, %s, %s)"
)
INSERT_DOCUMENT = "INSERT INTO claims.claim_documents (claim_id, name) VALUES (%s, %s)"


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
def claim(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A migrated database of its own that holds one claim."""
    run(fresh_database, ROLE, INSERT_CLAIM, (CLAIM_ID,))
    return fresh_database


def test_the_migration_is_the_twelfth_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert names[11] == "0012_claim_lifecycle.sql"
    assert ("0012_claim_lifecycle.sql",) in recorded


# ── claims.claims.triages ───────────────────────────────────────────────────
def test_a_new_claim_has_been_triaged_no_time(claim: DatabaseHandle) -> None:
    assert run(claim, ROLE, "SELECT triages FROM claims.claims") == [(0,)]


def test_the_claims_that_existed_count_their_proposals(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    assert files[11][0] == "0012_claim_lifecycle.sql"
    monkeypatch.setattr(runner, "migration_files", lambda: files[:11])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    for claim_id in ("CLM-0001", "CLM-0002", "CLM-0003"):
        run(empty_database, ROLE, INSERT_CLAIM, (claim_id,))
    # CLM-0001 has no proposal, CLM-0002 one, CLM-0003 three.
    for claim_id, times in (("CLM-0002", 1), ("CLM-0003", 3)):
        for number in range(times):
            run(
                empty_database,
                ROLE,
                INSERT_PROPOSAL,
                (
                    uuid.uuid4(),
                    claim_id,
                    uuid.uuid4(),
                    Jsonb({"route": "adjuster", "reason": "r"}),
                    f"2026-09-0{number + 1}T10:00:00Z",
                ),
            )
    monkeypatch.setattr(runner, "migration_files", lambda: files[:12])

    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = runner.apply_migrations(conn)

    assert applied == ["0012_claim_lifecycle.sql"]
    assert run(
        empty_database,
        OWNER,
        "SELECT claim_id, triages FROM claims.claims ORDER BY claim_id",
    ) == [("CLM-0001", 0), ("CLM-0002", 1), ("CLM-0003", 3)]


def test_claims_api_may_update_the_triage_count(claim: DatabaseHandle) -> None:
    run(claim, ROLE, "UPDATE claims.claims SET triages = triages + 1")

    assert run(claim, ROLE, "SELECT triages FROM claims.claims") == [(1,)]


def test_the_triage_count_may_be_zero_and_a_negative_one_is_refused(
    claim: DatabaseHandle,
) -> None:
    run(claim, ROLE, "UPDATE claims.claims SET triages = 0")

    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        run(claim, ROLE, "UPDATE claims.claims SET triages = -1")

    assert caught.value.diag.constraint_name == "claims_triages_not_negative"
    assert run(claim, ROLE, "SELECT triages FROM claims.claims") == [(0,)]


def test_the_triage_count_cannot_be_null(claim: DatabaseHandle) -> None:
    with pytest.raises(psycopg.errors.NotNullViolation):
        run(claim, ROLE, "UPDATE claims.claims SET triages = NULL")


@pytest.mark.parametrize(
    "assignment",
    [
        "submission = '{}'",
        "tenant = 'other'",
        "claim_id = 'CLM-0010'",
        "received_at = now()",
    ],
)
def test_claims_api_may_still_update_nothing_else_of_a_claim(
    claim: DatabaseHandle, assignment: str
) -> None:
    state = refused(claim, ROLE, f"UPDATE claims.claims SET {assignment}")  # noqa: S608

    assert state == INSUFFICIENT_PRIVILEGE


def test_claims_api_may_still_update_the_columns_of_0009(claim: DatabaseHandle) -> None:
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


@pytest.mark.parametrize("role", ["policy_mcp", "claims_mcp"])
def test_the_tool_servers_cannot_read_the_triage_count(
    migrated_database: DatabaseHandle, role: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_column_privilege(%s, 'claims.claims', 'triages', 'SELECT')",
        (role,),
    )

    assert rows == [(False,)]


# ── claims.decisions ────────────────────────────────────────────────────────
@pytest.mark.parametrize("word", OUTCOMES)
def test_every_outcome_word_is_accepted_with_a_run(
    claim: DatabaseHandle, word: str
) -> None:
    run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, RUN_ID, word))

    assert run(claim, ROLE, "SELECT decision FROM claims.decisions") == [(word,)]


@pytest.mark.parametrize("word", ["Send_back", "withdraw", "approved", "cancelled", ""])
def test_a_word_that_is_not_one_of_the_five_is_refused(
    claim: DatabaseHandle, word: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, RUN_ID, word))

    assert caught.value.diag.constraint_name == "decisions_decision_is_an_outcome"


@pytest.mark.parametrize("word", WORDS_THAT_END_A_RUN)
def test_the_words_that_end_a_run_are_refused_without_a_run(
    claim: DatabaseHandle, word: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, None, word))

    assert (
        caught.value.diag.constraint_name
        == "decisions_the_words_that_end_a_run_name_it"
    )
    assert run(claim, ROLE, "SELECT count(*) FROM claims.decisions") == [(0,)]


@pytest.mark.parametrize("word", DECISIONS)
def test_the_three_decisions_are_accepted_without_a_run(
    claim: DatabaseHandle, word: str
) -> None:
    run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, None, word))

    assert run(claim, ROLE, "SELECT decision, run_id FROM claims.decisions") == [
        (word, None)
    ]


def test_two_decisions_without_a_run_of_one_claim_are_accepted(
    claim: DatabaseHandle,
) -> None:
    run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, None, "approve"))
    run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, None, "reject"))

    assert run(claim, ROLE, "SELECT count(*) FROM claims.decisions") == [(2,)]


def test_a_run_is_still_decided_once(claim: DatabaseHandle) -> None:
    run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, RUN_ID, "send_back"))

    with pytest.raises(psycopg.errors.UniqueViolation):
        run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, RUN_ID, "withdrawn"))
    run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, OTHER_RUN_ID, "withdrawn"))


def test_the_check_of_0009_that_the_migration_drops_is_the_one_postgres_named(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    monkeypatch.setattr(runner, "migration_files", lambda: files[:11])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)

    rows = run(
        empty_database,
        OWNER,
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'claims.decisions'::regclass AND contype = 'c'",
    )

    assert rows == [("decisions_decision_check",)]


def test_the_old_check_is_replaced_not_left_beside_the_new_one(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'claims.decisions'::regclass AND contype = 'c' "
        "ORDER BY conname",
    )

    assert rows == [
        ("decisions_decision_is_an_outcome",),
        ("decisions_the_words_that_end_a_run_name_it",),
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE claims.decisions SET decision = 'reject'",
        "DELETE FROM claims.decisions",
    ],
)
def test_claims_api_may_still_neither_change_nor_delete_a_decision(
    claim: DatabaseHandle, statement: str
) -> None:
    run(claim, ROLE, INSERT_DECISION, (CLAIM_ID, None, "approve"))

    assert refused(claim, ROLE, statement) == INSUFFICIENT_PRIVILEGE


# ── claims.claim_documents ──────────────────────────────────────────────────
def test_claims_api_records_and_reads_a_document_name(claim: DatabaseHandle) -> None:
    run(claim, ROLE, INSERT_DOCUMENT, (CLAIM_ID, "repair-estimate.pdf"))

    assert run(
        claim,
        ROLE,
        "SELECT claim_id, name, received_at IS NOT NULL FROM claims.claim_documents",
    ) == [(CLAIM_ID, "repair-estimate.pdf", True)]


def test_the_documents_table_holds_names_and_no_content(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'claims' AND table_name = 'claim_documents' "
        "ORDER BY column_name",
    )

    assert rows == [("claim_id",), ("name",), ("received_at",)]


@pytest.mark.parametrize("name", ["a", "x" * 100])
def test_a_name_of_one_to_a_hundred_characters_is_accepted(
    claim: DatabaseHandle, name: str
) -> None:
    run(claim, ROLE, INSERT_DOCUMENT, (CLAIM_ID, name))

    assert run(claim, ROLE, "SELECT name FROM claims.claim_documents") == [(name,)]


@pytest.mark.parametrize("name", ["", "x" * 101])
def test_a_name_outside_the_bounds_is_refused(claim: DatabaseHandle, name: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        run(claim, ROLE, INSERT_DOCUMENT, (CLAIM_ID, name))


def test_a_name_cannot_be_null(claim: DatabaseHandle) -> None:
    with pytest.raises(psycopg.errors.NotNullViolation):
        run(claim, ROLE, INSERT_DOCUMENT, (CLAIM_ID, None))


def test_a_claim_holds_a_name_once_and_another_claim_may_hold_it_too(
    claim: DatabaseHandle,
) -> None:
    run(claim, ROLE, INSERT_CLAIM, ("CLM-0013",))
    run(claim, ROLE, INSERT_DOCUMENT, (CLAIM_ID, "estimate.pdf"))

    with pytest.raises(psycopg.errors.UniqueViolation):
        run(claim, ROLE, INSERT_DOCUMENT, (CLAIM_ID, "estimate.pdf"))
    run(claim, ROLE, INSERT_DOCUMENT, ("CLM-0013", "estimate.pdf"))


def test_a_document_of_an_unknown_claim_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        run(fresh_database, ROLE, INSERT_DOCUMENT, ("CLM-9999", "estimate.pdf"))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE claims.claim_documents SET name = 'other.pdf'",
        "UPDATE claims.claim_documents SET received_at = now()",
        "DELETE FROM claims.claim_documents",
        "TRUNCATE claims.claim_documents",
    ],
)
def test_claims_api_may_neither_change_nor_delete_a_document(
    claim: DatabaseHandle, statement: str
) -> None:
    run(claim, ROLE, INSERT_DOCUMENT, (CLAIM_ID, "estimate.pdf"))

    assert refused(claim, ROLE, statement) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("role", OTHER_ROLES)
def test_no_other_service_role_has_any_privilege_on_documents(
    migrated_database: DatabaseHandle, role: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_table_privilege(%s, 'claims.claim_documents', "
        "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'), "
        "has_any_column_privilege(%s, 'claims.claim_documents', "
        "'SELECT, INSERT, UPDATE, REFERENCES')",
        (role, role),
    )

    assert rows == [(False, False)]


def test_nothing_on_documents_is_granted_to_public(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT count(*) FROM information_schema.role_table_grants "
        "WHERE table_schema = 'claims' AND table_name = 'claim_documents' "
        "AND grantee = 'PUBLIC'",
    )

    assert rows == [(0,)]
