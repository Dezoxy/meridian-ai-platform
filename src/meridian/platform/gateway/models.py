"""The gateway's wire contract: minimal internal JSON (S009 decision)."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, StringConstraints

from meridian.platform.common.wire import NoNul, WireModel
from meridian.platform.gateway.response_schema import response_schema_errors

MAX_MESSAGES = 50
MAX_CONTENT_CHARS = 20_000
DEFAULT_OUTPUT_TOKENS = 1024
# 4,096 tokens cannot be generated inside the adapter's 20 s read limit, so the
# contract promised more than a call could serve; 1,024 needs about 52 tokens a
# second, which is not yet measured against Azure.
MAX_OUTPUT_TOKENS = 1024
# A request is at most this much text (T-55): an embedding is cheap per token,
# so the bound is on inputs and characters, not on a reply.
MAX_EMBEDDING_INPUTS = 16
MAX_EMBEDDING_INPUT_CHARS = 8000
# The provider refuses an input of more than 8,191 tokens. The gateway has no
# tokenizer, but a token of a byte-level BPE is at least one byte, so an input of
# at most 8,191 UTF-8 bytes cannot pass 8,191 tokens. It also refuses inputs that
# would have fitted (Cyrillic past 4,095 characters, CJK past 2,730): accepted.
MAX_EMBEDDING_INPUT_BYTES = 8191
# One fixed sentence that names the bound and nothing of the input: the
# service's 422 copies a validator's message (T-56).
EMBEDDING_INPUT_REFUSAL = (
    f"an embedding input must not exceed {MAX_EMBEDDING_INPUT_BYTES} bytes of UTF-8"
)
# The one sentence a refused schema is answered with. The service's 422 copies a
# validator's message, so it names nothing of the schema: a name or a value in
# it would travel to a log, a trace or a reply (T-56).
RESPONSE_SCHEMA_REFUSAL = "the response schema is outside the allowed subset"


def _check_response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if response_schema_errors(schema):
        raise ValueError(RESPONSE_SCHEMA_REFUSAL)
    return schema


# Treated as read-only, like a registry ``Tool.input_schema``: the model is
# frozen, the dict inside it is not, and nothing here or below changes it.
ResponseSchema = Annotated[dict[str, Any], AfterValidator(_check_response_schema)]


class Message(WireModel):
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, NoNul] = Field(min_length=1, max_length=MAX_CONTENT_CHARS)


class ChatRequest(WireModel):
    messages: tuple[Message, ...] = Field(min_length=1, max_length=MAX_MESSAGES)
    max_output_tokens: int = Field(DEFAULT_OUTPUT_TOKENS, ge=1, le=MAX_OUTPUT_TOKENS)
    # The shape of the answer (S051); the gateway refuses it for an agent or a
    # deployment that cannot honour one.
    response_schema: ResponseSchema | None = None


class ChatOutput(WireModel):
    text: str
    finish_reason: Literal["stop", "length"]


class Usage(WireModel):
    input_tokens: int
    output_tokens: int


class ChatResponse(WireModel):
    call_id: UUID
    mode: Literal["replay", "recorded", "live"]
    deployment: str
    provider: str
    model: str
    output: ChatOutput
    usage: Usage


def _check_input_bytes(text: str) -> str:
    # surrogatepass: a lone surrogate counts as the three bytes it would take
    # and never raises, so the only message this can produce is the fixed one.
    if len(text.encode("utf-8", errors="surrogatepass")) > MAX_EMBEDDING_INPUT_BYTES:
        raise ValueError(EMBEDDING_INPUT_REFUSAL)
    return text


EmbeddingInput = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_EMBEDDING_INPUT_CHARS),
    NoNul,
    AfterValidator(_check_input_bytes),
]


class EmbeddingRequest(WireModel):
    """The texts to embed and nothing else: the caller never chooses the model
    or the number of dimensions (T-54), both come from the registry.

    An input is at most 8,000 characters and at most 8,191 bytes of UTF-8: the
    provider refuses more than 8,191 tokens, and a token is at least a byte."""

    inputs: tuple[EmbeddingInput, ...] = Field(
        min_length=1, max_length=MAX_EMBEDDING_INPUTS
    )


class EmbeddingUsage(WireModel):
    input_tokens: int


class EmbeddingResponse(WireModel):
    """One vector per input, in input order, and the registry's name for what
    made them: only vectors of one deployment are comparable (T-54)."""

    call_id: UUID
    mode: Literal["replay", "recorded", "live"]
    deployment: str
    provider: str
    model: str
    dimensions: int
    embeddings: tuple[tuple[float, ...], ...]
    usage: EmbeddingUsage
