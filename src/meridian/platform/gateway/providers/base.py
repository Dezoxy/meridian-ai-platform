"""The provider protocols and their error: no SDK is imported here."""

from dataclasses import dataclass
from typing import Literal, Protocol

from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest
from meridian.platform.registry.models import Deployment

# ``filtered``: the provider's content filter refused the prompt (a 400) or
# withheld the completion (no status). It says nothing of the deployment.
# ``not-recorded``: no recording holds an answer to the request (S050, T-75);
# nothing was sent, and it says nothing of the deployment either.
ProviderErrorKind = Literal[
    "timeout",
    "rate-limited",
    "unavailable",
    "rejected",
    "filtered",
    "auth",
    "bad-response",
    "not-recorded",
]


class ProviderError(Exception):
    """A provider call failed. It carries the kind and the HTTP status only,
    never the provider's message or body: that can echo a prompt (T-18, T-03).

    ``sent`` is ``False`` only when the failure is known to have happened before
    a request left the gateway (no credential, no connection), so nothing can
    have been billed. It is not part of ``args`` or ``__str__``."""

    def __init__(
        self,
        kind: ProviderErrorKind,
        status_code: int | None = None,
        *,
        sent: bool = True,
    ) -> None:
        super().__init__(kind, status_code)
        self.kind = kind
        self.status_code = status_code
        self.sent = sent

    def __str__(self) -> str:
        suffix = "" if self.status_code is None else f" (HTTP {self.status_code})"
        return f"provider call failed: {self.kind}{suffix}"


class Reply(Protocol):
    """What the walk reads off any provider's answer, for the ledger, the span
    and the audit row; each purpose's reply adds its own content."""

    @property
    def model(self) -> str: ...

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...


@dataclass(frozen=True, slots=True)
class ProviderReply:
    text: str
    finish_reason: Literal["stop", "length"]
    model: str  # what the provider says it ran, e.g. gpt-4o-2024-11-20
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class EmbeddingReply:
    embeddings: tuple[tuple[float, ...], ...]  # one vector per input, in order
    model: str  # what the provider says it ran, e.g. text-embedding-3-large
    input_tokens: int

    @property
    def output_tokens(self) -> int:
        """An embedding has no output."""
        return 0


class ChatProvider(Protocol):
    """``timeout_seconds`` is the budget of the whole attempt, connecting and
    answering."""

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply: ...


class EmbeddingProvider(Protocol):
    """The same budget as ``ChatProvider``. A reply holds one vector of exactly
    the deployment's ``dimensions`` per input, in input order, or the call
    raises ``ProviderError`` (T-54)."""

    def embed(
        self,
        deployment: Deployment,
        request: EmbeddingRequest,
        *,
        timeout_seconds: float,
    ) -> EmbeddingReply: ...


class ModelProvider(ChatProvider, EmbeddingProvider, Protocol):
    """What the gateway holds for one provider kind: both purposes."""
