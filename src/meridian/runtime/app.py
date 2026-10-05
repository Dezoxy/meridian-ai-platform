"""The Agent Runtime app: ``POST /runs``, ``POST /runs/{run_id}/resume`` and
``GET /runs/{run_id}`` (ADR 2).

A run is synchronous at this step: the request answers when the graph ends or
pauses. The run row and its first audit event are written before any graph
runs; if they cannot be, nothing runs (503). A paused run is resumed by a
second request, which claims it once and runs the next leg.
"""

import logging
import ssl
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from typing import Any, Literal

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde import _msgpack
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span, Tracer

import meridian.runtime
from meridian.platform.common.audit import AuditEvent, write_audit
from meridian.platform.common.env import SettingsError
from meridian.platform.common.http import (
    DATABASE_UNAVAILABLE,
    REFUSED,
    SMALL_BODY_LIMIT_BYTES,
    BoundedEntityId,
    create_service_app,
    error_answer,
    error_responses,
)
from meridian.platform.common.identity import (
    NAME_REFUSAL_REASON,
    audited_refusals,
    caller_may_name,
    caller_policy,
    caller_service,
    install_caller_check,
)
from meridian.platform.common.logredaction import install_log_redaction
from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.platform.common.throttle import RefusalAuditThrottle
from meridian.platform.common.tls import verify_of
from meridian.platform.registry import Registry, load_registry
from meridian.runtime import SERVICE_NAME, runs
from meridian.runtime.checkpoints import open_saver
from meridian.runtime.failures import failure_reason
from meridian.runtime.graphs import GraphFactory, load_graph_factory
from meridian.runtime.model_client import ModelCallError, ModelCallTimeoutError
from meridian.runtime.models import (
    Reference,
    ResumeRequest,
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
from meridian.runtime.tool_transport import ToolTransport

GATEWAY_TIMEOUT_SECONDS = 30.0
FINISH_ATTEMPTS = 2
# The audit reason of a call the runtime's own allowlist refuses.
REFUSAL_REASON = "tool-not-allowed"
# The audit reason of a run request for a job agent, which has no graph.
JOB_REFUSAL_REASON = "not-a-graph-agent"
# The detail of a 404 for a run: the same text whether the run does not exist
# or belongs to another tenant or reference.
NO_SUCH_RUN = "no such run"
HTTP_BAD_GATEWAY = 502
HTTP_GATEWAY_TIMEOUT = 504
# The leg of a run a failure belongs to: its first, or one resumed after a pause.
Leg = Literal["first", "resumed"]

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


def _set_run_attributes(span: Span, run_id: uuid.UUID, agent: str, tenant: str) -> None:
    set_span_attributes(
        span,
        {
            "meridian.run_id": str(run_id),
            "meridian.agent": agent,
            "meridian.tenant": tenant,
        },
    )


def _log_failure(run_id: uuid.UUID, error: Exception, leg: Leg) -> None:
    # The leg, the reason word and the class; for a gateway call its status
    # code; for a tool error its tool (a registry ID or none) and, for a
    # refusal, the refusal word. Nothing else: a message could hold claim text.
    status = error.status_code if isinstance(error, ModelCallError) else None
    refusal = error.reason if isinstance(error, ToolRefused) else None
    logger.error(
        "run %s failed on its %s leg: %s (%s; gateway status %s; tool %s; refusal %s)",
        run_id,
        leg,
        failure_reason(error),
        type(error).__name__,
        status,
        _tool_of(error),
        refusal,
    )


def _record(write: Callable[[], None]) -> psycopg.Error | None:
    """Run a status write; try twice, return the last error if both fail."""
    last: psycopg.Error | None = None
    for _ in range(FINISH_ATTEMPTS):
        try:
            write()
        except psycopg.Error as exc:
            last = exc
        else:
            return None
    return last


def _finish(
    dsn: str,
    identity: RunIdentity,
    status: RunState,
    reason: str | None = None,
    tool: str | None = None,
) -> tuple[bool, psycopg.Error | None]:
    """Record the final status; try twice. Returns whether the run moved (the
    last write's answer: ``False`` when the run was no longer ``Running``) and
    the last error if both writes fail."""
    moved = False

    def write() -> None:
        nonlocal moved
        moved = runs.finish_run(dsn, identity, status, reason=reason, tool=tool)

    error = _record(write)
    return moved, error


def _pause_again(
    dsn: str, identity: RunIdentity, reason: str, tool: str | None
) -> tuple[bool, psycopg.Error | None]:
    """Record a failed resumed leg and the run's return to its pause; try
    twice. Returns whether the run moved (the last write's answer: ``False``
    when the run was no longer ``Running``) and the last error if both writes
    fail."""
    moved = False

    def write() -> None:
        nonlocal moved
        moved = runs.pause_after_failed_resume(dsn, identity, reason=reason, tool=tool)

    error = _record(write)
    return moved, error


def _record_end(
    dsn: str,
    identity: RunIdentity,
    leg: Leg,
    failure: Exception | None,
    outcome: RunOutcome,
) -> tuple[RunOutcome, bool, psycopg.Error | None]:
    """Write how a leg ended. Returns the outcome to answer with (a resumed leg
    that failed leaves its run paused again), whether the write moved the run,
    and the last error if it could not be written."""
    if failure is None:
        moved, unsaved = _finish(dsn, identity, outcome.status)
        return outcome, moved, unsaved
    if leg == "resumed" and failure_reason(failure) not in runs.NOTHING_TO_RESUME:
        moved, unsaved = _pause_again(
            dsn, identity, failure_reason(failure), _tool_of(failure)
        )
        return RunOutcome("AwaitingApproval", None), moved, unsaved
    moved, unsaved = _finish(
        dsn,
        identity,
        outcome.status,
        reason=failure_reason(failure),
        tool=_tool_of(failure),
    )
    return outcome, moved, unsaved


def _settle(
    dsn: str,
    identity: RunIdentity,
    leg: Leg,
    failure: Exception | None,
    outcome: RunOutcome,
) -> tuple[RunOutcome, Exception | None, psycopg.Error | None]:
    """Write how a leg ended and return what it answers: the outcome, the
    failure it answers (none when someone else ended the run, as the leg itself
    did not fail), and the last error if nothing could be written."""
    outcome, moved, unsaved = _record_end(dsn, identity, leg, failure, outcome)
    if unsaved is not None or moved:
        return outcome, failure, unsaved
    # Written, but over nothing: the leg's own earlier write had committed, or
    # the run was ended by someone else while it worked.
    outcome, ended, unsaved = _stored_outcome(dsn, identity, outcome, failure)
    return outcome, None if ended else failure, unsaved


def _unsaved_answer(
    span: Span, identity: RunIdentity, status: RunState, unsaved: psycopg.Error
) -> JSONResponse:
    """The 503 of a leg whose final status could not be written, with the run ID."""
    logger.error(
        "run %s could not be marked %s: %s (sqlstate %s)",
        identity.run_id,
        status,
        type(unsaved).__name__,
        unsaved.sqlstate or "none",
    )
    mark_error(span, unsaved)
    return error_answer(503, DATABASE_UNAVAILABLE, run_id=str(identity.run_id))


def _stored_outcome(
    dsn: str, identity: RunIdentity, own: RunOutcome, failure: Exception | None
) -> tuple[RunOutcome, bool, psycopg.Error | None]:
    """What a leg answers when its write moved nothing. Returns the outcome,
    whether someone else (the sweep) ended the run, and the error if the stored
    status cannot be read.

    A stored status equal to the leg's own means its own write is recorded (it
    committed and the connection dropped before the answer, so the retry found
    the run no longer ``Running``): the leg answers as if it had moved the run.
    Any other status is the answer, with no output. The warning has the run ID
    and the status, nothing of the claim.

    A leg that failed on a run already stored as ``Failed`` answers its own
    failure, but the trail may say only the sweep's reason (``abandoned``): one
    warning keeps the leg's reason word and tool, the run ID with them."""
    try:
        stored = runs.fetch_run(dsn, identity.run_id)
    except psycopg.Error as exc:
        return own, False, exc
    if stored is not None and stored.status == own.status:
        if failure is not None and own.status == "Failed":
            logger.warning(
                "run %s: its leg failed (%s; tool %s) on a run already Failed, "
                "so the trail may not hold this reason",
                identity.run_id,
                failure_reason(failure),
                _tool_of(failure),
            )
        return own, False, None
    logger.warning(
        "run %s was ended before its leg could mark it %s",
        identity.run_id,
        own.status,
    )
    status = own.status if stored is None else stored.status
    return RunOutcome(status, None), True, None


def _delete_checkpoints(
    saver: BaseCheckpointSaver,
    identity: RunIdentity,
    fresh: Callable[[], AbstractContextManager[BaseCheckpointSaver]],
) -> None:
    """Drop the run's thread; a failure is logged and changes no answer. A
    failed delete is tried once more on ``fresh()``, a saver of its own (the
    injected one, when there is one). The run is already recorded, and a message
    could hold claim text, so each log line has the run ID, the exception class
    and the sqlstate only."""
    thread = str(identity.thread_id)
    try:
        saver.delete_thread(thread)
        return
    except Exception as exc:  # whatever the saver raises changes no answer
        _log_delete_failure(identity, exc)
    try:
        with fresh() as retry:
            retry.delete_thread(thread)
    except Exception as exc:
        _log_delete_failure(identity, exc)


def _log_delete_failure(identity: RunIdentity, error: Exception) -> None:
    logger.error(
        "run %s: its checkpoints were not deleted: %s (sqlstate %s)",
        identity.run_id,
        type(error).__name__,
        (error.sqlstate if isinstance(error, psycopg.Error) else None) or "none",
    )


def make_gateway_client(
    settings: RuntimeSettings, verify: ssl.SSLContext | bool
) -> httpx.Client:
    """The client of the Model Gateway. ``verify`` is ``verify_of`` the settings'
    ``client_tls``: the context that presents the runtime's certificate and
    trusts the gateway's CA, or the default verification when there is none."""
    # trust_env=False: a proxy variable must not reroute claimant data.
    return httpx.Client(
        base_url=settings.gateway_url,
        timeout=GATEWAY_TIMEOUT_SECONDS,
        trust_env=False,
        verify=verify,
    )


def tool_client_for(
    servers: Mapping[str, ToolTarget],
    *,
    registry: Registry,
    dsn: str,
    tracer: Tracer,
    identity: RunIdentity,
    throttle: RefusalAuditThrottle,
    verify: ssl.SSLContext | bool = True,
    transport: ToolTransport | None = None,
) -> ToolClient:
    """The run's tool client, over the app's ``transport`` when it has one (the
    runs share its connections). A call its allowlist refuses is audited here,
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
        verify=verify,
        transport=transport,
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
    policy = caller_policy(settings.identity_prefix, SERVICE_NAME, registry)
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
    # One context for the gateway client and every tool call, built once: a
    # certificate or key that cannot be loaded stops the start.
    verify = verify_of(settings.client_tls)
    http = http_client or make_gateway_client(settings, verify)
    # One kept HTTP client for each tool server, shared by every run; closed
    # at shutdown, with the gateway client when the app made that itself.
    tool_transport = ToolTransport(verify)
    dsn = settings.database_url

    # An injected checkpointer (tests) serves every request; otherwise each
    # request opens the PostgreSQL saver on a connection of its own (S015).
    def saver_scope() -> AbstractContextManager[BaseCheckpointSaver]:
        return open_saver(dsn) if checkpointer is None else nullcontext(checkpointer)

    def close() -> None:
        # The gateway client is closed only when the app made it: an injected
        # one is its owner's.
        try:
            tool_transport.close()
        finally:
            if http_client is None:
                http.close()

    service = create_service_app(
        title="Meridian Agent Runtime",
        description="Runs an agent's graph and keeps the run's status (ADR 2).",
        service_name=SERVICE_NAME,
        tracer_name="meridian.runtime",
        max_body_bytes=SMALL_BODY_LIMIT_BYTES,
        tracer_provider=tracer_provider,
        close=close,
    )
    app, tracer = service.app, service.tracer

    # Outside the app's other middleware: a call that comes from no known
    # service is refused before its body is read (S055). The row says who in
    # ``reference``, which a run's own rows use for the claim's reference.
    install_caller_check(
        app,
        policy,
        audited_refusals(
            refusal_throttle,
            lambda reason, who, carried: write_audit(
                dsn,
                AuditEvent(
                    service=SERVICE_NAME,
                    event="run.refused",
                    outcome="refused",
                    reason=reason,
                    reference=who,
                    suppressed=carried,
                ),
            ),
        ),
    )

    def record_refusal(
        tenant: str,
        agent: str | None,
        reference: str,
        reason: str | None = None,
        run_id: uuid.UUID | None = None,
    ) -> None:
        """Write the audit row of a refusal. A caller's name refusal (S055) is
        due a row once per window (T-49): the key is the reason and the tenant
        only when the registry holds it, as a request's tenant is
        caller-chosen and never a key, and a write that fails releases the
        window. The runtime's other refusals write a row each."""
        event = AuditEvent(
            service=SERVICE_NAME,
            event="run.refused",
            outcome="refused",
            reason=reason,
            tenant=tenant,
            agent=agent,
            run_id=run_id,
            reference=reference,
        )
        if reason != NAME_REFUSAL_REASON:
            write_audit(dsn, event)
            return
        known = tenant if registry.tenant(tenant) is not None else None
        carried = refusal_throttle.due(known, NAME_REFUSAL_REASON)
        if carried is None:
            return
        try:
            write_audit(dsn, replace(event, suppressed=carried))
        except BaseException:
            refusal_throttle.release(known, NAME_REFUSAL_REASON, carried)
            raise

    def refuse(
        tenant: str,
        agent: str | None,
        reference: str,
        reason: str | None = None,
        run_id: uuid.UUID | None = None,
    ) -> HTTPException:
        record_refusal(tenant, agent, reference, reason, run_id)
        return HTTPException(status_code=403, detail=REFUSED)

    def run_leg(
        factory: GraphFactory,
        saver: BaseCheckpointSaver,
        span: Span,
        identity: RunIdentity,
        response: Response,
        run_input: dict[str, Any],
        resume: dict[str, Any] | None = None,
    ) -> RunResponse | JSONResponse:
        """Run one leg of a run that is ``Running`` (its first, or one resumed
        after a pause) and record how it ended: the failure logged, the status
        and its audit event written, the checkpoints dropped unless the run
        is paused, the answer built. A resumed leg that fails leaves the run
        paused, its pause still pending in the checkpoints, so a later resume
        can finish it; only a resume with nothing to resume ends it ``Failed``."""
        failure: Exception | None = None
        leg: Leg = "first" if resume is None else "resumed"
        try:
            tools = tool_client_for(
                servers,
                registry=registry,
                dsn=dsn,
                tracer=tracer,
                identity=identity,
                throttle=refusal_throttle,
                verify=verify,
                transport=tool_transport,
            )
            outcome = runs.execute(
                factory, saver, http, tools, tracer, identity, run_input, resume=resume
            )
        except Exception as exc:
            _log_failure(identity.run_id, exc, leg)
            mark_error(span, exc)
            failure, outcome = exc, RunOutcome("Failed", None)
        outcome, failure, unsaved = _settle(dsn, identity, leg, failure, outcome)
        if outcome.status != "AwaitingApproval":
            # The checkpoint holds the claim; a finished run needs none,
            # whether or not its status could be recorded.
            _delete_checkpoints(saver, identity, saver_scope)
        set_span_attributes(span, {"meridian.run_status": outcome.status})
        if unsaved is not None:
            return _unsaved_answer(span, identity, outcome.status, unsaved)
        if failure is not None:
            response.status_code = _failure_status(failure)
        return RunResponse(
            run_id=identity.run_id, status=outcome.status, output=outcome.output
        )

    @app.post(
        "/runs",
        tags=["runs"],
        summary="Start a run of an agent's graph and wait for it to end or pause.",
        response_model=RunResponse,
        # A failed run answers 502 (504 for a gateway timeout) with the usual
        # RunResponse, status "Failed"; a 503 names the run when it has one.
        responses=error_responses(401, 403, 413, 500)
        | error_responses(502, 504, model=RunResponse)
        | error_responses(503, model=RunErrorBody),
    )
    def create_run(
        body: RunRequest, response: Response, request: Request
    ) -> RunResponse | JSONResponse:
        calling = caller_service(request)
        if not caller_may_name(policy, calling, body.tenant, body.agent):
            raise refuse(body.tenant, body.agent, body.reference, NAME_REFUSAL_REASON)
        if not registry.tenant_may_run(body.tenant, body.agent):
            raise refuse(body.tenant, body.agent, body.reference)
        factory = factories.get(body.agent)
        if factory is None:
            # A job agent: a tenant may list it, but it has no graph to run.
            raise refuse(body.tenant, body.agent, body.reference, JOB_REFUSAL_REASON)
        identity = RunIdentity(
            run_id=uuid.uuid4(),
            thread_id=uuid.uuid4(),
            agent=body.agent,
            tenant=body.tenant,
            reference=body.reference,
        )
        # The saver is opened before the run row is written, so a database
        # that refuses its connection starts no run (same answer as start_run).
        with start_span(tracer, "runtime.run") as span, saver_scope() as saver:
            _set_run_attributes(span, identity.run_id, identity.agent, identity.tenant)
            runs.start_run(dsn, identity)
            return run_leg(factory, saver, span, identity, response, body.input)

    @app.post(
        "/runs/{run_id}/resume",
        tags=["runs"],
        summary="Resume a run that paused and wait for it to end or pause again.",
        response_model=RunResponse,
        # A run that is not paused (already resumed, running, finished) answers
        # 200 with its status and no output, unless it has been Running for
        # longer than its lease, when the resume takes it over (a leg that died
        # leaves a run so); the rest is as for POST /runs,
        # except that a failed leg answers its 502 or 504 with status
        # "AwaitingApproval": the run is paused again and can be resumed.
        responses=error_responses(401, 403, 404, 413, 500)
        | error_responses(502, 504, model=RunResponse)
        | error_responses(503, model=RunErrorBody),
    )
    def resume_run(
        run_id: uuid.UUID, body: ResumeRequest, response: Response, request: Request
    ) -> RunResponse | JSONResponse:
        # A resume names a tenant and a reference; the agent is the run's own.
        # The tenant is checked before the run is read, so a caller that may
        # not name it learns nothing of a run under it (T-10).
        calling = caller_service(request)
        if not caller_may_name(policy, calling, body.tenant):
            raise refuse(body.tenant, None, body.reference, NAME_REFUSAL_REASON)
        found = runs.fetch_run(dsn, run_id)
        # The same answer for a run that is not there and one under another
        # tenant or reference: no answer says that a run ID exists (T-10).
        if found is None or (found.tenant, found.reference) != (
            body.tenant,
            body.reference,
        ):
            raise HTTPException(status_code=404, detail=NO_SUCH_RUN)
        # A run whose agent the caller may not name is not there for it: a 403
        # would say that the run exists. The refusal is audited.
        if not caller_may_name(policy, calling, found.tenant, found.agent):
            record_refusal(
                found.tenant,
                found.agent,
                found.reference,
                NAME_REFUSAL_REASON,
                run_id=run_id,
            )
            raise HTTPException(status_code=404, detail=NO_SUCH_RUN)
        if not registry.tenant_may_run(found.tenant, found.agent):
            raise refuse(found.tenant, found.agent, found.reference, run_id=run_id)
        factory = factories.get(found.agent)
        if factory is None:
            raise refuse(
                found.tenant,
                found.agent,
                found.reference,
                JOB_REFUSAL_REASON,
                run_id=run_id,
            )
        if found.status in ("Completed", "Failed"):
            return RunResponse(run_id=run_id, status=found.status, output=None)
        # A run that is paused, or Running (a leg in progress, or one that died:
        # the claim takes over only a run idle for longer than its lease), goes
        # to the claim, which decides.
        # The saver is opened before the claim, so a database that refuses its
        # connection leaves the run paused instead of stuck as Running.
        with start_span(tracer, "runtime.resume") as span, saver_scope() as saver:
            _set_run_attributes(span, run_id, found.agent, found.tenant)
            identity = runs.claim_paused_run(dsn, run_id, body.tenant, body.reference)
            if identity is None:
                # Another request claimed the run between the read and the
                # claim: it runs the leg, and this one reports where the run is.
                current = runs.fetch_run(dsn, run_id)
                if current is None:
                    raise HTTPException(status_code=404, detail=NO_SUCH_RUN)
                return RunResponse(run_id=run_id, status=current.status, output=None)
            return run_leg(factory, saver, span, identity, response, {}, body.input)

    @app.get(
        "/runs/{run_id}",
        tags=["runs"],
        summary="Read the status of a run of the given tenant and reference.",
        responses=error_responses(401, 403, 404, 500, 503),
    )
    def read_run(
        run_id: uuid.UUID,
        tenant: BoundedEntityId,
        reference: Reference,
        request: Request,
    ) -> RunStatus:
        # As for a resume: the tenant is checked before the run is read, so a
        # caller that may not name it learns nothing of a run under it (T-10).
        calling = caller_service(request)
        if not caller_may_name(policy, calling, tenant):
            raise refuse(tenant, None, reference, NAME_REFUSAL_REASON)
        found = runs.fetch_run(dsn, run_id)
        # Bound like the resume: the same 404 for a run that is not there and
        # one under another tenant or reference (T-10).
        if found is None or (found.tenant, found.reference) != (tenant, reference):
            raise HTTPException(status_code=404, detail=NO_SUCH_RUN)
        # And for a run whose agent the caller may not name, which the answer
        # would otherwise show.
        if not caller_may_name(policy, calling, found.tenant, found.agent):
            record_refusal(
                found.tenant,
                found.agent,
                found.reference,
                NAME_REFUSAL_REASON,
                run_id=run_id,
            )
            raise HTTPException(status_code=404, detail=NO_SUCH_RUN)
        return found

    return app


def create_app_from_env() -> FastAPI:
    """The factory S041 runs under ``uvicorn --factory``."""
    install_log_redaction()
    return create_app(RuntimeSettings.from_env())
