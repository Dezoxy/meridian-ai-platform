"""The second host's checkpoint store against PostgreSQL (S037, R3b): the six
members of the framework's protocol, one thread per store, the host's two extras
(``forget`` and ``failures``), no database text in anything it raises, and the
two-process path the host will rely on, from the store alone."""

import dataclasses
import inspect
import logging
import pickle
import typing
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from agent_framework import CheckpointStorage, WorkflowCheckpoint
from agent_framework._workflows import _checkpoint_encoding
from agent_framework.exceptions import WorkflowCheckpointException
from dbsupport import OWNER, DatabaseHandle
from psycopg.types.json import Jsonb
from servicesupport import owner_rows
from workflowsupport import (
    BRIEF,
    CHECKPOINT_TYPES,
    WORKFLOW_NAME,
    Dials,
    MemoryStore,
    StepFailure,
    answer_latest,
    describe,
    latest_of,
    leg,
    restore_latest,
    start,
)

from meridian.platform.common.db import CONNECT_TIMEOUT_SECONDS, STATEMENT_TIMEOUT_MS
from meridian.runtime import SERVICE_NAME
from meridian.runtime.workflow_checkpoints import (
    CheckpointCodec,
    PostgresCheckpointStore,
)

CANARY = "canary-claim-text-7f3a"
THE_BRIEF = [{"brief": BRIEF, "filed": True}]
COUNT = "SELECT count(*) FROM runtime.workflow_checkpoints WHERE thread_id = %s"


def store_for(
    db: DatabaseHandle, thread: uuid.UUID, *, role: str = "agent_runtime"
) -> PostgresCheckpointStore:
    return PostgresCheckpointStore(db.dsn(role), thread, CHECKPOINT_TYPES)


def rows_of(db: DatabaseHandle, thread: uuid.UUID) -> int:
    return owner_rows(db, COUNT, (str(thread),))[0][0]


def a_checkpoint() -> WorkflowCheckpoint:
    """A real checkpoint, the pause of the small workflow."""
    memory = MemoryStore()
    start(Dials(), memory)
    return memory.saved[-1]


def variant(
    template: WorkflowCheckpoint, *, when: datetime, **changes: Any
) -> WorkflowCheckpoint:
    return dataclasses.replace(
        template,
        checkpoint_id=changes.pop("checkpoint_id", str(uuid.uuid4())),
        timestamp=when.isoformat(),
        **changes,
    )


def save_all(store: PostgresCheckpointStore, *checkpoints: WorkflowCheckpoint) -> None:
    for checkpoint in checkpoints:
        leg(lambda cp=checkpoint: store.save(cp))


# ── the protocol ────────────────────────────────────────────────────────────
def test_the_store_has_the_six_members_of_the_protocol_and_two_more_of_its_own() -> (
    None
):
    members = typing.get_protocol_members(CheckpointStorage)
    public = {
        name
        for name, _ in inspect.getmembers(PostgresCheckpointStore)
        if not name.startswith("_")
    }

    # A seventh member in a later version of the framework fails here, by name.
    assert members == {
        "save",
        "load",
        "list_checkpoints",
        "list_checkpoint_ids",
        "get_latest",
        "delete",
    }
    assert public == members | {"forget", "failures"}


def test_each_member_has_the_signature_the_protocol_declares() -> None:
    for name in typing.get_protocol_members(CheckpointStorage):
        declared = inspect.signature(getattr(CheckpointStorage, name))
        ours = inspect.signature(getattr(PostgresCheckpointStore, name))

        assert inspect.iscoroutinefunction(getattr(PostgresCheckpointStore, name))
        assert [(p.name, p.kind) for p in ours.parameters.values()] == [
            (p.name, p.kind) for p in declared.parameters.values()
        ], name


def test_a_store_is_bound_to_a_uuid_thread_and_refuses_text() -> None:
    with pytest.raises(TypeError):
        PostgresCheckpointStore("postgresql:///x", "not-a-uuid", [])  # type: ignore[arg-type]


