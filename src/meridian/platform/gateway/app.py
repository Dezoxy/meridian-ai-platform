"""The Model Gateway app: ``POST /v1/chat`` and ``POST /v1/embeddings``, replay
and live (S009, S010, S011, S045).

One path for both modes and both purposes. A call gets its ID first, so every
audit row of the request carries it. Then the route of its purpose is decided
and refused (403) and audited when nothing is allowed. The tenant's rate windows
come next, one set for both purposes: a request over them is refused (429, or
413 when it alone is larger than the tenant's token limit) before any circuit,
reservation or provider is touched. Then the allowed candidates are walked in
order under one deadline (S042), each one reserved in the ledger before it is
called (QA-12). A candidate whose circuit is open, or that the deadline leaves
no time for, is skipped; a deployment's own failure moves the walk to the next
candidate; a rejected request ends it; a reservation the tenant's budget
refuses ends it too (429, or the earlier attempt's answer).
Every candidate touched leaves one audit row, except that a refusal, whether
policy's 403 or a tenant limit's 429 or 413, leaves at most one row per tenant
and reason per minute, and that row says how many refusals it stands in for, so
a flood cannot fill the log (T-49). The audit write is part of the answer, so a
call that cannot be recorded returns no output (QA-05). There is no retry of
one deployment: the next candidate is the retry. The limits apply in replay
mode as in live mode: replay simulates the provider, not the gateway's
controls. The gateway counts tokens, cost and calls in its metrics. No text a
caller sends and no vector it gets back is in an audit row, a span, a metric or
a log line (T-56).
"""

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, NoReturn

from fastapi import FastAPI, Header, HTTPException
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span

from meridian.platform.common.audit import AuditEvent, write_audit
from meridian.platform.common.env import SettingsError
from meridian.platform.common.http import (
    GATEWAY_BODY_LIMIT_BYTES,
    HTTP_PAYLOAD_TOO_LARGE,
    REFUSED,
    BoundedEntityId,
    create_service_app,
    error_responses,
)
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import set_span_attributes, start_span
from meridian.platform.common.throttle import RefusalAuditThrottle
from meridian.platform.gateway.budget import (
    BudgetRefusalReason,
    Caller,
    Ledger,
    utc_today,
)
from meridian.platform.gateway.meters import CallRecord, GatewayMeters
from meridian.platform.gateway.models import (
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
)
from meridian.platform.gateway.operations import chat_operation, embedding_operation
from meridian.platform.gateway.providers.base import ModelProvider, ProviderError
from meridian.platform.gateway.ratelimit import RateRefusalReason, TenantRateLimiter
from meridian.platform.gateway.replay import ReplayProvider
from meridian.platform.gateway.resilience import CircuitBreaker
from meridian.platform.gateway.routing import RefusalReason, decide
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.gateway.walk import (
    BudgetRefused,
    CandidateWalker,
    Operation,
    Unanswered,
    caller_fields,
    route_attributes,
    route_facts,
)
from meridian.platform.registry import Registry, load_registry
from meridian.platform.registry.models import Deployment

SERVICE_NAME = "model-gateway"
CHAT_PURPOSE = "chat"
EMBEDDING_PURPOSE = "embedding"
REPLAY_ENVIRONMENTS = frozenset({"test", "ci", "kind"})
# The Azure CLI credential is a developer's login, so a gateway that builds its
# own live providers starts on a laptop only.
LIVE_ENVIRONMENT = "local"
AZURE_KIND = "azure-openai"
REPLAY_KIND = "replay"

