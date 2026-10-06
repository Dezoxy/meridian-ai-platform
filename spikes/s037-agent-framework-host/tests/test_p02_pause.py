"""Probe 2: the pause. ``request_info`` from an ``Executor`` class with a
``@response_handler``; what ``workflow.run`` returns; how a pause is detected."""

import uuid

from agent_framework import (
    InMemoryCheckpointStorage,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowRunState,
    executor,
)
from s037probe.flow import CLAIM, WORKFLOW_NAME, AskRequest, Marker
from s037probe.rig import Rig, all_checkpoints, leg, start


def test_a_pause_returns_a_result_with_one_request_event_and_no_output(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)

    deps = rig.deps()
    _, result = start(rig, deps)

    # workflow.run returns (it does not raise and does not wait) and says why.
    (request,) = result.get_request_info_events()
    assert result.get_outputs() == []
    assert result.get_final_state() == WorkflowRunState.IDLE_WITH_PENDING_REQUESTS
    assert isinstance(request.request_id, str)
    assert request.data == AskRequest(CLAIM, "a synthetic brief")
    assert request.response_type is Marker
    assert request.source_executor_id == "ask"
    assert dict(deps.counts) == {"gather": 1, "draft": 1, "ask": 1}


def test_the_pause_has_a_checkpoint_the_framework_names_and_the_store_holds(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)

    workflow, result = start(rig)
    ids = [event.request_id for event in result.get_request_info_events()]
    pause_id = leg(lambda: workflow.resolve_pause_checkpoint_id(ids))

    assert pause_id is not None
    stored = all_checkpoints(rig)
    # The pause checkpoint is the last one written, and the only one that holds
    # the request (the earlier ones are the entry and two supersteps).
    assert stored[-1].checkpoint_id == pause_id
    assert list(stored[-1].pending_request_info_events) == ids
    assert [len(c.pending_request_info_events) for c in stored] == [0, 0, 0, 1]
    assert [c.iteration_count for c in stored] == [0, 1, 2, 3]
    assert workflow.get_last_checkpoint_id() == pause_id


def test_a_function_executor_can_pause_but_its_answer_is_dropped() -> None:
    @executor(id="fn")
    async def fn(claim_id: str, ctx: WorkflowContext[str, str]) -> None:
        await ctx.request_info(AskRequest(claim_id, "brief"), Marker)

    storage = InMemoryCheckpointStorage()

    def build() -> Workflow:
        return WorkflowBuilder(
            name=WORKFLOW_NAME, start_executor=fn, checkpoint_storage=storage
        ).build()

    first = build()
    result = leg(lambda: first.run(CLAIM))
    (request,) = result.get_request_info_events()
    latest = leg(lambda: storage.get_latest(workflow_name=WORKFLOW_NAME))
    assert latest is not None

    second = build()
    resumed = leg(
        lambda: second.run(
            responses={request.request_id: Marker()},
            checkpoint_id=latest.checkpoint_id,
        )
    )

    # The pause worked; the answer went nowhere: no output, no error.
    assert resumed.get_outputs() == []
    assert not resumed.get_request_info_events()
