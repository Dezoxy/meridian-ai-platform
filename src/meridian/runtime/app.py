"""The Agent Runtime app: ``POST /runs`` and ``GET /runs/{run_id}`` (ADR 2).

A run is synchronous at this step: the request answers when the graph ends or
pauses. The run row and its first audit event are written before any graph
runs; if they cannot be, nothing runs (503).
"""

import logging
import time
import uuid
from collections.abc import Callable, Mapping

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde import _msgpack
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer

import meridian.runtime
from meridian.platform.common.audit import AuditEvent, write_audit
from meridian.platform.common.env import SettingsError
from meridian.platform.common.http import (
    DATABASE_UNAVAILABLE,
    REFUSED,
    SMALL_BODY_LIMIT_BYTES,
    create_service_app,
    error_answer,
    error_responses,
)
from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.platform.common.throttle import RefusalAuditThrottle
from meridian.platform.registry import Registry, load_registry
from meridian.runtime import SERVICE_NAME, runs
from meridian.runtime.failures import failure_reason
from meridian.runtime.graphs import load_graph_factory
from meridian.runtime.model_client import ModelCallError, ModelCallTimeoutError
from meridian.runtime.models import (
    RunErrorBody,
    RunRequest,
    RunResponse,
    RunState,
    RunStatus,
)
from meridian.runtime.runs import RunIdentity, RunOutcome
from meridian.runtime.settings import RuntimeSettings
from meridian.runtime.tool_client import (
    ToolClient,
    ToolError,
    ToolRefused,
    ToolTarget,
    prepare_sdk,
)

GATEWAY_TIMEOUT_SECONDS = 30.0
FINISH_ATTEMPTS = 2
# The audit reason of a call the runtime's own allowlist refuses.
REFUSAL_REASON = "tool-not-allowed"
# The audit reason of a run request for a job agent, which has no graph.
JOB_REFUSAL_REASON = "not-a-graph-agent"
HTTP_BAD_GATEWAY = 502
HTTP_GATEWAY_TIMEOUT = 504

logger = logging.getLogger(__name__)


def strict_msgpack_enabled() -> bool:
    """Whether LangGraph really is in strict msgpack mode in this process (the
    variable alone proves nothing once langgraph has been imported)."""
    return bool(_msgpack.STRICT_MSGPACK_ENABLED)


def _refuse_to_start_if_unsafe() -> None:
    requested = meridian.runtime.LANGSMITH_REQUESTED_BY
    if requested:
        raise SettingsError(
            "LangSmith tracing is refused (its endpoint is outside the EU and "
            "would receive claim data); unset " + ", ".join(requested)
        )
    if not strict_msgpack_enabled():
        raise SettingsError(
            "LangGraph's strict msgpack mode is off; meridian.runtime must be "
            "imported before anything that imports langgraph"
        )


def _failure_status(error: Exception) -> int:
    return (
        HTTP_GATEWAY_TIMEOUT
        if isinstance(error, ModelCallTimeoutError)
        else HTTP_BAD_GATEWAY
    )


def _tool_of(error: Exception) -> str | None:
    """The registry ID of the tool a tool error is about, else none."""
    return error.tool if isinstance(error, ToolError) else None


def _log_failure(run_id: uuid.UUID, error: Exception) -> None:
    # The reason word and the class; for a gateway call its status code; for a
    # tool error its tool (a registry ID or none) and, for a refusal, the
    # refusal word. Nothing else: a message could hold claim text.
    status = error.status_code if isinstance(error, ModelCallError) else None
    refusal = error.reason if isinstance(error, ToolRefused) else None
    logger.error(
        "run %s failed: %s (%s; gateway status %s; tool %s; refusal %s)",
        run_id,
        failure_reason(error),
        type(error).__name__,
        status,
        _tool_of(error),
        refusal,
    )


