"""The runtime's client of the Model Gateway.

It sets the three ``X-Meridian-*`` headers and forwards the trace context, so
graph code neither sets headers nor knows the gateway's address (T-08).
"""

import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Literal, get_args

import httpx
from opentelemetry import propagate
from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.platform.common.http import BoundedEntityId
from meridian.platform.registry.models import DataClass

CHAT_PATH = "/v1/chat"
DATA_CLASS_HEADER = "X-Meridian-Data-Class"
# The gateway marks its content-filter 400 with this header and value. A copy:
# the runtime does not import the gateway (ADR 2), and a test keeps the two
# equal. A 400 without the mark is FastAPI's own (an undecodable body), not a
# refusal by the provider's filter.
REFUSAL_HEADER = "X-Meridian-Refusal"
REFUSAL_CONTENT_FILTER = "content-filter"
# A second header beside the refusal mark, for a completion withheld after the
# provider ran (billed: the filter withheld it, or the model's own refusal of a
# structured request), and the three headers that then name the deployment.
# Copies too. The refusal header keeps its one value, so a runtime that knows
# only that header still reads a withheld completion as a filtered call; a value
# of this header other than the fixed one is ignored.
COMPLETION_HEADER = "X-Meridian-Completion"
COMPLETION_WITHHELD = "withheld"
DEPLOYMENT_HEADER = "X-Meridian-Deployment"
PROVIDER_HEADER = "X-Meridian-Provider"
MODE_HEADER = "X-Meridian-Mode"
# How a call to the gateway ended, for the runtime's metric: the closed set of
# words a call's reason comes from. ``unreachable`` is no answer at all (a
# transport error that is not a timeout), ``refused`` a 429 or a 403,
# ``filtered`` the provider's content filter, ``error`` any other status, an
# answer outside the contract, another HTTP error (a decoding error) or an
# exception that is not HTTP's at all, ``limit`` a call the run's own limit
# stopped before it was sent. The words are the metric's: the run's failure
# word is still the one ``failure_reason`` gives.
CallOutcome = Literal["completed", "failed"]
CallReason = Literal["unreachable", "timeout", "refused", "filtered", "error", "limit"]
CALL_REASONS: frozenset[str] = frozenset(get_args(CallReason))
# Told once for each call ``chat`` is asked to make, with the reason of a failure.
CallObserver = Callable[[CallOutcome, CallReason | None], None]
REFUSED_STATUSES = frozenset({HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.FORBIDDEN})
# One model call as a whole, from the send to the last byte of the answer. The
# HTTP client's own timeouts are for each phase and each wait for bytes (the
# read is 30 s), so a reply that trickles never trips them. The gateway bounds
# all its attempts at 25 s (a test keeps this above that), so a call the gateway
# answers in time, with its own 504 included, is never cut here; 30 s is also
# the client's wait for one read, so once the headers are in a call ends at this
# deadline plus at most one read timeout.
# The clock is read once the headers are in: the phases before them have their
# own bounds (``app.GATEWAY_*_TIMEOUT_SECONDS``), and headers that trickle, each
# wait under the read timeout, are bounded by neither.
MODEL_CALL_DEADLINE_SECONDS = 30.0
# The largest reply of the gateway that is read, counted on the decoded bytes.
# The model's output is capped at 1024 tokens (a few KiB of text) and the
# largest of the 27 recorded answers is 421 bytes as a JSON entry, so 1 MiB is
# a bound no honest reply comes near; a reply past it is no usable answer.
MAX_REPLY_BYTES = 1024 * 1024


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


@dataclass(frozen=True, slots=True)
class Drafter:
    """The deployment that drafted a completion the filter withheld, as the
    gateway named it in headers; each part passed its pattern."""

    deployment: str
    provider: str
    mode: str


class ModelCallFilteredError(ModelCallError):
    """The provider's content filter refused the request or withheld the
    completion: the gateway answers 400 with the ``X-Meridian-Refusal`` header
    for that. A 400 without it is a plain ``ModelCallError``.

    ``withheld`` is true when the gateway said the provider ran and its
    completion was withheld (it was billed); ``drafter`` then holds the
    deployment it named, or none when a header was missing or outside its
    pattern. A refused prompt has neither. The message is fixed text."""

    def __init__(
        self, *, withheld: bool = False, drafter: Drafter | None = None
    ) -> None:
        super().__init__(HTTPStatus.BAD_REQUEST)
        self.withheld = withheld
        self.drafter = drafter


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
    mode: Literal["replay", "recorded", "live"]
    output: Output
    usage: Usage


class _DeadlinePassed(httpx.TimeoutException):
    """The call outlasted ``MODEL_CALL_DEADLINE_SECONDS``: a timeout like the
    transport's, so it is counted and answered as one. Private and message-less."""


class _ReplyTooLarge(Exception):
    """The reply outgrew ``MAX_REPLY_BYTES``. Private and message-less."""


