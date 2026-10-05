"""A request over its rate limit is refused before its text is redacted (T-73).

The order is: route decision, the estimate of the text as sent, the tenant's
rate windows, redaction, then the walk (budget, candidates). A request a rate
window refuses costs no redaction; one the budget refuses has been redacted.
Live mode over a scripted provider, the real ledger on the throwaway
PostgreSQL and the limiter on a fake clock. The setup is a small copy of
``test_gateway_guardrails.py``'s, which is over the size ceiling.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from dbsupport import DatabaseHandle
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import FakeClock, audit_events, pin_chat_route

from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.gateway import redaction as redaction_module
from meridian.platform.gateway.app import create_app
from meridian.platform.gateway.budget import chat_estimate, embedding_estimate
from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest
from meridian.platform.gateway.providers.base import EmbeddingReply, ProviderReply
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.guardrails import Redaction
from meridian.platform.registry.models import Deployment

CHAT = "/v1/chat"
EMBEDDINGS = "/v1/embeddings"
FIRST = "aoai-sdc-gpt-4o"
TENANT = "claims-triage"
RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000058")
HTTP_TOO_LARGE = 413
HTTP_TOO_MANY = 429

# The documented fictional values (RFC 2606 domain, the standard test card).
EMAIL = "ana.kovacs@example.com"
CARD = "4111 1111 1111 1111"
CHAT_BODY = {
    "messages": [
        {"role": "system", "content": "You draft triage summaries."},
        {"role": "user", "content": f"Claimant {EMAIL} paid with {CARD}, thanks."},
    ]
}
EMBEDDING_BODY = {"inputs": [f"Mail {EMAIL} or pay {CARD}", "A storm hit the roof."]}
BODIES = {CHAT: CHAT_BODY, EMBEDDINGS: EMBEDDING_BODY}
# The values the redaction replaces in each body.
REDACTIONS = {CHAT: 2, EMBEDDINGS: 2}
SPANS = {CHAT: "gateway.chat", EMBEDDINGS: "gateway.embeddings"}
ORIGINAL_ESTIMATES = {
    CHAT: chat_estimate(ChatRequest.model_validate(CHAT_BODY)).tokens,
    EMBEDDINGS: embedding_estimate(
        EmbeddingRequest.model_validate(EMBEDDING_BODY)
    ).tokens,
}
SEED_LIMITS = (
    "      requests_per_10_seconds: 10\n"
    "      tokens_per_minute: 10000\n"
    "      tokens_per_day: 300000\n"
    "      cost_per_month_eur: 10\n"
)


class RecordingProvider:
    """Answers every call and records which deployments were called."""

    def __init__(self) -> None:
        self.called: list[str] = []

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        self.called.append(deployment.id)
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
        self.called.append(deployment.id)
        assert deployment.dimensions is not None
        vector = (0.0,) * deployment.dimensions
        return EmbeddingReply(
            embeddings=tuple(vector for _ in request.inputs),
            model="text-embedding-3-large",
            input_tokens=9,
        )


@dataclass(slots=True)
class Gateway:
    app: FastAPI
    client: TestClient
    provider: RecordingProvider
    exporter: InMemorySpanExporter
    database: DatabaseHandle

    def post(self, path: str) -> tuple[httpx.Response, ReadableSpan, dict]:
        """One request: its response, its span and its audit row. The spans of
        earlier requests are dropped first, so the one found is this request's."""
        self.exporter.clear()
        headers = {
            "X-Meridian-Tenant": TENANT,
            "X-Meridian-Agent": "claims-triage",
            "X-Meridian-Run": str(RUN_ID),
        }
        response = self.client.post(path, json=BODIES[path], headers=headers)
        (span,) = [
            s for s in self.exporter.get_finished_spans() if s.name == SPANS[path]
        ]
        return response, span, audit_events(self.database, RUN_ID)[-1]


