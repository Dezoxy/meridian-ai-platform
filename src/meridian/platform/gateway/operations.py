"""What a chat request and an embedding request each ask of the walk (S045).

The walk (``walk.py``) is the same for both: routing, the tenant's windows, the
ledger, the candidate walk, the circuit breaker and the audit row do not care
what is being asked. Each purpose builds one ``Operation`` per request, which is
all that differs: the estimate the limiter admits and the ledger reserves, the
call to make on a provider, and the wire response to build from the reply of
the candidate that answered. Nothing built here touches the request's text
beyond handing it to the provider (T-56).
"""

import uuid

from meridian.platform.gateway.budget import chat_estimate, embedding_estimate
from meridian.platform.gateway.models import (
    ChatOutput,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingUsage,
    Usage,
)
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ModelProvider,
    ProviderError,
    ProviderReply,
)
from meridian.platform.gateway.settings import GatewayMode
from meridian.platform.gateway.walk import Operation
from meridian.platform.registry.models import Deployment


def chat_operation(body: ChatRequest) -> Operation[ProviderReply, ChatResponse]:
    def call(
        provider: ModelProvider, deployment: Deployment, timeout_seconds: float
    ) -> ProviderReply:
        return provider.chat(deployment, body, timeout_seconds=timeout_seconds)

    def respond(
        call_id: uuid.UUID,
        mode: GatewayMode,
        deployment: Deployment,
        reply: ProviderReply,
    ) -> ChatResponse:
        return ChatResponse(
            call_id=call_id,
            mode=mode,
            deployment=deployment.id,
            provider=deployment.provider,
            model=deployment.model,
            output=ChatOutput(text=reply.text, finish_reason=reply.finish_reason),
            usage=Usage(
                input_tokens=reply.input_tokens, output_tokens=reply.output_tokens
            ),
        )

    return Operation(chat_estimate(body), call, respond)


def _dimensions_of(deployment: Deployment) -> int:
    """The length of the vectors the registry says ``deployment`` returns."""
    if deployment.dimensions is None:
        raise ValueError(f"deployment {deployment.id} has no dimensions")
    return deployment.dimensions


def _checked(reply: EmbeddingReply, inputs: int, dimensions: int) -> EmbeddingReply:
    """The reply when it holds one vector of ``dimensions`` numbers per input;
    anything else is a bad response, whichever provider gave it (T-54)."""
    if len(reply.embeddings) != inputs or any(
        len(vector) != dimensions for vector in reply.embeddings
    ):
        raise ProviderError("bad-response")
    return reply


def embedding_operation(
    body: EmbeddingRequest,
) -> Operation[EmbeddingReply, EmbeddingResponse]:
    def call(
        provider: ModelProvider, deployment: Deployment, timeout_seconds: float
    ) -> EmbeddingReply:
        # Checked inside the call, so the walk sees a wrong answer as this
        # deployment's failure: the circuit counts it, the reservation stays
        # charged and the next candidate is tried.
        dimensions = _dimensions_of(deployment)
        reply = provider.embed(deployment, body, timeout_seconds=timeout_seconds)
        return _checked(reply, len(body.inputs), dimensions)

    def respond(
        call_id: uuid.UUID,
        mode: GatewayMode,
        deployment: Deployment,
        reply: EmbeddingReply,
    ) -> EmbeddingResponse:
        return EmbeddingResponse(
            call_id=call_id,
            mode=mode,
            deployment=deployment.id,
            provider=deployment.provider,
            model=deployment.model,
            # The registry's number: ``call`` let no reply through whose vectors
            # are of another length, so it agrees with the vectors beside it
            # (T-54).
            dimensions=_dimensions_of(deployment),
            embeddings=reply.embeddings,
            usage=EmbeddingUsage(input_tokens=reply.input_tokens),
        )

    return Operation(embedding_estimate(body), call, respond)
