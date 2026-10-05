"""The runtime's client of the MCP tool servers (S013).

A graph reaches a tool only through the ``ToolClient`` it is given, as it
reaches a model only through the ``ModelClient``. The client holds the rules
that belong to the caller, and the server checks the same rules again:

* a tool must be in the registry and in the agent's allowlist, or nothing is
  sent and the runtime audits the refusal (hard rule 6);
* the key that makes a write happen once is derived here from the run, the tool
  and a label the graph's code gives the call site, so a model can neither mint
  nor reuse one (T-23);
* the call carries the run ID, the trace context and the time the runtime will
  still wait, never tenant, agent or claim, which the server reads from the
  runtime's own run row (T-22);
* the answer must fit the registry's output schema before the graph sees it:
  a tool result enters a prompt (TB-7).

No exception, log line or span holds an argument, a result or the text of an
SDK exception: the arguments and results are claim data (T-03, T-25). A
failure is logged by class name only. The SDK's own log records, which can
quote a message or a response body, are routed to a content-free handler by
``prepare_sdk`` (``quiet_sdk_logging``); a ``ToolClient`` and the runtime's
``create_app`` call it, and nothing is set up as a side effect of importing
this module.

The runtime is synchronous (a graph runs in a worker thread), and the SDK's
client is asynchronous. A call to an address goes through the runtime's
``ToolTransport`` when the client has one: an event loop and an HTTP client for
each server that outlive the call, so calls share a connection. Without one
(a command with no application, and a target that is not an address, such as a
test's in-process server) each call drives the SDK with ``anyio.run``.
"""

import hashlib
import logging
import re
import ssl
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, get_args

import anyio
import httpx2
import mcp_types as types
from jsonschema import Draft202012Validator
from mcp.client import Client, Transport
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.mcpserver import MCPServer
from mcp_types.version import LATEST_MODERN_VERSION
from opentelemetry import propagate
from opentelemetry.trace import Span, Tracer
from pydantic import BaseModel

from meridian.platform.common.sdklog import quiet_sdk_logging
from meridian.platform.common.telemetry import set_span_attributes, start_span
from meridian.platform.registry import Registry
from meridian.platform.registry.models import Tool
from meridian.platform.toolserver.validation import build_validator, fits
from meridian.platform.toolserver.wire import (
    MCP_PATH,
    META_CALL_ID,
    META_IDEMPOTENCY_KEY,
    META_REFUSAL,
    META_RUN,
    META_TIMEOUT_MS,
    MILLISECONDS_PER_SECOND,
    RefusalReason,
)

if TYPE_CHECKING:
    # ``tool_transport`` imports this module for the bound and ``send_call``.
    from meridian.runtime.tool_transport import ToolTransport

logger = logging.getLogger(__name__)

# What the SDK's ``Client`` accepts, as a target: an address (``/mcp`` is
# appended) or, in tests, a server or a transport.
type ToolTarget = str | Server[Any] | MCPServer | Transport | StdioServerParameters

# The whole call: connecting, the request and the answer.
TOOL_TIMEOUT_SECONDS = 10.0
SPAN_NAME = "runtime.tool"
# The graph's own name for a call site: it takes part in the idempotency key.
STEP_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
REFUSAL_REASONS: frozenset[str] = frozenset(get_args(RefusalReason))
UNKNOWN_REASON = "unknown"
# No probe and no fallback to the older handshake: the tool servers speak the
# newest per-request protocol (a probe showed the older one working too, and
# ``"auto"`` would try both).
CONNECT_MODE = LATEST_MODERN_VERSION


_sdk_lock = threading.Lock()


def unfinished_sdk_models() -> list[str]:
    """The names of the SDK's pydantic models that are not built yet."""
    return sorted(
        name
        for name, model in list(vars(types).items())
        if isinstance(model, type)
        and issubclass(model, BaseModel)
        and not model.__pydantic_complete__
    )


