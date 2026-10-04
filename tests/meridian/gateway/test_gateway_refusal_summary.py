"""The count of a refusal flood's last window is written (S058, T-49).

A refusal row stands for the refusals suppressed since the row before. The
refusals suppressed in a flood's last window are carried by no row, so a
``suppressed`` summary row is written once the flood has been quiet for two
windows, with the next request of any tenant, or when the app closes. A flood
that goes on has its count in its own row, so it never gets a summary. Live mode
over a recording provider, the real audit table on the throwaway PostgreSQL and
the throttle on a fake clock.
"""

import logging
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
import pytest
from callersupport import PREFIX, as_caller
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import FakeClock, owner_rows, pin_chat_route

from meridian.platform.common import audit as audit_module
from meridian.platform.common.audit import AuditEvent
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.throttle import (
    REFUSAL_AUDIT_SECONDS,
    REFUSAL_SUMMARY_SECONDS,
)
from meridian.platform.gateway import app as app_module
from meridian.platform.gateway.app import create_app
from meridian.platform.gateway.models import ChatRequest
from meridian.platform.gateway.providers.base import ProviderReply
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.registry.models import Deployment

CHAT = "/v1/chat"
FIRST = "aoai-sdc-gpt-4o"
TENANT = "claims-triage"
OTHER_TENANT = "evaluation"
AGENT = "claims-triage"
OTHER_AGENT = "agent-one"  # one no tenant may run
REASON = "agent-not-allowed"
EXCEPTION_TEXT = "the-text-of-the-exception"
BODY = {"messages": [{"role": "user", "content": "A storm hit the roof."}]}
HTTP_FORBIDDEN = 403
HTTP_UNAVAILABLE = 503
SPACING_SECONDS = 2.0  # 11 requests, 22 s in all: inside the two windows
FLOOD = 3
SUPPRESSED_BY_THE_FLOOD = FLOOD - 1
NOT_ALLOWED_CALLER = "policy-mcp"  # a service that does not call the gateway
CALLER_REASON = "caller-not-allowed"


class RecordingProvider:
    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        return ProviderReply(
            text="a fake answer",
            finish_reason="stop",
            model="gpt-4o-2024-11-20",
            input_tokens=11,
            output_tokens=7,
        )


@dataclass(slots=True)
class Gateway:
    client: TestClient
    clock: FakeClock
    database: DatabaseHandle

    def post(self, *, tenant: str = TENANT, agent: str = AGENT) -> httpx.Response:
        headers = {
            "X-Meridian-Tenant": tenant,
            "X-Meridian-Agent": agent,
            "X-Meridian-Run": str(uuid.uuid4()),
        }
        return self.client.post(CHAT, json=BODY, headers=headers)

    def flood(self, refusals: int = FLOOD) -> None:
        for _ in range(refusals):
            response = self.post(agent=OTHER_AGENT)
            assert response.status_code == HTTP_FORBIDDEN

    def summaries(self) -> list[tuple]:
        """The summary rows: tenant, reason, suppressed, and every column a
        summary leaves empty."""
        return owner_rows(
            self.database,
            "SELECT tenant, reason, suppressed, agent, run_id, call_id, purpose, "
            "deployment, provider, data_class, region, residency, reference "
            "FROM audit.events WHERE outcome = 'suppressed' ORDER BY recorded_at",
        )

    def refusals(self) -> list[tuple]:
        return owner_rows(
            self.database,
            "SELECT reason, suppressed FROM audit.events "
            "WHERE outcome = 'refused' ORDER BY recorded_at",
        )


@pytest.fixture
def gateway_with(
    plant: Callable[..., Path], fresh_database: DatabaseHandle
) -> Callable[..., Gateway]:
    def make(caller: str | None = None) -> Gateway:
        directory = pin_chat_route(plant(), FIRST)
        clock = FakeClock()
        settings = GatewaySettings(
            registry_dir=directory,
            mode="live",
            environment="test",
            database_url=fresh_database.dsn("model_gateway"),
            identity_prefix=None if caller is None else PREFIX,
        )
        app = create_app(
            settings,
            tracer_provider=make_tracer_provider(
                "model-gateway", InMemorySpanExporter()
            ),
            meter_provider=make_meter_provider("model-gateway", InMemoryMetricReader()),
            providers={"azure-openai": RecordingProvider()},
            clock=clock,
        )
        client = TestClient(app if caller is None else as_caller(app, caller))
        return Gateway(client, clock, fresh_database)

    return make


