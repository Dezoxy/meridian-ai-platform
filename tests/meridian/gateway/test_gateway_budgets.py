"""POST /v1/chat holds a tenant to its limits and records what a call costs (S011).

No network and no Azure: a scripted provider answers or raises per deployment.
The real ledger runs against the throwaway PostgreSQL, the limiter runs on a
fake clock and the metrics land in an in-memory reader. Limits are planted into
a copy of the registry, so the seeded ones stay as they are.
"""

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import httpx
import psycopg
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import (
    GLOBAL_DEPLOYMENT_YAML,
    REGISTRY_DIR,
    REPLAY_ENTRY,
    SECOND_DEPLOYMENT_YAML,
    FakeClock,
    assert_spans_hold_no_exception_and_no_canary,
    audit_events,
    owner_rows,
    pin_chat_route,
)

from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.throttle import (
    REFUSAL_AUDIT_SECONDS,
    RefusalAuditThrottle,
)
from meridian.platform.gateway import app as gateway_app
from meridian.platform.gateway import budget
from meridian.platform.gateway.app import (
    PROVIDER_FAILED,
    PROVIDER_TIMED_OUT,
    create_app,
)
from meridian.platform.gateway.budget import (
    MICRO,
    BudgetRefusalReason,
    chat_estimate,
    cost_micro_eur,
    estimate_input_tokens,
)
from meridian.platform.gateway.models import ChatRequest
from meridian.platform.gateway.providers.base import (
    ChatProvider,
    ProviderError,
    ProviderReply,
)
from meridian.platform.gateway.ratelimit import (
    RateRefusalReason,
    RateStoreRefusalReason,
)
from meridian.platform.gateway.refusals import (
    LIMIT_ANSWERS,
    TENANT_BUDGET_USED_UP,
    TENANT_RATE_LIMIT_REACHED,
    TENANT_REQUEST_TOO_LARGE,
)
from meridian.platform.gateway.resilience import FAILURE_THRESHOLD, OPEN_SECONDS
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.gateway.walk import closing_for
from meridian.platform.registry import Registry, load_registry
from meridian.platform.registry.models import Deployment

