"""The synchronous part of a tool call: the checks, the handler, the audit row.

The SDK's low-level ``Server`` validates nothing: an unknown tool, a bad
argument and an extra property all reach the handler. This module checks them,
in an order where each step needs only what the steps before it established:

    tool, run, binding, tenant, allowlist, approval, arguments, the policy's
    scope, bound argument, idempotency key, the handler, the result, the audit
    row.

The policy's scope (its product and wording version) is read only for a tool
bound to the product, and after every check that decides whether the caller may
use the tool at all: the claims role has no grant on ``policy.policies``.

Every refusal answers one fixed reason word. No argument value, result value or
exception message reaches an audit row, a span, a log line or an error text
(T-03, T-25): a tool result and a claim enter a prompt, and a database error
quotes the value it refused. A failed audit write fails the call and undoes the
tool's write, because the row and the effect commit together (QA-05).
"""

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import psycopg
from jsonschema import Draft202012Validator

from meridian.platform.common.audit import (
    AuditEvent,
    AuditUnavailable,
    record_event,
    write_audit,
)
from meridian.platform.common.db import connect
from meridian.platform.common.env import SettingsError
from meridian.platform.common.throttle import RefusalAuditThrottle
from meridian.platform.registry import Registry
from meridian.platform.registry.models import Tool
from meridian.platform.toolserver.binding import (
    BindingRefused,
    RunBinding,
    read_binding,
    with_policy_scope,
)
from meridian.platform.toolserver.handlers import (
    Completed,
    Refused,
    ToolCall,
    ToolHandler,
)
from meridian.platform.toolserver.validation import build_validator, fits, storable
from meridian.platform.toolserver.wire import (
    IDEMPOTENCY_KEY_PATTERN,
    META_IDEMPOTENCY_KEY,
    META_RUN,
    RefusalReason,
)

logger = logging.getLogger(__name__)

# A database that cannot be reached or has dropped the connection; every other
# error is ours.
UNAVAILABLE_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError)

FailureReason = Literal["invalid-result", "database-unavailable", "unexpected"]
Outcome = Literal["completed", "replayed", "refused", "failed"]


@dataclass(frozen=True, slots=True)
class _Entry:
    tool: Tool
    handler: ToolHandler
    arguments: Draft202012Validator
    result: Draft202012Validator


@dataclass(slots=True)
class Call:
    """What is known about one call so far, for its audit row and its span."""

    call_id: uuid.UUID
    tool: str | None = None  # a registry ID, never the caller's text
    # What was read of the run's record, once the run was found: the audit row
    # and the span of a refusal after that name the run.
    run_id: uuid.UUID | None = None
    tenant: str | None = None
    agent: str | None = None
    reference: str | None = None

    def read(
        self,
        run_id: uuid.UUID,
        tenant: str | None,
        agent: str | None,
        reference: str | None,
    ) -> None:
        self.run_id, self.tenant, self.agent, self.reference = (
            run_id,
            tenant,
            agent,
            reference,
        )


@dataclass(frozen=True, slots=True)
class _Done:
    """A handler's answer that fits its schema, and its text."""

    completed: Completed
    text: str

    @property
    def outcome(self) -> Outcome:
        return "replayed" if self.completed.replayed else "completed"


@dataclass(frozen=True, slots=True)
class Finished:
    """How a call ended. ``done`` is set exactly when it completed or replayed."""

    call: Call
    outcome: Outcome
    reason: str | None = None
    done: _Done | None = None


