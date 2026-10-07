"""The runtime's gateway client sets the identity headers itself."""

import json
import logging
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
    COMPLETION_HEADER as GATEWAY_COMPLETION_HEADER,
)
from meridian.platform.gateway.app import (
    COMPLETION_WITHHELD as GATEWAY_COMPLETION_WITHHELD,
)
from meridian.platform.gateway.app import (
    DEPLOYMENT_HEADER as GATEWAY_DEPLOYMENT_HEADER,
)
from meridian.platform.gateway.app import MODE_HEADER as GATEWAY_MODE_HEADER
from meridian.platform.gateway.app import PROVIDER_HEADER as GATEWAY_PROVIDER_HEADER
from meridian.platform.gateway.app import (
    REFUSAL_CONTENT_FILTER as GATEWAY_REFUSAL_CONTENT_FILTER,
)
from meridian.platform.gateway.app import REFUSAL_HEADER as GATEWAY_REFUSAL_HEADER
from meridian.platform.gateway.models import ChatOutput, ChatResponse, Usage
from meridian.platform.registry.models import DataClass
from meridian.runtime.model_client import (
    COMPLETION_HEADER,
    COMPLETION_WITHHELD,
    DEPLOYMENT_HEADER,
    MODE_HEADER,
    PROVIDER_HEADER,
    REFUSAL_CONTENT_FILTER,
    REFUSAL_HEADER,
    ChatResult,
    Drafter,
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


def test_response_schema_is_sent_when_given_and_left_out_when_not() -> None:
    http, seen = client_for()
    client = model(http)
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}

    client.chat([{"role": "user", "content": "hi"}], response_schema=schema)
    client.chat([{"role": "user", "content": "hi"}])

    given, omitted = (json.loads(request.content) for request in seen)
    assert given == {
        "messages": [{"role": "user", "content": "hi"}],
        "response_schema": schema,
    }
    assert "response_schema" not in omitted


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


WITHHELD_HEADERS = {
    REFUSAL_HEADER: REFUSAL_CONTENT_FILTER,
    COMPLETION_HEADER: COMPLETION_WITHHELD,
    DEPLOYMENT_HEADER: "aoai-sdc-gpt-4o",
    PROVIDER_HEADER: "azure-openai",
    MODE_HEADER: "live",
}


def refusing_client(
    headers: dict[str, str], status: int = 400, body: object | None = None
) -> httpx.Client:
    return httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status, json={} if body is None else body, headers=headers
            )
        ),
    )


def test_the_runtime_marker_of_a_withheld_completion_is_the_gateways() -> None:
    assert (
        COMPLETION_HEADER,
        COMPLETION_WITHHELD,
        DEPLOYMENT_HEADER,
        PROVIDER_HEADER,
        MODE_HEADER,
    ) == (
        GATEWAY_COMPLETION_HEADER,
        GATEWAY_COMPLETION_WITHHELD,
        GATEWAY_DEPLOYMENT_HEADER,
        GATEWAY_PROVIDER_HEADER,
        GATEWAY_MODE_HEADER,
    )
    assert (
        COMPLETION_HEADER,
        COMPLETION_WITHHELD,
        DEPLOYMENT_HEADER,
        PROVIDER_HEADER,
        MODE_HEADER,
    ) == (
        "X-Meridian-Completion",
        "withheld",
        "X-Meridian-Deployment",
        "X-Meridian-Provider",
        "X-Meridian-Mode",
    )


def test_a_client_of_the_refusal_header_alone_reads_a_withheld_one_as_filtered() -> (
    None
):
    # Built from the refusal header's constant alone, as a runtime from before
    # the second header reads a 400: no other header is looked at. This test
    # restates that reading and is close to a tautology on its own; the proof
    # that the gateway's withheld answer still carries ``content-filter`` is
    # ``test_a_refused_prompt_names_no_deployment_and_a_withheld_completion_names_it``
    # in ``tests/meridian/gateway/test_gateway_guardrails.py``.
    answer = httpx.Response(400, json={}, headers=WITHHELD_HEADERS)

    reads_a_filtered_call = (
        answer.status_code == 400
        and answer.headers.get(REFUSAL_HEADER) == REFUSAL_CONTENT_FILTER
    )

    assert reads_a_filtered_call
    assert COMPLETION_HEADER in answer.headers  # the second mark is only beside it