CLAIM_TEXT = "claimant-secret-text-42"
CANARY = "CANARY-provider-internals-9917"
ANSWER_TEXT = "a fake answer"
UNKNOWN_TENANT = "unknown-tenant-xyz"
UNKNOWN_AGENT = "unknown-agent-xyz"
PROVIDER_MODEL = "gpt-4o-2024-11-20"
INPUT_TOKENS, OUTPUT_TOKENS = 11, 7
SETTLED_TOKENS = INPUT_TOKENS + OUTPUT_TOKENS
BODY = {"messages": [{"role": "user", "content": CLAIM_TEXT}]}
REQUEST = ChatRequest.model_validate(BODY)
RESERVED_TOKENS = chat_estimate(REQUEST).tokens
FIRST = "aoai-sdc-gpt-4o"
SECOND = "aoai-sdc-gpt-4o-second"
OCTOBER_FIRST = date(2026, 10, 1)
# The largest reply the contract allows (1024) and the longest message: their
# reservation exceeds the 2000-token window the one test that uses this plants.
TOO_LARGE_BODY = {
    "messages": [{"role": "user", "content": "x" * 20_000}],
    "max_output_tokens": 1024,
}
RETRY_AFTER = "Retry-After"
HTTP_TOO_MANY = 429
HTTP_TOO_LARGE = 413
HTTP_UNAVAILABLE = 503
# The tenant's rows in tenants.yaml as the seed has them (claims-triage is the
# first), replaced by ``limits_block`` when a test plants its own.
SEED_LIMITS = (
    "      requests_per_10_seconds: 10\n"
    "      tokens_per_minute: 10000\n"
    "      tokens_per_day: 300000\n"
    "      cost_per_month_eur: 10\n"
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


# ── a provider that answers, fails or waits as scripted ─────────────────────
@dataclass(frozen=True, slots=True)
class Outcome:
    error: Exception | None = None
    takes: float = 0.0


OK = Outcome()


def with_status(kind: str, status: int) -> Outcome:
    return Outcome(ProviderError(kind, status))  # type: ignore[arg-type]


def without_status(kind: str, *, sent: bool = True) -> Outcome:
    return Outcome(ProviderError(kind, sent=sent))  # type: ignore[arg-type]


class ScriptedProvider:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.outcomes: dict[str, Outcome] = {FIRST: OK, SECOND: OK}
        self.calls: list[str] = []
        # Runs inside ``chat``, before it answers: a test looks at the world
        # from where the provider stands.
        self.before: Callable[[Deployment], None] | None = None

    @property
    def called(self) -> list[str]:
        return self.calls

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        self.calls.append(deployment.id)
        if self.before is not None:
            self.before(deployment)
        outcome = self.outcomes[deployment.id]
        self.clock.advance(outcome.takes)
        if outcome.error is not None:
            raise outcome.error
        return ProviderReply(
            text=ANSWER_TEXT,
            finish_reason="stop",
            model=PROVIDER_MODEL,
            input_tokens=INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
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
    provider: ScriptedProvider
    clock: FakeClock
    today: Today
    exporter: InMemorySpanExporter
    reader: InMemoryMetricReader
    database: DatabaseHandle
    registry: Registry
    run_ids: list[uuid.UUID] = field(default_factory=list)

    def post(
        self,
        tenant: str = "claims-triage",
        agent: str = "claims-triage",
        body: Mapping | None = None,
    ) -> tuple[httpx.Response, uuid.UUID]:
        run_id = uuid.uuid4()
        response = self.client.post(
            "/v1/chat",
            json=BODY if body is None else body,
            headers={
                "X-Meridian-Tenant": tenant,
                "X-Meridian-Agent": agent,
                "X-Meridian-Run": str(run_id),
            },
        )
        return response, run_id

    def deployment(self, deployment_id: str) -> Deployment:
        found = self.registry.deployment(deployment_id)
        assert found is not None
        return found

    def settled_micro(self, deployment_id: str = FIRST) -> int:
        return cost_micro_eur(
            self.deployment(deployment_id).price,
            self.registry.exchange,
            INPUT_TOKENS,
            OUTPUT_TOKENS,
        )

    def reserved_micro(self, deployment_id: str = FIRST) -> int:
        return cost_micro_eur(
            self.deployment(deployment_id).price,
            self.registry.exchange,
            estimate_input_tokens(REQUEST),
            REQUEST.max_output_tokens,
        )

    def usage(self) -> list[dict]:
        rows = owner_rows(
            self.database,
            "SELECT call_id, deployment, state, reserved_tokens, charged_tokens, "
            "charged_micro_eur, input_tokens, output_tokens, tenant, agent "
            "FROM gateway.usage ORDER BY reserved_at, deployment",
        )
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
        return [dict(zip(names, row, strict=True)) for row in rows]

    def counters(self) -> dict[tuple[str, date], int]:
        rows = owner_rows(
            self.database,
            "SELECT kind, period_start, amount FROM gateway.budget_counters",
        )
        return {(kind, period): amount for kind, period, amount in rows}

    def refused_audit_rows(self, reason: str) -> int:
        ((count,),) = owner_rows(
            self.database,
            "SELECT count(*) FROM audit.events "
            "WHERE outcome = 'refused' AND reason = %s",
            (reason,),
        )
        return count

    def suppressed_counts(self, reason: str) -> list[int | None]:
        """The ``suppressed`` of each refusal row of ``reason``, oldest first."""
        rows = owner_rows(
            self.database,
            "SELECT suppressed FROM audit.events "
            "WHERE outcome = 'refused' AND reason = %s ORDER BY recorded_at",
            (reason,),
        )
        return [suppressed for (suppressed,) in rows]

    def chat_span(self) -> ReadableSpan:
        (span,) = [
            s for s in self.exporter.get_finished_spans() if s.name == "gateway.chat"
        ]
        return span

    def points(self, name: str) -> list[tuple[dict, float]]:
        """The data points of one metric: attributes and value."""
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


# ── registries and gateways ─────────────────────────────────────────────────
@pytest.fixture
def planted(plant: Callable[..., Path]) -> Path:
    return plant(
        (
            "models.yaml",
            REPLAY_ENTRY,
            SECOND_DEPLOYMENT_YAML + GLOBAL_DEPLOYMENT_YAML + REPLAY_ENTRY,
        )
    )


@pytest.fixture
def registry_with(plant: Callable[..., Path], planted: Path) -> Callable[..., Path]:
    """A registry whose chat route lists ``candidates`` and, when limits are
    given, whose claims-triage tenant has them."""

    def make(*candidates: str, **limits: object) -> Path:
        if limits:
            plant(("tenants.yaml", SEED_LIMITS, limits_block(**limits)))
        return pin_chat_route(planted, *candidates)

    return make


@pytest.fixture
def build(fresh_database: DatabaseHandle) -> Callable[..., Gateway]:
    def make(
        registry_dir: Path,
        mode: str = "live",
        providers: Mapping[str, ChatProvider] | None = None,
    ) -> Gateway:
        clock, today = FakeClock(), Today()
        exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
        provider = ScriptedProvider(clock)
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
            providers=(
                (providers or {"azure-openai": provider}) if mode == "live" else None
            ),
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
def one(registry_with: Callable[..., Path], build: Callable[..., Gateway]) -> Gateway:
    """The seeded limits and a chat route of the first deployment alone."""
    return build(registry_with(FIRST))


# ── 1. a completed call ─────────────────────────────────────────────────────
def test_a_completed_call_is_settled_and_carries_one_call_id_everywhere(
    one: Gateway,
) -> None:
    response, run_id = one.post()

    assert response.status_code == 200
    call_id = uuid.UUID(response.json()["call_id"])
    (usage,) = one.usage()
    assert usage["call_id"] == call_id
    assert (usage["deployment"], usage["state"]) == (FIRST, "settled")
    assert (usage["input_tokens"], usage["output_tokens"]) == (
        INPUT_TOKENS,
        OUTPUT_TOKENS,
    )
    assert usage["charged_tokens"] == SETTLED_TOKENS
    assert usage["charged_micro_eur"] == one.settled_micro() > 0
    assert usage["reserved_tokens"] == RESERVED_TOKENS
    assert one.counters() == {
        ("tokens-day", OCTOBER_FIRST): SETTLED_TOKENS,
        ("cost-month", OCTOBER_FIRST): one.settled_micro(),
    }
    (row,) = audit_events(one.database, run_id)
    assert row["call_id"] == call_id
    assert row["provider_model"] == PROVIDER_MODEL
    assert row["http_status"] is None
    attributes = dict(one.chat_span().attributes)
    assert attributes["meridian.call_id"] == str(call_id)
    assert attributes["meridian.cost_micro_eur"] == one.settled_micro()


# ── 2. the reservation comes first ──────────────────────────────────────────
def test_the_reservation_exists_before_the_provider_is_called(one: Gateway) -> None:
    seen: list[list[dict]] = []
    one.provider.before = lambda _deployment: seen.append(one.usage())

    one.post()

    ((during,),) = seen
    assert (during["state"], during["reserved_tokens"]) == ("reserved", RESERVED_TOKENS)
    assert during["charged_tokens"] == RESERVED_TOKENS  # charged until it is closed
    assert one.usage()[0]["state"] == "settled"


# ── 3. the request rate ─────────────────────────────────────────────────────
def test_the_request_over_the_rate_limit_is_a_429_with_retry_after(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, requests=3))
    for _ in range(3):
        assert gateway.post()[0].status_code == 200
    gateway.provider.calls.clear()

    response, run_id = gateway.post()

    assert response.status_code == HTTP_TOO_MANY
    assert response.json() == {"detail": TENANT_RATE_LIMIT_REACHED}
    assert response.headers[RETRY_AFTER] == "10"
    assert gateway.provider.called == []
    assert len(gateway.usage()) == 3
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"]) == ("refused", "tenant-request-rate")
    assert row["call_id"] is not None
    gateway.clock.advance(10)
    assert gateway.post()[0].status_code == 200
    assert len(gateway.usage()) == 4


def test_a_request_the_rate_limit_refuses_touches_no_circuit_and_no_ledger(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, requests=1))
    gateway.provider.outcomes[FIRST] = with_status("unavailable", 503)
    gateway.post()  # one counted failure
    before = gateway.counters()

    for _ in range(FAILURE_THRESHOLD + 2):
        assert gateway.post()[0].status_code == HTTP_TOO_MANY

    assert gateway.provider.called == [FIRST]  # refused before the circuit
    assert gateway.counters() == before
    assert len(gateway.usage()) == 1


