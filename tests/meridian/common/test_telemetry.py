"""Shared tracing helpers: the attribute allowlist, providers, propagation."""

from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from opentelemetry import propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

from meridian.platform.common import telemetry
from meridian.platform.common.http import create_service_app
from meridian.platform.common.telemetry import (
    SPAN_ATTRIBUTE_KEYS,
    configure_propagation,
    make_tracer_provider,
    set_span_attributes,
    start_span,
)

OTLP_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"


class RecordingExporter(SpanExporter):
    """Stands in for the OTLP exporter; counts how often it is built."""

    instances: list["RecordingExporter"] = []  # noqa: RUF012

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []
        RecordingExporter.instances.append(self)

    def export(self, spans: object) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


@pytest.fixture(autouse=True)
def _fresh_recording_exporter() -> None:
    RecordingExporter.instances = []


@pytest.fixture
def no_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OTLP_ENDPOINT, raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)


@pytest.fixture
def restore_propagator() -> Iterator[None]:
    before = propagate.get_global_textmap()
    yield
    propagate.set_global_textmap(before)


def test_the_allowlist_holds_exactly_the_named_keys() -> None:
    assert (
        frozenset(
            {
                "meridian.claim_id",
                "meridian.run_id",
                "meridian.run_status",
                "meridian.tenant",
                "meridian.agent",
                "meridian.node",
                "meridian.deployment",
                "meridian.provider",
                "meridian.mode",
                "gen_ai.request.model",
                "gen_ai.usage.input_tokens",
                "gen_ai.usage.output_tokens",
            }
        )
        == SPAN_ATTRIBUTE_KEYS
    )


def test_allowlisted_attributes_reach_the_span(no_endpoint: None) -> None:
    exporter = InMemorySpanExporter()
    provider = make_tracer_provider("claims-api", exporter)
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("work") as span:
        set_span_attributes(
            span,
            {"meridian.claim_id": "CLM-0001", "gen_ai.usage.input_tokens": 12},
        )

    (finished,) = exporter.get_finished_spans()
    assert finished.attributes == {
        "meridian.claim_id": "CLM-0001",
        "gen_ai.usage.input_tokens": 12,
    }


@pytest.mark.parametrize(
    "key", ["claimant.name", "gen_ai.prompt", "meridian.prompt", "http.url", ""]
)
def test_a_key_outside_the_allowlist_is_refused(key: str, no_endpoint: None) -> None:
    exporter = InMemorySpanExporter()
    provider = make_tracer_provider("claims-api", exporter)
    tracer = provider.get_tracer("test")

    with (
        tracer.start_as_current_span("work") as span,
        pytest.raises(ValueError, match="not an allowed span attribute"),
    ):
        set_span_attributes(span, {"meridian.claim_id": "CLM-0001", key: "x"})

    (finished,) = exporter.get_finished_spans()
    assert finished.attributes == {}  # nothing is set when any key is refused


def test_each_provider_carries_its_own_service_name(no_endpoint: None) -> None:
    api_exporter, gateway_exporter = InMemorySpanExporter(), InMemorySpanExporter()
    api = make_tracer_provider("claims-api", api_exporter)
    gateway = make_tracer_provider("model-gateway", gateway_exporter)

    with api.get_tracer("t").start_as_current_span("a"):
        pass
    with gateway.get_tracer("t").start_as_current_span("b"):
        pass

    (api_span,) = api_exporter.get_finished_spans()
    (gateway_span,) = gateway_exporter.get_finished_spans()
    assert api_span.resource.attributes["service.name"] == "claims-api"
    assert gateway_span.resource.attributes["service.name"] == "model-gateway"


def test_the_provider_is_new_and_never_the_global_one(no_endpoint: None) -> None:
    from opentelemetry import trace

    first = make_tracer_provider("a")
    second = make_tracer_provider("a")

    assert first is not second
    assert first is not trace.get_tracer_provider()


def test_without_an_endpoint_and_an_exporter_nothing_is_exported(
    monkeypatch: pytest.MonkeyPatch, no_endpoint: None
) -> None:
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", RecordingExporter)

    provider = make_tracer_provider("claims-api")
    with provider.get_tracer("t").start_as_current_span("work"):
        pass
    provider.shutdown()

    assert RecordingExporter.instances == []