@pytest.mark.parametrize("value", ["", "Withheld", "refused", "withheld "])
def test_a_completion_header_with_any_other_value_is_a_filtered_call_with_no_mark(
    value: str,
) -> None:
    headers = {**WITHHELD_HEADERS, COMPLETION_HEADER: value}

    with pytest.raises(ModelCallFilteredError) as raised:
        model(refusing_client(headers)).chat([{"role": "user", "content": "hi"}])

    assert raised.value.withheld is False
    assert raised.value.drafter is None


def test_a_withheld_completion_is_a_filtered_call_with_the_deployment_named() -> None:
    seen: list[tuple[str, str | None]] = []
    client = ModelClient(
        refusing_client(WITHHELD_HEADERS),
        tenant="claims-triage",
        agent="claims-triage",
        run_id=RUN_ID,
        max_calls=1,
        on_call=lambda outcome, reason: seen.append((outcome, reason)),
    )

    with pytest.raises(ModelCallFilteredError) as raised:
        client.chat([{"role": "user", "content": "hi"}])

    assert raised.value.status_code == 400
    assert raised.value.withheld is True
    assert raised.value.drafter == Drafter("aoai-sdc-gpt-4o", "azure-openai", "live")
    assert seen == [("failed", "filtered")]


def test_a_refused_prompt_is_a_filtered_call_that_names_no_deployment() -> None:
    headers = {
        k: v for k, v in WITHHELD_HEADERS.items() if k != COMPLETION_HEADER
    }  # the provenance headers alone mark nothing

    with pytest.raises(ModelCallFilteredError) as raised:
        model(refusing_client(headers)).chat([{"role": "user", "content": "hi"}])

    assert raised.value.withheld is False
    assert raised.value.drafter is None


@pytest.mark.parametrize(
    "dropped",
    [
        pytest.param({DEPLOYMENT_HEADER: "Not An ID"}, id="deployment-with-spaces"),
        pytest.param({DEPLOYMENT_HEADER: "AOAI"}, id="deployment-in-capitals"),
        pytest.param({DEPLOYMENT_HEADER: "a" * 65}, id="deployment-too-long"),
        pytest.param({DEPLOYMENT_HEADER: ""}, id="deployment-empty"),
        pytest.param({PROVIDER_HEADER: "azure openai"}, id="provider-with-a-space"),
        pytest.param({PROVIDER_HEADER: "-azure"}, id="provider-leading-hyphen"),
        pytest.param({MODE_HEADER: "production"}, id="mode-outside-the-three"),
        pytest.param({MODE_HEADER: "Live"}, id="mode-in-capitals"),
    ],
)
def test_a_provenance_header_outside_its_pattern_is_dropped_not_carried(
    dropped: dict[str, str],
) -> None:
    headers = {**WITHHELD_HEADERS, **dropped}

    with pytest.raises(ModelCallFilteredError) as raised:
        model(refusing_client(headers)).chat([{"role": "user", "content": "hi"}])

    assert raised.value.withheld is True
    assert raised.value.drafter is None


@pytest.mark.parametrize("missing", [DEPLOYMENT_HEADER, PROVIDER_HEADER, MODE_HEADER])
def test_a_withheld_completion_with_a_header_missing_names_no_deployment(
    missing: str,
) -> None:
    headers = {k: v for k, v in WITHHELD_HEADERS.items() if k != missing}

    with pytest.raises(ModelCallFilteredError) as raised:
        model(refusing_client(headers)).chat([{"role": "user", "content": "hi"}])

    assert raised.value.withheld is True
    assert raised.value.drafter is None