def prepare_sdk() -> None:
    """Set the MCP SDK up for use; safe to call again and from any thread.

    It routes the SDK's log records to a content-free handler, and finishes the
    SDK's pydantic models. ``mcp_types`` rebuilds only the ``*Params`` models;
    ``CallToolRequest`` and four more are left to be built on first use. Two
    threads making their first call together both start the rebuild, and one
    fails with an ``AttributeError`` from pydantic. The runtime runs concurrent
    runs in worker threads, so the first calls of a fresh process would fail.
    Building every unfinished model first, under a lock, leaves nothing to
    race over.
    """
    with _sdk_lock:
        quiet_sdk_logging()
        for name in unfinished_sdk_models():
            getattr(types, name).model_rebuild()


class ToolError(Exception):
    """A tool call did not give the graph a result.

    Holds the tool's registry ID, or ``None`` when the name was not a registry
    tool, and never an argument or a result.
    """

    def __init__(self, tool: str | None, message: str) -> None:
        super().__init__(message)
        self.tool = tool


class ToolNotAllowed(ToolError):
    """The runtime's own allowlist refused the call; nothing was sent."""

    def __init__(self, tool: str | None) -> None:
        super().__init__(tool, "tool not allowed")


class ToolCallLimit(ToolError):
    """The run has made its allowed number of tool calls; nothing was sent."""

    def __init__(self, tool: str | None) -> None:
        super().__init__(tool, "tool call limit of the run reached")


class ToolRefused(ToolError):
    """The server refused the call; ``reason`` is one of ``RefusalReason``, else
    ``"unknown"``."""

    def __init__(self, tool: str | None, reason: str) -> None:
        super().__init__(tool, f"tool refused: {reason}")
        self.reason = reason


class ToolUnavailable(ToolError):
    """No address, a transport or protocol failure, a timeout, or an answer
    outside the contract."""

    def __init__(self, tool: str | None) -> None:
        super().__init__(tool, "tool unavailable")


@dataclass(frozen=True, slots=True)
class ToolResult:
    data: Mapping[str, Any]
    replayed: bool
    call_id: str | None


def _name(error: BaseException) -> str:
    """The class name of an exception; for a group, those of its members. The
    text of an exception can quote an argument."""
    if isinstance(error, BaseExceptionGroup):
        inner = sorted({_name(member) for member in error.exceptions})
        return f"{type(error).__name__}({', '.join(inner)})"
    return type(error).__name__


def _call_id(answer: types.CallToolResult) -> str | None:
    """The server's call ID when the answer carries one that is a UUID."""
    value = (answer.meta or {}).get(META_CALL_ID)
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


@asynccontextmanager
async def _open(
    target: ToolTarget, verify: ssl.SSLContext | bool
) -> AsyncIterator[Client]:
    """An SDK client on ``target``: an address, or (tests) a server or a
    transport the SDK accepts. ``verify`` is for an address: the context that
    presents the runtime's certificate and trusts the server's CA, or the
    default verification (never off)."""
    if not isinstance(target, str):
        async with Client(target, mode=CONNECT_MODE, cache=None) as client:
            yield client
        return
    # trust_env=False: a proxy variable must not reroute claimant data. A client
    # passed to the SDK's transport is not closed by it, so it is closed here.
    async with httpx2.AsyncClient(
        trust_env=False, timeout=TOOL_TIMEOUT_SECONDS, verify=verify
    ) as http:
        transport = streamable_http_client(
            target.rstrip("/") + MCP_PATH, http_client=http
        )
        async with Client(transport, mode=CONNECT_MODE, cache=None) as client:
            yield client


async def send_call(
    client: Client, tool: str, arguments: dict[str, Any], meta: dict[str, Any]
) -> types.CallToolResult:
    """The call on an open SDK client; the part of an exchange that a client
    made for the call and one over a ``ToolTransport``'s kept HTTP client share."""
    # The time left of the bound the caller put around this call, so the server
    # does not queue the call for longer than the runtime will wait (T-62). Read
    # here, as late as the code allows, so connecting has already been counted. A
    # new dict: ``meta`` is the caller's.
    left = min(
        anyio.current_effective_deadline() - anyio.current_time(),
        TOOL_TIMEOUT_SECONDS,
    )
    sent = {**meta, META_TIMEOUT_MS: max(1, int(left * MILLISECONDS_PER_SECOND))}
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(name=tool, arguments=arguments, _meta=sent)
    )
    # One request. ``Client.call_tool`` goes through the session's
    # ``call_tool``, which ends in ``validate_tool_result``: with no output
    # schema cached for the tool (and the SDK client lives for one call) that
    # sends ``tools/list`` after the call, inside the same bound and after a
    # write has committed. The runtime checks the result against the registry's
    # own schema, so that request buys nothing. ``send_request`` is the
    # session's public method for a request without that step (mcp 2.2.0). It
    # also skips the ``input_required`` loop, which our servers never use: such
    # an answer does not parse as a ``CallToolResult`` and so is
    # ``ToolUnavailable``. The HTTP test pins that a call is one request.
    return await client.session.send_request(request, types.CallToolResult)


