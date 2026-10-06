"""Probe 6: the store's protocol at 1.19.0, a store over one PostgreSQL table
keyed by a thread ID the host chooses, the JSON round trip, the refusal at save
and the absence of pickle on the path."""

import base64
import dataclasses
import inspect
import json
import pickle
import typing
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from agent_framework import CheckpointStorage, WorkflowCheckpoint
from agent_framework._workflows import _checkpoint_encoding
from agent_framework.exceptions import WorkflowCheckpointException
from psycopg import sql
from psycopg.types.json import Jsonb
from s037probe.codec import CheckpointCodec, registry_key
from s037probe.flow import (
    CHECKPOINT_TYPES,
    WORKFLOW_NAME,
    AskRequest,
    Drafted,
    Filing,
    Gathered,
    Marker,
)
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

# Each member: its parameters after ``self`` (a "*" marks keyword-only) and its
# return annotation, as the protocol declares them.
PROTOCOL = {
    "save": (["checkpoint"], "CheckpointID"),
    "load": (["checkpoint_id"], "WorkflowCheckpoint"),
    "list_checkpoints": (["*", "workflow_name"], "list[WorkflowCheckpoint]"),
    "delete": (["checkpoint_id"], "bool"),
    "get_latest": (["*", "workflow_name"], "WorkflowCheckpoint | None"),
    "list_checkpoint_ids": (["*", "workflow_name"], "list[CheckpointID]"),
}


def shape_of(function: Any) -> tuple[list[str], str]:
    signature = inspect.signature(function)
    names: list[str] = []
    for name, parameter in list(signature.parameters.items())[1:]:
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY and "*" not in names:
            names.append("*")
        names.append(name)
    return names, str(signature.return_annotation)


def test_the_checkpoint_storage_protocol_has_six_members_with_these_signatures() -> (
    None
):
    members = typing.get_protocol_members(CheckpointStorage)

    assert members == frozenset(PROTOCOL)
    for name, expected in PROTOCOL.items():
        assert shape_of(getattr(CheckpointStorage, name)) == expected


def test_the_store_has_every_member_of_the_protocol() -> None:
    members = typing.get_protocol_members(CheckpointStorage)

    assert all(hasattr(PostgresCheckpointStorage, name) for name in members)


