"""The grants, constraints and the insert-only audit table, as each role."""

import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from psycopg import sql

from meridian.platform.common.db import connect

SCHEMAS = ("audit", "claims", "gateway", "runtime")
# The services that append to the audit log; claims_api writes no audit event.
AUDIT_WRITERS = ("agent_runtime", "model_gateway")
CLAIM_ID = "CLM-0001"
RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
THREAD_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")


def run(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> list:
    """Run one statement as ``role`` in its own transaction; return the rows."""
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def insert_claim(db: DatabaseHandle, claim_id: str = CLAIM_ID) -> None:
    run(
        db,
        "claims_api",
        "INSERT INTO claims.claims (claim_id, tenant, submission) "
        "VALUES (%s, 'development', '{}') ON CONFLICT DO NOTHING",
        (claim_id,),
    )


def insert_event(db: DatabaseHandle, role: str) -> None:
    run(
        db,
        role,
        "INSERT INTO audit.events (service, event, outcome) VALUES (%s, 'test', 'ok')",
        (role,),
    )


AUDIT_ROWS_BY = {
    "db_role": "SELECT event_id, recorded_at, db_role, service, event "
    "FROM audit.events WHERE db_role = %s",
    "event": "SELECT event_id, recorded_at, db_role, service, event "
    "FROM audit.events WHERE event = %s",
}


def audit_rows(db: DatabaseHandle, column: str, value: str) -> list:
    """Read the audit log as the owner: the services cannot read it."""
    return run(db, OWNER, AUDIT_ROWS_BY[column], (value,))


# ── claims_api ──────────────────────────────────────────────────────────────
def test_claims_api_stores_a_claim_and_a_proposal(
    migrated_database: DatabaseHandle,
) -> None:
    insert_claim(migrated_database)
    run(
        migrated_database,
        "claims_api",
        "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, route, "
        "reason, draft, drafted_by_deployment, drafted_by_provider, "
        "drafted_by_mode) "
        "VALUES (%s, %s, %s, 'adjuster', 'r', 'd', 'replay-chat', 'replay', "
        "'replay')",
        (uuid.uuid4(), CLAIM_ID, RUN_ID),
    )

    rows = run(migrated_database, "claims_api", "SELECT claim_id FROM claims.claims")

    assert (CLAIM_ID,) in rows


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM runtime.runs",
        "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, "
        "status) VALUES (gen_random_uuid(), gen_random_uuid(), 'a', 't', 'r', "
        "'Running')",
        "UPDATE audit.events SET outcome = 'x'",
        "DELETE FROM audit.events",
        "SELECT * FROM audit.events",
        "INSERT INTO audit.events (service, event, outcome) "
        "VALUES ('claims-api', 'test', 'ok')",
        "UPDATE claims.claims SET tenant = 'x'",
        "DELETE FROM claims.claims",
        "CREATE TABLE claims.extra (id int)",
    ],
)
def test_claims_api_is_refused_outside_its_grants(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, "claims_api", statement)


# ── agent_runtime ───────────────────────────────────────────────────────────
def test_agent_runtime_records_and_updates_a_run(
    migrated_database: DatabaseHandle,
) -> None:
    run(
        migrated_database,
        "agent_runtime",
        "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, "
        "status) VALUES (%s, %s, 'claims-triage', 'development', %s, 'Running')",
        (RUN_ID, THREAD_ID, CLAIM_ID),
    )
    run(
        migrated_database,
        "agent_runtime",
        "UPDATE runtime.runs SET status = 'Completed', updated_at = now() "
        "WHERE run_id = %s",
        (RUN_ID,),
    )

    rows = run(
        migrated_database,
        "agent_runtime",
        "SELECT status FROM runtime.runs WHERE run_id = %s",
        (RUN_ID,),
    )

    assert rows == [("Completed",)]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM claims.claims",
        "INSERT INTO claims.claims (claim_id, tenant, submission) "
        "VALUES ('CLM-0002', 't', '{}')",
        "DELETE FROM runtime.runs",
        "UPDATE runtime.runs SET agent = 'x'",
        "UPDATE runtime.runs SET status = 'Failed', tenant = 'x'",
        "UPDATE audit.events SET outcome = 'x'",
        "DELETE FROM audit.events",
        "SELECT * FROM audit.events",
    ],
)
def test_agent_runtime_is_refused_outside_its_grants(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, "agent_runtime", statement)