@pytest.fixture
def summary_write_fails(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bool]]:
    """Make the audit write raise for a ``suppressed`` row only, while the
    returned list holds a true value."""
    failing = [True]
    real = audit_module.write_audit

    def write(database_url: str, event: AuditEvent) -> None:
        if failing and event.outcome == "suppressed":
            raise RuntimeError(EXCEPTION_TEXT)
        real(database_url, event)

    monkeypatch.setattr(app_module, "write_audit", write)
    yield failing


def test_the_count_of_a_flood_is_written_once_it_has_been_quiet_for_two_windows(
    gateway_with: Callable[..., Gateway],
) -> None:
    gateway = gateway_with()
    gateway.flood()
    assert gateway.refusals() == [(REASON, 0)]
    assert gateway.summaries() == []
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)

    response = gateway.post(tenant=OTHER_TENANT)

    assert response.status_code == 200
    # No agent, run, call ID, purpose or route: it stands for refusals of both
    # purposes and is not itself a refusal.
    assert gateway.summaries() == [
        (TENANT, REASON, SUPPRESSED_BY_THE_FLOOD) + (None,) * 10
    ]


def test_a_refusal_row_that_could_not_be_written_is_summarised_after_two_windows(
    gateway_with: Callable[..., Gateway], monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = gateway_with()
    real = audit_module.write_audit
    failing = [True]

    def write(database_url: str, event: AuditEvent) -> None:
        if failing and event.outcome == "refused":
            raise psycopg.OperationalError("connection refused")
        real(database_url, event)

    monkeypatch.setattr(app_module, "write_audit", write)
    assert gateway.post(agent=OTHER_AGENT).status_code == HTTP_UNAVAILABLE
    failing.clear()  # the database is back, and the flood does not go on
    assert gateway.refusals() == []

    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS - 1)
    assert gateway.post(tenant=OTHER_TENANT).status_code == 200
    assert gateway.summaries() == []  # the next refusal would carry it
    gateway.clock.advance(1)
    assert gateway.post(tenant=OTHER_TENANT).status_code == 200

    assert [row[:3] for row in gateway.summaries()] == [(TENANT, REASON, 1)]


def test_a_summary_is_written_once(gateway_with: Callable[..., Gateway]) -> None:
    gateway = gateway_with()
    gateway.flood()
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)

    assert gateway.post(tenant=OTHER_TENANT).status_code == 200
    assert gateway.post(tenant=OTHER_TENANT).status_code == 200
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)
    assert gateway.post(tenant=OTHER_TENANT).status_code == 200

    assert len(gateway.summaries()) == 1


def test_a_flood_that_goes_on_writes_no_summary_its_own_row_carries_the_count(
    gateway_with: Callable[..., Gateway],
) -> None:
    gateway = gateway_with()
    gateway.flood()
    gateway.clock.advance(REFUSAL_AUDIT_SECONDS)
    gateway.flood(1)  # one more, a window later: its row carries the count
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)

    assert gateway.post(tenant=OTHER_TENANT).status_code == 200

    assert gateway.refusals() == [(REASON, 0), (REASON, SUPPRESSED_BY_THE_FLOOD)]
    assert gateway.summaries() == []


def test_one_window_and_a_half_after_the_row_no_summary_is_written_yet(
    gateway_with: Callable[..., Gateway],
) -> None:
    gateway = gateway_with()
    gateway.flood()
    gateway.clock.advance(REFUSAL_AUDIT_SECONDS * 1.5)

    assert gateway.post(tenant=OTHER_TENANT).status_code == 200

    assert gateway.summaries() == []
    # The count is still held: the flood's next row, or the summary, has it.
    gateway.clock.advance(REFUSAL_AUDIT_SECONDS / 2)
    assert gateway.post(tenant=OTHER_TENANT).status_code == 200
    assert [row[:3] for row in gateway.summaries()] == [
        (TENANT, REASON, SUPPRESSED_BY_THE_FLOOD)
    ]


