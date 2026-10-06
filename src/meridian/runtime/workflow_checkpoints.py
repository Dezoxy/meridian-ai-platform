"""The checkpoint store of the second agent framework (S037).

Microsoft Agent Framework ships no PostgreSQL store, and every store it ships
pickles what it saves. This module is the Agent Runtime's own: a codec that
writes JSON and nothing else, and a store over ``runtime.workflow_checkpoints``
(migration 0023) that keeps one run's checkpoints, found by the run's thread.

Three rules hold the module together.

* **JSON only, and only what can be restored.** The codec takes primitives,
  lists, string-keyed mappings, the framework's request events and messages,
  and dataclasses the caller registered, each stored under a tag of this
  module's own. A document that names any other type is refused, whatever
  module it names: nothing is imported or called because a row says so. No
  ``pickle``, no ``eval``. A value the codec cannot restore refuses the SAVE,
  before a row is written: that includes text PostgreSQL's ``jsonb`` refuses (a
  NUL, a lone surrogate) and a float it would give back as an integer (from ten
  to the sixteenth up). A registered dataclass is plain data: no
  ``__post_init__``, a constructor of the dataclass's own, every field of a kind
  the codec can check, and a document is rebuilt only from fields of the kind
  each declares. Two types of one name are refused when they are registered.
* **No database text leaves the store.** The framework logs the text of what a
  store raises, and a driver's error quotes the row it refused. Every
  ``psycopg.Error`` is therefore turned into the framework's own exception with
  a fixed sentence, the driver error's class name and its sqlstate, and nothing
  of the original attached.
* **One thread.** A store is built for one run's thread and every statement
  says ``WHERE thread_id = <that thread>``: another thread's checkpoint ID is
  "not found", never another run's state.

The store's methods are ``async`` because the framework's protocol is. Each
opens a connection of its own (the runtime's role, autocommit, the service's
timeouts) with the asynchronous driver, so the event loop of a leg is never
blocked on the database, and closes it before it returns. That costs one
connect per call, four to six a leg; the protocol has no ``close`` for a kept
connection to be released by, so a connection kept for the leg would have no
owner. The module imports the framework: nothing under ``meridian.platform``
and not ``meridian.runtime.sweep`` may import it.
"""

import dataclasses
import json
import math
import types
import typing
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

import psycopg
from agent_framework import WorkflowCheckpoint, WorkflowEvent, WorkflowMessage
from agent_framework.exceptions import WorkflowCheckpointException
from psycopg import sql
from psycopg.types.json import Jsonb

from meridian.platform.common.db import CONNECT_TIMEOUT_SECONDS, STATEMENT_TIMEOUT_MS
from meridian.runtime import SERVICE_NAME
from meridian.runtime.sweep import GUARDED_DELETE, SWEPT_STATUSES

TABLE_NAME = "workflow_checkpoints"
TABLE = f"runtime.{TABLE_NAME}"
type Statement = str | sql.Composable
type Params = tuple[Any, ...] | Mapping[str, Any]
# The tags of this module's own. A key of a stored mapping that begins with two
# underscores is refused at save, so a stored mapping never collides with them.
EVENT_TAG = "__event__"
MESSAGE_TAG = "__message__"
DATACLASS_TAG = "__dataclass__"
FIELDS_KEY = "fields"
RESERVED_PREFIX = "__"
# Exact types: a subclass (an enum that is also a string) would come back as its
# base, which is not what was saved.
PRIMITIVE_TYPES = (bool, int, float, str)
# The types a request may name besides the caller's own dataclasses.
EVENT_BUILTIN_TYPES = (str, int, float, bool, dict, list)

