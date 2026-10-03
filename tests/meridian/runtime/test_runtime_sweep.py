"""What the sweep does to the runtime's tables: end an abandoned run, clean up
checkpoints (S052). Every statement runs as the sweep's own database role."""

import subprocess
import sys
import uuid
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from meridian.runtime.sweep import (
    ABANDONED_REASON,
    RUN_FAILED_EVENT,
    RUNNING_LEASE_SECONDS,
    delete_thread_checkpoints,
    end_abandoned_run,
    leftover_threads,
)
from servicesupport import audit_events, owner_rows

from meridian.platform.common.db import connect
from meridian.runtime import runs
from meridian.runtime import sweep as runtime_sweep

SWEEP_ROLE = "claims_sweep"
SERVICE = "claims-sweep"
AGENT = "claims-triage"
TENANT = "development"
REFERENCE = "CLM-5201"
PAST_THE_LEASE = RUNNING_LEASE_SECONDS + 60
INSIDE_THE_LEASE = RUNNING_LEASE_SECONDS - 60
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
PRESENT = dict.fromkeys(CHECKPOINT_TABLES, 1)
GONE = dict.fromkeys(CHECKPOINT_TABLES, 0)
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


# ── the modules the sweep imports are free of the agent framework ───────────
def test_the_sweep_modules_import_no_agent_framework() -> None:
    program = (
        "import sys\n"
        "import meridian.runtime.sweep\n"
        "import meridian.workloads.claims_triage.sweep\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'langgraph', 'langchain', 'langchain_core', 'fastapi', 'httpx'})\n"
        "print(loaded)\n"
        "raise SystemExit(1 if loaded else 0)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_lease_is_ten_minutes_and_the_runtime_still_reads_it_from_runs() -> None:
    assert RUNNING_LEASE_SECONDS == 600
    assert runs.RUNNING_LEASE_SECONDS is RUNNING_LEASE_SECONDS


def test_the_event_of_an_ended_run_is_the_runtimes_own_word_for_a_failure() -> None:
    assert runs.AUDIT_FOR_STATE["Failed"] == RUN_FAILED_EVENT
    assert ABANDONED_REASON == "abandoned"
    assert runtime_sweep.SWEPT_STATUSES == ("Running", "AwaitingApproval")


# ── rows to work on ─────────────────────────────────────────────────────────
def add_run(
    db: DatabaseHandle,
    status: str,
    *,
    idle_seconds: int = PAST_THE_LEASE,
    agent: str = AGENT,
    reference: str = REFERENCE,
    thread_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str]:
    """A run as the runtime records it, idle for ``idle_seconds``, with a row in
    each checkpoint table; returns its ID and its thread."""
    run_id, thread = uuid.uuid4(), thread_id or uuid.uuid4()
    owner_rows(
        db,
        "INSERT INTO runtime.runs "
        "(run_id, thread_id, agent, tenant, reference, status, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, now() - make_interval(secs => %s)) "
        "RETURNING 1",
        (run_id, thread, agent, TENANT, reference, status, float(idle_seconds)),
    )
    add_checkpoints(db, str(thread))
    return run_id, str(thread)


def add_checkpoints(db: DatabaseHandle, thread: str) -> None:
    for table in CHECKPOINT_TABLES:
        with connect(db.dsn("agent_runtime"), "test") as conn:
            conn.execute(INSERT_CHECKPOINT[table], (thread,))
            conn.commit()


def checkpoint_counts(db: DatabaseHandle, thread: str) -> dict[str, int]:
    return {
        table: owner_rows(
            db,
            f"SELECT count(*) FROM runtime.{table} WHERE thread_id = %s",  # noqa: S608
            (thread,),
        )[0][0]
        for table in CHECKPOINT_TABLES
    }


def status_of(db: DatabaseHandle, run_id: uuid.UUID) -> str:
    return owner_rows(
        db, "SELECT status FROM runtime.runs WHERE run_id = %s", (run_id,)
    )[0][0]


def as_the_sweep(db: DatabaseHandle, action: Any) -> Any:
    """Run ``action(conn)`` as the sweep's role and commit, as the pass does."""
    with connect(db.dsn(SWEEP_ROLE), SERVICE) as conn:
        result = action(conn)
        conn.commit()
    return result


# ── ending one run ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", ["Running", "AwaitingApproval"])
def test_an_unfinished_run_past_the_lease_is_failed_audited_and_its_thread_goes(
    fresh_database: DatabaseHandle, status: str
) -> None:
    run_id, thread = add_run(fresh_database, status)
    other_id, other_thread = add_run(fresh_database, "Completed")

    ended = as_the_sweep(
        fresh_database, lambda c: end_abandoned_run(c, run_id, service=SERVICE)
    )

    assert ended is True
    assert status_of(fresh_database, run_id) == "Failed"
    assert checkpoint_counts(fresh_database, thread) == GONE
    assert status_of(fresh_database, other_id) == "Completed"
    assert checkpoint_counts(fresh_database, other_thread) == PRESENT
    (event,) = audit_events(fresh_database, run_id)
    assert (
        event["service"],
        event["event"],
        event["outcome"],
        event["reason"],
        event["tenant"],
        event["agent"],
        event["reference"],
    ) == (SERVICE, "run.failed", "failed", "abandoned", TENANT, AGENT, REFERENCE)
    assert owner_rows(
        fresh_database,
        "SELECT db_role FROM audit.events WHERE run_id = %s",
        (run_id,),
    ) == [(SWEEP_ROLE,)]


def test_a_run_inside_the_lease_is_left_as_it_is(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "Running", idle_seconds=INSIDE_THE_LEASE)

    ended = as_the_sweep(
        fresh_database, lambda c: end_abandoned_run(c, run_id, service=SERVICE)
    )

    assert ended is False
    assert status_of(fresh_database, run_id) == "Running"
    assert checkpoint_counts(fresh_database, thread) == PRESENT
    assert audit_events(fresh_database, run_id) == []


@pytest.mark.parametrize("status", ["Completed", "Failed"])
def test_a_finished_run_is_left_as_it_is(
    fresh_database: DatabaseHandle, status: str
) -> None:
    run_id, thread = add_run(fresh_database, status)

    ended = as_the_sweep(
        fresh_database, lambda c: end_abandoned_run(c, run_id, service=SERVICE)
    )

    assert ended is False
    assert status_of(fresh_database, run_id) == status
    assert checkpoint_counts(fresh_database, thread) == PRESENT
    assert audit_events(fresh_database, run_id) == []


def test_a_run_a_resume_claimed_first_is_left_alone_with_its_checkpoints(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "AwaitingApproval")
    # What claim_paused_run does, committed before the sweep's update runs.
    owner_rows(
        fresh_database,
        "UPDATE runtime.runs SET status = 'Running', updated_at = now() "
        "WHERE run_id = %s RETURNING 1",
        (run_id,),
    )

    ended = as_the_sweep(
        fresh_database, lambda c: end_abandoned_run(c, run_id, service=SERVICE)
    )

    assert ended is False
    assert status_of(fresh_database, run_id) == "Running"
    assert checkpoint_counts(fresh_database, thread) == PRESENT
    assert audit_events(fresh_database, run_id) == []


def test_a_run_that_does_not_exist_is_not_ended(
    fresh_database: DatabaseHandle,
) -> None:
    assert not as_the_sweep(
        fresh_database, lambda c: end_abandoned_run(c, uuid.uuid4(), service=SERVICE)
    )


def test_a_run_and_its_event_and_its_checkpoints_are_one_transaction(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "AwaitingApproval")

    with connect(fresh_database.dsn(SWEEP_ROLE), SERVICE) as conn:
        assert end_abandoned_run(conn, run_id, service=SERVICE)
        conn.rollback()

    assert status_of(fresh_database, run_id) == "AwaitingApproval"
    assert checkpoint_counts(fresh_database, thread) == PRESENT
    assert audit_events(fresh_database, run_id) == []


# ── the checkpoints no run needs ────────────────────────────────────────────
def test_the_threads_of_finished_runs_and_of_no_run_are_leftovers_and_live_ones_are_not(
    fresh_database: DatabaseHandle,
) -> None:
    _, completed = add_run(fresh_database, "Completed")
    _, failed = add_run(fresh_database, "Failed")
    _, running = add_run(fresh_database, "Running", idle_seconds=0)
    _, paused = add_run(fresh_database, "AwaitingApproval")
    orphan = "thread-without-a-run"
    add_checkpoints(fresh_database, orphan)

    found = as_the_sweep(fresh_database, lambda c: leftover_threads(c, limit=100))

    assert sorted(found) == sorted([completed, failed, orphan])
    assert running not in found
    assert paused not in found


def test_a_thread_in_only_one_checkpoint_table_is_a_leftover(
    fresh_database: DatabaseHandle,
) -> None:
    with connect(fresh_database.dsn("agent_runtime"), "test") as conn:
        conn.execute(INSERT_CHECKPOINT["checkpoint_writes"], ("only-in-writes",))
        conn.commit()

    found = as_the_sweep(fresh_database, lambda c: leftover_threads(c, limit=100))

    assert found == ["only-in-writes"]


def test_the_listing_of_leftovers_is_bounded(fresh_database: DatabaseHandle) -> None:
    for number in range(5):
        add_checkpoints(fresh_database, f"orphan-{number}")

    found = as_the_sweep(fresh_database, lambda c: leftover_threads(c, limit=3))

    assert found == ["orphan-0", "orphan-1", "orphan-2"]


def test_deleting_a_leftover_thread_removes_its_rows_from_all_three_tables(
    fresh_database: DatabaseHandle,
) -> None:
    _, completed = add_run(fresh_database, "Completed")
    add_checkpoints(fresh_database, "orphan")
    add_checkpoints(fresh_database, "bystander")

    deleted = as_the_sweep(
        fresh_database, lambda c: delete_thread_checkpoints(c, completed)
    )
    deleted_orphan = as_the_sweep(
        fresh_database, lambda c: delete_thread_checkpoints(c, "orphan")
    )

    assert deleted == deleted_orphan == len(CHECKPOINT_TABLES)
    assert checkpoint_counts(fresh_database, completed) == GONE
    assert checkpoint_counts(fresh_database, "orphan") == GONE
    assert checkpoint_counts(fresh_database, "bystander") == PRESENT


@pytest.mark.parametrize("status", ["Running", "AwaitingApproval"])
def test_the_checkpoints_of_a_live_run_are_never_deleted(
    fresh_database: DatabaseHandle, status: str
) -> None:
    run_id, thread = add_run(fresh_database, status)

    deleted = as_the_sweep(
        fresh_database, lambda c: delete_thread_checkpoints(c, thread)
    )

    assert deleted == 0
    assert checkpoint_counts(fresh_database, thread) == PRESENT
    assert status_of(fresh_database, run_id) == status


def test_deleting_a_thread_twice_deletes_nothing_the_second_time(
    fresh_database: DatabaseHandle,
) -> None:
    add_checkpoints(fresh_database, "orphan")

    first = as_the_sweep(
        fresh_database, lambda c: delete_thread_checkpoints(c, "orphan")
    )
    second = as_the_sweep(
        fresh_database, lambda c: delete_thread_checkpoints(c, "orphan")
    )

    assert (first, second) == (len(CHECKPOINT_TABLES), 0)


def test_every_statement_of_the_module_runs_with_the_rights_of_the_sweeps_role(
    fresh_database: DatabaseHandle,
) -> None:
    # A right the role lacks would raise here and not in the cluster.
    with connect(fresh_database.dsn(SWEEP_ROLE), SERVICE) as conn:
        assert leftover_threads(conn, limit=1) == []
        assert delete_thread_checkpoints(conn, "nothing") == 0
        assert end_abandoned_run(conn, uuid.uuid4(), service=SERVICE) is False
        conn.rollback()
