"""The second host's legs against PostgreSQL and the real clients (S037, R4a):
a start that pauses, a resume in a host built from nothing (as a new process
would have), the three ways of a resume, the output, and the failure words.

The tool client talks to the real policy and claims servers in-process; the
model client to a stub gateway. Every leg runs in a thread of its own with a
time limit, as the runtime's legs do.
"""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from hostflows import (
    Dials,
    brief_factory,
    raising_factory,
    two_pauses_factory,
    yield_factory,
)
from hostsupport import CLAIM, BriefWorld, in_leg_thread, make_world
from servicesupport import owner_rows
from toolsupport import tracer_of

from meridian.runtime.agent_framework_host import AgentFrameworkHost, WorkflowFactory
from meridian.runtime.failures import GraphFailure, failure_reason
from meridian.runtime.hosts import Host
from meridian.runtime.runs import (
    NO_PENDING_PAUSE,
    SEVERAL_PENDING_PAUSES,
    RunOutcome,
)

CANARY = "canary-claim-text-4e7b"
THE_BRIEF = "drafted"
FIRST_OUTPUT = {"brief": THE_BRIEF}
FINAL_OUTPUT = {"brief": THE_BRIEF, "filed": True}
START_INPUT = {"claim_id": CLAIM}
COUNT = "SELECT count(*) FROM runtime.workflow_checkpoints WHERE thread_id = %s"
LINEAGE = (
    "SELECT checkpoint_id, body->>'previous_checkpoint_id' "
    "FROM runtime.workflow_checkpoints WHERE thread_id = %s"
)
NOTES = "SELECT count(*) FROM claims.notes"


@pytest.fixture
def world(fresh_database: DatabaseHandle, plant: Callable[..., Path]) -> BriefWorld:
    return make_world(fresh_database, plant)


def host_over(world: BriefWorld, factory: WorkflowFactory) -> AgentFrameworkHost:
    return AgentFrameworkHost(factory, dsn=world.dsn())


def start(
    host: Host,
    world: BriefWorld,
    run_input: dict[str, Any] | None = None,
    **limits: int,
) -> RunOutcome:
    return in_leg_thread(
        lambda: host.start(
            world.identity,
            world.model(max_calls=limits.get("model_calls", 4)),
            world.tools(max_calls=limits.get("tool_calls", 16)),
            tracer_of(world.exporter),
            START_INPUT if run_input is None else run_input,
        )
    )


def resume(
    host: Host, world: BriefWorld, value: dict[str, Any] | None = None
) -> RunOutcome:
    return in_leg_thread(
        lambda: host.resume(
            world.identity,
            world.model(),
            world.tools(),
            tracer_of(world.exporter),
            {} if value is None else value,
        )
    )


def forget(host: Host, world: BriefWorld) -> None:
    in_leg_thread(lambda: host.forget(world.identity))


def checkpoint_rows(world: BriefWorld) -> int:
    return owner_rows(world.db, COUNT, (str(world.thread_id),))[0][0]


def notes(world: BriefWorld) -> int:
    return owner_rows(world.db, NOTES)[0][0]


def ran(dials: Dials) -> dict[str, int]:
    return dict(dials.counts)


# ── a start ─────────────────────────────────────────────────────────────────
def test_a_start_that_pauses_answers_awaiting_approval_with_the_output_so_far(
    world: BriefWorld,
) -> None:
    host = host_over(world, brief_factory(Dials()))

    outcome = start(host, world)

    assert outcome == RunOutcome("AwaitingApproval", FIRST_OUTPUT)
    assert checkpoint_rows(world) > 0


def test_the_checkpoints_of_a_pause_are_one_lineage_in_the_runtime_s_own_table(
    world: BriefWorld,
) -> None:
    start(host_over(world, brief_factory(Dials())), world)

    rows = owner_rows(world.db, LINEAGE, (str(world.thread_id),))

    ids = {checkpoint_id for checkpoint_id, _ in rows}
    parents = [parent for _, parent in rows]
    assert len(ids) == len(rows) >= 2
    assert parents.count(None) == 1
    assert all(parent in ids for parent in parents if parent is not None)
    assert len({parent for parent in parents if parent is not None}) == len(rows) - 1


