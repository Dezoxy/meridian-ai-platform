"""The Azure OpenAI adapter, through the real openai SDK on ``httpx.MockTransport``.

No network and no Azure: the transport answers in memory, and the token
provider is a fake. Nothing here proves the live service accepts the request;
the opt-in live test is the arbiter for that.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import httpx2
import openai
import pytest
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage as EmbeddingUsage
from servicesupport import REGISTRY_DIR

from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest, Message
from meridian.platform.gateway.providers import azure_openai
from meridian.platform.gateway.providers.azure_openai import (
    API_VERSION,
    CONNECT_TIMEOUT_SECONDS,
    PROVIDER_TIMEOUT_SECONDS,
    TOKEN_SCOPE,
    AzureOpenAIProvider,
    azure_cli_token_provider,
    check_token,
    refuse_sdk_environment,
)
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ProviderError,
    ProviderReply,
)
from meridian.platform.registry import load_registry
from meridian.platform.registry.models import Deployment
from meridian.runtime.app import GATEWAY_TIMEOUT_SECONDS

ENDPOINT = "https://oai-meridian-sdc-a1b2c3.openai.azure.com"
OTHER_ENDPOINT = "https://oai-meridian-gwc-a1b2c3.openai.azure.com"
FAKE_TOKEN = "fake-entra-token"  # noqa: S105
CANARY = "CANARY-prompt-echo-7731"
REQUEST = ChatRequest(
    messages=(
        Message(role="system", content="You draft triage summaries."),
        Message(role="user", content="Storm damage to the roof."),
        Message(role="assistant", content="Understood."),
        Message(role="user", content="Draft it."),
    ),
    max_output_tokens=321,
)

Handler = Callable[[httpx.Request], httpx.Response]


def completion(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o-2024-11-20",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Drafted."},
            }
        ],
        "usage": {"prompt_tokens": 31, "completion_tokens": 7, "total_tokens": 38},
    }
    return body | overrides


def answer(**overrides: Any) -> Handler:
    return lambda _request: httpx.Response(200, json=completion(**overrides))


def make_provider(
    handler: Handler,
    requests: list[httpx.Request] | None = None,
    token_provider: Callable[[], str] = lambda: FAKE_TOKEN,
    endpoints: dict[str, str] | None = None,
) -> AzureOpenAIProvider:
    def recording(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return handler(request)

    return AzureOpenAIProvider(
        endpoints if endpoints is not None else {"sdc": ENDPOINT},
        token_provider,
        http_client=httpx.Client(transport=httpx.MockTransport(recording)),
    )


@pytest.fixture(scope="module")
def deployment() -> Deployment:
    found = load_registry(REGISTRY_DIR).deployment("aoai-sdc-gpt-4o")
    assert found is not None
    return found.model_copy(update={"deployment_name": "chat-deploy"})


def error_of(provider: AzureOpenAIProvider, deployment: Deployment) -> ProviderError:
    with pytest.raises(ProviderError) as raised:
        provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)
    return raised.value


# ── the request ──────────────────────────────────────────────────────────────
def test_the_request_goes_to_the_deployment_path_with_the_api_version(
    deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(answer(), requests)

    provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    (request,) = requests
    assert request.method == "POST"
    assert request.url.scheme == "https"
    assert request.url.host == "oai-meridian-sdc-a1b2c3.openai.azure.com"
    assert request.url.path == "/openai/deployments/chat-deploy/chat/completions"
    assert request.url.params["api-version"] == API_VERSION == "2024-10-21"


def test_a_trailing_slash_on_the_endpoint_gives_the_same_url(
    deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(answer(), requests, endpoints={"sdc": ENDPOINT + "/"})

    provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    assert requests[0].url.path == "/openai/deployments/chat-deploy/chat/completions"


def test_the_request_carries_a_bearer_token_and_no_api_key(
    deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(answer(), requests)

    provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    headers = requests[0].headers
    assert headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert "api-key" not in headers


def test_a_key_in_the_environment_never_becomes_an_api_key_header(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "env-key-azure")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key-openai")
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "env-token")
    requests: list[httpx.Request] = []
    provider = make_provider(answer(), requests)

    provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    headers = requests[0].headers
    assert "api-key" not in headers
    assert headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert "env-key" not in str(dict(headers))


def test_the_body_holds_the_messages_in_order_and_the_output_limit(
    deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(answer(), requests)

    provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    body = json.loads(requests[0].content)
    assert body["messages"] == [
        {"role": "system", "content": "You draft triage summaries."},
        {"role": "user", "content": "Storm damage to the roof."},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "Draft it."},
    ]
    assert body["max_tokens"] == 321
    assert body["n"] == 1
    assert not body.get("stream")
    assert "tools" not in body


def test_each_location_key_has_its_own_endpoint(deployment: Deployment) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(
        answer(),
        requests,
        endpoints={"sdc": ENDPOINT, "gwc": OTHER_ENDPOINT},
    )
    in_gwc = deployment.model_copy(update={"terraform_key": "gwc/gpt-4o"})

    provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)
    provider.chat(in_gwc, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    assert [r.url.host for r in requests] == [
        "oai-meridian-sdc-a1b2c3.openai.azure.com",
        "oai-meridian-gwc-a1b2c3.openai.azure.com",
    ]


def built_sdk_client(provider: AzureOpenAIProvider) -> openai.AzureOpenAI:
    return provider._clients["sdc"]


def test_the_built_client_has_redirects_and_environment_off() -> None:
    provider = AzureOpenAIProvider({"sdc": ENDPOINT}, lambda: FAKE_TOKEN)

    sdk = built_sdk_client(provider)
    inner = sdk._client

    assert isinstance(inner, httpx2.Client)
    assert inner.follow_redirects is False
    assert inner.trust_env is False


def test_the_built_client_bounds_connect_by_five_and_other_phases_by_twenty() -> None:
    provider = AzureOpenAIProvider({"sdc": ENDPOINT}, lambda: FAKE_TOKEN)

    sdk = built_sdk_client(provider)

    for timeout in (sdk.timeout, sdk._client.timeout):
        assert isinstance(timeout, httpx2.Timeout)
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (
            5.0,
            20.0,
            20.0,
            20.0,
        )
    assert (CONNECT_TIMEOUT_SECONDS, PROVIDER_TIMEOUT_SECONDS) == (5.0, 20.0)


def test_connect_plus_read_stays_under_the_runtimes_timeout_to_the_gateway() -> None:
    provider = AzureOpenAIProvider({"sdc": ENDPOINT}, lambda: FAKE_TOKEN)

    timeout = built_sdk_client(provider).timeout

    assert timeout.connect + timeout.read < GATEWAY_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("timeout_seconds", "connect", "rest"),
    [(25.0, 5.0, 20.0), (12.0, 5.0, 7.0), (3.0, 1.5, 1.5)],
)
def test_the_attempt_budget_is_split_between_connecting_and_the_other_phases(
    deployment: Deployment, timeout_seconds: float, connect: float, rest: float
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(answer(), requests)

    provider.chat(deployment, REQUEST, timeout_seconds=timeout_seconds)

    (request,) = requests
    assert request.extensions["timeout"] == {
        "connect": connect,
        "read": rest,
        "write": rest,
        "pool": rest,
    }
    assert connect + rest <= timeout_seconds


def test_the_next_call_is_not_bound_by_the_previous_calls_timeout(
    deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(answer(), requests)

    provider.chat(deployment, REQUEST, timeout_seconds=3.0)
    provider.chat(deployment, REQUEST, timeout_seconds=12.0)

    assert [r.extensions["timeout"]["read"] for r in requests] == [1.5, 7.0]


def test_one_built_client_is_shared_by_every_location(
    deployment: Deployment,
) -> None:
    provider = AzureOpenAIProvider(
        {"sdc": ENDPOINT, "gwc": OTHER_ENDPOINT}, lambda: FAKE_TOKEN
    )

    clients = {c._client for c in provider._clients.values()}

    assert len(clients) == 1


def test_close_closes_the_client_it_built_and_one_it_was_given() -> None:
    built = AzureOpenAIProvider({"sdc": ENDPOINT}, lambda: FAKE_TOKEN)
    given = httpx.Client(transport=httpx.MockTransport(answer()))
    injected = AzureOpenAIProvider(
        {"sdc": ENDPOINT}, lambda: FAKE_TOKEN, http_client=given
    )

    built.close()
    injected.close()

    assert built_sdk_client(built)._client.is_closed
    assert given.is_closed


@pytest.fixture
def redirecting_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> list[httpx2.Request]:
    """The SDK's own default client class, over a transport that answers 307:
    what the adapter builds is what is exercised."""
    seen: list[httpx2.Request] = []

    def redirect(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            307, headers={"location": ENDPOINT + "/openai/elsewhere"}, json={}
        )

    real = openai.DefaultHttpxClient
    monkeypatch.setattr(
        openai,
        "DefaultHttpxClient",
        lambda **kwargs: real(transport=httpx2.MockTransport(redirect), **kwargs),
    )
    return seen


def test_a_redirect_is_one_request_and_unavailable_through_the_built_client(
    deployment: Deployment, redirecting_transport: list[httpx2.Request]
) -> None:
    provider = AzureOpenAIProvider({"sdc": ENDPOINT}, lambda: FAKE_TOKEN)

    error = error_of(provider, deployment)

    assert len(redirecting_transport) == 1
    assert (error.kind, error.status_code) == ("unavailable", 307)


def test_a_redirect_is_one_request_and_unavailable_through_an_injected_client(
    deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(
        lambda _request: httpx.Response(307, headers={"location": ENDPOINT + "/x"}),
        requests,
    )

    error = error_of(provider, deployment)

    assert len(requests) == 1
    assert (error.kind, error.status_code) == ("unavailable", 307)


# ── the environment the SDK would read ───────────────────────────────────────
@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_CUSTOM_HEADERS",
        "OPENAI_LOG",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
    ],
)
def test_an_sdk_environment_variable_is_refused_by_name_and_never_by_value(
    name: str,
) -> None:
    with pytest.raises(ValueError, match=name) as raised:
        refuse_sdk_environment({"PATH": "/bin", name: "value-that-must-not-show"})

    assert "value-that-must-not-show" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_the_first_offending_variable_in_sorted_order_is_named() -> None:
    environ = {
        "OPENAI_PROJECT_ID": "a",
        "AZURE_OPENAI_ENDPOINT": "b",
        "OPENAI_LOG": "c",
    }

    with pytest.raises(ValueError) as raised:
        refuse_sdk_environment(environ)

    assert "AZURE_OPENAI_ENDPOINT" in str(raised.value)
    assert "OPENAI_LOG" not in str(raised.value)


def test_variables_the_sdk_does_not_read_are_accepted() -> None:
    refuse_sdk_environment(
        {
            "PATH": "/bin",
            "MERIDIAN_AZURE_OPENAI_ENDPOINTS": "{}",
            "OPENAI": "x",
            "XOPENAI_ORG_ID": "x",
            "openai_org_id": "x",
        }
    )


def test_the_process_environment_is_checked_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_ORG_ID", "value-that-must-not-show")

    with pytest.raises(ValueError, match="OPENAI_ORG_ID"):
        refuse_sdk_environment()


# ── the reply ────────────────────────────────────────────────────────────────
def test_a_200_maps_to_a_provider_reply(deployment: Deployment) -> None:
    provider = make_provider(answer())

    reply = provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    assert reply == ProviderReply(
        text="Drafted.",
        finish_reason="stop",
        model="gpt-4o-2024-11-20",
        input_tokens=31,
        output_tokens=7,
    )


def test_finish_reason_length_is_returned(deployment: Deployment) -> None:
    choice = {
        "index": 0,
        "finish_reason": "length",
        "message": {"role": "assistant", "content": "Cut o"},
    }
    provider = make_provider(answer(choices=[choice]))

    reply = provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    assert (reply.text, reply.finish_reason) == ("Cut o", "length")


def test_only_the_first_choice_is_read(deployment: Deployment) -> None:
    second = {
        "index": 1,
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": "Second."},
    }
    first = completion()["choices"][0]
    provider = make_provider(answer(choices=[first, second]))

    assert (
        provider.chat(
            deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS
        ).text
        == "Drafted."
    )


def test_a_completion_the_content_filter_withheld_is_filtered(
    deployment: Deployment,
) -> None:
    choice = {
        "index": 0,
        "finish_reason": "content_filter",
        "message": {"role": "assistant", "content": None},
    }
    provider = make_provider(answer(choices=[choice]))

    error = error_of(provider, deployment)

    assert (error.kind, error.status_code) == ("filtered", None)


# ── a response schema (S051) ─────────────────────────────────────────────────
SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["applies", "none"]}},
    "required": ["verdict"],
    "additionalProperties": False,
}
SCHEMA_REQUEST = REQUEST.model_copy(update={"response_schema": SCHEMA})


def test_a_schema_request_sends_exactly_the_strict_json_schema_response_format(
    deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(answer(), requests)

    provider.chat(deployment, SCHEMA_REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    body = json.loads(requests[0].content)
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "strict": True, "schema": SCHEMA},
    }
    assert body["messages"][-1] == {"role": "user", "content": "Draft it."}
    assert body["max_tokens"] == 321


def test_a_request_without_a_schema_sends_no_response_format_at_all(
    deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(answer(), requests)

    provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    assert "response_format" not in json.loads(requests[0].content)


def refusal_choice(refusal: object, **changes: Any) -> dict[str, Any]:
    """What structured outputs answer when the model declines: no content."""
    message = {"role": "assistant", "content": None, "refusal": refusal}
    return {"index": 0, "finish_reason": "stop", "message": message} | changes


@pytest.mark.parametrize(
    "choice",
    [
        pytest.param(refusal_choice(CANARY), id="refusal-with-no-content"),
        pytest.param(refusal_choice(""), id="empty-refusal"),
        pytest.param(
            refusal_choice(CANARY, finish_reason="length"), id="refusal-at-length"
        ),
        pytest.param(
            refusal_choice(
                CANARY,
                message={
                    "role": "assistant",
                    "content": '{"verdict":"none"}',
                    "refusal": CANARY,
                },
            ),
            id="refusal-beside-content",
        ),
    ],
)
def test_a_models_refusal_is_filtered_with_no_status_and_none_of_its_text(
    deployment: Deployment,
    choice: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    provider = make_provider(answer(choices=[choice]))

    with pytest.raises(ProviderError) as raised:
        provider.chat(
            deployment, SCHEMA_REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS
        )

    error = raised.value
    assert (error.kind, error.status_code, error.sent) == ("filtered", None, True)
    assert error.__context__ is None
    assert error.__cause__ is None
    assert CANARY not in str(error) + repr(error) + repr(error.args)
    assert CANARY not in caplog.text + " ".join(r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("refusal", [5, True, ["no"], {"text": "no"}, 1.5])
def test_a_refusal_that_is_not_a_string_is_a_bad_response(
    deployment: Deployment, refusal: object
) -> None:
    provider = make_provider(answer(choices=[refusal_choice(refusal)]))

    error = error_of(provider, deployment)

    assert (error.kind, error.status_code) == ("bad-response", None)


def test_a_null_refusal_with_content_is_an_ordinary_answer(
    deployment: Deployment,
) -> None:
    message = {"role": "assistant", "content": '{"verdict":"none"}', "refusal": None}
    provider = make_provider(answer(choices=[refusal_choice(None, message=message)]))

    reply = provider.chat(
        deployment, SCHEMA_REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS
    )

    assert reply.text == '{"verdict":"none"}'


def test_a_null_refusal_with_no_content_is_still_a_bad_response(
    deployment: Deployment,
) -> None:
    provider = make_provider(answer(choices=[refusal_choice(None)]))

    assert error_of(provider, deployment).kind == "bad-response"


def test_the_content_filter_finish_reason_wins_over_a_refusal(
    deployment: Deployment,
) -> None:
    choice = refusal_choice(5, finish_reason="content_filter")
    provider = make_provider(answer(choices=[choice]))

    assert error_of(provider, deployment).kind == "filtered"


def bad_choice(**changes: Any) -> dict[str, Any]:
    choice = completion()["choices"][0]
    return choice | changes


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"choices": []}, id="empty-choices"),
        pytest.param(
            {"choices": [bad_choice(message={"role": "assistant", "content": None})]},
            id="none-content",
        ),
        pytest.param({"usage": None}, id="usage-null"),
        pytest.param({"choices": [bad_choice(finish_reason="tool_calls")]}, id="tools"),
        pytest.param({"choices": [bad_choice(finish_reason=None)]}, id="no-finish"),
        pytest.param(
            {"choices": [bad_choice(finish_reason="function_call")]},
            id="function-call",
        ),
    ],
)
def test_a_reply_the_adapter_cannot_use_is_a_bad_response(
    deployment: Deployment, overrides: dict[str, Any]
) -> None:
    provider = make_provider(answer(**overrides))

    error = error_of(provider, deployment)

    assert (error.kind, error.status_code) == ("bad-response", None)


def bad_usage(**changes: Any) -> dict[str, Any]:
    usage = {"prompt_tokens": 31, "completion_tokens": 7, "total_tokens": 38}
    return {k: v for k, v in (usage | changes).items() if v is not MISSING}


MISSING = object()


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"choices": [bad_choice(message=None)]}, id="message-null"),
        pytest.param({"choices": [None]}, id="choices-null-item"),
        pytest.param({"choices": ["x"]}, id="choices-string-item"),
        pytest.param({"choices": "x"}, id="choices-string"),
        pytest.param({"choices": None}, id="choices-null"),
        pytest.param({"choices": {"0": 1}}, id="choices-object"),
        pytest.param(
            {"usage": bad_usage(completion_tokens=MISSING)},
            id="completion-tokens-missing",
        ),
        pytest.param(
            {"usage": bad_usage(completion_tokens=None)}, id="completion-tokens-null"
        ),
        pytest.param(
            {"usage": bad_usage(completion_tokens=-1)}, id="completion-tokens-negative"
        ),
        pytest.param(
            {"usage": bad_usage(completion_tokens=1.5)}, id="completion-tokens-float"
        ),
        pytest.param({"usage": bad_usage(prompt_tokens=None)}, id="prompt-tokens-null"),
        pytest.param(
            {"usage": bad_usage(prompt_tokens=-5)}, id="prompt-tokens-negative"
        ),
        pytest.param({"usage": "x"}, id="usage-string"),
        pytest.param({"model": "m" * 200}, id="model-200-characters"),
        pytest.param({"model": "gpt 4o"}, id="model-with-a-space"),
        pytest.param({"model": "gpt-4o\n"}, id="model-with-a-newline"),
        pytest.param({"model": ""}, id="model-empty"),
        pytest.param({"model": None}, id="model-null"),
        pytest.param({"model": 4}, id="model-number"),
        pytest.param(
            {"choices": [bad_choice(message={"role": "assistant", "content": 5})]},
            id="content-number",
        ),
        pytest.param({"choices": [bad_choice(finish_reason=5)]}, id="finish-number"),
        pytest.param({"choices": [{"index": 0}]}, id="choice-without-members"),
    ],
)
def test_a_reply_of_the_wrong_shape_is_a_bad_response_and_not_a_crash(
    deployment: Deployment, overrides: dict[str, Any]
) -> None:
    provider = make_provider(answer(**overrides))

    error = error_of(provider, deployment)

    assert (error.kind, error.status_code) == ("bad-response", None)
    assert error.__context__ is None
    assert error.__cause__ is None


def test_a_model_name_of_128_characters_is_the_longest_accepted(
    deployment: Deployment,
) -> None:
    provider = make_provider(answer(model="m" * 128))

    assert (
        provider.chat(
            deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS
        ).model
        == "m" * 128
    )


@pytest.mark.parametrize(
    ("sent", "seen_by_the_adapter"), [("3", 3), (True, 1), (3.0, 3)]
)
def test_the_sdk_coerces_a_numeric_string_or_bool_count_before_the_adapter_sees_it(
    deployment: Deployment, sent: object, seen_by_the_adapter: int
) -> None:
    # The SDK builds the completion without strict validation but still coerces
    # these to int, so the adapter cannot tell them from a true int. Pinned so
    # a change in the SDK shows (the contract asked for bad-response here).
    provider = make_provider(answer(usage=bad_usage(prompt_tokens=sent)))

    reply = provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    assert reply.input_tokens == seen_by_the_adapter


def test_zero_tokens_are_accepted(deployment: Deployment) -> None:
    provider = make_provider(
        answer(usage=bad_usage(prompt_tokens=0, completion_tokens=0))
    )

    reply = provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    assert (reply.input_tokens, reply.output_tokens) == (0, 0)


def test_a_reply_without_a_usage_member_is_a_bad_response(
    deployment: Deployment,
) -> None:
    body = completion()
    del body["usage"]
    provider = make_provider(lambda _request: httpx.Response(200, json=body))

    assert error_of(provider, deployment).kind == "bad-response"


def test_a_reply_that_is_not_json_is_a_bad_response(deployment: Deployment) -> None:
    provider = make_provider(
        lambda _request: httpx.Response(
            200, content=f"<html>{CANARY}</html>".encode(), headers={"x": "y"}
        )
    )

    error = error_of(provider, deployment)

    assert error.kind == "bad-response"
    assert CANARY not in repr(error) + str(error)


def test_a_json_answer_with_an_unparseable_body_is_a_bad_response_for_chat(
    deployment: Deployment,
) -> None:
    # With a JSON content type the SDK parses the body itself and raises on it,
    # where the test above gets the text back as a plain string.
    provider = make_provider(
        lambda _request: httpx.Response(
            200,
            content=f"<html>{CANARY}</html>".encode(),
            headers={"content-type": "application/json"},
        )
    )

    error = error_of(provider, deployment)

    assert (error.kind, error.status_code, error.sent) == ("bad-response", None, True)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert CANARY not in repr(error) + str(error)


# ── the errors ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, "rejected"),
        (404, "unavailable"),
        (422, "rejected"),
        (401, "auth"),
        (403, "auth"),
        (429, "rate-limited"),
        (500, "unavailable"),
        (502, "unavailable"),
        (503, "unavailable"),
        (504, "unavailable"),
        (408, "timeout"),
        (409, "rejected"),
        (413, "rejected"),
        (415, "rejected"),
        (418, "rejected"),
        (499, "rejected"),
    ],
)
def test_a_status_maps_to_its_kind_and_keeps_the_status(
    deployment: Deployment, status: int, kind: str
) -> None:
    provider = make_provider(
        lambda _request: httpx.Response(status, json={"error": {"message": CANARY}})
    )

    error = error_of(provider, deployment)

    assert (error.kind, error.status_code) == (kind, status)


def azure_error(
    status: int = 400,
    code: object = "content_filter",
    *,
    inner: object = "ResponsibleAIPolicyViolation",
) -> Handler:
    """An answer as Azure OpenAI gives for a prompt its content filter refused,
    with a canary where the provider's message, which echoes the prompt, is."""
    error = {
        "message": CANARY,
        "type": None,
        "param": "prompt",
        "code": code,
        "status": status,
        "innererror": {"code": inner, "content_filter_result": {"hate": CANARY}},
    }
    return lambda _request: httpx.Response(status, json={"error": error})


