"""0004: the policy store, the claim notes, the approval requests and the
audit ``tool`` column (S013)."""

import hashlib
import itertools
import json
import secrets
import uuid
from typing import Any

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from psycopg import sql

from meridian.platform.common.audit import AuditEvent, record_event
from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

CLAIM_ID = "CLM-0004"
RUN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000b4")
MAX_LENGTH = 128
HASH = "a" * 64
# The session database is shared and its rows stay, so each test takes numbers
# from these counters (or a fixed one it never stores). The privilege tests use
# POL-0001, HIST-0001 and CLM-0001; the boundary rows use POL-8nnn.
POLICY_NUMBERS = itertools.count(1000)
HISTORY_IDS = itertools.count(5000)
CLAIM_IDS = itertools.count(5000)

POLICY = {
    "policy_number": "POL-0001",
    "product": "HOME-STD",
    "wording_version": "2026-01",
    "start_date": "2025-08-18",
    "end_date": "2026-08-17",
    "status": "active",
    "lapsed_on": None,
    "deductible": 250,
    "sum_insured": 360000,
    "cover_limit": 360000,
}
HISTORY = {
    "history_id": "HIST-0001",
    "policy_number": "POL-0001",
    "loss_date": "2025-09-16",
    "peril": "storm",
    "paid_amount": 4430,
    "status": "closed",
}


def run(db: DatabaseHandle, role: str, statement: str, params: Any = ()) -> list:
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def insert_row(
    db: DatabaseHandle, role: str, table: str, row: dict[str, Any]
) -> list[tuple]:
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING 1").format(
        sql.SQL(table),
        sql.SQL(", ").join(map(sql.Identifier, row)),
        sql.SQL(", ").join(sql.Placeholder(name) for name in row),
    )
    return run(db, role, statement, row)


def insert_policy(db: DatabaseHandle, **values: Any) -> None:
    insert_row(db, OWNER, "policy.policies", POLICY | values)


def insert_history(db: DatabaseHandle, **values: Any) -> None:
    insert_row(db, OWNER, "policy.claim_history", HISTORY | values)


def a_policy_number(db: DatabaseHandle) -> str:
    """A policy number no other test of the session uses."""
    number = f"POL-{next(POLICY_NUMBERS):04d}"
    run(
        db,
        OWNER,
        "INSERT INTO policy.policies (policy_number, product, wording_version, "
        "start_date, end_date, status, deductible, cover_limit) "
        "VALUES (%s, 'HOME-STD', '2026-01', '2026-01-01', '2026-12-31', "
        "'active', 0, 0)",
        (number,),
    )
    return number


def a_claim(db: DatabaseHandle, policy_number: str | None = None) -> str:
    claim_id = f"CLM-{next(CLAIM_IDS):04d}"
    submission = json.dumps({"policy_number": policy_number} if policy_number else {})
    run(
        db,
        "claims_api",
        "INSERT INTO claims.claims (claim_id, tenant, submission) "
        "VALUES (%s, 'development', %s)",
        (claim_id, submission),
    )
    return claim_id


def note_row(claim_id: str, **values: Any) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "run_id": RUN_ID,
        "agent": "claims-triage",
        "note": "A note.",
        "idempotency_key": secrets.token_hex(32),
        "payload_hash": HASH,
    } | values


def approval_row(claim_id: str, **values: Any) -> dict[str, Any]:
    row = note_row(claim_id, reason="A reason.") | values
    row.pop("note", None)
    return row


def columns(db: DatabaseHandle, schema: str, table: str) -> list[tuple]:
    return run(
        db,
        OWNER,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY column_name",
        (schema, table),
    )


def test_the_ledger_holds_the_fourth_migration_file(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]

    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name, sha256 FROM public.meridian_migrations ORDER BY name",
    )

    assert names[3] == "0004_tool_servers.sql"
    assert [name for name, _ in recorded] == names
    for (name, text), (_, sha256) in zip(migration_files(), recorded, strict=True):
        assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest(), name


