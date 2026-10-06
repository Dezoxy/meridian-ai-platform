"""The codec of the second host's checkpoints: JSON only, only what comes back
(S037, R3b). No database: a real checkpoint comes from a real workflow that
pauses, saved by a store that keeps what it is given."""

import base64
import copy
import dataclasses
import json
import pickle
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
from agent_framework import WorkflowCheckpoint
from workflowsupport import (
    BRIEF,
    CHECKPOINT_TYPES,
    CLAIM,
    Dials,
    Drafted,
    Filing,
    Marker,
    MemoryStore,
    answer_latest,
    start,
)

from meridian.runtime.workflow_checkpoints import (
    DATACLASS_TAG,
    EVENT_TAG,
    MESSAGE_TAG,
    CheckpointCodec,
    CodecRefusal,
    type_name,
)


@pytest.fixture
def codec() -> CheckpointCodec:
    return CheckpointCodec(CHECKPOINT_TYPES)


@pytest.fixture
def paused() -> list[WorkflowCheckpoint]:
    """Every checkpoint of a run up to its pause, then of the resume after it."""
    store = MemoryStore()
    start(Dials(), store)
    answer_latest(Dials(), store)
    return store.saved


def pause_of(checkpoints: list[WorkflowCheckpoint]) -> WorkflowCheckpoint:
    return next(c for c in checkpoints if c.pending_request_info_events)


