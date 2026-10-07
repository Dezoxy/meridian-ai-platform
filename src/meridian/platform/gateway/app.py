"""The Model Gateway app: ``POST /v1/chat`` and ``POST /v1/embeddings``, replay
and live (S009, S010, S011, S045, S047).

One path for both modes and both purposes. A call gets its ID first, so every
audit row of the request carries it. The route of its purpose is decided next,
with the higher of the tenant's data class and the one the request's optional
``X-Meridian-Data-Class`` header names (T-11), and refused (403) and audited
when nothing is allowed or the class is ``special`` (T-13). The tenant's rate
windows come next, one set for both purposes, on the estimate of the text as
sent: a request over them is refused (429, or 413 when it alone is larger than
the tenant's token limit) before any redaction, circuit, reservation or
provider is touched (T-73). The windows are the process's own or, given the
store's address, shared through it (S066); a store that cannot be reached
refuses the call the same way (503, ``rate-store-unavailable``), never a pass.
Only a request that passes has its text redacted (T-20), whatever its class, so
nothing below sees an e-mail address, an IBAN or a card number; a request a
policy or a window refuses costs no redaction, one the budget refuses has been
redacted. Then the allowed candidates are walked in
order under one deadline (S042), each one reserved in the ledger before it is
called (QA-12). A candidate whose circuit is open, or that the deadline leaves
no time for, is skipped; a deployment's own failure moves the walk to the next
candidate; a rejected request ends it, and so does a request the provider's
content filter refuses, which the gateway answers 400 with the
``X-Meridian-Refusal`` header (T-67), with a second header and the deployment
named when the completion was withheld after the provider ran; a reservation the
tenant's budget refuses ends it too (429, or the earlier attempt's answer).
Every candidate touched leaves one audit row, except that a refusal, whether
policy's 403 or a tenant limit's 429 or 413, leaves at most one row per tenant
and reason per minute, and that row says how many refusals it stands in for, so
a flood cannot fill the log, and the count of a flood's last window, of these
refusals and of the caller check's, is written once the flood has been quiet
for two minutes, or at shutdown (T-49). The audit
write is part of the answer, so a call that cannot be recorded returns no
output (QA-05). There is no retry of one deployment: the next candidate is the
retry. The limits apply in replay mode as in live mode: replay simulates the
provider, not the gateway's controls. The gateway counts tokens, cost and calls
in its metrics. No text a caller sends and no vector it gets back is in an
audit row, a span, a metric or a log line (T-56).

A chat request may carry a response schema, the shape of the answer (S051). It
is checked against a closed subset when the body is read, so one outside it is
a 422 that names nothing of it. The route decision then refuses (403, audited,
before any redaction, limiter or reservation) an agent that does not declare
``structured_outputs`` in the registry (``schema-not-allowed``), and a request
for which no deployment the class allows declares it (``no-schema-deployment``);
a deployment that cannot honour a schema is left out of the candidates, so a
fallback never answers in free text. The schema passes redaction untouched,
counts as input tokens in the estimate, and is sent to the provider as it is.
The span says only that one was sent, as a boolean: never a word of it.
"""

import logging
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import date
from typing import Annotated, Any, NoReturn, get_args