# ── what the members do ─────────────────────────────────────────────────────
def test_a_saved_checkpoint_is_loaded_listed_and_found_as_the_latest(
    fresh_database: DatabaseHandle,
) -> None:
    thread = uuid.uuid4()
    store = store_for(fresh_database, thread)
    template = a_checkpoint()
    earlier = variant(template, when=datetime(2026, 10, 6, 10, tzinfo=UTC))
    later = variant(template, when=datetime(2026, 10, 6, 11, tzinfo=UTC))

    save_all(store, earlier, later)

    codec = CheckpointCodec(CHECKPOINT_TYPES)
    loaded = leg(lambda: store.load(earlier.checkpoint_id))
    assert codec.to_document(loaded) == codec.to_document(earlier)
    ids = leg(lambda: store.list_checkpoint_ids(workflow_name=WORKFLOW_NAME))
    assert ids == [earlier.checkpoint_id, later.checkpoint_id]
    listed = leg(lambda: store.list_checkpoints(workflow_name=WORKFLOW_NAME))
    assert [c.checkpoint_id for c in listed] == ids
    latest = leg(lambda: store.get_latest(workflow_name=WORKFLOW_NAME))
    assert latest is not None
    assert latest.checkpoint_id == later.checkpoint_id
    assert store.failures == ()


def test_the_latest_is_the_newest_by_its_time_whatever_the_order_it_was_saved_in(
    fresh_database: DatabaseHandle,
) -> None:
    store = store_for(fresh_database, uuid.uuid4())
    template = a_checkpoint()
    newer = variant(template, when=datetime(2026, 10, 6, 12, tzinfo=UTC))
    older = variant(template, when=datetime(2026, 10, 6, 9, tzinfo=UTC))

    save_all(store, newer, older)

    latest = leg(lambda: store.get_latest(workflow_name=WORKFLOW_NAME))
    assert latest is not None
    assert latest.checkpoint_id == newer.checkpoint_id


def test_two_checkpoints_of_one_instant_are_ordered_by_the_order_they_were_saved(
    fresh_database: DatabaseHandle,
) -> None:
    store = store_for(fresh_database, uuid.uuid4())
    template = a_checkpoint()
    instant = datetime(2026, 10, 6, 12, tzinfo=UTC)
    # The IDs sort the other way round, so only the sequence can decide.
    first = variant(template, when=instant, checkpoint_id="z-first")
    second = variant(template, when=instant, checkpoint_id="a-second")

    save_all(store, first, second)

    ids = leg(lambda: store.list_checkpoint_ids(workflow_name=WORKFLOW_NAME))
    latest = leg(lambda: store.get_latest(workflow_name=WORKFLOW_NAME))
    assert ids == ["z-first", "a-second"]
    assert latest is not None
    assert latest.checkpoint_id == "a-second"


def test_the_listings_and_the_latest_are_of_one_workflow_name(
    fresh_database: DatabaseHandle,
) -> None:
    store = store_for(fresh_database, uuid.uuid4())
    template = a_checkpoint()
    other = variant(
        template, when=datetime(2026, 10, 6, 13, tzinfo=UTC), workflow_name="other"
    )

    save_all(store, template, other)

    ids = leg(lambda: store.list_checkpoint_ids(workflow_name="other"))
    assert ids == [other.checkpoint_id]
    assert leg(lambda: store.get_latest(workflow_name="nothing-by-this-name")) is None


def test_a_checkpoint_that_is_not_there_is_not_found(
    fresh_database: DatabaseHandle,
) -> None:
    store = store_for(fresh_database, uuid.uuid4())

    with pytest.raises(WorkflowCheckpointException, match="No checkpoint found"):
        leg(lambda: store.load("no-such-checkpoint"))

    assert leg(lambda: store.delete("no-such-checkpoint")) is False
    assert leg(lambda: store.get_latest(workflow_name=WORKFLOW_NAME)) is None