HTTP_BAD_GATEWAY = 502
HTTP_SERVICE_UNAVAILABLE = 503
HTTP_GATEWAY_TIMEOUT = 504
# One fixed text per status: the provider's own words can echo a prompt (T-18).
PROVIDER_TIMED_OUT = "the model provider did not answer in time"
PROVIDER_FAILED = "the model provider failed"
# No attempt was made: every candidate was skipped.
PROVIDER_UNAVAILABLE = "the model provider is unavailable"
# The reason on the calls counter of a request no candidate was called for.
NOT_CALLED_REASON = "unavailable"
# The route's own description of 503; the shared one names the database only.
CHAT_UNAVAILABLE = (
    "The audit log is unavailable, or no model deployment can be tried now."
)
HTTP_TOO_MANY_REQUESTS = 429
# The route's own description of 413: the shared one names the body limit only.
CHAT_TOO_LARGE = (
    "The request body is too large, or the request is larger than the tenant's "
    "token limit."
)
# The embeddings route says the same of both.
EMBEDDINGS_UNAVAILABLE = CHAT_UNAVAILABLE
EMBEDDINGS_TOO_LARGE = CHAT_TOO_LARGE
# The three headers of a call, the same for both routes.
TenantHeader = Annotated[BoundedEntityId, Header(alias="X-Meridian-Tenant")]
AgentHeader = Annotated[BoundedEntityId, Header(alias="X-Meridian-Agent")]
RunHeader = Annotated[uuid.UUID, Header(alias="X-Meridian-Run")]
# One fixed text per refusal for a tenant limit; the reason is in the audit row.
TENANT_RATE_LIMIT_REACHED = "the tenant's rate limit is reached"
TENANT_BUDGET_USED_UP = "the tenant's budget is used up"
TENANT_REQUEST_TOO_LARGE = "the request is larger than the tenant's token limit"
LimitRefusalReason = RateRefusalReason | BudgetRefusalReason
LIMIT_ANSWERS: dict[LimitRefusalReason, tuple[int, str]] = {
    "tenant-request-rate": (HTTP_TOO_MANY_REQUESTS, TENANT_RATE_LIMIT_REACHED),
    "tenant-token-rate": (HTTP_TOO_MANY_REQUESTS, TENANT_RATE_LIMIT_REACHED),
    "tenant-token-budget": (HTTP_TOO_MANY_REQUESTS, TENANT_BUDGET_USED_UP),
    "tenant-cost-budget": (HTTP_TOO_MANY_REQUESTS, TENANT_BUDGET_USED_UP),
    "tenant-request-too-large": (HTTP_PAYLOAD_TOO_LARGE, TENANT_REQUEST_TOO_LARGE),
}


def _check_start_allowed(
    settings: GatewaySettings, providers: Mapping[str, ModelProvider] | None
) -> None:
    if settings.mode == "replay":
        if settings.environment not in REPLAY_ENVIRONMENTS:
            raise SettingsError(
                "replay mode is refused outside the test, ci and kind environments "
                "(T-39)"
            )
        return
    if providers is not None:
        return  # the caller brought its own providers (tests)
    if settings.azure_credential != "azure-cli":
        raise SettingsError(
            "live mode needs MERIDIAN_AZURE_CREDENTIAL=azure-cli: the only "
            "credential source is a developer's az login"
        )
    if settings.environment != LIVE_ENVIRONMENT:
        raise SettingsError(
            "live mode with the Azure CLI credential is refused outside the local "
            "environment"
        )


@dataclass(frozen=True, slots=True)
class _Route:
    """What one purpose may use: the deployments a request may use before policy
    narrows them (in replay mode its one replay deployment, in live mode the
    route's candidates in order), and, in replay mode only, that deployment: where
    the call goes is known before policy decides, so a refusal says it too
    (T-39)."""

    considered: tuple[Deployment, ...]
    replay: Deployment | None


def _route(settings: GatewaySettings, registry: Registry, purpose: str) -> _Route:
    if settings.mode == "replay":
        deployment = registry.replay_deployment(purpose)
        if deployment is None:
            raise SettingsError(f"the registry has no replay deployment for {purpose}")
        return _Route((deployment,), deployment)
    route = registry.route(purpose)
    if route is None:
        return _Route((), None)
    found = tuple(registry.deployment(name) for name in route.candidates)
    if any(d is None for d in found):
        raise SettingsError(
            f"the {purpose} route names a deployment the registry lacks"
        )
    return _Route(tuple(d for d in found if d is not None), None)


def _provider_kinds(
    registry: Registry, considered: Sequence[Deployment]
) -> dict[str, str]:
    """Deployment ID to the kind of its provider (the key of ``providers``)."""
    kinds: dict[str, str] = {}
    for deployment in considered:
        provider = registry.provider(deployment.provider)
        if provider is None:
            raise SettingsError(f"deployment {deployment.id} has no known provider")
        kinds[deployment.id] = provider.kind
    return kinds