def test_a_start_that_runs_to_the_end_answers_completed_with_its_last_output(
    world: BriefWorld,
) -> None:
    host = host_over(world, yield_factory({"step": 1}, {"step": 2}))

    outcome = start(host, world)

    assert outcome == RunOutcome("Completed", {"step": 2})


def test_a_workflow_that_yields_nothing_has_no_output(world: BriefWorld) -> None:
    host = host_over(world, yield_factory())

    assert start(host, world) == RunOutcome("Completed", None)


def test_the_output_is_the_last_one_yielded_and_it_must_be_an_object(
    world: BriefWorld,
) -> None:
    for values in (["a list"], [{"fine": True}, "text"], [7]):
        host = host_over(world, yield_factory(*values))

        with pytest.raises(TypeError, match="output must be an object"):
            start(host, world)


# ── a resume ────────────────────────────────────────────────────────────────
def test_a_resume_in_a_host_built_from_nothing_completes_and_forget_removes_the_rows(
    world: BriefWorld,
) -> None:
    first_dials = Dials()
    start(host_over(world, brief_factory(first_dials)), world)
    second_dials = Dials()
    second = host_over(world, brief_factory(second_dials))

    outcome = resume(second, world)

    assert outcome == RunOutcome("Completed", FINAL_OUTPUT)
    # A new host ran only what follows the pause.
    assert ran(second_dials) == {"answered": 1, "file": 1, "file_done": 1}
    assert checkpoint_rows(world) > 0
    forget(second, world)
    assert checkpoint_rows(world) == 0


def test_a_resumed_leg_s_output_is_its_own_not_the_first_legs(
    world: BriefWorld,
) -> None:
    start(host_over(world, brief_factory(Dials())), world)

    outcome = resume(host_over(world, brief_factory(Dials(quiet=True))), world)

    # The first leg yielded the brief; this one yields nothing, so it has no
    # output: the first leg's is not carried into it.
    assert outcome == RunOutcome("Completed", None)


def test_the_value_a_resume_carries_is_ignored_and_stored_nowhere(
    world: BriefWorld,
) -> None:
    host = host_over(world, brief_factory(Dials()))
    start(host, world)
    hostile = {"__pickled__": "x", "decision": "approve", "note": CANARY}

    outcome = resume(host, world, hostile)

    assert outcome == RunOutcome("Completed", FINAL_OUTPUT)
    held = owner_rows(
        world.db,
        "SELECT count(*) FROM runtime.workflow_checkpoints "
        "WHERE body::text LIKE %s OR body::text LIKE '%%__pickled__%%'",
        (f"%{CANARY}%",),
    )
    assert held[0][0] == 0


def test_a_step_that_fails_after_the_answer_is_re_opened_and_writes_once(
    world: BriefWorld,
) -> None:
    first = Dials(fail_in={"file_done"}, message=CANARY)
    replays: list[bool] = []
    start(host_over(world, brief_factory(first, replays)), world)

    with pytest.raises(Exception, match=CANARY):
        resume(host_over(world, brief_factory(first, replays)), world)
    after_failure = (ran(first), notes(world), checkpoint_rows(world))
    second = Dials()
    outcome = resume(host_over(world, brief_factory(second, replays)), world)

    # The answer handler ran once; the step after it twice. The note was
    # written by the first run of the step and replayed by the second, under
    # the same key, so the table holds one.
    assert after_failure[0]["answered"] == 1
    assert ran(second) == {"file": 1, "file_done": 1}
    assert replays == [False, True]
    assert notes(world) == after_failure[1] == 1
    assert outcome == RunOutcome("Completed", FINAL_OUTPUT)


def test_a_step_that_keeps_failing_can_be_re_opened_again_each_time(
    world: BriefWorld,
) -> None:
    dials = Dials(fail_in={"file"})
    host = host_over(world, brief_factory(dials))
    start(host, world)

    for _ in range(2):
        with pytest.raises(Exception, match="file"):
            resume(host, world)
    dials.fail_in.clear()
    outcome = resume(host, world)

    assert outcome == RunOutcome("Completed", FINAL_OUTPUT)
    assert ran(dials)["answered"] == 1
    assert ran(dials)["file"] == 3