class _CallFailed(Exception):
    def __init__(self, reason: FailureReason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Pipeline:
    """The synchronous part of a call; psycopg is synchronous, so the async
    handler runs it in a worker thread."""

    dsn: str = field(repr=False)
    registry: Registry
    service_name: str
    entries: Mapping[str, _Entry]
    throttle: RefusalAuditThrottle

    def run(
        self,
        call: Call,
        name: str,
        arguments: Mapping[str, Any],
        meta: Mapping[str, Any],
    ) -> Finished:
        """Never raises: a failure is an outcome, already audited when it could
        be."""
        try:
            answer = self._decide(call, name, arguments, meta)
            if isinstance(answer, Refused):
                self._audit_refusal(call, answer.reason)
                return Finished(call, "refused", answer.reason)
            return Finished(call, answer.outcome, None, answer)
        except Exception as exc:
            reason = _failure_reason(exc)
            _log_failure(exc)
            self.audit_failure(call, reason)
            return Finished(call, "failed", reason)

    def _decide(
        self,
        call: Call,
        name: str,
        arguments: Mapping[str, Any],
        meta: Mapping[str, Any],
    ) -> _Done | Refused:
        entry = self.entries.get(name)
        if entry is None:
            return Refused("unknown-tool")
        call.tool = name
        run_id = _run_id(meta)
        if run_id is None:
            return Refused("unknown-run")
        with connect(self.dsn, self.service_name) as conn:
            binding = read_binding(conn, run_id)
            if isinstance(binding, BindingRefused):
                call.read(
                    binding.run_id, binding.tenant, binding.agent, binding.reference
                )
                return Refused(binding.reason)
            call.read(binding.run_id, binding.tenant, binding.agent, binding.claim_id)
            if (reason := self._screen(entry, binding, arguments)) is not None:
                return Refused(reason)
            if entry.handler.bound_to == "product":
                scoped = with_policy_scope(conn, binding)
                if isinstance(scoped, BindingRefused):
                    return Refused(scoped.reason)
                binding = scoped
            if (reason := self._refusal(entry, binding, arguments, meta)) is not None:
                return Refused(reason)
            key = (
                _idempotency_key(meta) if entry.tool.idempotency_key_required else None
            )
            tool_call = ToolCall(binding, arguments, key, _payload_hash(arguments))
            return self._complete(conn, call, entry, entry.handler.run(conn, tool_call))

    def _screen(
        self, entry: _Entry, binding: RunBinding, arguments: Mapping[str, Any]
    ) -> RefusalReason | None:
        """The checks of whether this caller may use this tool with these
        arguments, before the bound argument is compared."""
        tool = entry.tool
        if not self.registry.tenant_may_run(binding.tenant, binding.agent):
            return "tenant-not-allowed"
        agent = self.registry.agent(binding.agent)
        if agent is None or tool.id not in agent.tools:
            return "tool-not-allowed"
        if tool.approval_required:  # approvals arrive in S015
            return "approval-required"
        # Storable second: it walks the arguments, which the schema has bounded.
        if not fits(entry.arguments, arguments) or not storable(arguments):
            return "invalid-arguments"
        return None

    def _refusal(
        self,
        entry: _Entry,
        binding: RunBinding,
        arguments: Mapping[str, Any],
        meta: Mapping[str, Any],
    ) -> RefusalReason | None:
        tool, handler = entry.tool, entry.handler
        if arguments[handler.bound_argument] != getattr(binding, handler.bound_to):
            return "outside-claim"
        if tool.idempotency_key_required and _idempotency_key(meta) is None:
            return "idempotency-key-missing"
        return None

    def _complete(
        self, conn: psycopg.Connection, call: Call, entry: _Entry, answer: object
    ) -> _Done | Refused:
        """Check the handler's answer, then commit its work with the audit row."""
        if isinstance(answer, Refused):
            conn.rollback()
            return answer
        if not isinstance(answer, Completed):
            raise TypeError("a handler answers Completed or Refused")
        if not fits(entry.result, answer.result):
            raise _CallFailed("invalid-result")
        text = json.dumps(answer.result, separators=(",", ":"), ensure_ascii=False)
        done = _Done(answer, text)
        record_event(conn, self._event(call, done.outcome))
        conn.commit()
        return done

    def _event(
        self,
        call: Call,
        outcome: str,
        reason: str | None = None,
        suppressed: int | None = None,
    ) -> AuditEvent:
        return AuditEvent(
            service=self.service_name,
            event="tool.call",
            outcome=outcome,
            tenant=call.tenant,
            agent=call.agent,
            run_id=call.run_id,
            reference=call.reference,
            reason=reason,
            call_id=call.call_id,
            suppressed=suppressed,
            tool=call.tool,
        )

    def _audit_refusal(self, call: Call, reason: str) -> None:
        """One row per tenant, tool and reason per window (T-49). The tenant
        is a key only when the registry knows it: the run row's value is
        caller-chosen, and a map keyed by it would not be bounded."""
        known = (
            call.tenant if call.tenant and self.registry.tenant(call.tenant) else None
        )
        key = f"{call.tool or '-'}/{reason}"
        carried = self.throttle.due(known, key)
        if carried is None:
            return
        try:
            write_audit(self.dsn, self._event(call, "refused", reason, carried))
        except BaseException:
            self.throttle.release(known, key, carried)
            raise

    def audit_failure(self, call: Call, reason: str) -> None:
        """Write the row of a failed call; a failure to write it is logged, as
        the call has failed already."""
        try:
            write_audit(self.dsn, self._event(call, "failed", reason))
        except Exception as exc:
            logger.error(
                "the audit row of a failed call was not written: %s", _name(exc)
            )


def _name(exc: BaseException) -> str:
    cause = exc.__cause__
    if isinstance(exc, AuditUnavailable) and cause is not None:
        return f"{type(exc).__name__} caused by {type(cause).__name__}"
    return type(exc).__name__


def _failure_reason(exc: Exception) -> FailureReason:
    if isinstance(exc, _CallFailed):
        return exc.reason
    cause = exc.__cause__ if isinstance(exc, AuditUnavailable) else exc
    return (
        "database-unavailable"
        if isinstance(cause, UNAVAILABLE_ERRORS)
        else "unexpected"
    )


def _log_failure(exc: Exception) -> None:
    """The class name and the SQLSTATE; the message can quote a value."""
    cause = exc.__cause__ if isinstance(exc, AuditUnavailable) else exc
    sqlstate = cause.sqlstate if isinstance(cause, psycopg.Error) else None
    logger.error("tool call failed: %s (sqlstate %s)", _name(exc), sqlstate or "none")


def _run_id(meta: Mapping[str, Any]) -> uuid.UUID | None:
    value = meta.get(META_RUN)
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _idempotency_key(meta: Mapping[str, Any]) -> str | None:
    value = meta.get(META_IDEMPOTENCY_KEY)
    if isinstance(value, str) and re.fullmatch(IDEMPOTENCY_KEY_PATTERN, value):
        return value
    return None


def _payload_hash(arguments: Mapping[str, Any]) -> str:
    text = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_entries(
    registry: Registry, server_id: str, handlers: Sequence[ToolHandler]
) -> dict[str, _Entry]:
    """One entry per registry tool of the server, or ``SettingsError`` naming
    the rule that a start broke."""
    if not registry.has_server(server_id):
        raise SettingsError(f"tool server {server_id!r} is not in the registry")
    tools = {tool.id: tool for tool in registry.tools if tool.server == server_id}
    by_tool: dict[str, ToolHandler] = {}
    for handler in handlers:
        if handler.tool in by_tool:
            raise SettingsError(f"tool {handler.tool!r} has more than one handler")
        by_tool[handler.tool] = handler
    missing, extra = (
        sorted(set(tools) - set(by_tool)),
        sorted(set(by_tool) - set(tools)),
    )
    if missing or extra:
        raise SettingsError(
            f"the handlers must be the registry's tools of {server_id!r}: "
            f"no handler for {missing}, not a tool of this server: {extra}"
        )
    entries: dict[str, _Entry] = {}
    for tool_id, tool in tools.items():
        handler = by_tool[tool_id]
        if handler.scope != tool.scope:
            raise SettingsError(
                f"tool {tool_id!r}: the handler's scope differs from the registry's"
            )
        if tool.output_schema is None:
            raise SettingsError(f"tool {tool_id!r} has no output schema")
        if handler.bound_argument not in tool.input_schema.get("required", []):
            raise SettingsError(
                f"tool {tool_id!r}: the bound argument {handler.bound_argument!r} "
                "is not a required property of its input schema"
            )
        entries[tool_id] = _Entry(
            tool,
            handler,
            build_validator(tool.input_schema, f"the input schema of {tool_id!r}"),
            build_validator(tool.output_schema, f"the output schema of {tool_id!r}"),
        )
    return entries