def test_the_generated_policy_number_is_filled_for_claims_that_already_exist(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    monkeypatch.setattr(runner, "migration_files", lambda: files[:3])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    submissions = {
        "CLM-0001": {"policy_number": "POL-0042"},
        "CLM-0002": {"policy_number": 5},  # a JSON number in its place
        "CLM-0003": {},
    }
    for claim_id, submission in submissions.items():
        run(
            empty_database,
            "claims_api",
            "INSERT INTO claims.claims (claim_id, tenant, submission) "
            "VALUES (%s, 'development', %s)",
            (claim_id, json.dumps(submission)),
        )
    # Up to 0004 only: later migrations are not what this test is about.
    monkeypatch.setattr(runner, "migration_files", lambda: files[:4])

    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = runner.apply_migrations(conn)

    assert applied == [files[3][0]]
    assert run(
        empty_database,
        OWNER,
        "SELECT claim_id, policy_number FROM claims.claims ORDER BY claim_id",
    ) == [("CLM-0001", "POL-0042"), ("CLM-0002", "5"), ("CLM-0003", None)]


@pytest.mark.parametrize("role", ["policy_mcp", "claims_mcp"])
def test_a_missing_tool_server_role_fails_clearly_and_leaves_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    files = migration_files()
    name, text = files[3]
    broken = text.replace(f"'{role}'", "'role_that_does_not_exist'", 1)
    assert broken != text
    monkeypatch.setattr(runner, "migration_files", lambda: [*files[:3], (name, broken)])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(
            psycopg.Error,
            match="required role role_that_does_not_exist does not exist",
        ):
            runner.apply_migrations(conn)

        assert conn.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname = 'policy'"
        ).fetchone() == (0,)


# ── policy.policies ─────────────────────────────────────────────────────────
def test_a_valid_policy_and_its_history_are_stored(
    migrated_database: DatabaseHandle,
) -> None:
    number = a_policy_number(migrated_database)

    insert_history(
        migrated_database,
        history_id=f"HIST-{next(HISTORY_IDS):04d}",
        policy_number=number,
    )

    assert run(
        migrated_database,
        OWNER,
        "SELECT count(*) FROM policy.claim_history WHERE policy_number = %s",
        (number,),
    ) == [(1,)]


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"policy_number": "POL-123"}, id="short-number"),
        pytest.param({"policy_number": "pol-0001"}, id="lower-case-number"),
        pytest.param({"policy_number": "POL-00001"}, id="long-number"),
        pytest.param({"product": ""}, id="empty-product"),
        pytest.param({"product": "x" * 33}, id="long-product"),
        pytest.param({"wording_version": ""}, id="empty-wording-version"),
        pytest.param({"wording_version": "x" * 17}, id="long-wording-version"),
        pytest.param({"status": "cancelled"}, id="unknown-status"),
        pytest.param({"status": "lapsed", "lapsed_on": None}, id="lapsed-no-date"),
        pytest.param({"status": "active", "lapsed_on": "2026-01-01"}, id="active-date"),
        pytest.param(
            {"start_date": "2026-08-18", "end_date": "2026-08-17"},
            id="end-before-start",
        ),
        pytest.param({"deductible": -1}, id="negative-deductible"),
        pytest.param({"sum_insured": -1}, id="negative-sum-insured"),
        pytest.param({"cover_limit": -1}, id="negative-cover-limit"),
        pytest.param({"deductible": 1_000_000_001}, id="deductible-over-cap"),
        pytest.param({"sum_insured": 1_000_000_001}, id="sum-insured-over-cap"),
        pytest.param({"cover_limit": 1_000_000_001}, id="cover-limit-over-cap"),
    ],
)
def test_a_bad_policy_row_is_refused(
    migrated_database: DatabaseHandle, values: dict[str, Any]
) -> None:
    row = POLICY | {"policy_number": "POL-9001"} | values

    with pytest.raises(psycopg.errors.CheckViolation):
        insert_row(migrated_database, OWNER, "policy.policies", row)


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(
            {"policy_number": "POL-8001", "end_date": "2025-08-18"}, id="one-day"
        ),
        pytest.param(
            {
                "policy_number": "POL-8002",
                "status": "lapsed",
                "lapsed_on": "2026-02-01",
            },
            id="lapsed-with-date",
        ),
        pytest.param(
            {"policy_number": "POL-8003", "sum_insured": None}, id="no-sum-insured"
        ),
        pytest.param(
            {
                "policy_number": "POL-8004",
                "deductible": 0,
                "cover_limit": 0,
                "sum_insured": 0,
            },
            id="zeros",
        ),
        pytest.param(
            {
                "policy_number": "POL-8005",
                "product": "x" * 32,
                "wording_version": "y" * 16,
            },
            id="at-caps",
        ),
        pytest.param(
            {
                "policy_number": "POL-8006",
                "deductible": 1_000_000_000,
                "cover_limit": 1_000_000_000,
                "sum_insured": 1_000_000_000,
            },
            id="amounts-at-the-contract-cap",
        ),
    ],
)
def test_a_boundary_policy_row_is_accepted(
    migrated_database: DatabaseHandle, values: dict[str, Any]
) -> None:
    insert_policy(migrated_database, **values)


