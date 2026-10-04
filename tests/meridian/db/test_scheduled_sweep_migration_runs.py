"""0014: what the sweep may do to runs, checkpoints and the audit log (S052).

The second of three files on the migration: ``runtime.runs`` with its trigger,
the three checkpoint tables, and ``audit.events``. The first covers the
migration, the grants and the claims; the third the claim trail, the indexes and
what the sweep cannot get around. The constants and helpers they share are in
``sweepmigrationsupport``.
"""

import uuid

import pytest
from dbsupport import OWNER, DatabaseHandle
from sweepmigrationsupport import (
    AGENT,
    CHECKPOINT_CONTENT,
    CHECKPOINT_TABLES,
    CLAIM_ID,
    INSERT_CHECKPOINT,
    INSERT_EVENT,
    INSERT_RUN,
    INSUFFICIENT_PRIVILEGE,
    ROLE,
    RUN_MESSAGE,
    STATUSES,
    SWEEP_RUN_MOVES,
    TENANT,
    count_rows,
    refused,
    run,
    start_run,
    status_of,
    trigger_refusal,
)


# ── runtime.runs ────────────────────────────────────────────────────────────
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
