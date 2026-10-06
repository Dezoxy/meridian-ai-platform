"""The count of a caller-check flood's last window is written too (S069, T-49).

The gateway's caller check refuses a call from no known service before its body
is read, and audits it through a throttle: one ``refused`` row per window. What
the flood's last window suppressed is written as a ``suppressed`` row by the
same writer as the model-call refusals, with the next request that gets past
the check and when the app closes. The real audit table on the throwaway
PostgreSQL and the throttle on a fake clock.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from callersupport import CALLER_HEADER, PREFIX, as_caller_named_by_header
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import FakeClock, owner_rows, pin_chat_route

from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.throttle import (
    REFUSAL_AUDIT_SECONDS,
    REFUSAL_SUMMARY_SECONDS,
)
from meridian.platform.gateway.app import create_app
from meridian.platform.gateway.models import ChatRequest
from meridian.platform.gateway.providers.base import ProviderReply
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.registry.models import Deployment

CHAT = "/v1/chat"
FIRST = "aoai-sdc-gpt-4o"
BODY = {"messages": [{"role": "user", "content": "A storm hit the roof."}]}
ALLOWED_CALLER = "agent-runtime"
NOT_ALLOWED_CALLER = "policy-mcp"  # a service that does not call the gateway
# What the refused rows say, and the throttle's key, which a summary row holds
# as it is: the registry's ID of a caller it maps, or none, and the reason.
REASON = "caller-not-allowed"
KEY = f"{NOT_ALLOWED_CALLER}/{REASON}"
UNKNOWN_KEY = "-/caller-unknown-service"
FLOOD = 4
SUPPRESSED_BY_THE_FLOOD = FLOOD - 1
HTTP_FORBIDDEN = 403
EVENT = "model.call"


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

    def post(self, caller: str) -> httpx.Response:
        headers = {
            CALLER_HEADER: caller,
            "X-Meridian-Tenant": "claims-triage",
            "X-Meridian-Agent": "claims-triage",
            "X-Meridian-Run": str(uuid.uuid4()),
        }
        return self.client.post(CHAT, json=BODY, headers=headers)

    def flood(self, caller: str = NOT_ALLOWED_CALLER, times: int = FLOOD) -> None:
        for _ in range(times):
            assert self.post(caller).status_code == HTTP_FORBIDDEN

    def summaries(self) -> list[tuple]:
        """The summary rows: event, tenant, reason, suppressed, and every column
        a caller-check summary leaves empty."""
        return owner_rows(
            self.database,
            "SELECT event, tenant, reason, suppressed, agent, run_id, call_id, "
            "purpose, reference, deployment "
            "FROM audit.events WHERE outcome = 'suppressed' ORDER BY recorded_at",
        )

    def refusals(self) -> list[tuple]:
        return owner_rows(
            self.database,
            "SELECT reason, suppressed FROM audit.events "
            "WHERE outcome = 'refused' ORDER BY recorded_at",
        )


@pytest.fixture
def gateway(plant: Callable[..., Path], fresh_database: DatabaseHandle) -> Gateway:
    directory = pin_chat_route(plant(), FIRST)
    clock = FakeClock()
    settings = GatewaySettings(
        registry_dir=directory,
        mode="live",
        environment="test",
        database_url=fresh_database.dsn("model_gateway"),
        identity_prefix=PREFIX,
    )
    app = create_app(
        settings,
        tracer_provider=make_tracer_provider("model-gateway", InMemorySpanExporter()),
        meter_provider=make_meter_provider("model-gateway", InMemoryMetricReader()),
        providers={"azure-openai": RecordingProvider()},
        clock=clock,
    )
    client = TestClient(as_caller_named_by_header(app))
    return Gateway(client, clock, fresh_database)


def test_the_count_of_a_caller_check_flood_is_written_with_the_next_allowed_request(
    gateway: Gateway,
) -> None:
    gateway.flood()
    assert gateway.refusals() == [(REASON, 0)]
    assert gateway.summaries() == []
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)

    response = gateway.post(ALLOWED_CALLER)

    assert response.status_code == 200
    # The event the caller check's rows use, no tenant (the check runs before a
    # body is read), the key as the reason, and nothing of a run, a call or a
    # purpose.
    assert gateway.summaries() == [
        (EVENT, None, KEY, SUPPRESSED_BY_THE_FLOOD) + (None,) * 6
    ]


def test_a_caller_check_summary_waits_for_a_request_that_gets_past_the_check(
    gateway: Gateway,
) -> None:
    gateway.flood()
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)

    gateway.flood(times=1)  # refused again: its own row carries the count

    assert gateway.summaries() == []
    assert gateway.refusals() == [(REASON, 0), (REASON, SUPPRESSED_BY_THE_FLOOD)]


def test_each_caller_check_reason_has_its_own_summary(gateway: Gateway) -> None:
    gateway.flood(NOT_ALLOWED_CALLER, 3)
    gateway.flood("not-a-service-of-the-registry", 2)
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)

    assert gateway.post(ALLOWED_CALLER).status_code == 200

    assert sorted(row[2:4] for row in gateway.summaries()) == [
        (UNKNOWN_KEY, 1),
        (KEY, 2),
    ]


def test_a_caller_check_summary_is_written_once(gateway: Gateway) -> None:
    gateway.flood()
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)

    assert gateway.post(ALLOWED_CALLER).status_code == 200
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)
    assert gateway.post(ALLOWED_CALLER).status_code == 200

    assert len(gateway.summaries()) == 1


def test_a_caller_check_flood_that_goes_on_has_its_count_in_its_own_row(
    gateway: Gateway,
) -> None:
    gateway.flood()
    gateway.clock.advance(REFUSAL_AUDIT_SECONDS)
    gateway.flood(times=1)  # a window later: its row carries the count
    gateway.clock.advance(REFUSAL_SUMMARY_SECONDS)

    assert gateway.post(ALLOWED_CALLER).status_code == 200

    assert gateway.refusals() == [(REASON, 0), (REASON, SUPPRESSED_BY_THE_FLOOD)]
    assert gateway.summaries() == []


def test_closing_the_app_writes_the_count_of_a_caller_check_window_that_has_not_ended(
    gateway: Gateway,
) -> None:
    with gateway.client:
        gateway.flood()
        gateway.clock.advance(REFUSAL_AUDIT_SECONDS / 2)
        assert gateway.summaries() == []

    assert [row[:4] for row in gateway.summaries()] == [
        (EVENT, None, KEY, SUPPRESSED_BY_THE_FLOOD)
    ]
