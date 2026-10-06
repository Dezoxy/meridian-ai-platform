"""Probe 8: a bound on steps. The framework counts supersteps (a round in which
every executor with a message runs once), not executors, and it counts across a
resume. The runtime's bound for LangGraph is ten steps a leg."""

import uuid

import pytest
from agent_framework import (
    Executor,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)
from agent_framework._workflows._const import DEFAULT_MAX_ITERATIONS
from agent_framework.exceptions import WorkflowConvergenceException
from s037probe.rig import Rig, describe, latest_sync, leg, resume, start


class Loop(Executor):
    """Sends itself a message each time it runs: a workflow that never ends."""

    def __init__(self) -> None:
        super().__init__(id="loop")
        self.runs = 0

    @handler
    async def run(self, n: int, ctx: WorkflowContext[int]) -> None:
        self.runs += 1
        await ctx.send_message(n + 1)


def looping(max_iterations: int | None = None) -> tuple[Workflow, Loop]:
    node = Loop()
    extra = {} if max_iterations is None else {"max_iterations": max_iterations}
    workflow = (
        WorkflowBuilder(name="loop", start_executor=node, **extra)
        .add_edge(node, node)
        .build()
    )
    return workflow, node


def test_the_default_bound_is_one_hundred_supersteps() -> None:
    assert DEFAULT_MAX_ITERATIONS == 100
    workflow, node = looping()

    with pytest.raises(WorkflowConvergenceException) as raised:
        leg(lambda: workflow.run(0))

    assert node.runs == 100
    assert str(raised.value) == "Runner did not converge after 100 iterations."


def test_a_bound_of_ten_stops_a_loop_after_ten_steps_with_a_convergence_exception() -> (
    None
):
    workflow, node = looping(max_iterations=10)

    with pytest.raises(WorkflowConvergenceException) as raised:
        leg(lambda: workflow.run(0))

    # Its own type, from workflow.run, with a text that holds no payload.
    assert type(raised.value) is WorkflowConvergenceException
    assert node.runs == 10
    assert str(raised.value) == "Runner did not converge after 10 iterations."


def test_the_probe_workflow_needs_three_supersteps_to_pause_and_two_to_finish(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)

    start(rig, rig.deps())
    pause = latest_sync(rig)
    resume(rig, {})
    final = latest_sync(rig)

    assert pause.iteration_count == 3
    assert final.iteration_count == 5


def test_the_count_carries_over_a_resume_so_the_bound_is_not_per_leg(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    # A new workflow for the resume, with a bound of four: the start leg used
    # three, the resume leg needs two more.
    deps = rig.deps()
    workflow = rig.workflow(deps, max_iterations=4)
    stored = latest_sync(rig)

    with pytest.raises(WorkflowConvergenceException):
        leg(
            lambda: workflow.run(
                responses={r: {} for r in stored.pending_request_info_events},
                checkpoint_id=stored.checkpoint_id,
            )
        )

    # The response handler ran (superstep four); the step after it did not.
    assert dict(deps.counts) == {"answered": 1}


def test_a_bound_set_for_the_resume_leg_from_the_checkpoints_count_is_per_leg(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    stored = latest_sync(rig)
    deps = rig.deps()
    # The host builds the resume leg's workflow with the count it restores plus
    # the leg's own allowance; the bound is not part of the graph's signature,
    # so a different bound than the start leg's is accepted.
    workflow = rig.workflow(deps, max_iterations=stored.iteration_count + 2)

    result = leg(
        lambda: workflow.run(
            responses={r: {} for r in stored.pending_request_info_events},
            checkpoint_id=stored.checkpoint_id,
        )
    )

    assert describe(result)["outputs"] == [
        {"brief": "a synthetic brief", "filed": True}
    ]
    assert dict(deps.counts) == {"answered": 1, "file": 1}


def test_a_start_leg_over_its_bound_fails_before_the_pause_is_reached(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    deps = rig.deps()
    workflow = rig.workflow(deps, max_iterations=2)

    with pytest.raises(WorkflowConvergenceException):
        leg(lambda: workflow.run("CLM-0001"))

    assert dict(deps.counts) == {"gather": 1, "draft": 1}
