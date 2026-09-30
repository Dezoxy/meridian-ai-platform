"""Tracing helpers shared by the three services.

Each service builds its own ``TracerProvider`` so that several services can
run in one test process with their own ``service.name``. Span attributes go
through an allowlist, so a claimant field or a prompt cannot be attached by
accident (T-03).
"""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import propagate
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)
from starlette.exceptions import HTTPException

OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
HTTP_SERVER_ERROR = 500

# The only attribute keys our code may set on a span: identifiers, never
# claimant fields or prompt text.
SPAN_ATTRIBUTE_KEYS: frozenset[str] = frozenset(
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


def set_span_attributes(span: Span, attributes: Mapping[str, str | int]) -> None:
    """Set allowlisted attributes; raise ``ValueError`` before setting any if
    one key is not on the allowlist."""
    refused = sorted(set(attributes) - SPAN_ATTRIBUTE_KEYS)
    if refused:
        raise ValueError(
            "not an allowed span attribute (T-03): " + ", ".join(map(repr, refused))
        )
    for key, value in attributes.items():
        span.set_attribute(key, value)


def mark_error(span: Span, error: BaseException) -> None:
    """Set the span's status to ERROR with the exception's class name.

    Never the message and never a stack trace: both can quote the claimant's
    text (an SQL error repeats the value it refused) and would bypass the
    attribute allowlist (T-03).
    """
    span.set_status(Status(StatusCode.ERROR, type(error).__name__))


@contextmanager
def start_span(tracer: Tracer, name: str) -> Iterator[Span]:
    """A current span that records a failure as a class name and nothing else.

    OpenTelemetry's default would record the exception message and stack trace
    as a span event and the message as the status description. Here an
    exception sets ERROR with its class name and propagates. An ``HTTPException``
    below 500 is an answer, not a failure, and leaves the status unset.
    """
    with tracer.start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield span
        except HTTPException as exc:
            if exc.status_code >= HTTP_SERVER_ERROR:
                mark_error(span, exc)
            raise
        except BaseException as exc:
            mark_error(span, exc)
            raise


def make_tracer_provider(
    service_name: str, exporter: SpanExporter | None = None
) -> TracerProvider:
    """A new provider for ``service_name``; never the global one.

    An explicit exporter gets a simple processor (tests read spans at once).
    Otherwise, with ``OTEL_EXPORTER_OTLP_ENDPOINT`` set, spans go to it over
    OTLP/HTTP in batches. With neither, spans are created and dropped.
    """
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif os.environ.get(OTLP_ENDPOINT_ENV):
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


def configure_propagation() -> None:
    """Propagate W3C trace context only; baggage is deliberately left out."""
    propagate.set_global_textmap(TraceContextTextMapPropagator())