# ── 4. the token rate ───────────────────────────────────────────────────────
def test_the_token_rate_limit_is_a_429_with_retry_after(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, tokens_per_minute=2 * RESERVED_TOKENS + 100))
    for _ in range(2):
        assert gateway.post()[0].status_code == 200

    response, run_id = gateway.post()

    assert response.status_code == HTTP_TOO_MANY
    assert response.json() == {"detail": TENANT_RATE_LIMIT_REACHED}
    assert response.headers[RETRY_AFTER] == "60"
    (row,) = audit_events(gateway.database, run_id)
    assert row["reason"] == "tenant-token-rate"


def test_a_request_whose_reservation_alone_exceeds_the_limit_is_a_413(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, tokens_per_minute=RESERVED_TOKENS - 1))

    response, run_id = gateway.post()

    assert response.status_code == HTTP_TOO_LARGE
    assert response.json() == {"detail": TENANT_REQUEST_TOO_LARGE}
    assert RETRY_AFTER not in response.headers
    assert gateway.provider.called == []
    assert gateway.usage() == []
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"]) == ("refused", "tenant-request-too-large")


def test_a_request_whose_reservation_equals_the_limit_is_admitted(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, tokens_per_minute=RESERVED_TOKENS))

    assert gateway.post()[0].status_code == 200


# ── 5. the daily token budget ───────────────────────────────────────────────
def test_the_daily_budget_refuses_the_call_that_does_not_fit_and_opens_on_the_next_day(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    # Room for one reservation, then only what the first call's real count left.
    day_limit = RESERVED_TOKENS + SETTLED_TOKENS - 1
    gateway = build(registry_with(FIRST, tokens_per_day=day_limit))
    assert gateway.post()[0].status_code == 200
    assert gateway.counters()[("tokens-day", OCTOBER_FIRST)] == SETTLED_TOKENS
    gateway.provider.calls.clear()

    response, run_id = gateway.post()

    assert response.status_code == HTTP_TOO_MANY
    assert response.json() == {"detail": TENANT_BUDGET_USED_UP}
    assert RETRY_AFTER not in response.headers
    assert gateway.provider.called == []
    assert len(gateway.usage()) == 1  # the refusal made no row
    assert gateway.counters()[("tokens-day", OCTOBER_FIRST)] == SETTLED_TOKENS
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"]) == ("refused", "tenant-token-budget")
    # The route facts of the candidate whose reservation was refused.
    assert (row["deployment"], row["provider"], row["model"]) == (
        FIRST,
        "azure-openai",
        "gpt-4o",
    )
    assert row["call_id"] is not None
    # What the settlement left is room for a smaller call.
    small = BODY | {"max_output_tokens": 10}
    assert gateway.post(body=small)[0].status_code == 200
    # The next UTC day starts a new counter.
    gateway.today.day = date(2026, 10, 2)
    assert gateway.post()[0].status_code == 200
    assert gateway.counters()[("tokens-day", date(2026, 10, 2))] == SETTLED_TOKENS


def test_a_released_reservation_gives_its_room_back(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, tokens_per_day=RESERVED_TOKENS))
    gateway.provider.outcomes[FIRST] = with_status("rejected", 400)
    assert gateway.post()[0].status_code == 502
    gateway.provider.outcomes[FIRST] = OK

    response, _ = gateway.post()

    assert response.status_code == 200


# ── 6. the monthly cost quota ───────────────────────────────────────────────
def test_the_monthly_quota_refuses_the_call_that_does_not_fit_and_opens_next_month(
    registry_with: Callable[..., Path],
    build: Callable[..., Gateway],
) -> None:
    probe = build(registry_with(FIRST))
    quota_micro = probe.reserved_micro() + probe.settled_micro() - 1
    gateway = build(
        registry_with(FIRST, cost_per_month_eur=Decimal(quota_micro) / MICRO)
    )
    assert gateway.post()[0].status_code == 200
    gateway.provider.calls.clear()

    response, run_id = gateway.post()

    assert response.status_code == HTTP_TOO_MANY
    assert response.json() == {"detail": TENANT_BUDGET_USED_UP}
    assert RETRY_AFTER not in response.headers  # a budget has no short wait
    assert gateway.provider.called == []
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"]) == ("refused", "tenant-cost-budget")
    assert row["deployment"] == FIRST
    # The refused call took nothing from the day either (one transaction).
    assert gateway.counters() == {
        ("tokens-day", OCTOBER_FIRST): SETTLED_TOKENS,
        ("cost-month", OCTOBER_FIRST): gateway.settled_micro(),
    }
    gateway.today.day = date(2026, 10, 2)  # a new day, the same month
    assert gateway.post()[0].status_code == HTTP_TOO_MANY
    # The refusal rolled back the new day's counter along with its charge.
    assert gateway.counters().get(("tokens-day", date(2026, 10, 2)), 0) == 0
    gateway.today.day = date(2026, 11, 1)
    assert gateway.post()[0].status_code == 200


