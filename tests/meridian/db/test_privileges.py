"""The grants, constraints and the insert-only audit table, as each role."""

import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from psycopg import sql

from meridian.platform.common.db import connect

SCHEMAS = ("audit", "claims", "gateway", "knowledge", "policy", "runtime")
# The services that append to the audit log; claims_api writes no audit event.
AUDIT_WRITERS = (
    "agent_runtime",
    "model_gateway",
    "policy_mcp",
    "claims_mcp",
    "knowledge_mcp",
)
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


# ── gateway: the budget counters and the usage ledger (0003, S011) ──────────
COUNTER = ("development", "tokens-day", "2026-10-01")
INSERT_COUNTER = (
    "INSERT INTO gateway.budget_counters (tenant, kind, period_start) "
    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING"
)
INSERT_USAGE = (
    "INSERT INTO gateway.usage (call_id, tenant, agent, run_id, deployment, "
    "provider, model, day, month, reserved_tokens, reserved_micro_eur, "
    "charged_tokens, charged_micro_eur) VALUES (%s, 'development', "
    "'claims-triage', %s, 'aoai-sdc-gpt-4o', 'azure-openai', 'gpt-4o', "
    "'2026-10-01', '2026-10-01', 100, 5, 100, 5) RETURNING attempt_id"
)
USAGE_CLOSING_UPDATES = [
    "state = 'settled'",
    "input_tokens = 60",
    "output_tokens = 40",
    "charged_tokens = 90",
    "charged_micro_eur = 4",
    "closed_at = now()",
]
USAGE_FIXED_UPDATES = [
    "tenant = 'other'",
    "agent = 'other'",
    "call_id = gen_random_uuid()",
    "run_id = gen_random_uuid()",
    "attempt_id = gen_random_uuid()",
    "deployment = 'other'",
    "day = '2026-10-02'",
    "month = '2026-11-01'",
    "reserved_tokens = 1",
    "reserved_micro_eur = 1",
    "reserved_at = now()",
]


def insert_usage(db: DatabaseHandle) -> uuid.UUID:
    ((attempt_id,),) = run(db, "model_gateway", INSERT_USAGE, (uuid.uuid4(), RUN_ID))
    return attempt_id


def test_model_gateway_selects_creates_and_updates_the_counters(
    migrated_database: DatabaseHandle,
) -> None:
    tenant = f"counter-{uuid.uuid4()}"

    run(migrated_database, "model_gateway", INSERT_COUNTER, (tenant, *COUNTER[1:]))
    run(
        migrated_database,
        "model_gateway",
        "UPDATE gateway.budget_counters SET amount = amount + 5 WHERE tenant = %s",
        (tenant,),
    )

    assert run(
        migrated_database,
        "model_gateway",
        "SELECT amount FROM gateway.budget_counters WHERE tenant = %s",
        (tenant,),
    ) == [(5,)]


@pytest.mark.parametrize("column", ["tenant", "kind", "period_start"])
def test_model_gateway_cannot_rewrite_the_key_of_a_counter(
    migrated_database: DatabaseHandle, column: str
) -> None:
    tenant = f"key-{uuid.uuid4()}"
    run(migrated_database, "model_gateway", INSERT_COUNTER, (tenant, *COUNTER[1:]))
    values = {
        "tenant": "'other'",
        "kind": "'cost-month'",
        "period_start": "'2026-10-02'",
    }

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(
            migrated_database,
            "model_gateway",
            sql.SQL(
                "UPDATE gateway.budget_counters SET {} = {} WHERE tenant = %s"
            ).format(sql.Identifier(column), sql.SQL(values[column])),
            (tenant,),
        )