# The reasons a refusal gives, fixed words that carry no data.
UNREGISTERED_TYPE = "unregistered-type"
UNSTORABLE_KEY = "unstorable-key"
NON_FINITE_NUMBER = "non-finite-number"
UNKNOWN_TAG = "unknown-tag"
UNKNOWN_NAME = "unknown-name"
NOT_RESTORABLE = "not-restorable"
NAIVE_TIMESTAMP = "naive-timestamp"
UNSTORABLE_TEXT = "unstorable-text"
WRONG_FIELDS = "wrong-fields"
WRONG_FIELD_TYPE = "wrong-field-type"
# A float of this size or more is refused. Python writes ``1e+16`` for it, with an
# exponent; ``jsonb`` turns that into its digits and a number of digits with no
# fraction is read back as an INTEGER, so the value does not come back as it
# went (and for most such floats, not even as the same number: ``1e300`` has the
# digits of 1 and 300 zeros, not of the float's exact value). Below it Python
# writes the positional form, which ``jsonb`` keeps, and a float comes back a
# float. Found against the database, with the boundary on both sides, in
# tests/meridian/runtime/test_workflow_checkpoint_store_rules.py. Every float of
# this size has no fraction, so the rule loses nothing but large magnitudes.
JSONB_FLOAT_LIMIT = 1e16