def test_the_framework_calls_only_save_and_load_the_host_calls_the_rest(
    scratch_schema: str, thread_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    for name in typing.get_protocol_members(CheckpointStorage):
        original = getattr(PostgresCheckpointStorage, name)

        def spy(
            self: Any, *a: Any, _o: Any = original, _n: str = name, **k: Any
        ) -> Any:
            called.append(_n)
            return _o(self, *a, **k)

        monkeypatch.setattr(PostgresCheckpointStorage, name, spy)
    rig = Rig(scratch_schema, thread_id)

    start(rig)
    resume(rig, {})

    # resume() above asks the store for the latest checkpoint itself; the
    # framework's own calls are the rest.
    assert set(called) == {"save", "load", "get_latest"}
    assert "get_latest" in called
    framework_only = [c for c in called if c != "get_latest"]
    assert set(framework_only) == {"save", "load"}


def test_a_checkpoint_has_these_fields_and_no_slot_for_the_hosts_thread_id(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)

    checkpoints = all_checkpoints(rig)

    assert [f.name for f in dataclasses.fields(WorkflowCheckpoint)] == [
        "workflow_name",
        "graph_signature_hash",
        "checkpoint_id",
        "previous_checkpoint_id",
        "timestamp",
        "messages",
        "state",
        "pending_request_info_events",
        "iteration_count",
        "metadata",
        "version",
    ]
    # ``metadata`` is the only free-form field and the framework leaves it
    # empty: the host cannot put the run into the checkpoint, so the store is
    # built for one run and writes the thread ID itself.
    assert [c.metadata for c in checkpoints] == [{}] * 4
    assert {c.workflow_name for c in checkpoints} == {WORKFLOW_NAME}
    assert {c.version for c in checkpoints} == {"1.0"}
    assert len({c.graph_signature_hash for c in checkpoints}) == 1


def test_two_runs_of_one_workflow_name_do_not_see_each_other_through_a_bound_store(
    scratch_schema: str,
) -> None:
    mine, theirs = Rig(scratch_schema, uuid.uuid4()), Rig(scratch_schema, uuid.uuid4())
    start(mine)
    start(theirs)
    their_id = latest_sync(theirs).checkpoint_id

    assert latest_sync(mine).checkpoint_id != their_id
    assert len(all_checkpoints(mine)) == len(all_checkpoints(theirs)) == 4
    with pytest.raises(WorkflowCheckpointException, match="No checkpoint found"):
        leg(lambda: mine.storage().load(their_id))
    assert leg(lambda: mine.storage().delete(their_id)) is False
    assert latest_sync(theirs).checkpoint_id == their_id


def test_a_pause_checkpoint_round_trips_through_json_field_for_field(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    codec = CheckpointCodec(CHECKPOINT_TYPES)

    for stored in all_checkpoints(rig):
        text = json.dumps(codec.to_document(stored))
        again = codec.from_document(json.loads(text))
        assert codec.to_document(again) == codec.to_document(stored)
    pause = latest_sync(rig)
    (event,) = pause.pending_request_info_events.values()
    assert event.data == AskRequest("CLM-0001", "a synthetic brief")
    assert event.response_type is Marker
    assert event.source_executor_id == "ask"
    assert pause.iteration_count == 3
    assert pause.previous_checkpoint_id == all_checkpoints(rig)[-2].checkpoint_id


def test_the_dataclasses_a_checkpoint_holds_are_the_ones_the_flow_passes_on(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    resume(rig, {})
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "__dataclass__" in value:
                seen.add(value["__dataclass__"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and value.startswith("s037probe."):
            seen.add(value.replace(".", ":", 1) if ":" not in value else value)

    codec = CheckpointCodec(CHECKPOINT_TYPES)
    for stored in all_checkpoints(rig):
        walk(codec.to_document(stored))

    # Every message type and the request's data, and the response type as a
    # name inside the request event: each needs a rule (is registered).
    assert {registry_key(t) for t in (Gathered, Drafted, AskRequest, Filing)} <= seen
    assert any(name.endswith("Marker") for name in seen)


@pytest.mark.parametrize("missing", [Gathered, Drafted, AskRequest, Filing, Marker])
def test_a_type_without_a_rule_is_refused_at_save_not_at_resume(
    scratch_schema: str, thread_id: uuid.UUID, missing: type
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    documented = all_checkpoints(rig)
    resume(rig, {})
    documented += all_checkpoints(rig)[len(documented) :]
    narrow = PostgresCheckpointStorage(
        scratch_schema, uuid.uuid4(), [t for t in CHECKPOINT_TYPES if t is not missing]
    )

    refused = 0
    for checkpoint in documented:
        try:
            leg(lambda cp=checkpoint: narrow.save(cp))
        except WorkflowCheckpointException as error:
            refused += 1
            assert "unregistered" not in str(error)  # the class name only
    assert refused >= 1
    assert len(narrow.failures) == refused


def test_a_value_that_cannot_be_restored_is_refused_at_save_and_nothing_is_written(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    template = latest_sync(rig)
    store = rig.storage()
    before = len(all_checkpoints(rig))
    bad_states = [
        {"x": object()},
        {"x": {1: "int key"}},
        {"x": {"__dataclass__": "subprocess:Popen", "fields": {}}},
        {"x": {"__pickled__": "AAAA"}},
        {"x": {"__event__": "not an event"}},
    ]

    for state in bad_states:
        poisoned = dataclasses.replace(
            template, checkpoint_id=str(uuid.uuid4()), state=state
        )
        with pytest.raises(WorkflowCheckpointException):
            leg(lambda cp=poisoned: store.save(cp))

    assert len(all_checkpoints(rig)) == before
    assert len(store.failures) == len(bad_states)


class Evil:
    """What a pickle-based store would execute at load: write a file."""

    def __init__(self, target: str) -> None:
        self.target = target

    def __reduce__(self) -> tuple[Any, ...]:
        return (Path.write_text, (Path(self.target), "executed"))


def test_a_planted_pickle_payload_stays_text_through_the_whole_path(
    scratch_schema: str, thread_id: uuid.UUID, tmp_path: Path
) -> None:
    sentinel = tmp_path / "executed"
    payload = base64.b64encode(pickle.dumps(Evil(str(sentinel)))).decode()
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    template = latest_sync(rig)
    codec = CheckpointCodec(CHECKPOINT_TYPES)
    document = codec.to_document(template)
    # Written by someone with write access to the table, in the form the
    # framework's own pickling store uses for a non-JSON value.
    document["state"]["planted"] = {"__pickled__": payload}
    planted_id = str(uuid.uuid4())
    document["checkpoint_id"] = planted_id
    with psycopg.connect(scratch_schema, autocommit=True) as conn:
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.{} (thread_id, checkpoint_id, workflow_name,"
                " checkpointed_at, body) VALUES (%s, %s, %s, now(), %s)"
            ).format(sql.Identifier(SCHEMA), sql.Identifier(TABLE)),
            (thread_id, planted_id, WORKFLOW_NAME, Jsonb(document)),
        )

    loaded = leg(lambda: rig.storage().load(planted_id))

    # It comes back as the same plain dict of text, never as a revived object.
    assert loaded.state["planted"] == {"__pickled__": payload}
    assert not sentinel.exists()
    # The control: the same text, handed to a pickle loader, does run.
    pickle.loads(base64.b64decode(payload))  # noqa: S301
    assert sentinel.read_text() == "executed"


def test_a_document_that_names_a_type_outside_the_rules_is_refused_not_imported(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    codec = CheckpointCodec(CHECKPOINT_TYPES)
    document = codec.to_document(latest_sync(rig))
    document["state"]["planted"] = {
        "__dataclass__": "subprocess:Popen",
        "fields": {"args": ["true"]},
    }
    planted_id = str(uuid.uuid4())
    document["checkpoint_id"] = planted_id
    with psycopg.connect(scratch_schema, autocommit=True) as conn:
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.{} (thread_id, checkpoint_id, workflow_name,"
                " checkpointed_at, body) VALUES (%s, %s, %s, now(), %s)"
            ).format(sql.Identifier(SCHEMA), sql.Identifier(TABLE)),
            (thread_id, planted_id, WORKFLOW_NAME, Jsonb(document)),
        )

    with pytest.raises(WorkflowCheckpointException, match="KeyError"):
        leg(lambda: rig.storage().load(planted_id))


def test_nothing_on_a_start_and_a_resume_calls_the_frameworks_pickle_codec(
    scratch_schema: str, thread_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def refuse(name: str) -> Any:
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
    rig = Rig(scratch_schema, thread_id)

    start(rig)
    _, result = resume(rig, {})

    assert describe(result)["paused"] is False
    assert calls == []