# ── 7. QA-12: concurrent requests cannot both pass the same check ───────────
def test_concurrent_requests_with_room_for_one_reservation_make_one_call(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    racers = 6
    gateway = build(
        registry_with(FIRST, tokens_per_day=RESERVED_TOKENS + SETTLED_TOKENS - 1)
    )
    release = threading.Event()
    peak: list[int] = []

    def hold(_deployment: Deployment) -> None:
        # Still reserved while the others are answered or refused.
        peak.append(gateway.counters()[("tokens-day", OCTOBER_FIRST)])
        assert release.wait(timeout=30)

    gateway.provider.before = hold
    with ThreadPoolExecutor(max_workers=racers) as pool:
        futures = [pool.submit(gateway.post) for _ in range(racers)]
        deadline = time.monotonic() + 30
        while sum(f.done() for f in futures) < racers - 1:
            assert time.monotonic() < deadline, "the refusals never came back"
            time.sleep(0.01)
        release.set()
        statuses = sorted(f.result()[0].status_code for f in futures)

    assert statuses == [200] + [HTTP_TOO_MANY] * (racers - 1)
    assert gateway.provider.called == [FIRST]
    assert peak == [RESERVED_TOKENS]
    assert gateway.counters()[("tokens-day", OCTOBER_FIRST)] == SETTLED_TOKENS
    assert [u["state"] for u in gateway.usage()] == ["settled"]


# ── how a failed attempt's reservation is closed (the table of closing_for) ──
# kind, status, sent, closes as
CLOSING_TABLE = [
    ("rejected", 400, True, "release"),
    ("rejected", 422, True, "release"),
    ("rejected", None, True, "keep"),  # a completion the content filter withheld
    ("auth", 401, True, "release"),
    ("auth", 403, True, "release"),
    ("auth", None, False, "release"),
    ("unavailable", 404, True, "release"),
    ("rate-limited", 429, True, "release"),
    ("timeout", 408, True, "keep"),
    ("timeout", None, True, "keep"),
    ("timeout", None, False, "release"),  # the connection never opened
    ("unavailable", 500, True, "keep"),
    ("unavailable", 503, True, "keep"),
    ("unavailable", None, True, "keep"),
    ("unavailable", None, False, "release"),
    ("bad-response", None, True, "keep"),
    ("bad-response", 200, True, "keep"),
]


@pytest.mark.parametrize(("kind", "status", "sent", "closes"), CLOSING_TABLE)
def test_closing_for_follows_the_table(
    kind: str, status: int | None, sent: bool, closes: str
) -> None:
    failure = ProviderError(kind, status, sent=sent)  # type: ignore[arg-type]

    assert closing_for(failure) == closes


def test_an_exception_that_is_not_a_provider_error_is_kept() -> None:
    assert closing_for(RuntimeError(CANARY)) == "keep"
    assert closing_for(ValueError()) == "keep"


# ── 8. fallback: a released first attempt, a settled second ─────────────────
def test_a_429_from_the_first_candidate_is_released_and_the_second_settles(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, SECOND))
    gateway.provider.outcomes[FIRST] = with_status("rate-limited", 429)

    response, run_id = gateway.post()

    assert response.status_code == 200
    call_id = uuid.UUID(response.json()["call_id"])
    first, second = gateway.usage()
    assert (first["deployment"], first["state"]) == (FIRST, "released")
    assert (first["charged_tokens"], first["charged_micro_eur"]) == (0, 0)
    assert (second["deployment"], second["state"]) == (SECOND, "settled")
    assert first["call_id"] == second["call_id"] == call_id
    assert gateway.counters() == {
        ("tokens-day", OCTOBER_FIRST): SETTLED_TOKENS,
        ("cost-month", OCTOBER_FIRST): gateway.settled_micro(SECOND),
    }
    failed, completed = audit_events(gateway.database, run_id)
    assert failed["call_id"] == completed["call_id"] == call_id
    assert (failed["outcome"], failed["http_status"]) == ("failed", 429)
    assert (completed["outcome"], completed["http_status"]) == ("completed", None)


def test_a_500_from_the_first_candidate_stays_charged_and_the_second_settles(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, SECOND))
    gateway.provider.outcomes[FIRST] = with_status("unavailable", 500)

    response, _ = gateway.post()

    assert response.status_code == 200
    first, second = gateway.usage()
    assert (first["deployment"], first["state"]) == (FIRST, "kept")
    assert first["charged_tokens"] == RESERVED_TOKENS
    assert (second["deployment"], second["state"]) == (SECOND, "settled")
    assert gateway.counters() == {
        ("tokens-day", OCTOBER_FIRST): RESERVED_TOKENS + SETTLED_TOKENS,
        ("cost-month", OCTOBER_FIRST): (
            gateway.reserved_micro(FIRST) + gateway.settled_micro(SECOND)
        ),
    }


# ── 9. the call may have been billed: the reservation is the charge ─────────
@pytest.mark.parametrize(
    ("kind", "status"),
    [(k, s) for k, s, sent, closes in CLOSING_TABLE if closes == "keep" and sent],
)
def test_a_failure_that_may_have_been_billed_keeps_the_reservation(
    registry_with: Callable[..., Path],
    build: Callable[..., Gateway],
    kind: str,
    status: int | None,
) -> None:
    gateway = build(registry_with(FIRST))
    gateway.provider.outcomes[FIRST] = Outcome(ProviderError(kind, status))  # type: ignore[arg-type]

    response, run_id = gateway.post()

    assert response.status_code in {502, 504}
    (usage,) = gateway.usage()
    assert usage["state"] == "kept"
    assert usage["charged_tokens"] == RESERVED_TOKENS
    assert usage["charged_micro_eur"] == gateway.reserved_micro()
    assert gateway.counters() == {
        ("tokens-day", OCTOBER_FIRST): RESERVED_TOKENS,
        ("cost-month", OCTOBER_FIRST): gateway.reserved_micro(),
    }
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"], row["http_status"]) == (
        "failed",
        kind,
        status,
    )
    assert row["call_id"] == usage["call_id"]


# ── 10. nothing was billed: the reservation is given back ───────────────────
@pytest.mark.parametrize(
    ("kind", "status", "sent"),
    [(k, s, sent) for k, s, sent, closes in CLOSING_TABLE if closes == "release"],
)
def test_a_failure_that_cannot_have_been_billed_releases_the_reservation(
    registry_with: Callable[..., Path],
    build: Callable[..., Gateway],
    kind: str,
    status: int | None,
    sent: bool,
) -> None:
    gateway = build(registry_with(FIRST))
    gateway.provider.outcomes[FIRST] = Outcome(
        ProviderError(kind, status, sent=sent)  # type: ignore[arg-type]
    )

    response, run_id = gateway.post()

    assert response.status_code in {502, 504}
    (usage,) = gateway.usage()
    assert usage["state"] == "released"
    assert gateway.counters() == {
        ("tokens-day", OCTOBER_FIRST): 0,
        ("cost-month", OCTOBER_FIRST): 0,
    }
    (row,) = audit_events(gateway.database, run_id)
    assert row["http_status"] == status


# ── 11. an unexpected exception ─────────────────────────────────────────────
def test_an_unexpected_exception_from_the_provider_keeps_the_reservation(
    one: Gateway,
) -> None:
    one.provider.outcomes[FIRST] = Outcome(RuntimeError(CANARY))

    response, run_id = one.post()

    assert response.status_code == 500
    assert CANARY not in response.text
    (usage,) = one.usage()
    assert usage["state"] == "kept"
    assert one.counters()[("tokens-day", OCTOBER_FIRST)] == RESERVED_TOKENS
    (row,) = audit_events(one.database, run_id)
    assert (row["outcome"], row["reason"]) == ("failed", "internal")
    # An unexpected exception is a failure with no reason: no kind to name.
    assert one.calls_counted() == {("failed", None): 1}