def tags_in(value: Any) -> set[str]:
    """Every tag and registered name a document holds."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.startswith("__"):
                found.add(key)
            if key == DATACLASS_TAG:
                found.add(item)
            found |= tags_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= tags_in(item)
    return found


# ── what comes back ─────────────────────────────────────────────────────────
def test_every_checkpoint_of_a_pause_and_its_resume_comes_back_as_it_went(
    codec: CheckpointCodec, paused: list[WorkflowCheckpoint]
) -> None:
    for stored in paused:
        text = json.dumps(codec.to_document(stored))

        again = codec.from_document(json.loads(text))

        assert codec.to_document(again) == codec.to_document(stored)
        assert again.checkpoint_id == stored.checkpoint_id
        assert again.iteration_count == stored.iteration_count


def test_the_pause_comes_back_with_its_request_its_data_and_its_response_type(
    codec: CheckpointCodec, paused: list[WorkflowCheckpoint]
) -> None:
    pause = pause_of(paused)

    again = codec.from_document(json.loads(json.dumps(codec.to_document(pause))))

    (event,) = again.pending_request_info_events.values()
    assert event.data == Drafted(CLAIM, BRIEF)
    assert event.response_type is Marker
    assert event.source_executor_id == "ask"
    assert again.workflow_name == pause.workflow_name


def test_a_checkpoint_holds_the_three_tags_and_the_registered_names_it_needs(
    codec: CheckpointCodec, paused: list[WorkflowCheckpoint]
) -> None:
    seen: set[str] = set()
    text = ""

    for stored in paused:
        seen |= tags_in(codec.to_document(stored))
        text += json.dumps(codec.to_document(stored))

    assert {EVENT_TAG, MESSAGE_TAG, DATACLASS_TAG} <= seen
    assert {type_name(Drafted), type_name(Filing)} <= seen
    # The marker is named inside the request, which the framework writes itself.
    assert f"{Marker.__module__}.{Marker.__qualname__}" in text


@pytest.mark.parametrize("missing", CHECKPOINT_TYPES)
def test_a_type_without_a_registration_refuses_the_save_not_the_resume(
    paused: list[WorkflowCheckpoint], missing: type
) -> None:
    narrow = CheckpointCodec([t for t in CHECKPOINT_TYPES if t is not missing])

    reasons = []
    for stored in paused:
        try:
            narrow.to_restorable_document(stored)
        except CodecRefusal as error:
            reasons.append(error.reason)

    assert reasons
    assert set(reasons) <= {"unregistered-type", "unknown-name"}


# ── what is refused at save ─────────────────────────────────────────────────
class Colour(StrEnum):
    RED = "red"


@dataclasses.dataclass
class Outsider:
    claim_id: str


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ({"x": object()}, "unregistered-type"),
        ({"x": Outsider("CLM-0001")}, "unregistered-type"),
        ({"x": {1: "an int key"}}, "unstorable-key"),
        ({"x": {"__dataclass__": "a name"}}, "unstorable-key"),
        ({"x": {"__pickled__": "AAAA"}}, "unstorable-key"),
        ({"x": float("nan")}, "non-finite-number"),
        ({"x": float("inf")}, "non-finite-number"),
        ({"x": (1, 2)}, "unregistered-type"),
        ({"x": {1, 2}}, "unregistered-type"),
        ({"x": Colour.RED}, "unregistered-type"),
        ({"x": b"bytes"}, "unregistered-type"),
    ],
)
def test_a_value_the_codec_cannot_restore_refuses_the_save_with_a_fixed_reason(
    codec: CheckpointCodec,
    paused: list[WorkflowCheckpoint],
    state: dict[str, Any],
    reason: str,
) -> None:
    poisoned = dataclasses.replace(pause_of(paused), state=state)

    with pytest.raises(CodecRefusal) as refused:
        codec.to_restorable_document(poisoned)

    assert refused.value.reason == reason
    assert str(refused.value) == reason


def test_a_value_that_does_not_come_back_the_same_refuses_the_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclasses.dataclass
    class Lossy:
        value: str

    codec = CheckpointCodec([*CHECKPOINT_TYPES, Lossy])

    def lossy_init(self: Lossy, value: str) -> None:
        # Every construction changes the value: a restore is not the same. (A
        # type that does this is refused at registration; it is put in place
        # after, so that the save's own comparison is what is shown.)
        self.value = value + "!"

    monkeypatch.setattr(Lossy, "__init__", lossy_init)
    store = MemoryStore()
    start(Dials(), store)
    template = store.saved[-1]
    lossy = dataclasses.replace(template, state={"x": Lossy("quiet")})

    with pytest.raises(CodecRefusal) as refused:
        codec.to_restorable_document(lossy)

    assert refused.value.reason == "not-restorable"


def test_a_type_that_is_not_a_dataclass_cannot_be_registered() -> None:
    with pytest.raises(TypeError):
        CheckpointCodec([int])


def test_a_same_named_class_is_not_the_registered_one() -> None:
    codec = CheckpointCodec([Drafted])

    @dataclasses.dataclass
    class Drafted2:
        claim_id: str
        brief: str

    Drafted2.__qualname__ = Drafted.__qualname__
    Drafted2.__module__ = Drafted.__module__
    impostor = Drafted2(CLAIM, BRIEF)

    with pytest.raises(CodecRefusal) as refused:
        codec.encode(impostor)

    assert refused.value.reason == "unregistered-type"


# ── what is refused at load ─────────────────────────────────────────────────
class Evil:
    """What a store that unpickles what it reads would execute: write a file."""

    def __init__(self, target: str) -> None:
        self.target = target

    def __reduce__(self) -> tuple[Any, ...]:
        return (Path.write_text, (Path(self.target), "executed"))


def planted(document: Any, **state: Any) -> Any:
    forged = copy.deepcopy(document)
    forged["state"].update(state)
    return forged


def test_a_planted_pickle_payload_is_refused_as_text_and_the_control_runs(
    codec: CheckpointCodec, paused: list[WorkflowCheckpoint], tmp_path: Path
) -> None:
    sentinel = tmp_path / "executed"
    payload = base64.b64encode(pickle.dumps(Evil(str(sentinel)))).decode()
    document = codec.to_document(pause_of(paused))
    forged = planted(document, planted={"__pickled__": payload})

    with pytest.raises(CodecRefusal) as refused:
        codec.from_document(forged)

    assert refused.value.reason == "unknown-tag"
    assert not sentinel.exists()
    # The control: the same text, handed to a pickle loader, does run.
    pickle.loads(base64.b64decode(payload))  # noqa: S301
    assert sentinel.read_text() == "executed"


@pytest.mark.parametrize(
    "name", ["subprocess:Popen", "os:system", "builtins:object", "builtins:eval"]
)
def test_a_document_that_names_a_type_outside_the_registry_is_refused_not_imported(
    codec: CheckpointCodec, paused: list[WorkflowCheckpoint], name: str
) -> None:
    document = codec.to_document(pause_of(paused))
    forged = planted(
        document, planted={DATACLASS_TAG: name, "fields": {"args": ["true"]}}
    )

    with pytest.raises(CodecRefusal) as refused:
        codec.from_document(forged)

    assert refused.value.reason == "unregistered-type"


@pytest.mark.parametrize("field", ["request_type", "response_type"])
@pytest.mark.parametrize(
    "name", ["subprocess.Popen", "builtins.object", "os.PathLike", "no.such.Type"]
)
def test_a_planted_request_naming_a_loaded_type_is_refused_before_the_framework_runs(
    codec: CheckpointCodec, paused: list[WorkflowCheckpoint], field: str, name: str
) -> None:
    document = codec.to_document(pause_of(paused))
    (request_id,) = document["pending_request_info_events"]
    forged = copy.deepcopy(document)
    forged["pending_request_info_events"][request_id][EVENT_TAG][field] = name

    with pytest.raises(CodecRefusal) as refused:
        codec.from_document(forged)

    assert refused.value.reason == "unknown-name"


@pytest.mark.parametrize(
    "tagged",
    [
        {EVENT_TAG: {}, "extra": 1},
        {MESSAGE_TAG: {}, DATACLASS_TAG: "x"},
        {DATACLASS_TAG: "workflowsupport:Marker"},
        {"__reduce__": ["os.system", "true"]},
    ],
)
def test_a_tagged_mapping_of_another_shape_is_refused(
    codec: CheckpointCodec, paused: list[WorkflowCheckpoint], tagged: dict[str, Any]
) -> None:
    forged = planted(codec.to_document(pause_of(paused)), planted=tagged)

    with pytest.raises(CodecRefusal) as refused:
        codec.from_document(forged)

    assert refused.value.reason == "unknown-tag"
