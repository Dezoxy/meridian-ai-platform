"""The runtime's gateway client sets the identity headers itself."""

import json
import subprocess
import sys
import uuid
from typing import get_args

import httpx
import pytest
from opentelemetry import propagate
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import GATEWAY_REPLY

from meridian.platform.common.telemetry import (
    configure_propagation,
    make_tracer_provider,
)
from meridian.platform.gateway.app import (
    REFUSAL_CONTENT_FILTER as GATEWAY_REFUSAL_CONTENT_FILTER,
)
from meridian.platform.gateway.app import REFUSAL_HEADER as GATEWAY_REFUSAL_HEADER
from meridian.platform.gateway.models import ChatOutput, ChatResponse, Usage
from meridian.platform.registry.models import DataClass
from meridian.runtime.model_client import (
    REFUSAL_CONTENT_FILTER,
    REFUSAL_HEADER,
    ChatResult,
    ModelCallError,
    ModelCallFilteredError,
    ModelCallLimitError,
    ModelCallTimeoutError,
    ModelClient,
)

RUN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000aa")


def client_for(
    handler: httpx.MockTransport | None = None, status: int = 200
) -> tuple[httpx.Client, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=GATEWAY_REPLY if status == 200 else {})

    http = httpx.Client(
        base_url="http://gateway.invalid",
        transport=handler or httpx.MockTransport(respond),
    )
    return http, seen


def model(http: httpx.Client, max_calls: int = 10) -> ModelClient:
    return ModelClient(
        http,
        tenant="claims-triage",
        agent="claims-triage",
        run_id=RUN_ID,
        max_calls=max_calls,
    )


def test_chat_posts_the_messages_with_the_three_identity_headers() -> None:
    http, seen = client_for()

    result = model(http).chat([{"role": "user", "content": "hi"}])

    (request,) = seen
    assert (request.method, request.url.path) == ("POST", "/v1/chat")
    assert request.headers["X-Meridian-Tenant"] == "claims-triage"
    assert request.headers["X-Meridian-Agent"] == "claims-triage"
    assert request.headers["X-Meridian-Run"] == str(RUN_ID)
    assert json.loads(request.content) == {
        "messages": [{"role": "user", "content": "hi"}]
    }
    assert result == ChatResult(
        text="drafted",
        deployment="replay-chat",
        provider="replay",
        model="replay-chat",
        mode="replay",
        input_tokens=1,
        output_tokens=1,
        finish_reason="stop",
    )