def _finish(
    dsn: str,
    identity: RunIdentity,
    status: RunState,
    reason: str | None = None,
    tool: str | None = None,
) -> psycopg.Error | None:
    """Record the final status; try twice, return the last error if both fail."""
    last: psycopg.Error | None = None
    for _ in range(FINISH_ATTEMPTS):
        try:
            runs.finish_run(dsn, identity, status, reason=reason, tool=tool)
        except psycopg.Error as exc:
            last = exc
        else:
            return None
    return last


def tool_client_for(
    servers: Mapping[str, ToolTarget],
    *,
    registry: Registry,
    dsn: str,
    tracer: Tracer,
    identity: RunIdentity,
    throttle: RefusalAuditThrottle,
) -> ToolClient:
    """The run's tool client. A call its allowlist refuses is audited here,
    with the tool's registry ID or none, at most one row per tenant and tool per
    window (T-49; from S014 a model may choose the tool); a failed audit write
    propagates and fails the run (QA-05). ``ToolNotAllowed`` is raised either
    way. The tenant is a key because ``create_run`` has checked it against the
    registry, so the throttle's map is bounded."""

    def audit_refusal(tool: str | None) -> None:
        key = f"{tool or '-'}/{REFUSAL_REASON}"
        carried = throttle.due(identity.tenant, key)
        if carried is None:
            return
        try:
            write_audit(
                dsn,
                AuditEvent(
                    service=SERVICE_NAME,
                    event="tool.call",
                    outcome="refused",
                    reason=REFUSAL_REASON,
                    tool=tool,
                    tenant=identity.tenant,
                    agent=identity.agent,
                    run_id=identity.run_id,
                    reference=identity.reference,
                    suppressed=carried,
                ),
            )
        except BaseException:
            throttle.release(identity.tenant, key, carried)
            raise

    return ToolClient(
        servers,
        registry=registry,
        agent=identity.agent,
        run_id=identity.run_id,
        tracer=tracer,
        on_refusal=audit_refusal,
        max_calls=runs.MAX_TOOL_CALLS_PER_RUN,
    )


