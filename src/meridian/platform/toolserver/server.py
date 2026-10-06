"""The tool-server kit: one MCP server over the registry's tools (S013).

This module is the MCP app: the SDK server, its two handlers, the trace span of
a call and the ASGI wiring. The checks, the handler call and the audit row of a
call are in ``pipeline.py``.
"""

import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from importlib.metadata import version
from typing import Any

import anyio.to_thread
import mcp_types as types
from mcp.server._otel import OpenTelemetryMiddleware
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from opentelemetry import context, propagate
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import get_current_span
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from meridian.platform.common.audit import AuditEvent, write_audit
from meridian.platform.common.certlife import expiry_check, load_certificate
from meridian.platform.common.env import SettingsError
from meridian.platform.common.http import (
    HEALTH_PATH,
    SMALL_BODY_LIMIT_BYTES,
    CertificateExpiring,
)
from meridian.platform.common.identity import (
    audited_refusals,
    caller_policy,
    install_caller_check,
)
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.sdklog import quiet_sdk_logging
from meridian.platform.common.telemetry import (
    configure_propagation,
    make_tracer_provider,
    set_span_attributes,
    start_span,
)
from meridian.platform.common.throttle import RefusalAuditThrottle
from meridian.platform.registry import Registry, load_registry
from meridian.platform.toolserver.contracts import tool_listing
from meridian.platform.toolserver.handlers import TIMED_OUT, Deadline, ToolHandler
from meridian.platform.toolserver.meters import ToolServerMeters
from meridian.platform.toolserver.pipeline import (
    Call,
    Finished,
    Pipeline,
    build_entries,
)
from meridian.platform.toolserver.postonly import PostOnlyMiddleware
from meridian.platform.toolserver.settings import ToolServerSettings
from meridian.platform.toolserver.wire import (
    MCP_PATH,
    META_CALL_ID,
    META_REFUSAL,
    call_budget_seconds,
    run_id_of,
)

logger = logging.getLogger(__name__)

TRACER_NAME = "meridian.tool-server"
SPAN_NAME = "tool.call"
TOOL_UNAVAILABLE = "tool unavailable"
TRACE_KEYS = ("traceparent", "tracestate")
# Handlers running at once, per app: the slots of the app's limiter, one held
# from before a call's thread starts until it has returned. psycopg is
# synchronous and every call opens a connection, so the slots are the bound on
# connections; the thread pool is not (its own limit is the library's, and the
# row of a shed call below runs on it without a slot).
MAX_CONCURRENT_CALLS = 8
# A call that waited for a slot as long as its caller would ends as TIMED_OUT
# (the handlers' word). It is on the call's span and its log line, and its
# audit row goes through the throttle of refusals: an overloaded server writes
# one such row per tool and window, with the count it left out.


@dataclass(frozen=True, slots=True)
class ToolApp:
    app: Starlette
    server: Server


def _caller_context(meta: Mapping[str, Any]) -> context.Context | None:
    """The caller's trace context from ``_meta``, unless a span is already
    current (an instrumented server in front of this one made it)."""
    if get_current_span().get_span_context().is_valid:
        return None
    carrier = {k: meta[k] for k in TRACE_KEYS if isinstance(meta.get(k), str)}
    extracted = propagate.extract(carrier)
    if get_current_span(extracted).get_span_context().is_valid:
        return extracted
    return None


def _listed_tools(registry: Registry, server_id: str) -> list[types.Tool]:
    """What ``tools/list`` answers, built once: a listing the SDK would reject
    fails the start and not the first client."""
    try:
        return [
            types.Tool.model_validate(item)
            for item in tool_listing(registry, server_id)
        ]
    except ValidationError:  # its text quotes the item
        raise SettingsError(
            f"tool server {server_id!r}: the tool listing is not one the SDK accepts"
        ) from None


def _answer(finished: Finished) -> types.CallToolResult:
    meta: dict[str, Any] = {META_CALL_ID: str(finished.call.call_id)}
    if finished.outcome == "refused":
        meta[META_REFUSAL] = finished.reason
        return types.CallToolResult(
            is_error=True,
            content=[
                types.TextContent(type="text", text=f"refused: {finished.reason}")
            ],
            meta=meta,
        )
    if finished.done is None:  # a failure is no answer: the caller sees an error
        raise MCPError(types.INTERNAL_ERROR, TOOL_UNAVAILABLE)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=finished.done.text)],
        structured_content=finished.done.completed.result,
        meta=meta,
    )


