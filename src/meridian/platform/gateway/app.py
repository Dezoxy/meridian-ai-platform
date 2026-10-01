"""The Model Gateway app: ``POST /v1/chat`` in replay mode (S009).

Policy first, then the provider, then the audit event; the audit write is part
of the answer, so a call that cannot be recorded returns no output (QA-05).
"""

import uuid
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from opentelemetry.sdk.trace import TracerProvider

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
from meridian.platform.gateway.replay import replay_chat
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.registry import Registry, load_registry
from meridian.platform.registry.models import Deployment

SERVICE_NAME = "model-gateway"
CHAT_PURPOSE = "chat"
REPLAY_ENVIRONMENTS = frozenset({"test", "ci", "kind"})


def _check_start_allowed(settings: GatewaySettings) -> None:
    if settings.mode == "live":
        raise SettingsError("live mode is refused: no live provider adapter until S010")
    if settings.environment not in REPLAY_ENVIRONMENTS:
        raise SettingsError(
            "replay mode is refused outside the test, ci and kind environments (T-39)"
        )


def request_allowed(
    registry: Registry, deployment: Deployment, tenant_id: str, agent_id: str
) -> bool:
    """The tenant exists, may run the agent, and its data class may reach the
    deployment (its data classes and its residency label)."""
    tenant = registry.tenant(tenant_id)
    if tenant is None or not registry.tenant_may_run(tenant_id, agent_id):
        return False
    data_class = registry.data_class(tenant.data_class)
    return (
        data_class is not None
        and tenant.data_class in deployment.data_classes
        and deployment.residency in data_class.residency
    )


def create_app(
    settings: GatewaySettings, *, tracer_provider: TracerProvider | None = None
) -> FastAPI:
    """Build the app; raise when the registry fails to load or the mode and
    environment are not an allowed pair."""
    _check_start_allowed(settings)
    registry = load_registry(settings.registry_dir)
    deployment = registry.replay_deployment(CHAT_PURPOSE)
    if deployment is None:
        raise SettingsError("the registry has no replay deployment for chat")
    service = create_service_app(
        title="Meridian Model Gateway",
        description="Every model call goes through here (ADR 3, hard rule 4).",
        service_name=SERVICE_NAME,
        tracer_name="meridian.gateway",
        max_body_bytes=GATEWAY_BODY_LIMIT_BYTES,
        tracer_provider=tracer_provider,
    )
    app, tracer = service.app, service.tracer
    provenance = {
        "meridian.deployment": deployment.id,
        "meridian.provider": deployment.provider,
        "meridian.mode": "replay",
        "gen_ai.request.model": deployment.model,
    }

    def audit(event: str, outcome: str, **fields: object) -> None:
        write_audit(
            settings.database_url,
            AuditEvent(service=SERVICE_NAME, event=event, outcome=outcome, **fields),
        )

    @app.post(
        "/v1/chat",
        tags=["chat"],
        summary="Answer a chat request (replay mode: simulated, no model is called).",
        responses=error_responses(403, 413, 500, 503),
    )
    def chat(
        body: ChatRequest,
        tenant_id: Annotated[BoundedEntityId, Header(alias="X-Meridian-Tenant")],
        agent_id: Annotated[BoundedEntityId, Header(alias="X-Meridian-Agent")],
        run_id: Annotated[uuid.UUID, Header(alias="X-Meridian-Run")],
    ) -> ChatResponse:
        with start_span(tracer, "gateway.chat") as span:
            # Where the call goes is known before policy decides (T-39), so a
            # refusal says it too.
            set_span_attributes(
                span,
                {
                    "meridian.tenant": tenant_id,
                    "meridian.agent": agent_id,
                    "meridian.run_id": str(run_id),
                }
                | provenance,
            )
            who = {"tenant": tenant_id, "agent": agent_id, "run_id": run_id}
            where = {
                "deployment": deployment.id,
                "provider": deployment.provider,
                "model": deployment.model,
            }
            if not request_allowed(registry, deployment, tenant_id, agent_id):
                audit("model.call", "refused", **who, **where)
                raise HTTPException(status_code=403, detail=REFUSED)
            reply = replay_chat(body.messages)
            audit(
                "model.call",
                "completed",
                **who,
                **where,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
            )
            set_span_attributes(
                span,
                {
                    "gen_ai.usage.input_tokens": reply.input_tokens,
                    "gen_ai.usage.output_tokens": reply.output_tokens,
                },
            )
            return ChatResponse(
                call_id=uuid.uuid4(),
                mode="replay",
                deployment=deployment.id,
                provider=deployment.provider,
                model=deployment.model,
                output=ChatOutput(text=reply.text),
                usage=Usage(
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                ),
            )

    return app


def create_app_from_env() -> FastAPI:
    """The factory S041 runs under ``uvicorn --factory``."""
    return create_app(GatewaySettings.from_env())