def test_model_gateway_cannot_create_a_counter_with_an_amount(
    migrated_database: DatabaseHandle,
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(
            migrated_database,
            "model_gateway",
            "INSERT INTO gateway.budget_counters (tenant, kind, period_start, amount) "
            "VALUES (%s, 'tokens-day', '2026-10-01', 5)",
            (f"amount-{uuid.uuid4()}",),
        )


def test_model_gateway_inserts_and_selects_a_usage_row(
    migrated_database: DatabaseHandle,
) -> None:
    attempt_id = insert_usage(migrated_database)

    rows = run(
        migrated_database,
        "model_gateway",
        "SELECT state, reserved_tokens FROM gateway.usage WHERE attempt_id = %s",
        (attempt_id,),
    )

    assert rows == [("reserved", 100)]


@pytest.mark.parametrize(
    "column",
    [assignment.split(" = ")[0] for assignment in USAGE_CLOSING_UPDATES],
)
def test_model_gateway_holds_the_update_grant_on_each_closing_column(
    migrated_database: DatabaseHandle, column: str
) -> None:
    rows = run(
        migrated_database,
        "model_gateway",
        "SELECT has_column_privilege('gateway.usage', %s, 'UPDATE')",
        (column,),
    )

    assert rows == [(True,)]


def test_model_gateway_closes_a_reserved_row_with_the_six_columns_at_once(
    migrated_database: DatabaseHandle,
) -> None:
    attempt_id = insert_usage(migrated_database)

    run(
        migrated_database,
        "model_gateway",
        sql.SQL("UPDATE gateway.usage SET {} WHERE attempt_id = %s").format(
            sql.SQL(", ").join(sql.SQL(a) for a in USAGE_CLOSING_UPDATES)
        ),
        (attempt_id,),
    )

    assert run(
        migrated_database,
        OWNER,
        "SELECT state, charged_tokens FROM gateway.usage WHERE attempt_id = %s",
        (attempt_id,),
    ) == [("settled", 90)]


@pytest.mark.parametrize("assignment", USAGE_FIXED_UPDATES)
def test_model_gateway_cannot_update_what_identifies_or_reserves(
    migrated_database: DatabaseHandle, assignment: str
) -> None:
    attempt_id = insert_usage(migrated_database)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(
            migrated_database,
            "model_gateway",
            sql.SQL("UPDATE gateway.usage SET {} WHERE attempt_id = %s").format(
                sql.SQL(assignment)
            ),
            (attempt_id,),
        )


@pytest.mark.parametrize("table", ["budget_counters", "usage"])
def test_model_gateway_cannot_delete_from_the_ledger(
    migrated_database: DatabaseHandle, table: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(
            migrated_database,
            "model_gateway",
            sql.SQL("DELETE FROM gateway.{}").format(sql.Identifier(table)),
        )


@pytest.mark.parametrize("table", ["budget_counters", "usage"])
def test_model_gateway_cannot_truncate_the_ledger(
    migrated_database: DatabaseHandle, table: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(
            migrated_database,
            "model_gateway",
            sql.SQL("TRUNCATE gateway.{}").format(sql.Identifier(table)),
        )


@pytest.mark.parametrize("role", ["claims_api", "agent_runtime"])
@pytest.mark.parametrize("table", ["budget_counters", "usage"])
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM {table}",
        "INSERT INTO {table} DEFAULT VALUES",
        "UPDATE {table} SET tenant = 'x'",
        "DELETE FROM {table}",
    ],
)
def test_the_other_service_roles_can_do_nothing_in_the_gateway_schema(
    migrated_database: DatabaseHandle, role: str, table: str, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(
            migrated_database,
            role,
            sql.SQL(statement).format(table=sql.SQL("gateway." + table)),
        )


@pytest.mark.parametrize("role", ["claims_api", "agent_runtime"])
def test_the_other_service_roles_have_no_usage_on_the_gateway_schema(
    migrated_database: DatabaseHandle, role: str
) -> None:
    rows = run(
        migrated_database, role, "SELECT has_schema_privilege('gateway', 'USAGE')"
    )

    assert rows == [(False,)]


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
        # 0003: the call's identifier, the provider's status and model name, and
        # the count of refusals a refusal row stands for.
        "call_id",
        "http_status",
        "provider_model",
        "suppressed",
        # 0004: the tool a tool server ran or refused.
        "tool",
    }


# ── PUBLIC ──────────────────────────────────────────────────────────────────
def test_public_has_no_privilege_on_the_schemas(
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


# ── policy_mcp and claims_mcp (0004, S013) ──────────────────────────────────
TOOL_ROLES = ("policy_mcp", "claims_mcp")
POLICY_NUMBER = "POL-0001"
HISTORY_ID = "HIST-0001"
RUN_COLUMNS = "run_id, agent, tenant, reference, status"
CLAIM_COLUMNS = "claim_id, tenant, policy_number"


def seed_policy_rows(db: DatabaseHandle) -> None:
    """One policy and one history row, written by the owner (nobody else may)."""
    run(
        db,
        OWNER,
        "INSERT INTO policy.policies (policy_number, product, wording_version, "
        "start_date, end_date, status, deductible, cover_limit) "
        "VALUES (%s, 'HOME-STD', '2026-01', '2026-01-01', '2026-12-31', "
        "'active', 250, 1000) ON CONFLICT DO NOTHING",
        (POLICY_NUMBER,),
    )
    run(
        db,
        OWNER,
        "INSERT INTO policy.claim_history (history_id, policy_number, loss_date, "
        "peril, paid_amount, status) "
        "VALUES (%s, %s, '2026-02-01', 'storm', 100, 'closed') "
        "ON CONFLICT DO NOTHING",
        (HISTORY_ID, POLICY_NUMBER),
    )


def insert_tool_run(db: DatabaseHandle) -> uuid.UUID:
    run_id = uuid.uuid4()
    run(
        db,
        "agent_runtime",
        "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, "
        "status) VALUES (%s, %s, 'claims-triage', 'development', %s, 'Running')",
        (run_id, uuid.uuid4(), CLAIM_ID),
    )
    return run_id


def insert_note(db: DatabaseHandle, role: str = "claims_mcp") -> str:
    """A note on CLM-0001; returns its idempotency key."""
    insert_claim(db)
    key = uuid.uuid4().hex + uuid.uuid4().hex
    run(
        db,
        role,
        "INSERT INTO claims.notes (claim_id, run_id, agent, note, idempotency_key, "
        "payload_hash) VALUES (%s, %s, 'claims-triage', 'n', %s, %s)",
        (CLAIM_ID, RUN_ID, key, "b" * 64),
    )
    return key


def insert_approval_request(db: DatabaseHandle) -> str:
    insert_claim(db)
    key = uuid.uuid4().hex + uuid.uuid4().hex
    run(
        db,
        "claims_mcp",
        "INSERT INTO claims.approval_requests (claim_id, run_id, agent, reason, "
        "idempotency_key, payload_hash) "
        "VALUES (%s, %s, 'claims-triage', 'r', %s, %s)",
        (CLAIM_ID, RUN_ID, key, "b" * 64),
    )
    return key


def test_policy_mcp_reads_the_policy_and_its_history(
    migrated_database: DatabaseHandle,
) -> None:
    seed_policy_rows(migrated_database)

    policies = run(
        migrated_database,
        "policy_mcp",
        "SELECT policy_number, cover_limit FROM policy.policies",
    )
    history = run(
        migrated_database,
        "policy_mcp",
        "SELECT history_id, paid_amount FROM policy.claim_history",
    )

    assert (POLICY_NUMBER, 1000) in policies
    assert (HISTORY_ID, 100) in history


@pytest.mark.parametrize("table", ["policies", "claim_history"])
def test_policy_mcp_may_select_everything_in_the_policy_tables(
    migrated_database: DatabaseHandle, table: str
) -> None:
    rows = run(migrated_database, "policy_mcp", f"SELECT * FROM policy.{table}")  # noqa: S608

    assert isinstance(rows, list)


def test_policy_mcp_has_usage_on_the_policy_schema(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        "policy_mcp",
        "SELECT has_schema_privilege('policy', 'USAGE')",
    )

    assert rows == [(True,)]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO policy.policies (policy_number, product, wording_version, "
        "start_date, end_date, status, deductible, cover_limit) "
        "VALUES ('POL-7001', 'p', 'v', '2026-01-01', '2026-12-31', 'active', 0, 0)",
        "UPDATE policy.policies SET deductible = 0",
        "DELETE FROM policy.policies",
        "INSERT INTO policy.claim_history (history_id, policy_number, loss_date, "
        "peril, paid_amount, status) "
        "VALUES ('HIST-7001', 'POL-0001', '2026-01-01', 'p', 0, 's')",
        "UPDATE policy.claim_history SET paid_amount = 0",
        "DELETE FROM policy.claim_history",
        "TRUNCATE policy.policies, policy.claim_history",
    ],
)
def test_policy_mcp_cannot_write_the_policy_tables(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    seed_policy_rows(migrated_database)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, "policy_mcp", statement)


@pytest.mark.parametrize("role", TOOL_ROLES)
def test_a_tool_server_reads_the_five_run_columns(
    migrated_database: DatabaseHandle, role: str
) -> None:
    run_id = insert_tool_run(migrated_database)

    rows = run(
        migrated_database,
        role,
        f"SELECT {RUN_COLUMNS} FROM runtime.runs WHERE run_id = %s",  # noqa: S608
        (run_id,),
    )

    assert rows == [(run_id, "claims-triage", "development", CLAIM_ID, "Running")]


@pytest.mark.parametrize("role", TOOL_ROLES)
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT thread_id FROM runtime.runs",
        "SELECT * FROM runtime.runs",
        "SELECT run_id FROM runtime.runs WHERE thread_id IS NOT NULL",
        "SELECT created_at FROM runtime.runs",
        "SELECT updated_at FROM runtime.runs",
        "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, "
        "status) VALUES (gen_random_uuid(), gen_random_uuid(), 'a', 't', 'r', "
        "'Running')",
        "UPDATE runtime.runs SET status = 'Failed'",
        "DELETE FROM runtime.runs",
    ],
)
def test_a_tool_server_cannot_go_beyond_its_run_columns(
    migrated_database: DatabaseHandle, role: str, statement: str
) -> None:
    insert_tool_run(migrated_database)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, role, statement)