def test_max_output_tokens_is_sent_when_given_and_left_out_when_not() -> None:
    http, seen = client_for()
    client = model(http)

    client.chat([{"role": "user", "content": "hi"}], max_output_tokens=256)
    client.chat([{"role": "user", "content": "hi"}])

    given, omitted = (json.loads(request.content) for request in seen)
    assert given == {
        "messages": [{"role": "user", "content": "hi"}],
        "max_output_tokens": 256,
    }
    assert omitted == {"messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_the_finish_reason_of_the_reply_is_in_the_result(finish_reason: str) -> None:
    reply = {**GATEWAY_REPLY, "output": {"text": "t", "finish_reason": finish_reason}}

    result = model(reply_client(reply)).chat([{"role": "user", "content": "hi"}])

    assert result.finish_reason == finish_reason


@pytest.mark.parametrize(
    "output",
    [
        {"text": "t"},
        {"text": "t", "finish_reason": "content_filter"},
        {"text": "t", "finish_reason": None},
    ],
)
def test_a_reply_without_a_known_finish_reason_is_no_usable_answer(
    output: dict,
) -> None:
    reply = {**GATEWAY_REPLY, "output": output}

    with pytest.raises(ModelCallError) as raised:
        model(reply_client(reply)).chat([{"role": "user", "content": "hi"}])

    assert raised.value.status_code == 0
    assert not isinstance(raised.value, ModelCallLimitError)


def test_the_call_after_the_limit_sends_nothing_and_raises() -> None:
    http, seen = client_for()
    client = model(http, max_calls=2)
    client.chat([{"role": "user", "content": "one"}])
    client.chat([{"role": "user", "content": "two"}])

    with pytest.raises(ModelCallLimitError) as raised:
        client.chat([{"role": "user", "content": "secret claimant text"}])

    assert len(seen) == 2
    assert isinstance(raised.value, ModelCallError)
    assert raised.value.status_code == 0
    assert str(raised.value) == "model call limit of the run reached"


def test_a_call_that_fails_counts_toward_the_limit() -> None:
    http, seen = client_for(status=500)
    client = model(http, max_calls=1)
    with pytest.raises(ModelCallError):
        client.chat([{"role": "user", "content": "one"}])

    with pytest.raises(ModelCallLimitError):
        client.chat([{"role": "user", "content": "two"}])

    assert len(seen) == 1


def test_a_non_2xx_answer_raises_with_the_status_code_only() -> None:
    http, _ = client_for(status=403)

    with pytest.raises(ModelCallError) as raised:
        model(http).chat([{"role": "user", "content": "secret claimant text"}])

    assert raised.value.status_code == 403
    assert "secret claimant text" not in str(raised.value)
    assert str(raised.value) == "model gateway answered 403"


@pytest.mark.parametrize("data_class", get_args(DataClass))
def test_the_data_class_is_sent_as_a_header_when_given(data_class: str) -> None:
    http, seen = client_for()

    model(http).chat([{"role": "user", "content": "hi"}], data_class=data_class)

    (request,) = seen
    assert request.headers["X-Meridian-Data-Class"] == data_class
    assert request.headers["X-Meridian-Tenant"] == "claims-triage"
    assert json.loads(request.content) == {
        "messages": [{"role": "user", "content": "hi"}]
    }


def test_no_data_class_header_is_sent_when_none_is_given() -> None:
    http, seen = client_for()
    client = model(http)

    client.chat([{"role": "user", "content": "hi"}])
    client.chat([{"role": "user", "content": "hi"}], data_class=None)

    assert all("X-Meridian-Data-Class" not in request.headers for request in seen)
    assert len(seen) == 2


FILTER_HEADERS = {REFUSAL_HEADER: REFUSAL_CONTENT_FILTER}


def test_the_runtime_marker_of_a_filtered_400_is_the_gateways() -> None:
    # The runtime does not import the gateway, so it keeps its own copy.
    assert (REFUSAL_HEADER, REFUSAL_CONTENT_FILTER) == (
        GATEWAY_REFUSAL_HEADER,
        GATEWAY_REFUSAL_CONTENT_FILTER,
    )
    assert (REFUSAL_HEADER, REFUSAL_CONTENT_FILTER) == (
        "X-Meridian-Refusal",
        "content-filter",
    )


def test_a_400_with_the_refusal_header_is_a_filtered_call_and_a_model_call_error() -> (
    None
):
    http = httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={}, headers=FILTER_HEADERS)
        ),
    )

    with pytest.raises(ModelCallFilteredError) as raised:
        model(http).chat([{"role": "user", "content": "hi"}])

    assert isinstance(raised.value, ModelCallError)
    assert raised.value.status_code == 400


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {REFUSAL_HEADER: "something-else"},
        {REFUSAL_HEADER: ""},
        {REFUSAL_HEADER: "Content-Filter"},
        {"X-Other": REFUSAL_CONTENT_FILTER},
    ],
)
def test_a_400_without_the_exact_refusal_header_is_a_plain_model_call_error(
    headers: dict[str, str],
) -> None:
    http = httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={}, headers=headers)
        ),
    )

    with pytest.raises(ModelCallError) as raised:
        model(http).chat([{"role": "user", "content": "hi"}])

    assert type(raised.value) is ModelCallError
    assert raised.value.status_code == 400


def test_a_filtered_call_keeps_no_body_message_or_cause() -> None:
    canary = "CANARY claimant text"
    http = httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                400, json={"detail": canary}, headers=FILTER_HEADERS
            )
        ),
    )

    with pytest.raises(ModelCallFilteredError) as raised:
        model(http).chat([{"role": "user", "content": "hi"}])

    error = raised.value
    assert canary not in str(error)
    assert canary not in repr(error.args)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__


@pytest.mark.parametrize("status", [403, 413, 422, 429, 500, 502, 503, 504])
def test_every_other_non_2xx_stays_a_plain_model_call_error(status: int) -> None:
    http, _ = client_for(status=status)

    with pytest.raises(ModelCallError) as raised:
        model(http).chat([{"role": "user", "content": "hi"}])

    assert type(raised.value) is ModelCallError
    assert raised.value.status_code == status


def reply_client(reply: dict) -> httpx.Client:
    return httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=reply)),
    )


def test_a_live_reply_with_a_finish_reason_and_an_unknown_field_parses() -> None:
    reply = {
        **GATEWAY_REPLY,
        "mode": "live",
        "deployment": "aoai-sdc-gpt-4o",
        "provider": "azure-openai",
        "model": "gpt-4o",
        "output": {"text": "drafted", "finish_reason": "length", "added_later": 1},
        "added_later": {"nested": True},
    }

    result = model(reply_client(reply)).chat([{"role": "user", "content": "hi"}])

    assert result == ChatResult(
        text="drafted",
        deployment="aoai-sdc-gpt-4o",
        provider="azure-openai",
        model="gpt-4o",
        mode="live",
        input_tokens=1,
        output_tokens=1,
        finish_reason="length",
    )


