"""Probe 5: a step fails after the resume (the case that changed S031's design).

The design says a failed resume leaves the run resumable: a second resume
answers the pause again and the failed step runs again. Measured: the pause is
CONSUMED in the sense that matters to ``responses=`` (the framework says "No
pending requests"), but the run is not lost: the latest checkpoint holds the
response in flight, and a restore with no responses runs on from it.
"""

import uuid

import pytest
from s037probe.flow import StepFailure
from s037probe.rig import (
    Rig,
    all_checkpoints,
    continue_from,
    describe,
    latest_sync,
    resume,
    start,
)

BRIEF = [{"brief": "a synthetic brief", "filed": True}]


def paused_rig(dsn: str, thread_id: uuid.UUID) -> Rig:
    rig = Rig(dsn, thread_id)
    start(rig)
    return rig


def failed_resume(rig: Rig, step: str) -> dict[str, int]:
    """A resume whose ``step`` raises; the counts of what ran."""
    deps = rig.deps()
    deps.fail_in.add(step)
    with pytest.raises(StepFailure) as raised:
        resume(rig, {}, deps)
    assert type(raised.value) is StepFailure
    return dict(deps.counts)


def test_a_step_after_the_response_handler_fails_and_the_response_is_in_flight(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = paused_rig(scratch_schema, thread_id)

    counts = failed_resume(rig, "file")

    assert counts == {"answered": 1, "file": 1}
    latest = latest_sync(rig)
    # The superstep that ran the response handler was checkpointed with its
    # message to the failing step still in flight; nothing is pending.
    assert latest.pending_request_info_events == {}
    assert list(latest.messages) == ["ask"]
    assert [c.iteration_count for c in all_checkpoints(rig)] == [0, 1, 2, 3, 3, 4]


def test_the_pause_is_consumed_for_a_second_resume_that_answers_it_again(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = paused_rig(scratch_schema, thread_id)
    failed_resume(rig, "file")
    deps = rig.deps()

    with pytest.raises(RuntimeError, match="No pending requests found"):
        resume(rig, {}, deps)

    # Contradicts the design as written: "answer again, the step runs again"
    # does not hold for ``responses=`` from the latest checkpoint.
    assert dict(deps.counts) == {}


def test_a_restore_of_the_latest_checkpoint_runs_only_the_failed_step_again(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = paused_rig(scratch_schema, thread_id)
    first = failed_resume(rig, "file")
    latest = latest_sync(rig)

    deps, result = continue_from(rig, latest.checkpoint_id)

    assert describe(result)["outputs"] == BRIEF
    # Across start, the failed resume and the second leg: the response handler
    # ran once, the failing step twice.
    assert first == {"answered": 1, "file": 1}
    assert dict(deps.counts) == {"file": 1}


def test_the_replay_of_the_pause_checkpoint_runs_every_step_after_it_again(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = paused_rig(scratch_schema, thread_id)
    pause = latest_sync(rig)
    failed_resume(rig, "file")

    deps, result = resume(rig, {}, checkpoint_id=pause.checkpoint_id)

    # The other way to re-open it, and the costlier: the response handler runs
    # a second time. It also branches the lineage from the pause checkpoint.
    assert describe(result)["outputs"] == BRIEF
    assert dict(deps.counts) == {"answered": 1, "file": 1}


def test_a_failure_inside_the_response_handler_is_re_opened_the_same_way(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = paused_rig(scratch_schema, thread_id)

    first = failed_resume(rig, "answered")
    latest = latest_sync(rig)
    deps, result = continue_from(rig, latest.checkpoint_id)

    assert first == {"answered": 1}
    # The latest checkpoint is the response-entry one: the response, in flight.
    assert list(latest.messages) == ["internal:ask"]
    assert describe(result)["outputs"] == BRIEF
    assert dict(deps.counts) == {"answered": 1, "file": 1}


def test_a_failure_on_the_second_try_leaves_the_run_re_openable_again(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = paused_rig(scratch_schema, thread_id)
    failed_resume(rig, "file")
    latest = latest_sync(rig)

    deps = rig.deps()
    deps.fail_in.add("file")
    with pytest.raises(StepFailure):
        continue_from(rig, latest.checkpoint_id, deps)
    again = latest_sync(rig)
    _, result = continue_from(rig, latest.checkpoint_id)

    # A restore writes no checkpoint of its own before its first superstep ends,
    # so the latest one did not move and the third try completes the run.
    assert again.checkpoint_id == latest.checkpoint_id
    assert describe(result)["outputs"] == BRIEF


def test_the_latest_checkpoint_tells_the_host_which_call_to_make(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    # The runtime's run row says "AwaitingApproval" for a pause and for a pause
    # whose resume failed; the store says which call the host makes.
    def shape(rig: Rig) -> tuple[int, list[str]]:
        latest = latest_sync(rig)
        return len(latest.pending_request_info_events), list(latest.messages)

    rig = paused_rig(scratch_schema, thread_id)
    at_pause = shape(rig)
    failed_resume(rig, "file")
    after_failure = shape(rig)
    continue_from(rig, latest_sync(rig).checkpoint_id)
    done = shape(rig)

    assert at_pause == (1, [])  # send the response: responses= from the latest
    assert after_failure == (0, ["ask"])  # run on: restore with no responses
    assert done == (0, [])  # nothing to resume: no-pending-pause
