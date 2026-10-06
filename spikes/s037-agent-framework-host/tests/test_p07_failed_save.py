"""Probe 7: a save that fails (the table gone, a constraint violated).

The framework logs a warning and goes on. What the host can check after
``workflow.run`` to fail the leg instead.
"""

import logging
import uuid

import psycopg
import pytest
from agent_framework import WorkflowRunResult
from psycopg import sql
from s037probe.flow import CHECKPOINT_TYPES, CLAIM, WORKFLOW_NAME
from s037probe.rig import (
    Rig,
    all_checkpoints,
    describe,
    latest_sync,
    leg,
    resume,
    start,
)
from s037probe.store import SCHEMA, TABLE, PostgresCheckpointStorage

RUNNER_LOG = "agent_framework._workflows._runner"
BRIEF = [{"brief": "a synthetic brief", "filed": True}]


def execute(dsn: str, statement: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL(statement))  # type: ignore[arg-type]


def limit_iterations(dsn: str, below: int) -> None:
    execute(
        dsn,
        f"ALTER TABLE {SCHEMA}.{TABLE} ADD CONSTRAINT lim "
        f"CHECK ((body->>'iteration_count')::int < {below})",
    )


def pause_ids(result: WorkflowRunResult) -> list[str]:
    return [event.request_id for event in result.get_request_info_events()]


def test_a_table_that_is_gone_does_not_stop_the_run_and_the_pause_is_unsaved(
    scratch_schema: str, thread_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    execute(scratch_schema, f"DROP TABLE {SCHEMA}.{TABLE}")
    rig = Rig(scratch_schema, thread_id)

    with caplog.at_level(logging.WARNING, logger=RUNNER_LOG):
        workflow, result = start(rig)

    # The run reaches its pause and reports the request, as if all were well.
    assert len(pause_ids(result)) == 1
    # What shows that nothing durable exists: the framework's own answer, the
    # runner's last saved ID, and the store's own record of its failures.
    assert leg(lambda: workflow.resolve_pause_checkpoint_id(pause_ids(result))) is None
    assert workflow.get_last_checkpoint_id() is None
    assert rig.used_storage.failures == ["UndefinedTable"] * 4
    warnings = [r for r in caplog.records if r.name == RUNNER_LOG]
    assert len(warnings) == 4
    assert all("Failed to create checkpoint" in r.getMessage() for r in warnings)


def test_a_constraint_that_refuses_only_the_pause_leaves_the_earlier_saves(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    limit_iterations(scratch_schema, below=3)
    rig = Rig(scratch_schema, thread_id)

    workflow, result = start(rig)

    assert len(pause_ids(result)) == 1
    stored = all_checkpoints(rig)
    assert [c.iteration_count for c in stored] == [0, 1, 2]
    assert leg(lambda: workflow.resolve_pause_checkpoint_id(pause_ids(result))) is None
    # The runner's last ID is the last good save, which is not a pause.
    assert workflow.get_last_checkpoint_id() == stored[-1].checkpoint_id
    assert stored[-1].pending_request_info_events == {}
    assert rig.used_storage.failures == ["CheckViolation"]


def test_a_failed_save_of_the_completed_run_looks_complete_to_the_runners_id(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    limit_iterations(scratch_schema, below=4)

    _, result = resume(rig, {})
    # A fresh look at the store, as the next leg's host would have it.
    latest = latest_sync(rig)

    # The run completed and gave its output...
    assert describe(result)["outputs"] == BRIEF
    # ...but the store never saw the end: its latest checkpoint still has the
    # response in flight, so a later resume would run the last steps again.
    assert list(latest.messages) == ["internal:ask"]
    assert rig.used_storage.failures == ["CheckViolation"] * 2


def test_the_runners_id_agrees_with_the_store_after_a_failed_final_save(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    limit_iterations(scratch_schema, below=4)
    storage = rig.storage()
    workflow = rig.workflow(storage=storage)
    stored = latest_sync(rig)

    leg(
        lambda: workflow.run(
            responses={rid: {} for rid in stored.pending_request_info_events},
            checkpoint_id=stored.checkpoint_id,
        )
    )

    # The runner's last ID is the last save that worked: the same as the
    # store's latest. Comparing the two is no check for a completed leg.
    assert workflow.get_last_checkpoint_id() == latest_sync(rig).checkpoint_id
    # The store's record is: it is empty only when every save of the leg worked.
    assert storage.failures != []


def test_a_leg_whose_saves_all_worked_has_no_failures_and_a_terminal_checkpoint(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)

    workflow, result = start(rig)
    ids = pause_ids(result)
    failures_of_start = list(rig.used_storage.failures)
    resume(rig, {})
    final = latest_sync(rig)

    assert failures_of_start == []
    assert rig.used_storage.failures == []
    assert leg(lambda: workflow.resolve_pause_checkpoint_id(ids)) is not None
    assert (final.pending_request_info_events, final.messages) == ({}, {})


class LeakyStore(PostgresCheckpointStorage):
    wrap_database_errors = False


def test_the_frameworks_warning_quotes_the_error_of_a_store_that_lets_one_escape(
    scratch_schema: str, thread_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    limit_iterations(scratch_schema, below=1)
    leaky, wrapped_rig = (
        Rig(scratch_schema, thread_id),
        Rig(scratch_schema, uuid.uuid4()),
    )
    workflow = leaky.workflow(
        storage=LeakyStore(scratch_schema, thread_id, CHECKPOINT_TYPES)
    )

    with caplog.at_level(logging.WARNING, logger=RUNNER_LOG):
        leg(lambda: workflow.run(CLAIM))
    raw = "\n".join(r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=RUNNER_LOG):
        start(wrapped_rig)
    wrapped = "\n".join(r.getMessage() for r in caplog.records)

    # A driver error quotes the failing row; the framework logs what ``save``
    # raises. The thread ID and the start of the body (the checkpoint's own
    # JSON) are in the raw text; the wrapped store's text has the class name.
    assert str(thread_id) in raw
    assert '{"state"' in raw
    assert wrapped != ""
    assert str(wrapped_rig.thread_id) not in wrapped
    assert '{"state"' not in wrapped
    assert "CheckViolation" in wrapped
    assert WORKFLOW_NAME not in wrapped