def create_app(
    settings: RuntimeSettings,
    *,
    tracer_provider: TracerProvider | None = None,
    http_client: httpx.Client | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    tool_servers: Mapping[str, ToolTarget] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build the app; raise when it must not start: a registry that fails to
    load, a graph that cannot be loaded, a tool server the registry does not
    have, LangSmith requested or LangGraph not in strict msgpack mode.

    ``tool_servers`` (tests) replaces the settings' addresses with targets the
    SDK's client accepts, such as an in-process server. ``clock`` times the
    audit throttle of the runtime's own tool refusals."""
    _refuse_to_start_if_unsafe()
    # At start, not on the first run: the SDK's log routing and its models.
    prepare_sdk()
    registry = load_registry(settings.registry_dir)
    servers: Mapping[str, ToolTarget] = (
        settings.tool_servers if tool_servers is None else tool_servers
    )
    refusal_throttle = RefusalAuditThrottle(clock)
    for server_id in servers:
        if not registry.has_server(server_id):
            raise SettingsError(f"tool server {server_id!r} is not in the registry")
    # Every graph agent's graph is resolved now, so a bad entry point stops the
    # start instead of failing a request; the handler uses this cache. A job
    # agent has no graph and so no entry here.
    factories = {
        agent.id: load_graph_factory(agent.id, registry)
        for agent in registry.agents
        if agent.kind == "graph"
    }
    # trust_env=False: a proxy variable must not reroute claimant data.
    http = http_client or httpx.Client(
        base_url=settings.gateway_url,
        timeout=GATEWAY_TIMEOUT_SECONDS,
        trust_env=False,
    )
    # Until S015 brings the PostgreSQL checkpointer, runs live in memory.
    saver = checkpointer or MemorySaver()
    dsn = settings.database_url
    service = create_service_app(
        title="Meridian Agent Runtime",
        description="Runs an agent's graph and keeps the run's status (ADR 2).",
        service_name=SERVICE_NAME,
        tracer_name="meridian.runtime",
        max_body_bytes=SMALL_BODY_LIMIT_BYTES,
        tracer_provider=tracer_provider,
        close=http.close if http_client is None else None,
    )
    app, tracer = service.app, service.tracer

    def refuse(body: RunRequest, reason: str | None = None) -> HTTPException:
        write_audit(
            dsn,
            AuditEvent(
                service=SERVICE_NAME,
                event="run.refused",
                outcome="refused",
                reason=reason,
                tenant=body.tenant,
                agent=body.agent,
                reference=body.reference,
            ),
        )
        return HTTPException(status_code=403, detail=REFUSED)

    @app.post(
        "/runs",
        tags=["runs"],
        summary="Start a run of an agent's graph and wait for it to end or pause.",
        response_model=RunResponse,
        # A failed run answers 502 (504 for a gateway timeout) with the usual
        # RunResponse, status "Failed"; a 503 names the run when it has one.
        responses=error_responses(403, 413, 500)
        | error_responses(502, 504, model=RunResponse)
        | error_responses(503, model=RunErrorBody),
    )
    def create_run(body: RunRequest, response: Response) -> RunResponse | JSONResponse:
        if not registry.tenant_may_run(body.tenant, body.agent):
            raise refuse(body)
        factory = factories.get(body.agent)
        if factory is None:
            # A job agent: a tenant may list it, but it has no graph to run.
            raise refuse(body, JOB_REFUSAL_REASON)
        identity = RunIdentity(
            run_id=uuid.uuid4(),
            thread_id=uuid.uuid4(),
            agent=body.agent,
            tenant=body.tenant,
            reference=body.reference,
        )
        with start_span(tracer, "runtime.run") as span:
            set_span_attributes(
                span,
                {
                    "meridian.run_id": str(identity.run_id),
                    "meridian.agent": identity.agent,
                    "meridian.tenant": identity.tenant,
                },
            )
            runs.start_run(dsn, identity)
            failure: Exception | None = None
            try:
                tools = tool_client_for(
                    servers,
                    registry=registry,
                    dsn=dsn,
                    tracer=tracer,
                    identity=identity,
                    throttle=refusal_throttle,
                )
                outcome = runs.execute(
                    factory,
                    saver,
                    http,
                    tools,
                    tracer,
                    identity,
                    body.input,
                )
            except Exception as exc:
                _log_failure(identity.run_id, exc)
                mark_error(span, exc)
                failure, outcome = exc, RunOutcome("Failed", None)
            if outcome.status != "AwaitingApproval":
                # The checkpoint holds the claim; a finished run needs none.
                saver.delete_thread(str(identity.thread_id))
            unsaved = _finish(
                dsn,
                identity,
                outcome.status,
                reason=None if failure is None else failure_reason(failure),
                tool=None if failure is None else _tool_of(failure),
            )
            set_span_attributes(span, {"meridian.run_status": outcome.status})
            if unsaved is not None:
                logger.error(
                    "run %s could not be marked %s: %s (sqlstate %s)",
                    identity.run_id,
                    outcome.status,
                    type(unsaved).__name__,
                    unsaved.sqlstate or "none",
                )
                mark_error(span, unsaved)
                return error_answer(
                    503, DATABASE_UNAVAILABLE, run_id=str(identity.run_id)
                )
        if failure is not None:
            response.status_code = _failure_status(failure)
        return RunResponse(
            run_id=identity.run_id, status=outcome.status, output=outcome.output
        )

    @app.get(
        "/runs/{run_id}",
        tags=["runs"],
        summary="Read the status of a run.",
        responses=error_responses(404, 500, 503),
    )
    def read_run(run_id: uuid.UUID) -> RunStatus:
        found = runs.fetch_run(dsn, run_id)
        if found is None:
            raise HTTPException(status_code=404, detail="no such run")
        return found

    return app


def create_app_from_env() -> FastAPI:
    """The factory S041 runs under ``uvicorn --factory``."""
    return create_app(RuntimeSettings.from_env())
