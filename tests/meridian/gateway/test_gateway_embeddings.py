"""POST /v1/embeddings: the controls of a chat call, on the embedding route (S045).

Replay mode and live mode, the real ledger on the throwaway PostgreSQL, the
limiter and the circuit breaker on a fake clock, the metrics in an in-memory
reader. Live mode runs over a scripted provider that answers or raises per
deployment and records every call, so a test can say that a deployment the
policy filtered out was never reached. Nothing here reaches a network or Azure.
Limits and prices are planted into a copy of the registry.
"""

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import (
    GLOBAL_EMBEDDING_YAML,
    REGISTRY_DIR,
    REPLAY_ENTRY,
    SECOND_EMBEDDING_YAML,
    FakeClock,
    assert_spans_hold_no_exception_and_no_canary,
    audit_events,
    owner_rows,
    pin_embedding_route,
)

from meridian.platform.common import audit
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.throttle import REFUSAL_AUDIT_SECONDS
from meridian.platform.gateway import app as gateway_app
from meridian.platform.gateway.app import (
    PROVIDER_FAILED,
    PROVIDER_TIMED_OUT,
    TENANT_BUDGET_USED_UP,
    TENANT_RATE_LIMIT_REACHED,
    TENANT_REQUEST_TOO_LARGE,
    create_app,
)
from meridian.platform.gateway.budget import (
    MICRO,
    chat_estimate,
    cost_micro_eur,
    embedding_estimate,
)
from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest
from meridian.platform.gateway.operations import embedding_operation
from meridian.platform.gateway.providers.azure_openai import AzureOpenAIProvider
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ProviderError,
    ProviderReply,
)
from meridian.platform.gateway.replay import ReplayProvider, replay_embedding
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.registry import Registry, load_registry
from meridian.platform.registry.checks import run_checks
from meridian.platform.registry.models import Deployment