def _check_azure_candidates(
    settings: GatewaySettings,
    considered: Sequence[Deployment],
    kinds: Mapping[str, str],
) -> None:
    """What ``AzureOpenAIProvider.chat`` and ``embed`` would otherwise raise a
    ``ValueError`` for at the first request."""
    for deployment in considered:
        if kinds[deployment.id] != AZURE_KIND:
            continue
        if deployment.terraform_key is None or deployment.deployment_name is None:
            raise SettingsError(
                f"deployment {deployment.id} has no terraform_key or deployment_name"
            )
        if deployment.purpose == EMBEDDING_PURPOSE and deployment.dimensions is None:
            raise SettingsError(f"deployment {deployment.id} has no dimensions")
        location = deployment.terraform_key.partition("/")[0]
        if location not in settings.azure_openai_endpoints:
            raise SettingsError(
                f"deployment {deployment.id} has no endpoint in "
                "MERIDIAN_AZURE_OPENAI_ENDPOINTS for its location"
            )


def _live_providers(
    settings: GatewaySettings,
    considered: Sequence[Deployment],
    kinds: Mapping[str, str],
) -> tuple[dict[str, ModelProvider], Callable[[], None]]:
    """The Azure provider and the call that closes it, with one token fetched
    now so a missing ``az login`` stops the start and not the first request.

    The adapter, and with it the SDKs, is imported here and nowhere at module
    level, so a replay process never loads them.
    """
    from meridian.platform.gateway.providers.azure_openai import (
        AzureOpenAIProvider,
        azure_cli_token_provider,
        check_token,
        refuse_sdk_environment,
    )

    try:
        refuse_sdk_environment()
    except ValueError as error:  # names the variable, never its value
        raise SettingsError(str(error)) from None
    if settings.azure_tenant_id is None:
        raise SettingsError(
            "live mode needs MERIDIAN_AZURE_TENANT_ID: the Azure CLI's default "
            "account must not decide which tenant a token is for"
        )
    _check_azure_candidates(settings, considered, kinds)
    token_provider = azure_cli_token_provider(settings.azure_tenant_id)
    try:
        check_token(token_provider)
    except ProviderError:
        # Nothing of the cause: it can name the tenant or the account.
        raise SettingsError(
            "no Azure token: run `az login` for the tenant in "
            "MERIDIAN_AZURE_TENANT_ID and start again"
        ) from None
    provider = AzureOpenAIProvider(settings.azure_openai_endpoints, token_provider)
    return {AZURE_KIND: provider}, provider.close


def _unanswered(result: Unanswered) -> NoReturn:
    """The status when no candidate answered: the last attempt's kind, or 503
    when none was called. Never a word of the provider's (T-18)."""
    if result.attempts == 0:
        raise HTTPException(
            status_code=HTTP_SERVICE_UNAVAILABLE, detail=PROVIDER_UNAVAILABLE
        )
    timed_out = result.last_attempt_kind == "timeout"
    raise HTTPException(
        status_code=HTTP_GATEWAY_TIMEOUT if timed_out else HTTP_BAD_GATEWAY,
        detail=PROVIDER_TIMED_OUT if timed_out else PROVIDER_FAILED,
    )


