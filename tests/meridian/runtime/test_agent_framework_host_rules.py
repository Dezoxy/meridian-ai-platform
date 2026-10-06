"""The second host's rules about what it will run and what it will trust (S037,
X1): a pause that was never saved is a failed leg, a definition is used for one
leg, a stored step count is a number in range, and a step's name is a plain
word. Each rule has a test on both sides of its boundary."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from agent_framework import Executor, WorkflowContext, handler, response_handler
from dbsupport import OWNER, DatabaseHandle
from hostflows import Dials, brief_factory, chain_factory, yield_factory
from hostsupport import CLAIM, BriefWorld, in_leg_thread, make_world
from psycopg.types.json import Jsonb
from servicesupport import owner_rows
from toolsupport import tracer_of

from meridian.runtime.agent_framework_host import (
    MAX_RESTORED_STEPS,
    AgentFrameworkHost,
    ResumeMarker,
    WorkflowDefinition,
    WorkflowFactory,
    check_definition,
)
from meridian.runtime.failures import GraphFailure, failure_reason
from meridian.runtime.hosts import AsyncModelClient, AsyncToolClient
from meridian.runtime.runs import RunOutcome

START_INPUT = {"claim_id": CLAIM}
COUNT = "SELECT count(*) FROM runtime.workflow_checkpoints WHERE thread_id = %s"
FORGE_COUNT = (
    "UPDATE runtime.workflow_checkpoints SET body = jsonb_set("
    "body, '{iteration_count}', %s) WHERE thread_id = %s AND seq = ("
    "SELECT max(seq) FROM runtime.workflow_checkpoints WHERE thread_id = %s)"
)


@pytest.fixture
def world(fresh_database: DatabaseHandle, plant: Callable[..., Path]) -> BriefWorld:
    return make_world(fresh_database, plant)


def host_over(world: BriefWorld, factory: WorkflowFactory) -> AgentFrameworkHost:
    return AgentFrameworkHost(factory, dsn=world.dsn())


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


# ── a pause that was never saved ────────────────────────────────────────────
class Switch:
    """A flag a test flips: while it is up, a step's checkpoint hook raises."""

    def __init__(self, up: bool = False) -> None:
        self.up = up


class BlindAsk(Executor):
    """A step that pauses, whose checkpoint hook raises while ``blind`` is up.
    The framework only logs a checkpoint it cannot build, and it never reaches
    the store, so the store records no failure."""

    def __init__(self, blind: Switch, *, after_asking: bool) -> None:
        super().__init__(id="ask")
        self._blind, self._after_asking, self._asked = blind, after_asking, False

    @handler
    async def ask(self, message: dict, ctx: WorkflowContext) -> None:
        self._asked = True
        await ctx.request_info({"claim_id": CLAIM}, ResumeMarker)

    @response_handler
    async def answered(
        self, original_request: dict, response: ResumeMarker, ctx: WorkflowContext
    ) -> None:
        return None

    async def on_checkpoint_save(self) -> dict[str, Any]:
        if self._blind.up and (self._asked or not self._after_asking):
            raise RuntimeError("the hook cannot build its state")
        return {}


def blind_factory(blind: Switch, *, after_asking: bool) -> WorkflowFactory:
    def factory(model: AsyncModelClient, tools: AsyncToolClient) -> WorkflowDefinition:
        return WorkflowDefinition(
            start=BlindAsk(blind, after_asking=after_asking), edges=()
        )

    return factory


def test_a_pause_the_framework_could_not_checkpoint_is_a_failed_leg_not_a_pause(
    world: BriefWorld, caplog: pytest.LogCaptureFixture
) -> None:
    # Only the checkpoint that would hold the pause cannot be built: the entry
    # checkpoint was saved, and it holds no pending request.
    host = host_over(world, blind_factory(Switch(up=True), after_asking=True))

    with caplog.at_level(logging.DEBUG), pytest.raises(GraphFailure) as raised:
        start(host, world)

    assert raised.value.code == "checkpoint-not-saved"
    assert failure_reason(raised.value) == "checkpoint-not-saved"
    assert rows(world) == 1


def test_a_pause_with_no_checkpoint_at_all_is_a_failed_leg_too(
    world: BriefWorld,
) -> None:
    host = host_over(world, blind_factory(Switch(up=True), after_asking=False))

    with pytest.raises(GraphFailure) as raised:
        start(host, world)

    assert raised.value.code == "checkpoint-not-saved"
    assert rows(world) == 0