@pytest.mark.parametrize("role", TOOL_ROLES)
def test_a_tool_server_reads_the_three_claim_columns(
    migrated_database: DatabaseHandle, role: str
) -> None:
    insert_claim(migrated_database)

    rows = run(
        migrated_database,
        role,
        f"SELECT {CLAIM_COLUMNS} FROM claims.claims WHERE claim_id = %s",  # noqa: S608
        (CLAIM_ID,),
    )

    assert rows == [(CLAIM_ID, "development", None)]


@pytest.mark.parametrize("role", TOOL_ROLES)
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT submission FROM claims.claims",
        "SELECT * FROM claims.claims",
        "SELECT received_at FROM claims.claims",
        "INSERT INTO claims.claims (claim_id, tenant, submission) "
        "VALUES ('CLM-7002', 't', '{}')",
        "UPDATE claims.claims SET tenant = 'x'",
        "DELETE FROM claims.claims",
        "SELECT * FROM claims.triage_proposals",
        "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, route, "
        "reason, draft, drafted_by_deployment, drafted_by_provider, drafted_by_mode) "
        "VALUES (gen_random_uuid(), 'CLM-0001', gen_random_uuid(), 'adjuster', 'r', "
        "'d', 'x', 'y', 'replay')",
    ],
)
def test_a_tool_server_cannot_go_beyond_its_claim_columns(
    migrated_database: DatabaseHandle, role: str, statement: str
) -> None:
    insert_claim(migrated_database)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, role, statement)


