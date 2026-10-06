"""What the codec checks of the types it is given and the documents it reads
(S037, X1). No database.

A registered dataclass is rebuilt only from fields of the kind it declares, so a
workload's state types are plain data by construction; a type the codec cannot
check is refused when it is registered, not when a row is read. And text the
database would refuse is refused wherever it sits in a checkpoint, the message
and event fields included.
"""

import dataclasses
import typing
from dataclasses import dataclass, make_dataclass
from enum import Enum
from typing import Any, Optional

import pytest
from agent_framework import WorkflowCheckpoint, WorkflowEvent
from workflowsupport import CHECKPOINT_TYPES, Dials, Drafted, Marker, MemoryStore, start

from meridian.runtime.workflow_checkpoints import (
    DATACLASS_TAG,
    FIELDS_KEY,
    CheckpointCodec,
    CodecRefusal,
    type_name,
)


@dataclass
class Inner:
    name: str


@dataclass
class Unlisted:
    name: str


class Colour(Enum):
    RED = "red"


def document_of(cls: type, **fields: Any) -> dict[str, Any]:
    return {DATACLASS_TAG: type_name(cls), FIELDS_KEY: fields}


def probe(annotation: Any) -> type:
    """A dataclass with one field of ``annotation``."""
    return make_dataclass("Probe", [("f", annotation)])


# ── what a type may declare ─────────────────────────────────────────────────
ALLOWED = [
    str,
    int,
    float,
    bool,
    None,
    list,
    dict,
    list[str],
    list[Inner],
    dict[str, int],
    dict[str, Inner],
    str | None,
    Optional[int],  # noqa: UP045 (the old spelling is one of the forms)
    int | str,
    Inner,
    Inner | None,
    list[str | None],
]
REFUSED = [
    typing.Any,
    object,
    tuple[str, ...],
    set[str],
    bytes,
    Colour,
    Unlisted,
    dict[int, str],
    list[typing.Any],
    dict[str, typing.Any],
    str | bytes,
    typing.Literal["a", "b"],
    list[Unlisted],
    typing.Callable[[], str],
]


@pytest.mark.parametrize("annotation", ALLOWED, ids=repr)
def test_a_field_of_a_kind_the_codec_can_check_is_accepted(annotation: Any) -> None:
    cls = probe(annotation)

    codec = CheckpointCodec([Inner, cls])

    assert codec is not None


@pytest.mark.parametrize("annotation", REFUSED, ids=repr)
def test_a_field_of_a_kind_the_codec_cannot_check_refuses_the_registration(
    annotation: Any,
) -> None:
    cls = probe(annotation)

    with pytest.raises(TypeError, match="cannot check"):
        CheckpointCodec([Inner, cls])


def test_a_field_named_by_a_string_annotation_is_resolved_and_checked() -> None:
    cls = make_dataclass("Probe", [("f", "list[Inner]")], module=__name__)

    codec = CheckpointCodec([Inner, cls])

    assert codec.decode(document_of(cls, f=[document_of(Inner, name="a")])).f == [
        Inner("a")
    ]


def test_a_type_with_a_post_init_is_refused_at_registration() -> None:
    @dataclass
    class Checked:
        name: str

        def __post_init__(self) -> None:
            return None

    with pytest.raises(TypeError, match="__post_init__"):
        CheckpointCodec([Checked])


def test_a_type_that_inherits_a_post_init_is_refused_too() -> None:
    @dataclass
    class Base:
        name: str

        def __post_init__(self) -> None:
            return None

    @dataclass
    class Child(Base):
        other: str = ""

    with pytest.raises(TypeError, match="__post_init__"):
        CheckpointCodec([Child])


def test_a_field_that_is_not_set_by_the_constructor_refuses_the_registration() -> None:
    @dataclass
    class Derived:
        name: str
        shout: str = dataclasses.field(init=False, default="")

    with pytest.raises(TypeError, match="constructor"):
        CheckpointCodec([Derived])


def test_a_type_with_a_constructor_of_its_own_is_refused_at_registration() -> None:
    @dataclass(init=False)
    class Mine:
        name: str

        def __init__(self, name: str) -> None:
            self.name = name.upper()

    with pytest.raises(TypeError, match="constructor"):
        CheckpointCodec([Mine])


def twin() -> type:
    @dataclass
    class Twin:
        name: str

    return Twin


def test_two_types_with_one_name_are_refused_at_registration() -> None:
    first, second = twin(), twin()
    assert type_name(first) == type_name(second) and first is not second

    with pytest.raises(TypeError, match="same name"):
        CheckpointCodec([first, second])


def test_one_type_listed_twice_is_one_registration() -> None:
    cls = twin()

    codec = CheckpointCodec([cls, cls])

    assert codec.decode(document_of(cls, name="a")) == cls("a")


# ── what a document may hold ────────────────────────────────────────────────
@pytest.fixture
def codec() -> CheckpointCodec:
    return CheckpointCodec([Inner, Drafted, Marker])


@pytest.mark.parametrize(
    "fields",
    [
        {"claim_id": 5, "brief": "b"},
        {"claim_id": {"a": 1}, "brief": "b"},
        {"claim_id": ["a"], "brief": "b"},
        {"claim_id": None, "brief": "b"},
        {"claim_id": True, "brief": "b"},
        {"claim_id": 1.5, "brief": "b"},
        {"claim_id": document_of(Inner, name="a"), "brief": "b"},
    ],
    ids=["int", "dict", "list", "none", "bool", "float", "dataclass"],
)
def test_a_field_of_the_wrong_kind_is_refused_and_nothing_is_built(
    codec: CheckpointCodec, fields: dict[str, Any]
) -> None:
    with pytest.raises(CodecRefusal) as refused:
        codec.decode(document_of(Drafted, **fields))

    assert refused.value.reason == "wrong-field-type"


