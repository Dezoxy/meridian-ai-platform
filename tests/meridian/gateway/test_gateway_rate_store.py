"""The gateway's rate windows kept in a store two processes share, and the answer
when the store cannot be reached (S066, T-45).

Two gateway apps on one real Redis (plain, on loopback, through clients the test
builds) share a tenant's windows; a store that raises, or that is a closed port,
is a 503 and never a pass. Live mode over a scripted provider, the real ledger on
the throwaway PostgreSQL, the clocks moved by hand. The setup is a small copy of
``test_gateway_order.py``'s.
"""

import logging
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import redis
from dbsupport import DatabaseHandle
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from redis.backoff import NoBackoff
from redis.retry import Retry
from redissupport import RateKeys
from servicesupport import FakeClock, audit_events, owner_rows, pin_chat_route

from meridian.platform.common.audit import AuditEvent, AuditUnavailable
from meridian.platform.common.http import AUDIT_UNAVAILABLE
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.throttle import REFUSAL_AUDIT_SECONDS
from meridian.platform.gateway import app as gateway_app
from meridian.platform.gateway import redaction as redaction_module
from meridian.platform.gateway.app import create_app
from meridian.platform.gateway.budget import chat_estimate
from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ProviderError,
    ProviderReply,
)
from meridian.platform.gateway.ratelimit import RateLimiter, RateRefusal
from meridian.platform.gateway.ratelimit_redis import (
    RateStoreUnavailable,
    RedisRateLimiter,
)
from meridian.platform.gateway.refusals import (
    RATE_STORE_RETRY_SECONDS,
    RATE_STORE_UNAVAILABLE,
    TENANT_RATE_LIMIT_REACHED,
)
from meridian.platform.gateway.resilience import FAILURE_THRESHOLD
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.guardrails import Redaction
from meridian.platform.registry.models import Deployment, TenantLimits

CHAT = "/v1/chat"
EMBEDDINGS = "/v1/embeddings"
FIRST = "aoai-sdc-gpt-4o"
TENANT = "claims-triage"
OTHER_TENANT = "evaluation"
AGENT = "claims-triage"
REASON = "rate-store-unavailable"
HTTP_TOO_MANY = 429
HTTP_UNAVAILABLE = 503
LIMIT = 4
CLAIM_TEXT = "a storm hit the roof"
BODIES = {
    CHAT: {"messages": [{"role": "user", "content": CLAIM_TEXT}]},
    EMBEDDINGS: {"inputs": [CLAIM_TEXT]},
}
SPANS = {CHAT: "gateway.chat", EMBEDDINGS: "gateway.embeddings"}
PURPOSES = {CHAT: "chat", EMBEDDINGS: "embedding"}
CHAT_TOKENS = chat_estimate(ChatRequest.model_validate(BODIES[CHAT])).tokens
SEED_LIMITS = (
    "      requests_per_10_seconds: 10\n"
    "      tokens_per_minute: 10000\n"
    "      tokens_per_day: 300000\n"
    "      cost_per_month_eur: 10\n"
)


class ScriptedProvider:
    """Answers every call, or fails every call while ``failing``."""

    def __init__(self) -> None:
        self.called: list[str] = []
        self.failing = False

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        self.called.append(deployment.id)
        if self.failing:
            raise ProviderError("unavailable", 503)
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


class DownStore:
    """A limiter whose store cannot be reached while ``down``, and that admits
    everything while it is up: the message is what ``RedisRateLimiter`` raises."""

    def __init__(self, *, down: bool = True) -> None:
        self.down = down
        self.asked = 0

    def admit(
        self, tenant: str, limits: TenantLimits, tokens: int
    ) -> RateRefusal | None:
        self.asked += 1
        if self.down:
            raise RateStoreUnavailable("the rate store did not answer (TimeoutError)")
        return None


