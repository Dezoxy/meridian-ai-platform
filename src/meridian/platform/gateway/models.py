"""The gateway's wire contract: minimal internal JSON (S009 decision)."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from meridian.platform.common.wire import NoNul, WireModel

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


class Message(WireModel):
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, NoNul] = Field(min_length=1, max_length=MAX_CONTENT_CHARS)


class ChatRequest(WireModel):
    messages: tuple[Message, ...] = Field(min_length=1, max_length=MAX_MESSAGES)
    max_output_tokens: int = Field(DEFAULT_OUTPUT_TOKENS, ge=1, le=MAX_OUTPUT_TOKENS)


class ChatOutput(WireModel):
    text: str
    finish_reason: Literal["stop", "length"]


class Usage(WireModel):
    input_tokens: int
    output_tokens: int


class ChatResponse(WireModel):
    call_id: UUID
    mode: Literal["replay", "live"]
    deployment: str
    provider: str
    model: str
    output: ChatOutput
    usage: Usage


EmbeddingInput = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_EMBEDDING_INPUT_CHARS),
    NoNul,
]


class EmbeddingRequest(WireModel):
    """The texts to embed and nothing else: the caller never chooses the model
    or the number of dimensions (T-54), both come from the registry."""

    inputs: tuple[EmbeddingInput, ...] = Field(
        min_length=1, max_length=MAX_EMBEDDING_INPUTS
    )


class EmbeddingUsage(WireModel):
    input_tokens: int


class EmbeddingResponse(WireModel):
    """One vector per input, in input order, and the registry's name for what
    made them: only vectors of one deployment are comparable (T-54)."""

    call_id: UUID
    mode: Literal["replay", "live"]
    deployment: str
    provider: str
    model: str
    dimensions: int
    embeddings: tuple[tuple[float, ...], ...]
    usage: EmbeddingUsage