def test_claims_mcp_inserts_a_note_and_reads_its_replay_columns(
    migrated_database: DatabaseHandle,
) -> None:
    key = insert_note(migrated_database)

    rows = run(
        migrated_database,
        "claims_mcp",
        "SELECT claim_id, run_id, payload_hash FROM claims.notes "
        "WHERE run_id = %s AND idempotency_key = %s",
        (RUN_ID, key),
    )

    assert rows == [(CLAIM_ID, RUN_ID, "b" * 64)]


def test_claims_mcp_inserts_a_request_and_reads_its_replay_columns(
    migrated_database: DatabaseHandle,
) -> None:
    key = insert_approval_request(migrated_database)

    rows = run(
        migrated_database,
        "claims_mcp",
        "SELECT claim_id, run_id, payload_hash FROM claims.approval_requests "
        "WHERE run_id = %s AND idempotency_key = %s",
        (RUN_ID, key),
    )

    assert rows == [(CLAIM_ID, RUN_ID, "b" * 64)]


@pytest.mark.parametrize(
    ("table", "id_column"),
    [("notes", "note_id"), ("approval_requests", "request_id")],
)
def test_claims_mcp_reads_exactly_the_five_replay_columns(
    migrated_database: DatabaseHandle, table: str, id_column: str
) -> None:
    insert_note(migrated_database)
    insert_approval_request(migrated_database)
    columns = f"{id_column}, claim_id, run_id, idempotency_key, payload_hash"

    rows = run(
        migrated_database,
        "claims_mcp",
        f"SELECT {columns} FROM claims.{table}",  # noqa: S608
    )

    assert rows


@pytest.mark.parametrize("table", ["notes", "approval_requests"])
@pytest.mark.parametrize(
    "select",
    [
        "SELECT {text} FROM claims.{table}",
        "SELECT * FROM claims.{table}",
        "SELECT agent FROM claims.{table}",
        "SELECT created_at FROM claims.{table}",
        # a column in a WHERE clause is read too: an oracle on the text
        "SELECT claim_id FROM claims.{table} WHERE {text} = 'n'",
        "SELECT claim_id FROM claims.{table} ORDER BY {text}",
    ],
)
def test_claims_mcp_cannot_read_the_text_or_the_rest_of_a_note_or_a_request(
    migrated_database: DatabaseHandle, table: str, select: str
) -> None:
    insert_note(migrated_database)
    insert_approval_request(migrated_database)
    text = "note" if table == "notes" else "reason"

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, "claims_mcp", select.format(table=table, text=text))