async def _exchange(
    target: ToolTarget,
    tool: str,
    arguments: dict[str, Any],
    meta: dict[str, Any],
    verify: ssl.SSLContext | bool,
) -> types.CallToolResult:
    # Read at call time, so the bound is the constant's current value.
    with anyio.fail_after(TOOL_TIMEOUT_SECONDS):
        async with _open(target, verify) as client:
            return await send_call(client, tool, arguments, meta)


class ToolClient:
    """Built per run. ``servers`` maps a registry server ID to what the SDK's
    ``Client`` accepts: a base URL (``/mcp`` is appended) or, in tests, a
    connected in-process server. ``on_refusal`` is called with the tool's
    registry ID, or ``None``, before a call the allowlist refuses; it writes the
    runtime's audit row, and an exception from it propagates. ``max_calls``
    bounds the calls of this client, so of one run: every call counts, a refused
    one too, and the one past it raises ``ToolCallLimit`` before the allowlist.
    ``verify`` is how a call to an address is made over TLS (S055): the context
    of the runtime's own certificate and CA, or the default verification, never
    off. ``transport`` (S059) is the runtime's kept HTTP client for each
    address, shared by its runs: a call to an address goes through it, and
    without one each call opens its own."""

    def __init__(
        self,
        servers: Mapping[str, ToolTarget],
        *,
        registry: Registry,
        agent: str,
        run_id: uuid.UUID,
        tracer: Tracer,
        on_refusal: Callable[[str | None], None],
        max_calls: int,
        verify: ssl.SSLContext | bool = True,
        transport: "ToolTransport | None" = None,
    ) -> None:
        prepare_sdk()
        self._verify = verify
        self._transport = transport
        self._max_calls = max_calls
        self._calls = 0
        self._lock = threading.Lock()
        self._servers = dict(servers)
        self._registry = registry
        self._agent = agent
        self._run_id = run_id
        self._tracer = tracer
        self._on_refusal = on_refusal
        self._validators: dict[str, Draft202012Validator] = {}

    def call(
        self, tool: str, arguments: Mapping[str, Any], *, step: str | None = None
    ) -> ToolResult:
        """Call ``tool`` and return its result.

        ``step`` is the graph's own name for the call site (never from a model);
        a tool that needs an idempotency key requires one, any other tool
        refuses one. Raises ``ValueError`` for a ``step`` that does not fit
        (a bug in the graph, before anything is sent), ``ToolCallLimit``,
        ``ToolNotAllowed``, ``ToolRefused`` or ``ToolUnavailable``.
        """
        self._count(tool)
        spec = self._allowed(tool)
        key = self._idempotency_key(spec, step)
        target = self._servers.get(spec.server)
        if target is None:
            # Not at start: a server of the registry may have no address
            # configured (every server publishes a contract, but only a
            # configured one is reachable), and its tools are unavailable.
            logger.warning(
                "tool server %s has no address, so %s is unavailable",
                spec.server,
                spec.id,
            )
            raise ToolUnavailable(spec.id)
        # A tool whose result cannot be checked is not called at all.
        self._validator(spec)
        with start_span(self._tracer, SPAN_NAME) as span:
            set_span_attributes(
                span, {"meridian.tool": spec.id, "meridian.tool_server": spec.server}
            )
            try:
                outcome = self._attempt(span, spec, target, arguments, key)
            except ToolUnavailable:
                set_span_attributes(span, {"meridian.tool_outcome": "unavailable"})
                raise
            if isinstance(outcome, ToolRefused):
                set_span_attributes(
                    span,
                    {
                        "meridian.tool_outcome": "refused",
                        "meridian.reason": outcome.reason,
                    },
                )
            else:
                label = "replayed" if outcome.replayed else "completed"
                set_span_attributes(span, {"meridian.tool_outcome": label})
        # A refusal is an answer, not a failure: it leaves the span's status unset.
        if isinstance(outcome, ToolRefused):
            raise outcome
        return outcome

    def _count(self, tool: str) -> None:
        with self._lock:  # a graph's parallel nodes share this client
            if self._calls >= self._max_calls:
                # The ID of a registry tool only: a made-up name is never kept.
                spec = self._registry.tool(tool)
                raise ToolCallLimit(spec.id if spec is not None else None)
            self._calls += 1

    def _allowed(self, tool: str) -> Tool:
        spec = self._registry.tool(tool)
        agent = self._registry.agent(self._agent)
        if spec is not None and agent is not None and spec.id in agent.tools:
            return spec
        # A name that is no registry tool is never stored: a model may have
        # made it up, and it could hold anything.
        known = spec.id if spec is not None else None
        self._on_refusal(known)
        raise ToolNotAllowed(known)

    def _idempotency_key(self, spec: Tool, step: str | None) -> str | None:
        if not spec.idempotency_key_required:
            if step is not None:
                raise ValueError("a tool without an idempotency key takes no step")
            return None
        if not isinstance(step, str) or STEP_PATTERN.fullmatch(step) is None:
            raise ValueError(
                "a write tool needs a step: 1 to 64 characters of a-z, 0-9, _, . "
                "and -, starting with a letter or digit"
            )
        return hashlib.sha256(f"{self._run_id}:{spec.id}:{step}".encode()).hexdigest()

    def _attempt(
        self,
        span: Span,
        spec: Tool,
        target: ToolTarget,
        arguments: Mapping[str, Any],
        key: str | None,
    ) -> ToolResult | ToolRefused:
        """The call and the reading of its answer; raises ``ToolUnavailable``."""
        meta: dict[str, Any] = {META_RUN: str(self._run_id)}
        if key is not None:
            meta[META_IDEMPOTENCY_KEY] = key
        # The SDK does not carry the trace; the server reads it from ``_meta``.
        propagate.inject(meta)
        try:
            if self._transport is not None and isinstance(target, str):
                answer = self._transport.exchange(
                    target, spec.id, dict(arguments), meta
                )
            else:
                answer = anyio.run(
                    _exchange, target, spec.id, dict(arguments), meta, self._verify
                )
        except Exception as error:
            # An exception group of ordinary exceptions is an ``Exception``; a
            # group holding a cancellation or ``SystemExit`` is not, and goes on.
            logger.error("tool call failed: %s", _name(error))
            raise ToolUnavailable(spec.id) from None
        call_id = _call_id(answer)
        if call_id is not None:
            set_span_attributes(span, {"meridian.call_id": call_id})
        return self._read(spec, answer, call_id)

    def _read(
        self, spec: Tool, answer: types.CallToolResult, call_id: str | None
    ) -> ToolResult | ToolRefused:
        if answer.is_error:
            # A tool server of ours always sets a string reason on a refusal. An
            # error answer without one is a server not behaving, so the call is
            # unavailable; a string this client does not list is a newer
            # server's reason, still a refusal, read as ``unknown``.
            reason = (answer.meta or {}).get(META_REFUSAL)
            if not isinstance(reason, str):
                raise ToolUnavailable(spec.id)
            return ToolRefused(
                spec.id, reason if reason in REFUSAL_REASONS else UNKNOWN_REASON
            )
        data = answer.structured_content
        if not isinstance(data, dict) or not fits(self._validator(spec), data):
            raise ToolUnavailable(spec.id)
        return ToolResult(data, data.get("replayed") is True, call_id)

    def _validator(self, spec: Tool) -> Draft202012Validator:
        validator = self._validators.get(spec.id)
        if validator is None:
            if spec.output_schema is None:
                # No schema, so no way to check what would enter a prompt.
                logger.warning(
                    "tool %s has no output schema, so it is not called", spec.id
                )
                raise ToolUnavailable(spec.id)
            validator = build_validator(spec.output_schema)
            self._validators[spec.id] = validator
        return validator