PATH = "/v1/embeddings"
CLAIM_TEXT = "claimant-secret-text-42"
INPUT_CANARY = "CANARY-input-text-5521"
CANARY = "CANARY-provider-internals-9917"
# A recognisable component of a vector, and the digits of it a log, a span or a
# label would carry even if it were cut short (T-56).
COMPONENT = 0.123456789
COMPONENT_DIGITS = "0.123456"
PROVIDER_MODEL = "text-embedding-3-large"
INPUT_TOKENS = 9
DIMENSIONS = 1024
TEXTS = (CLAIM_TEXT, "second text")
BODY = {"inputs": list(TEXTS)}
REQUEST = EmbeddingRequest.model_validate(BODY)
ESTIMATE = embedding_estimate(REQUEST).tokens
REPLAY_INPUT_TOKENS = sum(-(-len(text) // 4) for text in TEXTS)
EU = "aoai-sdc-text-embedding-3-large"
SECOND = "aoai-sdc-text-embedding-3-large-second"
GLOBAL = "aoai-sdc-text-embedding-3-large-global"
OCTOBER_FIRST = date(2026, 10, 1)
RETRY_AFTER = "Retry-After"
HTTP_TOO_MANY = 429
HTTP_TOO_LARGE = 413
HTTP_UNAVAILABLE = 503
# The tenant's row in tenants.yaml as the seed has it (claims-triage is the
# first), replaced by ``limits_block`` when a test plants its own.
SEED_LIMITS = (
    "      requests_per_10_seconds: 10\n"
    "      tokens_per_minute: 10000\n"
    "      tokens_per_day: 300000\n"
    "      cost_per_month_eur: 10\n"
)
# The replay embedding deployment's price as the seed has it (nothing), and a
# price a quota can be exhausted with.
FREE_REPLAY_PRICE = (
    "      input_per_million_tokens: 0\n"
    "      source: Runs inside the platform; no provider call\n"
)
PAID_REPLAY_PRICE = FREE_REPLAY_PRICE.replace(
    "input_per_million_tokens: 0", "input_per_million_tokens: 100000"
)


def limits_block(
    requests: int = 10,
    tokens_per_minute: int = 10000,
    tokens_per_day: int = 300000,
    cost_per_month_eur: Decimal | int = 10,
) -> str:
    return (
        f"      requests_per_10_seconds: {requests}\n"
        f"      tokens_per_minute: {tokens_per_minute}\n"
        f"      tokens_per_day: {tokens_per_day}\n"
        f"      cost_per_month_eur: {cost_per_month_eur}\n"
    )


# ── providers: the replay one watched, and a scripted one for live mode ─────
@dataclass(frozen=True, slots=True)
class Outcome:
    error: Exception | None = None
    takes: float = 0.0
    # What the provider answers instead of one good vector per input.
    reply: EmbeddingReply | None = None


OK = Outcome()


def with_status(kind: str, status: int) -> Outcome:
    return Outcome(ProviderError(kind, status))  # type: ignore[arg-type]


def without_status(kind: str) -> Outcome:
    return Outcome(ProviderError(kind))  # type: ignore[arg-type]


class SpyReplay:
    """The replay provider, recording what reached it."""

    def __init__(self) -> None:
        self.inner = ReplayProvider()
        self.called: list[str] = []
        self.chat_called: list[str] = []

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        self.chat_called.append(deployment.id)
        return self.inner.chat(deployment, request, timeout_seconds=timeout_seconds)

    def embed(
        self,
        deployment: Deployment,
        request: EmbeddingRequest,
        *,
        timeout_seconds: float,
    ) -> EmbeddingReply:
        self.called.append(deployment.id)
        return self.inner.embed(deployment, request, timeout_seconds=timeout_seconds)


def vector_of(length: int) -> tuple[float, ...]:
    return (COMPONENT,) + (0.0,) * (length - 1)


def one_vector() -> tuple[float, ...]:
    return vector_of(DIMENSIONS)


def answering(*vectors: tuple[float, ...]) -> Outcome:
    """An answer of exactly these vectors, whatever was asked."""
    return Outcome(
        reply=EmbeddingReply(
            embeddings=vectors, model=PROVIDER_MODEL, input_tokens=INPUT_TOKENS
        )
    )


class ScriptedEmbedder:
    """Answers or raises per deployment ID, after moving the fake clock. It has
    no ``chat``: the walk of an embedding must not need one."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.outcomes: dict[str, Outcome] = {EU: OK, SECOND: OK, GLOBAL: OK}
        self.called: list[str] = []
        self.requests: list[EmbeddingRequest] = []
        # Runs inside ``embed``, before it answers: a test looks at the world
        # from where the provider stands.
        self.before: Callable[[Deployment], None] | None = None

    def embed(
        self,
        deployment: Deployment,
        request: EmbeddingRequest,
        *,
        timeout_seconds: float,
    ) -> EmbeddingReply:
        self.called.append(deployment.id)
        self.requests.append(request)
        if self.before is not None:
            self.before(deployment)
        outcome = self.outcomes[deployment.id]
        self.clock.advance(outcome.takes)
        if outcome.error is not None:
            raise outcome.error
        if outcome.reply is not None:
            return outcome.reply
        return EmbeddingReply(
            embeddings=tuple(one_vector() for _ in request.inputs),
            model=PROVIDER_MODEL,
            input_tokens=INPUT_TOKENS,
        )


class Today:
    """The ledger's day, moved by hand."""

    def __init__(self, day: date = OCTOBER_FIRST) -> None:
        self.day = day

    def __call__(self) -> date:
        return self.day


@dataclass(slots=True)
class Gateway:
    client: TestClient
    provider: ScriptedEmbedder | SpyReplay
    clock: FakeClock
    today: Today
    exporter: InMemorySpanExporter
    reader: InMemoryMetricReader
    database: DatabaseHandle
    registry: Registry

    def post(
        self,
        tenant: str = "claims-triage",
        agent: str = "claims-triage",
        body: object | None = None,
    ) -> tuple[httpx.Response, uuid.UUID]:
        run_id = uuid.uuid4()
        response = self.client.post(
            PATH,
            json=BODY if body is None else body,
            headers={
                "X-Meridian-Tenant": tenant,
                "X-Meridian-Agent": agent,
                "X-Meridian-Run": str(run_id),
            },
        )
        return response, run_id

    def post_chat(self, text: str = "hello") -> httpx.Response:
        return self.client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": text}]},
            headers={
                "X-Meridian-Tenant": "claims-triage",
                "X-Meridian-Agent": "claims-triage",
                "X-Meridian-Run": str(uuid.uuid4()),
            },
        )

    def deployment(self, deployment_id: str) -> Deployment:
        found = self.registry.deployment(deployment_id)
        assert found is not None
        return found

    def reserved_micro(self, deployment_id: str) -> int:
        return cost_micro_eur(
            self.deployment(deployment_id).price, self.registry.exchange, ESTIMATE, 0
        )

    def settled_micro(self, deployment_id: str, input_tokens: int) -> int:
        return cost_micro_eur(
            self.deployment(deployment_id).price,
            self.registry.exchange,
            input_tokens,
            0,
        )

    def usage(self) -> list[dict]:
        names = [
            "call_id",
            "deployment",
            "state",
            "reserved_tokens",
            "charged_tokens",
            "charged_micro_eur",
            "input_tokens",
            "output_tokens",
            "tenant",
            "agent",
        ]
        rows = owner_rows(
            self.database,
            "SELECT call_id, deployment, state, reserved_tokens, charged_tokens, "
            "charged_micro_eur, input_tokens, output_tokens, tenant, agent "
            "FROM gateway.usage ORDER BY reserved_at, deployment",
        )
        return [dict(zip(names, row, strict=True)) for row in rows]

    def counters(self) -> dict[tuple[str, date], int]:
        rows = owner_rows(
            self.database,
            "SELECT kind, period_start, amount FROM gateway.budget_counters",
        )
        return {(kind, period): amount for kind, period, amount in rows}

    def refused_rows(self, reason: str) -> int:
        ((count,),) = owner_rows(
            self.database,
            "SELECT count(*) FROM audit.events "
            "WHERE outcome = 'refused' AND reason = %s",
            (reason,),
        )
        return count

    def suppressed_counts(self, reason: str) -> list[int | None]:
        rows = owner_rows(
            self.database,
            "SELECT suppressed FROM audit.events "
            "WHERE outcome = 'refused' AND reason = %s ORDER BY recorded_at",
            (reason,),
        )
        return [suppressed for (suppressed,) in rows]

    def span(self) -> ReadableSpan:
        (span,) = [
            s
            for s in self.exporter.get_finished_spans()
            if s.name == "gateway.embeddings"
        ]
        return span

    def points(self, name: str) -> list[tuple[dict, float]]:
        data = self.reader.get_metrics_data()
        found: list[tuple[dict, float]] = []
        for resource in data.resource_metrics if data else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == name:
                        points = metric.data.data_points
                        found += [(dict(p.attributes), p.value) for p in points]
        return found

    def calls_counted(self) -> dict[tuple, float]:
        return {
            (a["meridian.outcome"], a.get("meridian.reason")): v
            for a, v in self.points("meridian.gateway.calls")
        }

    def everything_it_wrote(self) -> str:
        """Every audit row, every ledger row, every span and every metric as one
        string: what a canary must not be found in."""
        audit_rows = owner_rows(self.database, "SELECT * FROM audit.events")
        metrics = [
            self.points(name)
            for name in (
                "meridian.gateway.tokens",
                "meridian.gateway.cost",
                "meridian.gateway.calls",
            )
        ]
        return str([audit_rows, self.usage(), metrics])


# ── registries and gateways ─────────────────────────────────────────────────
@pytest.fixture
def planted(plant: Callable[..., Path]) -> Path:
    """The registry that also holds a second EU embedding deployment and a
    global one; each test pins the route it needs."""
    return plant(
        (
            "models.yaml",
            REPLAY_ENTRY,
            SECOND_EMBEDDING_YAML + GLOBAL_EMBEDDING_YAML + REPLAY_ENTRY,
        )
    )


@pytest.fixture
def registry_with(plant: Callable[..., Path], planted: Path) -> Callable[..., Path]:
    """A registry whose embedding route lists ``candidates``, whose claims-triage
    tenant has the given limits and whose replay embedding has a price when
    ``paid`` says so."""

    def make(*candidates: str, paid: bool = False, **limits: object) -> Path:
        if limits:
            plant(("tenants.yaml", SEED_LIMITS, limits_block(**limits)))
        if paid:
            plant(("models.yaml", FREE_REPLAY_PRICE, PAID_REPLAY_PRICE))
        if candidates:
            pin_embedding_route(planted, *candidates)
        assert run_checks(load_registry(planted)) == ()
        return planted

    return make


@pytest.fixture
def build(fresh_database: DatabaseHandle) -> Callable[..., Gateway]:
    def make(registry_dir: Path = REGISTRY_DIR, mode: str = "live") -> Gateway:
        clock, today = FakeClock(), Today()
        exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
        provider: ScriptedEmbedder | SpyReplay = (
            ScriptedEmbedder(clock) if mode == "live" else SpyReplay()
        )
        kind = "azure-openai" if mode == "live" else "replay"
        settings = GatewaySettings(
            registry_dir=registry_dir,
            mode=mode,  # type: ignore[arg-type]
            environment="test",
            database_url=fresh_database.dsn("model_gateway"),
        )
        app = create_app(
            settings,
            tracer_provider=make_tracer_provider("model-gateway", exporter),
            meter_provider=make_meter_provider("model-gateway", reader),
            providers={kind: provider},  # type: ignore[dict-item]
            clock=clock,
            today=today,
        )
        return Gateway(
            TestClient(app),
            provider,
            clock,
            today,
            exporter,
            reader,
            fresh_database,
            load_registry(registry_dir),
        )

    return make


@pytest.fixture
def replay(build: Callable[..., Gateway]) -> Gateway:
    return build(mode="replay")


@pytest.fixture
def live(registry_with: Callable[..., Path], build: Callable[..., Gateway]) -> Gateway:
    """Live mode over the scripted provider, the embedding route of one
    deployment."""
    return build(registry_with(EU))


# ── a completed call, in replay mode ────────────────────────────────────────
def test_an_embedding_call_is_answered_audited_and_traced(replay: Gateway) -> None:
    response, run_id = replay.post()

    assert response.status_code == 200
    assert replay.provider.called == ["replay-embedding"]
    assert replay.provider.chat_called == []  # type: ignore[union-attr]
    body = response.json()
    assert set(body) == {
        "call_id",
        "mode",
        "deployment",
        "provider",
        "model",
        "dimensions",
        "embeddings",
        "usage",
    }
    assert uuid.UUID(body["call_id"])
    assert (body["mode"], body["deployment"], body["provider"], body["model"]) == (
        "replay",
        "replay-embedding",
        "replay",
        "replay-embedding",
    )
    assert body["dimensions"] == DIMENSIONS
    assert body["usage"] == {"input_tokens": REPLAY_INPUT_TOKENS}
    assert audit_events(replay.database, run_id) == [
        {
            "service": "model-gateway",
            "event": "model.call",
            "outcome": "completed",
            "tenant": "claims-triage",
            "agent": "claims-triage",
            "run_id": run_id,
            "reference": None,
            "deployment": "replay-embedding",
            "provider": "replay",
            "model": "replay-embedding",
            "input_tokens": REPLAY_INPUT_TOKENS,
            "output_tokens": 0,
            "reason": None,
            "data_class": "personal",
            "sku": None,
            "region": None,
            "residency": "eu-region",
            "call_id": uuid.UUID(body["call_id"]),
            "http_status": None,
            "provider_model": "replay-embedding",
            "suppressed": None,
            "tool": None,
        }
    ]
    assert dict(replay.span().attributes) == {
        "meridian.tenant": "claims-triage",
        "meridian.agent": "claims-triage",
        "meridian.run_id": str(run_id),
        "meridian.call_id": body["call_id"],
        "meridian.cost_micro_eur": 0,
        "meridian.deployment": "replay-embedding",
        "meridian.provider": "replay",
        "meridian.mode": "replay",
        "meridian.residency": "eu-region",
        "meridian.data_class": "personal",
        "gen_ai.request.model": "replay-embedding",
        "gen_ai.response.model": "replay-embedding",
        "gen_ai.usage.input_tokens": REPLAY_INPUT_TOKENS,
        "gen_ai.usage.output_tokens": 0,
        "meridian.attempts": 1,
        "meridian.skipped": 0,
    }


def test_the_answer_holds_one_vector_per_input_in_input_order(replay: Gateway) -> None:
    texts = ["storm damage", "water in the cellar", "storm damage", "fire"]

    response, _ = replay.post(body={"inputs": texts})

    vectors = response.json()["embeddings"]
    assert vectors == [list(replay_embedding(t, DIMENSIONS)) for t in texts]
    assert vectors[0] == vectors[2]
    assert vectors[0] != vectors[1]
    assert all(len(v) == DIMENSIONS for v in vectors)


def test_a_completed_call_is_one_settled_ledger_row_at_the_embedding_price(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(paid=True), mode="replay")

    response, run_id = gateway.post()

    assert response.status_code == 200
    call_id = uuid.UUID(response.json()["call_id"])
    (usage,) = gateway.usage()
    charge = gateway.settled_micro("replay-embedding", REPLAY_INPUT_TOKENS)
    assert charge > 0
    assert usage["call_id"] == call_id
    assert (usage["deployment"], usage["state"]) == ("replay-embedding", "settled")
    assert (usage["input_tokens"], usage["output_tokens"]) == (REPLAY_INPUT_TOKENS, 0)
    assert usage["charged_tokens"] == REPLAY_INPUT_TOKENS
    assert usage["charged_micro_eur"] == charge
    assert usage["reserved_tokens"] == ESTIMATE
    assert gateway.counters() == {
        ("tokens-day", OCTOBER_FIRST): REPLAY_INPUT_TOKENS,
        ("cost-month", OCTOBER_FIRST): charge,
    }
    assert dict(gateway.span().attributes)["meridian.cost_micro_eur"] == charge
    (row,) = audit_events(gateway.database, run_id)
    assert (row["call_id"], row["outcome"]) == (call_id, "completed")


def test_the_same_inputs_get_the_same_vectors_twice(replay: Gateway) -> None:
    first, _ = replay.post()
    second, _ = replay.post()

    assert first.json()["embeddings"] == second.json()["embeddings"]
    assert first.json()["call_id"] != second.json()["call_id"]


# ── the request is checked before anything else happens ─────────────────────
def message_body(inputs: object) -> dict:
    return {"inputs": inputs}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(message_body([]), id="no-input"),
        pytest.param(message_body(["x"] * 17), id="seventeen-inputs"),
        pytest.param(message_body([""]), id="empty-input"),
        pytest.param(message_body(["x" * 8001]), id="8001-characters"),
        pytest.param(message_body(["fine", ""]), id="one-empty-among-others"),
        pytest.param(message_body(["before\x00after"]), id="nul"),
        pytest.param(message_body(["x", 3]), id="a-number"),
        pytest.param(message_body(["x", None]), id="a-null"),
        pytest.param(message_body("x"), id="a-string-for-the-list"),
        pytest.param(message_body(None), id="null-inputs"),
        pytest.param({"inputs": ["x"], "model": "text-embedding-3-large"}, id="model"),
        pytest.param({"inputs": ["x"], "dimensions": 8}, id="dimensions"),
        pytest.param({"inputs": ["x"], "encoding_format": "float"}, id="format"),
        pytest.param({"input": ["x"]}, id="a-wrong-name"),
        pytest.param({}, id="empty-body"),
    ],
)
def test_an_invalid_body_is_a_422_and_reaches_no_provider(
    replay: Gateway, body: dict
) -> None:
    response, _ = replay.post(body=body)

    assert response.status_code == 422
    assert replay.provider.called == []
    assert replay.usage() == []