@dataclass(slots=True)
class Gateway:
    app: FastAPI
    client: TestClient
    provider: ScriptedProvider
    exporter: InMemorySpanExporter
    reader: InMemoryMetricReader
    database: DatabaseHandle
    clock: FakeClock

    def post(
        self, path: str = CHAT, tenant: str = TENANT, run_id: uuid.UUID | None = None
    ) -> tuple[httpx.Response, uuid.UUID]:
        run = run_id or uuid.uuid4()
        headers = {
            "X-Meridian-Tenant": tenant,
            "X-Meridian-Agent": AGENT,
            "X-Meridian-Run": str(run),
        }
        return self.client.post(path, json=BODIES[path], headers=headers), run

    def span(self, name: str) -> ReadableSpan:
        """The span of the latest request of that name."""
        spans = [s for s in self.exporter.get_finished_spans() if s.name == name]
        return spans[-1]

    def usage_rows(self) -> int:
        ((count,),) = owner_rows(self.database, "SELECT count(*) FROM gateway.usage")
        return count

    def counter_rows(self) -> int:
        ((count,),) = owner_rows(
            self.database, "SELECT count(*) FROM gateway.budget_counters"
        )
        return count

    def refusal_rows(self, reason: str) -> list[dict]:
        names = ("tenant", "agent", "purpose", "outcome", "suppressed")
        rows = owner_rows(
            self.database,
            "SELECT tenant, agent, purpose, outcome, suppressed FROM audit.events "
            "WHERE reason = %s ORDER BY recorded_at",
            (reason,),
        )
        return [dict(zip(names, row, strict=True)) for row in rows]

    def calls_counted(self) -> dict[tuple, float]:
        counted: dict[tuple, float] = {}
        data = self.reader.get_metrics_data()
        for resource in data.resource_metrics if data else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == "meridian.gateway.calls":
                        for point in metric.data.data_points:
                            attributes = dict(point.attributes)
                            key = (
                                attributes["meridian.outcome"],
                                attributes.get("meridian.reason"),
                            )
                            counted[key] = counted.get(key, 0) + point.value
        return counted


@pytest.fixture
def make_gateway(
    plant: Callable[..., Path], fresh_database: DatabaseHandle
) -> Callable[..., Gateway]:
    """A gateway on the given limiter whose claims-triage tenant has the given
    limits and whose chat route lists one EU deployment. The registry is planted
    by the first gateway of a test and shared by the others, which therefore have
    its limits; ``clock`` is shared by the gateways that are passed the same one."""
    planted: list[Path] = []

    def make(
        limiter: RateLimiter,
        clock: FakeClock | None = None,
        requests: int = 10,
        tokens_per_minute: int = 10000,
    ) -> Gateway:
        if not planted:
            directory = plant(
                (
                    "tenants.yaml",
                    SEED_LIMITS,
                    f"      requests_per_10_seconds: {requests}\n"
                    f"      tokens_per_minute: {tokens_per_minute}\n"
                    "      tokens_per_day: 300000\n"
                    "      cost_per_month_eur: 10\n",
                )
            )
            planted.append(pin_chat_route(directory, FIRST))
        (directory,) = planted
        clock = clock or FakeClock()
        exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
        provider = ScriptedProvider()
        settings = GatewaySettings(
            registry_dir=directory,
            mode="live",
            environment="test",
            database_url=fresh_database.dsn("model_gateway"),
        )
        app = create_app(
            settings,
            tracer_provider=make_tracer_provider("model-gateway", exporter),
            meter_provider=make_meter_provider("model-gateway", reader),
            providers={"azure-openai": provider},
            clock=clock,
            limiter=limiter,
        )
        return Gateway(
            app, TestClient(app), provider, exporter, reader, fresh_database, clock
        )

    return make


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@dataclass(slots=True)
class Shared:
    """Two gateways on one Redis, one clock and one set of keys."""

    first: Gateway
    second: Gateway


@pytest.fixture
def two_on_one_redis(
    make_gateway: Callable[..., Gateway], rate_keys: RateKeys
) -> Callable[..., Shared]:
    def make(requests: int = 10, tokens_per_minute: int = 10000) -> Shared:
        clock, prefix = FakeClock(), rate_keys.new_prefix()
        gateways = [
            make_gateway(
                RedisRateLimiter(rate_keys.new_client(), prefix=prefix, clock=clock),
                clock,
                requests,
                tokens_per_minute,
            )
            for _ in range(2)
        ]
        return Shared(*gateways)

    return make


# ── two processes, one window ───────────────────────────────────────────────
def test_two_gateways_on_one_store_share_the_request_window(
    two_on_one_redis: Callable[..., Shared],
) -> None:
    both = two_on_one_redis(requests=LIMIT)
    for gateway in (both.first, both.second, both.first, both.second):
        assert gateway.post()[0].status_code == 200

    from_first, _ = both.first.post()
    from_second, _ = both.second.post()

    for refused in (from_first, from_second):
        assert refused.status_code == HTTP_TOO_MANY
        assert refused.json() == {"detail": TENANT_RATE_LIMIT_REACHED}
        assert refused.headers["Retry-After"] == "10"
    assert len(both.first.provider.called) == 2
    assert len(both.second.provider.called) == 2