# ── model_gateway ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM claims.claims",
        "SELECT * FROM runtime.runs",
        "SELECT * FROM audit.events",
        "CREATE TABLE gateway.extra (id int)",
    ],
)
def test_model_gateway_is_refused_outside_its_grants(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, "model_gateway", statement)


def test_model_gateway_may_use_its_own_schema(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        "model_gateway",
        "SELECT has_schema_privilege('gateway', 'USAGE')",
    )

    assert rows == [(True,)]


# ── audit: insert-only for every role ───────────────────────────────────────
@pytest.mark.parametrize("role", AUDIT_WRITERS)
def test_the_runtime_and_the_gateway_append_to_the_audit_log(
    migrated_database: DatabaseHandle, role: str
) -> None:
    insert_event(migrated_database, role)

    assert audit_rows(migrated_database, "db_role", role)


@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_no_service_role_reads_the_audit_log(
    migrated_database: DatabaseHandle, role: str
) -> None:
    insert_event(migrated_database, "model_gateway")

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, role, "SELECT count(*) FROM audit.events")


def test_claims_api_cannot_even_reach_the_audit_schema(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        "claims_api",
        "SELECT has_schema_privilege('audit', 'USAGE')",
    )

    assert rows == [(False,)]


@pytest.mark.parametrize("role", SERVICE_ROLES)
@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit.events SET outcome = 'x'", "DELETE FROM audit.events"],
)
def test_no_service_role_changes_the_audit_log(
    migrated_database: DatabaseHandle, role: str, statement: str
) -> None:
    insert_event(migrated_database, "model_gateway")

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, role, statement)


def test_the_database_stamps_the_identity_of_every_audit_row(
    migrated_database: DatabaseHandle,
) -> None:
    forged_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    marker = f"forged-{uuid.uuid4()}"

    run(
        migrated_database,
        "model_gateway",
        "INSERT INTO audit.events (event_id, recorded_at, service, event, outcome) "
        "VALUES (%s, '2001-01-01T00:00:00Z', 'agent-runtime', %s, 'ok')",
        (forged_id, marker),
    )

    ((event_id, recorded_at, db_role, service, _),) = audit_rows(
        migrated_database, "event", marker
    )
    assert event_id != forged_id
    assert recorded_at.year >= 2026
    assert db_role == "model_gateway"  # the session user, whatever the row says
    assert service == "agent-runtime"  # self-asserted; db_role is the proof


def test_a_forged_db_role_is_overridden_too(
    migrated_database: DatabaseHandle,
) -> None:
    marker = f"forged-role-{uuid.uuid4()}"

    run(
        migrated_database,
        "agent_runtime",
        "INSERT INTO audit.events (db_role, service, event, outcome) "
        "VALUES ('meridian_owner', 'agent-runtime', %s, 'ok')",
        (marker,),
    )

    ((_, _, db_role, _, _),) = audit_rows(migrated_database, "event", marker)
    assert db_role == "agent_runtime"