@pytest.mark.parametrize("how", ["finished", "forgotten", "never started"])
def test_a_resume_with_nothing_to_resume_fails_before_anything_runs(
    world: BriefWorld, how: str
) -> None:
    dials = Dials()
    host = host_over(world, brief_factory(dials))
    if how != "never started":
        start(host, world)
        resume(host, world)
    if how == "forgotten":
        forget(host, world)
    before = (ran(dials), len(world.gateway.seen), notes(world))

    with pytest.raises(GraphFailure) as raised:
        resume(host, world)

    assert raised.value.code == NO_PENDING_PAUSE
    assert failure_reason(raised.value) == "no-pending-pause"
    assert (ran(dials), len(world.gateway.seen), notes(world)) == before


def test_a_resume_that_is_repeated_after_it_completed_has_nothing_to_resume(
    world: BriefWorld,
) -> None:
    host = host_over(world, brief_factory(Dials()))
    start(host, world)
    resume(host, world)

    with pytest.raises(GraphFailure) as raised:
        resume(host, world)

    assert raised.value.code == NO_PENDING_PAUSE
    assert notes(world) == 1


def test_a_resume_that_finds_several_pauses_fails_before_anything_runs(
    world: BriefWorld,
) -> None:
    host = host_over(world, two_pauses_factory())
    assert start(host, world).status == "AwaitingApproval"

    with pytest.raises(GraphFailure) as raised:
        resume(host, world)

    assert raised.value.code == SEVERAL_PENDING_PAUSES


def test_a_leg_never_deletes_the_checkpoints_that_is_the_callers_forget(
    world: BriefWorld,
) -> None:
    host = host_over(world, brief_factory(Dials()))
    start(host, world)
    resume(host, world)

    assert checkpoint_rows(world) > 0


def test_a_host_built_for_another_run_never_sees_this_runs_checkpoints(
    world: BriefWorld,
) -> None:
    host = host_over(world, brief_factory(Dials()))
    start(host, world)
    other = BriefWorld(**{**vars(world), "thread_id": uuid.uuid4()})

    with pytest.raises(GraphFailure) as raised:
        resume(host, other)

    assert raised.value.code == NO_PENDING_PAUSE
    assert checkpoint_rows(other) == 0
    assert checkpoint_rows(world) > 0


# ── what a failed leg is called ─────────────────────────────────────────────
def test_a_client_s_failure_leaves_the_leg_with_its_own_type_and_word(
    world: BriefWorld,
) -> None:
    host = host_over(world, brief_factory(Dials()))
    cases: list[tuple[str, Callable[[], None], dict[str, int]]] = [
        ("model-call-limit", lambda: None, {"model_calls": 0}),
        ("tool-call-limit", lambda: None, {"tool_calls": 1}),
        ("model-error", lambda: setattr(world.gateway, "status", 500), {}),
        ("tool-refused", lambda: world.set_run_status("Completed"), {}),
    ]

    for word, arrange, limits in cases:
        arrange()
        with pytest.raises(Exception) as raised:
            start(host, world, **limits)
        assert failure_reason(raised.value) == word, word
        world.gateway.status = 200
        world.set_run_status("Running")


def test_a_tool_server_the_client_has_no_address_for_is_unavailable(
    world: BriefWorld,
) -> None:
    host = host_over(world, brief_factory(Dials()))

    with pytest.raises(Exception) as raised:
        in_leg_thread(
            lambda: host.start(
                world.identity,
                world.model(),
                world.tools(servers={}),
                tracer_of(world.exporter),
                START_INPUT,
            )
        )

    assert failure_reason(raised.value) == "tool-unavailable"


def test_a_step_s_own_graph_failure_keeps_its_code_and_anything_else_is_unexpected(
    world: BriefWorld,
) -> None:
    with pytest.raises(GraphFailure) as own:
        start(host_over(world, raising_factory(GraphFailure("brief-too-long"))), world)
    with pytest.raises(RuntimeError) as other:
        start(host_over(world, raising_factory(RuntimeError(CANARY))), world)

    assert failure_reason(own.value) == "brief-too-long"
    assert failure_reason(other.value) == "unexpected"