def test_a_saved_checkpoint_is_deleted_once(fresh_database: DatabaseHandle) -> None:
    thread = uuid.uuid4()
    store = store_for(fresh_database, thread)
    template = a_checkpoint()
    save_all(store, template)

    first = leg(lambda: store.delete(template.checkpoint_id))
    second = leg(lambda: store.delete(template.checkpoint_id))

    assert (first, second) == (True, False)
    assert rows_of(fresh_database, thread) == 0


def test_saving_the_same_checkpoint_twice_is_refused_and_recorded(
    fresh_database: DatabaseHandle,
) -> None:
    thread = uuid.uuid4()
    store = store_for(fresh_database, thread)
    template = a_checkpoint()
    save_all(store, template)

    with pytest.raises(WorkflowCheckpointException, match="UniqueViolation"):
        save_all(store, template)

    assert store.failures == ("UniqueViolation",)
    assert rows_of(fresh_database, thread) == 1


# ── one thread ──────────────────────────────────────────────────────────────
def test_a_store_never_sees_or_touches_another_threads_checkpoints(
    fresh_database: DatabaseHandle,
) -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    my_store, their_store = (store_for(fresh_database, t) for t in (mine, theirs))
    template = a_checkpoint()
    my_checkpoint = variant(template, when=datetime(2026, 10, 6, 9, tzinfo=UTC))
    their_checkpoint = variant(template, when=datetime(2026, 10, 6, 10, tzinfo=UTC))
    save_all(my_store, my_checkpoint)
    save_all(their_store, their_checkpoint)

    with pytest.raises(WorkflowCheckpointException, match="No checkpoint found"):
        leg(lambda: my_store.load(their_checkpoint.checkpoint_id))
    deleted = leg(lambda: my_store.delete(their_checkpoint.checkpoint_id))
    ids = leg(lambda: my_store.list_checkpoint_ids(workflow_name=WORKFLOW_NAME))
    latest = leg(lambda: my_store.get_latest(workflow_name=WORKFLOW_NAME))
    forgotten = leg(my_store.forget)

    assert deleted is False
    assert ids == [my_checkpoint.checkpoint_id]
    assert latest is not None
    assert latest.checkpoint_id == my_checkpoint.checkpoint_id
    assert forgotten == 1
    assert rows_of(fresh_database, mine) == 0
    assert rows_of(fresh_database, theirs) == 1


def test_forget_deletes_every_row_of_the_thread_and_says_how_many(
    fresh_database: DatabaseHandle,
) -> None:
    thread = uuid.uuid4()
    store = store_for(fresh_database, thread)
    template = a_checkpoint()
    save_all(
        store,
        variant(template, when=datetime(2026, 10, 6, 9, tzinfo=UTC)),
        variant(template, when=datetime(2026, 10, 6, 10, tzinfo=UTC)),
        variant(template, when=datetime(2026, 10, 6, 11, tzinfo=UTC)),
    )

    forgotten = leg(store.forget)
    again = leg(store.forget)

    assert (forgotten, again) == (3, 0)
    assert rows_of(fresh_database, thread) == 0


# ── the connection ──────────────────────────────────────────────────────────
def test_each_call_connects_as_the_runtime_with_the_services_settings_and_closes(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[dict[str, Any], tuple[Any, ...]]] = []
    real = psycopg.AsyncConnection.connect

    async def spy(*args: Any, **kwargs: Any) -> psycopg.AsyncConnection[Any]:
        conn = await real(*args, **kwargs)
        cursor = await conn.execute(
            "SELECT current_user, current_setting('application_name'), "
            "current_setting('statement_timeout')"
        )
        seen.append((kwargs, await cursor.fetchone()))  # type: ignore[arg-type]
        return conn

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", spy)
    store = store_for(fresh_database, uuid.uuid4())
    template = a_checkpoint()

    save_all(store, template)
    leg(lambda: store.get_latest(workflow_name=WORKFLOW_NAME))

    assert len(seen) == 2
    for kwargs, (role, application, timeout) in seen:
        assert kwargs["autocommit"] is True
        assert kwargs["connect_timeout"] == CONNECT_TIMEOUT_SECONDS
        assert (role, application) == ("agent_runtime", SERVICE_NAME)
        assert timeout == f"{STATEMENT_TIMEOUT_MS // 1000}s"
    open_sessions = owner_rows(
        fresh_database,
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND application_name = %s",
        (SERVICE_NAME,),
    )
    assert open_sessions == [(0,)]