@pytest.mark.parametrize(
    "column",
    [
        "service",
        "event",
        "outcome",
        "tenant",
        "agent",
        "reference",
        "deployment",
        "provider",
        "model",
    ],
)
def test_an_audit_text_column_is_limited_to_128_characters(
    migrated_database: DatabaseHandle, column: str
) -> None:
    values = {"service": "s", "event": "e", "outcome": "o"}

    def insert(length: int) -> None:
        row = values | {column: "x" * length}
        statement = sql.SQL("INSERT INTO audit.events ({}) VALUES ({})").format(
            sql.SQL(", ").join(map(sql.Identifier, row)),
            sql.SQL(", ").join(sql.Placeholder() * len(row)),
        )
        run(migrated_database, "model_gateway", statement, tuple(row.values()))

    insert(128)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(129)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.events SET outcome = 'changed'",
        "DELETE FROM audit.events",
        "TRUNCATE audit.events",
    ],
)
def test_the_trigger_stops_the_owner_too(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    insert_event(migrated_database, "model_gateway")

    with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
        run(migrated_database, OWNER, statement)

    rows = run(migrated_database, OWNER, "SELECT count(*) FROM audit.events")
    assert rows[0][0] >= 1


def test_the_audit_log_has_no_content_columns(
    migrated_database: DatabaseHandle,
) -> None:
    columns = {
        name
        for (name,) in run(
            migrated_database,
            OWNER,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'audit' AND table_name = 'events'",
        )
    }

    assert columns == {
        "event_id",
        "recorded_at",
        "db_role",
        "service",
        "event",
        "outcome",
        "tenant",
        "agent",
        "run_id",
        "reference",
        "deployment",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        # 0002: identifiers and labels, still no content.
        "reason",
        "data_class",
        "sku",
        "region",
        "residency",
    }


# ── PUBLIC ──────────────────────────────────────────────────────────────────
def test_public_has_no_privilege_on_the_four_schemas(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT n.nspname FROM pg_namespace n, aclexplode(n.nspacl) a "
        "WHERE a.grantee = 0 AND n.nspname = ANY(%s)",
        (list(SCHEMAS),),
    )

    assert rows == []


def test_public_has_no_privilege_on_any_table_or_function_in_them(
    migrated_database: DatabaseHandle,
) -> None:
    tables = run(
        migrated_database,
        OWNER,
        "SELECT n.nspname || '.' || c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a "
        "WHERE a.grantee = 0 AND n.nspname = ANY(%s)",
        (list(SCHEMAS),),
    )
    functions = run(
        migrated_database,
        OWNER,
        "SELECT n.nspname || '.' || p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = ANY(%s) AND (p.proacl IS NULL OR EXISTS "
        "(SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee = 0))",
        (list(SCHEMAS),),
    )

    assert tables == []
    assert functions == []


# ── constraints ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("claim_id", ["CLM-1", "clm-0001", "CLM-00001", "CLM-ABCD"])
def test_a_malformed_claim_id_is_refused(
    migrated_database: DatabaseHandle, claim_id: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_claim(migrated_database, claim_id)


def test_an_unknown_proposal_route_is_refused(
    migrated_database: DatabaseHandle,
) -> None:
    insert_claim(migrated_database)

    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            migrated_database,
            "claims_api",
            "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, "
            "route, reason, draft, drafted_by_deployment, drafted_by_provider, "
            "drafted_by_mode) "
            "VALUES (%s, %s, %s, 'approve', 'r', 'd', 'x', 'y', 'replay')",
            (uuid.uuid4(), CLAIM_ID, RUN_ID),
        )


def test_a_proposal_for_an_unknown_claim_is_refused(
    migrated_database: DatabaseHandle,
) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        run(
            migrated_database,
            "claims_api",
            "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, "
            "route, reason, draft, drafted_by_deployment, drafted_by_provider, "
            "drafted_by_mode) "
            "VALUES (%s, 'CLM-9999', %s, 'adjuster', 'r', 'd', 'x', 'y', 'replay')",
            (uuid.uuid4(), RUN_ID),
        )


def test_an_unknown_run_status_is_refused(migrated_database: DatabaseHandle) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            migrated_database,
            "agent_runtime",
            "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, "
            "status) VALUES (%s, %s, 'a', 't', 'r', 'Done')",
            (uuid.uuid4(), uuid.uuid4()),
        )


def test_a_proposal_must_say_in_which_mode_it_was_drafted(
    migrated_database: DatabaseHandle,
) -> None:
    insert_claim(migrated_database)

    with pytest.raises(psycopg.errors.NotNullViolation):
        run(
            migrated_database,
            "claims_api",
            "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, "
            "route, reason, draft, drafted_by_deployment, drafted_by_provider) "
            "VALUES (%s, %s, %s, 'adjuster', 'r', 'd', 'x', 'y')",
            (uuid.uuid4(), CLAIM_ID, RUN_ID),
        )


@pytest.mark.parametrize(
    "index",
    [
        ("audit", "events", "run_id"),
        ("claims", "triage_proposals", "claim_id"),
    ],
)
def test_the_lookup_columns_are_indexed(
    migrated_database: DatabaseHandle, index: tuple[str, str, str]
) -> None:
    schema, table, column = index

    rows = run(
        migrated_database,
        OWNER,
        "SELECT 1 FROM pg_indexes WHERE schemaname = %s AND tablename = %s "
        "AND indexdef ~ %s",
        (schema, table, rf"\({column}\)"),
    )

    assert rows
