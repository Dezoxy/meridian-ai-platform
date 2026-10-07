"""The ingestion's client of the Model Gateway's ``POST /v1/embeddings`` (S012).

Like the runtime's ``ModelClient`` it sets the three ``X-Meridian-*`` headers
and forwards the trace context, and like it, it does not import the gateway's
models: the two services share a wire contract, not code. It also refuses an
answer that is not that contract, because a vector the table would accept but
the search could not compare is worse than no vector (T-54): a vector the
database cannot compare (``usable_vector``) is refused before it is stored.
"""

import json
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated

import httpx
from opentelemetry import propagate
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meridian.platform.knowledge_mcp.store import usable_vector

EMBEDDINGS_PATH = "/v1/embeddings"
RETRY_AFTER_HEADER = "Retry-After"
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVICE_UNAVAILABLE = 503
# The lengths of the columns of knowledge.chunks that name the model.
NAME_MAX_CHARACTERS = 128

# The gateway's four 503s and the one fixed word each has (S073, decision 16).
# The keys are the gateway's own ``detail`` texts, copied: this module does not
# import the gateway's application (that pulls the web framework in), so a test
# compares the copy with the gateway's constants and with every 503 its sources
# write. The words are those of the gateway's reasons where it has one.
GATEWAY_503_WORDS = MappingProxyType(
    {
        "the rate store is unavailable": "rate-store-unavailable",
        "the database is unavailable": "database-unavailable",
        "the audit log is unavailable": "audit-unavailable",
        "the model provider is unavailable": "provider-unavailable",
    }
)
UNKNOWN_GATEWAY_WORD = "unknown"
# A 503 body longer than this is not parsed: the longest of the four texts as
# the gateway writes it (``{"detail":"the model provider is unavailable"}``) is
# 46 bytes. The client reads a reply without a size bound (a known gap, see the
# service's README); this keeps the matching from parsing what it should not.
MAX_503_BODY_BYTES = 256


class EmbeddingCallError(Exception):
    """The gateway did not answer an embedding call with a usable 2xx.

    Carries the status code and, for a 429, the wait the gateway asked for. The
    body is never kept (it could echo an input) and no input text is in the
    message. ``status_code`` is 0 when there was no usable answer: the call
    failed in transit or the answer was not the contract. For a 503,
    ``gateway_word`` is the fixed word of the gateway's text, or ``unknown``;
    it is None for any other status. The word is a key of the closed set above,
    never a part of the body, and is not in the message.
    """

    def __init__(
        self,
        status_code: int,
        retry_after_seconds: float | None = None,
        gateway_word: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.gateway_word = gateway_word
        super().__init__(
            f"model gateway answered {status_code}"
            if status_code
            else "model gateway gave no usable answer"
        )


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """One vector per text, in the order the texts were sent, and the registry's
    name for what made them: only vectors of one deployment are comparable."""

    deployment: str
    model: str
    dimensions: int
    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int


Name = Annotated[str, Field(min_length=1, max_length=NAME_MAX_CHARACTERS)]


class _Reply(BaseModel):
    """Only the fields of the gateway's reply that ``EmbeddingBatch`` needs.

    Unknown fields are ignored, so a newer gateway never breaks the client. The
    types are strict: a boolean is not a vector component, and NaN or infinity
    is no component either.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    class Usage(BaseModel):
        model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

        input_tokens: Annotated[int, Field(ge=0)]

    deployment: Name
    model: Name
    dimensions: Annotated[int, Field(ge=1)]
    embeddings: tuple[tuple[Annotated[float, Field(allow_inf_nan=False)], ...], ...]
    usage: Usage


def _retry_after(response: httpx.Response) -> float | None:
    """The wait a 429 asked for, when it is a finite number of seconds that is
    not negative; an HTTP date or anything else is no wait at all."""
    if response.status_code != HTTP_TOO_MANY_REQUESTS:
        return None
    try:
        seconds = float(response.headers.get(RETRY_AFTER_HEADER, ""))
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _gateway_word(response: httpx.Response) -> str | None:
    """Which of the gateway's 503s this is: the word of the text its ``detail``
    equals, ``unknown`` for anything else, None for another status. Reads only
    what httpx has already buffered, and parses it only when it is no longer than
    a 503 of the gateway's is. The text is compared by equality and is not kept."""
    if response.status_code != HTTP_SERVICE_UNAVAILABLE:
        return None
    body = response.content
    if len(body) > MAX_503_BODY_BYTES:
        return UNKNOWN_GATEWAY_WORD
    try:
        payload = json.loads(body)
    except ValueError:
        return UNKNOWN_GATEWAY_WORD
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(detail, str):
        return UNKNOWN_GATEWAY_WORD
    return GATEWAY_503_WORDS.get(detail, UNKNOWN_GATEWAY_WORD)


class EmbeddingClient:
    """Built per ingestion, over an injected client whose base URL is the
    gateway. ``run_id`` is the one every call of the ingestion carries; the
    tenant and the agent are the ones the calls go out under, readable so that
    a caller checks the registry for the very values the gateway will see."""

    def __init__(
        self, http: httpx.Client, *, tenant: str, agent: str, run_id: uuid.UUID
    ) -> None:
        self._http = http
        self._tenant = tenant
        self._agent = agent
        self.run_id = run_id
        self._headers = {
            "X-Meridian-Tenant": tenant,
            "X-Meridian-Agent": agent,
            "X-Meridian-Run": str(run_id),
        }

    @property
    def tenant(self) -> str:
        return self._tenant

    @property
    def agent(self) -> str:
        return self._agent

    def embed(
        self, texts: Sequence[str], *, timeout: httpx.Timeout | None = None
    ) -> EmbeddingBatch:
        """``timeout`` bounds this request alone; None keeps the injected
        client's own."""
        headers = dict(self._headers)
        propagate.inject(headers)
        # Left out when there is none: ``None`` would mean "no limit" to httpx,
        # and a client that is not httpx's own (Starlette's test client) warns
        # about any ``timeout`` it is given.
        options = {} if timeout is None else {"timeout": timeout}
        try:
            response = self._http.post(
                EMBEDDINGS_PATH,
                json={"inputs": list(texts)},
                headers=headers,
                **options,
            )
        except httpx.HTTPError:
            # Neither the transport's message nor its cause is kept.
            raise EmbeddingCallError(0) from None
        if not 200 <= response.status_code < 300:
            raise EmbeddingCallError(
                response.status_code, _retry_after(response), _gateway_word(response)
            )
        try:
            reply = _Reply.model_validate_json(response.content)
        except ValidationError:
            raise EmbeddingCallError(0) from None
        vectors = reply.embeddings
        if (
            len(vectors) != len(texts)
            or any(len(vector) != reply.dimensions for vector in vectors)
            or not all(usable_vector(vector) for vector in vectors)
        ):
            raise EmbeddingCallError(0)
        return EmbeddingBatch(
            deployment=reply.deployment,
            model=reply.model,
            dimensions=reply.dimensions,
            vectors=vectors,
            input_tokens=reply.usage.input_tokens,
        )
