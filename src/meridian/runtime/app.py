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
from contextlib import AbstractContextManager, ExitStack, nullcontext
from dataclasses import replace
from typing import Any

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde import _msgpack
from opentelemetry.sdk.metrics import MeterProvider
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
from meridian.platform.common.logformat import configure_logging
from meridian.platform.common.logredaction import install_log_redaction
from meridian.platform.common.metrics import make_meter_provider
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
from meridian.runtime.graphs import load_graph_factory
from meridian.runtime.host_wiring import build_host_scopes
from meridian.runtime.hosts import Host, HostScope, log_forget_failure
from meridian.runtime.meters import RuntimeMeters, shut_down
from meridian.runtime.model_client import (
    ModelCallError,
    ModelCallTimeoutError,
    ModelClient,
)
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
from meridian.runtime.settling import Leg, settle, tool_of
from meridian.runtime.tool_client import (
    ClientRefusal,
    ToolClient,
    ToolRefused,
    ToolTarget,
    prepare_sdk,
)
from meridian.runtime.tool_transport import ToolTransport

GATEWAY_TIMEOUT_SECONDS = 30.0
# The audit reason of a call the runtime's own allowlist refuses.
REFUSAL_REASON: ClientRefusal = "tool-not-allowed"
# The audit reason of a run request for a job agent, which has no graph.
JOB_REFUSAL_REASON = "not-a-graph-agent"
# The detail of a 404 for a run: the same text whether the run does not exist
# or belongs to another tenant or reference.
NO_SUCH_RUN = "no such run"
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
        tool_of(error),
        refusal,
    )


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


def _forget(host: Host, identity: RunIdentity) -> None:
    """Have the run's host drop its checkpoints; a failure is logged and changes
    no answer (the sweep is the backstop). LangGraph's host retries and logs on
    its own and raises nothing; the second host raises, and one line says so."""
    try:
        host.forget(identity)
    except Exception as exc:  # whatever a host raises changes no answer
        log_forget_failure(identity, exc)


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

    def audit_refusal(
        tool: str | None,
        reason: ClientRefusal = REFUSAL_REASON,
        worker: str | None = None,
    ) -> None:
        # A window per tool and reason: a worker's refusal of a tool does not
        # use up the window of the agent's refusal of it (S031). The worker is
        # the view's, from the client, never a name a model or a caller chose.
        key = f"{tool or '-'}/{reason}"
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
                    reason=reason,
                    tool=tool,
                    worker=worker,
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
        on_worker_refusal=audit_refusal,
        max_calls=runs.MAX_TOOL_CALLS_PER_RUN,
        verify=verify,
        transport=transport,
    )