def test_the_policy_table_has_no_personal_data_column(
    migrated_database: DatabaseHandle,
) -> None:
    assert columns(migrated_database, "policy", "policies") == [
        ("cover_limit", "integer", "NO"),
        ("deductible", "integer", "NO"),
        ("end_date", "date", "NO"),
        ("lapsed_on", "date", "YES"),
        ("policy_number", "text", "NO"),
        ("product", "text", "NO"),
        ("start_date", "date", "NO"),
        ("status", "text", "NO"),
        ("sum_insured", "integer", "YES"),
        ("wording_version", "text", "NO"),
    ]


def test_a_second_policy_with_the_same_number_is_refused(
    migrated_database: DatabaseHandle,
) -> None:
    number = a_policy_number(migrated_database)

    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_policy(migrated_database, policy_number=number)


# ── policy.claim_history ────────────────────────────────────────────────────
def test_a_history_row_for_an_unknown_policy_is_refused(
    migrated_database: DatabaseHandle,
) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert_history(
            migrated_database, history_id="HIST-9001", policy_number="POL-9999"
        )


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"history_id": "HIST-1"}, id="short-id"),
        pytest.param({"history_id": "hist-0001"}, id="lower-case-id"),
        pytest.param({"peril": ""}, id="empty-peril"),
        pytest.param({"peril": "x" * 65}, id="long-peril"),
        pytest.param({"paid_amount": -1}, id="negative-amount"),
        pytest.param({"paid_amount": 1_000_000_001}, id="amount-over-cap"),
        pytest.param({"status": ""}, id="empty-status"),
        pytest.param({"status": "x" * 33}, id="long-status"),
    ],
)
def test_a_bad_history_row_is_refused(
    migrated_database: DatabaseHandle, values: dict[str, Any]
) -> None:
    number = a_policy_number(migrated_database)
    row = HISTORY | {"history_id": "HIST-9002", "policy_number": number} | values

    with pytest.raises(psycopg.errors.CheckViolation):
        insert_row(migrated_database, OWNER, "policy.claim_history", row)


def test_a_history_row_at_the_caps_is_accepted(
    migrated_database: DatabaseHandle,
) -> None:
    number = a_policy_number(migrated_database)

    insert_history(
        migrated_database,
        history_id=f"HIST-{next(HISTORY_IDS):04d}",
        policy_number=number,
        peril="x" * 64,
        status="y" * 32,
        paid_amount=1_000_000_000,
    )


def test_the_history_is_indexed_by_policy_then_newest_loss_first(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'policy' "
        "AND tablename = 'claim_history' AND indexdef ~ %s",
        (r"\(policy_number, loss_date DESC\)",),
    )

    assert rows


# ── claims.claims.policy_number ─────────────────────────────────────────────
def test_the_claim_carries_the_policy_number_of_its_submission(
    migrated_database: DatabaseHandle,
) -> None:
    claim_id = a_claim(migrated_database, "POL-0042")

    rows = run(
        migrated_database,
        OWNER,
        "SELECT policy_number FROM claims.claims WHERE claim_id = %s",
        (claim_id,),
    )

    assert rows == [("POL-0042",)]


def test_a_claim_without_a_policy_number_in_its_submission_has_none(
    migrated_database: DatabaseHandle,
) -> None:
    claim_id = a_claim(migrated_database)

    rows = run(
        migrated_database,
        OWNER,
        "SELECT policy_number FROM claims.claims WHERE claim_id = %s",
        (claim_id,),
    )

    assert rows == [(None,)]


