"""Metric helpers shared by the services, in the style of ``telemetry.py``.

Each service builds its own ``MeterProvider`` so that several services can run
in one test process with their own ``service.name``. Metric attributes go
through an allowlist like span attributes do, so a header value, a claimant
field or a prompt cannot become a label by accident (T-03, T-49).
"""

import functools
import logging
import os
from collections.abc import Callable, Mapping

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from meridian.platform.common.telemetry import OTLP_ENDPOINT_ENV, require_otlp_ca

logger = logging.getLogger(__name__)

# The only attribute keys our code may put on a metric: registry identifiers and
# fixed words, never a caller-supplied value. ``meridian.tool`` is a registry
# tool's ID, ``meridian.finding`` a fixed word of the code that counts it.
METRIC_ATTRIBUTE_KEYS: frozenset[str] = frozenset(
    {
        "meridian.tenant",
        "meridian.agent",
        "meridian.provider",
        "gen_ai.request.model",
        "gen_ai.token.type",
        "meridian.outcome",
        "meridian.reason",
        "meridian.tool",
        "meridian.finding",
    }
)


def metric_attributes(attributes: Mapping[str, str]) -> dict[str, str]:
    """The attributes, or ``ValueError`` naming a key that is not on the
    allowlist."""
    refused = sorted(set(attributes) - METRIC_ATTRIBUTE_KEYS)
    if refused:
        raise ValueError(
            "not an allowed metric attribute (T-49): " + ", ".join(map(repr, refused))
        )
    return dict(attributes)


def make_meter_provider(
    service_name: str, reader: MetricReader | None = None
) -> MeterProvider:
    """A new provider for ``service_name``; never the global one.

    An explicit reader is used as it is (tests pass an ``InMemoryMetricReader``).
    Otherwise, with ``OTEL_EXPORTER_OTLP_ENDPOINT`` set, metrics go to it over
    OTLP/HTTP from a periodic reader; an ``https`` endpoint needs the CA file
    (``require_otlp_ca``). With neither, nothing is exported, and one INFO line
    says so: a service whose metrics go nowhere is not a quiet service.
    """
    readers: list[MetricReader] = []
    if reader is not None:
        readers.append(reader)
    elif os.environ.get(OTLP_ENDPOINT_ENV):
        require_otlp_ca()
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
    else:
        # Not a silent no-op: a chart that leaves the address out is visible.
        logger.info(
            "%s: metrics are not exported: %s is not set",
            service_name,
            OTLP_ENDPOINT_ENV,
        )
    return MeterProvider(
        resource=Resource.create({"service.name": service_name}),
        metric_readers=readers,
    )


def counted_safely[**P](count: Callable[P, None]) -> Callable[P, None]:
    """Wrap a function that records a metric so that it never raises into
    the work it counts: a failure is one WARNING with the exception's class
    and the function's name, and the work goes on."""

    @functools.wraps(count)
    def safe(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            count(*args, **kwargs)
        except Exception as exc:
            logger.warning(
                "a metric was not recorded: %s in %s",
                type(exc).__name__,
                count.__qualname__,
            )

    return safe
