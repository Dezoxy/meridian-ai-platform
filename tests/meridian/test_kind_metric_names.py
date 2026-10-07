"""The dashboard's metric names, tied to the gateway's instruments (S043)."""

import re

from kindsupport import (
    GATEWAY_SERIES,
    SMOKE_SH,
    panel_titled,
    prometheus_name,
    variable_named,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from servicesupport import REGISTRY_DIR

from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.gateway.meters import GatewayMeters
from meridian.platform.registry import load_registry

# ── The dashboard and the metric names, tied to their sources (S043) ─────────


def test_the_dashboard_refuses_a_custom_dimension_and_says_the_retention() -> None:
    text = panel_titled("What these numbers are")["options"]["content"]

    # A crafted link must not put PromQL into `by (...)`.
    assert variable_named("dimension")["allowCustomValue"] is False
    assert "looked for up to 24 hours back" in text
    assert text.endswith(
        "Choose a range of at least two minutes. Prometheus keeps 24 hours on kind, "
        "so a longer range shows what it still holds, and a series with no sample "
        "for longer than that is a new series."
    )


def test_the_pinned_series_names_follow_from_the_gateways_instruments() -> None:
    reader = InMemoryMetricReader()
    meters = GatewayMeters(make_meter_provider("model-gateway", reader))
    deployment = load_registry(REGISTRY_DIR).deployments[0]

    meters.settled("claims-triage", "triage", deployment, 3, 4, 5)
    meters.call_record().end("completed")
    data = reader.get_metrics_data()

    assert data is not None
    (scope,) = data.resource_metrics[0].scope_metrics
    assert {prometheus_name(metric) for metric in scope.metrics} == GATEWAY_SERIES


def test_smoke_looks_for_the_same_three_series_as_the_dashboard_uses() -> None:
    (listed,) = re.findall(r"^readonly COST_SERIES=\((.*)\)$", SMOKE_SH, re.MULTILINE)

    assert set(listed.split()) == GATEWAY_SERIES
    assert len(listed.split()) == 3
