"""0014: the claim trail, the indexes and what the sweep cannot get around (S052).

The third of three files on the migration: the ``audit.claim_trail`` view, the
two indexes, the ways round a column grant that the sweep must not find, and the
triggers under ``SET ROLE``. The first covers the migration, the grants and the
claims; the second runs, checkpoints and the audit log. The constants and
helpers they share, and the ``claim`` fixture, are in ``sweepmigrationsupport``.
"""

import uuid

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from psycopg.conninfo import make_conninfo
from sweepmigrationsupport import (
    CHECKPOINT_TABLES,
    CLAIM_ID,
    CLAIM_MESSAGE,
    INDEX_COLUMNS,
    INSERT_CHECKPOINT,
    INSERT_CLAIM,
    INSERT_EVENT,
    INSUFFICIENT_PRIVILEGE,
    OTHER_CLAIM_ID,
    OTHER_TENANT,
    ROLE,
    RUN_MESSAGE,
    TENANT,
    TRAIL_COLUMNS,
    count_rows,
    move_params,
    refused,
    run,
    set_state,
    start_run,
    state_of,
    status_of,
)
from sweepmigrationsupport import (
    claim as claim,  # a fixture: pytest finds it in this module's namespace
)

from meridian.platform.common.db import connect
from meridian.workloads.claims_triage.lifecycle import MOVE_CLAIM


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


def test_the_view_has_the_nine_columns_of_0011_0014_and_0017_in_order_seq_last(
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


# ── what the sweep cannot read or get around ────────────────────────────────
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE claims.claims SET state = 'triage_failed' RETURNING submission",
        "UPDATE claims.claims SET state = 'triage_failed' RETURNING *",
        "UPDATE claims.claims SET state = 'triage_failed' "
        "WHERE submission::text LIKE '%%x%%'",
        "DELETE FROM runtime.checkpoints WHERE thread_id = 't' RETURNING checkpoint",
        "DELETE FROM runtime.checkpoints WHERE checkpoint::text LIKE '%%x%%'",
        "COPY claims.claims (submission) TO STDOUT",
    ],
    ids=[
        "update-returning-submission",
        "update-returning-star",
        "update-where-on-submission",
        "delete-returning-checkpoint",
        "delete-where-on-checkpoint",
        "copy-submission",
    ],
)
def test_the_sweep_cannot_read_a_column_it_lacks_through_a_write_or_a_copy(
    claim: DatabaseHandle, statement: str
) -> None:
    set_state(claim, "triaging")
    run(claim, "agent_runtime", INSERT_CHECKPOINT["checkpoints"], ("t",))

    assert refused(claim, ROLE, statement) == INSUFFICIENT_PRIVILEGE
    assert state_of(claim) == "triaging"
    assert count_rows(claim, "checkpoints", "t") == 1


def test_the_sweep_may_lock_a_claim_and_a_run_for_update(
    claim: DatabaseHandle,
) -> None:
    # The sweep's locking rests on it: UPDATE on one column is enough for FOR
    # UPDATE, and the sweep holds that on both tables.
    run_id, _ = start_run(claim, "Running")

    claims = run(claim, ROLE, "SELECT claim_id FROM claims.claims FOR UPDATE")
    runs = run(
        claim,
        ROLE,
        "SELECT run_id FROM runtime.runs WHERE run_id = %s FOR UPDATE",
        (run_id,),
    )

    assert claims == [(CLAIM_ID,)]
    assert runs == [(run_id,)]


@pytest.mark.parametrize("table", CHECKPOINT_TABLES)
def test_the_sweep_may_not_lock_a_checkpoint_row_for_update(
    claim: DatabaseHandle, table: str
) -> None:
    run(claim, "agent_runtime", INSERT_CHECKPOINT[table], ("t",))

    state = refused(
        claim,
        ROLE,
        f"SELECT thread_id FROM runtime.{table} FOR UPDATE",  # noqa: S608
    )

    assert state == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "statement",
    [
        "SET ROLE claims_api",
        "SET ROLE meridian_owner",
        "SET SESSION AUTHORIZATION meridian_owner",
        "SET session_replication_role = replica",
        "ALTER TABLE claims.claims DISABLE TRIGGER claims_confine_sweep",
        "ALTER TABLE runtime.runs DISABLE TRIGGER runs_confine_sweep",
    ],
)
def test_the_sweep_cannot_become_another_role_or_switch_its_triggers_off(
    claim: DatabaseHandle, statement: str
) -> None:
    assert refused(claim, ROLE, statement) == INSUFFICIENT_PRIVILEGE


def test_no_login_is_a_member_of_the_sweep_role(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT count(*) FROM pg_auth_members "
        "WHERE roleid = 'claims_sweep'::regrole OR member = 'claims_sweep'::regrole",
    )

    assert rows == [(0,)]


# ── the triggers hold for SET ROLE as well as for the login ─────────────────
def as_sweep_by_set_role(db: DatabaseHandle) -> psycopg.Connection:
    """A connection whose session user is the admin and whose role is the sweep.

    This is the session of a login later made a member of claims_sweep that runs
    SET ROLE claims_sweep: session_user is not claims_sweep, current_user is. A
    superuser may SET ROLE to any role without a membership, and its own powers
    do not apply once it has.
    """
    conn = psycopg.connect(make_conninfo(db.admin_dsn, dbname=db.name))
    conn.execute("SET ROLE claims_sweep")
    names = conn.execute("SELECT session_user, current_user").fetchone()
    assert names is not None
    assert names[1] == ROLE
    assert names[0] != ROLE
    return conn


@pytest.mark.parametrize(
    ("source", "target", "refusal"),
    [
        ("approved", "awaiting_adjuster", CLAIM_MESSAGE),
        ("triaging", "approved", CLAIM_MESSAGE),
        ("triaging", "triage_failed", None),
    ],
)
def test_a_claim_trigger_binds_a_session_that_set_role_to_the_sweep(
    claim: DatabaseHandle, source: str, target: str, refusal: str | None
) -> None:
    set_state(claim, source)

    with as_sweep_by_set_role(claim) as conn:
        try:
            conn.execute(MOVE_CLAIM, move_params(source, target))  # type: ignore[arg-type]
        except psycopg.errors.RaiseException as exc:
            message = exc.diag.message_primary
        else:
            message = None

    assert message == refusal
    assert state_of(claim) == (source if refusal else target)


@pytest.mark.parametrize(
    ("source", "target", "refusal"),
    [
        ("Completed", "Failed", RUN_MESSAGE),
        ("Running", "Completed", RUN_MESSAGE),
        ("Running", "Failed", None),
    ],
)
def test_a_run_trigger_binds_a_session_that_set_role_to_the_sweep(
    fresh_database: DatabaseHandle, source: str, target: str, refusal: str | None
) -> None:
    run_id, _ = start_run(fresh_database, source)

    with as_sweep_by_set_role(fresh_database) as conn:
        try:
            conn.execute(
                "UPDATE runtime.runs SET status = %s WHERE run_id = %s",
                (target, run_id),
            )
        except psycopg.errors.RaiseException as exc:
            message = exc.diag.message_primary
        else:
            message = None

    assert message == refusal
    assert status_of(fresh_database, run_id) == (source if refusal else target)
