"""Probe 10: one run per ``Workflow`` object. The first spike's note was about a
concurrent run; a second run on the same object is allowed, and carries the
first one's state and checkpoint lineage with it. So the host builds a workflow
for each leg: how long that takes is printed, not asserted."""

import logging
import time
import uuid
from collections.abc import Callable

import pytest
from agent_framework import InMemoryCheckpointStorage, WorkflowException
from s037probe.flow import CLAIM, WORKFLOW_NAME, build_workflow
from s037probe.rig import Rig, all_checkpoints, leg, plain_deps

BUILDS = 50


def test_a_second_run_on_the_same_object_after_a_pause_runs_again_with_a_warning(
    scratch_schema: str, thread_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(scratch_schema, thread_id)
    deps = rig.deps()
    workflow = rig.workflow(deps)
    leg(lambda: workflow.run(CLAIM))

    with caplog.at_level(logging.WARNING):
        second = leg(lambda: workflow.run(CLAIM))

    assert len(second.get_request_info_events()) == 1
    assert dict(deps.counts) == {"gather": 2, "draft": 2, "ask": 2}
    assert any(
        "received a fresh message while 1 request_info event(s) are still pending"
        in r.getMessage()
        for r in caplog.records
    )


def test_a_second_run_after_completion_works_and_joins_the_first_ones_lineage(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    workflow = rig.workflow()
    first = leg(lambda: workflow.run(CLAIM))
    request_ids = [e.request_id for e in first.get_request_info_events()]
    leg(lambda: workflow.run(responses={rid: {} for rid in request_ids}))
    after_first = all_checkpoints(rig)

    second = leg(lambda: workflow.run(CLAIM))

    assert len(second.get_request_info_events()) == 1
    stored = all_checkpoints(rig)
    entry_of_second = stored[len(after_first)]
    # The second run's entry checkpoint is parented on the first run's last one:
    # one chain for two runs, in one thread's rows.
    assert entry_of_second.iteration_count == 0
    assert entry_of_second.previous_checkpoint_id == after_first[-1].checkpoint_id
    assert {c.workflow_name for c in stored} == {WORKFLOW_NAME}


def test_a_second_run_while_the_first_is_not_finished_is_refused() -> None:
    workflow = build_workflow(plain_deps(), InMemoryCheckpointStorage())

    async def go() -> None:
        first = workflow.run(CLAIM)
        with pytest.raises(WorkflowException, match="already running"):
            workflow.run(CLAIM)
        await first

    leg(go)


def test_building_a_workflow_for_a_leg_is_a_new_object_and_costs_this_much(
    record_property: Callable[[str, object], None],
) -> None:
    deps = plain_deps()
    storage = InMemoryCheckpointStorage()

    started = time.perf_counter()
    built = [build_workflow(deps, storage) for _ in range(BUILDS)]
    mean_ms = (time.perf_counter() - started) / BUILDS * 1000

    print(f"build_workflow: {mean_ms:.2f} ms each, mean of {BUILDS}")
    record_property("build_ms", f"{mean_ms:.2f}")
    assert len({id(w) for w in built}) == BUILDS