class _Provenance(BaseModel):
    """The deployment headers of a withheld completion, held to the patterns of
    the registry's IDs and the three modes: all three or none is kept."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment: BoundedEntityId
    provider: BoundedEntityId
    mode: Literal["replay", "recorded", "live"]


def _drafter_of(headers: httpx.Headers) -> Drafter | None:
    try:
        named = _Provenance.model_validate(
            {
                "deployment": headers.get(DEPLOYMENT_HEADER),
                "provider": headers.get(PROVIDER_HEADER),
                "mode": headers.get(MODE_HEADER),
            }
        )
    except ValidationError:
        return None  # a value outside its pattern is dropped, never carried
    return Drafter(named.deployment, named.provider, named.mode)


@dataclass(frozen=True, slots=True)
class _Answer:
    """What the gateway said: the status, whether it marked a filtered 400 (and
    whether the completion was withheld, with the deployment named), and the
    body of a 2xx only (the body of any other status is never read)."""

    status_code: int
    filtered: bool
    payload: bytes
    withheld: bool = False
    drafter: Drafter | None = None


class ModelClient:
    """Built per run, over an injected client whose base URL is the gateway.

    ``max_calls`` bounds the calls of this client, so of one run; an attempt
    counts whether or not the gateway answers it. ``on_call`` is told once of
    each call ``chat`` is asked to make, a call the limit stops included.
    ``clock`` times the deadline of a call (``MODEL_CALL_DEADLINE_SECONDS``)."""

    def __init__(
        self,
        http: httpx.Client,
        *,
        tenant: str,
        agent: str,
        run_id: uuid.UUID,
        max_calls: int,
        on_call: CallObserver | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._max_calls = max_calls
        self._on_call = on_call
        self._clock = clock
        self._calls = 0
        self._lock = threading.Lock()
        self._headers = {
            # No content coding: a compressed reply would be counted after it
            # is decoded, and one chunk could then hold a thousand times what
            # arrived. The gateway is in the same cluster; nothing is saved.
            "Accept-Encoding": "identity",
            "X-Meridian-Tenant": tenant,
            "X-Meridian-Agent": agent,
            "X-Meridian-Run": str(run_id),
        }

    def _observe(self, outcome: CallOutcome, reason: CallReason | None = None) -> None:
        if self._on_call is not None:
            self._on_call(outcome, reason)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int | None = None,
        data_class: DataClass | None = None,
        response_schema: Mapping[str, Any] | None = None,
    ) -> ChatResult:
        """Raises ``ModelCallLimitError`` past ``max_calls``, before any send.

        ``data_class`` is the class of the content of this call; the gateway
        uses the higher of it and the tenant's. Raises
        ``ModelCallFilteredError`` when the gateway answers 400 with the
        content-filter header; any other 400 is a ``ModelCallError``.

        ``response_schema`` asks the provider for an answer of that shape. The
        gateway refuses it (403) from an agent the registry does not declare
        for it. The answer is still text, and the caller still reads it: the
        schema is not checked here."""
        with self._lock:  # a graph's parallel nodes share this client
            over_limit = self._calls >= self._max_calls
            if not over_limit:
                self._calls += 1
        if over_limit:
            self._observe("failed", "limit")
            raise ModelCallLimitError
        body: dict[str, Any] = {"messages": messages}
        if max_output_tokens is not None:
            body["max_output_tokens"] = max_output_tokens
        if response_schema is not None:
            body["response_schema"] = dict(response_schema)
        headers = dict(self._headers)
        if data_class is not None:
            headers[DATA_CLASS_HEADER] = data_class
        propagate.inject(headers)
        answer = self._exchange(body, headers)
        if answer.filtered:
            self._observe("failed", "filtered")
            raise ModelCallFilteredError(
                withheld=answer.withheld, drafter=answer.drafter
            ) from None
        if not 200 <= answer.status_code < 300:
            refused = answer.status_code in REFUSED_STATUSES
            self._observe("failed", "refused" if refused else "error")
            raise ModelCallError(answer.status_code)
        try:
            reply = _Reply.model_validate(json.loads(answer.payload))
        except (ValueError, ValidationError):
            self._observe("failed", "error")
            raise ModelCallError(0) from None
        self._observe("completed")
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

    def _exchange(self, body: dict[str, Any], headers: dict[str, str]) -> _Answer:
        """Send one request and read the answer as a stream, against the call's
        deadline. The clock is read once the headers are in and after every
        chunk, so the call ends at the deadline plus at most one read timeout;
        the response is closed on every way out, which closes a connection
        whose body was not read to its end. A failure is counted and raised as
        the HTTP client's own would be."""
        started = self._clock()
        try:
            with self._http.stream(
                "POST", CHAT_PATH, json=body, headers=headers
            ) as response:
                self._check_deadline(started)
                status = response.status_code
                filtered = (
                    status == HTTPStatus.BAD_REQUEST
                    and response.headers.get(REFUSAL_HEADER) == REFUSAL_CONTENT_FILTER
                )
                withheld = (
                    filtered
                    and response.headers.get(COMPLETION_HEADER) == COMPLETION_WITHHELD
                )
                drafter = _drafter_of(response.headers) if withheld else None
                chunks: list[bytes] = []
                size = 0
                if 200 <= status < 300:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_REPLY_BYTES:
                            raise _ReplyTooLarge
                        chunks.append(chunk)
                        self._check_deadline(started)
            return _Answer(status, filtered, b"".join(chunks), withheld, drafter)
        except _ReplyTooLarge:
            # Counted on the decoded bytes, as they come; nothing of the body
            # is kept or named, and the response is closed on the way out.
            self._observe("failed", "error")
            raise ModelCallError(0) from None
        except httpx.TimeoutException:
            # Neither the transport's message nor its cause is kept.
            self._observe("failed", "timeout")
            raise ModelCallTimeoutError from None
        except httpx.TransportError:
            self._observe("failed", "unreachable")
            raise ModelCallError(0) from None
        except httpx.HTTPError:
            # A decoding error, too many redirects: not "no answer at all".
            self._observe("failed", "error")
            raise ModelCallError(0) from None
        except Exception:
            # Not the transport's (a closed client): counted, and raised as it
            # is, for the run to fail as it would have.
            self._observe("failed", "error")
            raise

    def _check_deadline(self, started: float) -> None:
        if self._clock() - started > MODEL_CALL_DEADLINE_SECONDS:
            raise _DeadlinePassed("")