# ── 12. a budget refusal after an earlier attempt ───────────────────────────
def test_a_budget_refusal_at_the_second_candidate_is_a_skipped_row_and_the_first_status(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    # The first attempt times out with no status, so its reservation is kept and
    # leaves no room for the second candidate's.
    gateway = build(
        registry_with(
            FIRST, SECOND, tokens_per_day=RESERVED_TOKENS + RESERVED_TOKENS // 2
        )
    )
    gateway.provider.outcomes[FIRST] = without_status("timeout")

    response, run_id = gateway.post()

    assert response.status_code == 504
    assert response.json() == {"detail": PROVIDER_TIMED_OUT}
    assert gateway.provider.called == [FIRST]
    failed, skipped = audit_events(gateway.database, run_id)
    assert (failed["outcome"], failed["deployment"], failed["reason"]) == (
        "failed",
        FIRST,
        "timeout",
    )
    assert (skipped["outcome"], skipped["deployment"], skipped["reason"]) == (
        "skipped",
        SECOND,
        "tenant-token-budget",
    )
    assert failed["call_id"] == skipped["call_id"] is not None
    assert [u["deployment"] for u in gateway.usage()] == [FIRST]
    parent = dict(gateway.chat_span().attributes)
    assert (parent["meridian.attempts"], parent["meridian.skipped"]) == (1, 1)
    assert "meridian.refusal" not in parent  # it is the first attempt's 504


def test_a_budget_refusal_at_the_second_candidate_answers_502_for_a_502(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(
        registry_with(
            FIRST, SECOND, tokens_per_day=RESERVED_TOKENS + RESERVED_TOKENS // 2
        )
    )
    gateway.provider.outcomes[FIRST] = without_status("unavailable")

    response, _ = gateway.post()

    assert response.status_code == 502
    assert response.json() == {"detail": PROVIDER_FAILED}


# ── 13. a skipped candidate reserves nothing ────────────────────────────────
def test_a_candidate_with_an_open_circuit_makes_no_usage_row(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, SECOND))
    gateway.provider.outcomes[FIRST] = with_status("unavailable", 503)
    for _ in range(FAILURE_THRESHOLD):
        gateway.post()
    seen = len(gateway.usage())

    response, run_id = gateway.post()

    assert response.status_code == 200
    new = gateway.usage()[seen:]
    assert [u["deployment"] for u in new] == [SECOND]
    assert audit_events(gateway.database, run_id)[0]["outcome"] == "skipped"


def test_a_candidate_the_deadline_leaves_no_time_for_makes_no_usage_row(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, SECOND))
    gateway.provider.outcomes[FIRST] = Outcome(ProviderError("timeout", 500), 24.0)

    response, run_id = gateway.post()

    assert response.status_code == 504
    assert [u["deployment"] for u in gateway.usage()] == [FIRST]
    assert [r["outcome"] for r in audit_events(gateway.database, run_id)] == [
        "failed",
        "skipped",
    ]


def test_a_skipped_first_candidate_then_a_budget_refusal_is_a_429_and_one_skipped_row(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    # The first candidate fails until its circuit opens, with a 429 so each
    # attempt is released and takes nothing from the budget; the second's three
    # answers leave no room for a fourth reservation.
    gateway = build(
        registry_with(
            FIRST, SECOND, tokens_per_day=RESERVED_TOKENS + 3 * SETTLED_TOKENS - 1
        )
    )
    gateway.provider.outcomes[FIRST] = with_status("rate-limited", 429)
    for _ in range(FAILURE_THRESHOLD):
        assert gateway.post()[0].status_code == 200
    gateway.provider.calls.clear()

    response, run_id = gateway.post()  # FIRST skipped (open), SECOND refused

    assert response.status_code == HTTP_TOO_MANY
    assert response.json() == {"detail": TENANT_BUDGET_USED_UP}
    assert RETRY_AFTER not in response.headers
    assert gateway.provider.called == []
    skipped = [r for r in audit_events(gateway.database, run_id)]
    assert [(r["outcome"], r["deployment"], r["reason"]) for r in skipped] == [
        ("skipped", FIRST, "circuit-open"),
        ("refused", SECOND, "tenant-token-budget"),
    ]


def test_a_database_error_at_the_second_candidates_reserve_is_a_503_and_no_second_call(
    registry_with: Callable[..., Path],
    build: Callable[..., Gateway],
    ledger_switch: "LedgerSwitch",
) -> None:
    gateway = build(registry_with(FIRST, SECOND))
    gateway.provider.outcomes[FIRST] = with_status("unavailable", 503)
    # connection 1 reserves the first, 2 closes it, 3 would reserve the second
    ledger_switch.fail_from = 3

    response, _ = gateway.post()

    assert response.status_code == HTTP_UNAVAILABLE
    assert "hunter2" not in response.text
    assert gateway.provider.called == [FIRST]
    (usage,) = gateway.usage()
    assert (usage["deployment"], usage["state"]) == (FIRST, "kept")
    (raised,) = ledger_switch.raised
    assert raised.__context__ is None  # the first attempt's exception is gone


class Boom(BaseException):
    """What is not an ``Exception``: it must leave nothing half done."""


def test_a_base_exception_from_the_provider_leaves_the_row_reserved_and_the_probe_free(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, SECOND))
    gateway.provider.outcomes[FIRST] = with_status("unavailable", 503)
    for _ in range(FAILURE_THRESHOLD):
        gateway.post()
    gateway.clock.advance(OPEN_SECONDS)  # the first is due for a probe
    gateway.provider.outcomes[FIRST] = Outcome(Boom())
    seen = len(gateway.usage())

    with pytest.raises(Boom):
        gateway.post()  # the probe raises a BaseException

    probe = gateway.usage()[seen]
    assert (probe["deployment"], probe["state"]) == (FIRST, "reserved")
    gateway.provider.outcomes[FIRST] = OK
    gateway.provider.calls.clear()
    response, _ = gateway.post()
    assert response.json()["deployment"] == FIRST  # the probe was free again
    assert gateway.provider.called == [FIRST]