def create_app(
    settings: GatewaySettings,
    *,
    tracer_provider: TracerProvider | None = None,
    meter_provider: MeterProvider | None = None,
    providers: Mapping[str, ModelProvider] | None = None,
    clock: Callable[[], float] = time.monotonic,
    today: Callable[[], date] = utc_today,
) -> FastAPI:
    """Build the app; raise when the registry fails to load or the mode,
    environment and providers are not an allowed combination.

    ``providers`` is keyed by provider kind (``replay``, ``azure-openai``); a
    test injects fakes. Without it the app builds the replay provider and, in
    live mode, the Azure one. A provider is asked for the method of the purpose
    of the request only, so a fake with ``chat`` alone serves chat, and nothing
    checks at the start that it has ``embed``. ``clock`` times the circuit
    breaker (one per app), the tenants' rate windows and each request's
    deadline; ``today`` is the UTC day the ledger charges to. A test injects
    fakes of both. A ``meter_provider`` is its caller's to shut down; without
    one the app builds its own.
    """
    _check_start_allowed(settings, providers)
    registry = load_registry(settings.registry_dir)
    routes = {
        purpose: _route(settings, registry, purpose)
        for purpose in (CHAT_PURPOSE, EMBEDDING_PURPOSE)
    }
    considered = tuple(d for route in routes.values() for d in route.considered)
    kinds = _provider_kinds(registry, considered)
    if settings.mode == "live":
        for purpose, route in routes.items():
            if any(kinds[d.id] == REPLAY_KIND for d in route.considered):
                # The registry checks refuse it too; this is the second line.
                raise SettingsError(
                    f"the {purpose} route has a replay candidate in live mode"
                )
    close: Callable[[], None] | None = None
    if providers is None:
        if settings.mode == "replay":
            providers = {REPLAY_KIND: ReplayProvider()}
        else:
            providers, close = _live_providers(settings, considered, kinds)
    if not set(kinds.values()) <= providers.keys():
        raise SettingsError("a deployment is routed to a provider kind with no adapter")
    owns_meter_provider = meter_provider is None
    app_meter_provider = (
        make_meter_provider(SERVICE_NAME) if meter_provider is None else meter_provider
    )

    def close_all() -> None:
        """Only what this function built: an injected provider is its caller's."""
        try:
            if close is not None:
                close()
        finally:
            if owns_meter_provider:
                app_meter_provider.shutdown()

    service = create_service_app(
        title="Meridian Model Gateway",
        description="Every model call goes through here (ADR 3, hard rule 4).",
        service_name=SERVICE_NAME,
        tracer_name="meridian.gateway",
        max_body_bytes=GATEWAY_BODY_LIMIT_BYTES,
        tracer_provider=tracer_provider,
        close=close_all,
    )
    app, tracer = service.app, service.tracer
    meters = GatewayMeters(app_meter_provider)
    limiter = TenantRateLimiter(clock=clock)
    refusal_throttle = RefusalAuditThrottle(clock=clock)

    def audit(event: str, outcome: str, **fields: object) -> None:
        write_audit(
            settings.database_url,
            AuditEvent(service=SERVICE_NAME, event=event, outcome=outcome, **fields),
        )

    walker = CandidateWalker(
        tracer=tracer,
        providers=providers,
        kinds=kinds,
        breaker=CircuitBreaker(clock=clock),
        audit=audit,
        ledger=Ledger(settings.database_url, exchange=registry.exchange, today=today),
        meters=meters,
        clock=clock,
        mode=settings.mode,
    )

    def audit_refusal(
        caller: Caller,
        throttle_tenant: str | None,
        reason: str,
        facts: dict[str, str | None],
    ) -> None:
        """Write the row of a refusal when the throttle says it is due, with
        the count of the refusals it stands in for (T-49). The window starts
        when the refusal is due, so overlapping refusals leave one row, and a
        write that fails releases it and loses nothing: the next refusal is due
        and counts this one."""
        carried = refusal_throttle.due(throttle_tenant, reason)
        if carried is None:
            return
        try:
            audit(
                "model.call",
                "refused",
                **caller_fields(caller),
                **facts,
                reason=reason,
                suppressed=carried,
            )
        except BaseException:
            refusal_throttle.release(throttle_tenant, reason, carried)
            raise

    def refuse(
        span: Span,
        record: CallRecord,
        caller: Caller,
        route: _Route,
        data_class: str | None,
        refusal: RefusalReason,
    ) -> NoReturn:
        """Answer 403 for a request policy refuses. The throttle key is the
        reason and, unless the tenant is unknown, the tenant: a header's value
        is caller-chosen and never becomes a key; never the agent."""
        set_span_attributes(span, {"meridian.refusal": refusal})
        record.end("refused", refusal)
        throttle_tenant = None if refusal == "unknown-tenant" else caller.tenant
        facts = route_facts(route.replay, data_class)
        audit_refusal(caller, throttle_tenant, refusal, facts)
        raise HTTPException(status_code=403, detail=REFUSED)

    def refuse_limit(
        span: Span,
        record: CallRecord,
        caller: Caller,
        route: _Route,
        data_class: str | None,
        reason: LimitRefusalReason,
        *,
        retry_after: int | None = None,
        candidate: Deployment | None = None,
    ) -> NoReturn:
        """Answer a request a tenant limit refuses. Every refusal sets the span
        attribute and is counted; the audit row is written for the first of its
        tenant and reason in the window only (T-49). Both parts of that key are
        registry IDs, because routing passed. In replay mode every row names
        the replay deployment of the request's purpose (T-39)."""
        status, detail = LIMIT_ANSWERS[reason]
        set_span_attributes(span, {"meridian.refusal": reason})
        record.end("refused", reason)
        facts = route_facts(
            candidate if candidate is not None else route.replay, data_class
        )
        audit_refusal(caller, caller.tenant, reason, facts)
        headers = None if retry_after is None else {"Retry-After": str(retry_after)}
        raise HTTPException(status_code=status, detail=detail, headers=headers)

    def route_responses(unavailable: str, too_large: str) -> dict[int | str, Any]:
        responses = error_responses(403, 413, 429, 500, 502, 503, 504)
        responses[HTTP_SERVICE_UNAVAILABLE]["description"] = unavailable
        responses[HTTP_PAYLOAD_TOO_LARGE]["description"] = too_large
        return responses

    @app.post(
        "/v1/chat",
        tags=["chat"],
        summary="Answer a chat request (replay simulates, live calls a model).",
        responses=route_responses(CHAT_UNAVAILABLE, CHAT_TOO_LARGE),
    )
    def chat(
        body: ChatRequest,
        tenant_id: TenantHeader,
        agent_id: AgentHeader,
        run_id: RunHeader,
    ) -> ChatResponse:
        # The call ID exists before anything can refuse the request.
        caller = Caller(uuid.uuid4(), tenant_id, agent_id, run_id)
        return handle(
            "gateway.chat", routes[CHAT_PURPOSE], caller, chat_operation(body)
        )

    @app.post(
        "/v1/embeddings",
        tags=["embeddings"],
        summary="Embed texts (replay simulates, live calls a model).",
        responses=route_responses(EMBEDDINGS_UNAVAILABLE, EMBEDDINGS_TOO_LARGE),
    )
    def embeddings(
        body: EmbeddingRequest,
        tenant_id: TenantHeader,
        agent_id: AgentHeader,
        run_id: RunHeader,
    ) -> EmbeddingResponse:
        caller = Caller(uuid.uuid4(), tenant_id, agent_id, run_id)
        return handle(
            "gateway.embeddings",
            routes[EMBEDDING_PURPOSE],
            caller,
            embedding_operation(body),
        )

    def handle[ResponseT](
        span_name: str,
        route: _Route,
        caller: Caller,
        operation: Operation[Any, ResponseT],
    ) -> ResponseT:
        """One request of either purpose: a span of its own, counted once by its
        outcome."""
        record = meters.call_record()
        try:
            with start_span(tracer, span_name) as span:
                response = answer(span, record, caller, route, operation)
        except Exception:
            record.end("failed")  # a refusal counted itself first and stays one
            raise
        record.end("completed")
        return response

    def describe_call(span: Span, caller: Caller, route: _Route) -> None:
        set_span_attributes(
            span,
            {
                "meridian.tenant": caller.tenant,
                "meridian.agent": caller.agent,
                "meridian.run_id": str(caller.run_id),
                "meridian.call_id": str(caller.call_id),
                "meridian.mode": settings.mode,
            },
        )
        if route.replay is not None:
            set_span_attributes(span, route_attributes(route.replay))

    def answer[ResponseT](
        span: Span,
        record: CallRecord,
        caller: Caller,
        route: _Route,
        operation: Operation[Any, ResponseT],
    ) -> ResponseT:
        describe_call(span, caller, route)
        decision = decide(registry, route.considered, caller.tenant, caller.agent)
        limits = decision.limits  # None exactly for an unknown tenant, refused here
        if decision.refusal is not None or limits is None:
            refuse(
                span,
                record,
                caller,
                route,
                decision.data_class,
                decision.refusal or "unknown-tenant",
            )
        record.known(caller.tenant, caller.agent)  # registry IDs from here on
        if decision.data_class is not None:
            set_span_attributes(span, {"meridian.data_class": decision.data_class})
        # The same number the ledger reserves, for either purpose.
        rate_refusal = limiter.admit(caller.tenant, limits, operation.estimate.tokens)
        if rate_refusal is not None:
            refuse_limit(
                span,
                record,
                caller,
                route,
                decision.data_class,
                rate_refusal.reason,
                retry_after=rate_refusal.retry_after_seconds,
            )
        result = walker.run(span, caller, limits, operation, decision)
        if isinstance(result, BudgetRefused):
            refuse_limit(
                span,
                record,
                caller,
                route,
                decision.data_class,
                result.reason,
                candidate=result.candidate,
            )
        if isinstance(result, Unanswered):
            # The reason is the last attempt's kind, a fixed word and never the
            # provider's text; no candidate called is "unavailable".
            record.end("failed", result.last_attempt_kind or NOT_CALLED_REASON)
            _unanswered(result)
        return result

    return app


def create_app_from_env() -> FastAPI:
    """The factory S041 runs under ``uvicorn --factory``."""
    return create_app(GatewaySettings.from_env())