def test_a_pause_whose_checkpoint_holds_its_request_is_a_pause(
    world: BriefWorld,
) -> None:
    host = host_over(world, blind_factory(Switch(up=False), after_asking=True))

    outcome = start(host, world)

    assert outcome == RunOutcome("AwaitingApproval", None)
    assert rows(world) >= 2


def test_a_leg_that_ends_without_a_pause_needs_no_pending_checkpoint(
    world: BriefWorld,
) -> None:
    host = host_over(world, yield_factory({"done": True}))

    outcome = start(host, world)

    assert outcome == RunOutcome("Completed", {"done": True})


# ── a definition is used for one leg ────────────────────────────────────────
def caching(factory: WorkflowFactory) -> WorkflowFactory:
    """A factory that builds its definition once and hands it out every time:
    its steps would hold the first call's clients for every later leg."""
    kept: list[WorkflowDefinition] = []

    def cached(model: AsyncModelClient, tools: AsyncToolClient) -> WorkflowDefinition:
        if not kept:
            kept.append(factory(model, tools))
        return kept[0]

    return cached


def test_a_factory_that_caches_its_definition_is_refused_at_the_second_leg(
    world: BriefWorld,
) -> None:
    dials = Dials()
    host = host_over(world, caching(brief_factory(dials)))
    assert start(host, world).status == "AwaitingApproval"
    before = dict(dials.counts)

    with pytest.raises(TypeError, match="new executors on every call") as raised:
        resume(host, world)

    assert dict(dials.counts) == before
    assert failure_reason(raised.value) == "unexpected"


def test_a_factory_that_caches_one_step_of_a_new_definition_is_refused_too(
    world: BriefWorld,
) -> None:
    dials = Dials()
    fresh = brief_factory(dials)
    first: list[Executor] = []

    def reuses_one_step(
        model: AsyncModelClient, tools: AsyncToolClient
    ) -> WorkflowDefinition:
        built = fresh(model, tools)
        if not first:
            first.append(built.edges[-1][0])
        replaced = tuple(
            (first[0] if source.id == first[0].id else source, target)
            for source, target in built.edges
        )
        return WorkflowDefinition(built.start, replaced, built.state_types)

    host = host_over(world, reuses_one_step)
    assert start(host, world).status == "AwaitingApproval"

    with pytest.raises(TypeError, match="new executors on every call"):
        resume(host, world)


def test_a_factory_that_builds_new_executors_on_every_call_runs_every_leg(
    world: BriefWorld,
) -> None:
    host = host_over(world, brief_factory(Dials()))

    paused = start(host, world)
    done = resume(host, world)

    assert (paused.status, done.status) == ("AwaitingApproval", "Completed")


# ── the step count a checkpoint restores ────────────────────────────────────
def forge_count(world: BriefWorld, value: Any) -> None:
    with psycopg.connect(world.db.dsn(OWNER), autocommit=True) as conn:
        thread = str(world.thread_id)
        conn.execute(FORGE_COUNT, (Jsonb(value), thread, thread))


@pytest.mark.parametrize(
    "forged", ["x", None, -1, 1.5, True, MAX_RESTORED_STEPS + 1, 10**12, [1], {"n": 1}]
)
def test_a_step_count_that_is_not_a_number_in_range_is_checkpoint_not_read(
    world: BriefWorld, forged: Any
) -> None:
    dials = Dials()
    host = host_over(world, brief_factory(dials))
    start(host, world)
    forge_count(world, forged)
    ran = dict(dials.counts)

    with pytest.raises(GraphFailure) as raised:
        resume(host, world)

    assert raised.value.code == "checkpoint-not-read"
    assert dict(dials.counts) == ran


@pytest.mark.parametrize("count", [0, 1, MAX_RESTORED_STEPS])
def test_a_step_count_in_range_resumes_with_ten_steps_after_it(
    world: BriefWorld, count: int
) -> None:
    host = host_over(world, brief_factory(Dials()))
    start(host, world)
    forge_count(world, count)

    outcome = resume(host, world)

    assert outcome.status == "Completed"


# ── the executors a checkpoint names ────────────────────────────────────────
LATEST = (
    "SELECT seq, body FROM runtime.workflow_checkpoints "
    "WHERE thread_id = %s ORDER BY seq DESC LIMIT 1"
)
REWRITE = "UPDATE runtime.workflow_checkpoints SET body = %s WHERE seq = %s"


def forge_latest(world: BriefWorld, change: Callable[[dict[str, Any]], None]) -> None:
    """Rewrite the latest checkpoint of the run's thread, as a holder of the
    runtime's role could."""
    with psycopg.connect(world.db.dsn(OWNER), autocommit=True) as conn:
        ((seq, body),) = conn.execute(LATEST, (str(world.thread_id),)).fetchall()
        change(body)
        conn.execute(REWRITE, (Jsonb(body), seq))


