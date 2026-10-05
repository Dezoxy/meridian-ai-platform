"""0014: the role of the scheduled sweep and what it may do (S052).

The first of three files on the migration: the migration itself, the grants,
and the claims with their trigger. The second covers runs, checkpoints and the
audit log; the third the claim trail, the indexes and what the sweep cannot get
around. The constants and helpers they share, and the ``claim`` fixture, are in
``sweepmigrationsupport``.
"""

import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from sweepmigrationsupport import (
    CLAIM_ID,
    CLAIM_MESSAGE,
    INSERT_CLAIM,
    INSUFFICIENT_PRIVILEGE,
    OTHER_CLAIM_ID,
    OTHER_ROLES,
    ROLE,
    RUN_MESSAGE,
    SELECT_CLAIM_COLUMNS,
    STATES,
    SWEEP_HOLDS,
    SWEEP_MOVES,
    TENANT,
    TRIGGER_FUNCTIONS,
    move_params,
    privileges,
    refused,
    run,
    set_state,
    start_run,
    state_of,
    trigger_refusal,
)
from sweepmigrationsupport import (
    claim as claim,  # a fixture: pytest finds it in this module's namespace
)

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files
from meridian.workloads.claims_triage.lifecycle import MOVE_CLAIM


# ── claims ──────────────────────────────────────────────────────────────────
def move_claim(db: DatabaseHandle, role: str, source: str, target: str) -> str | None:
    """``MOVE_CLAIM`` as ``role``: the trigger's message, or ``None`` if it ran."""
    return trigger_refusal(db, role, MOVE_CLAIM, move_params(source, target))  # type: ignore[arg-type]


def test_the_migration_is_the_fourteenth_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert names[13] == "0014_scheduled_sweep.sql"
    assert ("0014_scheduled_sweep.sql",) in recorded


def test_a_missing_sweep_role_fails_clearly_and_changes_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    name, text = files[13]
    broken = text.replace(f"ARRAY['{ROLE}']", "ARRAY['role_that_does_not_exist']")
    assert broken != text
    monkeypatch.setattr(
        runner, "migration_files", lambda: [*files[:13], (name, broken)]
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
        # would have worked: none of them ran, and no 0014 object exists.
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
    assert files[13][0] == "0014_scheduled_sweep.sql"
    monkeypatch.setattr(runner, "migration_files", lambda: files[:13])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    before = {role: privileges(empty_database, role) for role in SERVICE_ROLES}
    monkeypatch.setattr(runner, "migration_files", lambda: files[:14])

    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = runner.apply_migrations(conn)

    assert applied == ["0014_scheduled_sweep.sql"]
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
    run(
        claim,
        OWNER,
        "UPDATE claims.claims SET state = 'triaging', run_id = %s",
        (run_id,),
    )

    run(
        claim,
        ROLE,
        "UPDATE claims.claims SET state = 'triage_failed', "
        "state_changed_at = clock_timestamp(), run_id = run_id, triages = triages "
        "WHERE claim_id = %s",
        (CLAIM_ID,),
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

    with pytest.raises(psycopg.errors.RaiseException) as caught:
        run(claim, ROLE, MOVE_CLAIM, move_params("approved", "awaiting_adjuster"))  # type: ignore[arg-type]

    diag = caught.value.diag
    assert diag.message_primary == CLAIM_MESSAGE
    assert diag.message_detail is None
    assert diag.message_hint is None
    assert CLAIM_ID not in str(caught.value)
    assert "approved" not in str(caught.value)


def test_a_run_the_sweep_may_not_change_names_no_row_value_in_its_refusal(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, _ = start_run(fresh_database, "Completed")

    with pytest.raises(psycopg.errors.RaiseException) as caught:
        run(
            fresh_database,
            ROLE,
            "UPDATE runtime.runs SET status = 'Failed' WHERE run_id = %s",
            (run_id,),
        )

    diag = caught.value.diag
    assert diag.message_primary == RUN_MESSAGE
    assert diag.message_detail is None
    assert diag.message_hint is None
    assert str(run_id) not in str(caught.value)


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


def test_the_sweep_may_clear_the_run_of_a_claim_it_moves(
    claim: DatabaseHandle,
) -> None:
    run(
        claim,
        OWNER,
        "UPDATE claims.claims SET state = 'triaging', run_id = %s",
        (uuid.uuid4(),),
    )

    refusal = trigger_refusal(
        claim,
        ROLE,
        "UPDATE claims.claims SET state = 'triage_failed', run_id = NULL",
    )

    assert refusal is None
    assert run(claim, OWNER, "SELECT state, run_id FROM claims.claims") == [
        ("triage_failed", None)
    ]


def test_the_sweep_may_keep_the_run_of_a_claim_it_moves(
    claim: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    run(
        claim,
        OWNER,
        "UPDATE claims.claims SET state = 'triaging', run_id = %s",
        (run_id,),
    )

    refusal = trigger_refusal(
        claim,
        ROLE,
        "UPDATE claims.claims SET state = 'triage_failed', run_id = run_id",
    )

    assert refusal is None
    assert run(claim, OWNER, "SELECT state, run_id FROM claims.claims") == [
        ("triage_failed", run_id)
    ]


@pytest.mark.parametrize("old_run", [None, uuid.uuid4()], ids=["no-run", "a-run"])
def test_the_sweep_may_not_point_a_claim_at_another_run(
    claim: DatabaseHandle, old_run: uuid.UUID | None
) -> None:
    run(
        claim,
        OWNER,
        "UPDATE claims.claims SET state = 'triaging', run_id = %s",
        (old_run,),
    )

    refusal = trigger_refusal(
        claim,
        ROLE,
        "UPDATE claims.claims SET state = 'triage_failed', run_id = %s",
        (uuid.uuid4(),),
    )

    assert refusal == CLAIM_MESSAGE
    assert run(claim, OWNER, "SELECT state, run_id FROM claims.claims") == [
        ("triaging", old_run)
    ]


def test_the_sweep_may_not_move_the_time_a_claim_changed_back(
    claim: DatabaseHandle,
) -> None:
    set_state(claim, "triaging")

    refusal = trigger_refusal(
        claim,
        ROLE,
        "UPDATE claims.claims SET state = 'triage_failed', "
        "state_changed_at = '1970-01-01T00:00:00Z'",
    )

    assert refusal == CLAIM_MESSAGE
    assert state_of(claim) == "triaging"


@pytest.mark.parametrize(
    "new_time", ["state_changed_at", "clock_timestamp() + interval '1 hour'"]
)
def test_the_sweep_may_keep_or_advance_the_time_a_claim_changed(
    claim: DatabaseHandle, new_time: str
) -> None:
    set_state(claim, "triaging")

    refusal = trigger_refusal(
        claim,
        ROLE,
        f"UPDATE claims.claims SET state = 'triage_failed', "  # noqa: S608
        f"state_changed_at = {new_time}",
    )

    assert refusal is None
    assert state_of(claim) == "triage_failed"


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