def _span_attributes(finished: Finished) -> dict[str, str]:
    call = finished.call
    attributes = {
        "meridian.call_id": str(call.call_id),
        "meridian.tool_outcome": finished.outcome,
    }
    named = {
        "meridian.tool": call.tool,
        "meridian.reason": finished.reason,
        "meridian.run_id": call.run_id,
        "meridian.tenant": call.tenant,
        "meridian.agent": call.agent,
        # One of the agent's workers: an ID, set only once the name was found
        # among them, so a made-up name never reaches a span (S031).
        "meridian.worker": call.worker,
    }
    attributes |= {key: str(value) for key, value in named.items() if value}
    return attributes


@dataclass(frozen=True, slots=True)
class _CallRunner:
    """Gets a call one of the ``MAX_CONCURRENT_CALLS`` slots of ``limiter``,
    bounded by the call's deadline, and runs ``pipeline`` for it in a thread."""

    limiter: anyio.CapacityLimiter
    pipeline: Pipeline

    async def run(
        self,
        call: Call,
        params: types.CallToolRequestParams,
        meta: Mapping[str, Any],
        deadline: Deadline,
    ) -> Finished:
        # Getting a slot (bounded by the caller's budget) and running in a
        # thread (which no cancel interrupts) are two steps: the thread pool
        # takes no more than the slots held here, and a slot is held until the
        # thread has returned.
        if not await self._slot(call, deadline):
            return await self._shed(call, params.name, meta)
        work = partial(
            self.pipeline.run,
            call,
            params.name,
            params.arguments or {},
            meta,
            deadline,
        )
        try:
            return await anyio.to_thread.run_sync(work)
        except Exception as exc:  # the pipeline itself never raises
            logger.error("tool call failed: %s", type(exc).__name__)
            # In this thread: handing the write to a worker is what failed.
            self.pipeline.audit_failure(call, "unexpected")
            return Finished(call, "failed", "unexpected")
        finally:
            self.limiter.release_on_behalf_of(call.call_id)

    async def _slot(self, call: Call, deadline: Deadline) -> bool:
        """Take one of the ``MAX_CONCURRENT_CALLS`` slots for ``call``, waiting
        no later than ``deadline``; whether it got one. A free slot is taken
        without waiting, so a call is never shed while one is free. The caller
        releases it, once."""
        try:
            self.limiter.acquire_on_behalf_of_nowait(call.call_id)
            return True
        except anyio.WouldBlock:
            pass
        with anyio.move_on_after(deadline.remaining()) as waiting:
            await self.limiter.acquire_on_behalf_of(call.call_id)
        # A call whose wait was cut short holds no slot: the limiter takes a
        # cancelled waiter out of its queue, and gives back a slot it had handed
        # over as the cancel came.
        return not waiting.cancelled_caught

    async def _shed(self, call: Call, name: str, meta: Mapping[str, Any]) -> Finished:
        """A call that got no slot in time: ended without running, and audited
        through the throttle (the row has the tool when it is ours, and no run:
        the ID in ``_meta`` is not verified here)."""
        call.tool = name if name in self.pipeline.entries else None
        # The run's ID only when it is one, the tool only when it is ours: the
        # rest of the request is the caller's text.
        logger.warning(
            "tool call shed, no free slot within its budget (run %s, tool %s)",
            run_id_of(meta) or "-",
            call.tool or "-",
        )
        # The throttle decides here, on the loop, so a flood does not queue a
        # thread for each shed call; only a row that is due is written, in a
        # thread of the default pool (no slot: it is no handler, and the slots
        # are all held, which is why the call is shed).
        if (write := self.pipeline.timed_out_row(call)) is not None:
            await anyio.to_thread.run_sync(write)
        return Finished(call, "failed", TIMED_OUT)


