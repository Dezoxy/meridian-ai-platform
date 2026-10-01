"""The Model Gateway app: ``POST /v1/chat``, replay and live (S009, S010, S011).

One path for both modes. A call gets its ID first, so every audit row of the
request carries it. Then the route is decided and refused (403) and audited
when nothing is allowed. The tenant's rate windows come next: a request over
them is refused (429, or 413 when it alone is larger than the tenant's token
limit) before any circuit, reservation or provider is touched. Then the allowed
candidates are walked in order under one deadline (S042), each one reserved in
the ledger before it is called (QA-12). A candidate whose circuit is open, or
that the deadline leaves no time for, is skipped; a deployment's own failure
moves the walk to the next candidate; a rejected request ends it; a reservation
the tenant's budget refuses ends it too (429, or the earlier attempt's answer).
Every candidate touched leaves one audit row, except that a refusal, whether
policy's 403 or a tenant limit's 429 or 413, leaves at most one row per tenant
and reason per minute, and that row says how many refusals it stands in for, so
a flood cannot fill the log (T-49). The audit write is part of the answer, so a
call that cannot be recorded returns no output (QA-05). There is no retry of
one deployment: the next candidate is the retry. The limits apply in replay
mode as in live mode: replay simulates the provider, not the gateway's
controls. The gateway counts tokens, cost and calls in its metrics.
"""

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Annotated, NoReturn

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
from meridian.platform.gateway.budget import (
    BudgetRefusalReason,
    Caller,
    Ledger,
    reservation_tokens,
    utc_today,
)
from meridian.platform.gateway.meters import CallRecord, GatewayMeters
from meridian.platform.gateway.models import ChatRequest, ChatResponse
from meridian.platform.gateway.providers.base import ChatProvider, ProviderError
from meridian.platform.gateway.ratelimit import (
    RateRefusalReason,
    RefusalAuditThrottle,
    TenantRateLimiter,
)
from meridian.platform.gateway.replay import ReplayProvider
from meridian.platform.gateway.resilience import CircuitBreaker
from meridian.platform.gateway.routing import RefusalReason, decide
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.gateway.walk import (
    BudgetRefused,
    CandidateWalker,
    Unanswered,
    caller_fields,
    route_attributes,
    route_facts,
)
from meridian.platform.registry import Registry, load_registry
from meridian.platform.registry.models import Deployment

SERVICE_NAME = "model-gateway"
CHAT_PURPOSE = "chat"
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
    settings: GatewaySettings, providers: Mapping[str, ChatProvider] | None
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


