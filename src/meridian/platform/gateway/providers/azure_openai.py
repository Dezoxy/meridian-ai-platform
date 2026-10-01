"""The Azure OpenAI chat adapter (hard rule 4, T-19).

The import contracts in ``pyproject.toml`` make this the only module in
``src/`` that imports ``openai`` or any ``azure`` package.

One gateway attempt is one HTTP request: the SDK's own retries are off
(``max_retries=0``), so the audit row is true; the next candidate is the
gateway's walk (S042), never the SDK's.
The credential is a bearer token from a named source, never an API key: key
authentication is off on the account (S007). A ``ProviderError`` carries a kind
and an HTTP status and no cause or context, and tests pin that a provider's
message, which can echo a prompt, appears in none of them (T-18).

IMPLEMENTED against a mocked transport; the opt-in live test is what shows the
service accepts the request.
"""

import logging
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
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

from meridian.platform.gateway.models import ChatRequest
from meridian.platform.gateway.providers.base import (
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


def kind_of_status(status: int) -> ProviderErrorKind:
    """The table first; any other 4xx is the request's fault (T-45), so it ends
    the walk and counts for no circuit; everything else is the service's."""
    explicit = STATUS_KINDS.get(status)
    if explicit is not None:
        return explicit
    return "rejected" if status in CLIENT_ERRORS else "unavailable"


Failure = tuple[ProviderErrorKind, int | None]


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
        raise ProviderError("auth")


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
        if deployment.deployment_name is None:
            raise ValueError(f"deployment {deployment.id} has no deployment_name")
        client = self._client_for(deployment)
        # Connect gets at most half the budget and the other phases the rest, so
        # connect plus the answer stay inside it: 25 s gives 5 s and 20 s, 12 s
        # gives 5 s and 7 s, 3 s gives 1.5 s and 1.5 s. Writing the request and
        # waiting for a pooled connection are phases of their own, the read
        # limit is between bytes, and the token source runs before the request:
        # none is inside the budget, so the runtime's own 30 s is the last line.
        connect = min(CONNECT_TIMEOUT_SECONDS, timeout_seconds / 2)
        rest = min(PROVIDER_TIMEOUT_SECONDS, timeout_seconds - connect)
        failure: Failure
        try:
            completion = client.chat.completions.create(
                model=deployment.deployment_name,
                messages=[
                    {"role": m.role, "content": m.content} for m in request.messages
                ],
                # S007's smoke test sends max_tokens to this API version; the
                # opt-in live test is the arbiter.
                max_tokens=request.max_output_tokens,
                n=1,
                timeout=openai.Timeout(rest, connect=connect),
            )
        # The provider's exception is mapped to (kind, status) and dropped
        # inside the arm. Raising from the arm would leave it in __context__
        # even with ``from None``, and its message can echo a prompt.
        except openai.APITimeoutError:  # before APIConnectionError, its base
            failure = ("timeout", None)
        except openai.APIConnectionError:
            failure = ("unavailable", None)
        except openai.APIStatusError as error:
            status = error.status_code
            failure = (kind_of_status(status), status)
        except openai.APIResponseValidationError:
            failure = ("bad-response", None)
        except AzureError:  # the token provider failed, before any request
            failure = ("auth", None)
        except openai.OpenAIError:
            failure = ("unavailable", None)
        else:
            return _reply_from(completion)
        raise ProviderError(*failure) from None


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
            raise ProviderError("rejected")
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