def create_tool_app(
    settings: ToolServerSettings,
    *,
    server_id: str,
    service_name: str,
    handlers: Sequence[ToolHandler],
    tracer_provider: TracerProvider | None = None,
    meter_provider: MeterProvider | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_close: Callable[[], None] | None = None,
) -> ToolApp:
    """The ASGI app and the SDK server of one registry server.

    ``on_close`` is called once when the app's lifespan ends, whether or not
    the tracer provider is the caller's (the server's own clients close there).
    A tracer or meter provider this function made is shut down there; one the
    caller gave is the caller's.

    Raises ``SettingsError`` when the handlers and the registry disagree, or a
    schema or the listing is one the server could not serve.
    """
    quiet_sdk_logging()
    registry = load_registry(settings.registry_dir)
    policy = caller_policy(settings.identity_prefix, server_id, registry)
    entries = build_entries(registry, server_id, handlers)
    listed = _listed_tools(registry, server_id)
    # One limiter per app: every call, whatever its run ID, costs a database
    # connection in a worker thread. Created here, it binds to the first event
    # loop that uses it.
    limiter = anyio.CapacityLimiter(MAX_CONCURRENT_CALLS)
    throttle = RefusalAuditThrottle(clock)
    pipeline = Pipeline(
        dsn=settings.database_url,
        registry=registry,
        service_name=service_name,
        entries=entries,
        throttle=throttle,
    )
    runner = _CallRunner(limiter, pipeline)
    configure_propagation()
    provider = tracer_provider or make_tracer_provider(service_name)
    tracer = provider.get_tracer(TRACER_NAME)
    meters_provider = meter_provider or make_meter_provider(service_name)
    meters = ToolServerMeters(meters_provider, registry, entries)

    async def on_list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=list(listed))

    async def on_call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        meta: Mapping[str, Any] = ctx.meta or {}
        # On arrival, before anything waits: the caller's budget counts from
        # here, bounded by this server's own maximum.
        deadline = Deadline(clock() + call_budget_seconds(meta), clock)
        call = Call(uuid.uuid4())
        parent = _caller_context(meta)
        token = context.attach(parent) if parent is not None else None
        try:
            with start_span(tracer, SPAN_NAME) as span:
                finished = await runner.run(call, params, meta, deadline)
                set_span_attributes(span, _span_attributes(finished))
                meters.call_ended(finished)
                return _answer(finished)
        finally:
            if token is not None:
                context.detach(token)

    server: Server = Server(
        service_name,
        version=version("meridian"),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    # The SDK's own span is named after the raw tool name, which is the
    # caller's text and is not checked yet; the kit's span is the only one.
    server.middleware[:] = [
        middleware
        for middleware in server.middleware
        if not isinstance(middleware, OpenTelemetryMiddleware)
    ]

    # Loaded here: the file on disk changes at renewal, this process's
    # certificate does not. Inside its margin the check reads the file again
    # and says so only when a newer certificate is there, so the restart loads
    # it, or when this one has ended (S056).
    near_end = expiry_check(load_certificate(os.environ))

    async def healthz(request: Request) -> JSONResponse:
        if near_end():
            return JSONResponse(
                CertificateExpiring(status="certificate-expiring").model_dump(),
                status_code=503,
            )
        return JSONResponse({"status": "ok"})

    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=SMALL_BODY_LIMIT_BYTES,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=[],
        ),
        custom_starlette_routes=[Route(HEALTH_PATH, healthz, methods=["GET"])],
    )
    # The SDK would open an event stream on a GET that never ends.
    app.add_middleware(PostOnlyMiddleware, path=MCP_PATH)
    # Added last, so outside every other middleware: a call from no service
    # the registry maps is refused before the SDK reads a byte (S055). The row
    # says who in ``reference``, which a tool call's rows use for the claim.
    install_caller_check(
        app,
        policy,
        audited_refusals(
            throttle,
            lambda reason, who, carried: write_audit(
                settings.database_url,
                AuditEvent(
                    service=service_name,
                    event="tool.call",
                    outcome="refused",
                    reason=reason,
                    reference=who,
                    suppressed=carried,
                ),
            ),
        ),
    )
    sessions = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(inner: Starlette) -> AsyncIterator[None]:
        try:
            async with sessions(inner):
                yield
        finally:
            try:
                try:
                    if tracer_provider is None:  # one this function made is its own
                        provider.shutdown()
                finally:
                    if meter_provider is None:
                        meters_provider.shutdown()
            finally:
                if on_close is not None:
                    on_close()

    app.router.lifespan_context = lifespan
    return ToolApp(app=app, server=server)