def test_claims_api_cannot_write_the_generated_policy_number(
    migrated_database: DatabaseHandle,
) -> None:
    with pytest.raises(psycopg.errors.GeneratedAlways):
        run(
            migrated_database,
            "claims_api",
            "INSERT INTO claims.claims (claim_id, tenant, submission, policy_number) "
            "VALUES ('CLM-7001', 'development', '{}', 'POL-0001')",
        )


# ── claims.notes and claims.approval_requests ───────────────────────────────
@pytest.mark.parametrize(
    ("table", "text_column", "maximum", "build"),
    [
        ("claims.notes", "note", 2000, note_row),
        ("claims.approval_requests", "reason", 1000, approval_row),
    ],
)
def test_the_text_of_a_note_or_a_request_is_bounded(
    migrated_database: DatabaseHandle, table: str, text_column: str, maximum: int, build
) -> None:
    claim_id = a_claim(migrated_database)

    insert_row(
        migrated_database,
        "claims_mcp",
        table,
        build(claim_id, **{text_column: "x" * maximum}),
    )
    for text in ("", "x" * (maximum + 1)):
        with pytest.raises(psycopg.errors.CheckViolation):
            insert_row(
                migrated_database,
                "claims_mcp",
                table,
                build(claim_id, **{text_column: text}),
            )


@pytest.mark.parametrize("table", ["claims.notes", "claims.approval_requests"])
@pytest.mark.parametrize("column", ["idempotency_key", "payload_hash"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("a" * 63, id="63-chars"),
        pytest.param("a" * 65, id="65-chars"),
        pytest.param("A" * 64, id="upper-case"),
        pytest.param("g" * 64, id="not-hex"),
        pytest.param("a" * 63 + "\n", id="trailing-newline"),
    ],
)
def test_a_bad_key_or_hash_is_refused(
    migrated_database: DatabaseHandle, table: str, column: str, value: str
) -> None:
    claim_id = a_claim(migrated_database)
    build = note_row if table == "claims.notes" else approval_row

    with pytest.raises(psycopg.errors.CheckViolation):
        insert_row(
            migrated_database, "claims_mcp", table, build(claim_id, **{column: value})
        )


@pytest.mark.parametrize("table", ["claims.notes", "claims.approval_requests"])
def test_a_second_row_with_the_same_run_and_idempotency_key_is_refused(
    migrated_database: DatabaseHandle, table: str
) -> None:
    claim_id = a_claim(migrated_database)
    build = note_row if table == "claims.notes" else approval_row
    row = build(claim_id)
    insert_row(migrated_database, "claims_mcp", table, row)

    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_row(migrated_database, "claims_mcp", table, row)


@pytest.mark.parametrize("table", ["claims.notes", "claims.approval_requests"])
def test_the_same_idempotency_key_from_another_run_is_another_row(
    migrated_database: DatabaseHandle, table: str
) -> None:
    claim_id = a_claim(migrated_database)
    build = note_row if table == "claims.notes" else approval_row
    row = build(claim_id)
    other_run = uuid.uuid4()
    insert_row(migrated_database, "claims_mcp", table, row)

    insert_row(migrated_database, "claims_mcp", table, row | {"run_id": other_run})

    assert run(
        migrated_database,
        OWNER,
        f"SELECT count(*) FROM {table} WHERE idempotency_key = %s",  # noqa: S608
        (row["idempotency_key"],),
    ) == [(2,)]


@pytest.mark.parametrize("table", ["claims.notes", "claims.approval_requests"])
def test_the_idempotency_constraint_is_the_run_and_key_pair_and_no_more(
    migrated_database: DatabaseHandle, table: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = %s::regclass AND contype = 'u'",
        (table,),
    )

    assert rows == [("UNIQUE (run_id, idempotency_key)",)]


@pytest.mark.parametrize("table", ["claims.notes", "claims.approval_requests"])
def test_a_note_or_a_request_for_an_unknown_claim_is_refused(
    migrated_database: DatabaseHandle, table: str
) -> None:
    build = note_row if table == "claims.notes" else approval_row

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert_row(migrated_database, "claims_mcp", table, build("CLM-9999"))