def test_the_request_window_opens_again_for_both_when_it_has_passed(
    two_on_one_redis: Callable[..., Shared],
) -> None:
    both = two_on_one_redis(requests=1)
    assert both.first.post()[0].status_code == 200
    assert both.second.post()[0].status_code == HTTP_TOO_MANY

    both.first.clock.advance(10)

    assert both.second.post()[0].status_code == 200
    assert both.first.post()[0].status_code == HTTP_TOO_MANY


def test_two_gateways_on_one_store_share_the_token_window(
    two_on_one_redis: Callable[..., Shared],
) -> None:
    # Room for two requests as sent, not for a third.
    both = two_on_one_redis(tokens_per_minute=2 * CHAT_TOKENS + 100)
    assert both.first.post()[0].status_code == 200
    assert both.second.post()[0].status_code == 200

    from_first, _ = both.first.post()
    from_second, _ = both.second.post()

    for refused in (from_first, from_second):
        assert refused.status_code == HTTP_TOO_MANY
        assert refused.headers["Retry-After"] == "60"


def test_the_two_purposes_of_a_tenant_share_one_window_across_gateways(
    two_on_one_redis: Callable[..., Shared],
) -> None:
    both = two_on_one_redis(requests=2)
    assert both.first.post(CHAT)[0].status_code == 200
    assert both.second.post(CHAT)[0].status_code == 200

    response, _ = both.first.post(EMBEDDINGS)

    assert response.status_code == HTTP_TOO_MANY


def test_two_tenants_do_not_share_a_window_across_gateways(
    two_on_one_redis: Callable[..., Shared],
) -> None:
    both = two_on_one_redis(requests=1)
    assert both.first.post()[0].status_code == 200
    assert both.second.post()[0].status_code == HTTP_TOO_MANY

    other, _ = both.second.post(tenant=OTHER_TENANT)

    assert other.status_code == 200


# ── a store that cannot be reached ──────────────────────────────────────────
@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
def test_a_store_that_cannot_be_reached_is_a_503_with_a_retry_hint(
    make_gateway: Callable[..., Gateway], path: str
) -> None:
    gateway = make_gateway(DownStore())

    response, _ = gateway.post(path)

    assert response.status_code == HTTP_UNAVAILABLE
    assert response.json() == {"detail": RATE_STORE_UNAVAILABLE}
    assert response.headers["Retry-After"] == str(RATE_STORE_RETRY_SECONDS)


def test_the_answer_says_nothing_of_the_store_but_that_it_is_unavailable() -> None:
    assert RATE_STORE_UNAVAILABLE == "the rate store is unavailable"
    assert 1 <= RATE_STORE_RETRY_SECONDS <= 10


@pytest.fixture
def redact_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The texts the gateway's redaction was asked to redact."""
    calls: list[str] = []
    real = redaction_module.redact

    def spy(text: str) -> Redaction:
        calls.append(text)
        return real(text)

    monkeypatch.setattr(redaction_module, "redact", spy)
    return calls


@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
def test_a_refused_call_is_redacted_reserved_and_called_for_nowhere(
    make_gateway: Callable[..., Gateway], redact_calls: list[str], path: str
) -> None:
    gateway = make_gateway(DownStore())

    response, _ = gateway.post(path)

    assert response.status_code == HTTP_UNAVAILABLE
    assert redact_calls == []
    assert gateway.provider.called == []
    assert gateway.usage_rows() == 0
    assert gateway.counter_rows() == 0
    assert "meridian.redactions" not in gateway.span(SPANS[path]).attributes


def test_the_store_failing_never_touches_the_circuit(
    make_gateway: Callable[..., Gateway],
) -> None:
    store = DownStore(down=False)
    gateway = make_gateway(store)
    gateway.provider.failing = True
    gateway.post()  # one failure the circuit counts
    store.down = True

    for _ in range(FAILURE_THRESHOLD + 2):
        assert gateway.post()[0].status_code == HTTP_UNAVAILABLE

    store.down = False
    gateway.post()
    # Had the refusals counted, the circuit would be open and skip the provider.
    assert gateway.provider.called == [FIRST, FIRST]


def test_the_store_is_asked_once_per_call_and_a_refusal_is_not_retried(
    make_gateway: Callable[..., Gateway],
) -> None:
    store = DownStore()
    gateway = make_gateway(store)

    gateway.post()

    assert store.asked == 1


