"""A recorder in place of the OTLP metric exporter's ``export`` (S064).

For the tests that build a service with no meter provider injected and the
collector's address set: the service builds its own provider with the real
periodic reader and the real exporter, and the one thing replaced is the call
that would leave the machine. A test counts something, shuts the service down
(the reader exports once more as it closes) and reads what the recorder got.
"""

from typing import Any

import pytest
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics.export import MetricExportResult, MetricsData

from meridian.platform.common.telemetry import (
    OTLP_CERTIFICATE_ENV,
    OTLP_ENDPOINT_ENV,
    OTLP_PER_SIGNAL_ENVS,
)

# A local address nothing listens on: the exporter never connects, its
# ``export`` being replaced, and an ``http`` address needs no CA file.
LOCAL_ENDPOINT = "http://127.0.0.1:1"


class ExportRecorder:
    """What the exporter was asked to send."""

    def __init__(self) -> None:
        self.batches: list[MetricsData] = []

    def points(self, name: str) -> list[tuple[str, dict[str, Any], float]]:
        """Every data point of metric ``name`` in every batch: the service
        name of the batch's resource, the point's attributes and its value."""
        found: list[tuple[str, dict[str, Any], float]] = []
        for batch in self.batches:
            for resource in batch.resource_metrics:
                service = str(resource.resource.attributes.get("service.name"))
                for scope in resource.scope_metrics:
                    for metric in scope.metrics:
                        if metric.name == name:
                            found += [
                                (service, dict(p.attributes), p.value)  # type: ignore[attr-defined]
                                for p in metric.data.data_points
                            ]
        return found


def record_exports(monkeypatch: pytest.MonkeyPatch) -> ExportRecorder:
    """Point the services at a local collector address and replace the
    exporter's ``export`` by a recorder that reports success."""
    for name in (*OTLP_PER_SIGNAL_ENVS, OTLP_CERTIFICATE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(OTLP_ENDPOINT_ENV, LOCAL_ENDPOINT)
    recorder = ExportRecorder()

    def export(
        self: OTLPMetricExporter,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: Any,
    ) -> MetricExportResult:
        recorder.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    monkeypatch.setattr(OTLPMetricExporter, "export", export)
    return recorder
