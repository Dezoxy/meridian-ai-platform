"""Tracing helpers shared by the three services.

Each service builds its own ``TracerProvider`` so that several services can
run in one test process with their own ``service.name``. Span attributes go
through an allowlist, so a claimant field or a prompt cannot be attached by
accident (T-03).
"""

import os
import ssl
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
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)
from starlette.exceptions import HTTPException

from meridian.platform.common.env import SettingsError

OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
# The CA file the OTLP exporters trust, read by the SDK itself; the chart sets it
# to the collector's authority (S063, T-90), never to the service CA.
OTLP_CERTIFICATE_ENV = "OTEL_EXPORTER_OTLP_CERTIFICATE"
# The SDK gives these precedence over the two above (an empty one counts as
# unset), so one would send the spans and metrics somewhere, or trust something,
# that the start-up check does not look at. Nothing sets them: they are refused.
OTLP_PER_SIGNAL_ENVS = (
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_METRICS_CERTIFICATE",
)
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
        "meridian.worker",
        "meridian.node",
        "meridian.step",
        "meridian.deployment",
        "meridian.provider",
        "meridian.mode",
        "meridian.sku",
        "meridian.region",
        "meridian.residency",
        "meridian.data_class",
        "meridian.refusal",
        "meridian.redactions",
        "meridian.response_schema",
        "meridian.attempt",
        "meridian.attempts",
        "meridian.skipped",
        "meridian.call_id",
        "meridian.cost_micro_eur",
        "meridian.tool",
        "meridian.tool_outcome",
        "meridian.tool_server",
        "meridian.reason",
        "error.type",
        "gen_ai.request.model",
        "gen_ai.response.model",
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


def require_otlp_ca(environ: Mapping[str, str] = os.environ) -> None:
    """Raise ``SettingsError`` when the OTLP endpoint is ``https`` and the CA
    file the exporters would trust is unset or cannot be loaded as certificates.

    Without it the SDK falls back to the system's CAs, which do not know the
    collector's authority, and every export would fail after the service had
    started. The message names the variable and never the path or the file's
    content. An endpoint that is not ``https`` (or no endpoint) needs no file.

    A per-signal endpoint or certificate variable (``OTLP_PER_SIGNAL_ENVS``) is
    refused first, whatever the generic endpoint is: the SDK would read it
    before the generic one, so what is checked here would not be what is used.
    The message names the variable and never its value.
    """
    for name in OTLP_PER_SIGNAL_ENVS:
        if environ.get(name):
            raise SettingsError(
                f"{name} is not supported: set {OTLP_ENDPOINT_ENV} and "
                f"{OTLP_CERTIFICATE_ENV}"
            )
    if not environ.get(OTLP_ENDPOINT_ENV, "").lower().startswith("https://"):
        return
    path = environ.get(OTLP_CERTIFICATE_ENV)
    if not path:
        raise SettingsError(
            f"{OTLP_CERTIFICATE_ENV} must name the collector's CA file when "
            f"{OTLP_ENDPOINT_ENV} is an https address"
        )
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cafile=path)
    except (OSError, ssl.SSLError):
        raise SettingsError(
            f"{OTLP_CERTIFICATE_ENV} cannot be loaded as a CA certificate"
        ) from None


def make_tracer_provider(
    service_name: str, exporter: SpanExporter | None = None
) -> TracerProvider:
    """A new provider for ``service_name``; never the global one.

    An explicit exporter gets a simple processor (tests read spans at once).
    Otherwise, with ``OTEL_EXPORTER_OTLP_ENDPOINT`` set, spans go to it over
    OTLP/HTTP in batches; an ``https`` endpoint needs the CA file
    (``require_otlp_ca``). With neither, spans are created and dropped.
    """
    # ALWAYS_ON, not the default parent-based sampler: a caller's ``traceparent``
    # with the sampled flag 00 must not switch tracing off for its request.
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}), sampler=ALWAYS_ON
    )
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif os.environ.get(OTLP_ENDPOINT_ENV):
        require_otlp_ca()
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


def configure_propagation() -> None:
    """Propagate W3C trace context only; baggage is deliberately left out."""
    propagate.set_global_textmap(TraceContextTextMapPropagator())
