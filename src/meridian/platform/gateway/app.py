"""The Model Gateway app: ``POST /v1/chat``, replay and live (S009, S010).

One path for both modes: decide the route, refuse and audit when nothing is
allowed, call the first allowed candidate through its provider, audit the
outcome. The audit write is part of the answer, so a call that cannot be
recorded returns no output (QA-05). No retry, breaker or fallback yet (S042).
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, NoReturn

from fastapi import FastAPI, Header, HTTPException
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span

from meridian.platform.common.audit import AuditEvent, write_audit
from meridian.platform.common.env import SettingsError
from meridian.platform.common.http import (
    GATEWAY_BODY_LIMIT_BYTES,
    REFUSED,
    BoundedEntityId,
    create_service_app,
    error_responses,
)
from meridian.platform.common.telemetry import set_span_attributes, start_span
from meridian.platform.gateway.models import (
    ChatOutput,
    ChatRequest,
    ChatResponse,
    Usage,
)
from meridian.platform.gateway.providers.base import ChatProvider, ProviderError
from meridian.platform.gateway.replay import ReplayProvider
from meridian.platform.gateway.routing import RefusalReason, decide
from meridian.platform.gateway.settings import GatewaySettings
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
HTTP_GATEWAY_TIMEOUT = 504
# One fixed text per status: the provider's own words can echo a prompt (T-18).
PROVIDER_TIMED_OUT = "the model provider did not answer in time"
PROVIDER_FAILED = "the model provider failed"
# The audit reason and span error type of a provider call that raised something
# other than a ProviderError.
INTERNAL_REASON = "internal"


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


def _route_facts(
    deployment: Deployment | None, data_class: str | None
) -> dict[str, str | None]:
    """The audit columns that say where a call went and why it could go there."""
    facts: dict[str, str | None] = {"data_class": data_class}
    if deployment is not None:
        facts |= {
            "deployment": deployment.id,
            "provider": deployment.provider,
            "model": deployment.model,
            "sku": deployment.sku,
            "region": deployment.region,
            "residency": deployment.residency,
        }
    return facts


def _route_attributes(
    deployment: Deployment, data_class: str | None = None
) -> dict[str, str]:
    """The span attributes for the deployment a call goes to. A replay
    deployment has no sku or region; before policy decides there is no data
    class to say."""
    attributes = {
        "meridian.deployment": deployment.id,
        "meridian.provider": deployment.provider,
        "gen_ai.request.model": deployment.model,
        "meridian.residency": deployment.residency,
    }
    if data_class is not None:
        attributes["meridian.data_class"] = data_class
    if deployment.sku is not None:
        attributes["meridian.sku"] = deployment.sku
    if deployment.region is not None:
        attributes["meridian.region"] = deployment.region
    return attributes


def create_app(
    settings: GatewaySettings,
    *,
    tracer_provider: TracerProvider | None = None,
    providers: Mapping[str, ChatProvider] | None = None,
) -> FastAPI:
    """Build the app; raise when the registry fails to load or the mode,
    environment and providers are not an allowed combination.

    ``providers`` is keyed by provider kind (``replay``, ``azure-openai``); a
    test injects fakes. Without it the app builds the replay provider and, in
    live mode, the Azure one.
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
    service = create_service_app(
        title="Meridian Model Gateway",
        description="Every model call goes through here (ADR 3, hard rule 4).",
        service_name=SERVICE_NAME,
        tracer_name="meridian.gateway",
        max_body_bytes=GATEWAY_BODY_LIMIT_BYTES,
        tracer_provider=tracer_provider,
        # Only what this function built: an injected provider is its caller's.
        close=close,
    )
    app, tracer = service.app, service.tracer

    def audit(event: str, outcome: str, **fields: object) -> None:
        write_audit(
            settings.database_url,
            AuditEvent(service=SERVICE_NAME, event=event, outcome=outcome, **fields),
        )

    def refuse(
        span: Span,
        who: dict[str, object],
        data_class: str | None,
        refusal: RefusalReason,
    ) -> NoReturn:
        set_span_attributes(span, {"meridian.refusal": refusal})
        audit(
            "model.call",
            "refused",
            **who,
            **_route_facts(replay, data_class),
            reason=refusal,
        )
        raise HTTPException(status_code=403, detail=REFUSED)

    def fail(
        span: Span, who: dict[str, object], facts: dict[str, str | None], kind: str
    ) -> NoReturn:
        set_span_attributes(span, {"error.type": kind})
        audit("model.call", "failed", **who, **facts, reason=kind)
        timed_out = kind == "timeout"
        raise HTTPException(
            status_code=HTTP_GATEWAY_TIMEOUT if timed_out else HTTP_BAD_GATEWAY,
            detail=PROVIDER_TIMED_OUT if timed_out else PROVIDER_FAILED,
        )

    @app.post(
        "/v1/chat",
        tags=["chat"],
        summary="Answer a chat request (replay simulates, live calls a model).",
        responses=error_responses(403, 413, 500, 502, 503, 504),
    )
    def chat(
        body: ChatRequest,
        tenant_id: Annotated[BoundedEntityId, Header(alias="X-Meridian-Tenant")],
        agent_id: Annotated[BoundedEntityId, Header(alias="X-Meridian-Agent")],
        run_id: Annotated[uuid.UUID, Header(alias="X-Meridian-Run")],
    ) -> ChatResponse:
        with start_span(tracer, "gateway.chat") as span:
            set_span_attributes(
                span,
                {
                    "meridian.tenant": tenant_id,
                    "meridian.agent": agent_id,
                    "meridian.run_id": str(run_id),
                    "meridian.mode": settings.mode,
                },
            )
            if replay is not None:
                set_span_attributes(span, _route_attributes(replay))
            who = {"tenant": tenant_id, "agent": agent_id, "run_id": run_id}
            decision = decide(registry, considered, tenant_id, agent_id)
            if decision.refusal is not None:
                refuse(span, who, decision.data_class, decision.refusal)
            deployment = decision.candidates[0]
            facts = _route_facts(deployment, decision.data_class)
            attributes = _route_attributes(deployment, decision.data_class)
            set_span_attributes(span, attributes)
            try:
                reply = providers[kinds[deployment.id]].chat(deployment, body)
            except ProviderError as error:
                fail(span, who, facts, error.kind)
            except Exception:
                # Whatever it was, the call may have reached the provider, so it
                # leaves a row. Nothing of the exception is kept (T-03, T-18);
                # it is re-raised for the unexpected-error middleware's 500.
                set_span_attributes(span, {"error.type": INTERNAL_REASON})
                audit("model.call", "failed", **who, **facts, reason=INTERNAL_REASON)
                raise
            response = ChatResponse(
                call_id=uuid.uuid4(),
                mode=settings.mode,
                deployment=deployment.id,
                provider=deployment.provider,
                model=deployment.model,
                output=ChatOutput(text=reply.text, finish_reason=reply.finish_reason),
                usage=Usage(
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                ),
            )
            set_span_attributes(
                span,
                {
                    "gen_ai.usage.input_tokens": reply.input_tokens,
                    "gen_ai.usage.output_tokens": reply.output_tokens,
                    "gen_ai.response.model": reply.model,
                },
            )
            # The last thing that can fail: nothing after the audit write may.
            audit(
                "model.call",
                "completed",
                **who,
                **facts,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
            )
            return response

    return app


def create_app_from_env() -> FastAPI:
    """The factory S041 runs under ``uvicorn --factory``."""
    return create_app(GatewaySettings.from_env())
