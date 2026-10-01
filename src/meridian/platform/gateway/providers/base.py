"""The provider protocol and its error: no SDK is imported here."""

from dataclasses import dataclass
from typing import Literal, Protocol

from meridian.platform.gateway.models import ChatRequest
from meridian.platform.registry.models import Deployment

ProviderErrorKind = Literal[
    "timeout", "rate-limited", "unavailable", "rejected", "auth", "bad-response"
]


class ProviderError(Exception):
    """A provider call failed. It carries the kind and the HTTP status only,
    never the provider's message or body: that can echo a prompt (T-18, T-03)."""

    def __init__(self, kind: ProviderErrorKind, status_code: int | None = None) -> None:
        super().__init__(kind, status_code)
        self.kind = kind
        self.status_code = status_code

    def __str__(self) -> str:
        suffix = "" if self.status_code is None else f" (HTTP {self.status_code})"
        return f"provider call failed: {self.kind}{suffix}"


@dataclass(frozen=True, slots=True)
class ProviderReply:
    text: str
    finish_reason: Literal["stop", "length"]
    model: str  # what the provider says it ran, e.g. gpt-4o-2024-11-20
    input_tokens: int
    output_tokens: int


class ChatProvider(Protocol):
    def chat(self, deployment: Deployment, request: ChatRequest) -> ProviderReply: ...
