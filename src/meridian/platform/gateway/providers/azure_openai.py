"""The Azure OpenAI chat and embeddings adapter (hard rule 4, T-19).

The import contracts in ``pyproject.toml`` make this the only module in
``src/`` that imports ``openai`` or any ``azure`` package.

One gateway attempt is one HTTP request: the SDK's own retries are off
(``max_retries=0``), so the audit row is true; the next candidate is the
gateway's walk (S042), never the SDK's.
The credential is a bearer token from a named source, never an API key: key
authentication is off on the account (S007). A ``ProviderError`` carries a kind,
an HTTP status and whether a request left the gateway, and no cause or context,
and tests pin that a provider's message, which can echo a prompt, appears in
none of them (T-18). Chat and embeddings share the one mapping from the SDK's
exceptions to that error.

Azure's content filter is told apart from any other refusal of a request: a 400
whose error code is exactly ``content_filter``, or a completion whose
``finish_reason`` is ``content_filter``, is ``filtered``. Only the result of that
comparison is kept (T-67).

An embeddings answer is trusted only when it holds exactly one vector of the
registry's length per input, in index order, of finite numbers (T-54); anything
else is a bad response.

IMPLEMENTED against a mocked transport; the opt-in live test is what shows the
service accepts the request.
"""

import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx
import openai
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential
from openai import AzureOpenAI
from openai.types import CompletionUsage, CreateEmbeddingResponse, Embedding
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.create_embedding_response import Usage as EmbeddingUsage

from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ProviderError,
    ProviderErrorKind,
    ProviderReply,
)
from meridian.platform.registry.models import Deployment

API_VERSION = "2024-10-21"  # the version S007's smoke test uses
# The client's default, and the most any phase of one request may take. The
# gateway passes each attempt the time left under its call deadline (S042);
# ``chat`` fits connect and the other phases inside that budget.
CONNECT_TIMEOUT_SECONDS = 5.0
PROVIDER_TIMEOUT_SECONDS = 20.0
# The variables the SDK reads, which can add headers, switch on its logging or
# redirect the client; a live gateway starts without them.
SDK_ENVIRONMENT_PREFIXES = ("OPENAI_", "AZURE_OPENAI_")
# The model string goes on a span, so it must look like a model name.
MODEL_NAME = re.compile(r"[A-Za-z0-9._:-]{1,128}")
AZURE_IDENTITY_LOGGER = "azure.identity"
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"  # noqa: S105  # a scope
TOKEN_REFRESH_MARGIN_SECONDS = 300

STATUS_KINDS: Mapping[int, ProviderErrorKind] = {
    400: "rejected",
    # Azure answers 404 for a deployment that does not exist, which no prompt causes.
    404: "unavailable",
    408: "timeout",
    422: "rejected",
    401: "auth",
    403: "auth",
    429: "rate-limited",
}
CLIENT_ERRORS = range(400, 500)
# Azure answers 400 with this error code for a prompt its content filter
# refused; every other 400 stays ``rejected``. A completion the filter withheld
# comes as a 200 whose ``finish_reason`` is this word.
BAD_REQUEST = 400
CONTENT_FILTER_CODE = "content_filter"


def kind_of_status(status: int) -> ProviderErrorKind:
    """The table first; any other 4xx is the request's fault (T-45), so it ends
    the walk and counts for no circuit; everything else is the service's."""
    explicit = STATUS_KINDS.get(status)
    if explicit is not None:
        return explicit
    return "rejected" if status in CLIENT_ERRORS else "unavailable"


# The kind, the status and whether a request left the gateway.
Failure = tuple[ProviderErrorKind, int | None, bool]


def _now() -> float:
    return time.time()


def refuse_sdk_environment(environ: Mapping[str, str] = os.environ) -> None:
    """Raise ``ValueError`` naming the first variable (in sorted order) the SDK
    would read, never its value."""
    for name in sorted(environ):
        if name.startswith(SDK_ENVIRONMENT_PREFIXES):
            raise ValueError(
                f"{name} is set, and the OpenAI SDK reads it: unset it and start again"
            )