@pytest.mark.parametrize(
    "fields",
    [{"claim_id": "a"}, {"brief": "b"}, {}, {"claim_id": "a", "brief": "b", "x": 1}],
    ids=["no-brief", "no-claim", "none", "extra"],
)
def test_a_document_with_missing_or_extra_fields_is_refused(
    codec: CheckpointCodec, fields: dict[str, Any]
) -> None:
    with pytest.raises(CodecRefusal) as refused:
        codec.decode(document_of(Drafted, **fields))

    assert refused.value.reason == "wrong-fields"


def test_the_right_kinds_are_built(codec: CheckpointCodec) -> None:
    built = codec.decode(document_of(Drafted, claim_id="a", brief="b"))

    assert built == Drafted("a", "b")


def test_an_integer_is_a_float_and_a_bool_is_not_an_integer() -> None:
    cls = make_dataclass("Mixed", [("price", float), ("count", int)])
    mixed = CheckpointCodec([cls])

    built = mixed.decode(document_of(cls, price=3, count=2))
    with pytest.raises(CodecRefusal):
        mixed.decode(document_of(cls, price=3.5, count=True))
    with pytest.raises(CodecRefusal):
        mixed.decode(document_of(cls, price=True, count=2))

    assert (built.price, built.count) == (3, 2)


def test_an_optional_field_takes_none_and_its_type_and_nothing_else() -> None:
    cls = make_dataclass("Maybe", [("inner", Inner | None), ("tags", list[str])])
    maybe = CheckpointCodec([Inner, cls])

    without = maybe.decode(document_of(cls, inner=None, tags=[]))
    with_one = maybe.decode(
        document_of(cls, inner=document_of(Inner, name="a"), tags=["x"])
    )
    for fields in (
        {"inner": "text", "tags": []},
        {"inner": None, "tags": [1]},
        {"inner": None, "tags": "x"},
        {"inner": document_of(Marker), "tags": []},
    ):
        with pytest.raises(CodecRefusal):
            maybe.decode(document_of(cls, **fields))

    assert without.inner is None
    assert with_one.inner == Inner("a")


def test_a_type_saved_with_a_field_of_the_wrong_kind_is_refused_at_save() -> None:
    cls = make_dataclass("Strict", [("name", str)])
    strict = CheckpointCodec([*CHECKPOINT_TYPES, cls])
    store = MemoryStore()
    start(Dials(), store)
    checkpoint = dataclasses.replace(store.saved[-1], state={"s": cls(name=5)})

    with pytest.raises(CodecRefusal) as refused:
        strict.to_restorable_document(checkpoint)

    assert refused.value.reason == "wrong-field-type"


# ── text the database refuses, in a message's and an event's own fields ─────
NUL = "before\x00after"
LONE_SURROGATE = "before\ud800after"


def entry_checkpoint() -> WorkflowCheckpoint:
    """The checkpoint before the first step: it holds the first message."""
    store = MemoryStore()
    start(Dials(), store)
    return store.saved[0]


def pause_checkpoint() -> WorkflowCheckpoint:
    store = MemoryStore()
    start(Dials(), store)
    return store.saved[-1]


def with_message(**changes: Any) -> WorkflowCheckpoint:
    checkpoint = entry_checkpoint()
    ((source, (message,)),) = checkpoint.messages.items()
    forged = dataclasses.replace(message, **changes)
    return dataclasses.replace(checkpoint, messages={source: [forged]})


def with_event(**changes: Any) -> WorkflowCheckpoint:
    checkpoint = pause_checkpoint()
    ((request_id, event),) = checkpoint.pending_request_info_events.items()
    arguments = {
        "request_id": request_id,
        "source_executor_id": event.source_executor_id,
        "request_data": event.data,
        "response_type": Marker,
    }
    forged = WorkflowEvent.request_info(**{**arguments, **changes})
    return dataclasses.replace(
        checkpoint, pending_request_info_events={forged.request_id: forged}
    )


CASES = {
    "a message's target": lambda text: with_message(target_id=text),
    "a message's source": lambda text: with_message(source_id=text),
    "a message's trace context": lambda text: with_message(
        trace_contexts=[{"traceparent": text}]
    ),
    "a message's span ID": lambda text: with_message(source_span_ids=[text]),
    "an event's request ID": lambda text: with_event(request_id=text),
    "an event's source executor": lambda text: with_event(source_executor_id=text),
}


@pytest.mark.parametrize("text", [NUL, LONE_SURROGATE], ids=["nul", "surrogate"])
@pytest.mark.parametrize("where", list(CASES))
def test_text_the_database_would_refuse_is_refused_wherever_a_checkpoint_holds_it(
    where: str, text: str
) -> None:
    codec = CheckpointCodec(CHECKPOINT_TYPES)
    checkpoint = CASES[where](text)

    with pytest.raises(CodecRefusal) as refused:
        codec.to_restorable_document(checkpoint)

    assert refused.value.reason == "unstorable-text"
    assert "\x00" not in str(refused.value) and "\ud800" not in str(refused.value)


def test_the_same_fields_with_ordinary_text_are_saved() -> None:
    codec = CheckpointCodec(CHECKPOINT_TYPES)

    for build in (with_message, with_event):
        assert codec.to_restorable_document(build()) is not None