# ── a refused or failed save ────────────────────────────────────────────────
def test_a_value_that_cannot_be_restored_is_refused_unwritten_and_recorded(
    fresh_database: DatabaseHandle,
) -> None:
    thread = uuid.uuid4()
    store = store_for(fresh_database, thread)
    poisoned = dataclasses.replace(a_checkpoint(), state={"x": object()})

    with pytest.raises(WorkflowCheckpointException) as refused:
        save_all(store, poisoned)

    assert str(refused.value) == (
        "cannot save a checkpoint: CodecRefusal (unregistered-type)"
    )
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    assert store.failures == ("CodecRefusal",)
    assert rows_of(fresh_database, thread) == 0


def test_a_checkpoint_with_a_naive_time_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    thread = uuid.uuid4()
    store = store_for(fresh_database, thread)
    naive = variant(a_checkpoint(), when=datetime(2026, 10, 6, 9))

    with pytest.raises(WorkflowCheckpointException, match="naive-timestamp"):
        save_all(store, naive)

    assert store.failures == ("CodecRefusal",)
    assert rows_of(fresh_database, thread) == 0


def refuse_rows_holding(db: DatabaseHandle, text: str) -> None:
    with psycopg.connect(db.dsn(OWNER), autocommit=True) as conn:
        conn.execute(
            "ALTER TABLE runtime.workflow_checkpoints ADD CONSTRAINT no_canary "
            f"CHECK (body::text NOT LIKE '%{text}%')"
        )


def test_the_driver_error_quotes_the_row_which_is_why_the_store_may_not_pass_it_on(
    fresh_database: DatabaseHandle,
) -> None:
    refuse_rows_holding(fresh_database, CANARY)
    thread = uuid.uuid4()
    checkpoint = dataclasses.replace(a_checkpoint(), state={"note": CANARY})
    codec = CheckpointCodec(CHECKPOINT_TYPES)

    with (
        psycopg.connect(fresh_database.dsn("agent_runtime")) as conn,
        pytest.raises(psycopg.errors.CheckViolation) as raw,
    ):
        conn.execute(
            "INSERT INTO runtime.workflow_checkpoints (thread_id, checkpoint_id, "
            "workflow_name, checkpointed_at, body) VALUES (%s, 'c', 'w', now(), %s)",
            (str(thread), Jsonb(codec.to_document(checkpoint))),
        )

    # The control: the raw text holds the claim's text and the thread.
    assert CANARY in str(raw.value)
    assert str(thread) in str(raw.value)


def test_a_save_the_table_refuses_leaves_no_trace_of_the_row_in_any_text(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    refuse_rows_holding(fresh_database, CANARY)
    thread = uuid.uuid4()
    store = store_for(fresh_database, thread)
    checkpoint = dataclasses.replace(a_checkpoint(), state={"note": CANARY})

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(WorkflowCheckpointException) as e,
    ):
        save_all(store, checkpoint)

    error = e.value
    assert str(error) == "cannot save a checkpoint: CheckViolation (23514)"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert store.failures == ("CheckViolation",)
    texts = [str(error), *store.failures, caplog.text]
    for record in caplog.records:
        texts += [record.getMessage(), record.exc_text or "", str(record.args)]
    assert not [t for t in texts if CANARY in t or str(thread) in t]
    assert rows_of(fresh_database, thread) == 0