def in_flight(world: BriefWorld) -> tuple[AgentFrameworkHost, Dials]:
    """A run whose step after the answer failed: the latest checkpoint holds the
    answer's message in flight and no pending request."""
    dials = Dials(fail_in={"file"})
    host = host_over(world, brief_factory(dials))
    start(host, world)
    with pytest.raises(Exception, match="file"):
        resume(host, world)
    dials.fail_in.clear()
    return host, dials


def the_message(body: dict[str, Any]) -> dict[str, Any]:
    ((_, (wrapped,)),) = body["messages"].items()
    return wrapped["__message__"]


def rename_source_key(body: dict[str, Any]) -> None:
    body["messages"] = {"intruder": next(iter(body["messages"].values()))}


def forge_message(**changes: Any) -> Callable[[dict[str, Any]], None]:
    def change(body: dict[str, Any]) -> None:
        the_message(body).update(changes)

    return change


def test_a_message_in_flight_between_this_workflow_s_own_steps_is_resumed(
    world: BriefWorld,
) -> None:
    host, dials = in_flight(world)
    named = the_message(owner_latest(world))

    outcome = resume(host, world)

    assert named["source_id"] == "ask"
    assert outcome.status == "Completed"
    assert dials.counts["file"] == 2


def owner_latest(world: BriefWorld) -> dict[str, Any]:
    with psycopg.connect(world.db.dsn(OWNER), autocommit=True) as conn:
        ((_, body),) = conn.execute(LATEST, (str(world.thread_id),)).fetchall()
    return body


@pytest.mark.parametrize(
    "forge",
    [
        forge_message(target_id="intruder"),
        forge_message(source_id="intruder"),
        forge_message(source_id="internal:intruder"),
        rename_source_key,
    ],
    ids=["target", "source", "internal-source", "source-key"],
)
def test_a_message_that_names_an_executor_the_workflow_does_not_have_is_not_read(
    world: BriefWorld, forge: Callable[[dict[str, Any]], None]
) -> None:
    host, dials = in_flight(world)
    forge_latest(world, forge)
    ran = dict(dials.counts)

    with pytest.raises(GraphFailure) as raised:
        resume(host, world)

    assert raised.value.code == "checkpoint-not-read"
    assert dict(dials.counts) == ran


def test_a_pending_request_from_an_executor_the_workflow_does_not_have_is_not_read(
    world: BriefWorld,
) -> None:
    dials = Dials()
    host = host_over(world, brief_factory(dials))
    start(host, world)

    def forge(body: dict[str, Any]) -> None:
        ((_, wrapped),) = body["pending_request_info_events"].items()
        wrapped["__event__"]["source_executor_id"] = "intruder"

    forge_latest(world, forge)
    ran = dict(dials.counts)

    with pytest.raises(GraphFailure) as raised:
        resume(host, world)

    assert raised.value.code == "checkpoint-not-read"
    assert dict(dials.counts) == ran


# ── a step's name ───────────────────────────────────────────────────────────
class Named(Executor):
    def __init__(self, name: str) -> None:
        super().__init__(id=name)

    @handler
    async def run(self, message: dict, ctx: WorkflowContext) -> None:
        return None


def definition_of(
    start_id: str, *others: str, state_types: tuple[type, ...] = ()
) -> WorkflowDefinition:
    first = Named(start_id)
    rest = [Named(name) for name in others]
    return WorkflowDefinition(first, tuple((first, step) for step in rest), state_types)


@dataclass
class Opaque:
    anything: Any


@dataclass
class Plain:
    claim_id: str
    facts: dict


def test_a_state_type_the_codec_cannot_check_is_refused_with_the_definition() -> None:
    # At the start of the service, not when the first checkpoint holding one is
    # read back: the definition is checked the way a leg would register it.
    with pytest.raises(TypeError, match="cannot check"):
        check_definition(definition_of("gather", state_types=(Opaque,)))


def test_a_state_type_of_plain_data_is_accepted_with_the_definition() -> None:
    definition = definition_of("gather", state_types=(Plain,))

    assert check_definition(definition) is definition


@pytest.mark.parametrize(
    "name", ["gather", "draft", "ask", "file", "before-0", "file_note", "a", "a" * 48]
)
def test_a_step_named_with_lower_case_words_and_hyphens_or_underscores_is_accepted(
    name: str,
) -> None:
    definition = definition_of(name)

    assert check_definition(definition) is definition


