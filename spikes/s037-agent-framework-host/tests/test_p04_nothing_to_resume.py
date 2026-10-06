"""Probe 4: nothing to resume, three cases, so the host can give all of them the
runtime's one failure word (``no-pending-pause``).

What the host can read in the store before it calls the framework is part of
the answer: the latest checkpoint's pending requests and in-flight messages.
"""

import uuid

import pytest
from agent_framework.exceptions import WorkflowCheckpointException
from s037probe.flow import Marker
from s037probe.rig import (
    Rig,
    continue_from,
    describe,
    latest_sync,
    leg,
    resume,
    start,
)


def completed_rig(dsn: str, thread_id: uuid.UUID) -> Rig:
    rig = Rig(dsn, thread_id)
    start(rig)
    resume(rig, {})
    return rig


def test_a_run_that_never_started_has_no_checkpoint_to_read(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)

    assert latest_sync(rig) is None
    # The framework's own answer to an address nobody saved is a checkpoint
    # exception from the store's ``load``, raised before any step runs.
    deps = rig.deps()
    workflow = rig.workflow(deps)
    with pytest.raises(WorkflowCheckpointException, match="No checkpoint found"):
        leg(
            lambda: workflow.run(
                responses={"unknown": Marker()}, checkpoint_id=str(uuid.uuid4())
            )
        )
    assert dict(deps.counts) == {}


def test_a_run_of_another_thread_is_a_run_that_never_started(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    other = Rig(scratch_schema, uuid.uuid4())
    start(other)

    mine = Rig(scratch_schema, thread_id)

    assert latest_sync(other) is not None
    assert latest_sync(mine) is None


def test_a_run_that_completed_has_a_latest_checkpoint_with_nothing_pending(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = completed_rig(scratch_schema, thread_id)

    latest = latest_sync(rig)

    assert latest is not None
    assert latest.pending_request_info_events == {}
    assert latest.messages == {}


def test_a_resume_of_a_completed_run_raises_a_runtime_error_and_runs_nothing(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = completed_rig(scratch_schema, thread_id)
    deps = rig.deps()

    # Sent the way the framework wants it (a response keyed by a request ID):
    # there is none to key it by, so the host has to invent one.
    workflow = rig.workflow(deps)
    latest = latest_sync(rig)
    with pytest.raises(RuntimeError, match="No pending requests found"):
        leg(
            lambda: workflow.run(
                responses={"made-up": Marker()}, checkpoint_id=latest.checkpoint_id
            )
        )

    assert dict(deps.counts) == {}


def test_a_continue_from_a_completed_run_is_a_silent_no_op_not_an_error(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = completed_rig(scratch_schema, thread_id)
    latest = latest_sync(rig)

    deps, result = continue_from(rig, latest.checkpoint_id)

    # The trap of a host that resumes "from the latest checkpoint" without
    # looking at it: a completed run is restored and nothing happens.
    assert describe(result)["outputs"] == []
    assert describe(result)["paused"] is False
    assert dict(deps.counts) == {}


def test_a_second_resume_of_an_answered_pause_is_the_completed_case(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    resume(rig, {})

    deps = rig.deps()
    with pytest.raises(RuntimeError, match="No pending requests found"):
        resume(rig, {}, deps)

    assert dict(deps.counts) == {}


def test_a_resume_that_names_the_old_pause_checkpoint_answers_it_twice(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    # Not a "nothing to resume" case: the framework allows it (the first spike
    # saw the same), so a host that addresses a pause by its checkpoint ID
    # instead of "the latest" would let a second answer through. Resume-once is
    # the runtime's conditional update, taken before the host is called.
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    pause = latest_sync(rig)
    resume(rig, {})

    deps, result = resume(rig, {}, checkpoint_id=pause.checkpoint_id)

    assert describe(result)["outputs"] == [
        {"brief": "a synthetic brief", "filed": True}
    ]
    assert dict(deps.counts) == {"answered": 1, "file": 1}