def check_token(token_provider: Callable[[], str]) -> None:
    """Call the provider once; raise ``ProviderError("auth")`` when Azure's
    credential fails. Nothing of the failure is kept: it can name the account."""
    failed = False
    try:
        token_provider()
    except AzureError:
        failed = True  # raised below, so the credential's error is no context
    if failed:
        raise ProviderError("auth", sent=False)


@dataclass(frozen=True, slots=True)
class _CachedToken:
    token: str
    expires_on: float


def _silence_azure_identity_log() -> None:
    """On a failed login ``azure.identity`` logs the CLI's own error text, which
    can name the account: give the logger a handler and stop it propagating."""
    logger = logging.getLogger(AZURE_IDENTITY_LOGGER)
    if not any(isinstance(h, logging.NullHandler) for h in logger.handlers):
        logger.addHandler(logging.NullHandler())
    logger.propagate = False


def azure_cli_token_provider(tenant_id: str | None) -> Callable[[], str]:
    """A token source backed by the developer's ``az login`` (a laptop only).

    ``az`` is a subprocess, so the token is reused until shortly before it
    expires; the lock lets one caller run ``az`` while the others wait.
    """
    _silence_azure_identity_log()
    credential = AzureCliCredential(tenant_id=tenant_id or "")
    lock = threading.Lock()
    cached = _CachedToken("", 0.0)

    def token() -> str:
        nonlocal cached
        with lock:
            if _now() >= cached.expires_on - TOKEN_REFRESH_MARGIN_SECONDS:
                fresh = credential.get_token(TOKEN_SCOPE)
                cached = _CachedToken(fresh.token, float(fresh.expires_on))
            return cached.token

    return token