@pytest.mark.parametrize(
    "inputs",
    [
        pytest.param(["x"], id="one"),
        pytest.param([f"text {n}" for n in range(16)], id="sixteen"),
        pytest.param(["x" * 8000], id="8000-characters"),
        pytest.param(["é" * 8000], id="8000-two-byte-characters"),
    ],
)
def test_the_limits_themselves_are_accepted(replay: Gateway, inputs: list[str]) -> None:
    response, _ = replay.post(body=message_body(inputs))

    assert response.status_code == 200
    assert len(response.json()["embeddings"]) == len(inputs)


def test_a_422_does_not_echo_what_was_sent(replay: Gateway) -> None:
    body = {"inputs": [INPUT_CANARY, ""], "dimensions": INPUT_CANARY}

    response, _ = replay.post(body=body)

    assert response.status_code == 422
    assert INPUT_CANARY not in response.text


def test_a_nul_byte_is_a_422_and_not_an_outage(replay: Gateway) -> None:
    response, _ = replay.post(body=message_body(["before\x00after"]))

    assert response.status_code == 422
    assert "before" not in response.text


@pytest.mark.parametrize(
    "drop", ["X-Meridian-Tenant", "X-Meridian-Agent", "X-Meridian-Run"]
)
def test_a_missing_header_is_a_422(replay: Gateway, drop: str) -> None:
    sent = {
        "X-Meridian-Tenant": "claims-triage",
        "X-Meridian-Agent": "claims-triage",
        "X-Meridian-Run": str(uuid.uuid4()),
    }

    response = replay.client.post(
        PATH, json=BODY, headers={k: v for k, v in sent.items() if k != drop}
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("X-Meridian-Tenant", "Claims_Triage"),
        ("X-Meridian-Agent", "has space"),
        ("X-Meridian-Run", "not-a-uuid"),
    ],
)
def test_a_malformed_header_is_a_422(replay: Gateway, header: str, value: str) -> None:
    headers = {
        "X-Meridian-Tenant": "claims-triage",
        "X-Meridian-Agent": "claims-triage",
        "X-Meridian-Run": str(uuid.uuid4()),
    } | {header: value}

    assert replay.client.post(PATH, json=BODY, headers=headers).status_code == 422