def test_a_table_the_role_may_not_read_is_reported_by_class_and_sqlstate_only(
    fresh_database: DatabaseHandle,
) -> None:
    store = store_for(fresh_database, uuid.uuid4(), role="claims_api")

    with pytest.raises(WorkflowCheckpointException) as refused:
        leg(lambda: store.get_latest(workflow_name=WORKFLOW_NAME))

    assert str(refused.value) == (
        "cannot read the latest checkpoint: InsufficientPrivilege (42501)"
    )
    assert refused.value.__context__ is None


def test_a_database_that_cannot_be_reached_is_reported_without_its_address(
    fresh_database: DatabaseHandle,
) -> None:
    store = PostgresCheckpointStore(
        "postgresql://127.0.0.1:1/nodatabase?connect_timeout=2",
        uuid.uuid4(),
        CHECKPOINT_TYPES,
    )
    template = a_checkpoint()

    with pytest.raises(WorkflowCheckpointException) as refused:
        save_all(store, template)

    assert "127.0.0.1" not in str(refused.value)
    assert "nodatabase" not in str(refused.value)
    assert str(refused.value).startswith("cannot save a checkpoint: OperationalError")
    assert store.failures == ("OperationalError",)


def test_a_stored_document_that_is_not_ours_is_refused_when_it_is_read(
    fresh_database: DatabaseHandle,
) -> None:
    thread = uuid.uuid4()
    store = store_for(fresh_database, thread)
    template = a_checkpoint()
    document = CheckpointCodec(CHECKPOINT_TYPES).to_document(template)
    document["state"]["planted"] = {"__pickled__": "AAAA"}
    with psycopg.connect(fresh_database.dsn(OWNER), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runtime.workflow_checkpoints (thread_id, checkpoint_id, "
            "workflow_name, checkpointed_at, body) "
            "VALUES (%s, 'planted', %s, now(), %s)",
            (str(thread), WORKFLOW_NAME, Jsonb(document)),
        )

    with pytest.raises(WorkflowCheckpointException) as refused:
        leg(lambda: store.load("planted"))

    assert str(refused.value) == "cannot read a checkpoint: CodecRefusal (unknown-tag)"
    assert refused.value.__context__ is None


# ── no pickle on the path ───────────────────────────────────────────────────
def test_nothing_on_a_start_and_a_resume_calls_a_pickle_loader_or_the_frameworks_codec(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def refuse(name: str) -> Callable[..., Any]:
        def fail(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(name)

        return fail

    for name in ("encode_checkpoint_value", "decode_checkpoint_value"):
        monkeypatch.setattr(_checkpoint_encoding, name, refuse(name))
    monkeypatch.setattr(
        _checkpoint_encoding, "_RestrictedUnpickler", refuse("unpickler")
    )
    monkeypatch.setattr(pickle, "loads", refuse("pickle.loads"))
    monkeypatch.setattr(pickle, "load", refuse("pickle.load"))
    store = store_for(fresh_database, uuid.uuid4())

    start(Dials(), store)
    result = answer_latest(Dials(), store)

    assert describe(result)["outputs"] == THE_BRIEF
    assert calls == []


# ── the path the host relies on, from the store alone ──────────────────────
def shape_of(store: PostgresCheckpointStore) -> tuple[int, list[str]]:
    """The latest checkpoint's pending requests and the executors holding a
    message in flight: the three shapes tell a host which call to make."""
    latest = leg(lambda: latest_of(store))
    return len(latest.pending_request_info_events), list(latest.messages)


def test_a_new_workflow_and_a_new_store_resume_a_paused_run_from_the_latest_checkpoint(
    fresh_database: DatabaseHandle,
) -> None:
    thread = uuid.uuid4()
    first_dials, second_dials = Dials(), Dials()
    first = store_for(fresh_database, thread)
    paused = start(first_dials, first)
    second = store_for(fresh_database, thread)

    finished = answer_latest(second_dials, second)

    assert describe(paused) == {"paused": True, "outputs": []}
    assert describe(finished) == {"paused": False, "outputs": THE_BRIEF}
    # The second process ran only what follows the pause.
    assert dict(first_dials.counts) == {"draft": 1, "ask": 1}
    assert dict(second_dials.counts) == {"answered": 1, "file": 1}
    assert first.failures == second.failures == ()


def test_the_latest_checkpoint_has_one_of_three_shapes_and_each_says_what_to_call(
    fresh_database: DatabaseHandle,
) -> None:
    thread = uuid.uuid4()
    paused_dials = Dials()
    store = store_for(fresh_database, thread)
    start(paused_dials, store)
    at_pause = shape_of(store_for(fresh_database, thread))
    failing = Dials(fail_in={"file"})
    with pytest.raises(StepFailure):
        answer_latest(failing, store_for(fresh_database, thread))
    after_failure = shape_of(store_for(fresh_database, thread))
    restore_latest(Dials(), store_for(fresh_database, thread))
    done = shape_of(store_for(fresh_database, thread))

    # One pending request: answer it (responses= from the latest checkpoint).
    assert at_pause == (1, [])
    # No pending request, an answer in flight: restore it with no response.
    assert after_failure == (0, ["ask"])
    # Neither: there is nothing to resume.
    assert done == (0, [])


def test_after_a_step_fails_on_the_first_resume_a_restore_runs_only_that_step_again(
    fresh_database: DatabaseHandle,
) -> None:
    thread = uuid.uuid4()
    start(Dials(), store_for(fresh_database, thread))
    failing = Dials(fail_in={"file"})

    with pytest.raises(StepFailure):
        answer_latest(failing, store_for(fresh_database, thread))
    with pytest.raises(RuntimeError, match="No pending requests found"):
        answer_latest(Dials(), store_for(fresh_database, thread))
    again = Dials()
    finished = restore_latest(again, store_for(fresh_database, thread))

    # The response handler ran once in all, the failed step twice.
    assert dict(failing.counts) == {"answered": 1, "file": 1}
    assert dict(again.counts) == {"file": 1}
    assert describe(finished) == {"paused": False, "outputs": THE_BRIEF}


def test_a_run_that_ends_is_forgotten_and_nothing_is_left_to_resume(
    fresh_database: DatabaseHandle,
) -> None:
    thread = uuid.uuid4()
    store = store_for(fresh_database, thread)
    start(Dials(), store)
    answer_latest(Dials(), store_for(fresh_database, thread))
    held = rows_of(fresh_database, thread)

    forgotten = leg(store.forget)

    assert held == forgotten > 0
    assert rows_of(fresh_database, thread) == 0
    assert leg(lambda: store.get_latest(workflow_name=WORKFLOW_NAME)) is None


# ── a failed save, which the framework only logs ────────────────────────────
def test_a_save_the_table_refuses_does_not_stop_the_run_and_shows_in_the_failures(
    fresh_database: DatabaseHandle,
) -> None:
    with psycopg.connect(fresh_database.dsn(OWNER), autocommit=True) as conn:
        conn.execute(
            "ALTER TABLE runtime.workflow_checkpoints ADD CONSTRAINT limited "
            "CHECK ((body->>'iteration_count')::int < 2)"
        )
    thread = uuid.uuid4()
    store = store_for(fresh_database, thread)

    paused = start(Dials(), store)

    # The framework reports the pause as if all were well; the store's record
    # is the one thing that shows the pause was not saved.
    assert describe(paused)["paused"] is True
    assert store.failures == ("CheckViolation",)
    # The pause is iteration 2: the two saves before it are all there is.
    assert rows_of(fresh_database, thread) == 2
    latest = leg(lambda: latest_of(store))
    assert latest.pending_request_info_events == {}


def test_a_leg_whose_saves_all_worked_has_no_failures(
    fresh_database: DatabaseHandle,
) -> None:
    store = store_for(fresh_database, uuid.uuid4())

    start(Dials(), store)
    answer_latest(Dials(), store)

    assert store.failures == ()