class AzureOpenAIProvider:
    def __init__(
        self,
        endpoints: Mapping[str, str],
        token_provider: Callable[[], str],
        *,
        http_client: httpx.Client | openai.DefaultHttpxClient | None = None,
        timeout_seconds: float = PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        """``endpoints`` maps a Terraform location key (``sdc`` in
        ``sdc/gpt-4o``) to that account's address.

        No API key is passed: an explicit token provider stops the SDK reading
        ``AZURE_OPENAI_API_KEY`` or ``OPENAI_API_KEY``, so a stray key in the
        environment cannot become an ``api-key`` header. Without an injected
        ``http_client`` the adapter builds one on the SDK's own client type
        with redirects and ``trust_env`` (proxy variables, netrc) off; every
        location shares it. ``close`` closes the clients.
        """
        timeout = openai.Timeout(timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS)
        if http_client is None:
            http_client = openai.DefaultHttpxClient(
                follow_redirects=False, trust_env=False, timeout=timeout
            )
        self._clients = {
            location: AzureOpenAI(
                azure_endpoint=endpoint,
                api_version=API_VERSION,
                azure_ad_token_provider=token_provider,
                max_retries=0,
                timeout=timeout,
                http_client=http_client,
            )
            for location, endpoint in endpoints.items()
        }

    def close(self) -> None:
        for client in self._clients.values():
            client.close()

    def _client_for(self, deployment: Deployment) -> AzureOpenAI:
        if deployment.terraform_key is None:
            raise ValueError(f"deployment {deployment.id} has no terraform_key")
        location = deployment.terraform_key.partition("/")[0]
        client = self._clients.get(location)
        if client is None:
            raise ValueError(
                f"deployment {deployment.id} has no endpoint for location {location}"
            )
        return client

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        """One chat completion within ``timeout_seconds``, the budget of the whole
        attempt; raise ``ProviderError`` (kind and status only) for anything the
        provider does wrong."""
        name = deployment.deployment_name
        if name is None:
            raise ValueError(f"deployment {deployment.id} has no deployment_name")
        client = self._client_for(deployment)
        timeout = _attempt_timeout(timeout_seconds)
        completion = _call_sdk(
            lambda: client.chat.completions.create(
                model=name,
                messages=[
                    {"role": m.role, "content": m.content} for m in request.messages
                ],
                # S007's smoke test sends max_tokens to this API version; the
                # opt-in live test is the arbiter.
                max_tokens=request.max_output_tokens,
                n=1,
                timeout=timeout,
            )
        )
        return _reply_from(completion)

    def embed(
        self,
        deployment: Deployment,
        request: EmbeddingRequest,
        *,
        timeout_seconds: float,
    ) -> EmbeddingReply:
        """One embeddings call within ``timeout_seconds``, with the deployment's
        ``dimensions`` (never the caller's) and plain floats back, so the answer
        needs no decoding; raise ``ProviderError`` for anything the provider
        does wrong, including an answer whose count, size or numbers are off."""
        name, dimensions = deployment.deployment_name, deployment.dimensions
        if name is None:
            raise ValueError(f"deployment {deployment.id} has no deployment_name")
        if dimensions is None:
            raise ValueError(f"deployment {deployment.id} has no dimensions")
        client = self._client_for(deployment)
        timeout = _attempt_timeout(timeout_seconds)
        response = _call_sdk(
            lambda: client.embeddings.create(
                model=name,
                input=list(request.inputs),
                dimensions=dimensions,
                encoding_format="float",
                timeout=timeout,
            )
        )
        return _embedding_reply(
            response, inputs=len(request.inputs), dimensions=dimensions
        )


def _attempt_timeout(timeout_seconds: float) -> openai.Timeout:
    """The SDK's timeouts for one attempt of ``timeout_seconds``.

    Connect gets at most half the budget and the other phases the rest, so
    connect plus the answer stay inside it: 25 s gives 5 s and 20 s, 12 s gives
    5 s and 7 s, 3 s gives 1.5 s and 1.5 s. Writing the request and waiting for
    a pooled connection are phases of their own, the read limit is between
    bytes, and the token source runs before the request: none is inside the
    budget, so the runtime's own 30 s is the last line.
    """
    connect = min(CONNECT_TIMEOUT_SECONDS, timeout_seconds / 2)
    rest = min(PROVIDER_TIMEOUT_SECONDS, timeout_seconds - connect)
    return openai.Timeout(rest, connect=connect)


def _call_sdk[T](create: Callable[[], T]) -> T:
    """Run one SDK request: its result, or a ``ProviderError`` (kind, status and
    whether the request left, nothing else) for anything the provider or the
    credential does wrong. Chat and embeddings map their failures here, once."""
    failure: Failure
    try:
        return create()
    # The provider's exception is mapped to (kind, status) and dropped inside the
    # arm. Raising from the arm would leave it in __context__ even with ``from
    # None``, and its message can echo a prompt. The SDK raises its error from
    # the httpx one, which says whether the request left: a connect timeout or a
    # connect error is a request that never did, so nothing can have been
    # billed. The cause is read in the arm and only the answer is kept.
    except openai.APITimeoutError as error:  # before APIConnectionError
        sent = not isinstance(error.__cause__, httpx.ConnectTimeout)
        failure = ("timeout", None, sent)
    except openai.APIConnectionError as error:
        sent = not isinstance(error.__cause__, httpx.ConnectError)
        failure = ("unavailable", None, sent)
    except openai.APIStatusError as error:
        status = error.status_code
        # Only the comparison's result is kept: the code and the body can echo
        # the prompt, and the SDK reads the code from the body's own ``error``.
        refused_by_filter = status == BAD_REQUEST and error.code == CONTENT_FILTER_CODE
        kind = "filtered" if refused_by_filter else kind_of_status(status)
        failure = (kind, status, True)
    except openai.APIResponseValidationError:
        failure = ("bad-response", None, True)
    except json.JSONDecodeError:
        # A 200 that says it is JSON and is not: the answer is wrong.
        failure = ("bad-response", None, True)
    except OverflowError:
        # The arm covers the whole SDK call, so any OverflowError raised inside
        # it is reported as the provider's bad response, whatever raised it. The
        # one expected: the SDK turns a whole number in a vector into a float
        # and raises for one that does not fit.
        failure = ("bad-response", None, True)
    except AzureError:  # the token provider failed, before any request
        failure = ("auth", None, False)
    except openai.OpenAIError:
        failure = ("unavailable", None, True)
    kind, status_code, sent = failure
    raise ProviderError(kind, status_code, sent=sent) from None


def _is_count(value: object) -> bool:
    """A token count: an int that is not a bool, and not negative."""
    return type(value) is int and value >= 0


def _reply_from(completion: ChatCompletion) -> ProviderReply:
    """Read the first choice; anything missing or of the wrong type is a bad
    response.

    The SDK builds the completion without validating it, so a 200 with a wrong
    shape arrives as a ``ChatCompletion`` whose members are not what the type
    says. Every member read is checked; the net is for one that slips through.
    """
    reply: ProviderReply | None = None
    try:
        reply = _read(completion)
    except (AttributeError, TypeError):
        reply = None  # raised below, so the failure is no context
    if reply is None:
        raise ProviderError("bad-response")
    return reply


def _read(completion: ChatCompletion) -> ProviderReply:
    # A 200 whose body is not JSON comes back from the SDK as a plain string.
    if not isinstance(completion, ChatCompletion):
        raise ProviderError("bad-response")
    choices, usage, model = completion.choices, completion.usage, completion.model
    if not isinstance(choices, list) or not choices:
        raise ProviderError("bad-response")
    choice = choices[0]
    if not isinstance(choice, Choice) or not isinstance(
        choice.message, ChatCompletionMessage
    ):
        raise ProviderError("bad-response")
    if (
        not isinstance(usage, CompletionUsage)
        or not _is_count(usage.prompt_tokens)
        or not _is_count(usage.completion_tokens)
        or not isinstance(model, str)
        or MODEL_NAME.fullmatch(model) is None
    ):
        raise ProviderError("bad-response")
    match choice.finish_reason:
        case "content_filter":
            raise ProviderError("filtered")
        case "stop" | "length" as finish_reason:
            pass
        case _:
            raise ProviderError("bad-response")
    text = choice.message.content
    if not isinstance(text, str):
        raise ProviderError("bad-response")
    return ProviderReply(
        text=text,
        finish_reason=finish_reason,
        model=model,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
    )


def _embedding_reply(
    response: CreateEmbeddingResponse, *, inputs: int, dimensions: int
) -> EmbeddingReply:
    """Read the vectors; anything missing, miscounted, mis-sized or of the wrong
    type is a bad response (T-54).

    As for chat, the SDK builds the response without validating it, so every
    member read is checked; the net is for one that slips through, and for an
    int too large to be a float, which a reader called past the SDK can meet.
    """
    reply: EmbeddingReply | None = None
    try:
        reply = _read_embeddings(response, inputs, dimensions)
    except (AttributeError, TypeError, OverflowError):
        reply = None  # raised below, so the failure is no context
    if reply is None:
        raise ProviderError("bad-response")
    return reply


def _is_number(value: object) -> bool:
    """An int or a float, never a bool, and finite."""
    return (type(value) is int or type(value) is float) and math.isfinite(value)


def _indexed_vector(item: object, dimensions: int) -> tuple[int, tuple[float, ...]]:
    """The index of one item and its vector as floats, or a bad response."""
    if not isinstance(item, Embedding) or type(item.index) is not int:
        raise ProviderError("bad-response")
    vector = item.embedding
    if (
        not isinstance(vector, list)
        or len(vector) != dimensions
        or not all(_is_number(x) for x in vector)
    ):
        raise ProviderError("bad-response")
    return item.index, tuple(float(x) for x in vector)


def _read_embeddings(
    response: CreateEmbeddingResponse, inputs: int, dimensions: int
) -> EmbeddingReply:
    # A 200 whose body is not JSON comes back from the SDK as a plain string.
    if not isinstance(response, CreateEmbeddingResponse):
        raise ProviderError("bad-response")
    data, usage, model = response.data, response.usage, response.model
    if (
        not isinstance(data, list)
        or len(data) != inputs
        or not isinstance(usage, EmbeddingUsage)
        or not _is_count(usage.prompt_tokens)
        or not isinstance(model, str)
        or MODEL_NAME.fullmatch(model) is None
    ):
        raise ProviderError("bad-response")
    by_index = dict(_indexed_vector(item, dimensions) for item in data)
    # A repeated index collapses in the dict, so only 0..n-1 passes the count.
    if sorted(by_index) != list(range(inputs)):
        raise ProviderError("bad-response")
    return EmbeddingReply(
        embeddings=tuple(by_index[n] for n in range(inputs)),
        model=model,
        input_tokens=usage.prompt_tokens,
    )