def create_app(
    settings: RuntimeSettings,
    *,
    tracer_provider: TracerProvider | None = None,
    meter_provider: MeterProvider | None = None,
    http_client: httpx.Client | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    tool_servers: Mapping[str, ToolTarget] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build the app; raise when it must not start: a registry that fails to
    load, a graph that cannot be loaded or is not what its agent's host runs, a
    tool server the registry does not have, LangSmith requested or LangGraph not
    in strict msgpack mode.

    ``tool_servers`` (tests) replaces the settings' addresses with targets the
    SDK's client accepts, such as an in-process server. ``clock`` times the
    audit throttle of the runtime's own tool refusals. A ``meter_provider`` is
    its caller's to shut down; without one the app builds its own, which it
    shuts down with the app."""
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
    # start instead of failing a request; the hosts are built from this. A job
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
    owns_meter_provider = meter_provider is None
    app_meter_provider = (
        make_meter_provider(SERVICE_NAME) if meter_provider is None else meter_provider
    )
    meters = RuntimeMeters(app_meter_provider)

    # An injected checkpointer (tests) serves every request; otherwise each
    # request opens the PostgreSQL saver on a connection of its own (S015).
    def saver_scope() -> AbstractContextManager[BaseCheckpointSaver]:
        return open_saver(dsn) if checkpointer is None else nullcontext(checkpointer)

    def close() -> None:
        # The gateway client and the meter provider are closed only when the
        # app made them: an injected one is its owner's.
        try:
            tool_transport.close()
        finally:
            try:
                if http_client is None:
                    http.close()
            finally:
                if owns_meter_provider:
                    shut_down(app_meter_provider)  # a failure is a WARNING

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
    # The host each agent's registry entry names, built once and kept; an entry
    # point that is not what its host runs stops the start (S037).
    try:
        scopes: dict[str, HostScope] = build_host_scopes(
            registry,
            factories,
            dsn=dsn,
            saver_scope=saver_scope,
            http=http,
            servers=servers,
            tracer=tracer,
            verify=verify,
            transport=tool_transport,
        )
    except Exception:
        close()
        raise

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
        host: Host,
        span: Span,
        identity: RunIdentity,
        response: Response,
        run_input: dict[str, Any],
        resume: dict[str, Any] | None = None,
    ) -> RunResponse | JSONResponse:
        """Run one leg of a run that is ``Running`` (its first, or one resumed
        after a pause) on the agent's ``host``, and record how it ended: the
        failure logged, the status and its audit event written, the checkpoints
        forgotten unless the run is paused, the answer built. A resumed leg that
        fails leaves the run paused, its pause still pending in the checkpoints,
        so a later resume can finish it; only a resume that can never succeed
        ends it ``Failed`` (``runs.RESUME_CANNOT_SUCCEED``: nothing to resume, a
        checkpoint the host refuses, a graph that changed). The leg's two
        clients, with their limits, are the neutral code's and the host's
        workload gets nothing else."""
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
            model = ModelClient(
                http,
                tenant=identity.tenant,
                agent=identity.agent,
                run_id=identity.run_id,
                max_calls=runs.MAX_MODEL_CALLS_PER_RUN,
                on_call=meters.model_call_observer(identity),
            )
            if resume is None:
                outcome = host.start(identity, model, tools, tracer, run_input)
            else:
                outcome = host.resume(identity, model, tools, tracer, resume)
        except Exception as exc:
            _log_failure(identity.run_id, exc, leg)
            mark_error(span, exc)
            failure, outcome = exc, RunOutcome("Failed", None)
        settled, answered, unsaved = settle(dsn, identity, leg, failure, outcome)
        # Counted once, after the write, by what the leg itself did (not what
        # settle answers for the sweep); not-saved when nothing was written.
        meters.leg_ended(identity, outcome, failure, saved=unsaved is None)
        if settled.status != "AwaitingApproval":
            # The checkpoint holds the claim; a finished run needs none,
            # whether or not its status could be recorded. After settle: the
            # second host's delete skips a run its row does not say has ended.
            _forget(host, identity)
        set_span_attributes(span, {"meridian.run_status": settled.status})
        if unsaved is not None:
            return _unsaved_answer(span, identity, settled.status, unsaved)
        if answered is not None:
            response.status_code = _failure_status(answered)
        return RunResponse(
            run_id=identity.run_id, status=settled.status, output=settled.output
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
        scope = scopes.get(body.agent)
        if scope is None:
            # A job agent: a tenant may list it, but it has no graph to run.
            raise refuse(body.tenant, body.agent, body.reference, JOB_REFUSAL_REASON)
        identity = RunIdentity(
            run_id=uuid.uuid4(),
            thread_id=uuid.uuid4(),
            agent=body.agent,
            tenant=body.tenant,
            reference=body.reference,
        )
        # The host's scope is entered before the run row is written: LangGraph's
        # opens its saver there, so a database that refuses its connection
        # starts no run (same answer as start_run).
        with start_span(tracer, "runtime.run") as span, ExitStack() as opened:
            _set_run_attributes(span, identity.run_id, identity.agent, identity.tenant)
            # Both passed tenant_may_run: a refused start counts under them.
            with meters.start_counted(identity.tenant, identity.agent):
                host = opened.enter_context(scope())
                runs.start_run(dsn, identity)
            return run_leg(host, span, identity, response, body.input)

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
        scope = scopes.get(found.agent)
        if scope is None:
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
        # The host's scope is entered before the claim, so a database that
        # refuses the saver's connection leaves the run paused instead of stuck
        # as Running.
        with start_span(tracer, "runtime.resume") as span, ExitStack() as opened:
            _set_run_attributes(span, run_id, found.agent, found.tenant)
            # Both passed tenant_may_run; an empty claim is no error, no count.
            with meters.start_counted(found.tenant, found.agent):
                host = opened.enter_context(scope())
                identity = runs.claim_paused_run(
                    dsn, run_id, body.tenant, body.reference
                )
            if identity is None:
                # Another request claimed the run between the read and the
                # claim: it runs the leg, and this one reports where the run is.
                current = runs.fetch_run(dsn, run_id)
                if current is None:
                    raise HTTPException(status_code=404, detail=NO_SUCH_RUN)
                return RunResponse(run_id=run_id, status=current.status, output=None)
            # The host was taken from the read, which had to come before the
            # claim (the scope opens the saver first). The claimed row is the
            # authority: a leg never runs on another agent's host.
            if identity.agent != found.agent:
                raise RuntimeError("the claimed run is not the run that was read")
            return run_leg(host, span, identity, response, {}, body.input)

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
    configure_logging(SERVICE_NAME)
    return create_app(RuntimeSettings.from_env())