def test_a_summary_write_that_fails_leaves_the_answer_and_the_count_alone(
    gateway_with: Callable[..., Gateway],
    summary_write_fails: list[bool],
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = gateway_with()
    gateway.flood()
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)

    with caplog.at_level(logging.WARNING):
        response = gateway.post(tenant=OTHER_TENANT)

    assert response.status_code == 200
    assert response.json()["output"]["text"] == "a fake answer"
    assert gateway.summaries() == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()
    assert EXCEPTION_TEXT not in caplog.text
    assert warnings[0].exc_info is None
    summary_write_fails.clear()  # the database is back

    # Inside the two windows after the failure: no attempt, so no row and no
    # warning, however well the database is.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert gateway.post(tenant=OTHER_TENANT).status_code == 200
    assert gateway.summaries() == []
    assert caplog.records == []
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)

    assert gateway.post(tenant=OTHER_TENANT).status_code == 200

    assert [row[:3] for row in gateway.summaries()] == [
        (TENANT, REASON, SUPPRESSED_BY_THE_FLOOD)
    ]


def test_a_summary_that_keeps_failing_is_tried_once_per_two_windows(
    gateway_with: Callable[..., Gateway],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = gateway_with()
    real = audit_module.write_audit
    attempts: list[AuditEvent] = []

    def write(database_url: str, event: AuditEvent) -> None:
        if event.outcome == "suppressed":
            attempts.append(event)
            raise RuntimeError(EXCEPTION_TEXT)
        real(database_url, event)

    monkeypatch.setattr(app_module, "write_audit", write)
    gateway.flood()
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)

    with caplog.at_level(logging.WARNING):
        # The first request tries and fails; the ten after it, inside the two
        # windows, make no attempt. They alternate between two tenants and are
        # spaced out, to stay under the tenants' own limits.
        for n in range(11):
            tenant = OTHER_TENANT if n % 2 else TENANT
            assert gateway.post(tenant=tenant).status_code == 200
            gateway.clock.advance(SPACING_SECONDS)

    assert len(attempts) == 1
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert EXCEPTION_TEXT not in caplog.text
    assert gateway.summaries() == []


def test_closing_the_app_writes_the_count_of_a_window_that_has_not_ended(
    gateway_with: Callable[..., Gateway],
) -> None:
    gateway = gateway_with()

    with gateway.client:
        gateway.flood()
        gateway.clock.advance(REFUSAL_AUDIT_SECONDS / 2)
        assert gateway.summaries() == []

    assert [row[:3] for row in gateway.summaries()] == [
        (TENANT, REASON, SUPPRESSED_BY_THE_FLOOD)
    ]


def test_closing_the_app_writes_nothing_when_nothing_was_suppressed(
    gateway_with: Callable[..., Gateway],
) -> None:
    gateway = gateway_with()

    with gateway.client:
        gateway.flood(1)

    assert gateway.summaries() == []


def test_a_summary_that_fails_at_shutdown_does_not_stop_the_shutdown(
    gateway_with: Callable[..., Gateway],
    summary_write_fails: list[bool],
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = gateway_with()

    with caplog.at_level(logging.WARNING), gateway.client:
        gateway.flood()

    assert gateway.summaries() == []
    assert EXCEPTION_TEXT not in caplog.text


def test_a_caller_check_refusal_is_not_summarised_and_keeps_its_row_per_window(
    gateway_with: Callable[..., Gateway],
) -> None:
    gateway = gateway_with(caller=NOT_ALLOWED_CALLER)

    with gateway.client:
        for _ in range(FLOOD):
            assert gateway.post().status_code == HTTP_FORBIDDEN
        gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)
        assert gateway.post().status_code == HTTP_FORBIDDEN

    # The caller check has a throttle of its own: one row per window, and the
    # next window's row carries what the first suppressed.
    assert gateway.refusals() == [(CALLER_REASON, 0), (CALLER_REASON, 2)]
    assert gateway.summaries() == []