@pytest.mark.parametrize(
    ("table", "text_column"), [("notes", "note"), ("approval_requests", "reason")]
)
def test_the_owner_reads_the_text_the_tool_server_stored(
    migrated_database: DatabaseHandle, table: str, text_column: str
) -> None:
    key = (
        insert_note(migrated_database)
        if table == "notes"
        else insert_approval_request(migrated_database)
    )

    rows = run(
        migrated_database,
        OWNER,
        f"SELECT claim_id, {text_column} FROM claims.{table} "  # noqa: S608
        "WHERE idempotency_key = %s",
        (key,),
    )

    assert rows == [(CLAIM_ID, "n" if table == "notes" else "r")]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE claims.notes SET note = 'changed'",
        "DELETE FROM claims.notes",
        "TRUNCATE claims.notes",
        "UPDATE claims.approval_requests SET reason = 'changed'",
        "DELETE FROM claims.approval_requests",
        "TRUNCATE claims.approval_requests",
    ],
)
def test_claims_mcp_cannot_change_or_remove_a_note_or_a_request(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    insert_note(migrated_database)
    insert_approval_request(migrated_database)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, "claims_mcp", statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM claims.notes",
        "INSERT INTO claims.notes (claim_id, run_id, agent, note, idempotency_key, "
        "payload_hash) VALUES ('CLM-0001', gen_random_uuid(), 'a', 'n', "
        "repeat('c', 64), repeat('c', 64))",
        "UPDATE claims.notes SET note = 'x'",
        "DELETE FROM claims.notes",
        "SELECT * FROM claims.approval_requests",
        "INSERT INTO claims.approval_requests (claim_id, run_id, agent, reason, "
        "idempotency_key, payload_hash) VALUES ('CLM-0001', gen_random_uuid(), "
        "'a', 'r', repeat('c', 64), repeat('c', 64))",
    ],
)
def test_policy_mcp_cannot_read_or_write_notes_or_requests(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    insert_note(migrated_database)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, "policy_mcp", statement)


@pytest.mark.parametrize("table", ["notes", "approval_requests"])
def test_claims_api_cannot_read_notes_or_requests(
    migrated_database: DatabaseHandle, table: str
) -> None:
    insert_note(migrated_database)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, "claims_api", f"SELECT * FROM claims.{table}")  # noqa: S608


@pytest.mark.parametrize("role", TOOL_ROLES)
def test_a_tool_server_appends_to_the_audit_log(
    migrated_database: DatabaseHandle, role: str
) -> None:
    insert_event(migrated_database, role)

    assert audit_rows(migrated_database, "db_role", role)


@pytest.mark.parametrize("role", TOOL_ROLES)
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count(*) FROM audit.events",
        "UPDATE audit.events SET outcome = 'x'",
        "DELETE FROM audit.events",
    ],
)
def test_a_tool_server_cannot_read_or_change_the_audit_log(
    migrated_database: DatabaseHandle, role: str, statement: str
) -> None:
    insert_event(migrated_database, role)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, role, statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM policy.policies",
        "SELECT * FROM policy.claim_history",
        "INSERT INTO policy.policies (policy_number) VALUES ('POL-7003')",
    ],
)
def test_claims_mcp_cannot_reach_the_policy_tables(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, "claims_mcp", statement)


def test_claims_mcp_has_no_usage_on_the_policy_schema(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        "claims_mcp",
        "SELECT has_schema_privilege('policy', 'USAGE')",
    )

    assert rows == [(False,)]


@pytest.mark.parametrize("role", ["claims_api", "agent_runtime", "model_gateway"])
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM policy.policies",
        "SELECT * FROM policy.claim_history",
        "INSERT INTO policy.policies (policy_number) VALUES ('POL-7004')",
        "UPDATE policy.policies SET deductible = 0",
        "DELETE FROM policy.claim_history",
    ],
)
def test_the_older_roles_cannot_read_or_write_the_policy_tables(
    migrated_database: DatabaseHandle, role: str, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, role, statement)


@pytest.mark.parametrize("role", ["claims_api", "agent_runtime", "model_gateway"])
def test_the_older_roles_have_no_usage_on_the_policy_schema(
    migrated_database: DatabaseHandle, role: str
) -> None:
    rows = run(
        migrated_database, role, "SELECT has_schema_privilege('policy', 'USAGE')"
    )

    assert rows == [(False,)]


@pytest.mark.parametrize("role", ["agent_runtime", "model_gateway"])
@pytest.mark.parametrize("table", ["notes", "approval_requests"])
def test_the_older_runtime_roles_cannot_touch_notes_or_requests(
    migrated_database: DatabaseHandle, role: str, table: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, role, f"SELECT * FROM claims.{table}")  # noqa: S608
