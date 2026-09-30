"""The gateway's wire contract: minimal internal JSON (S009 decision)."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from meridian.platform.common.wire import NoNul, WireModel

MAX_MESSAGES = 50
MAX_CONTENT_CHARS = 20_000
DEFAULT_OUTPUT_TOKENS = 1024
MAX_OUTPUT_TOKENS = 4096


class Message(WireModel):
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, NoNul] = Field(min_length=1, max_length=MAX_CONTENT_CHARS)


class ChatRequest(WireModel):
    messages: tuple[Message, ...] = Field(min_length=1, max_length=MAX_MESSAGES)
    max_output_tokens: int = Field(DEFAULT_OUTPUT_TOKENS, ge=1, le=MAX_OUTPUT_TOKENS)


class ChatOutput(WireModel):
    text: str


class Usage(WireModel):
    input_tokens: int
    output_tokens: int


class ChatResponse(WireModel):
    call_id: UUID
    mode: Literal["replay"]
    deployment: str
    provider: str
    model: str
    output: ChatOutput
    usage: Usage