@pytest.fixture
def gateway_with(
    plant: Callable[..., Path], fresh_database: DatabaseHandle
) -> Callable[..., Gateway]:
    """A gateway whose tenant has the given limits (the seeded ones otherwise)
    and whose chat route lists one EU deployment."""

    def make(
        requests: int = 10, tokens_per_minute: int = 10000, tokens_per_day: int = 300000
    ) -> Gateway:
        directory = plant(
            (
                "tenants.yaml",
                SEED_LIMITS,
                f"      requests_per_10_seconds: {requests}\n"
                f"      tokens_per_minute: {tokens_per_minute}\n"
                f"      tokens_per_day: {tokens_per_day}\n"
                "      cost_per_month_eur: 10\n",
            )
        )
        pin_chat_route(directory, FIRST)
        exporter = InMemorySpanExporter()
        provider = RecordingProvider()
        settings = GatewaySettings(
            registry_dir=directory,
            mode="live",
            environment="test",
            database_url=fresh_database.dsn("model_gateway"),
        )
        app = create_app(
            settings,
            tracer_provider=make_tracer_provider("model-gateway", exporter),
            meter_provider=make_meter_provider("model-gateway", InMemoryMetricReader()),
            providers={"azure-openai": provider},
            clock=FakeClock(),
        )
        return Gateway(app, TestClient(app), provider, exporter, fresh_database)

    return make


@pytest.fixture
def redact_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The texts the gateway's redaction was asked to redact, where the gateway
    looks ``redact`` up."""
    calls: list[str] = []
    real = redaction_module.redact

    def spy(text: str) -> Redaction:
        calls.append(text)
        return real(text)

    monkeypatch.setattr(redaction_module, "redact", spy)
    return calls


def assert_refused_unredacted(
    status: int,
    reason: str,
    response: httpx.Response,
    span: ReadableSpan,
    row: dict,
    gateway: Gateway,
    redact_calls: list[str],
) -> None:
    assert response.status_code == status
    assert (row["outcome"], row["reason"]) == ("refused", reason)
    assert redact_calls == []
    assert gateway.provider.called == []
    assert "meridian.redactions" not in span.attributes


@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
def test_a_request_over_the_request_window_is_refused_before_redaction(
    gateway_with: Callable[..., Gateway], redact_calls: list[str], path: str
) -> None:
    gateway = gateway_with(requests=1)
    assert gateway.post(path)[0].status_code == 200
    redact_calls.clear()
    gateway.provider.called.clear()

    response, span, row = gateway.post(path)

    assert_refused_unredacted(
        HTTP_TOO_MANY, "tenant-request-rate", response, span, row, gateway, redact_calls
    )


@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
def test_a_request_over_the_token_window_is_refused_before_redaction(
    gateway_with: Callable[..., Gateway], redact_calls: list[str], path: str
) -> None:
    # Room for one request as sent, not for a second.
    gateway = gateway_with(tokens_per_minute=ORIGINAL_ESTIMATES[path] + 1)
    assert gateway.post(path)[0].status_code == 200
    redact_calls.clear()
    gateway.provider.called.clear()

    response, span, row = gateway.post(path)

    assert_refused_unredacted(
        HTTP_TOO_MANY, "tenant-token-rate", response, span, row, gateway, redact_calls
    )


@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
def test_a_request_larger_than_the_token_limit_is_refused_before_redaction(
    gateway_with: Callable[..., Gateway], redact_calls: list[str], path: str
) -> None:
    # The redacted request would fit; the request as sent does not.
    gateway = gateway_with(tokens_per_minute=ORIGINAL_ESTIMATES[path] - 1)

    response, span, row = gateway.post(path)

    assert_refused_unredacted(
        HTTP_TOO_LARGE,
        "tenant-request-too-large",
        response,
        span,
        row,
        gateway,
        redact_calls,
    )


@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
def test_a_request_the_budget_refuses_has_been_redacted(
    gateway_with: Callable[..., Gateway], redact_calls: list[str], path: str
) -> None:
    # The window admits the request as sent; the day's budget does not.
    gateway = gateway_with(tokens_per_day=ORIGINAL_ESTIMATES[path] - 1)

    response, span, row = gateway.post(path)

    assert response.status_code == HTTP_TOO_MANY
    assert (row["outcome"], row["reason"]) == ("refused", "tenant-token-budget")
    assert len(redact_calls) > 0
    assert gateway.provider.called == []
    assert span.attributes["meridian.redactions"] == REDACTIONS[path]
