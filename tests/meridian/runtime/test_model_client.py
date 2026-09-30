"""The runtime's gateway client sets the identity headers itself."""

import json
import uuid

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
from meridian.runtime.model_client import (
    ChatResult,
    ModelCallError,
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


def model(http: httpx.Client) -> ModelClient:
    return ModelClient(
        http, tenant="claims-triage", agent="claims-triage", run_id=RUN_ID
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
    )


def test_a_non_2xx_answer_raises_with_the_status_code_only() -> None:
    http, _ = client_for(status=403)

    with pytest.raises(ModelCallError) as raised:
        model(http).chat([{"role": "user", "content": "secret claimant text"}])

    assert raised.value.status_code == 403
    assert "secret claimant text" not in str(raised.value)
    assert str(raised.value) == "model gateway answered 403"


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