class CodecRefusal(ValueError):
    """The codec refuses a value or a document. The text is a fixed reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# What reading a document may raise to say it is not one this codec can restore:
# the codec's own refusal (a ``ValueError``), the errors a wrong shape raises (a
# field missing, a value of the wrong type or kind), and the framework's own
# exception, which ``WorkflowCheckpoint.from_dict`` raises for a document that is
# not a checkpoint's fields. Everything else is not a verdict on the row.
READ_REFUSALS = (
    CodecRefusal,
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    WorkflowCheckpointException,
)


class CheckpointUnreadable(WorkflowCheckpointException):
    """A stored checkpoint was read from the database and the codec refuses it.

    Raised by ``_read`` and by nothing else. It is the one failure of a read that
    trying again cannot cure: the row is what it is, and the code's types are
    what they are. A store that could not be reached raises the framework's own
    ``WorkflowCheckpointException`` with no subclass, so the host can tell the
    two by class and never by text."""


def _require_storable_text(text: str) -> None:
    """Refuse a string PostgreSQL's ``jsonb`` refuses (a NUL: sqlstate 22P05) and
    one the driver cannot encode (a lone surrogate: an unwrapped
    ``UnicodeEncodeError`` that names the character), with a fixed reason."""
    unstorable = "\x00" in text
    if not unstorable:
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            unstorable = True
    if unstorable:
        raise CodecRefusal(UNSTORABLE_TEXT)


def _is_number(value: Any) -> bool:
    """An int or a float, never a bool: a field typed ``float`` may hold ``3``."""
    return type(value) in (int, float)


# The kinds of a field that need no look at the registry. A bare ``list`` or
# ``dict`` is checked for its container only: what is inside was built by this
# codec from the document, so it is primitives, lists, mappings and registered
# types, and nothing else.
_SIMPLE_CHECKS: dict[Any, Callable[[Any], bool]] = {
    str: lambda value: type(value) is str,
    int: lambda value: type(value) is int,
    bool: lambda value: type(value) is bool,
    float: _is_number,
    type(None): lambda value: value is None,
    list: lambda value: isinstance(value, list),
    dict: lambda value: isinstance(value, dict),
}


def type_name(cls: type) -> str:
    """The name a registered dataclass is stored under."""
    return f"{cls.__module__}:{cls.__qualname__}"


def _event_name(cls: type) -> str:
    """The name the framework gives a type inside a request event."""
    return f"{cls.__module__}.{cls.__qualname__}"


class CheckpointCodec:
    """Turns a ``WorkflowCheckpoint`` into a JSON document and back.

    ``types`` are the dataclasses a workflow keeps in its messages and requests:
    the workload's state types and the host's response marker. Only these are
    ever built from a document.
    """

    def __init__(self, types: Iterable[type]) -> None:
        registered = list(types)
        for cls in registered:
            if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
                raise TypeError("a checkpoint type must be a dataclass")
        self._types: dict[str, type] = {}
        for cls in registered:
            if self._types.setdefault(type_name(cls), cls) is not cls:
                raise TypeError("two checkpoint types have the same name")
        self._event_types = {
            _event_name(cls): cls for cls in (*registered, *EVENT_BUILTIN_TYPES)
        }
        # Checked when the type is registered, so a workload whose state cannot
        # be restored safely fails at start and not when a row is read.
        self._checks = {cls: self._field_checks(cls) for cls in self._types.values()}

    # ── registration ────────────────────────────────────────────────────────
    def _field_checks(self, cls: type) -> dict[str, Callable[[Any], bool]]:
        """One check per field of ``cls``, from its declared type. A registered
        type is plain data: no ``__post_init__`` (a constructor that runs code on
        what a row holds), every field set by the constructor, and every field of
        a kind the codec can check."""
        if hasattr(cls, "__post_init__"):
            raise TypeError("a checkpoint type must not define __post_init__")
        fields = dataclasses.fields(cls)
        if not all(field.init for field in fields):
            raise TypeError(
                "a checkpoint type's fields must all be set by its constructor"
            )
        if not cls.__dataclass_params__.init:  # type: ignore[attr-defined]
            raise TypeError(
                "a checkpoint type's constructor must be the one the dataclass makes"
            )
        try:
            hints = typing.get_type_hints(cls)
            return {field.name: self._checker(hints[field.name]) for field in fields}
        except (NameError, TypeError, KeyError):
            pass
        raise TypeError(
            f"a field of checkpoint type {cls.__qualname__} has a type the codec "
            "cannot check: use str, int, float, bool, None, list, dict, a "
            "registered dataclass, or a list, dict or union of those"
        )

    def _checker(self, kind: Any) -> Callable[[Any], bool]:
        """A function that says whether a decoded value is of ``kind``, for the
        kinds the codec itself produces. Raises ``TypeError`` for any other."""
        simple = _SIMPLE_CHECKS.get(kind)
        if simple is not None:
            return simple
        if kind in self._types.values():
            return lambda value: type(value) is kind
        origin, args = typing.get_origin(kind), typing.get_args(kind)
        if origin in (typing.Union, types.UnionType):
            options = [self._checker(option) for option in args]
            return lambda value: any(option(value) for option in options)
        if origin is list and len(args) == 1:
            element = self._checker(args[0])
            return lambda value: isinstance(value, list) and all(map(element, value))
        if origin is dict and len(args) == 2 and args[0] is str:
            member = self._checker(args[1])
            return lambda value: (
                isinstance(value, dict) and all(map(member, value.values()))
            )
        raise TypeError("a type the codec cannot check")

    # ── encoding ────────────────────────────────────────────────────────────
    def encode(self, value: Any) -> Any:
        if value is None or type(value) in PRIMITIVE_TYPES:
            return self._primitive(value)
        if isinstance(value, list):
            return [self.encode(item) for item in value]
        if isinstance(value, dict):
            return self._mapping(value)
        if isinstance(value, WorkflowEvent):
            return {EVENT_TAG: self._event(value)}
        if isinstance(value, WorkflowMessage):
            return {MESSAGE_TAG: self._message(value)}
        return self._dataclass(value)

    @staticmethod
    def _primitive(value: Any) -> Any:
        if type(value) is str:
            _require_storable_text(value)
        elif type(value) is float:
            if not math.isfinite(value):
                raise CodecRefusal(NON_FINITE_NUMBER)
            if abs(value) >= JSONB_FLOAT_LIMIT:
                # jsonb would give it back as an integer (see the constant).
                raise CodecRefusal(NOT_RESTORABLE)
        return value

    def _mapping(self, value: Mapping[Any, Any]) -> dict[str, Any]:
        for key in value:
            if not isinstance(key, str) or key.startswith(RESERVED_PREFIX):
                raise CodecRefusal(UNSTORABLE_KEY)
            _require_storable_text(key)
        return {key: self.encode(item) for key, item in value.items()}

    def _event(self, event: WorkflowEvent[Any]) -> dict[str, Any]:
        # Every field goes through ``encode``, not only the data: the IDs and
        # type names are text too, and text the database refuses must be
        # refused here.
        return {key: self.encode(item) for key, item in event.to_dict().items()}

    def _message(self, message: WorkflowMessage) -> dict[str, Any]:
        return {key: self.encode(item) for key, item in message.to_dict().items()}

    def _dataclass(self, value: Any) -> dict[str, Any]:
        name = type_name(type(value))
        if self._types.get(name) is not type(value):
            raise CodecRefusal(UNREGISTERED_TYPE)
        return {
            DATACLASS_TAG: name,
            FIELDS_KEY: {
                field.name: self.encode(getattr(value, field.name))
                for field in dataclasses.fields(value)
            },
        }

    # ── decoding ────────────────────────────────────────────────────────────
    def decode(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.decode(item) for item in value]
        if not isinstance(value, dict):
            return value
        tags = [key for key in value if key.startswith(RESERVED_PREFIX)]
        if not tags:
            return {key: self.decode(item) for key, item in value.items()}
        if tags == [EVENT_TAG] and len(value) == 1:
            return self._decode_event(value[EVENT_TAG])
        if tags == [MESSAGE_TAG] and len(value) == 1:
            return self._decode_message(value[MESSAGE_TAG])
        if tags == [DATACLASS_TAG] and set(value) == {DATACLASS_TAG, FIELDS_KEY}:
            return self._decode_dataclass(value)
        # Any other key of the reserved kind (a pickle's tag, say) is not ours.
        raise CodecRefusal(UNKNOWN_TAG)

    def _decode_event(self, fields: Any) -> WorkflowEvent[Any]:
        for key in ("request_type", "response_type"):
            # The framework would resolve a name to any type loaded in the
            # process; only the names this codec was given are allowed through.
            if fields[key] not in self._event_types:
                raise CodecRefusal(UNKNOWN_NAME)
        event = {**fields, "data": self.decode(fields["data"])}
        return WorkflowEvent.from_dict(event, allowed_types=self._event_types)

    def _decode_message(self, fields: Any) -> WorkflowMessage:
        return WorkflowMessage.from_dict(
            {
                **fields,
                "data": self.decode(fields["data"]),
                "original_request_info_event": self.decode(
                    fields["original_request_info_event"]
                ),
            }
        )

    def _decode_dataclass(self, value: Mapping[str, Any]) -> Any:
        name = value[DATACLASS_TAG]
        cls = self._types.get(name) if isinstance(name, str) else None
        if cls is None:
            raise CodecRefusal(UNREGISTERED_TYPE)
        checks = self._checks[cls]
        fields = value[FIELDS_KEY]
        if not isinstance(fields, dict) or set(fields) != set(checks):
            raise CodecRefusal(WRONG_FIELDS)
        built = {field: self.decode(item) for field, item in fields.items()}
        # Nothing is constructed from a field of the wrong kind.
        if not all(checks[field](item) for field, item in built.items()):
            raise CodecRefusal(WRONG_FIELD_TYPE)
        return cls(**built)

    # ── a whole checkpoint ──────────────────────────────────────────────────
    def to_document(self, checkpoint: WorkflowCheckpoint) -> Any:
        return self.encode(checkpoint.to_dict())

    def from_document(self, document: Any) -> WorkflowCheckpoint:
        return WorkflowCheckpoint.from_dict(self.decode(document))

    def to_restorable_document(self, checkpoint: WorkflowCheckpoint) -> Any:
        """The document of ``checkpoint``, once it is shown to come back as the
        same document through JSON text and ``from_document``: refuse now what
        could not be restored later."""
        document = self.to_document(checkpoint)
        restored = self.from_document(json.loads(json.dumps(document)))
        if self.to_document(restored) != document:
            raise CodecRefusal(NOT_RESTORABLE)
        return document


@dataclasses.dataclass(frozen=True)
class _Failure:
    """What a failed statement leaves behind: the driver error's class name and
    sqlstate, never the error itself (its text quotes the row)."""

    name: str
    sqlstate: str | None

    def text(self, what: str) -> str:
        return f"{what}: {self.name} ({self.sqlstate or 'no sqlstate'})"


@dataclasses.dataclass(frozen=True)
class _Outcome:
    """One statement's result: its rows and row count, or its failure."""

    rows: list[tuple[Any, ...]]
    count: int
    failure: _Failure | None


