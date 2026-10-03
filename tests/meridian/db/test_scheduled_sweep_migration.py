"""0013: the role of the scheduled sweep and what it may do (S052)."""

import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files
from meridian.workloads.claims_triage.lifecycle import MAX_TRIAGES_PER_CLAIM, MOVE_CLAIM

ROLE = "claims_sweep"
OTHER_ROLES = tuple(role for role in SERVICE_ROLES if role != ROLE)
TENANT = "development"
OTHER_TENANT = "other-tenant"
AGENT = "claims-triage"
CLAIM_ID = "CLM-0013"
OTHER_CLAIM_ID = "CLM-0014"
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
# The three changes of state the sweep may make, as (source, target).
SWEEP_MOVES = frozenset(
    {
        ("documents_requested", "awaiting_adjuster"),
        ("submitted", "triage_failed"),
        ("triaging", "triage_failed"),
    }
)
CLAIM_MESSAGE = "claims_sweep may only expire a claim, not decide or reopen it"
STATUSES = ("Running", "AwaitingApproval", "Completed", "Failed")
SWEEP_RUN_MOVES = frozenset({("Running", "Failed"), ("AwaitingApproval", "Failed")})
RUN_MESSAGE = "claims_sweep may only mark an unfinished run Failed"
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
TRAIL_COLUMNS = (
    "claim_id",
    "tenant",
    "recorded_at",
    "db_role",
    "service",
    "event",
    "outcome",
    "reason",
)