# ── policy: an unknown tenant, an agent the tenant may not run ──────────────
@pytest.mark.parametrize(
    ("tenant", "agent", "reason", "data_class"),
    [
        ("no-such-tenant", "claims-triage", "unknown-tenant", None),
        ("claims-triage", "some-other-agent", "agent-not-allowed", "personal"),
    ],
)
def test_a_refused_request_answers_403_and_names_the_replay_embedding_deployment(
    replay: Gateway, tenant: str, agent: str, reason: str, data_class: str | None
) -> None:
    response, run_id = replay.post(tenant, agent)

    assert response.status_code == 403
    assert response.json() == {"detail": "request refused"}
    assert "embeddings" not in response.text
    assert replay.provider.called == []
    assert replay.usage() == []
    (event,) = audit_events(replay.database, run_id)
    assert (event["service"], event["event"], event["outcome"]) == (
        "model-gateway",
        "model.call",
        "refused",
    )
    assert (event["tenant"], event["agent"]) == (tenant, agent)
    assert (event["reason"], event["data_class"]) == (reason, data_class)
    # T-39: where the call would have gone is the embedding deployment, not chat's.
    assert (event["deployment"], event["provider"], event["model"]) == (
        "replay-embedding",
        "replay",
        "replay-embedding",
    )
    assert (event["input_tokens"], event["output_tokens"]) == (None, None)
    assert dict(replay.span().attributes)["meridian.refusal"] == reason
    assert dict(replay.span().attributes)["meridian.deployment"] == "replay-embedding"


def test_unknown_tenants_leave_one_row_a_minute_and_the_next_counts_the_rest(
    replay: Gateway,
) -> None:
    for n in range(5):
        assert replay.post(tenant=f"made-up-tenant-{n}")[0].status_code == 403

    assert replay.refused_rows("unknown-tenant") == 1
    assert replay.suppressed_counts("unknown-tenant") == [0]
    replay.clock.advance(REFUSAL_AUDIT_SECONDS)
    replay.post(tenant="made-up-tenant-last")

    assert replay.suppressed_counts("unknown-tenant") == [0, 4]
    assert replay.calls_counted() == {("refused", "unknown-tenant"): 6}


def test_a_policy_refusal_is_audited_once_per_tenant_and_reason_never_per_agent(
    replay: Gateway,
) -> None:
    for agent in ("agent-one", "agent-two", "agent-three"):
        assert replay.post(agent=agent)[0].status_code == 403

    assert replay.refused_rows("agent-not-allowed") == 1
    assert replay.suppressed_counts("agent-not-allowed") == [0]
    assert replay.calls_counted() == {("refused", "agent-not-allowed"): 3}