def test_a_budget_refusal_while_a_probe_is_held_frees_the_probe(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    # The first candidate's 429s are released, so they use no budget; the second
    # candidate's three answers use 3 x 18 tokens, which leaves no room for a fourth
    # reservation of the first candidate (the probe) on that day.
    gateway = build(
        registry_with(
            FIRST, SECOND, tokens_per_day=RESERVED_TOKENS + 3 * SETTLED_TOKENS - 1
        )
    )
    gateway.provider.outcomes[FIRST] = with_status("rate-limited", 429)
    for _ in range(FAILURE_THRESHOLD):
        assert gateway.post()[0].status_code == 200
    gateway.clock.advance(OPEN_SECONDS)  # the first is due for a probe
    gateway.provider.calls.clear()

    refused, _ = gateway.post()  # the probe is taken, its reservation refused

    assert refused.status_code == HTTP_TOO_MANY
    assert gateway.provider.called == []
    gateway.today.day = date(2026, 10, 2)  # a new day: room again
    gateway.provider.outcomes[FIRST] = OK
    response, _ = gateway.post()
    assert response.json()["deployment"] == FIRST  # probed, not left held


def test_a_reserve_that_leaves_no_time_is_released_and_the_candidate_skipped(
    one: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_reserve = budget.Ledger.reserve

    def slow(self: budget.Ledger, *args: Any, **kwargs: Any) -> Any:
        reservation = real_reserve(self, *args, **kwargs)
        one.clock.advance(20.0)  # leaves 5 s of the 25
        return reservation

    monkeypatch.setattr(budget.Ledger, "reserve", slow)

    response, run_id = one.post()

    assert response.status_code == HTTP_UNAVAILABLE
    assert one.provider.called == []
    (usage,) = one.usage()
    assert (usage["state"], usage["charged_tokens"]) == ("released", 0)
    assert one.counters() == {
        ("tokens-day", OCTOBER_FIRST): 0,
        ("cost-month", OCTOBER_FIRST): 0,
    }
    (row,) = audit_events(one.database, run_id)
    assert (row["outcome"], row["deployment"], row["reason"]) == (
        "skipped",
        FIRST,
        "deadline",
    )
    parent = dict(one.chat_span().attributes)
    assert (parent["meridian.attempts"], parent["meridian.skipped"]) == (0, 1)
    assert one.calls_counted() == {("failed", "unavailable"): 1}


def test_a_reserve_that_leaves_exactly_the_minimum_still_calls_the_provider(
    one: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_reserve = budget.Ledger.reserve

    def slow(self: budget.Ledger, *args: Any, **kwargs: Any) -> Any:
        reservation = real_reserve(self, *args, **kwargs)
        one.clock.advance(15.0)  # leaves exactly 10 s
        return reservation

    monkeypatch.setattr(budget.Ledger, "reserve", slow)

    response, _ = one.post()

    assert response.status_code == 200
    assert one.provider.called == [FIRST]


class NoLookup(dict):
    """Providers whose lookup raises: the walk must not have reserved yet."""

    def __getitem__(self, key: str) -> ChatProvider:
        raise RuntimeError(CANARY)


def test_the_provider_is_looked_up_before_anything_is_reserved(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST), providers=NoLookup({"azure-openai": None}))

    response, run_id = gateway.post()

    assert response.status_code == 500
    assert gateway.usage() == []
    assert gateway.counters() == {}
    assert audit_events(gateway.database, run_id) == []


# ── 14. the ledger is down ──────────────────────────────────────────────────
class LedgerSwitch:
    """Stands in for the ledger's connection: the n-th connection and the ones
    after it raise an operational error that quotes a secret."""

    def __init__(self) -> None:
        self.real = budget.connect
        self.fail_from: int | None = None
        self.connections = 0
        self.raised: list[psycopg.OperationalError] = []

    def __call__(self, dsn: str, application_name: str):
        self.connections += 1
        if self.fail_from is not None and self.connections >= self.fail_from:
            error = psycopg.OperationalError("connection refused: password=hunter2")
            self.raised.append(error)
            raise error
        return self.real(dsn, application_name)


@pytest.fixture
def ledger_switch(monkeypatch: pytest.MonkeyPatch) -> LedgerSwitch:
    switch = LedgerSwitch()
    monkeypatch.setattr(budget, "connect", switch)
    return switch


def test_with_the_ledger_down_at_reserve_no_provider_is_called(
    one: Gateway, ledger_switch: LedgerSwitch
) -> None:
    ledger_switch.fail_from = 1

    response, _ = one.post()

    assert response.status_code == HTTP_UNAVAILABLE
    assert "hunter2" not in response.text
    assert one.provider.called == []
    assert one.usage() == []


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (OK, HTTP_UNAVAILABLE),
        (with_status("unavailable", 503), HTTP_UNAVAILABLE),
        (without_status("timeout"), HTTP_UNAVAILABLE),
        (Outcome(RuntimeError(CANARY)), HTTP_UNAVAILABLE),
    ],
    ids=["settle", "release", "keep", "crash"],
)
def test_with_the_ledger_down_after_the_call_the_row_stays_reserved_and_charged(
    one: Gateway, ledger_switch: LedgerSwitch, outcome: Outcome, status: int
) -> None:
    ledger_switch.fail_from = 2  # the reservation is made, its closing is not
    one.provider.outcomes[FIRST] = outcome

    response, _ = one.post()

    assert response.status_code == status
    assert "hunter2" not in response.text
    assert ANSWER_TEXT not in response.text
    assert CANARY not in response.text
    (usage,) = one.usage()
    assert usage["state"] == "reserved"
    assert one.counters()[("tokens-day", OCTOBER_FIRST)] == RESERVED_TOKENS
    (raised,) = ledger_switch.raised
    # The provider's exception, which can hold prompt text, is not its context.
    assert raised.__context__ is None
    assert raised.__cause__ is None


# ── 15. refusals are audited once per tenant and reason per minute ──────────
def test_twenty_refused_requests_leave_one_row_and_one_more_a_minute_later(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, requests=1))
    assert gateway.post()[0].status_code == 200

    for _ in range(20):
        assert gateway.post()[0].status_code == HTTP_TOO_MANY

    assert gateway.suppressed_counts("tenant-request-rate") == [0]
    assert gateway.calls_counted()[("refused", "tenant-request-rate")] == 20
    gateway.clock.advance(REFUSAL_AUDIT_SECONDS)
    assert gateway.post()[0].status_code == 200  # the window moved on
    assert gateway.post()[0].status_code == HTTP_TOO_MANY
    assert gateway.suppressed_counts("tenant-request-rate") == [0, 19]
    assert gateway.calls_counted()[("refused", "tenant-request-rate")] == 21


