"""0011: the adjuster's queue and a claim's audit trail (S016, T-71)."""

import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import migration_files

ROLE = "claims_api"
OTHER_ROLES = tuple(role for role in SERVICE_ROLES if role != ROLE)
TENANT = "development"
OTHER_TENANT = "other-tenant"
AGENT = "claims-triage"
CLAIM_A = "CLM-0101"
CLAIM_B = "CLM-0102"
CLAIM_OF_OTHER_TENANT = "CLM-0103"
INSUFFICIENT_PRIVILEGE = "42501"
OBJECT_NOT_IN_PREREQUISITE_STATE = "55000"
TRAIL_COLUMNS = (
    "claim_id",
    "tenant",
    "recorded_at",
    "db_role",
    "service",
    "event",
    "outcome",
    "reason",  # appended by 0014
    "seq",  # appended by 0017
)
INSERT_CLAIM = (
    "INSERT INTO claims.claims (claim_id, tenant, submission) VALUES (%s, %s, '{}')"
)
INSERT_RUN = (
    "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, status) "
    "VALUES (%s, %s, %s, %s, %s, 'Completed')"
)
INSERT_EVENT = (
    "INSERT INTO audit.events (service, event, outcome, tenant, run_id, reference) "
    "VALUES (%s, %s, 'ok', %s, %s, %s)"
)
SELECT_TRAIL = (
    "SELECT claim_id, tenant, db_role, service, event "
    "FROM audit.claim_trail ORDER BY claim_id, event"
)
INDEX_COLUMNS = (
    "SELECT a.attname FROM pg_index i "
    "JOIN pg_class ic ON ic.oid = i.indexrelid "
    "JOIN pg_class t ON t.oid = i.indrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, position) "
    "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
    "WHERE n.nspname = %s AND t.relname = %s AND ic.relname = %s "
    "ORDER BY k.position"
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


def start_run(db: DatabaseHandle, agent: str, tenant: str, reference: str) -> uuid.UUID:
    """A run the runtime records for ``reference``; returns its ID."""
    run_id = uuid.uuid4()
    run(
        db,
        "agent_runtime",
        INSERT_RUN,
        (run_id, uuid.uuid4(), agent, tenant, reference),
    )
    return run_id


def write_event(
    db: DatabaseHandle,
    role: str,
    event: str,
    *,
    tenant: str | None = None,
    run_id: uuid.UUID | None = None,
    reference: str | None = None,
) -> None:
    """One audit row as ``role``; the database stamps ``db_role`` itself."""
    run(db, role, INSERT_EVENT, (role, event, tenant, run_id, reference))


def seed_claim(db: DatabaseHandle, claim_id: str, tenant: str) -> None:
    """A claim, its triage run and the rows each service writes about it.

    The event names say which row is which, so a test can tell them apart. The
    Claims API's rows carry the run too, as ``move_claim`` writes them, so the
    trail must show them once although both of its conditions match.
    """
    run(db, ROLE, INSERT_CLAIM, (claim_id, tenant))
    run_id = start_run(db, AGENT, tenant, claim_id)
    write_event(
        db,
        ROLE,
        f"{claim_id}:claims-api",
        tenant=tenant,
        run_id=run_id,
        reference=claim_id,
    )
    write_event(db, "agent_runtime", f"{claim_id}:runtime", run_id=run_id)
    write_event(db, "claims_mcp", f"{claim_id}:tool", run_id=run_id)
    write_event(db, "model_gateway", f"{claim_id}:gateway", run_id=run_id)


def seed_rows_that_are_not_a_trail(db: DatabaseHandle) -> None:
    """Rows that name CLAIM_A and must stay out of its trail."""
    # Another role carrying the claim's reference, with no run of the claim.
    write_event(db, "model_gateway", "foreign-role", tenant=TENANT, reference=CLAIM_A)
    # Another agent's run on the same reference.
    other_agent = start_run(db, "other-agent", TENANT, CLAIM_A)
    write_event(db, "agent_runtime", "other-agent-run", run_id=other_agent)
    # The Claims API naming the claim under another tenant.
    write_event(
        db, ROLE, "claims-api-other-tenant", tenant=OTHER_TENANT, reference=CLAIM_A
    )
    # A run of the claim's agent and reference under another tenant.
    other_tenant_run = start_run(db, AGENT, OTHER_TENANT, CLAIM_A)
    write_event(db, "agent_runtime", "run-of-other-tenant", run_id=other_tenant_run)
    # An event of no claim at all.
    write_event(db, "agent_runtime", "unrelated", run_id=uuid.uuid4())


@pytest.fixture
def trails(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A migrated database of its own that holds three claims and their rows."""
    seed_claim(fresh_database, CLAIM_A, TENANT)
    seed_claim(fresh_database, CLAIM_B, TENANT)
    seed_claim(fresh_database, CLAIM_OF_OTHER_TENANT, OTHER_TENANT)
    seed_rows_that_are_not_a_trail(fresh_database)
    return fresh_database


def expected_trail(claim_id: str, tenant: str) -> list[tuple]:
    return [
        (claim_id, tenant, "claims_api", "claims_api", f"{claim_id}:claims-api"),
        (claim_id, tenant, "model_gateway", "model_gateway", f"{claim_id}:gateway"),
        (claim_id, tenant, "agent_runtime", "agent_runtime", f"{claim_id}:runtime"),
        (claim_id, tenant, "claims_mcp", "claims_mcp", f"{claim_id}:tool"),
    ]


def test_the_migration_is_the_eleventh_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert names[10] == "0011_adjuster_queue.sql"
    assert ("0011_adjuster_queue.sql",) in recorded


@pytest.mark.parametrize(
    ("schema", "table", "index", "columns"),
    [
        (
            "claims",
            "claims",
            "claims_queue_idx",
            ["tenant", "state_changed_at", "claim_id"],
        ),
        ("audit", "events", "events_reference_idx", ["reference"]),
        ("runtime", "runs", "runs_reference_idx", ["reference"]),
    ],
)
def test_the_indexes_exist_on_those_columns_in_that_order(
    migrated_database: DatabaseHandle,
    schema: str,
    table: str,
    index: str,
    columns: list[str],
) -> None:
    rows = run(migrated_database, OWNER, INDEX_COLUMNS, (schema, table, index))

    assert [name for (name,) in rows] == columns


@pytest.mark.parametrize(
    ("index", "predicate"),
    [
        (
            "claims.claims_queue_idx",
            "WHERE (state = ANY (ARRAY['awaiting_adjuster'::text, "
            "'triage_failed'::text]))",
        ),
        (
            "audit.events_reference_idx",
            # Widened by 0014 to the Claims API's and the sweep's rows.
            "WHERE (db_role = ANY (ARRAY['claims_api'::name, 'claims_sweep'::name]))",
        ),
        ("runtime.runs_reference_idx", None),
    ],
)
def test_the_partial_indexes_carry_their_predicates(
    migrated_database: DatabaseHandle, index: str, predicate: str | None
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT pg_get_indexdef(%s::regclass)",
        (index,),
    )

    definition = rows[0][0]
    _, found, where = definition.partition(" WHERE ")
    assert (f"WHERE {where}" if found else None) == predicate


def test_the_view_has_exactly_the_nine_columns_in_order(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'audit' AND table_name = 'claim_trail' "
        "ORDER BY ordinal_position",
    )

    assert tuple(name for (name,) in rows) == TRAIL_COLUMNS


def test_the_view_is_a_security_barrier(migrated_database: DatabaseHandle) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT reloptions FROM pg_class WHERE oid = 'audit.claim_trail'::regclass",
    )

    assert rows == [(["security_barrier=true"],)]


def test_claims_api_selects_the_view_and_not_the_log(
    trails: DatabaseHandle,
) -> None:
    assert run(trails, ROLE, "SELECT count(*) FROM audit.claim_trail")[0][0] > 0
    assert refused(trails, ROLE, "SELECT * FROM audit.events") == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "column",
    ["event_id", "run_id", "deployment", "provider", "model", "input_tokens"],
)
def test_the_view_does_not_carry_the_logs_other_columns(
    trails: DatabaseHandle, column: str
) -> None:
    with pytest.raises(psycopg.errors.UndefinedColumn):
        run(trails, ROLE, f"SELECT {column} FROM audit.claim_trail")  # noqa: S608


def test_claims_api_holds_select_on_the_view_and_nothing_else(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_table_privilege(%s, 'audit.claim_trail', 'SELECT'), "
        "has_table_privilege(%s, 'audit.claim_trail', "
        "'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'), "
        "has_table_privilege(%s, 'audit.events', 'SELECT')",
        (ROLE, ROLE, ROLE),
    )

    assert rows == [(True, False, False)]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO audit.claim_trail (claim_id, tenant, recorded_at, db_role, "
        "service, event, outcome) "
        "VALUES ('CLM-0101', 'development', now(), 'claims_api', 's', 'e', 'o')",
        "UPDATE audit.claim_trail SET outcome = 'changed'",
        "DELETE FROM audit.claim_trail",
    ],
)
def test_claims_api_cannot_write_through_the_view(
    trails: DatabaseHandle, statement: str
) -> None:
    before = run(trails, OWNER, "SELECT count(*) FROM audit.events")

    # The view joins two tables, so PostgreSQL refuses a write to it as "cannot
    # insert into view" before it checks a privilege. The privilege test above
    # holds the grant to SELECT only; this one holds that nothing gets through.
    sqlstate = refused(trails, ROLE, statement)

    assert sqlstate in {INSUFFICIENT_PRIVILEGE, OBJECT_NOT_IN_PREREQUISITE_STATE}
    assert run(trails, OWNER, "SELECT count(*) FROM audit.events") == before


@pytest.mark.parametrize("role", OTHER_ROLES)
def test_no_other_service_role_reads_the_view(
    migrated_database: DatabaseHandle, role: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_table_privilege(%s, 'audit.claim_trail', 'SELECT')",
        (role,),
    )

    assert rows == [(False,)]


@pytest.mark.parametrize(
    ("claim_id", "tenant"),
    [
        (CLAIM_A, TENANT),
        (CLAIM_B, TENANT),
        (CLAIM_OF_OTHER_TENANT, OTHER_TENANT),
    ],
)
def test_the_trail_of_a_claim_is_its_own_rows_once_each(
    trails: DatabaseHandle, claim_id: str, tenant: str
) -> None:
    rows = run(
        trails,
        ROLE,
        "SELECT claim_id, tenant, db_role, service, event FROM audit.claim_trail "
        "WHERE claim_id = %s AND tenant = %s ORDER BY event",
        (claim_id, tenant),
    )

    assert rows == sorted(expected_trail(claim_id, tenant), key=lambda row: row[4])


def test_the_view_holds_those_rows_and_no_others(trails: DatabaseHandle) -> None:
    rows = run(trails, ROLE, SELECT_TRAIL)

    expected = [
        row
        for claim_id, tenant in (
            (CLAIM_A, TENANT),
            (CLAIM_B, TENANT),
            (CLAIM_OF_OTHER_TENANT, OTHER_TENANT),
        )
        for row in expected_trail(claim_id, tenant)
    ]
    assert rows == sorted(expected, key=lambda row: (row[0], row[4]))


@pytest.mark.parametrize(
    "left_out",
    [
        "foreign-role",
        "other-agent-run",
        "claims-api-other-tenant",
        "run-of-other-tenant",
        "unrelated",
    ],
)
def test_a_row_that_only_names_the_claim_is_not_in_its_trail(
    trails: DatabaseHandle, left_out: str
) -> None:
    rows = run(
        trails,
        ROLE,
        "SELECT event FROM audit.claim_trail WHERE event = %s",
        (left_out,),
    )

    assert rows == []


def test_the_rows_that_are_left_out_are_in_the_log(trails: DatabaseHandle) -> None:
    # Guards the test above: the rows it looks for exist, so an empty answer is
    # the view's and not a seed that never ran.
    rows = run(
        trails,
        OWNER,
        "SELECT event FROM audit.events WHERE event = ANY(%s) ORDER BY event",
        (
            [
                "foreign-role",
                "other-agent-run",
                "claims-api-other-tenant",
                "run-of-other-tenant",
                "unrelated",
            ],
        ),
    )

    assert len(rows) == 5


def test_an_event_that_matches_by_reference_and_by_run_appears_once(
    trails: DatabaseHandle,
) -> None:
    rows = run(
        trails,
        ROLE,
        "SELECT count(*) FROM audit.claim_trail WHERE event = %s",
        (f"{CLAIM_A}:claims-api",),
    )

    assert rows == [(1,)]


def test_a_second_run_of_the_claim_adds_its_rows_to_the_trail(
    trails: DatabaseHandle,
) -> None:
    retried = start_run(trails, AGENT, TENANT, CLAIM_A)
    write_event(trails, "agent_runtime", "retried-run", run_id=retried)

    rows = run(
        trails,
        ROLE,
        "SELECT claim_id FROM audit.claim_trail WHERE event = 'retried-run'",
    )

    assert rows == [(CLAIM_A,)]


@pytest.mark.parametrize(
    ("tenant", "reference"),
    [(None, CLAIM_A), (TENANT, None), (None, None)],
    ids=["null-tenant", "null-reference", "null-both"],
)
def test_a_claims_api_event_of_the_run_with_a_null_is_in_the_trail_once(
    trails: DatabaseHandle, tenant: str | None, reference: str | None
) -> None:
    # The first branch needs the claim's reference and tenant, so a NULL in
    # either leaves this row to the run branch; it must not drop out of both.
    run_of_claim = start_run(trails, AGENT, TENANT, CLAIM_A)
    write_event(
        trails,
        ROLE,
        "claims-api-null",
        tenant=tenant,
        run_id=run_of_claim,
        reference=reference,
    )

    rows = run(
        trails,
        ROLE,
        "SELECT claim_id, db_role FROM audit.claim_trail WHERE event = %s",
        ("claims-api-null",),
    )

    assert rows == [(CLAIM_A, ROLE)]


def test_a_claims_api_event_with_the_runs_id_and_the_claims_names_is_once(
    trails: DatabaseHandle,
) -> None:
    # Both branches match this row; the second leaves out what the first holds.
    run_of_claim = start_run(trails, AGENT, TENANT, CLAIM_A)
    write_event(
        trails,
        ROLE,
        "claims-api-both",
        tenant=TENANT,
        run_id=run_of_claim,
        reference=CLAIM_A,
    )

    rows = run(
        trails,
        ROLE,
        "SELECT claim_id FROM audit.claim_trail WHERE event = %s",
        ("claims-api-both",),
    )

    assert rows == [(CLAIM_A,)]


@pytest.mark.parametrize("role", OTHER_ROLES)
def test_a_row_another_role_wrote_with_the_runs_id_is_in_the_trail(
    trails: DatabaseHandle, role: str
) -> None:
    # Documents what the run branch trusts: any role with INSERT on audit.events
    # that writes the run's ID (T-25). db_role says which role it was.
    run_of_claim = start_run(trails, AGENT, TENANT, CLAIM_A)
    write_event(trails, role, "another-role-with-run", run_id=run_of_claim)

    rows = run(
        trails,
        ROLE,
        "SELECT claim_id, db_role FROM audit.claim_trail WHERE event = %s",
        ("another-role-with-run",),
    )

    assert rows == [(CLAIM_A, role)]


def test_a_claim_with_no_run_and_no_events_has_an_empty_trail(
    trails: DatabaseHandle,
) -> None:
    run(trails, ROLE, INSERT_CLAIM, ("CLM-0199", TENANT))

    rows = run(
        trails,
        ROLE,
        "SELECT event FROM audit.claim_trail WHERE claim_id = %s AND tenant = %s",
        ("CLM-0199", TENANT),
    )

    assert rows == []


# A function of the caller's own session: it reports each value it is asked
# about, so a test sees which rows the planner showed it before the view's own
# conditions had kept or dropped them.
CREATE_LEAK = (
    "CREATE FUNCTION pg_temp.leak(text) RETURNS boolean LANGUAGE plpgsql "
    "COST 0.0001 AS $$ BEGIN RAISE NOTICE 'leaked:%', $1; RETURN true; END $$"
)
LEAK_PROBE = (
    "SELECT event FROM audit.claim_trail WHERE claim_id = %s AND pg_temp.leak(event)"
)
LEAK_PREFIX = "leaked:"


def leaked_events(db: DatabaseHandle, claim_id: str) -> list[str]:
    """The events a caller's own function was shown by a query of the view.

    Skips the test where claims_api may not create a function in its pg_temp.
    """
    notices: list[str] = []
    with connect(db.dsn(ROLE), "test") as conn:
        conn.add_notice_handler(lambda diag: notices.append(diag.message_primary or ""))
        try:
            conn.execute(CREATE_LEAK)
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("claims_api cannot create a function in pg_temp here")
        conn.execute(LEAK_PROBE, (claim_id,)).fetchall()
    return [n.removeprefix(LEAK_PREFIX) for n in notices if n.startswith(LEAK_PREFIX)]


def test_a_function_of_the_caller_sees_only_the_trail_not_the_log(
    trails: DatabaseHandle,
) -> None:
    seen = leaked_events(trails, CLAIM_A)

    # Only the claim's own rows reach the function; none of the rows that name
    # the claim without belonging to it, nor any other claim's.
    assert sorted(seen) == sorted(row[4] for row in expected_trail(CLAIM_A, TENANT))


def test_the_probe_does_see_the_log_once_the_barrier_is_off(
    trails: DatabaseHandle,
) -> None:
    # Guards the test above: with the barrier removed the same probe is shown
    # rows outside the trail, so the test above can fail.
    run(trails, OWNER, "ALTER VIEW audit.claim_trail SET (security_barrier = false)")

    seen = leaked_events(trails, CLAIM_A)

    assert set(seen) - {row[4] for row in expected_trail(CLAIM_A, TENANT)}


SEEDED_CLAIMS = 3000
# Claim n is CLM-nnnn; even n belong to TENANT, odd n to OTHER_TENANT. A third
# of the claims wait for an adjuster, a third failed triage, the rest are done.
SEED_CLAIMS = (
    "INSERT INTO claims.claims (claim_id, tenant, submission, state) "
    "SELECT 'CLM-' || lpad(n::text, 4, '0'), "
    "CASE WHEN mod(n, 2) = 0 THEN %s ELSE %s END, '{}', "
    "CASE mod(n, 3) WHEN 0 THEN 'awaiting_adjuster' WHEN 1 THEN 'triage_failed' "
    "ELSE 'approved' END "
    "FROM generate_series(1, %s) AS n"
)
SEED_RUNS = (
    "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, status) "
    "SELECT gen_random_uuid(), gen_random_uuid(), %s, tenant, claim_id, 'Completed' "
    "FROM claims.claims"
)
SEED_RUN_EVENTS = (
    "INSERT INTO audit.events (service, event, outcome, tenant, run_id) "
    "SELECT 'agent_runtime', 'step-' || k, 'ok', NULL, run_id "
    "FROM runtime.runs CROSS JOIN generate_series(1, 3) AS k"
)
SEED_API_EVENTS = (
    "INSERT INTO audit.events (service, event, outcome, tenant, reference) "
    "SELECT 'claims_api', 'moved', 'ok', "
    "CASE WHEN mod(n, 2) = 0 THEN %s ELSE %s END, 'CLM-' || lpad(n::text, 4, '0') "
    "FROM generate_series(1, %s) AS n"
)
TRAIL_QUERY = (
    "SELECT * FROM audit.claim_trail WHERE claim_id = %s AND tenant = %s "
    "ORDER BY recorded_at, seq LIMIT 200"
)
QUEUE_QUERY = (
    "SELECT claim_id FROM claims.claims WHERE tenant = %s "
    "AND state IN ('awaiting_adjuster', 'triage_failed') "
    "ORDER BY state_changed_at, claim_id LIMIT 100"
)


@pytest.fixture
def many_claims(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A few thousand claims, their runs and events, analysed."""
    db = fresh_database
    run(db, OWNER, SEED_CLAIMS, (TENANT, OTHER_TENANT, SEEDED_CLAIMS))
    run(db, OWNER, SEED_RUNS, (AGENT,))
    run(db, OWNER, SEED_RUN_EVENTS)
    run(db, ROLE, SEED_API_EVENTS, (TENANT, OTHER_TENANT, SEEDED_CLAIMS))
    run(db, OWNER, "ANALYZE")
    return db


def plan_of(db: DatabaseHandle, query: str, params: tuple) -> str:
    """The plan of ``query`` as claims_api runs it, with seq scans discouraged.

    Seq scans are turned off because a few thousand rows is a size where the
    planner would rather read the table; the test asks which index the query
    can use, not which plan wins at this size.
    """
    with connect(db.dsn(ROLE), "test") as conn:
        conn.execute("SET enable_seqscan = off")
        rows = conn.execute(f"EXPLAIN {query}", params).fetchall()
    return "\n".join(line for (line,) in rows)


def test_the_trail_query_can_use_the_indexes_of_both_branches(
    many_claims: DatabaseHandle,
) -> None:
    plan = plan_of(many_claims, TRAIL_QUERY, ("CLM-0002", TENANT))

    for index in ("events_reference_idx", "runs_reference_idx", "events_run_id_idx"):
        assert index in plan, plan


def test_the_trail_of_a_seeded_claim_is_its_four_rows(
    many_claims: DatabaseHandle,
) -> None:
    # Guards the plan test: the query it explains answers from the seeded rows.
    rows = run(many_claims, ROLE, TRAIL_QUERY, ("CLM-0002", TENANT))

    assert sorted(row[5] for row in rows) == ["moved", "step-1", "step-2", "step-3"]


def test_the_queue_query_can_use_the_partial_queue_index(
    many_claims: DatabaseHandle,
) -> None:
    plan = plan_of(many_claims, QUEUE_QUERY, (TENANT,))

    assert "claims_queue_idx" in plan, plan
