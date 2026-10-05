"""What a chat request and an embedding request each ask of the walk (S045).

The walk (``walk.py``) is the same for both: routing, the tenant's windows, the
ledger, the candidate walk, the circuit breaker and the audit row do not care
what is being asked. Each purpose builds one ``Operation`` per request, which is
all that differs: the estimate the limiter admits and the ledger reserves, the
call to make on a provider, and the wire response to build from the reply of
the candidate that answered. The estimate is of the request as sent and the
request handed to the provider is the redacted one (T-73). A reply whose token
counts are out of bounds for the request is a bad response of that candidate
(S058). Nothing built here touches the request's text beyond handing it to the
provider (T-56).
"""

import uuid

from meridian.platform.gateway.budget import TokenEstimate
from meridian.platform.gateway.models import (
    MAX_OUTPUT_TOKENS,
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
    Reply,
)
from meridian.platform.gateway.settings import GatewayMode
from meridian.platform.gateway.walk import Operation
from meridian.platform.registry.models import Deployment

# A token of a byte-level BPE is at least one byte, and the estimate is the
# UTF-8 bytes over three, so a count over three times the estimate cannot be of
# this request. Four leaves room for what redaction's placeholders add and for
# the provider's framing of a message and a schema.
INPUT_TOKEN_FACTOR = 4


def _bounded[ReplyT: Reply](reply: ReplyT, estimate: TokenEstimate) -> ReplyT:
    """The reply when its counts are credible for this request; anything else
    is a bad response, whichever provider gave it (S058).

    The input count may not exceed ``INPUT_TOKEN_FACTOR`` times the estimate.
    The output count is held to the wire's cap, ``MAX_OUTPUT_TOKENS``, and not
    to the request's own ``max_output_tokens``: the replay provider ignores
    that cap (its text is fixed), so a request's cap is not a bound on a
    reply."""
    if not (
        0 <= reply.input_tokens <= INPUT_TOKEN_FACTOR * estimate.input_tokens
        and 0 <= reply.output_tokens <= MAX_OUTPUT_TOKENS
    ):
        raise ProviderError("bad-response")
    return reply


def chat_operation(
    body: ChatRequest, estimate: TokenEstimate
) -> Operation[ProviderReply, ChatResponse]:
    def call(
        provider: ModelProvider, deployment: Deployment, timeout_seconds: float
    ) -> ProviderReply:
        # Bounded inside the call, so the walk sees a count out of bounds as
        # this deployment's failure, as it sees a wrong vector length below.
        reply = provider.chat(deployment, body, timeout_seconds=timeout_seconds)
        return _bounded(reply, estimate)

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

    return Operation(estimate, call, respond)


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
    body: EmbeddingRequest, estimate: TokenEstimate
) -> Operation[EmbeddingReply, EmbeddingResponse]:
    def call(
        provider: ModelProvider, deployment: Deployment, timeout_seconds: float
    ) -> EmbeddingReply:
        # Checked inside the call, so the walk sees a wrong answer as this
        # deployment's failure: the circuit counts it, the reservation stays
        # charged and the next candidate is tried.
        dimensions = _dimensions_of(deployment)
        reply = provider.embed(deployment, body, timeout_seconds=timeout_seconds)
        return _bounded(_checked(reply, len(body.inputs), dimensions), estimate)

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

    return Operation(estimate, call, respond)
