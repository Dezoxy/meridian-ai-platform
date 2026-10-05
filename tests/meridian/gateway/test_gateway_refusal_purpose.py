"""A refusal row of the gateway carries the call's purpose (S058).

In live mode a refusal names no deployment, so the row says ``chat`` or
``embedding`` instead. The throttle's key stays the tenant and the reason, so a
row's purpose is that of the call that wrote it. Rows that are not refusals
carry none. Live mode over a recording provider, the real audit table on the
throwaway PostgreSQL and the throttle on a fake clock.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from callersupport import PREFIX, as_caller
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import FakeClock, owner_rows, pin_chat_route

from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.throttle import REFUSAL_AUDIT_SECONDS
from meridian.platform.gateway.app import create_app
from meridian.platform.gateway.budget import chat_estimate, embedding_estimate
from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest
from meridian.platform.gateway.providers.base import EmbeddingReply, ProviderReply
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.registry.models import Deployment

CHAT = "/v1/chat"
EMBEDDINGS = "/v1/embeddings"
ROUTES = [(CHAT, "chat"), (EMBEDDINGS, "embedding")]
FIRST = "aoai-sdc-gpt-4o"
TENANT = "claims-triage"
AGENT = "claims-triage"
OTHER_AGENT = "agent-one"  # one the tenant may not run
NAME_REASON = "caller-name-not-allowed"
BODIES = {
    CHAT: {"messages": [{"role": "user", "content": "A storm hit the roof."}]},
    EMBEDDINGS: {"inputs": ["A storm hit the roof."]},
}
ESTIMATES = {
    CHAT: chat_estimate(ChatRequest.model_validate(BODIES[CHAT])).tokens,
    EMBEDDINGS: embedding_estimate(
        EmbeddingRequest.model_validate(BODIES[EMBEDDINGS])
    ).tokens,
}
SEED_LIMITS = (
    "      requests_per_10_seconds: 10\n"
    "      tokens_per_minute: 10000\n"
    "      tokens_per_day: 300000\n"
    "      cost_per_month_eur: 10\n"
)
HTTP_FORBIDDEN = 403
HTTP_TOO_MANY = 429


class RecordingProvider:
    """Answers every call of either purpose."""

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

    def embed(
        self,
        deployment: Deployment,
        request: EmbeddingRequest,
        *,
        timeout_seconds: float,
    ) -> EmbeddingReply:
        assert deployment.dimensions is not None
        vector = (0.0,) * deployment.dimensions
        return EmbeddingReply(
            embeddings=tuple(vector for _ in request.inputs),
            model="text-embedding-3-large",
            input_tokens=9,
        )


@dataclass(slots=True)
class Gateway:
    client: TestClient
    clock: FakeClock
    database: DatabaseHandle

    def post(
        self, path: str, *, tenant: str = TENANT, agent: str = AGENT
    ) -> httpx.Response:
        headers = {
            "X-Meridian-Tenant": tenant,
            "X-Meridian-Agent": agent,
            "X-Meridian-Run": str(uuid.uuid4()),
        }
        return self.client.post(path, json=BODIES[path], headers=headers)

    def refusals(self) -> list[tuple]:
        """The refusal rows, oldest first: reason, purpose, suppressed."""
        return owner_rows(
            self.database,
            "SELECT reason, purpose, suppressed FROM audit.events "
            "WHERE outcome = 'refused' ORDER BY recorded_at",
        )

    def purposes_of(self, outcome: str) -> list[str | None]:
        rows = owner_rows(
            self.database,
            "SELECT purpose FROM audit.events WHERE outcome = %s",
            (outcome,),
        )
        return [purpose for (purpose,) in rows]


@pytest.fixture
def gateway_with(
    plant: Callable[..., Path], fresh_database: DatabaseHandle
) -> Callable[..., Gateway]:
    """A gateway whose tenant has the given limits (the seeded ones otherwise)
    and whose chat route lists one EU deployment; with ``caller`` it has a
    caller policy and every request comes from that service."""

    def make(
        requests: int = 10, tokens_per_day: int = 300000, caller: str | None = None
    ) -> Gateway:
        directory = plant(
            (
                "tenants.yaml",
                SEED_LIMITS,
                f"      requests_per_10_seconds: {requests}\n"
                "      tokens_per_minute: 10000\n"
                f"      tokens_per_day: {tokens_per_day}\n"
                "      cost_per_month_eur: 10\n",
            )
        )
        pin_chat_route(directory, FIRST)
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


@pytest.mark.parametrize(("path", "purpose"), ROUTES)
def test_a_policy_refusal_row_carries_the_purpose(
    gateway_with: Callable[..., Gateway], path: str, purpose: str
) -> None:
    gateway = gateway_with()

    response = gateway.post(path, agent=OTHER_AGENT)

    assert response.status_code == HTTP_FORBIDDEN
    assert gateway.refusals() == [("agent-not-allowed", purpose, 0)]


@pytest.mark.parametrize(("path", "purpose"), ROUTES)
def test_a_rate_refusal_row_carries_the_purpose(
    gateway_with: Callable[..., Gateway], path: str, purpose: str
) -> None:
    gateway = gateway_with(requests=1)
    assert gateway.post(path).status_code == 200

    response = gateway.post(path)

    assert response.status_code == HTTP_TOO_MANY
    assert gateway.refusals() == [("tenant-request-rate", purpose, 0)]


@pytest.mark.parametrize(("path", "purpose"), ROUTES)
def test_a_budget_refusal_row_carries_the_purpose(
    gateway_with: Callable[..., Gateway], path: str, purpose: str
) -> None:
    # The rate window admits the request; the day's budget does not.
    gateway = gateway_with(tokens_per_day=ESTIMATES[path] - 1)

    response = gateway.post(path)

    assert response.status_code == HTTP_TOO_MANY
    assert gateway.refusals() == [("tenant-token-budget", purpose, 0)]


@pytest.mark.parametrize(("path", "purpose"), ROUTES)
def test_a_name_refusal_row_carries_the_purpose(
    gateway_with: Callable[..., Gateway], path: str, purpose: str
) -> None:
    # The runtime does not name the evaluation tenant.
    gateway = gateway_with(caller="agent-runtime")

    response = gateway.post(path, tenant="evaluation")

    assert response.status_code == HTTP_FORBIDDEN
    assert gateway.refusals() == [(NAME_REASON, purpose, 0)]


@pytest.mark.parametrize(("path", "_purpose"), ROUTES)
def test_a_completed_call_row_carries_no_purpose(
    gateway_with: Callable[..., Gateway], path: str, _purpose: str
) -> None:
    gateway = gateway_with()

    response = gateway.post(path)

    assert response.status_code == 200
    assert gateway.purposes_of("completed") == [None]
    assert gateway.refusals() == []


def test_one_window_holds_one_row_of_the_first_purpose_and_counts_the_other(
    gateway_with: Callable[..., Gateway],
) -> None:
    gateway = gateway_with()

    assert gateway.post(CHAT, agent=OTHER_AGENT).status_code == HTTP_FORBIDDEN
    # Same tenant, same reason, other purpose, same window: no row.
    assert gateway.post(EMBEDDINGS, agent=OTHER_AGENT).status_code == HTTP_FORBIDDEN

    assert gateway.refusals() == [("agent-not-allowed", "chat", 0)]
    gateway.clock.advance(REFUSAL_AUDIT_SECONDS)
    assert gateway.post(EMBEDDINGS, agent=OTHER_AGENT).status_code == HTTP_FORBIDDEN

    # The row of the next window is the embedding call's and counts the one
    # of the window before.
    assert gateway.refusals() == [
        ("agent-not-allowed", "chat", 0),
        ("agent-not-allowed", "embedding", 1),
    ]
