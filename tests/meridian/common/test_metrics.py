"""Shared metric helpers: the attribute allowlist and the meter provider (S011)."""

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from meridian.platform.common import metrics
from meridian.platform.common.metrics import (
    METRIC_ATTRIBUTE_KEYS,
    make_meter_provider,
    metric_attributes,
)

OTLP_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"


@pytest.fixture
def no_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OTLP_ENDPOINT, raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)


def test_the_allowlist_holds_exactly_the_named_keys() -> None:
    assert (
        frozenset(
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
        == METRIC_ATTRIBUTE_KEYS
    )


def test_allowlisted_attributes_are_returned_as_a_new_dict() -> None:
    given = {"meridian.tenant": "claims-triage", "gen_ai.token.type": "input"}

    returned = metric_attributes(given)

    assert returned == given
    assert returned is not given


def test_a_key_outside_the_allowlist_is_refused_and_named() -> None:
    with pytest.raises(ValueError, match=r"meridian\.run_id") as raised:
        metric_attributes({"meridian.tenant": "claims-triage", "meridian.run_id": "x"})

    assert "meridian.tenant" not in str(raised.value)


def test_a_provider_with_a_reader_records(no_endpoint: None) -> None:
    reader = InMemoryMetricReader()
    provider = make_meter_provider("model-gateway", reader)
    provider.get_meter("test").create_counter("things").add(3)

    data = reader.get_metrics_data()

    assert data is not None
    (resource_metrics,) = data.resource_metrics
    assert resource_metrics.resource.attributes["service.name"] == "model-gateway"
    (metric,) = resource_metrics.scope_metrics[0].metrics
    assert metric.data.data_points[0].value == 3


def test_the_provider_is_new_and_never_the_global_one(no_endpoint: None) -> None:
    first = make_meter_provider("a", InMemoryMetricReader())
    second = make_meter_provider("b", InMemoryMetricReader())

    assert isinstance(first, MeterProvider)
    assert first is not second


def test_without_a_reader_and_without_an_endpoint_nothing_is_exported(
    monkeypatch: pytest.MonkeyPatch, no_endpoint: None
) -> None:
    built: list[object] = []
    monkeypatch.setattr(
        metrics, "OTLPMetricExporter", lambda *a, **k: built.append((a, k))
    )

    provider = make_meter_provider("model-gateway")
    provider.get_meter("test").create_counter("things").add(1)
    provider.shutdown()  # flushes; there is nothing to flush to

    assert built == []


def test_with_an_endpoint_the_otlp_exporter_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT, "http://localhost:4318")
    built: list[object] = []

    class Recording(metrics.OTLPMetricExporter):
        def __init__(self) -> None:
            super().__init__()
            built.append(self)

    monkeypatch.setattr(metrics, "OTLPMetricExporter", Recording)

    provider = make_meter_provider("model-gateway")

    assert len(built) == 1
    provider.shutdown()


def test_an_explicit_reader_wins_over_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT, "http://localhost:4318")
    built: list[object] = []
    monkeypatch.setattr(
        metrics, "OTLPMetricExporter", lambda *a, **k: built.append((a, k))
    )

    make_meter_provider("model-gateway", InMemoryMetricReader())

    assert built == []
