"""POST /v1/chat walks the allowed candidates under one deadline (S042).

No network and no Azure: a scripted provider answers or raises per deployment
and moves a fake clock, so a test says how long an attempt took. The real chat
route lists one or two deployments, so every test pins its own route.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from servicesupport import (
    GLOBAL_DEPLOYMENT_YAML,
    REPLAY_ENTRY,
    SECOND_DEPLOYMENT_YAML,
    FakeClock,
    assert_spans_hold_no_exception_and_no_canary,
    audit_events,
    pin_chat_route,
)

from meridian.platform.common import audit
from meridian.platform.common.audit import AuditEvent, AuditUnavailable
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.gateway import app as gateway_app
from meridian.platform.gateway import walk
from meridian.platform.gateway.app import (
    PROVIDER_FAILED,
    PROVIDER_TIMED_OUT,
    PROVIDER_UNAVAILABLE,
    create_app,
)
from meridian.platform.gateway.models import ChatRequest
from meridian.platform.gateway.providers.base import (
    ProviderError,
    ProviderErrorKind,
    ProviderReply,
)
from meridian.platform.gateway.resilience import (
    CALL_DEADLINE_SECONDS,
    DEPLOYMENT_FAILURES,
    FAILURE_THRESHOLD,
    MIN_ATTEMPT_SECONDS,
    OPEN_SECONDS,
)
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.registry import load_registry
from meridian.platform.registry.checks import run_checks
from meridian.platform.registry.models import Deployment

UNUSED_DSN = "postgresql://model_gateway@db.invalid/meridian"
CLAIM_TEXT = "claimant-secret-text-42"
CANARY = "CANARY-provider-internals-9917"
ANSWER_TEXT = "a fake answer"
BODY = {"messages": [{"role": "user", "content": CLAIM_TEXT}]}
FIRST = "aoai-sdc-gpt-4o"
SECOND = "aoai-sdc-gpt-4o-second"
GLOBAL = "aoai-sdc-gpt-4o-global"
COUNTED_KINDS = sorted(DEPLOYMENT_FAILURES)
UNCOUNTED_KINDS = ["rejected", "auth"]
HTTP_SERVICE_UNAVAILABLE = 503


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one call to a deployment does: take ``takes`` seconds of the fake
    clock, then raise ``error`` or answer."""

    error: Exception | None = None
    takes: float = 0.0


OK = Outcome()


def failing(kind: ProviderErrorKind, takes: float = 0.0) -> Outcome:
    return Outcome(ProviderError(kind, 500), takes)


class ScriptedProvider:
    """Answers or raises per deployment ID; records every call it receives."""

    def __init__(
        self, clock: FakeClock, outcomes: Mapping[str, Outcome] | None = None
    ) -> None:
        self.clock = clock
        self.outcomes: dict[str, Outcome] = {FIRST: OK, SECOND: OK, GLOBAL: OK} | dict(
            outcomes or {}
        )
        self.calls: list[tuple[str, float]] = []

    def script(self, deployment_id: str, outcome: Outcome) -> None:
        self.outcomes[deployment_id] = outcome

    @property
    def called(self) -> list[str]:
        return [deployment_id for deployment_id, _ in self.calls]

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        self.calls.append((deployment.id, timeout_seconds))
        outcome = self.outcomes[deployment.id]
        self.clock.advance(outcome.takes)
        if outcome.error is not None:
            raise outcome.error
        return ProviderReply(
            text=ANSWER_TEXT,
            finish_reason="stop",
            model="gpt-4o-2024-11-20",
            input_tokens=11,
            output_tokens=7,
        )