@pytest.mark.parametrize("table", ["claims.notes", "claims.approval_requests"])
def test_an_agent_name_is_bounded_to_128_characters(
    migrated_database: DatabaseHandle, table: str
) -> None:
    claim_id = a_claim(migrated_database)
    build = note_row if table == "claims.notes" else approval_row

    insert_row(migrated_database, "claims_mcp", table, build(claim_id, agent="x" * 128))
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_row(
            migrated_database, "claims_mcp", table, build(claim_id, agent="x" * 129)
        )


@pytest.mark.parametrize(
    ("table", "select"),
    [
        ("claims.notes", "SELECT note_id, created_at FROM claims.notes"),
        (
            "claims.approval_requests",
            "SELECT request_id, created_at FROM claims.approval_requests",
        ),
    ],
)
def test_a_note_or_a_request_gets_an_id_and_a_time(
    migrated_database: DatabaseHandle, table: str, select: str
) -> None:
    claim_id = a_claim(migrated_database)
    build = note_row if table == "claims.notes" else approval_row
    row = build(claim_id)
    insert_row(migrated_database, "claims_mcp", table, row)

    ((identifier, created_at),) = run(
        migrated_database,
        OWNER,
        select + " WHERE idempotency_key = %s",
        (row["idempotency_key"],),
    )

    assert isinstance(identifier, uuid.UUID)
    assert created_at.year >= 2026


def test_the_approval_request_has_no_status_column(
    migrated_database: DatabaseHandle,
) -> None:
    names = [
        row[0] for row in columns(migrated_database, "claims", "approval_requests")
    ]

    assert names == [
        "agent",
        "claim_id",
        "created_at",
        "idempotency_key",
        "payload_hash",
        "reason",
        "request_id",
        "run_id",
    ]


@pytest.mark.parametrize(
    "index", [("notes", "claim_id"), ("approval_requests", "claim_id")]
)
def test_notes_and_requests_are_indexed_by_claim(
    migrated_database: DatabaseHandle, index: tuple[str, str]
) -> None:
    table, column = index

    rows = run(
        migrated_database,
        OWNER,
        "SELECT 1 FROM pg_indexes WHERE schemaname = 'claims' AND tablename = %s "
        "AND indexdef ~ %s",
        (table, rf"\({column}\)"),
    )

    assert rows


# ── audit.events.tool ───────────────────────────────────────────────────────
def test_the_audit_tool_column_is_nullable_text(
    migrated_database: DatabaseHandle,
) -> None:
    rows = [
        row for row in columns(migrated_database, "audit", "events") if row[0] == "tool"
    ]

    assert rows == [("tool", "text", "YES")]


def test_the_audit_tool_accepts_128_characters_and_refuses_129(
    migrated_database: DatabaseHandle,
) -> None:
    def insert(length: int) -> None:
        run(
            migrated_database,
            "policy_mcp",
            "INSERT INTO audit.events (service, event, outcome, tool) "
            "VALUES ('policy-mcp', 'tool.call', 'ok', %s)",
            ("x" * length,),
        )

    insert(MAX_LENGTH)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(MAX_LENGTH + 1)


def test_an_audit_event_with_a_tool_is_stored_and_read_back(
    migrated_database: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    event = AuditEvent(
        service="policy-mcp",
        event="tool.call",
        outcome="completed",
        tenant="development",
        agent="claims-triage",
        run_id=run_id,
        tool="policy_lookup",
    )
    with connect(migrated_database.dsn("policy_mcp"), "test") as conn:
        record_event(conn, event)
        conn.commit()

    rows = run(
        migrated_database,
        OWNER,
        "SELECT service, event, outcome, tool, db_role FROM audit.events "
        "WHERE run_id = %s",
        (run_id,),
    )

    assert rows == [
        ("policy-mcp", "tool.call", "completed", "policy_lookup", "policy_mcp")
    ]


def test_an_audit_event_without_a_tool_stores_null(
    migrated_database: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    with connect(migrated_database.dsn("agent_runtime"), "test") as conn:
        record_event(
            conn,
            AuditEvent(service="agent-runtime", event="e", outcome="o", run_id=run_id),
        )
        conn.commit()

    rows = run(
        migrated_database,
        OWNER,
        "SELECT tool FROM audit.events WHERE run_id = %s",
        (run_id,),
    )

    assert rows == [(None,)]