def test_a_mode_the_runtime_does_not_know_is_no_usable_answer() -> None:
    # The stored proposal records the mode (T-39), so an unknown one is
    # refused instead of being written down as provenance.
    reply = {**GATEWAY_REPLY, "mode": "shadow"}

    with pytest.raises(ModelCallError) as caught:
        model(reply_client(reply)).chat([{"role": "user", "content": "hi"}])

    assert caught.value.status_code == 0


@pytest.mark.parametrize("mode", ["replay", "live"])
def test_what_the_gateway_answers_is_what_the_runtime_parses(mode: str) -> None:
    # The runtime keeps its own copy of the contract; this is the one place
    # the two sides meet, so a renamed field fails here and not in a demo.
    answered = ChatResponse(
        call_id=uuid.uuid4(),
        mode=mode,
        deployment="aoai-sdc-gpt-4o",
        provider="azure-openai",
        model="gpt-4o",
        output=ChatOutput(text="drafted", finish_reason="stop"),
        usage=Usage(input_tokens=7, output_tokens=3),
    ).model_dump(mode="json")

    result = model(reply_client(answered)).chat([{"role": "user", "content": "hi"}])

    assert result == ChatResult(
        text="drafted",
        deployment="aoai-sdc-gpt-4o",
        provider="azure-openai",
        model="gpt-4o",
        mode=mode,
        input_tokens=7,
        output_tokens=3,
        finish_reason="stop",
    )


def test_a_reply_without_the_output_text_is_still_no_usable_answer() -> None:
    reply = {**GATEWAY_REPLY, "output": {"finish_reason": "stop"}}

    with pytest.raises(ModelCallError) as raised:
        model(reply_client(reply)).chat([{"role": "user", "content": "hi"}])

    assert raised.value.status_code == 0


@pytest.mark.parametrize(
    "missing", ["deployment", "provider", "model", "mode", "usage"]
)
def test_a_reply_missing_a_field_the_result_needs_is_an_error(missing: str) -> None:
    reply = {k: v for k, v in GATEWAY_REPLY.items() if k != missing}

    with pytest.raises(ModelCallError) as raised:
        model(reply_client(reply)).chat([{"role": "user", "content": "hi"}])

    assert raised.value.status_code == 0


def test_the_runtime_does_not_import_the_gateway_package() -> None:
    # A fresh interpreter, so no earlier test has loaded the gateway.
    code = (
        "import sys\n"
        "import meridian.runtime.app\n"
        "import meridian.runtime.model_client\n"
        "loaded = sorted(m for m in sys.modules "
        "if m == 'meridian.platform.gateway' "
        "or m.startswith('meridian.platform.gateway.'))\n"
        "print('loaded:' + ','.join(loaded))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "loaded:", completed.stdout


def test_a_reply_that_is_not_the_contract_is_an_error() -> None:
    http = httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"x": 1})),
    )

    with pytest.raises(ModelCallError):
        model(http).chat([{"role": "user", "content": "hi"}])


def test_the_current_trace_context_travels_in_the_request() -> None:
    configure_propagation()
    exporter = InMemorySpanExporter()
    tracer = make_tracer_provider("agent-runtime", exporter).get_tracer("t")
    http, seen = client_for()

    with tracer.start_as_current_span("node") as span:
        model(http).chat([{"role": "user", "content": "hi"}])

    carried = propagate.extract({"traceparent": seen[0].headers["traceparent"]})
    from opentelemetry import trace

    assert trace.get_current_span(carried).get_span_context().trace_id == (
        span.get_span_context().trace_id
    )


def unreachable(error: Exception) -> httpx.Client:
    def fail(_: httpx.Request) -> httpx.Response:
        raise error

    return httpx.Client(
        base_url="http://gateway.invalid", transport=httpx.MockTransport(fail)
    )


def test_a_timeout_is_a_model_call_timeout_without_the_transport_message() -> None:
    http = unreachable(httpx.ReadTimeout("slow password=hunter2"))

    with pytest.raises(ModelCallTimeoutError) as raised:
        model(http).chat([{"role": "user", "content": "hi"}])

    assert isinstance(raised.value, ModelCallError)
    assert raised.value.status_code == 0
    assert "hunter2" not in str(raised.value)


def test_any_other_transport_failure_is_a_model_call_error_with_no_status() -> None:
    http = unreachable(httpx.ConnectError("refused password=hunter2"))

    with pytest.raises(ModelCallError) as raised:
        model(http).chat([{"role": "user", "content": "hi"}])

    assert not isinstance(raised.value, ModelCallTimeoutError)
    assert raised.value.status_code == 0
    assert "hunter2" not in str(raised.value)
