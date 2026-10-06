"""What stops or fails a leg of the second host (S037, R4a): the step bound on a
start and on a resume, a save the table refuses, a store that cannot be reached
or read, and a stored document the codec refuses. Each failure is a fixed word,
and no log line or error text holds claim content or a driver's text."""

import base64
import logging
import pickle
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from hostflows import Dials, brief_factory, chain_factory, loop_factory, yield_factory
from hostsupport import AGENT, BriefWorld, in_leg_thread, make_world
from psycopg.types.json import Jsonb
from servicesupport import owner_rows
from toolsupport import tracer_of, unused_port

from meridian.runtime.agent_framework_host import AgentFrameworkHost, WorkflowFactory
from meridian.runtime.failures import GraphFailure, failure_reason
from meridian.runtime.runs import RECURSION_LIMIT, RunOutcome

CANARY = "canary-claim-text-91d2"
START_INPUT = {"claim_id": "CLM-0001"}
COUNT = "SELECT count(*) FROM runtime.workflow_checkpoints WHERE thread_id = %s"


@pytest.fixture
def world(fresh_database: DatabaseHandle, plant: Callable[..., Path]) -> BriefWorld:
    return make_world(fresh_database, plant)


def host_over(
    world: BriefWorld, factory: WorkflowFactory, dsn: str | None = None
) -> AgentFrameworkHost:
    return AgentFrameworkHost(factory, dsn=dsn or world.dsn())


def start(host: AgentFrameworkHost, world: BriefWorld) -> RunOutcome:
    return in_leg_thread(
        lambda: host.start(
            world.identity,
            world.model(),
            world.tools(),
            tracer_of(world.exporter),
            START_INPUT,
        )
    )


def resume(host: AgentFrameworkHost, world: BriefWorld) -> RunOutcome:
    return in_leg_thread(
        lambda: host.resume(
            world.identity, world.model(), world.tools(), tracer_of(world.exporter), {}
        )
    )


def rows(world: BriefWorld) -> int:
    return owner_rows(world.db, COUNT, (str(world.thread_id),))[0][0]


def refuse_saves_from(db: DatabaseHandle, iteration: int) -> None:
    """Make the table refuse a checkpoint at or past ``iteration`` steps."""
    with psycopg.connect(db.dsn(OWNER), autocommit=True) as conn:
        conn.execute(
            "ALTER TABLE runtime.workflow_checkpoints ADD CONSTRAINT limited "
            f"CHECK ((body->>'iteration_count')::int < {int(iteration)})"
        )


# ── the step bound ──────────────────────────────────────────────────────────
def test_a_workflow_that_never_converges_stops_after_ten_steps_on_a_start(
    world: BriefWorld,
) -> None:
    dials = Dials()
    host = host_over(world, loop_factory(dials))

    with pytest.raises(GraphFailure) as raised:
        start(host, world)

    assert raised.value.code == "step-limit"
    assert failure_reason(raised.value) == "step-limit"
    assert dials.counts["loop"] == RECURSION_LIMIT
    # The framework's own sentence, with its count, is not carried along.
    assert str(raised.value) == "step-limit"
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


def test_the_bound_is_ten_steps_a_leg_on_both_sides_of_the_boundary(
    world: BriefWorld,
) -> None:
    within = Dials()
    over = Dials()

    # Nine relays and the pause are ten steps; ten relays and the pause eleven.
    paused = start(host_over(world, chain_factory(within, before=9, after=0)), world)
    with pytest.raises(GraphFailure) as raised:
        start(host_over(world, chain_factory(over, before=10, after=0)), world)

    assert paused.status == "AwaitingApproval"
    assert raised.value.code == "step-limit"
    # Ten relays ran, and the eleventh step (the pause) was not let begin.
    assert (over.counts["before-9"], over.counts["pause"]) == (1, 0)
    assert within.counts["pause"] == 1


def test_a_resumed_leg_has_ten_steps_of_its_own_whatever_the_first_leg_used(
    world: BriefWorld,
) -> None:
    dials = Dials()
    host = host_over(world, chain_factory(dials, before=6, after=5))
    assert start(host, world).status == "AwaitingApproval"

    outcome = resume(host, world)

    # Seven steps to the pause and seven after it: fourteen in all, which the
    # framework's cumulative count alone would have stopped.
    assert outcome == RunOutcome("Completed", {"done": True})
    assert dials.counts["finish"] == 1


def test_a_resumed_leg_that_never_converges_stops_ten_steps_after_the_pause(
    world: BriefWorld,
) -> None:
    dials = Dials()
    host = host_over(world, chain_factory(dials, before=4, after=0, loop_after=True))
    start(host, world)

    with pytest.raises(GraphFailure) as raised:
        resume(host, world)

    # The answer handler is the leg's first step; the loop has the other nine.
    assert raised.value.code == "step-limit"
    assert dials.counts["answered"] == 1
    assert dials.counts["loop"] == RECURSION_LIMIT - 1