from fastapi import FastAPI, Header, HTTPException, Request
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
    ErrorBody,
    create_service_app,
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
from meridian.platform.common.refusal_summary import write_ended_summaries
from meridian.platform.common.telemetry import set_span_attributes, start_span
from meridian.platform.common.throttle import RefusalAuditThrottle
from meridian.platform.gateway.budget import (
    Caller,
    Ledger,
    TokenEstimate,
    chat_estimate,
    embedding_estimate,
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
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ModelProvider,
    ProviderReply,
)
from meridian.platform.gateway.rate_store import limiter_from_settings
from meridian.platform.gateway.ratelimit import (
    RateLimiter,
    RateStoreUnavailable,
    TenantRateLimiter,
)
from meridian.platform.gateway.redaction import redact_chat, redact_embeddings
from meridian.platform.gateway.refusals import (
    LIMIT_ANSWERS,
    MODEL_CALL_EVENT,
    RATE_STORE_RETRY_SECONDS,
    LimitRefusalReason,
    RefusalAudit,
)
from meridian.platform.gateway.replay import ReplayProvider
from meridian.platform.gateway.resilience import CircuitBreaker
from meridian.platform.gateway.routing import RefusalReason, decide
from meridian.platform.gateway.settings import GatewayMode, GatewaySettings
from meridian.platform.gateway.startup import (
    AZURE_KIND as AZURE_KIND,  # re-exported: the tests import it from here
)
from meridian.platform.gateway.startup import (
    CHAT_PURPOSE,
    EMBEDDING_PURPOSE,
    RECORDED_KIND,
    REPLAY_KIND,
    Route,
    check_start_allowed,
    live_providers,
    provider_kinds,
    recorded_providers,
    route_for,
)
from meridian.platform.gateway.walk import (
    BudgetRefused,
    CandidateWalker,
    Operation,
    Unanswered,
    route_attributes,
    route_facts,
)
from meridian.platform.registry import load_registry
from meridian.platform.registry.models import (
    DataClass,
    Deployment,
    Service,
    TenantLimits,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "model-gateway"

HTTP_BAD_REQUEST = 400
# The mark of the gateway's own 400: FastAPI answers 400 too, for a body it
# cannot decode, and the runtime must not read that as a content-filter refusal.
# ``runtime/model_client.py`` keeps a copy of every constant of this block (a
# test compares them). ``X-Meridian-Refusal: content-filter`` is on every
# filtered answer: the provider's filter refused the prompt, or the provider ran
# and its completion was withheld. A withheld one, billed with the reservation
# kept (the filter withheld the completion, or the model's own refusal of a
# structured request: billed either way), carries a second header of its own,
# ``X-Meridian-Completion: withheld``, and names the deployment in three more,
# each a registry ID or a closed word and never a provider's text (S069). A
# refused prompt carries none of the four. A runtime that knows only the refusal
# header still reads a withheld completion as a filtered call.
REFUSAL_HEADER = "X-Meridian-Refusal"
REFUSAL_CONTENT_FILTER = "content-filter"
COMPLETION_HEADER = "X-Meridian-Completion"
COMPLETION_WITHHELD = "withheld"
DEPLOYMENT_HEADER = "X-Meridian-Deployment"
PROVIDER_HEADER = "X-Meridian-Provider"
MODE_HEADER = "X-Meridian-Mode"
HTTP_BAD_GATEWAY = 502
HTTP_SERVICE_UNAVAILABLE = 503
HTTP_GATEWAY_TIMEOUT = 504
# One fixed text per status: the provider's own words can echo a prompt (T-18).
PROVIDER_TIMED_OUT = "the model provider did not answer in time"
PROVIDER_FAILED = "the model provider failed"
# What the gateway itself answers 400 for, with the refusal header: the
# provider's content filter refused the prompt or withheld the completion
# (T-67). A malformed request is a 422; FastAPI's own 400, for a body that is not
# UTF-8, has no such header.
PROVIDER_FILTERED = "the model provider's content filter refused the request"
# No attempt was made: every candidate was skipped.
PROVIDER_UNAVAILABLE = "the model provider is unavailable"
# Recorded mode: the recording holds no answer to this request (T-78). Fixed, so
# it names no prompt; it says which command records again.
NOT_RECORDED = "no recording answers this request; record again (make eval-record)"
# The reason on the calls counter of a request no candidate was called for.
NOT_CALLED_REASON = "unavailable"
# The route's own description of 503; the shared one names the database only.
CHAT_UNAVAILABLE = (
    "The audit log or the rate store is unavailable, or no model deployment can "
    "be tried now."
)
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
# The class of the request, when it is higher than its tenant's (T-11). FastAPI
# refuses a value that is not one of the four classes, with a 422.
DataClassHeader = Annotated[DataClass | None, Header(alias="X-Meridian-Data-Class")]


def _filtered_headers(result: Unanswered, mode: str) -> dict[str, str]:
    """The headers of the content filter's 400: the refusal mark always; for a
    withheld completion also its own mark and the deployment that drafted it:
    its registry ID, its provider's registry ID and the gateway's mode."""
    deployment = result.withheld_by
    if deployment is None:
        return {REFUSAL_HEADER: REFUSAL_CONTENT_FILTER}
    return {
        REFUSAL_HEADER: REFUSAL_CONTENT_FILTER,
        COMPLETION_HEADER: COMPLETION_WITHHELD,
        DEPLOYMENT_HEADER: deployment.id,
        PROVIDER_HEADER: deployment.provider,
        MODE_HEADER: mode,
    }


def _unanswered(result: Unanswered, mode: str) -> NoReturn:
    """The status when no candidate answered: the last attempt's kind (400 for a
    content filter, 504 for a timeout, 502 for the rest), or 503 when none was
    called. Never a word of the provider's (T-18)."""
    if result.attempts == 0:
        raise HTTPException(
            status_code=HTTP_SERVICE_UNAVAILABLE, detail=PROVIDER_UNAVAILABLE
        )
    if result.last_attempt_kind == "filtered":
        raise HTTPException(  # the one 400 the gateway gives, marked by its header
            status_code=HTTP_BAD_REQUEST,
            detail=PROVIDER_FILTERED,
            headers=_filtered_headers(result, mode),
        )
    if result.last_attempt_kind == "not-recorded":
        raise HTTPException(status_code=HTTP_BAD_GATEWAY, detail=NOT_RECORDED)
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
    limiter: RateLimiter | None = None,
) -> FastAPI:
    """Build the app; raise when the registry fails to load or the mode,
    environment and providers are not an allowed combination.

    ``providers`` is keyed by provider kind (``replay``, ``recorded``,
    ``azure-openai``); a test injects fakes. Without it the app builds the
    replay provider, in recorded mode the recorded one too (from
    ``settings.recordings``) and, in live mode, the Azure one. A provider is
    asked for the method of the purpose of the request only, so a fake with
    ``chat`` alone serves chat, and nothing checks at the start that it has
    ``embed``. ``clock`` times the circuit
    breaker (one per app), the tenants' rate windows (unless ``limiter`` is
    given) and each request's deadline; ``today`` is the UTC day the ledger
    charges to. A test injects fakes of both. ``limiter`` keeps the tenants'
    rate windows (S066): without one they are the process's own, on ``clock``;
    ``create_app_from_env`` passes the shared store's when its address is set. A
    limiter that raises ``RateStoreUnavailable`` refuses the call (503). A
    ``meter_provider`` is its caller's to shut down; without one the app builds
    its own.
    """
    check_start_allowed(settings, providers)
    registry = load_registry(settings.registry_dir)
    policy = caller_policy(settings.identity_prefix, SERVICE_NAME, registry)
    routes = {
        purpose: route_for(settings, registry, purpose)
        for purpose in (CHAT_PURPOSE, EMBEDDING_PURPOSE)
    }
    considered = tuple(d for route in routes.values() for d in route.considered)
    kinds = provider_kinds(registry, considered)
    if settings.mode == "live":
        for purpose, route in routes.items():
            for kind in (REPLAY_KIND, RECORDED_KIND):
                if any(kinds[d.id] == kind for d in route.considered):
                    # The registry checks refuse it too; this is the second line.
                    raise SettingsError(
                        f"the {purpose} route has a {kind} candidate in live mode"
                    )
    close: Callable[[], None] | None = None
    if providers is None:
        if settings.mode == "replay":
            providers = {REPLAY_KIND: ReplayProvider()}
        elif settings.mode == "recorded":
            providers = recorded_providers(settings)
        else:
            providers, close = live_providers(settings, considered, kinds)
    if not set(kinds.values()) <= providers.keys():
        raise SettingsError("a deployment is routed to a provider kind with no adapter")
    owns_meter_provider = meter_provider is None
    app_meter_provider = (
        make_meter_provider(SERVICE_NAME) if meter_provider is None else meter_provider
    )

    def audit(event: str, outcome: str, **fields: object) -> None:
        write_audit(
            settings.database_url,
            AuditEvent(service=SERVICE_NAME, event=event, outcome=outcome, **fields),
        )

    refusals = RefusalAudit(RefusalAuditThrottle(clock=clock), audit)
    # The caller check's own throttle: its keys are a service and a word, not a
    # tenant and a reason. Its floods are summarised by the same writer.
    caller_throttle = RefusalAuditThrottle(clock=clock)

    def write_ended(*, everything: bool = False) -> None:
        """The counts of the floods that ended, of model-call refusals and of
        the caller check's; neither writer raises an ``Exception``."""
        refusals.write_ended(everything=everything)
        write_ended_summaries(
            caller_throttle, audit, MODEL_CALL_EVENT, everything=everything
        )

    def close_all() -> None:
        """Only what this function built: an injected provider is its caller's.
        The counts of refusal floods are written first; with the database
        unreachable they are lost with the process, and the shutdown goes on."""
        try:
            write_ended(everything=True)
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
    limiter = limiter if limiter is not None else TenantRateLimiter(clock=clock)

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

    # Outside the app's other middleware: a call that comes from no known
    # service is refused before its body is read (S055).
    install_caller_check(
        app,
        policy,
        audited_refusals(
            caller_throttle,
            lambda reason, who, carried: audit(
                "model.call",
                "refused",
                reason=reason,
                reference=who,
                suppressed=carried,
            ),
        ),
    )

    def refuse(
        span: Span,
        record: CallRecord,
        caller: Caller,
        route: Route,
        data_class: str | None,
        refusal: RefusalReason,
    ) -> NoReturn:
        """Answer 403 for a request policy refuses. The throttle key is the
        reason and, unless the tenant is unknown, the tenant: a header's value
        is caller-chosen and never becomes a key; never the agent."""
        set_span_attributes(span, {"meridian.refusal": refusal})
        record.end("refused", refusal)
        throttle_tenant = None if refusal == "unknown-tenant" else caller.tenant
        facts = route_facts(route.replay, data_class) | {"purpose": route.purpose}
        refusals.record(caller, throttle_tenant, refusal, facts)
        raise HTTPException(status_code=403, detail=REFUSED)

    def refuse_name(
        span: Span,
        record: CallRecord,
        caller: Caller,
        route: Route,
        calling: Service | None,
    ) -> NoReturn:
        """Answer 403 for a caller that names a tenant or an agent it may not
        (S055), or that the scope does not name at all while the app has a
        policy. The throttle key is the reason and the tenant when the registry
        holds it: a header's value is caller-chosen and never becomes a key. The
        row names the calling service in ``reference``, none without one."""
        set_span_attributes(span, {"meridian.refusal": NAME_REFUSAL_REASON})
        record.end("refused", NAME_REFUSAL_REASON)
        known = caller.tenant if registry.tenant(caller.tenant) else None
        who = None if calling is None else calling.id
        facts = {"reference": who, "purpose": route.purpose}
        refusals.record(caller, known, NAME_REFUSAL_REASON, facts)
        raise HTTPException(status_code=403, detail=REFUSED)

    def refuse_limit(
        span: Span,
        record: CallRecord,
        caller: Caller,
        route: Route,
        data_class: str | None,
        reason: LimitRefusalReason,
        *,
        retry_after: int | None = None,
        candidate: Deployment | None = None,
        store_failure: RateStoreUnavailable | None = None,
    ) -> NoReturn:
        """Answer a request a tenant limit refuses. Every refusal sets the span
        attribute and is counted; the audit row is written for the first of its
        tenant and reason in the window only (T-49). Both parts of that key are
        registry IDs, because routing passed. In replay mode every row names
        the replay deployment of the request's purpose (T-39). ``store_failure``
        is why the shared store gave no answer: it is logged once per window,
        with the row, and not once per request (the exception's text holds no
        address)."""
        status, detail = LIMIT_ANSWERS[reason]
        set_span_attributes(span, {"meridian.refusal": reason})
        record.end("refused", reason)
        facts = route_facts(
            candidate if candidate is not None else route.replay, data_class
        ) | {"purpose": route.purpose}
        row_written = refusals.record(caller, caller.tenant, reason, facts)
        if row_written and store_failure is not None:
            logger.error(
                "the rate store is unavailable (%s): %s",
                type(store_failure).__name__,
                store_failure,
            )
        headers = None if retry_after is None else {"Retry-After": str(retry_after)}
        raise HTTPException(status_code=status, detail=detail, headers=headers)

    def admit_or_refuse(
        span: Span,
        record: CallRecord,
        caller: Caller,
        route: Route,
        data_class: str | None,
        limits: TenantLimits,
        tokens: int,
    ) -> None:
        """Return when the tenant's windows admit the request; otherwise answer
        it and never return: 429 or 413 for a window, and 503 when the store of
        the windows gives no answer. No limit is known then, so no call is made:
        never a pass and never the process's own windows (S066)."""
        try:
            rate_refusal = limiter.admit(caller.tenant, limits, tokens)
        except RateStoreUnavailable as error:
            refuse_limit(
                span,
                record,
                caller,
                route,
                data_class,
                "rate-store-unavailable",
                retry_after=RATE_STORE_RETRY_SECONDS,
                store_failure=error,
            )
        if rate_refusal is not None:
            refuse_limit(
                span,
                record,
                caller,
                route,
                data_class,
                rate_refusal.reason,
                retry_after=rate_refusal.retry_after_seconds,
            )

    def route_responses(unavailable: str, too_large: str) -> dict[int | str, Any]:
        responses = error_responses(401, 403, 413, 429, 500, 502, 503, 504)
        responses[HTTP_SERVICE_UNAVAILABLE]["description"] = unavailable
        responses[HTTP_PAYLOAD_TOO_LARGE]["description"] = too_large
        # The gateway's own 400, the one with the refusal header: the shared
        # descriptions have none.
        responses[HTTP_BAD_REQUEST] = {
            "model": ErrorBody,
            "description": PROVIDER_FILTERED.capitalize() + ".",
            "headers": {
                REFUSAL_HEADER: {
                    "description": (
                        "Marks this 400 as the content filter's refusal; a 400 "
                        "without it is not. The one value, on every filtered "
                        "answer, whether the prompt was refused or a "
                        "completion was withheld."
                    ),
                    "schema": {"type": "string", "enum": [REFUSAL_CONTENT_FILTER]},
                },
                COMPLETION_HEADER: {
                    "description": (
                        "Beside the refusal header, only when the provider ran "
                        "and the completion was withheld, billed either way: "
                        "a completion the filter withheld, or the model's own "
                        "refusal of a structured request. A refused prompt "
                        "(nothing billed) does not carry it."
                    ),
                    "schema": {"type": "string", "enum": [COMPLETION_WITHHELD]},
                },
                DEPLOYMENT_HEADER: {
                    "description": (
                        "With the completion header only: the registry ID of "
                        "the deployment that drafted the completion."
                    ),
                    "schema": {"type": "string"},
                },
                PROVIDER_HEADER: {
                    "description": (
                        "With the completion header only: the registry ID of "
                        "that deployment's provider."
                    ),
                    "schema": {"type": "string"},
                },
                MODE_HEADER: {
                    "description": (
                        "With the completion header only: the gateway's mode."
                    ),
                    "schema": {
                        "type": "string",
                        "enum": list(get_args(GatewayMode)),
                    },
                },
            },
        }
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
        request: Request,
        data_class: DataClassHeader = None,
    ) -> ChatResponse:
        # The call ID exists before anything can refuse the request.
        caller = Caller(uuid.uuid4(), tenant_id, agent_id, run_id)

        def build(
            estimate: TokenEstimate,
        ) -> tuple[Operation[ProviderReply, ChatResponse], int]:
            redacted, redactions = redact_chat(body)
            return chat_operation(redacted, estimate), redactions

        return handle(
            "gateway.chat",
            routes[CHAT_PURPOSE],
            caller,
            lambda: chat_estimate(body),
            build,
            data_class,
            wants_schema=body.response_schema is not None,
            calling=caller_service(request),
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
        request: Request,
        data_class: DataClassHeader = None,
    ) -> EmbeddingResponse:
        caller = Caller(uuid.uuid4(), tenant_id, agent_id, run_id)

        def build(
            estimate: TokenEstimate,
        ) -> tuple[Operation[EmbeddingReply, EmbeddingResponse], int]:
            redacted, redactions = redact_embeddings(body)
            return embedding_operation(redacted, estimate), redactions

        return handle(
            "gateway.embeddings",
            routes[EMBEDDING_PURPOSE],
            caller,
            lambda: embedding_estimate(body),
            build,
            data_class,
            calling=caller_service(request),
        )

    def handle[ResponseT](
        span_name: str,
        route: Route,
        caller: Caller,
        estimate: Callable[[], TokenEstimate],
        build: Callable[[TokenEstimate], tuple[Operation[Any, ResponseT], int]],
        requested: DataClass | None,
        wants_schema: bool = False,
        calling: Service | None = None,
    ) -> ResponseT:
        """One request of either purpose: a span of its own, counted once by its
        outcome. ``estimate`` sizes the text as sent; ``build`` redacts it and
        builds the operation, once the rate windows admit it; ``answer`` runs both.
        ``requested`` is the class the request's header names, if any;
        ``wants_schema`` is true for a chat request with a response schema;
        ``calling`` is the service the caller check let through, none where
        the app has no check (and then a policy refuses every name). The count
        of a flood that ended is written first, with this request of any
        tenant."""
        write_ended()
        record = meters.call_record()
        try:
            with start_span(tracer, span_name) as span:
                response = answer(
                    span,
                    record,
                    caller,
                    route,
                    estimate,
                    build,
                    requested,
                    wants_schema,
                    calling,
                )
        except Exception:
            record.end("failed")  # a refusal counted itself first and stays one
            raise
        record.end("completed")
        return response

    def describe_call(span: Span, caller: Caller, route: Route) -> None:
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
        route: Route,
        estimate: Callable[[], TokenEstimate],
        build: Callable[[TokenEstimate], tuple[Operation[Any, ResponseT], int]],
        requested: DataClass | None,
        wants_schema: bool,
        calling: Service | None,
    ) -> ResponseT:
        describe_call(span, caller, route)
        # What the calling service may name comes from the registry, not from
        # the headers it sends (S055). With a policy and no caller in the scope
        # nothing may be named.
        if not caller_may_name(policy, calling, caller.tenant, caller.agent):
            refuse_name(span, record, caller, route, calling)
        decision = decide(
            registry,
            route.considered,
            caller.tenant,
            caller.agent,
            requested,
            wants_schema=wants_schema,
        )
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
            # The class used, never the tenant's alone: a header may raise it.
            set_span_attributes(span, {"meridian.data_class": decision.data_class})
        if wants_schema:  # that one was sent, never the schema (S051)
            set_span_attributes(span, {"meridian.response_schema": True})
        # Redaction costs time in proportion to the text, so a request a policy
        # or a rate window refuses is never redacted (T-20, T-73); one the budget
        # refuses has been. The limiter and the ledger see one number, the size
        # of the text as sent, and nothing else below sees the original.
        sized = estimate()
        admit_or_refuse(
            span, record, caller, route, decision.data_class, limits, sized.tokens
        )
        operation, redactions = build(sized)
        if redactions > 0:  # a count only: never a kind, never a value
            set_span_attributes(span, {"meridian.redactions": redactions})
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
            _unanswered(result, settings.mode)
        return result

    return app


def create_app_from_env() -> FastAPI:
    """The factory S041 runs under ``uvicorn --factory``."""
    install_log_redaction()
    configure_logging(SERVICE_NAME)
    settings = GatewaySettings.from_env()
    return create_app(settings, limiter=limiter_from_settings(settings))
