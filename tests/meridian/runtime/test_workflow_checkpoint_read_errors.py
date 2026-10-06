"""Only a refusal is a refusal (S037, F4, the security review's low 4).

The store turns a failure to read a stored checkpoint into one of two things.
``CheckpointUnreadable`` means the codec read the row and refuses it: no later
attempt can change that, and the host ends the run on it (``checkpoint-refused``).
The framework's own ``WorkflowCheckpointException`` means the read failed for a
reason that may pass, and the run stays paused to be resumed again. The codec's
refusals and the errors a wrong shape raises are the first kind; anything else (a
``MemoryError``, an ``OSError``) is the second, because ending a run for good on
a fault that was gone a second later strands a brief for ever.

These tests need no database: ``_read`` is the store's alone, over its codec.
"""

import uuid
from typing import Any

import pytest
from agent_framework.exceptions import WorkflowCheckpointException

from meridian.runtime.workflow_checkpoints import (
    CheckpointCodec,
    CheckpointUnreadable,
    CodecRefusal,
    PostgresCheckpointStore,
)

SENTENCE = "claimant-text-in-an-error"


def a_store() -> PostgresCheckpointStore:
    return PostgresCheckpointStore("postgresql://unused.invalid/x", uuid.uuid4(), ())


def read_with(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> WorkflowCheckpointException:
    """The exception ``_read`` raises when the codec raises ``error``."""

    def refusing(self: CheckpointCodec, document: Any) -> Any:
        raise error

    monkeypatch.setattr(CheckpointCodec, "from_document", refusing)
    with pytest.raises(WorkflowCheckpointException) as raised:
        a_store()._read({})
    return raised.value


# ── what the codec refuses, and what a wrong shape raises ───────────────────
@pytest.mark.parametrize(
    "error",
    [
        CodecRefusal("unknown-tag"),
        TypeError(SENTENCE),
        ValueError(SENTENCE),
        KeyError(SENTENCE),
        AttributeError(SENTENCE),
        WorkflowCheckpointException(SENTENCE),
    ],
    ids=lambda error: type(error).__name__,
)
def test_what_the_codec_refuses_and_what_a_wrong_shape_raises_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    raised = read_with(monkeypatch, error)

    assert isinstance(raised, CheckpointUnreadable)
    assert SENTENCE not in str(raised)
    assert raised.__cause__ is None
    assert raised.__context__ is None


# ── anything else is a fault that may pass ──────────────────────────────────
@pytest.mark.parametrize(
    "error",
    [
        MemoryError(SENTENCE),
        OSError(SENTENCE),
        RuntimeError(SENTENCE),
        RecursionError(SENTENCE),
        ImportError(SENTENCE),
        IndexError(SENTENCE),
    ],
    ids=lambda error: type(error).__name__,
)
def test_any_other_error_of_the_codec_stays_the_transient_failure(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    raised = read_with(monkeypatch, error)

    assert type(raised) is WorkflowCheckpointException
    assert type(error).__name__ in str(raised)
    assert SENTENCE not in str(raised)
    assert raised.__cause__ is None
    assert raised.__context__ is None


# ── the real codec, on real wrong shapes ────────────────────────────────────
@pytest.mark.parametrize(
    "document",
    [
        pytest.param("not a document", id="a-string"),
        pytest.param([], id="a-list"),
        pytest.param({}, id="no-fields"),
        pytest.param({"__event__": {}}, id="an-event-with-no-fields"),
        pytest.param({"__message__": "x"}, id="a-message-that-is-text"),
        pytest.param({"__pickle__": "x"}, id="a-tag-that-is-not-the-codecs"),
        pytest.param(
            {"__dataclass__": "os:system", "fields": {}}, id="a-type-nobody-registered"
        ),
    ],
)
def test_the_real_codec_s_refusals_of_wrong_shapes_are_unreadable(
    document: Any,
) -> None:
    with pytest.raises(CheckpointUnreadable):
        a_store()._read(document)