def test_an_azure_400_with_the_content_filter_code_is_filtered(
    deployment: Deployment,
) -> None:
    error = error_of(make_provider(azure_error()), deployment)

    assert (error.kind, error.status_code, error.sent) == ("filtered", 400, True)


def test_the_provider_text_of_a_filtered_400_is_nowhere(
    deployment: Deployment, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    error = error_of(make_provider(azure_error()), deployment)

    assert error.__cause__ is None
    assert error.__context__ is None
    assert CANARY not in str(error) + repr(error) + repr(error.args)
    assert CANARY not in caplog.text + " ".join(r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(azure_error(code="context_length_exceeded"), id="other-code"),
        pytest.param(azure_error(code=None), id="no-code"),
        pytest.param(azure_error(code="Content_Filter"), id="other-case"),
        pytest.param(azure_error(code="content_filter "), id="trailing-space"),
        pytest.param(
            azure_error(code="invalid_prompt", inner="content_filter"),
            id="only-the-inner-code",
        ),
        pytest.param(
            lambda _request: httpx.Response(400, content=b"content_filter"),
            id="not-json",
        ),
        pytest.param(
            lambda _request: httpx.Response(400, json={"error": "content_filter"}),
            id="error-is-a-string",
        ),
        pytest.param(
            lambda _request: httpx.Response(400, json=["content_filter"]),
            id="body-is-a-list",
        ),
    ],
)
def test_any_other_400_stays_rejected(deployment: Deployment, handler: Handler) -> None:
    error = error_of(make_provider(handler), deployment)

    assert (error.kind, error.status_code, error.sent) == ("rejected", 400, True)
    assert error.__context__ is None