INSERT_CLAIM = (
    "INSERT INTO claims.claims (claim_id, tenant, submission) VALUES (%s, %s, '{}')"
)
INSERT_RUN = (
    "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, status) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
INSERT_EVENT = (
    "INSERT INTO audit.events (service, event, outcome, tenant, run_id, reference, "
    "reason) VALUES (%s, %s, 'ok', %s, %s, %s, %s)"
)
# One row per checkpoint table, with only the columns that have no default.
INSERT_CHECKPOINT = {
    "checkpoints": (
        "INSERT INTO runtime.checkpoints (thread_id, checkpoint_id, checkpoint) "
        "VALUES (%s, 'c1', '{}')"
    ),
    "checkpoint_blobs": (
        "INSERT INTO runtime.checkpoint_blobs (thread_id, channel, version, type) "
        "VALUES (%s, 'ch', 'v1', 'msgpack')"
    ),
    "checkpoint_writes": (
        "INSERT INTO runtime.checkpoint_writes "
        "(thread_id, checkpoint_id, task_id, idx, channel, blob) "
        "VALUES (%s, 'c1', 't1', 0, 'ch', '\\x00')"
    ),
}
# Columns of each checkpoint table that hold the graph's state.
CHECKPOINT_CONTENT = {
    "checkpoints": ("checkpoint", "metadata", "checkpoint_id"),
    "checkpoint_blobs": ("blob", "channel"),
    "checkpoint_writes": ("blob", "task_id"),
}
SELECT_CLAIM_COLUMNS = (
    "SELECT claim_id, tenant, state, state_changed_at, run_id, triages "
    "FROM claims.claims"
)

# What a role holds, as (kind, object, privilege, column) rows. Tables and views
# are asked for their table-level privileges; a column is asked only on tables,
# so that a view's new column is not a change of grants. The public schema is
# left out: its USAGE for everyone is the server's default, not a grant here.
SNAPSHOT = """
SELECT 'table', n.nspname || '.' || c.relname, p.priv, ''
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
    'REFERENCES', 'TRIGGER']) AS p(priv)
WHERE c.relkind IN ('r', 'p', 'v', 'm')
    AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
    AND has_table_privilege(%(role)s, c.oid, p.priv)
UNION ALL
SELECT 'column', n.nspname || '.' || c.relname, p.priv, a.attname
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_attribute AS a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
CROSS JOIN unnest(ARRAY['SELECT', 'UPDATE', 'REFERENCES']) AS p(priv)
WHERE c.relkind IN ('r', 'p')
    AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
    AND has_column_privilege(%(role)s, c.oid, a.attnum, p.priv)
UNION ALL
SELECT 'schema', n.nspname, p.priv, ''
FROM pg_namespace AS n
CROSS JOIN unnest(ARRAY['USAGE', 'CREATE']) AS p(priv)
WHERE n.nspname !~ '^pg_' AND n.nspname NOT IN ('information_schema', 'public')
    AND has_schema_privilege(%(role)s, n.oid, p.priv)
"""
SWEEP_HOLDS = frozenset(
    {
        ("table", "audit.events", "INSERT", ""),
        *(("table", f"runtime.{t}", "DELETE", "") for t in CHECKPOINT_TABLES),
        *(("column", f"runtime.{t}", "SELECT", "thread_id") for t in CHECKPOINT_TABLES),
        *(
            ("column", "claims.claims", "SELECT", column)
            for column in (
                "claim_id",
                "tenant",
                "state",
                "state_changed_at",
                "run_id",
                "triages",
            )
        ),
        *(
            ("column", "claims.claims", "UPDATE", column)
            for column in ("state", "state_changed_at", "run_id", "triages")
        ),
        *(
            ("column", "runtime.runs", "SELECT", column)
            for column in (
                "run_id",
                "thread_id",
                "agent",
                "tenant",
                "reference",
                "status",
                "updated_at",
            )
        ),
        *(
            ("column", "runtime.runs", "UPDATE", column)
            for column in ("status", "updated_at")
        ),
        ("schema", "audit", "USAGE", ""),
        ("schema", "claims", "USAGE", ""),
        ("schema", "runtime", "USAGE", ""),
    }
)
TRIGGER_FUNCTIONS = (
    "claims.confine_sweep_claim_moves()",
    "runtime.confine_sweep_run_moves()",
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


def trigger_refusal(
    db: DatabaseHandle, role: str, statement: str, params: tuple = ()
) -> str | None:
    """The message a trigger refused ``statement`` with, or ``None`` if it ran.

    Any other error is not caught: a missing grant is not a trigger's refusal.
    """
    try:
        run(db, role, statement, params)
    except psycopg.errors.RaiseException as exc:
        return exc.diag.message_primary
    return None


def privileges(db: DatabaseHandle, role: str) -> frozenset[tuple]:
    """Everything ``role`` holds on the tables, views, columns and schemas."""
    return frozenset(run(db, OWNER, SNAPSHOT, {"role": role}))  # type: ignore[arg-type]


# ── claims ──────────────────────────────────────────────────────────────────
def set_state(db: DatabaseHandle, state: str, claim_id: str = CLAIM_ID) -> None:
    """Put a claim in ``state`` as the owner, which no trigger of 0013 binds."""
    run(
        db,
        OWNER,
        "UPDATE claims.claims SET state = %s WHERE claim_id = %s",
        (state, claim_id),
    )


def move_claim(db: DatabaseHandle, role: str, source: str, target: str) -> str | None:
    """``MOVE_CLAIM`` as ``role``: the trigger's message, or ``None`` if it ran."""
    params = {
        "target": target,
        "run_id": None,
        "claim_id": CLAIM_ID,
        "tenant": TENANT,
        "source": source,
        "changed_at": None,
        "max_triages": MAX_TRIAGES_PER_CLAIM,
    }
    return trigger_refusal(db, role, MOVE_CLAIM, params)  # type: ignore[arg-type]


def state_of(db: DatabaseHandle, claim_id: str = CLAIM_ID) -> str:
    rows = run(
        db, OWNER, "SELECT state FROM claims.claims WHERE claim_id = %s", (claim_id,)
    )
    return rows[0][0]


@pytest.fixture
def claim(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A migrated database of its own that holds one claim."""
    run(fresh_database, "claims_api", INSERT_CLAIM, (CLAIM_ID, TENANT))
    return fresh_database


def test_the_migration_is_the_thirteenth_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert names[12] == "0013_scheduled_sweep.sql"
    assert ("0013_scheduled_sweep.sql",) in recorded


def test_a_missing_sweep_role_fails_clearly_and_changes_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    name, text = files[12]
    broken = text.replace(f"ARRAY['{ROLE}']", "ARRAY['role_that_does_not_exist']")
    assert broken != text
    monkeypatch.setattr(
        runner, "migration_files", lambda: [*files[:12], (name, broken)]
    )

    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(
            psycopg.Error,
            match=(
                "required role role_that_does_not_exist does not exist; "
                "create it out of band before migrating"
            ),
        ):
            runner.apply_migrations(conn)
        conn.rollback()

        # The role exists in the cluster, so the grants that follow the check
        # would have worked: none of them ran, and no 0013 object exists.
        assert conn.execute(
            "SELECT has_schema_privilege(%s, 'runtime', 'USAGE'), "
            "has_schema_privilege(%s, 'audit', 'USAGE')",
            (ROLE, ROLE),
        ).fetchone() == (False, False)
        assert conn.execute(
            "SELECT count(*) FROM pg_class WHERE relname = 'claims_run_id_idx'"
        ).fetchone() == (0,)


def test_the_sweep_holds_exactly_the_grants_of_the_contract(
    migrated_database: DatabaseHandle,
) -> None:
    held = privileges(migrated_database, ROLE)

    assert held == SWEEP_HOLDS, (held - SWEEP_HOLDS, SWEEP_HOLDS - held)


def test_the_migration_gives_the_sweep_everything_it_holds_and_changes_no_other_role(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    assert files[12][0] == "0013_scheduled_sweep.sql"
    monkeypatch.setattr(runner, "migration_files", lambda: files[:12])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    before = {role: privileges(empty_database, role) for role in SERVICE_ROLES}
    monkeypatch.setattr(runner, "migration_files", lambda: files[:13])

    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = runner.apply_migrations(conn)

    assert applied == ["0013_scheduled_sweep.sql"]
    assert before[ROLE] == frozenset()
    assert privileges(empty_database, ROLE) == SWEEP_HOLDS
    for role in OTHER_ROLES:
        assert privileges(empty_database, role) == before[role], role


@pytest.mark.parametrize("function", TRIGGER_FUNCTIONS)
@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_no_service_role_may_execute_a_trigger_function_of_the_sweep(
    migrated_database: DatabaseHandle, role: str, function: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
        (role, function),
    )

    assert rows == [(False,)]


def test_nothing_the_migration_made_is_granted_to_public(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_function_privilege('public', %s::regprocedure, 'EXECUTE'), "
        "has_function_privilege('public', %s::regprocedure, 'EXECUTE')",
        TRIGGER_FUNCTIONS,
    )

    assert rows == [(False, False)]


def test_the_sweep_reads_and_updates_the_granted_columns_of_a_claim(
    claim: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    set_state(claim, "triaging")

    run(
        claim,
        ROLE,
        "UPDATE claims.claims SET state = 'triage_failed', "
        "state_changed_at = clock_timestamp(), run_id = %s, triages = triages "
        "WHERE claim_id = %s",
        (run_id, CLAIM_ID),
    )
    rows = run(claim, ROLE, SELECT_CLAIM_COLUMNS)

    assert [(r[0], r[1], r[2], r[4], r[5]) for r in rows] == [
        (CLAIM_ID, TENANT, "triage_failed", run_id, 0)
    ]


@pytest.mark.parametrize("column", ["submission", "policy_number", "received_at"])
def test_the_sweep_cannot_read_what_the_claimant_wrote(
    claim: DatabaseHandle, column: str
) -> None:
    state = refused(claim, ROLE, f"SELECT {column} FROM claims.claims")  # noqa: S608

    assert state == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "table",
    [
        "triage_proposals",
        "decisions",
        "claim_documents",
        "notes",
        "approval_requests",
    ],
)
def test_the_sweep_cannot_read_the_rest_of_the_claims_schema(
    claim: DatabaseHandle, table: str
) -> None:
    state = refused(claim, ROLE, f"SELECT count(*) FROM claims.{table}")  # noqa: S608

    assert state == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "assignment",
    [
        "submission = '{}'",
        "tenant = 'other'",
        "claim_id = 'CLM-0010'",
        "received_at = now()",
    ],
)
def test_the_sweep_cannot_update_any_other_column_of_a_claim(
    claim: DatabaseHandle, assignment: str
) -> None:
    state = refused(claim, ROLE, f"UPDATE claims.claims SET {assignment}")  # noqa: S608

    assert state == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "statement", ["DELETE FROM claims.claims", "TRUNCATE claims.claims"]
)
def test_the_sweep_cannot_delete_a_claim(claim: DatabaseHandle, statement: str) -> None:
    assert refused(claim, ROLE, statement) == INSUFFICIENT_PRIVILEGE


def test_the_sweep_cannot_create_a_claim(claim: DatabaseHandle) -> None:
    state = refused(claim, ROLE, INSERT_CLAIM, ("CLM-0020", TENANT))

    assert state == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("source", STATES)
def test_the_sweep_moves_a_claim_along_its_three_edges_and_no_other(
    claim: DatabaseHandle, source: str
) -> None:
    for target in STATES:
        set_state(claim, source)

        refusal = move_claim(claim, ROLE, source, target)

        if (source, target) in SWEEP_MOVES:
            assert refusal is None, (source, target)
            assert state_of(claim) == target, (source, target)
        else:
            assert refusal == CLAIM_MESSAGE, (source, target)
            assert state_of(claim) == source, (source, target)


def test_a_move_the_sweep_may_not_make_names_no_row_value_in_its_refusal(
    claim: DatabaseHandle,
) -> None:
    set_state(claim, "approved")

    refusal = move_claim(claim, ROLE, "approved", "awaiting_adjuster")

    assert refusal == CLAIM_MESSAGE
    assert CLAIM_ID not in refusal
    assert "approved" not in refusal


def test_the_sweep_may_not_raise_the_triage_count_in_an_edge_it_may_take(
    claim: DatabaseHandle,
) -> None:
    set_state(claim, "triaging")

    refusal = trigger_refusal(
        claim,
        ROLE,
        "UPDATE claims.claims SET state = 'triage_failed', triages = triages + 1",
    )

    assert refusal == CLAIM_MESSAGE
    assert state_of(claim) == "triaging"
    assert run(claim, OWNER, "SELECT triages FROM claims.claims") == [(0,)]


def test_the_sweep_may_not_lower_the_triage_count_in_an_edge_it_may_take(
    claim: DatabaseHandle,
) -> None:
    run(claim, OWNER, "UPDATE claims.claims SET state = 'triaging', triages = 2")

    refusal = trigger_refusal(
        claim,
        ROLE,
        "UPDATE claims.claims SET state = 'triage_failed', triages = 1",
    )

    assert refusal == CLAIM_MESSAGE
    assert run(claim, OWNER, "SELECT state, triages FROM claims.claims") == [
        ("triaging", 2)
    ]


def test_the_sweep_may_not_change_a_claim_without_moving_it(
    claim: DatabaseHandle,
) -> None:
    set_state(claim, "triaging")

    refusal = trigger_refusal(
        claim, ROLE, "UPDATE claims.claims SET state_changed_at = clock_timestamp()"
    )

    assert refusal == CLAIM_MESSAGE


def test_one_row_the_sweep_may_not_move_refuses_the_whole_statement(
    claim: DatabaseHandle,
) -> None:
    run(claim, "claims_api", INSERT_CLAIM, (OTHER_CLAIM_ID, TENANT))
    set_state(claim, "triaging", OTHER_CLAIM_ID)
    set_state(claim, "approved")

    # One statement over both claims: the row that may not move refuses it all.
    refusal = trigger_refusal(
        claim, ROLE, "UPDATE claims.claims SET state = 'triage_failed'"
    )

    assert refusal == CLAIM_MESSAGE
    assert state_of(claim) == "approved"
    assert state_of(claim, OTHER_CLAIM_ID) == "triaging"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("submitted", "triaging"),
        ("triaging", "approved"),
        ("awaiting_adjuster", "approved"),
        ("documents_requested", "withdrawn"),
        ("approved", "awaiting_adjuster"),
    ],
)
def test_the_claims_api_is_not_bound_by_the_sweeps_trigger(
    claim: DatabaseHandle, source: str, target: str
) -> None:
    set_state(claim, source)

    refusal = move_claim(claim, "claims_api", source, target)

    assert refusal is None
    assert state_of(claim) == target


def test_the_owner_is_not_bound_by_the_sweeps_trigger(claim: DatabaseHandle) -> None:
    set_state(claim, "approved")

    set_state(claim, "awaiting_adjuster")

    assert state_of(claim) == "awaiting_adjuster"


# ── runtime.runs ────────────────────────────────────────────────────────────
def start_run(db: DatabaseHandle, status: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A run the runtime records in ``status``; returns its ID and its thread."""
    run_id, thread_id = uuid.uuid4(), uuid.uuid4()
    run(
        db,
        "agent_runtime",
        INSERT_RUN,
        (run_id, thread_id, AGENT, TENANT, CLAIM_ID, status),
    )
    return run_id, thread_id


def set_status(
    db: DatabaseHandle, role: str, run_id: uuid.UUID, status: str
) -> str | None:
    """Change a run's status as ``role``: the trigger's message, or ``None``."""
    return trigger_refusal(
        db,
        role,
        "UPDATE runtime.runs SET status = %s, updated_at = now() WHERE run_id = %s",
        (status, run_id),
    )


def status_of(db: DatabaseHandle, run_id: uuid.UUID) -> str:
    rows = run(
        db, OWNER, "SELECT status FROM runtime.runs WHERE run_id = %s", (run_id,)
    )
    return rows[0][0]


def test_the_sweep_reads_the_columns_of_a_run_it_is_granted(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread_id = start_run(fresh_database, "Running")

    rows = run(
        fresh_database,
        ROLE,
        "SELECT run_id, thread_id, agent, tenant, reference, status, "
        "updated_at IS NOT NULL FROM runtime.runs",
    )

    assert rows == [(run_id, thread_id, AGENT, TENANT, CLAIM_ID, "Running", True)]


def test_the_sweep_cannot_read_when_a_run_was_created(
    fresh_database: DatabaseHandle,
) -> None:
    start_run(fresh_database, "Running")

    state = refused(fresh_database, ROLE, "SELECT created_at FROM runtime.runs")

    assert state == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("source", STATUSES)
def test_the_sweep_ends_an_unfinished_run_as_failed_and_changes_none_other(
    fresh_database: DatabaseHandle, source: str
) -> None:
    for target in STATUSES:
        run_id, _ = start_run(fresh_database, source)

        refusal = set_status(fresh_database, ROLE, run_id, target)

        if (source, target) in SWEEP_RUN_MOVES:
            assert refusal is None, (source, target)
            assert status_of(fresh_database, run_id) == target, (source, target)
        else:
            assert refusal == RUN_MESSAGE, (source, target)
            assert status_of(fresh_database, run_id) == source, (source, target)


@pytest.mark.parametrize(
    "assignment",
    [
        "run_id = gen_random_uuid()",
        "thread_id = gen_random_uuid()",
        "agent = 'other'",
        "tenant = 'other'",
        "reference = 'CLM-0010'",
        "created_at = now()",
    ],
)
def test_the_sweep_cannot_update_any_other_column_of_a_run(
    fresh_database: DatabaseHandle, assignment: str
) -> None:
    start_run(fresh_database, "Running")

    state = refused(fresh_database, ROLE, f"UPDATE runtime.runs SET {assignment}")  # noqa: S608

    assert state == INSUFFICIENT_PRIVILEGE


def test_the_sweep_cannot_create_or_delete_a_run(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, _ = start_run(fresh_database, "Running")

    insert = refused(
        fresh_database,
        ROLE,
        INSERT_RUN,
        (uuid.uuid4(), uuid.uuid4(), AGENT, TENANT, CLAIM_ID, "Running"),
    )
    delete = refused(fresh_database, ROLE, "DELETE FROM runtime.runs")

    assert (insert, delete) == (INSUFFICIENT_PRIVILEGE, INSUFFICIENT_PRIVILEGE)
    assert status_of(fresh_database, run_id) == "Running"


@pytest.mark.parametrize("target", ["Completed", "Failed", "AwaitingApproval"])
def test_the_runtime_is_not_bound_by_the_sweeps_trigger(
    fresh_database: DatabaseHandle, target: str
) -> None:
    run_id, _ = start_run(fresh_database, "Running")

    refusal = set_status(fresh_database, "agent_runtime", run_id, target)

    assert refusal is None
    assert status_of(fresh_database, run_id) == target


# ── runtime.checkpoints, checkpoint_blobs, checkpoint_writes ────────────────
def count_rows(db: DatabaseHandle, table: str, thread: str) -> int:
    rows = run(
        db,
        OWNER,
        f"SELECT count(*) FROM runtime.{table} WHERE thread_id = %s",  # noqa: S608
        (thread,),
    )
    return rows[0][0]


@pytest.mark.parametrize("table", CHECKPOINT_TABLES)
def test_the_sweep_reads_the_thread_of_a_checkpoint_row(
    fresh_database: DatabaseHandle, table: str
) -> None:
    run(fresh_database, "agent_runtime", INSERT_CHECKPOINT[table], ("thread-1",))

    rows = run(fresh_database, ROLE, f"SELECT thread_id FROM runtime.{table}")  # noqa: S608

    assert rows == [("thread-1",)]


@pytest.mark.parametrize("table", CHECKPOINT_TABLES)
def test_the_sweep_removes_the_rows_of_one_thread_and_no_other(
    fresh_database: DatabaseHandle, table: str
) -> None:
    for thread in ("thread-1", "thread-2"):
        run(fresh_database, "agent_runtime", INSERT_CHECKPOINT[table], (thread,))

    run(
        fresh_database,
        ROLE,
        f"DELETE FROM runtime.{table} WHERE thread_id = %s",  # noqa: S608
        ("thread-1",),
    )

    assert count_rows(fresh_database, table, "thread-1") == 0
    assert count_rows(fresh_database, table, "thread-2") == 1


def test_the_sweep_removes_the_checkpoints_of_a_runs_thread_found_in_the_runs(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread_id = start_run(fresh_database, "Running")
    for table in CHECKPOINT_TABLES:
        run(
            fresh_database,
            "agent_runtime",
            INSERT_CHECKPOINT[table],
            (str(thread_id),),
        )

    for table in CHECKPOINT_TABLES:
        run(
            fresh_database,
            ROLE,
            f"DELETE FROM runtime.{table} WHERE thread_id = "  # noqa: S608
            "(SELECT thread_id::text FROM runtime.runs WHERE run_id = %s)",
            (run_id,),
        )

    assert [
        count_rows(fresh_database, t, str(thread_id)) for t in CHECKPOINT_TABLES
    ] == [
        0,
        0,
        0,
    ]


@pytest.mark.parametrize(
    ("table", "column"),
    [(t, c) for t, columns in CHECKPOINT_CONTENT.items() for c in columns],
)
def test_the_sweep_cannot_read_what_a_checkpoint_holds(
    fresh_database: DatabaseHandle, table: str, column: str
) -> None:
    run(fresh_database, "agent_runtime", INSERT_CHECKPOINT[table], ("thread-1",))

    state = refused(
        fresh_database,
        ROLE,
        f"SELECT {column} FROM runtime.{table}",  # noqa: S608
    )

    assert state == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("table", CHECKPOINT_TABLES)
def test_the_sweep_cannot_write_a_checkpoint_table_but_delete(
    fresh_database: DatabaseHandle, table: str
) -> None:
    run(fresh_database, "agent_runtime", INSERT_CHECKPOINT[table], ("thread-1",))

    insert = refused(fresh_database, ROLE, INSERT_CHECKPOINT[table], ("thread-2",))
    update = refused(
        fresh_database,
        ROLE,
        f"UPDATE runtime.{table} SET type = 'x' WHERE thread_id = %s",  # noqa: S608
        ("thread-1",),
    )
    truncate = refused(fresh_database, ROLE, f"TRUNCATE runtime.{table}")

    assert (insert, update, truncate) == (INSUFFICIENT_PRIVILEGE,) * 3
    assert count_rows(fresh_database, table, "thread-1") == 1


# ── audit ───────────────────────────────────────────────────────────────────
def test_the_sweep_appends_an_event_and_the_database_stamps_its_role(
    fresh_database: DatabaseHandle,
) -> None:
    run(
        fresh_database,
        ROLE,
        INSERT_EVENT,
        ("claims-sweep", "claim.triage_failed", TENANT, None, CLAIM_ID, "stuck-triage"),
    )

    rows = run(
        fresh_database,
        OWNER,
        "SELECT db_role, service, event, reason FROM audit.events",
    )

    assert rows == [
        ("claims_sweep", "claims-sweep", "claim.triage_failed", "stuck-triage")
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count(*) FROM audit.events",
        "SELECT count(*) FROM audit.claim_trail",
        "UPDATE audit.events SET outcome = 'x'",
        "DELETE FROM audit.events",
    ],
)
def test_the_sweep_cannot_read_change_or_delete_the_log(
    fresh_database: DatabaseHandle, statement: str
) -> None:
    assert refused(fresh_database, ROLE, statement) == INSUFFICIENT_PRIVILEGE


# ── audit.claim_trail ───────────────────────────────────────────────────────
def write_event(
    db: DatabaseHandle,
    role: str,
    event: str,
    *,
    tenant: str | None = None,
    run_id: uuid.UUID | None = None,
    reference: str | None = None,
    reason: str | None = None,
) -> None:
    """One audit row as ``role``; the database stamps ``db_role`` itself."""
    run(db, role, INSERT_EVENT, (role, event, tenant, run_id, reference, reason))


def trail_of(db: DatabaseHandle, claim_id: str = CLAIM_ID) -> list[tuple]:
    return run(
        db,
        "claims_api",
        "SELECT event, db_role, reason FROM audit.claim_trail "
        "WHERE claim_id = %s ORDER BY event",
        (claim_id,),
    )


def test_the_view_has_the_seven_columns_of_0011_in_order_and_the_reason_last(
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


def test_the_view_is_still_a_security_barrier(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT reloptions FROM pg_class WHERE oid = 'audit.claim_trail'::regclass",
    )

    assert rows == [(["security_barrier=true"],)]


def test_claims_api_keeps_its_select_on_the_view_and_the_sweep_has_none(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_table_privilege('claims_api', 'audit.claim_trail', 'SELECT'), "
        "has_table_privilege('claims_sweep', 'audit.claim_trail', 'SELECT'), "
        "has_any_column_privilege('claims_sweep', 'audit.claim_trail', 'SELECT')",
    )

    assert rows == [(True, False, False)]


def test_an_event_of_the_sweep_about_a_claim_is_in_its_trail_with_its_reason(
    claim: DatabaseHandle,
) -> None:
    write_event(
        claim,
        ROLE,
        "claim.triage_failed",
        tenant=TENANT,
        reference=CLAIM_ID,
        reason="stuck-triage",
    )

    assert trail_of(claim) == [("claim.triage_failed", ROLE, "stuck-triage")]


def test_an_event_of_the_claims_api_shows_its_reason_too(
    claim: DatabaseHandle,
) -> None:
    write_event(
        claim,
        "claims_api",
        "claim.triaging",
        tenant=TENANT,
        reference=CLAIM_ID,
        reason="triage-started",
    )

    assert trail_of(claim) == [("claim.triaging", "claims_api", "triage-started")]


def test_an_event_of_another_role_in_the_claims_run_shows_its_reason(
    claim: DatabaseHandle,
) -> None:
    run_id, _ = start_run(claim, "Running")
    write_event(claim, "agent_runtime", "run.step", run_id=run_id, reason="a-word")

    assert trail_of(claim) == [("run.step", "agent_runtime", "a-word")]


def test_a_run_failed_event_of_the_sweep_with_the_runs_id_is_in_the_trail_once(
    claim: DatabaseHandle,
) -> None:
    # Both branches of the view could match this row: the sweep wrote it with
    # the claim's reference and tenant and with the ID of the claim's run.
    run_id, _ = start_run(claim, "Running")
    write_event(
        claim,
        ROLE,
        "run.failed",
        tenant=TENANT,
        run_id=run_id,
        reference=CLAIM_ID,
        reason="abandoned-run",
    )

    assert trail_of(claim) == [("run.failed", ROLE, "abandoned-run")]


def test_an_event_of_the_sweep_with_the_runs_id_alone_is_in_the_trail_once(
    claim: DatabaseHandle,
) -> None:
    run_id, _ = start_run(claim, "Running")
    write_event(claim, ROLE, "run.failed", run_id=run_id, reason="abandoned-run")

    assert trail_of(claim) == [("run.failed", ROLE, "abandoned-run")]


@pytest.mark.parametrize(
    ("tenant", "reference"),
    [(None, CLAIM_ID), (TENANT, None), (None, None), (OTHER_TENANT, CLAIM_ID)],
    ids=["null-tenant", "null-reference", "null-both", "other-tenant"],
)
def test_a_sweep_event_of_the_run_with_other_names_is_in_the_trail_once(
    claim: DatabaseHandle, tenant: str | None, reference: str | None
) -> None:
    # The first branch needs the claim's tenant and reference; a row that lacks
    # either belongs to the run branch and must not drop out of both.
    run_id, _ = start_run(claim, "Running")
    write_event(
        claim, ROLE, "run.failed", tenant=tenant, run_id=run_id, reference=reference
    )

    assert trail_of(claim) == [("run.failed", ROLE, None)]


@pytest.mark.parametrize(
    ("tenant", "reference"),
    [(OTHER_TENANT, CLAIM_ID), (None, CLAIM_ID), (TENANT, None), (TENANT, "CLM-0099")],
    ids=["other-tenant", "null-tenant", "null-reference", "other-claim"],
)
def test_a_sweep_event_that_only_names_the_claim_is_not_in_its_trail(
    claim: DatabaseHandle, tenant: str | None, reference: str | None
) -> None:
    write_event(claim, ROLE, "stray", tenant=tenant, reference=reference)

    assert trail_of(claim) == []


def test_a_sweep_event_is_in_the_trail_of_its_own_claim_only(
    claim: DatabaseHandle,
) -> None:
    run(claim, "claims_api", INSERT_CLAIM, (OTHER_CLAIM_ID, TENANT))
    write_event(claim, ROLE, "claim.triage_failed", tenant=TENANT, reference=CLAIM_ID)

    assert trail_of(claim, OTHER_CLAIM_ID) == []
    assert len(trail_of(claim)) == 1


# ── indexes ─────────────────────────────────────────────────────────────────
def test_the_events_index_serves_the_rows_of_both_writers_of_a_claim(
    migrated_database: DatabaseHandle,
) -> None:
    columns = run(
        migrated_database,
        OWNER,
        INDEX_COLUMNS,
        ("audit", "events", "events_reference_idx"),
    )
    definition = run(
        migrated_database,
        OWNER,
        "SELECT pg_get_indexdef('audit.events_reference_idx'::regclass)",
    )[0][0]

    assert [name for (name,) in columns] == ["reference"]
    assert definition.endswith(
        "WHERE (db_role = ANY (ARRAY['claims_api'::name, 'claims_sweep'::name]))"
    )


def test_the_claims_run_index_is_partial_on_the_claims_that_name_a_run(
    migrated_database: DatabaseHandle,
) -> None:
    columns = run(
        migrated_database,
        OWNER,
        INDEX_COLUMNS,
        ("claims", "claims", "claims_run_id_idx"),
    )
    definition = run(
        migrated_database,
        OWNER,
        "SELECT pg_get_indexdef('claims.claims_run_id_idx'::regclass)",
    )[0][0]

    assert [name for (name,) in columns] == ["run_id"]
    assert definition.endswith("WHERE (run_id IS NOT NULL)")


def test_the_sweeps_question_does_a_claim_name_this_run_can_use_the_index(
    claim: DatabaseHandle,
) -> None:
    # Seq scans are off because the table is a row long: the test asks which
    # index the query can use, not which plan wins at this size.
    with connect(claim.dsn(ROLE), "test") as conn:
        conn.execute("SET enable_seqscan = off")
        rows = conn.execute(
            "EXPLAIN SELECT claim_id FROM claims.claims WHERE run_id = %s",
            (uuid.uuid4(),),
        ).fetchall()

    assert "claims_run_id_idx" in "\n".join(line for (line,) in rows)
