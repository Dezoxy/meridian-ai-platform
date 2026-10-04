"""The recorded provider and the recording wrapper (S050, T-78).

A recording is a file of answers a real model gave, each found by the SHA-256 of
the request the provider was sent. ``RecordedProvider`` replays it; nothing here
reaches a network, and no SDK is imported. ``RecordingProvider`` wraps a real
provider and stores what it answered; only the evaluation's recording run, in
process, builds it, and no environment variable switches it on.

An entry holds the six fields of the reply the walk reads and the latency, and
nothing else: not the request, not a header, not an ID. The key is the hash of
the ``ChatRequest`` the provider receives, so after the gateway's redaction and
output budget, and never of the tenant, the agent or the run: a changed prompt
finds no entry, and the call is refused with its own error kind.
"""

import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    Field,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
)

from meridian.platform.common.jsonfile import (
    HexDigest,
    JsonFileError,
    describe_validation_error,
    read_json_file,
)
from meridian.platform.common.wire import WireModel
from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ModelProvider,
    ProviderError,
    ProviderReply,
)
from meridian.platform.registry.models import Deployment

RECORDING_FORMAT = 1
MILLISECONDS_PER_SECOND = 1000
# What the Azure adapter's MODEL_NAME takes, repeated because the adapter
# imports the SDK and this module must not (a test keeps the two equal). The
# replayed model reaches a span attribute and a column with a length check.
MODEL_NAME_PATTERN = r"[A-Za-z0-9._:-]{1,128}"

Count = Annotated[StrictInt, Field(ge=0)]
ModelName = Annotated[StrictStr, StringConstraints(pattern=f"^{MODEL_NAME_PATTERN}$")]
RecordingLabel = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9-]*$", max_length=64)
]


class RecordingError(JsonFileError):
    """A recording cannot be read; the message never quotes its content."""


def request_key(request: ChatRequest) -> str:
    """The SHA-256 (64 hex digits) of the canonical JSON of ``request``: keys
    sorted, compact separators, UTF-8 not escaped, a field that is ``None``
    left out."""
    canonical = json.dumps(
        request.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RecordedAnswer(WireModel):
    """What one recorded call answered: exactly these fields and no other."""

    text: StrictStr
    finish_reason: Literal["stop", "length"]
    model: ModelName
    input_tokens: Count
    output_tokens: Count
    latency_ms: Count


class Recording(WireModel):
    """The file: ``recorded_for`` holds labels the caller chooses (for example
    a prompt version) with the hash each stands for."""

    format: Annotated[StrictInt, Field(ge=RECORDING_FORMAT, le=RECORDING_FORMAT)]
    recorded_for: dict[RecordingLabel, HexDigest]
    entries: dict[HexDigest, RecordedAnswer]


def dump_recording(recording: Recording) -> str:
    """The canonical text of ``recording``: byte-stable for equal recordings."""
    return (
        json.dumps(
            recording.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )


def write_recording(recording: Recording, path: Path) -> None:
    """Write ``recording`` to ``path``; the directory must exist. The text goes
    to a temporary file beside ``path`` and replaces it in one step, so a reader
    never sees half a recording."""
    text = dump_recording(recording)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def load_recording(path: Path) -> Recording:
    """Read and validate a recording; raise ``RecordingError`` for any defect,
    with a message that names no content of the file."""
    try:
        document = read_json_file(path)
    except JsonFileError as error:
        raise RecordingError(str(error)) from None
    try:
        return Recording.model_validate(document)
    except ValidationError as error:
        raise RecordingError(describe_validation_error(error)) from None


def _reply_of(answer: RecordedAnswer) -> ProviderReply:
    return ProviderReply(
        text=answer.text,
        finish_reason=answer.finish_reason,
        model=answer.model,
        input_tokens=answer.input_tokens,
        output_tokens=answer.output_tokens,
    )


class RecordedProvider:
    """Replays a recording. A request it holds no entry for is refused with
    ``not-recorded`` before anything is sent. Safe under the gateway's threads."""

    def __init__(self, recording: Recording) -> None:
        self._entries: Mapping[str, RecordedAnswer] = dict(recording.entries)
        self._lock = threading.Lock()
        self._asked: set[str] = set()
        self._missed: list[str] = []

    @property
    def missed(self) -> tuple[str, ...]:
        """The keys asked and not found, in the order they were asked."""
        with self._lock:
            return tuple(self._missed)

    def unused(self) -> tuple[str, ...]:
        """The keys of the entries no call asked for, sorted."""
        with self._lock:
            return tuple(sorted(self._entries.keys() - self._asked))

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        key = request_key(request)
        answer = self._entries.get(key)
        with self._lock:
            if answer is None:
                self._missed.append(key)
            else:
                self._asked.add(key)
        if answer is None:
            raise ProviderError("not-recorded", sent=False)
        return _reply_of(answer)

    def embed(
        self,
        deployment: Deployment,
        request: EmbeddingRequest,
        *,
        timeout_seconds: float,
    ) -> EmbeddingReply:
        """Recorded mode answers embeddings with the replay provider."""
        raise ProviderError("not-recorded", sent=False)


class RecordingProvider:
    """Calls ``inner`` and stores each reply under its request's key. A failed
    call stores nothing and its error propagates unchanged; the same key asked
    twice keeps the last answer. Safe under the gateway's threads."""

    def __init__(
        self,
        inner: ModelProvider,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._now = now
        self._lock = threading.Lock()
        self._entries: dict[str, RecordedAnswer] = {}

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        started = self._now()
        reply = self._inner.chat(deployment, request, timeout_seconds=timeout_seconds)
        elapsed_ms = max(0, round((self._now() - started) * MILLISECONDS_PER_SECOND))
        answer = RecordedAnswer(
            text=reply.text,
            finish_reason=reply.finish_reason,
            model=reply.model,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            latency_ms=elapsed_ms,
        )
        with self._lock:
            self._entries[request_key(request)] = answer
        return reply

    def embed(
        self,
        deployment: Deployment,
        request: EmbeddingRequest,
        *,
        timeout_seconds: float,
    ) -> EmbeddingReply:
        """Delegates and records nothing."""
        return self._inner.embed(deployment, request, timeout_seconds=timeout_seconds)

    def recording(self, recorded_for: Mapping[str, str]) -> Recording:
        """What was stored so far, as a recording labelled ``recorded_for``."""
        with self._lock:
            entries = dict(self._entries)
        return Recording(
            format=RECORDING_FORMAT, recorded_for=dict(recorded_for), entries=entries
        )
