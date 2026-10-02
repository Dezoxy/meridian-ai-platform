"""The runtime's client of the Model Gateway.

It sets the three ``X-Meridian-*`` headers and forwards the trace context, so
graph code neither sets headers nor knows the gateway's address (T-08).
"""

import threading
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from opentelemetry import propagate
from pydantic import BaseModel, ConfigDict, ValidationError

CHAT_PATH = "/v1/chat"


class ModelCallError(Exception):
    """The gateway did not answer a chat call with a usable 2xx.

    Carries the status code only; the body is not kept, because it could echo
    a prompt. ``status_code`` is 0 when there was no usable answer: the call
    failed in transit or the answer was not the contract.
    """

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(
            f"model gateway answered {status_code}"
            if status_code
            else "model gateway gave no usable answer"
        )


class ModelCallTimeoutError(ModelCallError):
    """The gateway did not answer in time; the runtime answers 504 for it."""

    def __init__(self) -> None:
        super().__init__(0)
        self.args = ("model gateway did not answer in time",)


class ModelCallLimitError(ModelCallError):
    """The run has made its allowed number of model calls; nothing was sent."""

    def __init__(self) -> None:
        super().__init__(0)
        self.args = ("model call limit of the run reached",)


@dataclass(frozen=True, slots=True)
class ChatResult:
    text: str
    deployment: str
    provider: str
    model: str
    mode: str
    input_tokens: int
    output_tokens: int
    finish_reason: Literal["stop", "length"]


class _Reply(BaseModel):
    """Only the fields of the gateway's reply that ``ChatResult`` needs.

    Unknown fields are ignored, so a newer gateway (a field added later) never
    breaks the runtime. An unknown mode is refused: the stored proposal records
    it as provenance (T-39). So is a missing or unknown finish reason: a graph
    must know whether a reply was cut short. The runtime does not
    import the gateway's models: the two services share a wire contract, not
    code.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    class Output(BaseModel):
        model_config = ConfigDict(extra="ignore", frozen=True)

        text: str
        finish_reason: Literal["stop", "length"]

    class Usage(BaseModel):
        model_config = ConfigDict(extra="ignore", frozen=True)

        input_tokens: int
        output_tokens: int

    deployment: str
    provider: str
    model: str
    mode: Literal["replay", "live"]
    output: Output
    usage: Usage


class ModelClient:
    """Built per run, over an injected client whose base URL is the gateway.

    ``max_calls`` bounds the calls of this client, so of one run; an attempt
    counts whether or not the gateway answers it."""

    def __init__(
        self,
        http: httpx.Client,
        *,
        tenant: str,
        agent: str,
        run_id: uuid.UUID,
        max_calls: int,
    ) -> None:
        self._http = http
        self._max_calls = max_calls
        self._calls = 0
        self._lock = threading.Lock()
        self._headers = {
            "X-Meridian-Tenant": tenant,
            "X-Meridian-Agent": agent,
            "X-Meridian-Run": str(run_id),
        }

    def chat(
        self, messages: list[dict[str, str]], *, max_output_tokens: int | None = None
    ) -> ChatResult:
        """Raises ``ModelCallLimitError`` past ``max_calls``, before any send."""
        with self._lock:  # a graph's parallel nodes share this client
            if self._calls >= self._max_calls:
                raise ModelCallLimitError
            self._calls += 1
        body: dict[str, Any] = {"messages": messages}
        if max_output_tokens is not None:
            body["max_output_tokens"] = max_output_tokens
        headers = dict(self._headers)
        propagate.inject(headers)
        try:
            response = self._http.post(CHAT_PATH, json=body, headers=headers)
        except httpx.TimeoutException:
            # Neither the transport's message nor its cause is kept.
            raise ModelCallTimeoutError from None
        except httpx.HTTPError:
            raise ModelCallError(0) from None
        if not 200 <= response.status_code < 300:
            raise ModelCallError(response.status_code)
        try:
            reply = _Reply.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ModelCallError(0) from None
        return ChatResult(
            text=reply.output.text,
            deployment=reply.deployment,
            provider=reply.provider,
            model=reply.model,
            mode=reply.mode,
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            finish_reason=reply.output.finish_reason,
        )