@pytest.mark.parametrize(
    "name",
    [
        "Gather",
        "two words",
        "claim CLM-0001",
        "x;y",
        "-first",
        "_first",
        "0first",
        "a" * 49,
        "gather\n",
        "gäther",
        "gather.draft",
        "{claim}",
    ],
)
def test_a_step_with_any_other_name_is_refused_whichever_end_of_an_edge_it_is(
    name: str,
) -> None:
    for definition in (definition_of(name), definition_of("gather", name)):
        with pytest.raises(TypeError, match="step name") as raised:
            check_definition(definition)

        # The name is not copied into the error: it could be data.
        assert name not in str(raised.value)


# ── a resume that can never succeed (F3r, the security review's high) ───────
# A release that changes a workflow's state types or its graph while a brief
# waits makes every later resume of that run fail the same way. Each of the two
# has a word of its own, which ends the run (see ``runs.RESUME_CANNOT_SUCCEED``);
# a store that cannot be reached keeps ``checkpoint-not-read``, which pauses it
# again, because the next attempt may reach it.
FORGED_TEXT = "claimant-text-in-a-forged-field"


def add_a_field_to_the_pending_request(body: dict[str, Any]) -> None:
    """The state type of the stored request has a field more than the code's
    (what a release that took one away leaves behind)."""
    ((_, wrapped),) = body["pending_request_info_events"].items()
    wrapped["__event__"]["data"]["fields"]["extra"] = FORGED_TEXT


def drop_a_field_of_the_pending_request(body: dict[str, Any]) -> None:
    ((_, wrapped),) = body["pending_request_info_events"].items()
    del wrapped["__event__"]["data"]["fields"]["brief"]


def retype_the_pending_request_as_a_type_the_code_does_not_have(
    body: dict[str, Any],
) -> None:
    ((_, wrapped),) = body["pending_request_info_events"].items()
    wrapped["__event__"]["data"]["__dataclass__"] = "hostflows:Renamed"


@pytest.mark.parametrize(
    "forge",
    [
        add_a_field_to_the_pending_request,
        drop_a_field_of_the_pending_request,
        retype_the_pending_request_as_a_type_the_code_does_not_have,
    ],
    ids=["a-field-more", "a-field-less", "a-type-renamed"],
)
def test_a_stored_state_type_the_code_no_longer_has_ends_the_resume_with_its_word(
    world: BriefWorld, forge: Callable[[dict[str, Any]], None], caplog: Any
) -> None:
    dials = Dials()
    host = host_over(world, brief_factory(dials))
    start(host, world)
    forge_latest(world, forge)
    ran = dict(dials.counts)

    with caplog.at_level(logging.DEBUG), pytest.raises(GraphFailure) as raised:
        resume(host, world)

    assert raised.value.code == "checkpoint-refused"
    assert failure_reason(raised.value) == "checkpoint-refused"
    assert str(raised.value) == "checkpoint-refused"
    assert dict(dials.counts) == ran
    assert FORGED_TEXT not in caplog.text


def chain_host(world: BriefWorld, *, before: int, after: int) -> AgentFrameworkHost:
    return host_over(world, chain_factory(Dials(), before=before, after=after))


@pytest.mark.parametrize(
    ("paused_with", "resumed_with"),
    [
        pytest.param((1, 0), (1, 1), id="a-step-added-after-the-pause"),
        pytest.param((1, 0), (2, 0), id="a-step-added-before-the-pause"),
        pytest.param((2, 0), (1, 0), id="a-step-removed"),
    ],
)
def test_a_workflow_whose_graph_changed_while_the_run_waited_ends_the_resume(
    world: BriefWorld,
    paused_with: tuple[int, int],
    resumed_with: tuple[int, int],
) -> None:
    start(chain_host(world, before=paused_with[0], after=paused_with[1]), world)
    changed = Dials()
    host = host_over(
        world, chain_factory(changed, before=resumed_with[0], after=resumed_with[1])
    )

    with pytest.raises(GraphFailure) as raised:
        resume(host, world)

    assert raised.value.code == "workflow-changed"
    assert failure_reason(raised.value) == "workflow-changed"
    assert dict(changed.counts) == {}


def test_a_workflow_whose_graph_did_not_change_resumes_as_before(
    world: BriefWorld,
) -> None:
    start(chain_host(world, before=1, after=1), world)
    dials = Dials()
    host = host_over(world, chain_factory(dials, before=1, after=1))

    outcome = resume(host, world)

    assert outcome.status == "Completed"