@pytest.mark.parametrize("status", [401, 403, 404, 422, 429, 500])
def test_the_content_filter_code_on_another_status_is_not_filtered(
    deployment: Deployment, status: int
) -> None:
    error = error_of(make_provider(azure_error(status)), deployment)

    assert error.kind != "filtered"
    assert error.status_code == status


@pytest.mark.parametrize("status", [500, 429])
def test_a_failing_status_causes_exactly_one_request(
    deployment: Deployment, status: int
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(
        lambda _request: httpx.Response(status, json={"error": {"message": "x"}}),
        requests,
    )

    error_of(provider, deployment)

    assert len(requests) == 1


def test_a_transport_failure_causes_exactly_one_request(
    deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider = make_provider(refuse, requests)

    error_of(provider, deployment)

    assert len(requests) == 1


@pytest.mark.parametrize(
    "failure", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout]
)
def test_a_transport_timeout_is_a_timeout(
    deployment: Deployment, failure: type[httpx.TimeoutException]
) -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        raise failure("timed out", request=request)

    error = error_of(make_provider(time_out), deployment)

    assert (error.kind, error.status_code) == ("timeout", None)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadError])
def test_a_connection_failure_is_unavailable(
    deployment: Deployment, failure: type[httpx.TransportError]
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise failure("refused", request=request)

    error = error_of(make_provider(refuse), deployment)

    assert (error.kind, error.status_code) == ("unavailable", None)


@pytest.mark.parametrize(
    "failure",
    [
        ClientAuthenticationError(message=CANARY),
        CredentialUnavailableError(message=CANARY),
    ],
    ids=["client-authentication", "credential-unavailable"],
)
def test_a_failing_token_provider_is_an_auth_error_and_no_request_is_sent(
    deployment: Deployment, failure: Exception
) -> None:
    requests: list[httpx.Request] = []

    def failing() -> str:
        raise failure

    provider = make_provider(answer(), requests, token_provider=failing)

    error = error_of(provider, deployment)

    assert (error.kind, error.status_code) == ("auth", None)
    assert requests == []


def raise_status(status: int) -> Handler:
    return lambda _request: httpx.Response(
        status,
        json={"error": {"message": CANARY, "code": CANARY}},
        headers={"x-echo": CANARY},
    )


def raise_transport(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(CANARY, request=request)


def raise_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout(CANARY, request=request)


def raise_connect_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout(CANARY, request=request)


def fail_token() -> str:
    raise ClientAuthenticationError(message=CANARY)


CANARY_CASES = [
    pytest.param(raise_status(400), id="400"),
    pytest.param(raise_status(401), id="401"),
    pytest.param(raise_status(429), id="429"),
    pytest.param(raise_status(500), id="500"),
    pytest.param(raise_transport, id="connect-error"),
    pytest.param(raise_timeout, id="timeout"),
    pytest.param(raise_connect_timeout, id="connect-timeout"),
]


@pytest.mark.parametrize("handler", CANARY_CASES)
def test_a_canary_in_the_provider_failure_appears_nowhere_in_the_raised_error(
    deployment: Deployment, handler: Handler
) -> None:
    error = error_of(make_provider(handler), deployment)

    assert error.__cause__ is None
    assert error.__context__ is None
    assert CANARY not in str(error)
    assert CANARY not in repr(error)
    assert all(CANARY not in repr(arg) for arg in error.args)


def test_a_canary_in_a_credential_failure_appears_nowhere_in_the_raised_error(
    deployment: Deployment,
) -> None:
    error = error_of(make_provider(answer(), token_provider=fail_token), deployment)

    assert error.__cause__ is None
    assert error.__context__ is None
    assert CANARY not in str(error) + repr(error) + repr(error.args)


def test_the_error_carries_a_kind_and_a_status_only() -> None:
    error = ProviderError("rate-limited", 429)

    assert error.args == ("rate-limited", 429)
    assert str(error) == "provider call failed: rate-limited (HTTP 429)"


# ── sent: was a request on the wire when the call failed (S011) ──────────────
def test_sent_is_keyword_only_defaults_to_true_and_is_not_in_the_text() -> None:
    sent = ProviderError("timeout")
    unsent = ProviderError("timeout", None, sent=False)

    assert sent.sent is True
    assert unsent.sent is False
    assert unsent.args == ("timeout", None)
    assert str(unsent) == str(sent) == "provider call failed: timeout"
    with pytest.raises(TypeError):
        ProviderError("timeout", None, False)  # type: ignore[misc]


def raising(failure: type[httpx.TransportError]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure(CANARY, request=request)

    return handler


@pytest.mark.parametrize(
    ("failure", "kind", "sent"),
    [
        (httpx.ConnectTimeout, "timeout", False),
        (httpx.ReadTimeout, "timeout", True),
        (httpx.WriteTimeout, "timeout", True),
        (httpx.PoolTimeout, "timeout", True),
        (httpx.ConnectError, "unavailable", False),
        (httpx.ReadError, "unavailable", True),
        (httpx.WriteError, "unavailable", True),
        (httpx.RemoteProtocolError, "unavailable", True),
    ],
)
def test_a_transport_failure_says_whether_the_request_left(
    deployment: Deployment,
    failure: type[httpx.TransportError],
    kind: str,
    sent: bool,
) -> None:
    error = error_of(make_provider(raising(failure)), deployment)

    assert (error.kind, error.status_code, error.sent) == (kind, None, sent)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize("status", [400, 401, 404, 408, 422, 429, 500, 503])
def test_an_error_status_is_a_sent_request(deployment: Deployment, status: int) -> None:
    error = error_of(make_provider(raise_status(status)), deployment)

    assert error.status_code == status
    assert error.sent is True


@pytest.mark.parametrize(
    "failure",
    [
        ClientAuthenticationError(message=CANARY),
        CredentialUnavailableError(message=CANARY),
    ],
    ids=["client-authentication", "credential-unavailable"],
)
def test_a_failing_token_provider_is_a_request_that_never_left(
    deployment: Deployment, failure: Exception
) -> None:
    def failing() -> str:
        raise failure

    error = error_of(make_provider(answer(), token_provider=failing), deployment)

    assert (error.kind, error.sent) == ("auth", False)


FILTERED_CHOICE = {
    "index": 0,
    "finish_reason": "content_filter",
    "message": {"role": "assistant", "content": None},
}


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        (answer(choices=[FILTERED_CHOICE]), "filtered"),
        (answer(choices=[]), "bad-response"),
        (lambda _request: httpx.Response(200, content=b"not json"), "bad-response"),
    ],
    ids=["content-filter", "no-choices", "not-json"],
)
def test_a_failure_in_reading_a_200_is_a_sent_request(
    deployment: Deployment, reply: Handler, kind: str
) -> None:
    error = error_of(make_provider(reply), deployment)

    assert (error.kind, error.status_code, error.sent) == (kind, None, True)


def test_check_token_failure_is_a_request_that_never_left() -> None:
    def failing() -> str:
        raise ClientAuthenticationError(message=CANARY)

    with pytest.raises(ProviderError) as raised:
        check_token(failing)

    assert raised.value.sent is False


# ── configuration errors ─────────────────────────────────────────────────────
def test_an_unknown_location_key_raises_before_any_request(
    deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(answer(), requests, endpoints={"gwc": OTHER_ENDPOINT})

    with pytest.raises(ValueError, match="sdc"):
        provider.chat(deployment, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    assert requests == []


def test_a_deployment_without_a_terraform_key_raises_before_any_request(
    deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(answer(), requests)
    keyless = deployment.model_copy(update={"terraform_key": None})

    with pytest.raises(ValueError, match="terraform_key"):
        provider.chat(keyless, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    assert requests == []


def test_a_deployment_without_a_deployment_name_raises_before_any_request(
    deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(answer(), requests)
    nameless = deployment.model_copy(update={"deployment_name": None})

    with pytest.raises(ValueError, match="deployment_name"):
        provider.chat(nameless, REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)

    assert requests == []


# ── the Azure CLI token provider (no `az` is run) ────────────────────────────
class FakeToken:
    def __init__(self, token: str, expires_on: int) -> None:
        self.token = token
        self.expires_on = expires_on


class FakeCredential:
    instances: list["FakeCredential"] = []  # noqa: RUF012  # test double

    def __init__(self, *, tenant_id: str = "") -> None:
        self.tenant_id = tenant_id
        self.scopes: list[tuple[str, ...]] = []
        FakeCredential.instances.append(self)

    def get_token(self, *scopes: str) -> FakeToken:
        self.scopes.append(scopes)
        return FakeToken(f"token-{len(self.scopes)}", expires_on=4_000_000_000)


@pytest.fixture
def fake_credential(monkeypatch: pytest.MonkeyPatch) -> type[FakeCredential]:
    FakeCredential.instances = []
    monkeypatch.setattr(azure_openai, "AzureCliCredential", FakeCredential)
    return FakeCredential


def test_the_cli_token_provider_asks_for_the_cognitive_services_scope(
    fake_credential: type[FakeCredential],
) -> None:
    provider = azure_cli_token_provider("tenant-1")

    first = provider()

    (credential,) = fake_credential.instances
    assert first == "token-1"
    assert credential.tenant_id == "tenant-1"
    assert credential.scopes == [(TOKEN_SCOPE,)]
    assert TOKEN_SCOPE == "https://cognitiveservices.azure.com/.default"  # noqa: S105


def test_the_cli_token_provider_without_a_tenant_uses_the_logged_in_one(
    fake_credential: type[FakeCredential],
) -> None:
    azure_cli_token_provider(None)()

    assert fake_credential.instances[0].tenant_id == ""


def test_the_cli_token_provider_does_not_run_az_again_while_the_token_is_fresh(
    fake_credential: type[FakeCredential],
) -> None:
    provider = azure_cli_token_provider(None)

    first, second = provider(), provider()

    assert first == second
    assert len(fake_credential.instances[0].scopes) == 1


def test_the_cli_token_provider_asks_again_when_the_token_is_about_to_expire(
    fake_credential: type[FakeCredential], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        FakeCredential,
        "get_token",
        lambda self, *scopes: (
            self.scopes.append(scopes) or FakeToken(f"t{len(self.scopes)}", 1_000)
        ),
    )
    monkeypatch.setattr(azure_openai, "_now", lambda: 900.0)
    provider = azure_cli_token_provider(None)

    first, second = provider(), provider()

    assert (first, second) == ("t1", "t2")


def test_concurrent_callers_share_one_token_fetch(
    fake_credential: type[FakeCredential], monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_get_token(self: FakeCredential, *scopes: str) -> FakeToken:
        self.scopes.append(scopes)
        time.sleep(0.05)  # long enough for every thread to be waiting
        return FakeToken("shared", expires_on=4_000_000_000)

    monkeypatch.setattr(FakeCredential, "get_token", slow_get_token)
    provider = azure_cli_token_provider(None)
    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(provider())) for _ in range(8)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["shared"] * 8
    assert len(fake_credential.instances[0].scopes) == 1


# ── what the Azure credential may log ───────────────────────────────────────
@pytest.fixture
def azure_identity_logger(monkeypatch: pytest.MonkeyPatch) -> logging.Logger:
    """The real logger, with its propagation restored after the test."""
    logger = logging.getLogger("azure.identity")
    monkeypatch.setattr(logger, "propagate", True)
    monkeypatch.setattr(logger, "handlers", list(logger.handlers))
    return logger


def test_a_failed_login_leaves_no_log_record_with_the_clis_error_text(
    fake_credential: type[FakeCredential],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    azure_identity_logger: logging.Logger,
) -> None:
    def failing_login(self: FakeCredential, *scopes: str) -> FakeToken:
        # What AzureCliCredential does: it logs the CLI's own error text.
        logging.getLogger("azure.identity._credentials.azure_cli").error(
            "Azure CLI error: %s", CANARY
        )
        logging.getLogger("azure.identity").warning("AzureCliCredential: %s", CANARY)
        raise ClientAuthenticationError(message=CANARY)

    monkeypatch.setattr(FakeCredential, "get_token", failing_login)
    caplog.set_level(logging.DEBUG)
    provider = azure_cli_token_provider(None)

    with pytest.raises(ClientAuthenticationError):
        provider()

    assert [r.getMessage() for r in caplog.records if CANARY in r.getMessage()] == []
    assert CANARY not in caplog.text


def test_the_azure_identity_logger_gets_one_null_handler_however_often_it_is_set_up(
    fake_credential: type[FakeCredential], azure_identity_logger: logging.Logger
) -> None:
    azure_cli_token_provider(None)
    azure_cli_token_provider(None)

    null_handlers = [
        h for h in azure_identity_logger.handlers if isinstance(h, logging.NullHandler)
    ]
    assert len(null_handlers) == 1
    assert azure_identity_logger.propagate is False


# ── check_token: one call, Azure's errors mapped inside the arm ──────────────
def test_check_token_calls_the_provider_once_and_returns_nothing() -> None:
    calls: list[int] = []

    def token() -> str:
        calls.append(1)
        return FAKE_TOKEN

    assert check_token(token) is None
    assert calls == [1]


@pytest.mark.parametrize(
    "failure",
    [
        ClientAuthenticationError(message=CANARY),
        CredentialUnavailableError(message=CANARY),
    ],
    ids=["client-authentication", "credential-unavailable"],
)
def test_check_token_maps_an_azure_credential_failure_to_an_auth_error(
    failure: Exception,
) -> None:
    def failing() -> str:
        raise failure

    with pytest.raises(ProviderError) as raised:
        check_token(failing)

    assert (raised.value.kind, raised.value.status_code) == ("auth", None)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert CANARY not in str(raised.value) + repr(raised.value)


def test_check_token_lets_anything_else_through() -> None:
    def failing() -> str:
        raise RuntimeError("not a credential failure")

    with pytest.raises(RuntimeError):
        check_token(failing)


# ── embeddings (S045) ────────────────────────────────────────────────────────
# One call to the embeddings endpoint with the registry's dimensions, and an
# answer that is trusted only when its count, its indexes, its sizes and its
# numbers are right (T-54).
DIMENSIONS = 8
EMBED_INPUTS = ("Storm damage.", "Water in the cellar.")
EMBED_REQUEST = EmbeddingRequest(inputs=EMBED_INPUTS)
EMBED_PATH = "/openai/deployments/embed-deploy/embeddings"
JSON_HEADERS = {"content-type": "application/json"}


def vector(seed: int, size: int = DIMENSIONS) -> list[float]:
    return [seed + n / 8 for n in range(size)]  # exact in binary, so == is safe


def embedding_data(count: int = 2, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "object": "list",
        "model": "text-embedding-3-large",
        "data": [
            {"object": "embedding", "index": n, "embedding": vector(n)}
            for n in range(count)
        ],
        "usage": {"prompt_tokens": 9, "total_tokens": 9},
    }
    return body | overrides


def embed_answer(**overrides: Any) -> Handler:
    """A 200 with a JSON body. ``json.dumps`` rather than httpx's ``json=``,
    which refuses NaN and infinity: the point is to send them."""
    content = json.dumps(embedding_data(**overrides)).encode()
    return lambda _request: httpx.Response(200, content=content, headers=JSON_HEADERS)


@pytest.fixture(scope="module")
def embedding_deployment() -> Deployment:
    found = load_registry(REGISTRY_DIR).deployment("aoai-sdc-text-embedding-3-large")
    assert found is not None
    return found.model_copy(update={"deployment_name": "embed-deploy", "dimensions": 8})


def embed_error_of(
    provider: AzureOpenAIProvider, deployment: Deployment
) -> ProviderError:
    with pytest.raises(ProviderError) as raised:
        provider.embed(
            deployment, EMBED_REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS
        )
    return raised.value


def embed(provider: AzureOpenAIProvider, deployment: Deployment) -> EmbeddingReply:
    return provider.embed(
        deployment, EMBED_REQUEST, timeout_seconds=PROVIDER_TIMEOUT_SECONDS
    )


def test_the_embedding_request_goes_to_the_deployments_embeddings_path(
    embedding_deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(embed_answer(), requests)

    embed(provider, embedding_deployment)

    (request,) = requests
    assert request.method == "POST"
    assert request.url.host == "oai-meridian-sdc-a1b2c3.openai.azure.com"
    assert request.url.path == EMBED_PATH
    assert request.url.params["api-version"] == API_VERSION
    assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert "api-key" not in request.headers


def test_the_embedding_body_carries_the_inputs_the_dimensions_and_float_format(
    embedding_deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(embed_answer(), requests)

    embed(provider, embedding_deployment)

    body = json.loads(requests[0].content)
    assert body["input"] == list(EMBED_INPUTS)
    assert body["dimensions"] == 8  # the deployment's, never the caller's
    assert body["encoding_format"] == "float"
    assert body["model"] == "embed-deploy"
    assert "user" not in body


@pytest.mark.parametrize(
    ("timeout_seconds", "connect", "rest"),
    [(25.0, 5.0, 20.0), (12.0, 5.0, 7.0), (3.0, 1.5, 1.5)],
)
def test_the_embedding_budget_is_split_like_the_chat_budget(
    embedding_deployment: Deployment,
    timeout_seconds: float,
    connect: float,
    rest: float,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(embed_answer(), requests)

    provider.embed(embedding_deployment, EMBED_REQUEST, timeout_seconds=timeout_seconds)

    assert requests[0].extensions["timeout"] == {
        "connect": connect,
        "read": rest,
        "write": rest,
        "pool": rest,
    }


def test_a_good_answer_is_an_embedding_reply_of_floats_in_input_order(
    embedding_deployment: Deployment,
) -> None:
    provider = make_provider(embed_answer())

    reply = embed(provider, embedding_deployment)

    assert reply == EmbeddingReply(
        embeddings=(tuple(vector(0)), tuple(vector(1))),
        model="text-embedding-3-large",
        input_tokens=9,
    )
    assert reply.output_tokens == 0
    assert all(type(x) is float for v in reply.embeddings for x in v)


def test_items_out_of_order_are_returned_in_index_order(
    embedding_deployment: Deployment,
) -> None:
    swapped = list(reversed(embedding_data()["data"]))
    provider = make_provider(embed_answer(data=swapped))

    reply = embed(provider, embedding_deployment)

    assert reply.embeddings == (tuple(vector(0)), tuple(vector(1)))


def test_whole_numbers_in_a_vector_are_returned_as_floats(
    embedding_deployment: Deployment,
) -> None:
    whole = [{"object": "embedding", "index": 0, "embedding": [1] * DIMENSIONS}]
    provider = make_provider(embed_answer(data=whole))

    reply = provider.embed(
        embedding_deployment,
        EmbeddingRequest(inputs=("one",)),
        timeout_seconds=PROVIDER_TIMEOUT_SECONDS,
    )

    assert reply.embeddings == ((1.0,) * DIMENSIONS,)
    assert all(type(x) is float for x in reply.embeddings[0])


def test_sixteen_inputs_come_back_as_sixteen_vectors(
    embedding_deployment: Deployment,
) -> None:
    provider = make_provider(embed_answer(**embedding_data(16)))

    reply = provider.embed(
        embedding_deployment,
        EmbeddingRequest(inputs=tuple(f"text {n}" for n in range(16))),
        timeout_seconds=PROVIDER_TIMEOUT_SECONDS,
    )

    assert [v[0] for v in reply.embeddings] == [float(n) for n in range(16)]


def item(index: object = 0, embedding: object = None, **changes: Any) -> dict[str, Any]:
    found = {
        "object": "embedding",
        "index": index,
        "embedding": vector(0) if embedding is None else embedding,
    }
    return found | changes


def usage(**changes: Any) -> dict[str, Any]:
    return {"prompt_tokens": 9, "total_tokens": 9} | changes


def two(first: dict[str, Any], second: dict[str, Any] | None = None) -> list[Any]:
    return [first, item(1) if second is None else second]


BAD_ANSWERS = [
    pytest.param({"data": [item(0)]}, id="one-item-for-two-inputs"),
    pytest.param(
        {"data": [item(0), item(1), item(2)]}, id="three-items-for-two-inputs"
    ),
    pytest.param({"data": []}, id="no-items"),
    pytest.param({"data": two(item(0), item(0))}, id="duplicated-index"),
    # The indexes collapse to exactly 0 and 1, so only the count says no.
    pytest.param(
        {"data": [item(0), item(1), item(1, vector(2))]},
        id="more-items-than-inputs-whose-indexes-collapse-to-the-right-ones",
    ),
    pytest.param({"data": two(item(0), item(2))}, id="index-out-of-range"),
    pytest.param({"data": two(item(1), item(2))}, id="indexes-from-one"),
    pytest.param({"data": two(item(0), item(-1))}, id="negative-index"),
    pytest.param({"data": two(item(0), item(None))}, id="index-null"),
    pytest.param({"data": two(item(0), item("1"))}, id="index-string"),
    pytest.param({"data": two(item(0), item(1.0))}, id="index-float"),
    pytest.param({"data": two(item(0), item(True))}, id="index-bool"),
    pytest.param(
        {"data": two(item(0), {"object": "embedding", "embedding": vector(1)})},
        id="index-missing",
    ),
    pytest.param({"data": two(item(0), item(1, vector(1, 7)))}, id="vector-too-short"),
    pytest.param({"data": two(item(0), item(1, vector(1, 9)))}, id="vector-too-long"),
    pytest.param({"data": two(item(0), item(1, []))}, id="vector-empty"),
    pytest.param({"data": two(item(0), item(1, "abcdefgh"))}, id="vector-string"),
    pytest.param({"data": two(item(0), item(1, {"a": 1}))}, id="vector-object"),
    pytest.param(
        {"data": two(item(0), {"object": "embedding", "index": 1})},
        id="vector-missing",
    ),
    pytest.param({"data": two(item(0), item(1, [None] * DIMENSIONS))}, id="null-parts"),
    pytest.param(
        {"data": two(item(0), item(1, ["0.5"] * DIMENSIONS))}, id="string-parts"
    ),
    pytest.param({"data": two(item(0), item(1, [[0.5]] * DIMENSIONS))}, id="nested"),
    pytest.param({"data": None}, id="data-null"),
    pytest.param({"data": {"0": 1}}, id="data-object"),
    pytest.param({"data": "x"}, id="data-string"),
    pytest.param({"data": [None, None]}, id="items-null"),
    pytest.param({"data": [3, 4]}, id="items-number"),
    pytest.param({"usage": None}, id="usage-null"),
    pytest.param({"usage": "x"}, id="usage-string"),
    pytest.param({"usage": usage(prompt_tokens=None)}, id="prompt-tokens-null"),
    pytest.param({"usage": usage(prompt_tokens=-1)}, id="prompt-tokens-negative"),
    pytest.param({"usage": usage(prompt_tokens=True)}, id="prompt-tokens-bool"),
    pytest.param({"usage": usage(prompt_tokens=1.5)}, id="prompt-tokens-float"),
    pytest.param({"usage": usage(prompt_tokens="9")}, id="prompt-tokens-string"),
    pytest.param({"usage": {"total_tokens": 9}}, id="prompt-tokens-missing"),
    pytest.param({"model": "gpt 4o"}, id="model-with-a-space"),
    pytest.param({"model": "m" * 129}, id="model-over-128-characters"),
    pytest.param({"model": "model\n"}, id="model-with-a-newline"),
    pytest.param({"model": ""}, id="model-empty"),
    pytest.param({"model": None}, id="model-null"),
    pytest.param({"model": 4}, id="model-number"),
]


@pytest.mark.parametrize("overrides", BAD_ANSWERS)
def test_an_answer_that_cannot_be_trusted_is_a_bad_response_and_not_a_crash(
    embedding_deployment: Deployment, overrides: dict[str, Any]
) -> None:
    provider = make_provider(embed_answer(**overrides))

    error = embed_error_of(provider, embedding_deployment)

    assert (error.kind, error.status_code, error.sent) == ("bad-response", None, True)
    assert error.__context__ is None
    assert error.__cause__ is None


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
def test_a_vector_with_a_number_that_is_not_finite_is_a_bad_response(
    embedding_deployment: Deployment, bad: float
) -> None:
    broken = vector(1)
    broken[3] = bad
    provider = make_provider(embed_answer(data=two(item(0), item(1, broken))))

    error = embed_error_of(provider, embedding_deployment)

    assert (error.kind, error.status_code, error.sent) == ("bad-response", None, True)
    assert error.__context__ is None


def test_a_number_too_large_for_a_float_is_a_bad_response(
    embedding_deployment: Deployment,
) -> None:
    # The SDK turns whole numbers in a vector into floats and raises
    # OverflowError for one that does not fit; that is the answer being wrong,
    # not the gateway.
    body = json.dumps(embedding_data()).replace("1.0,", "1" + "0" * 400 + ",", 1)
    provider = make_provider(
        lambda _request: httpx.Response(
            200, content=body.encode(), headers=JSON_HEADERS
        )
    )

    error = embed_error_of(provider, embedding_deployment)

    assert (error.kind, error.status_code, error.sent) == ("bad-response", None, True)
    assert error.__context__ is None
    assert error.__cause__ is None


@pytest.mark.parametrize(
    "content",
    [b"[1, 2, 3]", b'"text"', b"null", b"{}"],
    ids=["json-list", "json-string", "json-null", "json-empty-object"],
)
def test_a_json_answer_that_is_not_an_embeddings_response_is_a_bad_response(
    embedding_deployment: Deployment, content: bytes
) -> None:
    provider = make_provider(
        lambda _request: httpx.Response(200, content=content, headers=JSON_HEADERS)
    )

    error = embed_error_of(provider, embedding_deployment)

    assert (error.kind, error.sent) == ("bad-response", True)


def test_an_answer_that_is_not_json_is_a_bad_response_and_keeps_no_text(
    embedding_deployment: Deployment,
) -> None:
    provider = make_provider(
        lambda _request: httpx.Response(
            200, content=f"<html>{CANARY}</html>".encode(), headers={"x": "y"}
        )
    )

    error = embed_error_of(provider, embedding_deployment)

    assert error.kind == "bad-response"
    assert CANARY not in repr(error) + str(error)


def test_a_json_answer_with_an_unparseable_body_is_a_bad_response_for_embeddings(
    embedding_deployment: Deployment,
) -> None:
    provider = make_provider(
        lambda _request: httpx.Response(
            200, content=f"<html>{CANARY}</html>".encode(), headers=JSON_HEADERS
        )
    )

    error = embed_error_of(provider, embedding_deployment)

    assert (error.kind, error.status_code, error.sent) == ("bad-response", None, True)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert CANARY not in repr(error) + str(error)


def test_an_embedding_model_name_of_128_characters_is_the_longest_accepted(
    embedding_deployment: Deployment,
) -> None:
    provider = make_provider(embed_answer(model="m" * 128))

    assert embed(provider, embedding_deployment).model == "m" * 128


def test_zero_tokens_and_zero_components_are_accepted(
    embedding_deployment: Deployment,
) -> None:
    zeros = [{"object": "embedding", "index": 0, "embedding": [0.0] * DIMENSIONS}]
    provider = make_provider(embed_answer(data=zeros, usage=usage(prompt_tokens=0)))

    reply = provider.embed(
        embedding_deployment,
        EmbeddingRequest(inputs=("x",)),
        timeout_seconds=PROVIDER_TIMEOUT_SECONDS,
    )

    assert (reply.embeddings, reply.input_tokens) == (((0.0,) * DIMENSIONS,), 0)


def embeddings_response(*vectors: object, **changes: Any) -> CreateEmbeddingResponse:
    """What the SDK hands over for a 200: built without validation."""
    fields: dict[str, Any] = {
        "object": "list",
        "model": "text-embedding-3-large",
        "data": [
            Embedding.construct(object="embedding", index=n, embedding=v)
            for n, v in enumerate(vectors)
        ],
        "usage": EmbeddingUsage.construct(prompt_tokens=9, total_tokens=9),
    }
    return CreateEmbeddingResponse.construct(**(fields | changes))


@pytest.mark.parametrize(
    "component",
    [True, False, "0.5", None, [0.5], b"0"],
    ids=["true", "false", "string", "null", "list", "bytes"],
)
def test_a_component_that_is_not_an_int_or_a_float_is_refused_before_it_is_a_float(
    component: object,
) -> None:
    # The SDK turns a JSON true into 1.0 before the adapter sees it (and does the
    # same in ``construct``), so a bool cannot come through the transport; it is
    # put into the vector afterwards, and the reader is called with it.
    response = embeddings_response(vector(0), [0.5] * DIMENSIONS)
    response.data[1].embedding[0] = component  # type: ignore[index]

    with pytest.raises(ProviderError) as raised:
        azure_openai._embedding_reply(response, inputs=2, dimensions=DIMENSIONS)

    assert (raised.value.kind, raised.value.sent) == ("bad-response", True)
    assert raised.value.__context__ is None


def test_the_reader_accepts_ints_and_floats_and_nothing_else_and_returns_floats() -> (
    None
):
    response = embeddings_response([0, 1, -2, 3] + [0.25] * (DIMENSIONS - 4))

    reply = azure_openai._embedding_reply(response, inputs=1, dimensions=DIMENSIONS)

    assert reply.embeddings == ((0.0, 1.0, -2.0, 3.0) + (0.25,) * (DIMENSIONS - 4),)
    assert all(type(x) is float for x in reply.embeddings[0])


def test_the_reader_refuses_a_response_that_is_not_the_sdks_type() -> None:
    with pytest.raises(ProviderError) as raised:
        azure_openai._embedding_reply(
            embedding_data(),  # type: ignore[arg-type]
            inputs=2,
            dimensions=DIMENSIONS,
        )

    assert raised.value.kind == "bad-response"


# ── the errors map as chat's do ──────────────────────────────────────────────
@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, "rejected"),
        (401, "auth"),
        (403, "auth"),
        (404, "unavailable"),
        (408, "timeout"),
        (422, "rejected"),
        (429, "rate-limited"),
        (500, "unavailable"),
        (503, "unavailable"),
    ],
)
def test_an_embedding_status_maps_to_its_kind_and_keeps_the_status(
    embedding_deployment: Deployment, status: int, kind: str
) -> None:
    error = embed_error_of(make_provider(raise_status(status)), embedding_deployment)

    assert (error.kind, error.status_code, error.sent) == (kind, status, True)


def test_an_embedding_400_with_the_content_filter_code_is_filtered(
    embedding_deployment: Deployment,
) -> None:
    error = embed_error_of(make_provider(azure_error()), embedding_deployment)

    assert (error.kind, error.status_code, error.sent) == ("filtered", 400, True)
    assert error.__context__ is None
    assert CANARY not in str(error) + repr(error) + repr(error.args)


def test_an_embedding_400_with_another_code_stays_rejected(
    embedding_deployment: Deployment,
) -> None:
    handler = azure_error(code="context_length_exceeded")

    error = embed_error_of(make_provider(handler), embedding_deployment)

    assert (error.kind, error.status_code) == ("rejected", 400)


@pytest.mark.parametrize(
    ("failure", "kind", "sent"),
    [
        (httpx.ConnectTimeout, "timeout", False),
        (httpx.ReadTimeout, "timeout", True),
        (httpx.ConnectError, "unavailable", False),
        (httpx.ReadError, "unavailable", True),
    ],
)
def test_an_embedding_transport_failure_says_whether_the_request_left(
    embedding_deployment: Deployment,
    failure: type[httpx.TransportError],
    kind: str,
    sent: bool,
) -> None:
    error = embed_error_of(make_provider(raising(failure)), embedding_deployment)

    assert (error.kind, error.status_code, error.sent) == (kind, None, sent)


@pytest.mark.parametrize("handler", CANARY_CASES)
def test_a_canary_in_an_embedding_failure_appears_nowhere_in_the_raised_error(
    embedding_deployment: Deployment, handler: Handler
) -> None:
    error = embed_error_of(make_provider(handler), embedding_deployment)

    assert error.__cause__ is None
    assert error.__context__ is None
    assert CANARY not in str(error) + repr(error) + repr(error.args)


def test_a_failing_token_provider_is_an_auth_error_and_no_embedding_request_is_sent(
    embedding_deployment: Deployment,
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(embed_answer(), requests, token_provider=fail_token)

    error = embed_error_of(provider, embedding_deployment)

    assert (error.kind, error.status_code, error.sent) == ("auth", None, False)
    assert error.__context__ is None
    assert requests == []


@pytest.mark.parametrize("status", [500, 429])
def test_a_failing_embedding_causes_exactly_one_request(
    embedding_deployment: Deployment, status: int
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(raise_status(status), requests)

    embed_error_of(provider, embedding_deployment)

    assert len(requests) == 1


def test_an_embedding_reply_in_error_is_a_sent_request(
    embedding_deployment: Deployment,
) -> None:
    error = embed_error_of(
        make_provider(embed_answer(data=[item(0)])), embedding_deployment
    )

    assert error.sent is True


# ── configuration errors, before any request ────────────────────────────────
@pytest.mark.parametrize(
    ("update", "word"),
    [
        ({"dimensions": None}, "dimensions"),
        ({"deployment_name": None}, "deployment_name"),
        ({"terraform_key": None}, "terraform_key"),
        ({"terraform_key": "gwc/text-embedding-3-large"}, "gwc"),
    ],
    ids=["dimensions", "deployment-name", "terraform-key", "location"],
)
def test_an_embedding_deployment_the_adapter_cannot_call_raises_before_any_request(
    embedding_deployment: Deployment, update: dict[str, Any], word: str
) -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(embed_answer(), requests)

    with pytest.raises(ValueError, match=word):
        embed(provider, embedding_deployment.model_copy(update=update))

    assert requests == []