# ── a save the table refuses ────────────────────────────────────────────────
def test_a_pause_whose_save_the_table_refuses_is_a_failed_leg_not_a_pause(
    world: BriefWorld, caplog: pytest.LogCaptureFixture
) -> None:
    world.gateway.reply = {
        **world.gateway.reply,
        "output": {"text": CANARY, "finish_reason": "stop"},
    }
    refuse_saves_from(world.db, 3)  # the pause is the third step's checkpoint
    host = host_over(world, brief_factory(Dials()))

    with caplog.at_level(logging.DEBUG), pytest.raises(GraphFailure) as raised:
        start(host, world)

    assert raised.value.code == "checkpoint-not-saved"
    assert failure_reason(raised.value) == "checkpoint-not-saved"
    # What did save is there (the entry and the first two steps), and the pause
    # is not: the run is not resumable.
    assert rows(world) == 3
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "CheckViolation" in text
    assert CANARY not in text
    assert CANARY not in " ".join(map(str, vars(raised.value).values()))


def test_a_workflow_that_ran_to_the_end_is_failed_too_when_a_save_was_refused(
    world: BriefWorld,
) -> None:
    refuse_saves_from(world.db, 1)
    host = host_over(world, yield_factory({"done": True}))

    with pytest.raises(GraphFailure) as raised:
        start(host, world)

    assert raised.value.code == "checkpoint-not-saved"
    assert rows(world) == 1  # the entry's, before any step ran


def test_a_leg_whose_first_save_is_refused_does_not_go_on_to_write(
    world: BriefWorld,
) -> None:
    refuse_saves_from(world.db, 1)
    dials = Dials()
    host = host_over(world, brief_factory(dials))

    with pytest.raises(GraphFailure):
        start(host, world)

    # Stopped after the step whose checkpoint was refused: nothing was asked of
    # an adjuster, and the run's one write tool was not called.
    assert dict(dials.counts) == {"gather": 1}


def test_a_table_that_refuses_even_the_entry_checkpoint_stops_the_leg_before_any_step(
    world: BriefWorld,
) -> None:
    refuse_saves_from(world.db, 0)
    dials = Dials()
    host = host_over(world, brief_factory(dials))

    with pytest.raises(GraphFailure) as raised:
        start(host, world)

    assert raised.value.code == "checkpoint-not-saved"
    assert dict(dials.counts) == {}
    assert rows(world) == 0


# ── a store that cannot be reached, or read ─────────────────────────────────
def unreachable(world: BriefWorld) -> tuple[str, int]:
    port = unused_port()
    return f"postgresql://agent_runtime@127.0.0.1:{port}/meridian", port


def test_a_store_that_cannot_be_reached_fails_the_leg_with_its_class_name_only(
    world: BriefWorld, caplog: pytest.LogCaptureFixture
) -> None:
    dsn, port = unreachable(world)
    host = host_over(world, brief_factory(Dials()), dsn)

    with caplog.at_level(logging.DEBUG), pytest.raises(GraphFailure) as raised:
        start(host, world)

    assert raised.value.code == "checkpoint-not-saved"
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "OperationalError" in text
    assert str(port) not in text
    assert "127.0.0.1" not in text


def test_a_resume_that_cannot_read_its_checkpoints_fails_before_anything_runs(
    world: BriefWorld, caplog: pytest.LogCaptureFixture
) -> None:
    dsn, port = unreachable(world)
    dials = Dials()
    host = host_over(world, brief_factory(dials), dsn)

    with caplog.at_level(logging.DEBUG), pytest.raises(GraphFailure) as raised:
        resume(host, world)

    assert raised.value.code == "checkpoint-not-read"
    assert failure_reason(raised.value) == "checkpoint-not-read"
    assert dict(dials.counts) == {}
    assert str(port) not in " ".join(r.getMessage() for r in caplog.records)


def test_a_forget_that_cannot_reach_the_store_raises_without_the_address(
    world: BriefWorld,
) -> None:
    dsn, port = unreachable(world)
    host = host_over(world, brief_factory(Dials()), dsn)

    with pytest.raises(Exception) as raised:
        in_leg_thread(lambda: host.forget(world.identity))

    assert str(port) not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None


class Detonator:
    """A pickle that writes a file when it is loaded."""

    def __init__(self, path: str) -> None:
        self.path = path

    def __reduce__(self) -> tuple[Any, ...]:
        return (Path(self.path).write_text, ("loaded",))


def test_a_stored_document_that_names_a_pickle_is_refused_and_nothing_loads_it(
    world: BriefWorld, tmp_path: Path
) -> None:
    sentinel = tmp_path / "sentinel"
    hostile = base64.b64encode(pickle.dumps(Detonator(str(sentinel)))).decode()
    host = host_over(world, brief_factory(Dials()))
    start(host, world)
    with psycopg.connect(world.db.dsn(OWNER), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runtime.workflow_checkpoints (thread_id, checkpoint_id, "
            "workflow_name, checkpointed_at, body) "
            "VALUES (%s, %s, %s, now() + interval '1 hour', %s)",
            (
                str(world.thread_id),
                str(uuid.uuid4()),
                AGENT,
                Jsonb({"__pickled__": hostile}),
            ),
        )
    dials = Dials()

    with pytest.raises(GraphFailure) as raised:
        resume(host_over(world, brief_factory(dials)), world)

    assert raised.value.code == "checkpoint-not-read"
    assert not sentinel.exists()
    assert dict(dials.counts) == {}