def test_with_an_endpoint_the_otlp_exporter_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT, "http://localhost:4318")
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", RecordingExporter)

    provider = make_tracer_provider("claims-api")
    with provider.get_tracer("t").start_as_current_span("work"):
        pass
    provider.shutdown()  # flushes the batch processor

    (exporter,) = RecordingExporter.instances
    assert [s.name for s in exporter.spans] == ["work"]


def test_an_explicit_exporter_wins_over_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT, "http://localhost:4318")
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", RecordingExporter)
    exporter = InMemorySpanExporter()

    provider = make_tracer_provider("claims-api", exporter)
    with provider.get_tracer("t").start_as_current_span("work"):
        pass

    assert RecordingExporter.instances == []
    # A simple processor exports at span end, without a flush.
    assert [s.name for s in exporter.get_finished_spans()] == ["work"]


def test_propagation_is_w3c_trace_context_only(restore_propagator: None) -> None:
    propagate.set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )

    configure_propagation()

    assert isinstance(propagate.get_global_textmap(), TraceContextTextMapPropagator)
    assert propagate.get_global_textmap().fields == {"traceparent", "tracestate"}


# ── start_span: a failure marks the span without leaking its message (T-03) ──
CANARY = "canary-claimant@example.invalid"


def one_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    (span,) = exporter.get_finished_spans()
    return span


def test_start_span_makes_the_span_current_and_leaves_a_clean_one_unset(
    no_endpoint: None,
) -> None:
    exporter = InMemorySpanExporter()
    tracer = make_tracer_provider("claims-api", exporter).get_tracer("t")

    with start_span(tracer, "work") as span:
        assert trace.get_current_span() is span

    assert one_span(exporter).status.status_code is StatusCode.UNSET


def test_an_exception_sets_error_with_the_class_name_only_and_is_reraised(
    no_endpoint: None,
) -> None:
    exporter = InMemorySpanExporter()
    tracer = make_tracer_provider("claims-api", exporter).get_tracer("t")

    with pytest.raises(ValueError, match=CANARY), start_span(tracer, "work"):
        raise ValueError(CANARY)

    span = one_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "ValueError"
    assert span.events == ()  # no "exception" event with message or stack trace


def test_a_client_error_is_not_a_span_error(no_endpoint: None) -> None:
    exporter = InMemorySpanExporter()
    tracer = make_tracer_provider("claims-api", exporter).get_tracer("t")

    with pytest.raises(HTTPException), start_span(tracer, "work"):
        raise HTTPException(status_code=409, detail=CANARY)

    span = one_span(exporter)
    assert span.status.status_code is StatusCode.UNSET
    assert span.events == ()


def test_a_server_error_answer_is_a_span_error_without_its_detail(
    no_endpoint: None,
) -> None:
    exporter = InMemorySpanExporter()
    tracer = make_tracer_provider("claims-api", exporter).get_tracer("t")

    with pytest.raises(HTTPException), start_span(tracer, "work"):
        raise HTTPException(status_code=503, detail=CANARY)

    span = one_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "HTTPException"
    assert span.events == ()


def test_a_caller_cannot_switch_tracing_off_with_an_unsampled_traceparent(
    no_endpoint: None, restore_propagator: None
) -> None:
    exporter = InMemorySpanExporter()
    provider = make_tracer_provider("claims-api", exporter)
    service = create_service_app(
        title="t",
        description="d",
        service_name="claims-api",
        tracer_name="t",
        max_body_bytes=1024,
        tracer_provider=provider,
    )
    unsampled = f"00-{'a' * 32}-{'b' * 16}-00"

    answer = TestClient(service.app).get("/healthz", headers={"traceparent": unsampled})

    assert answer.status_code == 200
    spans = exporter.get_finished_spans()
    assert spans, "the unsampled flag of the caller switched tracing off"
    assert {f"{s.context.trace_id:032x}" for s in spans} == {"a" * 32}