# ── the request rate ────────────────────────────────────────────────────────
def test_the_request_over_the_rate_limit_is_a_429_with_retry_after(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(requests=3), mode="replay")
    for _ in range(3):
        assert gateway.post()[0].status_code == 200
    gateway.provider.called.clear()

    response, run_id = gateway.post()

    assert response.status_code == HTTP_TOO_MANY
    assert response.json() == {"detail": TENANT_RATE_LIMIT_REACHED}
    assert response.headers[RETRY_AFTER] == "10"
    assert gateway.provider.called == []
    assert len(gateway.usage()) == 3
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"]) == ("refused", "tenant-request-rate")
    assert (row["deployment"], row["provider"]) == ("replay-embedding", "replay")
    gateway.clock.advance(10)
    assert gateway.post()[0].status_code == 200


# ── the token window ────────────────────────────────────────────────────────
def test_the_token_rate_limit_is_a_429_with_retry_after(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(tokens_per_minute=2 * ESTIMATE + 1), mode="replay")
    for _ in range(2):
        assert gateway.post()[0].status_code == 200
    gateway.provider.called.clear()

    response, run_id = gateway.post()

    assert response.status_code == HTTP_TOO_MANY
    assert response.json() == {"detail": TENANT_RATE_LIMIT_REACHED}
    assert response.headers[RETRY_AFTER] == "60"
    assert gateway.provider.called == []
    (row,) = audit_events(gateway.database, run_id)
    assert row["reason"] == "tenant-token-rate"


def test_a_request_larger_than_the_token_limit_alone_is_a_413(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(tokens_per_minute=ESTIMATE - 1), mode="replay")

    response, run_id = gateway.post()

    assert response.status_code == HTTP_TOO_LARGE
    assert response.json() == {"detail": TENANT_REQUEST_TOO_LARGE}
    assert RETRY_AFTER not in response.headers
    assert gateway.provider.called == []
    assert gateway.usage() == []
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"]) == ("refused", "tenant-request-too-large")
    assert row["deployment"] == "replay-embedding"


def test_a_request_whose_estimate_equals_the_limit_is_admitted(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(tokens_per_minute=ESTIMATE), mode="replay")

    assert gateway.post()[0].status_code == 200


def test_sixteen_long_inputs_are_larger_than_the_tenants_window_and_a_413(
    replay: Gateway,
) -> None:
    # 16 x 8000 characters is about 43,000 estimated tokens; the seeded tenant
    # may use 10,000 a minute, so T-55's bound on one batch is the tenant's own.
    response, _ = replay.post(body={"inputs": ["x" * 8000] * 16})

    assert response.status_code == HTTP_TOO_LARGE
    assert replay.provider.called == []


