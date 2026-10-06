"""Shared metric helpers: the attribute allowlist and the meter provider (S011)."""

import logging

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from meridian.platform.common import metrics
from meridian.platform.common.metrics import (
    METRIC_ATTRIBUTE_KEYS,
    counted_safely,
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


# ── no silent no-op: a provider without a reader says so, once ──────────────
def test_a_provider_without_a_reader_or_an_endpoint_says_so_once_at_info(
    caplog: pytest.LogCaptureFixture, no_endpoint: None
) -> None:
    with caplog.at_level(logging.INFO, logger=metrics.__name__):
        make_meter_provider("model-gateway")

    (record,) = [r for r in caplog.records if r.name == metrics.__name__]
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        "model-gateway: metrics are not exported: "
        "OTEL_EXPORTER_OTLP_ENDPOINT is not set"
    )


def test_a_provider_with_a_reader_or_an_endpoint_says_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT, "http://localhost:4318")

    with caplog.at_level(logging.INFO, logger=metrics.__name__):
        make_meter_provider("model-gateway").shutdown()  # nothing to flush
        make_meter_provider("model-gateway", InMemoryMetricReader())

    assert [r for r in caplog.records if r.name == metrics.__name__] == []


# ── counted_safely: a metric never fails the work it counts ─────────────────
def test_a_wrapped_function_is_called_with_its_arguments() -> None:
    seen: list[tuple[tuple[int, ...], dict[str, str]]] = []

    @counted_safely
    def count(*args: int, **kwargs: str) -> None:
        seen.append((args, kwargs))

    count(1, 2, key="value")

    assert seen == [((1, 2), {"key": "value"})]
    assert count.__name__ == "count"


def test_a_wrapped_function_that_raises_logs_one_warning_and_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Meter:
        @counted_safely
        def leg_ended(self) -> None:
            raise ValueError("a claimant's text must not reach the log")

    with caplog.at_level(logging.WARNING, logger=metrics.__name__):
        returned = Meter().leg_ended()

    (record,) = caplog.records
    assert returned is None
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "a metric was not recorded: ValueError in "
        "test_a_wrapped_function_that_raises_logs_one_warning_and_returns."
        "<locals>.Meter.leg_ended"
    )
    assert "claimant" not in caplog.text


def test_a_wrapped_function_does_not_hide_an_exit_that_is_not_an_exception() -> None:
    @counted_safely
    def count() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        count()


# ── The resource's instance ID and name under the environment (S064, C3) ─────
# The sweep is a process that lives seconds, so by default each pass is a new
# set of series under a random ``service.instance.id``. The chart gives the
# CronJob ``OTEL_RESOURCE_ATTRIBUTES=service.instance.id=claims-sweep`` so the
# six series are the same from pass to pass. These pin what the pinned SDK does
# with that variable: the instance ID follows it, the service's name does not.
def resource_of(service_name: str) -> dict:
    reader = InMemoryMetricReader()
    provider = make_meter_provider(service_name, reader)
    provider.get_meter("test").create_counter("things").add(1)
    data = reader.get_metrics_data()
    assert data is not None
    return dict(data.resource_metrics[0].resource.attributes)


def test_the_environment_sets_the_instance_id_and_the_name_passed_in_stays(
    monkeypatch: pytest.MonkeyPatch, no_endpoint: None
) -> None:
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.instance.id=claims-sweep")

    attributes = resource_of("claims-sweep-test")

    assert attributes["service.instance.id"] == "claims-sweep"
    assert attributes["service.name"] == "claims-sweep-test"


def test_the_environment_cannot_rename_the_service(
    monkeypatch: pytest.MonkeyPatch, no_endpoint: None
) -> None:
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        "service.instance.id=claims-sweep,service.name=renamed",
    )
    monkeypatch.setenv("OTEL_SERVICE_NAME", "renamed-too")

    attributes = resource_of("claims-sweep-test")

    assert attributes["service.name"] == "claims-sweep-test"
    assert attributes["service.instance.id"] == "claims-sweep"


def test_without_the_variable_the_instance_id_is_a_generated_one(
    monkeypatch: pytest.MonkeyPatch, no_endpoint: None
) -> None:
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)

    attributes = resource_of("claims-sweep-test")

    instance = str(attributes["service.instance.id"])
    assert instance != "claims-sweep"
    assert len(instance) == 36 and instance.count("-") == 4
