"""The tool servers' metrics (S064): a counter that raises, and the meter
provider's life with each of the three servers.

A counter that raises does not fail the call it counts; a provider the caller
gave is left to the caller, and one the app built is shut down with it. The
constants and helpers are in ``metersupport``, the ``world`` fixture in the
directory's ``conftest.py``.
"""

import logging
from collections.abc import Callable
from typing import Any

import anyio
import httpx
import pytest
from exportsupport import record_exports
from metersupport import LOOKUP, RUN_LABELS, SERVER_TOOLS, TOOL, a_call
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR
from toolsupport import CANARY, World, run_call, settings_for

from meridian.platform.claims_mcp.app import (
    create_app as create_claims_app,
)
from meridian.platform.common import metrics as common_metrics
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.knowledge_mcp.app import create_app as create_knowledge_app
from meridian.platform.knowledge_mcp.settings import KnowledgeServerSettings
from meridian.platform.policy_mcp.app import create_app as create_policy_app
from meridian.platform.registry import load_registry
from meridian.platform.toolserver import server as server_module
from meridian.platform.toolserver.meters import CALLS, ToolServerMeters
from meridian.platform.toolserver.pipeline import Finished
from meridian.platform.toolserver.settings import ToolServerSettings


# ── a counter that raises does not fail the call it counts ──────────────────
class ExplodingCounter:
    def add(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError(CANARY)


class ExplodingMeter:
    def create_counter(self, *args: object, **kwargs: object) -> ExplodingCounter:
        return ExplodingCounter()


class ExplodingProvider:
    def get_meter(self, *args: object, **kwargs: object) -> ExplodingMeter:
        return ExplodingMeter()


def test_a_counter_that_raises_is_one_warning_with_a_class_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = load_registry(REGISTRY_DIR)
    meters = ToolServerMeters(ExplodingProvider(), registry, SERVER_TOOLS)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING):
        meters.call_ended(Finished(a_call(), "completed", None))

    (record,) = [r for r in caplog.records if r.name == common_metrics.__name__]
    assert "RuntimeError" in record.getMessage()
    assert "ToolServerMeters.call_ended" in record.getMessage()
    assert CANARY not in caplog.text


def test_a_tool_call_that_completed_is_answered_as_completed_when_its_counter_raises(
    world: World,
) -> None:
    app = create_policy_app(
        settings_for(world.db, "policy_mcp"),
        meter_provider=ExplodingProvider(),  # type: ignore[arg-type]
    )

    result = run_call(app.server, TOOL, LOOKUP, run_id=world.run_id)

    assert result.is_error is False
    assert result.structured_content is not None


# ── the meter provider of each server ───────────────────────────────────────
class ShutdownSpy(MeterProvider):
    def __init__(self) -> None:
        super().__init__()
        self.shutdowns = 0

    def shutdown(self, *args: object, **kwargs: object) -> None:
        self.shutdowns += 1
        super().shutdown(*args, **kwargs)


DSN = "postgresql://tool_server@db.invalid/x"


HOSTS = ("127.0.0.1:8000",)


def policy_app(provider: MeterProvider | None) -> Any:
    settings = ToolServerSettings(
        registry_dir=REGISTRY_DIR, database_url=DSN, allowed_hosts=HOSTS
    )
    return create_policy_app(settings, meter_provider=provider)


def claims_app(provider: MeterProvider | None) -> Any:
    settings = ToolServerSettings(
        registry_dir=REGISTRY_DIR, database_url=DSN, allowed_hosts=HOSTS
    )
    return create_claims_app(settings, meter_provider=provider)


def knowledge_app(provider: MeterProvider | None) -> Any:
    settings = KnowledgeServerSettings(
        registry_dir=REGISTRY_DIR,
        database_url=DSN,
        allowed_hosts=HOSTS,
        gateway_url="http://gateway.invalid",
    )
    return create_knowledge_app(
        settings,
        http=httpx.Client(base_url="http://gateway.invalid"),
        meter_provider=provider,
    )


# Each server, with the service name its own meter provider is made under.
SERVERS: dict[str, tuple[Callable[[MeterProvider | None], Any], str]] = {
    "policy": (policy_app, "policy-mcp"),
    "claims": (claims_app, "claims-mcp"),
    "knowledge": (knowledge_app, "knowledge-mcp"),
}


def serve_and_stop(app: Any) -> None:
    async def go() -> None:
        async with app.app.router.lifespan_context(app.app):
            pass

    anyio.run(go)


@pytest.mark.parametrize("server", SERVERS)
def test_a_meter_provider_the_caller_gave_is_left_to_the_caller(server: str) -> None:
    spy = ShutdownSpy()
    build, _ = SERVERS[server]

    serve_and_stop(build(spy))

    assert spy.shutdowns == 0


@pytest.mark.parametrize("server", SERVERS)
def test_a_meter_provider_the_app_built_is_shut_down_with_the_app(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = ShutdownSpy()
    names: list[str] = []
    build, service_name = SERVERS[server]

    def made(name: str) -> MeterProvider:
        names.append(name)
        return spy

    monkeypatch.setattr(server_module, "make_meter_provider", made)
    app = build(None)
    assert spy.shutdowns == 0

    serve_and_stop(app)

    assert spy.shutdowns == 1
    assert names == [service_name]


@pytest.mark.parametrize("server", SERVERS)
def test_a_server_sets_no_global_meter_provider(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        otel_metrics, "set_meter_provider", lambda provider: calls.append("meter")
    )
    build, _ = SERVERS[server]

    serve_and_stop(build(None))

    assert calls == []


@pytest.mark.parametrize("server", SERVERS)
def test_without_the_collectors_address_nothing_is_exported_and_nothing_fails(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    built: list[object] = []
    monkeypatch.setattr(
        common_metrics, "OTLPMetricExporter", lambda *a, **k: built.append((a, k))
    )
    build, _ = SERVERS[server]

    serve_and_stop(build(None))

    assert built == []


def test_a_server_built_with_no_meter_provider_sends_its_series_to_the_collector(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = record_exports(monkeypatch)
    app = create_policy_app(
        settings_for(world.db, "policy_mcp"),
        tracer_provider=make_tracer_provider("policy-mcp", InMemorySpanExporter()),
    )
    run_call(app.server, TOOL, LOOKUP, run_id=world.run_id)

    serve_and_stop(app)  # the reader exports once more as the provider closes

    completed = {"meridian.tool": TOOL, "meridian.outcome": "completed"}
    assert recorder.points(CALLS) == [("policy-mcp", completed | RUN_LABELS, 1)]