def test_chat_and_embeddings_draw_on_one_token_window(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    text = "hello"
    chat_request = ChatRequest.model_validate(
        {"messages": [{"role": "user", "content": text}]}
    )
    chat_tokens = chat_estimate(chat_request).tokens
    gateway = build(
        registry_with(tokens_per_minute=chat_tokens + ESTIMATE - 1), mode="replay"
    )
    assert gateway.post_chat(text).status_code == 200

    response, _ = gateway.post()

    assert response.status_code == HTTP_TOO_MANY
    assert response.json() == {"detail": TENANT_RATE_LIMIT_REACHED}
    assert gateway.provider.called == []


def test_a_request_the_rate_limit_refuses_touches_no_ledger(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(EU, requests=1))
    gateway.provider.outcomes[EU] = with_status("unavailable", 503)
    gateway.post()  # one counted failure
    before = gateway.counters()

    for _ in range(5):
        assert gateway.post()[0].status_code == HTTP_TOO_MANY

    assert gateway.provider.called == [EU]
    assert gateway.counters() == before
    assert len(gateway.usage()) == 1


# ── the daily token budget ──────────────────────────────────────────────────
def test_the_daily_budget_refuses_the_call_that_does_not_fit_and_opens_next_day(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    # Room for one reservation, then only what the first call's real count left.
    day_limit = ESTIMATE + REPLAY_INPUT_TOKENS - 1
    gateway = build(registry_with(tokens_per_day=day_limit), mode="replay")
    assert gateway.post()[0].status_code == 200
    assert gateway.counters()[("tokens-day", OCTOBER_FIRST)] == REPLAY_INPUT_TOKENS
    gateway.provider.called.clear()

    response, run_id = gateway.post()

    assert response.status_code == HTTP_TOO_MANY
    assert response.json() == {"detail": TENANT_BUDGET_USED_UP}
    assert RETRY_AFTER not in response.headers
    assert gateway.provider.called == []
    assert len(gateway.usage()) == 1  # the refusal made no row
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"]) == ("refused", "tenant-token-budget")
    assert (row["deployment"], row["provider"]) == ("replay-embedding", "replay")
    gateway.today.day = date(2026, 10, 2)
    assert gateway.post()[0].status_code == 200


# ── the monthly cost quota ──────────────────────────────────────────────────
def test_the_monthly_quota_refuses_the_call_that_does_not_fit_and_opens_next_month(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    probe = build(registry_with(paid=True), mode="replay")
    quota_micro = probe.reserved_micro("replay-embedding") + probe.settled_micro(
        "replay-embedding", REPLAY_INPUT_TOKENS
    )
    assert quota_micro > 0
    gateway = build(
        registry_with(cost_per_month_eur=Decimal(quota_micro - 1) / MICRO),
        mode="replay",
    )
    assert gateway.post()[0].status_code == 200
    gateway.provider.called.clear()

    response, run_id = gateway.post()

    assert response.status_code == HTTP_TOO_MANY
    assert response.json() == {"detail": TENANT_BUDGET_USED_UP}
    assert gateway.provider.called == []
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"]) == ("refused", "tenant-cost-budget")
    assert len(gateway.usage()) == 1
    gateway.today.day = date(2026, 11, 1)
    assert gateway.post()[0].status_code == 200


# ── live mode: the route's candidates, the filter, the walk ─────────────────
def test_a_live_call_reaches_the_routed_deployment_and_is_audited_and_traced(
    live: Gateway,
) -> None:
    response, run_id = live.post()

    assert response.status_code == 200
    body = response.json()
    assert (body["mode"], body["deployment"], body["provider"]) == (
        "live",
        EU,
        "azure-openai",
    )
    # The registry's names; the provider's own string goes to the span only.
    assert (body["model"], body["dimensions"]) == ("text-embedding-3-large", DIMENSIONS)
    assert body["embeddings"] == [list(one_vector())] * 2
    assert body["usage"] == {"input_tokens": INPUT_TOKENS}
    assert live.provider.called == [EU]
    (request,) = live.provider.requests
    assert request.inputs == TEXTS
    expected_cost = live.settled_micro(EU, INPUT_TOKENS)
    assert expected_cost > 0
    (row,) = audit_events(live.database, run_id)
    assert (row["outcome"], row["deployment"], row["provider"], row["model"]) == (
        "completed",
        EU,
        "azure-openai",
        "text-embedding-3-large",
    )
    assert (row["input_tokens"], row["output_tokens"]) == (INPUT_TOKENS, 0)
    assert (row["sku"], row["region"], row["residency"]) == (
        "Standard",
        "swedencentral",
        "eu-region",
    )
    assert row["provider_model"] == PROVIDER_MODEL
    attributes = dict(live.span().attributes)
    assert attributes["meridian.cost_micro_eur"] == expected_cost
    assert attributes["gen_ai.usage.output_tokens"] == 0
    (usage,) = live.usage()
    assert (usage["state"], usage["charged_micro_eur"]) == ("settled", expected_cost)


def test_a_completed_call_adds_input_tokens_cost_and_one_call_to_the_metrics(
    live: Gateway,
) -> None:
    live.post()

    labels = {
        "meridian.tenant": "claims-triage",
        "meridian.agent": "claims-triage",
        "meridian.provider": "azure-openai",
        "gen_ai.request.model": "text-embedding-3-large",
    }
    tokens = {
        a["gen_ai.token.type"]: v for a, v in live.points("meridian.gateway.tokens")
    }
    assert tokens == {"input": INPUT_TOKENS, "output": 0}
    ((cost_labels, cost),) = live.points("meridian.gateway.cost")
    assert cost_labels == labels
    assert cost == pytest.approx(live.settled_micro(EU, INPUT_TOKENS) / MICRO)
    assert live.calls_counted() == {("completed", None): 1}


# T-44, for this route: the filter runs before any provider call.
@pytest.mark.parametrize(
    ("tenant", "reached", "residency", "data_class"),
    [
        ("claims-triage", EU, "eu-region", "personal"),
        ("development", GLOBAL, "global", "synthetic"),
    ],
)
def test_a_global_deployment_ahead_of_an_eu_one_is_reached_by_synthetic_data_only(
    registry_with: Callable[..., Path],
    build: Callable[..., Gateway],
    tenant: str,
    reached: str,
    residency: str,
    data_class: str,
) -> None:
    gateway = build(registry_with(GLOBAL, EU))

    response, run_id = gateway.post(tenant)

    assert response.status_code == 200
    assert response.json()["deployment"] == reached
    # The deployment the policy filtered out was never asked.
    assert gateway.provider.called == [reached]
    (event,) = audit_events(gateway.database, run_id)
    assert (event["deployment"], event["residency"], event["data_class"]) == (
        reached,
        residency,
        data_class,
    )


def test_a_personal_tenant_never_reaches_a_global_deployment_even_when_all_fail(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(GLOBAL, EU))
    gateway.provider.outcomes[EU] = with_status("unavailable", 503)

    response, _ = gateway.post("claims-triage")

    assert response.status_code == 502
    assert gateway.provider.called == [EU]


def test_a_tenant_no_candidate_may_reach_is_refused_without_a_provider_call(
    build: Callable[..., Gateway], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry checks refuse a route nobody can use, so the registry is
    # narrowed in memory after it was loaded.
    registry = load_registry(REGISTRY_DIR)
    narrowed = registry.model_copy(
        update={
            "deployments": tuple(
                d.model_copy(update={"data_classes": ("synthetic",)})
                if d.id == EU
                else d
                for d in registry.deployments
            )
        }
    )
    monkeypatch.setattr(gateway_app, "load_registry", lambda _: narrowed)
    gateway = build()

    response, run_id = gateway.post()

    assert response.status_code == 403
    assert gateway.provider.called == []
    (event,) = audit_events(gateway.database, run_id)
    assert (event["outcome"], event["reason"]) == ("refused", "no-allowed-deployment")
    # A live refusal names no deployment: nothing was chosen.
    assert event["deployment"] is None


def test_an_embedding_purpose_without_a_route_is_refused(
    build: Callable[..., Gateway], monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = load_registry(REGISTRY_DIR)
    narrowed = registry.model_copy(
        update={"routes": tuple(r for r in registry.routes if r.purpose != "embedding")}
    )
    monkeypatch.setattr(gateway_app, "load_registry", lambda _: narrowed)
    gateway = build()

    response, run_id = gateway.post()

    assert response.status_code == 403
    assert gateway.provider.called == []
    (event,) = audit_events(gateway.database, run_id)
    assert event["reason"] == "no-route"


def test_a_failing_first_candidate_is_followed_by_the_second_and_both_leave_rows(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(EU, SECOND))
    gateway.provider.outcomes[EU] = with_status("unavailable", 503)

    response, run_id = gateway.post()

    assert response.status_code == 200
    body = response.json()
    assert body["deployment"] == SECOND
    assert gateway.provider.called == [EU, SECOND]
    call_id = uuid.UUID(body["call_id"])
    failed, completed = audit_events(gateway.database, run_id)
    assert (failed["outcome"], failed["deployment"], failed["reason"]) == (
        "failed",
        EU,
        "unavailable",
    )
    assert failed["http_status"] == 503
    assert (completed["outcome"], completed["deployment"]) == ("completed", SECOND)
    assert failed["call_id"] == completed["call_id"] == call_id
    assert (completed["input_tokens"], completed["output_tokens"]) == (INPUT_TOKENS, 0)
    first, second = gateway.usage()
    assert (first["deployment"], first["state"]) == (EU, "kept")
    assert (second["deployment"], second["state"]) == (SECOND, "settled")
    parent = dict(gateway.span().attributes)
    assert (parent["meridian.attempts"], parent["meridian.skipped"]) == (2, 0)


def test_a_rejected_request_ends_the_walk_and_releases_the_reservation(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(EU, SECOND))
    gateway.provider.outcomes[EU] = with_status("rejected", 400)

    response, run_id = gateway.post()

    assert response.status_code == 502
    assert response.json() == {"detail": PROVIDER_FAILED}
    assert gateway.provider.called == [EU]  # no other candidate will do
    (usage,) = gateway.usage()
    assert (usage["deployment"], usage["state"], usage["charged_tokens"]) == (
        EU,
        "released",
        0,
    )
    assert gateway.counters() == {
        ("tokens-day", OCTOBER_FIRST): 0,
        ("cost-month", OCTOBER_FIRST): 0,
    }
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"], row["http_status"]) == (
        "failed",
        "rejected",
        400,
    )


def test_a_timeout_keeps_the_reservation_as_the_charge(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(EU))
    gateway.provider.outcomes[EU] = without_status("timeout")

    response, run_id = gateway.post()

    assert response.status_code == 504
    assert response.json() == {"detail": PROVIDER_TIMED_OUT}
    (usage,) = gateway.usage()
    assert (usage["state"], usage["charged_tokens"]) == ("kept", ESTIMATE)
    assert usage["charged_micro_eur"] == gateway.reserved_micro(EU)
    assert gateway.counters() == {
        ("tokens-day", OCTOBER_FIRST): ESTIMATE,
        ("cost-month", OCTOBER_FIRST): gateway.reserved_micro(EU),
    }
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"]) == ("failed", "timeout")
    assert gateway.points("meridian.gateway.tokens") == []


# T-54, whichever provider answers: the gateway checks the count and the length
# of the vectors itself, so a provider with no checks of its own is held to it.
@pytest.mark.parametrize(
    "vectors",
    [
        pytest.param((one_vector(),), id="too-few-vectors"),
        pytest.param((one_vector(),) * 3, id="too-many-vectors"),
        pytest.param((), id="no-vectors"),
        pytest.param((one_vector(), vector_of(DIMENSIONS - 1)), id="one-too-short"),
        pytest.param((one_vector(), vector_of(DIMENSIONS + 1)), id="one-too-long"),
    ],
)
def test_an_answer_of_the_wrong_count_or_length_is_a_502_and_a_kept_reservation(
    live: Gateway, vectors: tuple[tuple[float, ...], ...]
) -> None:
    live.provider.outcomes[EU] = answering(*vectors)

    response, run_id = live.post()

    assert response.status_code == 502
    assert response.json() == {"detail": PROVIDER_FAILED}
    assert "embeddings" not in response.text
    (row,) = audit_events(live.database, run_id)  # no completed row beside it
    assert (row["outcome"], row["reason"], row["deployment"]) == (
        "failed",
        "bad-response",
        EU,
    )
    (usage,) = live.usage()
    assert (usage["state"], usage["charged_tokens"]) == ("kept", ESTIMATE)
    assert live.points("meridian.gateway.tokens") == []


def test_a_short_vector_from_the_first_candidate_is_followed_by_the_second(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(EU, SECOND))
    gateway.provider.outcomes[EU] = answering(one_vector(), vector_of(DIMENSIONS - 1))

    response, run_id = gateway.post()

    assert response.status_code == 200
    body = response.json()
    assert body["deployment"] == SECOND
    assert body["embeddings"] == [list(one_vector())] * 2
    assert gateway.provider.called == [EU, SECOND]
    failed, completed = audit_events(gateway.database, run_id)
    assert (failed["outcome"], failed["deployment"], failed["reason"]) == (
        "failed",
        EU,
        "bad-response",
    )
    assert (completed["outcome"], completed["deployment"]) == ("completed", SECOND)
    first, second = gateway.usage()
    assert (first["deployment"], first["state"]) == (EU, "kept")
    assert (second["deployment"], second["state"]) == (SECOND, "settled")


def test_a_deployment_without_dimensions_cannot_be_called_for_embeddings() -> None:
    provider = ScriptedEmbedder(FakeClock())
    nameless = load_registry(REGISTRY_DIR).deployment(EU)
    assert nameless is not None
    nameless = nameless.model_copy(update={"dimensions": None})

    with pytest.raises(ValueError, match="dimensions"):
        embedding_operation(REQUEST).call(provider, nameless, 1.0)  # type: ignore[arg-type]

    assert provider.called == []  # refused before any request, as the adapters do


def test_the_reservation_exists_before_the_provider_is_called(live: Gateway) -> None:
    seen: list[list[dict]] = []
    live.provider.before = lambda _deployment: seen.append(live.usage())  # type: ignore[union-attr]

    live.post()

    ((during,),) = seen
    assert (during["state"], during["reserved_tokens"]) == ("reserved", ESTIMATE)
    assert during["charged_tokens"] == ESTIMATE  # charged until it is closed
    assert live.usage()[0]["state"] == "settled"


def test_an_unexpected_exception_from_the_provider_keeps_the_reservation(
    live: Gateway,
) -> None:
    live.provider.outcomes[EU] = Outcome(RuntimeError(f"{CANARY} {INPUT_CANARY}"))

    response, run_id = live.post()

    assert response.status_code == 500
    assert CANARY not in response.text
    (usage,) = live.usage()
    assert usage["state"] == "kept"
    (row,) = audit_events(live.database, run_id)
    assert (row["outcome"], row["reason"]) == ("failed", "internal")
    assert live.calls_counted() == {("failed", None): 1}


def test_when_the_audit_write_of_a_completed_call_fails_the_answer_has_no_vectors(
    live: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise psycopg.OperationalError("connection refused: password=hunter2")

    monkeypatch.setattr(audit, "connect", fail_connect)

    response, _ = live.post()

    assert response.status_code == HTTP_UNAVAILABLE
    assert live.provider.called == [EU]  # the audit, not the ledger, failed
    assert "embeddings" not in response.text
    assert "hunter2" not in response.text


def test_with_the_circuit_open_a_candidate_is_skipped_and_reserves_nothing(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(EU, SECOND))
    gateway.provider.outcomes[EU] = with_status("unavailable", 503)
    for _ in range(3):  # FAILURE_THRESHOLD
        assert gateway.post()[0].status_code == 200
    seen = len(gateway.usage())

    response, run_id = gateway.post()

    assert response.status_code == 200
    assert [u["deployment"] for u in gateway.usage()[seen:]] == [SECOND]
    first, _ = audit_events(gateway.database, run_id)
    assert (first["outcome"], first["deployment"], first["reason"]) == (
        "skipped",
        EU,
        "circuit-open",
    )


# ── live mode through the real adapter, over a mocked transport ─────────────
def adapter_answer(vectors: list[list[float]], tokens: int = 7) -> dict:
    return {
        "object": "list",
        "model": PROVIDER_MODEL,
        "data": [
            {"object": "embedding", "index": n, "embedding": vector}
            for n, vector in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }


def through_the_adapter(
    answer: dict, registry_dir: Path, database: DatabaseHandle
) -> tuple[TestClient, list[dict]]:
    """A gateway over the real Azure adapter whose transport answers ``answer``
    and records the JSON body of every request it gets."""
    sent: list[dict] = []

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=json.dumps(answer).encode(),
            headers={"content-type": "application/json"},
        )

    adapter = AzureOpenAIProvider(
        {"sdc": "https://oai-meridian-sdc-a1b2c3.openai.azure.com"},
        lambda: "fake-entra-token",
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    settings = GatewaySettings(
        registry_dir=registry_dir,
        mode="live",
        environment="test",
        database_url=database.dsn("model_gateway"),
    )
    app = create_app(settings, providers={"azure-openai": adapter})
    return TestClient(app), sent


def test_a_call_through_the_real_adapter_asks_for_the_registrys_dimensions(
    registry_with: Callable[..., Path], fresh_database: DatabaseHandle
) -> None:
    vectors = [[0.5] * DIMENSIONS, [0.25] * DIMENSIONS]
    client, sent = through_the_adapter(
        adapter_answer(vectors), registry_with(EU), fresh_database
    )
    run_id = uuid.uuid4()

    response = client.post(
        PATH,
        json=BODY,
        headers={
            "X-Meridian-Tenant": "claims-triage",
            "X-Meridian-Agent": "claims-triage",
            "X-Meridian-Run": str(run_id),
        },
    )

    assert response.status_code == 200
    reply = response.json()
    assert reply["embeddings"] == vectors
    assert (reply["dimensions"], reply["usage"]) == (DIMENSIONS, {"input_tokens": 7})
    (request,) = sent
    assert request["input"] == list(TEXTS)
    assert request["dimensions"] == DIMENSIONS  # the registry's, never the caller's
    assert request["encoding_format"] == "float"
    (row,) = audit_events(fresh_database, run_id)
    assert (row["outcome"], row["input_tokens"], row["output_tokens"]) == (
        "completed",
        7,
        0,
    )
    assert row["provider_model"] == PROVIDER_MODEL


def test_a_vector_of_the_wrong_length_from_the_real_adapter_is_a_502_and_one_row(
    registry_with: Callable[..., Path], fresh_database: DatabaseHandle
) -> None:
    short = [[0.5] * (DIMENSIONS - 1), [0.25] * (DIMENSIONS - 1)]
    client, _ = through_the_adapter(
        adapter_answer(short), registry_with(EU), fresh_database
    )
    run_id = uuid.uuid4()

    response = client.post(
        PATH,
        json=BODY,
        headers={
            "X-Meridian-Tenant": "claims-triage",
            "X-Meridian-Agent": "claims-triage",
            "X-Meridian-Run": str(run_id),
        },
    )

    assert response.status_code == 502
    assert response.json() == {"detail": PROVIDER_FAILED}
    assert "embeddings" not in response.text
    (row,) = audit_events(fresh_database, run_id)
    assert (row["outcome"], row["reason"], row["deployment"]) == (
        "failed",
        "bad-response",
        EU,
    )
    (usage,) = owner_rows(
        fresh_database, "SELECT state FROM gateway.usage"
    )  # the answer may have been billed
    assert usage == ("kept",)


# ── the OpenAPI document ────────────────────────────────────────────────────
def test_the_embeddings_route_declares_the_429_and_the_413_names_both_limits(
    replay: Gateway,
) -> None:
    responses = replay.client.app.openapi()["paths"][PATH]["post"]["responses"]  # type: ignore[attr-defined]

    assert responses["429"]["description"] == "A limit of the tenant is reached."
    assert "body" in responses["413"]["description"]
    assert "token limit" in responses["413"]["description"]
    assert "audit log" in responses["503"]["description"]


# ── T-56: no input and no vector component in anything the gateway writes ───
SCENARIOS = [
    "completed",
    "refused-by-policy",
    "refused-by-a-limit",
    "failed",
    "crashed",
]


def provider_error_with_canary() -> Exception:
    error = ProviderError("unavailable", 502)
    error.__cause__ = RuntimeError(f"{CANARY} {INPUT_CANARY}")
    return error


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_no_row_span_label_or_log_holds_an_input_a_canary_or_a_vector_component(
    registry_with: Callable[..., Path],
    build: Callable[..., Gateway],
    caplog: pytest.LogCaptureFixture,
    scenario: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    limits = {"requests": 1} if scenario == "refused-by-a-limit" else {}
    gateway = build(registry_with(EU, **limits))
    if scenario == "failed":
        gateway.provider.outcomes[EU] = Outcome(provider_error_with_canary())
    if scenario == "crashed":
        gateway.provider.outcomes[EU] = Outcome(
            RuntimeError(f"{CANARY} {INPUT_CANARY} {COMPONENT}")
        )
    body = {"inputs": [f"{INPUT_CANARY} about a storm", "and a flood"]}
    if scenario == "refused-by-a-limit":
        assert gateway.post(body=body)[0].status_code == 200  # uses the one request

    response, run_id = gateway.post(
        agent="some-other-agent"
        if scenario == "refused-by-policy"
        else "claims-triage",
        body=body,
    )

    expected = {
        "completed": 200,
        "refused-by-policy": 403,
        "refused-by-a-limit": HTTP_TOO_MANY,
        "failed": 502,
        "crashed": 500,
    }[scenario]
    assert response.status_code == expected
    written = gateway.everything_it_wrote()
    logged = caplog.text + " ".join(r.getMessage() for r in caplog.records)
    for secret in (INPUT_CANARY, CANARY, COMPONENT_DIGITS):
        assert secret not in written
        assert secret not in logged
        assert_spans_hold_no_exception_and_no_canary(gateway.exporter, secret)
        if scenario != "completed":
            assert secret not in response.text
    assert INPUT_CANARY not in response.text
    assert len(audit_events(gateway.database, run_id)) >= 1