def test_a_refusal_whose_row_could_not_be_written_is_carried_by_the_next_row(
    registry_with: Callable[..., Path],
    build: Callable[..., Gateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.platform.common import audit

    gateway = build(registry_with(FIRST, requests=1))
    gateway.post()
    real_connect = audit.connect

    def down(*_a: object, **_k: object) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(audit, "connect", down)
    failed, _ = gateway.post()  # the first refusal: its row cannot be written
    monkeypatch.setattr(audit, "connect", real_connect)
    after, run_id = gateway.post()  # the very next refusal

    assert failed.status_code == HTTP_UNAVAILABLE
    assert after.status_code == HTTP_TOO_MANY
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"]) == ("refused", "tenant-request-rate")
    assert row["suppressed"] == 1
    assert gateway.suppressed_counts("tenant-request-rate") == [1]
    gateway.post()  # and now the window is open: this one is suppressed
    assert gateway.suppressed_counts("tenant-request-rate") == [1]


def test_two_reasons_leave_a_row_each(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, requests=1, tokens_per_minute=2000))
    assert gateway.post()[0].status_code == 200

    for _ in range(3):
        assert gateway.post()[0].status_code == HTTP_TOO_MANY
        assert gateway.post(body=TOO_LARGE_BODY)[0].status_code == HTTP_TOO_LARGE

    assert gateway.refused_audit_rows("tenant-request-rate") == 1
    assert gateway.refused_audit_rows("tenant-request-too-large") == 1
    counted = gateway.calls_counted()
    assert counted[("refused", "tenant-request-rate")] == 3
    assert counted[("refused", "tenant-request-too-large")] == 3


@pytest.fixture
def throttles(monkeypatch: pytest.MonkeyPatch) -> list[RefusalAuditThrottle]:
    """The throttles the app builds, kept for a test to look at."""
    built: list[RefusalAuditThrottle] = []

    def recording(*args: Any, **kwargs: Any) -> RefusalAuditThrottle:
        built.append(RefusalAuditThrottle(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr(gateway_app, "RefusalAuditThrottle", recording)
    return built


def test_twenty_unknown_tenant_names_leave_one_row_and_no_key_and_no_label(
    throttles: list[RefusalAuditThrottle], one: Gateway
) -> None:
    names = [f"made-up-tenant-{n}" for n in range(20)]

    for name in names:
        assert one.post(tenant=name)[0].status_code == 403

    assert one.refused_audit_rows("unknown-tenant") == 1
    assert one.suppressed_counts("unknown-tenant") == [0]
    assert one.calls_counted() == {("refused", "unknown-tenant"): 20}
    # The app builds two throttles; the caller check's holds no key here.
    (throttle,) = [t for t in throttles if t._windows]
    assert set(throttle._windows) == {(None, "unknown-tenant")}
    everything = str([one.points("meridian.gateway.calls")])
    assert not any(name in everything for name in names)


def test_the_unknown_tenant_row_a_minute_later_counts_the_ones_between(
    one: Gateway,
) -> None:
    for n in range(5):
        one.post(tenant=f"made-up-tenant-{n}")
    one.clock.advance(REFUSAL_AUDIT_SECONDS)

    one.post(tenant="made-up-tenant-last")

    assert one.suppressed_counts("unknown-tenant") == [0, 4]


def test_a_policy_refusal_is_audited_once_per_tenant_and_reason_and_never_per_agent(
    one: Gateway,
) -> None:
    assert one.post(agent="agent-one")[0].status_code == 403
    assert one.post(agent="agent-two")[0].status_code == 403
    assert one.refused_audit_rows("agent-not-allowed") == 1

    assert one.post(tenant="development", agent="agent-one")[0].status_code == 403
    assert one.post(tenant="development", agent="agent-two")[0].status_code == 403

    assert one.refused_audit_rows("agent-not-allowed") == 2
    assert one.suppressed_counts("agent-not-allowed") == [0, 0]
    assert one.calls_counted()[("refused", "agent-not-allowed")] == 4


def test_a_policy_refusal_row_keeps_its_content_and_adds_the_count(
    one: Gateway,
) -> None:
    response, run_id = one.post(agent="agent-one")

    assert response.status_code == 403
    (row,) = audit_events(one.database, run_id)
    assert (row["outcome"], row["reason"]) == ("refused", "agent-not-allowed")
    assert (row["tenant"], row["agent"]) == ("claims-triage", "agent-one")
    assert row["data_class"] == "personal"
    assert row["call_id"] is not None
    assert row["suppressed"] == 0


def test_the_answers_of_the_limits_are_exactly_the_reasons_there_are() -> None:
    reasons = (
        set(get_args(RateRefusalReason))
        | set(get_args(BudgetRefusalReason))
        | set(get_args(RateStoreRefusalReason))
    )

    assert set(LIMIT_ANSWERS) == reasons
    assert len(reasons) == 6


def test_a_refusal_whose_audit_write_fails_is_a_503_and_still_counted(
    registry_with: Callable[..., Path],
    build: Callable[..., Gateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.platform.common import audit

    gateway = build(registry_with(FIRST, requests=1))
    gateway.post()

    def down(*_a: object, **_k: object) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(audit, "connect", down)
    response, _ = gateway.post()

    assert response.status_code == HTTP_UNAVAILABLE
    counted = gateway.calls_counted()
    assert counted[("refused", "tenant-request-rate")] == 1
    assert ("failed", None) not in counted
    assert not any(outcome == "failed" for outcome, _ in counted)


# ── 16. metrics ─────────────────────────────────────────────────────────────
def test_a_completed_call_adds_tokens_cost_and_one_call_to_the_metrics(
    one: Gateway,
) -> None:
    one.post()

    labels = {
        "meridian.tenant": "claims-triage",
        "meridian.agent": "claims-triage",
        "meridian.provider": "azure-openai",
        "gen_ai.request.model": "gpt-4o",
    }
    assert sorted(one.points("meridian.gateway.tokens"), key=str) == sorted(
        [
            (labels | {"gen_ai.token.type": "input"}, INPUT_TOKENS),
            (labels | {"gen_ai.token.type": "output"}, OUTPUT_TOKENS),
        ],
        key=str,
    )
    ((cost_labels, cost),) = one.points("meridian.gateway.cost")
    assert cost_labels == labels
    assert cost == pytest.approx(one.settled_micro() / MICRO)
    assert one.points("meridian.gateway.calls") == [
        (
            {
                "meridian.outcome": "completed",
                "meridian.tenant": "claims-triage",
                "meridian.agent": "claims-triage",
            },
            1,
        )
    ]


def test_a_kept_attempt_adds_no_tokens_and_no_cost(one: Gateway) -> None:
    one.provider.outcomes[FIRST] = without_status("timeout")

    one.post()

    assert one.points("meridian.gateway.tokens") == []
    assert one.points("meridian.gateway.cost") == []
    assert one.calls_counted() == {("failed", "timeout"): 1}


def test_every_request_is_counted_once_by_its_outcome(
    registry_with: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(registry_with(FIRST, requests=2))
    gateway.post()
    gateway.provider.outcomes[FIRST] = without_status("unavailable")
    gateway.post()  # failed
    gateway.post()  # refused: the rate
    gateway.post(tenant=UNKNOWN_TENANT)  # refused: policy

    assert gateway.calls_counted() == {
        ("completed", None): 1,
        ("failed", "unavailable"): 1,
        ("refused", "tenant-request-rate"): 1,
        ("refused", "unknown-tenant"): 1,
    }


def test_a_header_value_never_becomes_a_label(one: Gateway) -> None:
    one.post(tenant=UNKNOWN_TENANT)
    one.post(agent=UNKNOWN_AGENT)
    one.post(tenant=UNKNOWN_TENANT, agent=UNKNOWN_AGENT)
    one.post()

    assert one.calls_counted()[("refused", "unknown-tenant")] == 2
    assert one.calls_counted()[("refused", "agent-not-allowed")] == 1
    everything = str(
        [
            (name, one.points(name))
            for name in (
                "meridian.gateway.tokens",
                "meridian.gateway.cost",
                "meridian.gateway.calls",
            )
        ]
    )
    assert UNKNOWN_TENANT not in everything
    assert UNKNOWN_AGENT not in everything
    # A refusal of policy carries the outcome and the reason, no tenant, no agent.
    for attributes, _ in one.points("meridian.gateway.calls"):
        if attributes["meridian.outcome"] == "refused":
            assert set(attributes) == {"meridian.outcome", "meridian.reason"}


# ── 17. replay mode ─────────────────────────────────────────────────────────
def test_replay_mode_is_limited_and_recorded_like_live_with_a_cost_of_zero(
    plant: Callable[..., Path], build: Callable[..., Gateway]
) -> None:
    gateway = build(
        plant(("tenants.yaml", SEED_LIMITS, limits_block(requests=2))), mode="replay"
    )
    first, _ = gateway.post()
    second, _ = gateway.post()

    third, run_id = gateway.post()

    assert (first.status_code, second.status_code) == (200, 200)
    assert third.status_code == HTTP_TOO_MANY
    first_usage, _ = gateway.usage()
    assert first_usage["call_id"] == uuid.UUID(first.json()["call_id"])
    assert (first_usage["deployment"], first_usage["state"]) == (
        "replay-chat",
        "settled",
    )
    assert first_usage["charged_micro_eur"] == 0
    assert first_usage["charged_tokens"] > 0
    usage = first.json()["usage"]
    counted = usage["input_tokens"] + usage["output_tokens"]
    assert first_usage["charged_tokens"] == counted
    assert gateway.counters()[("cost-month", OCTOBER_FIRST)] == 0
    (row,) = audit_events(gateway.database, run_id)
    assert row["reason"] == "tenant-request-rate"
    # In replay mode every row names the replay deployment (T-39).
    assert (row["deployment"], row["provider"], row["model"]) == (
        "replay-chat",
        "replay",
        "replay-chat",
    )


# ── 18. nothing of the caller in a row, a span or a label ───────────────────
def provider_error_with_canary() -> Exception:
    error = ProviderError("unavailable", 502)
    error.__cause__ = RuntimeError(f"{CANARY} {CLAIM_TEXT}")
    return error


@pytest.mark.parametrize(
    "outcome",
    [
        OK,
        Outcome(provider_error_with_canary()),
        Outcome(RuntimeError(f"{CANARY} {CLAIM_TEXT}")),
    ],
    ids=["answered", "failover", "crash"],
)
def test_no_row_span_or_label_holds_the_prompt_or_a_canary(
    one: Gateway, outcome: Outcome
) -> None:
    one.provider.outcomes[FIRST] = outcome

    response, run_id = one.post()

    metrics = str(
        [
            one.points(name)
            for name in (
                "meridian.gateway.tokens",
                "meridian.gateway.cost",
                "meridian.gateway.calls",
            )
        ]
    )
    for text in (CANARY, CLAIM_TEXT, ANSWER_TEXT):
        if text != ANSWER_TEXT:
            assert text not in response.text
        assert text not in str(one.usage())
        assert text not in str(audit_events(one.database, run_id))
        assert text not in metrics
        assert_spans_hold_no_exception_and_no_canary(one.exporter, text)


# ── the OpenAPI document ────────────────────────────────────────────────────
def test_the_chat_route_declares_the_429_and_the_413_names_both_limits(
    one: Gateway,
) -> None:
    responses = one.client.app.openapi()["paths"]["/v1/chat"]["post"]["responses"]

    assert responses["429"]["description"] == "A limit of the tenant is reached."
    assert "body" in responses["413"]["description"]
    assert "token limit" in responses["413"]["description"]


# ── the meter provider's lifecycle ──────────────────────────────────────────
class ShutdownSpy(MeterProvider):
    def __init__(self) -> None:
        super().__init__()
        self.shutdowns = 0

    def shutdown(self, *args: object, **kwargs: object) -> None:
        self.shutdowns += 1
        super().shutdown(*args, **kwargs)


def replay_settings() -> GatewaySettings:
    return GatewaySettings(
        registry_dir=REGISTRY_DIR,
        mode="replay",
        environment="test",
        database_url="postgresql://model_gateway@db.invalid/meridian",
    )


def test_an_injected_meter_provider_is_left_to_its_caller() -> None:
    spy = ShutdownSpy()

    with TestClient(create_app(replay_settings(), meter_provider=spy)):
        pass

    assert spy.shutdowns == 0


def test_a_meter_provider_the_app_built_is_shut_down_with_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = ShutdownSpy()
    monkeypatch.setattr(gateway_app, "make_meter_provider", lambda _name: spy)

    with TestClient(create_app(replay_settings())):
        assert spy.shutdowns == 0

    assert spy.shutdowns == 1