def client_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """What the client itself logged: httpx logs each request at INFO."""
    return [r for r in caplog.records if r.name == "meridian.runtime.model_client"]


def test_a_provenance_header_outside_its_pattern_is_said_once_without_its_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "CANARY claimant text"
    headers = {**WITHHELD_HEADERS, DEPLOYMENT_HEADER: canary}

    with caplog.at_level(logging.DEBUG), pytest.raises(ModelCallFilteredError):
        model(refusing_client(headers, body={"detail": canary})).chat(
            [{"role": "user", "content": "hi"}]
        )

    (record,) = client_records(caplog)
    assert record.levelno == logging.WARNING
    assert "ValidationError" in record.getMessage()
    assert canary not in record.getMessage() + repr(record.args)
    assert record.exc_info is None
    assert canary not in caplog.text


def test_a_withheld_completion_with_good_headers_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG), pytest.raises(ModelCallFilteredError):
        model(refusing_client(WITHHELD_HEADERS)).chat(
            [{"role": "user", "content": "hi"}]
        )

    assert client_records(caplog) == []


def test_a_provider_word_in_a_header_is_never_kept_by_the_error() -> None:
    canary = "CANARY claimant text"
    headers = {**WITHHELD_HEADERS, DEPLOYMENT_HEADER: canary}

    with pytest.raises(ModelCallFilteredError) as raised:
        model(refusing_client(headers, body={"detail": canary})).chat(
            [{"role": "user", "content": "hi"}]
        )

    error = raised.value
    assert canary not in str(error) + repr(error.args) + repr(error.drafter)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_the_withheld_value_on_a_status_other_than_400_is_a_plain_error() -> None:
    with pytest.raises(ModelCallError) as raised:
        model(refusing_client(WITHHELD_HEADERS, status=502)).chat(
            [{"role": "user", "content": "hi"}]
        )

    assert type(raised.value) is ModelCallError
    assert raised.value.status_code == 502


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


@pytest.mark.parametrize(
    "named",
    [
        pytest.param({"deployment": "Not An ID"}, id="deployment-with-spaces"),
        pytest.param({"deployment": "AOAI"}, id="deployment-in-capitals"),
        pytest.param({"deployment": "a" * 65}, id="deployment-too-long"),
        pytest.param({"deployment": ""}, id="deployment-empty"),
        pytest.param({"provider": "azure openai"}, id="provider-with-a-space"),
        pytest.param({"provider": "-azure"}, id="provider-leading-hyphen"),
        pytest.param({"provider": "a" * 65}, id="provider-too-long"),
        pytest.param({"provider": ""}, id="provider-empty"),
    ],
)
def test_a_reply_naming_a_deployment_outside_the_id_pattern_is_no_usable_answer(
    named: dict[str, str],
) -> None:
    # The stored proposal records both words as provenance, so a success is
    # held to the same bounded words as a withheld completion's headers.
    calls: list[tuple[str, str | None]] = []
    client = ModelClient(
        reply_client({**GATEWAY_REPLY, **named}),
        tenant="claims-triage",
        agent="claims-triage",
        run_id=uuid.uuid4(),
        max_calls=10,
        on_call=lambda outcome, reason: calls.append((outcome, reason)),
    )

    with pytest.raises(ModelCallError) as raised:
        client.chat([{"role": "user", "content": "hi"}])

    assert raised.value.status_code == 0
    assert calls == [("failed", "error")]


def test_a_mode_the_runtime_does_not_know_is_no_usable_answer() -> None:
    # The stored proposal records the mode (T-39), so an unknown one is
    # refused instead of being written down as provenance.
    reply = {**GATEWAY_REPLY, "mode": "shadow"}

    with pytest.raises(ModelCallError) as caught:
        model(reply_client(reply)).chat([{"role": "user", "content": "hi"}])

    assert caught.value.status_code == 0


@pytest.mark.parametrize("mode", ["replay", "recorded", "live"])
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