class PostgresCheckpointStore:
    """The framework's ``CheckpointStorage`` over ``runtime.workflow_checkpoints``
    for one run's thread, plus ``forget`` and ``failures`` for the host.

    ``dsn`` is the runtime role's. ``thread_id`` is the run's ``thread_id`` from
    its row (a ``uuid.UUID``, so its text is the canonical one the sweep's
    statements compare), never a caller's value. ``types`` are the dataclasses
    the workflow keeps in its state (see ``CheckpointCodec``).

    The framework only logs a save that failed, so the host reads ``failures``
    after a leg and fails the leg when it is not empty.
    """

    def __init__(self, dsn: str, thread_id: uuid.UUID, types: Iterable[type]) -> None:
        if not isinstance(thread_id, uuid.UUID):
            raise TypeError("a store is bound to the run's thread_id, a uuid")
        self._dsn = dsn
        self._thread_id = thread_id
        self._thread = str(thread_id)
        self._codec = CheckpointCodec(types)
        self._failures: list[str] = []

    @property
    def failures(self) -> tuple[str, ...]:
        """The class name of every save that was refused or that failed."""
        return tuple(self._failures)

    # ── the framework's six ─────────────────────────────────────────────────
    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        refusal: str | None = None
        try:
            document = self._codec.to_restorable_document(checkpoint)
            when = _aware(checkpoint.timestamp)
        except Exception as error:  # the class name is all that is kept
            refusal = _refusal_text(error)
            self._failures.append(type(error).__name__)
        if refusal is not None:
            raise WorkflowCheckpointException(f"cannot save a checkpoint: {refusal}")
        outcome = await self._run(
            f"INSERT INTO {TABLE} (thread_id, checkpoint_id, workflow_name, "  # noqa: S608
            "checkpointed_at, body) VALUES (%s, %s, %s, %s, %s)",
            (
                self._thread,
                checkpoint.checkpoint_id,
                checkpoint.workflow_name,
                when,
                Jsonb(document),
            ),
        )
        if outcome.failure is not None:
            self._failures.append(outcome.failure.name)
            raise WorkflowCheckpointException(
                outcome.failure.text("cannot save a checkpoint")
            )
        return checkpoint.checkpoint_id

    async def load(self, checkpoint_id: str) -> WorkflowCheckpoint:
        rows = await self._query(
            "cannot read a checkpoint",
            f"SELECT body FROM {TABLE} "  # noqa: S608
            "WHERE thread_id = %s AND checkpoint_id = %s",
            (self._thread, checkpoint_id),
        )
        if not rows:
            raise WorkflowCheckpointException(
                f"No checkpoint found with ID {checkpoint_id}"
            )
        return self._read(rows[0][0])

    async def list_checkpoints(self, *, workflow_name: str) -> list[WorkflowCheckpoint]:
        rows = await self._query(
            "cannot read the checkpoints",
            f"SELECT body FROM {TABLE} "  # noqa: S608
            "WHERE thread_id = %s AND workflow_name = %s ORDER BY seq",
            (self._thread, workflow_name),
        )
        return [self._read(body) for (body,) in rows]

    async def list_checkpoint_ids(self, *, workflow_name: str) -> list[str]:
        rows = await self._query(
            "cannot read the checkpoints",
            f"SELECT checkpoint_id FROM {TABLE} "  # noqa: S608
            "WHERE thread_id = %s AND workflow_name = %s ORDER BY seq",
            (self._thread, workflow_name),
        )
        return [checkpoint_id for (checkpoint_id,) in rows]

    async def get_latest(self, *, workflow_name: str) -> WorkflowCheckpoint | None:
        rows = await self._query(
            "cannot read the latest checkpoint",
            f"SELECT body FROM {TABLE} "  # noqa: S608
            "WHERE thread_id = %s AND workflow_name = %s "
            "ORDER BY seq DESC LIMIT 1",
            (self._thread, workflow_name),
        )
        return self._read(rows[0][0]) if rows else None

    async def delete(self, checkpoint_id: str) -> bool:
        deleted = await self._count(
            "cannot delete a checkpoint",
            f"DELETE FROM {TABLE} "  # noqa: S608
            "WHERE thread_id = %s AND checkpoint_id = %s",
            (self._thread, checkpoint_id),
        )
        return deleted == 1

    # ── for the host alone ──────────────────────────────────────────────────
    async def forget(self) -> int:
        """Delete every checkpoint of this thread; return how many went. The
        host calls it when the run ends: a finished run needs none.

        The delete carries the sweep's own guard (``sweep.GUARDED_DELETE``): it
        deletes nothing while a run of this thread is ``Running`` or
        ``AwaitingApproval``. So the caller records the run as ended BEFORE it
        forgets, or the rows stay (the sweep removes them later, as it does any
        leftover). That is also what keeps a leg that outlived its lease, whose
        run another leg has taken over, from deleting the rows of the leg that
        holds the run now."""
        return await self._count(
            "cannot delete the checkpoints",
            sql.SQL(GUARDED_DELETE).format(table=sql.Identifier(TABLE_NAME)),
            {
                "thread": self._thread,
                "run_thread": self._thread_id,
                "statuses": list(SWEPT_STATUSES),
            },
        )

    # ── the connection ──────────────────────────────────────────────────────
    def _read(self, document: Any) -> WorkflowCheckpoint:
        """The checkpoint a stored document holds. Only a refusal is a refusal:
        what the codec raises to refuse a document (``READ_REFUSALS``) is
        ``CheckpointUnreadable``, which ends the run; any other exception (a
        ``MemoryError``, an ``OSError``) is a fault that may pass, so it is the
        plain ``WorkflowCheckpointException`` and the run pauses again. Either
        way only the class name is kept, and nothing is attached to what is
        raised (it is raised outside the ``except``)."""
        unreadable: str | None = None
        failed: str | None = None
        try:
            return self._codec.from_document(document)
        except READ_REFUSALS as error:
            unreadable = _refusal_text(error)
        except Exception as error:  # the class name is all that is kept
            failed = type(error).__name__
        if unreadable is not None:
            raise CheckpointUnreadable(f"cannot read a checkpoint: {unreadable}")
        raise WorkflowCheckpointException(f"cannot read a checkpoint: {failed}")

    async def _query(
        self, what: str, statement: str, params: tuple[Any, ...]
    ) -> list[tuple[Any, ...]]:
        outcome = await self._run(statement, params, fetch=True)
        if outcome.failure is not None:
            raise WorkflowCheckpointException(outcome.failure.text(what))
        return outcome.rows

    async def _count(self, what: str, statement: Statement, params: Params) -> int:
        outcome = await self._run(statement, params)
        if outcome.failure is not None:
            raise WorkflowCheckpointException(outcome.failure.text(what))
        return outcome.count

    async def _run(
        self, statement: Statement, params: Params, *, fetch: bool = False
    ) -> _Outcome:
        """One statement on a connection of its own. A driver error becomes a
        ``_Failure`` that is returned, not raised, and the caller raises its own
        exception outside any ``except``: so no driver error is left attached to
        what leaves the store, as its cause or as its context."""
        try:
            async with await self._connect() as conn:
                cursor = await conn.execute(statement, params)
                rows = list(await cursor.fetchall()) if fetch else []
                return _Outcome(rows, cursor.rowcount, None)
        except psycopg.Error as error:
            return _Outcome([], 0, _Failure(type(error).__name__, error.sqlstate))

    async def _connect(self) -> psycopg.AsyncConnection[Any]:
        return await psycopg.AsyncConnection.connect(
            self._dsn,
            autocommit=True,
            application_name=SERVICE_NAME,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
        )


def _aware(timestamp: str) -> datetime:
    """The checkpoint's time. The framework writes an aware one; a naive one
    would be read in the session's time zone and could misorder two
    checkpoints, so it is refused."""
    when = datetime.fromisoformat(timestamp)
    if when.tzinfo is None:
        raise CodecRefusal(NAIVE_TIMESTAMP)
    return when


def _refusal_text(error: Exception) -> str:
    """The class name of ``error`` and, for the codec's own refusals, its fixed
    reason: never the text of any other error (it may hold what was refused)."""
    if isinstance(error, CodecRefusal):
        return f"{type(error).__name__} ({error.reason})"
    return type(error).__name__