def _considered(
    settings: GatewaySettings, registry: Registry
) -> tuple[Deployment, ...]:
    """The deployments a request may use before policy narrows them: replay's
    one deployment, or the chat route's candidates in order."""
    if settings.mode == "replay":
        deployment = registry.replay_deployment(CHAT_PURPOSE)
        if deployment is None:
            raise SettingsError("the registry has no replay deployment for chat")
        return (deployment,)
    route = registry.route(CHAT_PURPOSE)
    if route is None:
        return ()
    found = tuple(registry.deployment(name) for name in route.candidates)
    if any(d is None for d in found):
        raise SettingsError("the chat route names a deployment the registry lacks")
    return tuple(d for d in found if d is not None)


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
    """What ``AzureOpenAIProvider.chat`` would otherwise raise a ``ValueError``
    for at the first request."""
    for deployment in considered:
        if kinds[deployment.id] != AZURE_KIND:
            continue
        if deployment.terraform_key is None or deployment.deployment_name is None:
            raise SettingsError(
                f"deployment {deployment.id} has no terraform_key or deployment_name"
            )
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
) -> tuple[dict[str, ChatProvider], Callable[[], None]]:
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
    providers: Mapping[str, ChatProvider] | None = None,
    clock: Callable[[], float] = time.monotonic,
    today: Callable[[], date] = utc_today,
) -> FastAPI:
    """Build the app; raise when the registry fails to load or the mode,
    environment and providers are not an allowed combination.

    ``providers`` is keyed by provider kind (``replay``, ``azure-openai``); a
    test injects fakes. Without it the app builds the replay provider and, in
    live mode, the Azure one. ``clock`` times the circuit breaker (one per app),
    the tenants' rate windows and each request's deadline; ``today`` is the UTC
    day the ledger charges to. A test injects fakes of both. A ``meter_provider``
    is its caller's to shut down; without one the app builds its own.
    """
    _check_start_allowed(settings, providers)
    registry = load_registry(settings.registry_dir)
    considered = _considered(settings, registry)
    kinds = _provider_kinds(registry, considered)
    if settings.mode == "live" and REPLAY_KIND in kinds.values():
        # The registry checks refuse it too; this is the second line.
        raise SettingsError("the chat route has a replay candidate in live mode")
    close: Callable[[], None] | None = None
    if providers is None:
        if settings.mode == "replay":
            providers = {REPLAY_KIND: ReplayProvider()}
        else:
            providers, close = _live_providers(settings, considered, kinds)
    if not set(kinds.values()) <= providers.keys():
        raise SettingsError("a deployment is routed to a provider kind with no adapter")
    # In replay mode where the call goes is known before policy decides, so a
    # refusal says it too (T-39).
    replay = considered[0] if settings.mode == "replay" else None
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
        once the row is written, so a write that fails loses nothing: the next
        refusal is due and counts this one."""
        suppressed = refusal_throttle.due(throttle_tenant, reason)
        if suppressed is None:
            return
        audit(
            "model.call",
            "refused",
            **caller_fields(caller),
            **facts,
            reason=reason,
            suppressed=suppressed,
        )
        refusal_throttle.mark(throttle_tenant, reason)

    def refuse(
        span: Span,
        record: CallRecord,
        caller: Caller,
        data_class: str | None,
        refusal: RefusalReason,
    ) -> NoReturn:
        """Answer 403 for a request policy refuses. The throttle key is the
        reason and, unless the tenant is unknown, the tenant: a header's value
        is caller-chosen and never becomes a key; never the agent."""
        set_span_attributes(span, {"meridian.refusal": refusal})
        record.end("refused", refusal)
        throttle_tenant = None if refusal == "unknown-tenant" else caller.tenant
        audit_refusal(caller, throttle_tenant, refusal, route_facts(replay, data_class))
        raise HTTPException(status_code=403, detail=REFUSED)

    def refuse_limit(
        span: Span,
        record: CallRecord,
        caller: Caller,
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
        the replay deployment (T-39)."""
        status, detail = LIMIT_ANSWERS[reason]
        set_span_attributes(span, {"meridian.refusal": reason})
        record.end("refused", reason)
        facts = route_facts(candidate if candidate is not None else replay, data_class)
        audit_refusal(caller, caller.tenant, reason, facts)
        headers = None if retry_after is None else {"Retry-After": str(retry_after)}
        raise HTTPException(status_code=status, detail=detail, headers=headers)

    chat_responses = error_responses(403, 413, 429, 500, 502, 503, 504)
    chat_responses[HTTP_SERVICE_UNAVAILABLE]["description"] = CHAT_UNAVAILABLE
    chat_responses[HTTP_PAYLOAD_TOO_LARGE]["description"] = CHAT_TOO_LARGE

    @app.post(
        "/v1/chat",
        tags=["chat"],
        summary="Answer a chat request (replay simulates, live calls a model).",
        responses=chat_responses,
    )
    def chat(
        body: ChatRequest,
        tenant_id: Annotated[BoundedEntityId, Header(alias="X-Meridian-Tenant")],
        agent_id: Annotated[BoundedEntityId, Header(alias="X-Meridian-Agent")],
        run_id: Annotated[uuid.UUID, Header(alias="X-Meridian-Run")],
    ) -> ChatResponse:
        # The call ID exists before anything can refuse the request.
        caller = Caller(uuid.uuid4(), tenant_id, agent_id, run_id)
        record = meters.call_record()
        try:
            with start_span(tracer, "gateway.chat") as span:
                response = answer(span, record, caller, body)
        except Exception:
            record.end("failed")  # a refusal counted itself first and stays one
            raise
        record.end("completed")
        return response

    def describe_call(span: Span, caller: Caller) -> None:
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
        if replay is not None:
            set_span_attributes(span, route_attributes(replay))

    def answer(
        span: Span, record: CallRecord, caller: Caller, body: ChatRequest
    ) -> ChatResponse:
        describe_call(span, caller)
        decision = decide(registry, considered, caller.tenant, caller.agent)
        limits = decision.limits  # None exactly for an unknown tenant, refused here
        if decision.refusal is not None or limits is None:
            refuse(
                span,
                record,
                caller,
                decision.data_class,
                decision.refusal or "unknown-tenant",
            )
        record.known(caller.tenant, caller.agent)  # registry IDs from here on
        if decision.data_class is not None:
            set_span_attributes(span, {"meridian.data_class": decision.data_class})
        rate_refusal = limiter.admit(caller.tenant, limits, reservation_tokens(body))
        if rate_refusal is not None:
            refuse_limit(
                span,
                record,
                caller,
                decision.data_class,
                rate_refusal.reason,
                retry_after=rate_refusal.retry_after_seconds,
            )
        result = walker.run(span, caller, limits, body, decision)
        if isinstance(result, BudgetRefused):
            refuse_limit(
                span,
                record,
                caller,
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