@dataclass(slots=True)
class Gateway:
    client: TestClient
    provider: ScriptedProvider
    clock: FakeClock
    exporter: InMemorySpanExporter
    database: DatabaseHandle | None

    def post(self, tenant: str = "claims-triage") -> tuple[httpx.Response, uuid.UUID]:
        run_id = uuid.uuid4()
        response = self.client.post(
            "/v1/chat",
            json=BODY,
            headers={
                "X-Meridian-Tenant": tenant,
                "X-Meridian-Agent": "claims-triage",
                "X-Meridian-Run": str(run_id),
            },
        )
        return response, run_id

    def rows(self, run_id: uuid.UUID) -> list[dict]:
        assert self.database is not None
        return audit_events(self.database, run_id)

    def chat_span(self) -> ReadableSpan:
        (span,) = [
            s for s in self.exporter.get_finished_spans() if s.name == "gateway.chat"
        ]
        return span

    def attempt_spans(self) -> list[ReadableSpan]:
        spans = [
            s for s in self.exporter.get_finished_spans() if s.name == "gateway.attempt"
        ]
        return sorted(spans, key=lambda s: s.attributes["meridian.attempt"])


def build_gateway(
    registry_dir: Path,
    database: DatabaseHandle | None = None,
    outcomes: Mapping[str, Outcome] | None = None,
) -> Gateway:
    clock = FakeClock()
    exporter = InMemorySpanExporter()
    provider = ScriptedProvider(clock, outcomes)
    settings = GatewaySettings(
        registry_dir=registry_dir,
        mode="live",
        environment="test",
        database_url=database.dsn("model_gateway") if database else UNUSED_DSN,
    )
    app = create_app(
        settings,
        tracer_provider=make_tracer_provider("model-gateway", exporter),
        providers={"azure-openai": provider},
        clock=clock,
    )
    return Gateway(TestClient(app), provider, clock, exporter, database)


def facts(deployment_id: str) -> dict[str, str]:
    return {
        "deployment": deployment_id,
        "provider": "azure-openai",
        "model": "gpt-4o",
        "data_class": "personal",
        "sku": "Standard",
        "region": "swedencentral",
        "residency": "eu-region",
    }


def row_facts(row: dict) -> dict:
    return {name: row[name] for name in facts(FIRST)}


def summary(rows: list[dict]) -> list[tuple[str, str, str | None]]:
    return [(r["outcome"], r["deployment"], r["reason"]) for r in rows]


def attributes(span: ReadableSpan) -> dict:
    return dict(span.attributes)


# ── registries ──────────────────────────────────────────────────────────────
@pytest.fixture
def planted(plant: Callable[..., Path]) -> Path:
    """A registry that also holds the test-only second EU deployment and the
    global one; each test pins the route it needs."""
    return plant(
        (
            "models.yaml",
            REPLAY_ENTRY,
            SECOND_DEPLOYMENT_YAML + GLOBAL_DEPLOYMENT_YAML + REPLAY_ENTRY,
        )
    )


def pinned(planted: Path, *candidates: str) -> Path:
    directory = pin_chat_route(planted, *candidates)
    assert run_checks(load_registry(directory)) == ()
    return directory


@pytest.fixture
def two(planted: Path) -> Path:
    return pinned(planted, FIRST, SECOND)


def open_first(gateway: Gateway) -> None:
    """Fail the first candidate until its circuit opens; the second answers."""
    gateway.provider.script(FIRST, failing("unavailable"))
    for _ in range(FAILURE_THRESHOLD):
        response, _ = gateway.post()
        assert response.status_code == 200
    gateway.provider.calls.clear()
    gateway.exporter.clear()


class AuditSwitch:
    """Stands in for the gateway's audit write: raises a bare ``AuditUnavailable``
    for the outcomes in ``failing`` and writes every other row for real."""

    def __init__(self) -> None:
        self.real = gateway_app.write_audit
        self.failing: set[str] = set()
        self.raised: list[AuditUnavailable] = []

    def __call__(self, dsn: str, event: AuditEvent) -> None:
        if event.outcome in self.failing:
            error = AuditUnavailable("down")
            self.raised.append(error)
            raise error
        self.real(dsn, event)