def test_the_span_and_the_counter_say_refused_for_this_reason(
    make_gateway: Callable[..., Gateway],
) -> None:
    gateway = make_gateway(DownStore())
    for _ in range(3):
        gateway.post()

    assert gateway.span("gateway.chat").attributes["meridian.refusal"] == REASON
    assert gateway.calls_counted() == {("refused", REASON): 3}


@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
def test_the_refusal_leaves_one_audit_row_with_the_caller_and_the_purpose(
    make_gateway: Callable[..., Gateway], path: str
) -> None:
    gateway = make_gateway(DownStore())

    response, run_id = gateway.post(path)

    assert response.status_code == HTTP_UNAVAILABLE
    (row,) = audit_events(gateway.database, run_id)
    assert (row["service"], row["event"], row["outcome"]) == (
        "model-gateway",
        "model.call",
        "refused",
    )
    assert (row["tenant"], row["agent"], row["reason"]) == (TENANT, AGENT, REASON)
    assert row["call_id"] is not None
    assert row["suppressed"] == 0


def test_a_second_failure_inside_the_window_writes_no_second_row(
    make_gateway: Callable[..., Gateway],
) -> None:
    gateway = make_gateway(DownStore())
    for _ in range(3):
        assert gateway.post()[0].status_code == HTTP_UNAVAILABLE

    assert len(gateway.refusal_rows(REASON)) == 1

    gateway.clock.advance(REFUSAL_AUDIT_SECONDS)
    gateway.post()

    rows = gateway.refusal_rows(REASON)
    assert [row["suppressed"] for row in rows] == [0, 2]


def test_a_failed_audit_write_is_still_a_503_and_is_logged(
    make_gateway: Callable[..., Gateway],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = make_gateway(DownStore())

    def fail(dsn: str, event: AuditEvent) -> None:
        raise AuditUnavailable("down")

    monkeypatch.setattr(gateway_app, "write_audit", fail)
    caplog.set_level(logging.ERROR)

    response, _ = gateway.post()

    # The call is refused all the same; the shared handler's text is the one
    # every refusal whose audit row cannot be written is answered with.
    assert response.status_code == HTTP_UNAVAILABLE
    assert response.json() == {"detail": AUDIT_UNAVAILABLE}
    assert gateway.provider.called == []
    assert "audit write failed" in caplog.text
    assert gateway.calls_counted() == {("refused", REASON): 1}


def test_the_failure_is_one_log_line_with_a_class_name_and_no_address(
    make_gateway: Callable[..., Gateway], caplog: pytest.LogCaptureFixture
) -> None:
    port = closed_port()
    store = RedisRateLimiter(
        redis.Redis(
            host="127.0.0.1",
            port=port,
            socket_connect_timeout=0.1,
            socket_timeout=0.1,
            retry=Retry(NoBackoff(), 0),
        )
    )
    gateway = make_gateway(store)
    caplog.set_level(logging.DEBUG)

    response, _ = gateway.post()

    assert response.status_code == HTTP_UNAVAILABLE
    assert response.json() == {"detail": RATE_STORE_UNAVAILABLE}
    ours = [r for r in caplog.records if "rate store" in r.getMessage()]
    assert len(ours) == 1
    assert "RateStoreUnavailable" in ours[0].getMessage()
    assert "127.0.0.1" not in caplog.text
    assert str(port) not in caplog.text
    assert "127.0.0.1" not in response.text


def test_a_closed_port_is_the_same_503_without_a_stub(
    make_gateway: Callable[..., Gateway],
) -> None:
    store = RedisRateLimiter(
        redis.Redis(
            host="127.0.0.1",
            port=closed_port(),
            socket_connect_timeout=0.1,
            socket_timeout=0.1,
            retry=Retry(NoBackoff(), 0),
        )
    )
    gateway = make_gateway(store)

    response, run_id = gateway.post()

    assert response.status_code == HTTP_UNAVAILABLE
    assert response.headers["Retry-After"] == str(RATE_STORE_RETRY_SECONDS)
    assert gateway.provider.called == []
    (row,) = audit_events(gateway.database, run_id)
    assert row["reason"] == REASON


def test_a_request_too_large_is_still_a_413_while_the_store_is_down(
    make_gateway: Callable[..., Gateway],
) -> None:
    # Answered from the limits alone, before any store is asked (the order of
    # the checks does not change with the store).
    store = RedisRateLimiter(
        redis.Redis(host="127.0.0.1", port=closed_port(), retry=Retry(NoBackoff(), 0))
    )
    gateway = make_gateway(store, tokens_per_minute=CHAT_TOKENS - 1)

    response, _ = gateway.post()

    assert response.status_code == 413
    assert "Retry-After" not in response.headers