@pytest.fixture
def audit_switch(monkeypatch: pytest.MonkeyPatch) -> AuditSwitch:
    switch = AuditSwitch()
    monkeypatch.setattr(gateway_app, "write_audit", switch)
    return switch


def raise_once_in_the_walk(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``walk.<name>`` raise on its first call and work afterwards."""
    real = getattr(walk, name)
    calls: list[None] = []

    def wrapper(*args: object, **kwargs: object) -> object:
        if not calls:
            calls.append(None)
            raise RuntimeError(CANARY)
        return real(*args, **kwargs)

    monkeypatch.setattr(walk, name, wrapper)


# ── a counted failure falls over to the next candidate ──────────────────────
@pytest.mark.parametrize("kind", COUNTED_KINDS)
def test_a_failing_first_candidate_is_answered_by_the_second(
    fresh_database: DatabaseHandle, two: Path, kind: str
) -> None:
    gateway = build_gateway(two, fresh_database, {FIRST: failing(kind)})

    response, run_id = gateway.post()

    assert response.status_code == 200
    assert response.json()["deployment"] == SECOND
    assert gateway.provider.called == [FIRST, SECOND]
    first, second = gateway.rows(run_id)
    assert summary([first, second]) == [
        ("failed", FIRST, kind),
        ("completed", SECOND, None),
    ]
    assert row_facts(first) == facts(FIRST)
    assert (first["input_tokens"], first["output_tokens"]) == (None, None)
    assert row_facts(second) == facts(SECOND)
    assert (second["input_tokens"], second["output_tokens"]) == (11, 7)
    chat = gateway.chat_span()
    one, two_span = gateway.attempt_spans()
    assert [s.attributes["meridian.attempt"] for s in (one, two_span)] == [1, 2]
    assert one.parent.span_id == two_span.parent.span_id == chat.context.span_id
    assert one.attributes["meridian.deployment"] == FIRST
    assert one.attributes["error.type"] == kind
    assert one.status.status_code == StatusCode.ERROR
    assert two_span.attributes["meridian.deployment"] == SECOND
    assert "error.type" not in two_span.attributes
    assert two_span.status.status_code != StatusCode.ERROR
    parent = attributes(chat)
    assert parent["meridian.attempts"] == 2
    assert parent["meridian.skipped"] == 0
    assert parent["meridian.deployment"] == SECOND
    assert parent["meridian.data_class"] == "personal"
    assert "error.type" not in parent


# ── T-45: a rejected request does not walk on and does not open a circuit ───
@pytest.mark.parametrize("kind", UNCOUNTED_KINDS)
def test_a_rejected_or_unauthorised_call_ends_the_walk_and_never_opens_a_circuit(
    fresh_database: DatabaseHandle, two: Path, kind: str
) -> None:
    gateway = build_gateway(two, fresh_database, {FIRST: failing(kind)})

    for _ in range(FAILURE_THRESHOLD + 1):
        response, run_id = gateway.post()
        assert response.status_code == 502
        assert response.json() == {"detail": PROVIDER_FAILED}
        (row,) = gateway.rows(run_id)
        assert (row["outcome"], row["deployment"], row["reason"]) == (
            "failed",
            FIRST,
            kind,
        )

    # The first candidate was called every time, the second never.
    assert gateway.provider.called == [FIRST] * (FAILURE_THRESHOLD + 1)
    gateway.provider.script(FIRST, OK)
    response, _ = gateway.post()
    assert response.json()["deployment"] == FIRST


def test_an_uncounted_failure_between_counted_ones_does_not_reset_the_count(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(two, fresh_database, {FIRST: failing("unavailable")})
    for _ in range(FAILURE_THRESHOLD - 1):
        gateway.post()
    gateway.provider.script(FIRST, failing("rejected"))
    gateway.post()
    gateway.provider.script(FIRST, failing("unavailable"))
    gateway.post()  # the threshold's failure
    gateway.provider.calls.clear()

    _, run_id = gateway.post()

    assert gateway.provider.called == [SECOND]
    assert summary(gateway.rows(run_id))[0] == ("skipped", FIRST, "circuit-open")


# ── both fail ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("first_kind", "last_kind", "status", "detail"),
    [
        ("unavailable", "unavailable", 502, PROVIDER_FAILED),
        ("timeout", "rate-limited", 502, PROVIDER_FAILED),
        ("unavailable", "timeout", 504, PROVIDER_TIMED_OUT),
        ("timeout", "bad-response", 502, PROVIDER_FAILED),
    ],
)
def test_when_every_candidate_fails_the_last_kind_picks_the_status(
    fresh_database: DatabaseHandle,
    two: Path,
    first_kind: str,
    last_kind: str,
    status: int,
    detail: str,
) -> None:
    gateway = build_gateway(
        two,
        fresh_database,
        {FIRST: failing(first_kind), SECOND: failing(last_kind)},
    )

    response, run_id = gateway.post()

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert summary(gateway.rows(run_id)) == [
        ("failed", FIRST, first_kind),
        ("failed", SECOND, last_kind),
    ]
    parent = attributes(gateway.chat_span())
    assert parent["error.type"] == last_kind
    assert parent["meridian.deployment"] == SECOND
    assert (parent["meridian.attempts"], parent["meridian.skipped"]) == (2, 0)


# ── an open circuit is skipped ──────────────────────────────────────────────
def test_after_the_threshold_the_next_call_skips_the_first_candidate(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(two, fresh_database)
    open_first(gateway)

    response, run_id = gateway.post()

    assert response.status_code == 200
    assert response.json()["deployment"] == SECOND
    assert gateway.provider.called == [SECOND]
    skipped, completed = gateway.rows(run_id)
    assert summary([skipped, completed]) == [
        ("skipped", FIRST, "circuit-open"),
        ("completed", SECOND, None),
    ]
    assert row_facts(skipped) == facts(FIRST)
    assert (skipped["input_tokens"], skipped["output_tokens"]) == (None, None)
    parent = attributes(gateway.chat_span())
    assert (parent["meridian.attempts"], parent["meridian.skipped"]) == (1, 1)
    (attempt,) = gateway.attempt_spans()
    assert attempt.attributes["meridian.attempt"] == 1  # a skip is not an attempt


def test_one_failure_fewer_than_the_threshold_does_not_skip(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(two, fresh_database, {FIRST: failing("timeout")})
    for _ in range(FAILURE_THRESHOLD - 1):
        gateway.post()
    gateway.provider.calls.clear()

    _, run_id = gateway.post()

    assert gateway.provider.called == [FIRST, SECOND]
    assert [r["outcome"] for r in gateway.rows(run_id)] == ["failed", "completed"]


def test_with_every_circuit_open_the_answer_is_503_and_no_provider_is_called(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(
        two,
        fresh_database,
        {FIRST: failing("unavailable"), SECOND: failing("unavailable")},
    )
    for _ in range(FAILURE_THRESHOLD):
        response, _ = gateway.post()
        assert response.status_code == 502
    gateway.provider.calls.clear()
    gateway.exporter.clear()

    response, run_id = gateway.post()

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    assert response.json() == {"detail": PROVIDER_UNAVAILABLE}
    assert gateway.provider.calls == []
    skipped = gateway.rows(run_id)
    assert summary(skipped) == [
        ("skipped", FIRST, "circuit-open"),
        ("skipped", SECOND, "circuit-open"),
    ]
    assert row_facts(skipped[1]) == facts(SECOND)
    parent = attributes(gateway.chat_span())
    assert parent["error.type"] == "circuit-open"
    assert (parent["meridian.attempts"], parent["meridian.skipped"]) == (0, 2)
    # Nothing was called, so the walk names no deployment on the parent span.
    assert not any(name.startswith("meridian.deployment") for name in parent)
    assert "meridian.provider" not in parent
    assert gateway.attempt_spans() == []


# ── the probe after the cooldown ────────────────────────────────────────────
def test_after_the_cooldown_one_probe_that_answers_closes_the_circuit(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(two, fresh_database)
    open_first(gateway)
    gateway.clock.advance(OPEN_SECONDS)
    gateway.provider.script(FIRST, OK)

    response, run_id = gateway.post()
    follow_response, follow_run = gateway.post()

    assert response.json()["deployment"] == FIRST
    assert summary(gateway.rows(run_id)) == [("completed", FIRST, None)]
    assert follow_response.json()["deployment"] == FIRST
    assert summary(gateway.rows(follow_run)) == [("completed", FIRST, None)]
    assert gateway.provider.called == [FIRST, FIRST]


def test_after_the_cooldown_a_probe_that_fails_opens_the_circuit_again(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(two, fresh_database)
    open_first(gateway)
    gateway.clock.advance(OPEN_SECONDS)

    _, probe_run = gateway.post()
    _, follow_run = gateway.post()

    assert summary(gateway.rows(probe_run)) == [
        ("failed", FIRST, "unavailable"),
        ("completed", SECOND, None),
    ]
    assert summary(gateway.rows(follow_run)) == [
        ("skipped", FIRST, "circuit-open"),
        ("completed", SECOND, None),
    ]
    assert gateway.provider.called == [FIRST, SECOND, SECOND]


def test_just_before_the_cooldown_ends_the_circuit_is_still_open(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(two, fresh_database)
    open_first(gateway)
    gateway.clock.advance(OPEN_SECONDS - 0.5)

    gateway.post()

    assert gateway.provider.called == [SECOND]


def test_an_unexpected_exception_in_a_probe_is_a_500_and_the_next_call_probes_again(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(two, fresh_database)
    open_first(gateway)
    gateway.clock.advance(OPEN_SECONDS)
    gateway.provider.script(FIRST, Outcome(RuntimeError(CANARY)))

    response, run_id = gateway.post()

    assert response.status_code == 500
    assert CANARY not in response.text
    assert summary(gateway.rows(run_id)) == [("failed", FIRST, "internal")]
    assert gateway.provider.called == [FIRST]  # no other candidate was called
    parent = attributes(gateway.chat_span())
    assert parent["error.type"] == "internal"
    assert (parent["meridian.attempts"], parent["meridian.skipped"]) == (1, 0)
    # The probe was released, not held: the next call probes the same candidate.
    gateway.provider.script(FIRST, OK)
    follow, follow_run = gateway.post()
    assert follow.json()["deployment"] == FIRST
    assert summary(gateway.rows(follow_run)) == [("completed", FIRST, None)]


# ── the deadline ────────────────────────────────────────────────────────────
def test_a_normal_call_gets_the_whole_call_deadline(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(two, fresh_database)

    gateway.post()

    assert gateway.provider.calls == [(FIRST, CALL_DEADLINE_SECONDS)]


def test_the_next_attempt_gets_the_time_the_deadline_has_left(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(
        two, fresh_database, {FIRST: failing("unavailable", takes=10.0)}
    )

    response, _ = gateway.post()

    assert response.status_code == 200
    assert gateway.provider.calls == [(FIRST, CALL_DEADLINE_SECONDS), (SECOND, 15.0)]


def test_with_less_than_the_minimum_left_the_next_candidate_is_skipped(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(
        two, fresh_database, {FIRST: failing("timeout", takes=24.0)}
    )

    response, run_id = gateway.post()

    assert response.status_code == 504
    assert response.json() == {"detail": PROVIDER_TIMED_OUT}
    assert gateway.provider.called == [FIRST]
    first, skipped = gateway.rows(run_id)
    assert summary([first, skipped]) == [
        ("failed", FIRST, "timeout"),
        ("skipped", SECOND, "deadline"),
    ]
    assert row_facts(skipped) == facts(SECOND)
    # The parent names the candidate that was called and why it failed, not the
    # one that was skipped for the deadline afterwards.
    parent = attributes(gateway.chat_span())
    assert parent["error.type"] == "timeout"
    assert parent["meridian.deployment"] == FIRST
    assert (parent["meridian.attempts"], parent["meridian.skipped"]) == (1, 1)


def test_with_exactly_the_minimum_left_the_next_candidate_is_still_tried(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(
        two,
        fresh_database,
        {
            FIRST: failing(
                "unavailable", takes=CALL_DEADLINE_SECONDS - MIN_ATTEMPT_SECONDS
            )
        },
    )

    response, _ = gateway.post()

    assert response.status_code == 200
    assert gateway.provider.calls[1] == (SECOND, MIN_ATTEMPT_SECONDS)


def test_with_a_little_less_than_the_minimum_left_the_next_candidate_is_skipped(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(
        two,
        fresh_database,
        {
            FIRST: failing(
                "unavailable", takes=CALL_DEADLINE_SECONDS - MIN_ATTEMPT_SECONDS + 0.5
            )
        },
    )

    _, run_id = gateway.post()

    assert gateway.provider.called == [FIRST]
    assert summary(gateway.rows(run_id)) == [
        ("failed", FIRST, "unavailable"),
        ("skipped", SECOND, "deadline"),
    ]


def test_a_first_candidate_that_used_its_time_leaves_the_second_a_circuit_untouched(
    fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(
        two, fresh_database, {FIRST: failing("timeout", takes=20.0)}
    )

    for _ in range(FAILURE_THRESHOLD):
        response, run_id = gateway.post()
        assert response.status_code == 504
        assert summary(gateway.rows(run_id)) == [
            ("failed", FIRST, "timeout"),
            ("skipped", SECOND, "deadline"),
        ]

    # The second was never called and never acquired: only the first is open.
    assert gateway.provider.called == [FIRST] * FAILURE_THRESHOLD
    _, next_run = gateway.post()
    assert gateway.provider.called[FAILURE_THRESHOLD:] == [SECOND]
    assert summary(gateway.rows(next_run)) == [
        ("skipped", FIRST, "circuit-open"),
        ("completed", SECOND, None),
    ]


def test_a_deadline_skip_does_not_take_the_probe_of_a_circuit_due_for_one(
    fresh_database: DatabaseHandle, planted: Path
) -> None:
    gateway = build_gateway(
        pinned(planted, SECOND, FIRST),
        fresh_database,
        {FIRST: failing("unavailable"), SECOND: failing("unavailable")},
    )
    for _ in range(FAILURE_THRESHOLD):
        gateway.post()  # both fail: both circuits open
    gateway.clock.advance(OPEN_SECONDS)  # both are due for a probe
    gateway.provider.script(SECOND, failing("timeout", takes=24.0))

    _, slow_run = gateway.post()  # the second's probe eats the deadline
    gateway.provider.script(FIRST, OK)
    _, next_run = gateway.post()

    assert summary(gateway.rows(slow_run)) == [
        ("failed", SECOND, "timeout"),
        ("skipped", FIRST, "deadline"),
    ]
    # The first was skipped for the deadline without being acquired, so its
    # probe is still free for the next call.
    assert summary(gateway.rows(next_run)) == [
        ("skipped", SECOND, "circuit-open"),
        ("completed", FIRST, None),
    ]


# ── T-44: a failing candidate never opens the way to a filtered one ─────────
def test_a_global_deployment_is_never_called_for_personal_data_after_a_failure(
    fresh_database: DatabaseHandle, planted: Path
) -> None:
    route = pinned(planted, GLOBAL, FIRST, SECOND)
    gateway = build_gateway(route, fresh_database, {FIRST: failing("timeout")})

    response, run_id = gateway.post()

    assert response.status_code == 200
    assert response.json()["deployment"] == SECOND
    assert gateway.provider.called == [FIRST, SECOND]
    assert GLOBAL not in str(gateway.rows(run_id))


def test_a_failing_last_eu_candidate_is_a_502_and_never_reaches_a_global_one(
    fresh_database: DatabaseHandle, planted: Path
) -> None:
    route = pinned(planted, FIRST, GLOBAL)
    gateway = build_gateway(route, fresh_database, {FIRST: failing("unavailable")})

    response, run_id = gateway.post()

    assert response.status_code == 502
    assert gateway.provider.called == [FIRST]
    assert summary(gateway.rows(run_id)) == [("failed", FIRST, "unavailable")]
    assert GLOBAL not in str(gateway.rows(run_id))
    assert all(
        s.attributes.get("meridian.deployment") != GLOBAL
        for s in gateway.exporter.get_finished_spans()
    )


def test_synthetic_data_does_fall_over_to_the_global_deployment(
    fresh_database: DatabaseHandle, planted: Path
) -> None:
    route = pinned(planted, FIRST, GLOBAL)
    gateway = build_gateway(route, fresh_database, {FIRST: failing("timeout")})

    response, _ = gateway.post(tenant="development")

    assert response.status_code == 200
    assert response.json()["deployment"] == GLOBAL
    assert gateway.provider.called == [FIRST, GLOBAL]


# ── the audit write is part of the answer ───────────────────────────────────
def test_when_the_audit_of_a_failed_attempt_fails_no_other_candidate_is_called(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle, two: Path
) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise psycopg.OperationalError("connection refused: password=hunter2")

    monkeypatch.setattr(audit, "connect", fail_connect)
    gateway = build_gateway(two, fresh_database, {FIRST: failing("unavailable")})

    response, _ = gateway.post()

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    assert "hunter2" not in response.text
    assert gateway.provider.called == [FIRST]
    parent = attributes(gateway.chat_span())
    assert parent["error.type"] == "unavailable"
    assert parent["meridian.attempts"] == 1


def test_a_failing_audit_write_does_not_leave_a_probe_held(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle, two: Path
) -> None:
    gateway = build_gateway(two, fresh_database)
    open_first(gateway)
    gateway.clock.advance(OPEN_SECONDS)
    gateway.provider.script(FIRST, Outcome(RuntimeError(CANARY)))
    real_connect = audit.connect

    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(audit, "connect", fail_connect)
    response, _ = gateway.post()  # the probe crashes and its audit write fails
    monkeypatch.setattr(audit, "connect", real_connect)

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    gateway.provider.script(FIRST, OK)
    follow, _ = gateway.post()
    assert follow.json()["deployment"] == FIRST  # probed again, not skipped


def test_a_probe_that_times_out_while_its_audit_write_fails_opens_the_circuit_again(
    fresh_database: DatabaseHandle, two: Path, audit_switch: AuditSwitch
) -> None:
    gateway = build_gateway(two, fresh_database)
    open_first(gateway)
    gateway.clock.advance(OPEN_SECONDS)
    gateway.provider.script(FIRST, failing("timeout"))
    audit_switch.failing = {"failed"}

    response, _ = gateway.post()  # the probe times out; its row cannot be written
    audit_switch.failing = set()

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    # Open again with a fresh cooldown, not stuck half-open with a probe held.
    gateway.provider.calls.clear()
    _, skipped_run = gateway.post()
    assert gateway.provider.called == [SECOND]
    assert summary(gateway.rows(skipped_run))[0] == ("skipped", FIRST, "circuit-open")
    gateway.clock.advance(OPEN_SECONDS)
    gateway.provider.script(FIRST, OK)
    follow, _ = gateway.post()
    assert follow.json()["deployment"] == FIRST  # the next call probes


def test_a_completed_row_that_cannot_be_written_means_no_output_and_a_closed_circuit(
    fresh_database: DatabaseHandle, two: Path, audit_switch: AuditSwitch
) -> None:
    gateway = build_gateway(two, fresh_database)
    open_first(gateway)
    gateway.clock.advance(OPEN_SECONDS)
    gateway.provider.script(FIRST, OK)
    audit_switch.failing = {"completed"}

    response, _ = gateway.post()  # the probe answers; its row cannot be written
    audit_switch.failing = set()

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    assert ANSWER_TEXT not in response.text
    # The reply settled the probe before the row: the circuit is closed, so the
    # next calls go to the first candidate again.
    gateway.provider.calls.clear()
    gateway.post()
    gateway.post()
    assert gateway.provider.called == [FIRST, FIRST]


@pytest.mark.parametrize("name", ["start_span", "set_span_attributes"])
def test_an_exception_before_the_provider_call_does_not_leave_a_probe_held(
    monkeypatch: pytest.MonkeyPatch,
    fresh_database: DatabaseHandle,
    two: Path,
    name: str,
) -> None:
    gateway = build_gateway(two, fresh_database)
    open_first(gateway)
    gateway.clock.advance(OPEN_SECONDS)
    gateway.provider.script(FIRST, OK)
    raise_once_in_the_walk(monkeypatch, name)

    response, run_id = gateway.post()  # takes the probe, then fails before the call

    assert response.status_code == 500
    assert CANARY not in response.text
    assert gateway.provider.calls == []
    assert gateway.rows(run_id) == []
    follow, follow_run = gateway.post()
    assert follow.json()["deployment"] == FIRST  # probed again, not skipped
    assert summary(gateway.rows(follow_run)) == [("completed", FIRST, None)]


def test_the_audit_failure_of_a_crashed_attempt_carries_no_provider_exception(
    fresh_database: DatabaseHandle, two: Path, audit_switch: AuditSwitch
) -> None:
    gateway = build_gateway(two, fresh_database, {FIRST: Outcome(RuntimeError(CANARY))})
    audit_switch.failing = {"failed"}

    response, _ = gateway.post()

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    (raised,) = audit_switch.raised
    assert raised.__context__ is None  # the provider's exception, with its text
    assert raised.__cause__ is None


def test_the_audit_failure_of_a_failed_attempt_carries_no_provider_exception(
    fresh_database: DatabaseHandle, two: Path, audit_switch: AuditSwitch
) -> None:
    gateway = build_gateway(
        two, fresh_database, {FIRST: Outcome(provider_error_with_canary())}
    )
    audit_switch.failing = {"failed"}

    gateway.post()

    (raised,) = audit_switch.raised
    assert raised.__context__ is None
    assert raised.__cause__ is None


def test_the_503_of_the_chat_route_says_a_deployment_or_the_store_can_be_unavailable(
    two: Path,
) -> None:
    gateway = build_gateway(two)

    description = gateway.client.app.openapi()["paths"]["/v1/chat"]["post"][
        "responses"
    ]["503"]["description"]

    assert description == (
        "The audit log or the rate store is unavailable, or no model deployment "
        "can be tried now."
    )


# ── T-18, T-03: nothing of an exception or the claim is kept ────────────────
def provider_error_with_canary() -> Exception:
    error = ProviderError("unavailable", 502)
    error.__cause__ = RuntimeError(f"{CANARY} {CLAIM_TEXT}")
    return error


@pytest.mark.parametrize(
    "outcome",
    [Outcome(provider_error_with_canary()), Outcome(RuntimeError(f"{CANARY}"))],
    ids=["failover", "crash"],
)
def test_no_span_or_row_holds_the_claim_or_a_canary_from_a_provider_exception(
    fresh_database: DatabaseHandle, two: Path, outcome: Outcome
) -> None:
    gateway = build_gateway(two, fresh_database, {FIRST: outcome})

    response, run_id = gateway.post()

    for canary in (CANARY, CLAIM_TEXT):
        assert canary not in response.text
    for canary in (CANARY, CLAIM_TEXT, ANSWER_TEXT):
        assert canary not in str(gateway.rows(run_id))
        assert_spans_hold_no_exception_and_no_canary(gateway.exporter, canary)
    assert gateway.attempt_spans()
