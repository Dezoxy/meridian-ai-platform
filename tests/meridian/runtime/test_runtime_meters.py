"""The Agent Runtime's metrics (S064, M1): runs counted once per leg and model
calls counted once per call, by fixed words only (T-03, T-49).

The first half needs no database (the reason words, the model client against a
stub gateway, the meter provider's lifecycle); the second runs legs through the
real runtime app and PostgreSQL, with the stand-ins of ``test_runtime_app``.
"""

import logging
import uuid
from collections.abc import Callable
from typing import Any, TypedDict, get_args

import httpx
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from runtimesupport import register
from servicesupport import (
    GATEWAY_REPLY,
    REGISTRY_DIR,
    metric_points,
    owner_rows,
)

from meridian.platform.common import metrics as common_metrics
from meridian.platform.common.metrics import METRIC_ATTRIBUTE_KEYS
from meridian.runtime import app as runtime_app
from meridian.runtime import runs
from meridian.runtime.failures import GraphFailure
from meridian.runtime.meters import (
    GRAPH_FAILURE,
    RUN_FAILURE_REASONS,
    RuntimeMeters,
    run_reason,
)
from meridian.runtime.model_client import (
    CALL_REASONS,
    REFUSAL_CONTENT_FILTER,
    REFUSAL_HEADER,
    CallOutcome,
    CallReason,
    ModelCallError,
    ModelCallFilteredError,
    ModelCallLimitError,
    ModelCallTimeoutError,
    ModelClient,
)
from meridian.runtime.settings import RuntimeSettings
from meridian.runtime.tool_client import (
    ClientRefusal,
    ToolCallLimit,
    ToolClient,
    ToolError,
    ToolNotAllowed,
    ToolRefused,
    ToolUnavailable,
)

RUNS = "meridian.runtime.runs"
MODEL_CALLS = "meridian.runtime.model_calls"
IDENTITY = {"meridian.tenant": "claims-triage", "meridian.agent": "claims-triage"}
RUN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000bb")


# ── the reason words of a failed leg ────────────────────────────────────────
def test_the_closed_set_of_failure_words_is_pinned() -> None:
    assert {
        "model-call-limit",
        "tool-call-limit",
        "tool-not-allowed",
        "worker-tool-not-allowed",
        "worker-missing",
        "worker-unknown",
        "tool-refused",
        "tool-unavailable",
        "model-timeout",
        "model-filtered",
        "model-error",
        "unexpected",
        "graph-failure",
        "not-saved",
        "not-started",
    } == RUN_FAILURE_REASONS


def test_the_set_holds_every_word_the_client_of_the_tools_can_refuse_with() -> None:
    assert set(get_args(ClientRefusal)) <= RUN_FAILURE_REASONS


@pytest.mark.parametrize(
    "error",
    [
        ModelCallLimitError(),
        ToolCallLimit("policy_lookup"),
        ToolNotAllowed(None),
        ToolNotAllowed("add_claim_note", "worker-tool-not-allowed"),
        ToolNotAllowed(None, "worker-unknown"),
        ToolNotAllowed("policy_lookup", "worker-missing"),
        ToolRefused("policy_lookup", "outside-claim"),
        ToolRefused("policy_lookup", "unknown"),
        ToolUnavailable("policy_lookup"),
        ModelCallTimeoutError(),
        ModelCallFilteredError(),
        ModelCallError(400),
        ModelCallError(0),
        ValueError("boom"),
        ToolError("policy_lookup", "a tool error of no known kind"),
    ],
    ids=lambda error: f"{type(error).__name__}-{getattr(error, 'args', '')}",
)
def test_every_word_a_runtime_error_can_give_is_in_the_closed_set(
    error: Exception,
) -> None:
    assert run_reason(error) in RUN_FAILURE_REASONS


@pytest.mark.parametrize(
    "code", ["anything-a-workload-wrote", "model-error", "no-pending-pause"]
)
def test_a_graph_failure_is_the_one_word_whatever_its_code(code: str) -> None:
    # A code is text the workload chose, even when it spells a word of the
    # runtime's own set: it is never a label.
    assert run_reason(GraphFailure(code)) == GRAPH_FAILURE
    assert GRAPH_FAILURE == "graph-failure"


def test_a_word_outside_the_set_reads_as_unexpected() -> None:
    # A new failure word that the set does not know yet cannot become a label.
    error = ToolNotAllowed("policy_lookup", "a-word-nobody-listed")  # type: ignore[arg-type]

    assert run_reason(error) == "unexpected"


# ── the model client against a stub gateway ─────────────────────────────────
Reported = list[tuple[CallOutcome, CallReason | None]]


def client_over(
    respond: Callable[[httpx.Request], httpx.Response], *, max_calls: int = 10
) -> tuple[ModelClient, Reported, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    reported: Reported = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    client = ModelClient(
        httpx.Client(
            base_url="http://gateway.invalid", transport=httpx.MockTransport(handler)
        ),
        tenant="claims-triage",
        agent="claims-triage",
        run_id=RUN_ID,
        max_calls=max_calls,
        on_call=lambda outcome, reason: reported.append((outcome, reason)),
    )
    return client, reported, seen


def answer(status: int, **headers: str) -> Callable[[httpx.Request], httpx.Response]:
    body = GATEWAY_REPLY if status == 200 else {}
    return lambda request: httpx.Response(status, json=body, headers=headers)


def failing(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        raise error

    return respond


FAILURES: list[tuple[str, Callable[[httpx.Request], httpx.Response], CallReason]] = [
    ("no-answer", failing(httpx.ConnectError("down")), "unreachable"),
    ("a-reset", failing(httpx.ReadError("reset")), "unreachable"),
    ("too-slow", failing(httpx.ReadTimeout("slow")), "timeout"),
    # An HTTP error that is not the transport's: no answer is not what it says.
    ("undecodable", failing(httpx.DecodingError("bad body")), "error"),
    ("redirected", failing(httpx.TooManyRedirects("loop")), "error"),
    ("busy", answer(429), "refused"),
    ("forbidden", answer(403), "refused"),
    ("filtered", answer(400, **{REFUSAL_HEADER: REFUSAL_CONTENT_FILTER}), "filtered"),
    ("a-plain-400", answer(400), "error"),
    ("unauthorised", answer(401), "error"),
    ("broken", answer(500), "error"),
    ("bad-gateway", answer(502), "error"),
    (
        "not-the-contract",
        lambda request: httpx.Response(200, json={"output": "no usage"}),
        "error",
    ),
    ("not-json", lambda request: httpx.Response(200, text="<html>"), "error"),
]


@pytest.mark.parametrize(
    ("respond", "reason"),
    [(respond, reason) for _, respond, reason in FAILURES],
    ids=[name for name, _, _ in FAILURES],
)
def test_each_way_a_call_fails_is_counted_once_under_its_reason(
    respond: Callable[[httpx.Request], httpx.Response], reason: CallReason
) -> None:
    client, reported, _ = client_over(respond)

    with pytest.raises(ModelCallError):
        client.chat([{"role": "user", "content": "hi"}])

    assert reported == [("failed", reason)]


def test_an_error_that_is_not_an_http_one_is_counted_and_raised_as_it_is() -> None:
    closed = RuntimeError("client closed")
    client, reported, _ = client_over(failing(closed))

    with pytest.raises(RuntimeError) as raised:
        client.chat([{"role": "user", "content": "hi"}])

    assert raised.value is closed
    assert reported == [("failed", "error")]


def test_a_call_the_gateway_answered_is_counted_as_completed() -> None:
    client, reported, _ = client_over(answer(200))

    client.chat([{"role": "user", "content": "hi"}])

    assert reported == [("completed", None)]


def test_a_call_past_the_limit_is_counted_as_limit_and_never_sent() -> None:
    client, reported, seen = client_over(answer(200), max_calls=1)
    client.chat([{"role": "user", "content": "hi"}])

    with pytest.raises(ModelCallLimitError):
        client.chat([{"role": "user", "content": "hi"}])

    assert reported == [("completed", None), ("failed", "limit")]
    assert len(seen) == 1


def test_the_failures_above_cover_every_reason_of_the_closed_set() -> None:
    covered = {reason for _, _, reason in FAILURES} | {"limit"}

    assert covered == set(get_args(CallReason))
    assert covered == CALL_REASONS


@pytest.mark.parametrize(
    ("respond", "expected"),
    [
        (failing(httpx.ConnectError("down")), ModelCallError),
        (failing(httpx.ReadTimeout("slow")), ModelCallTimeoutError),
        (answer(429), ModelCallError),
        (answer(403), ModelCallError),
    ],
    ids=["unreachable", "timeout", "busy", "forbidden"],
)
def test_the_errors_the_client_raises_stay_what_they_were(
    respond: Callable[[httpx.Request], httpx.Response], expected: type[Exception]
) -> None:
    # The run's failure word does not change with the finer word of the metric.
    client, _, _ = client_over(respond)

    with pytest.raises(ModelCallError) as raised:
        client.chat([{"role": "user", "content": "hi"}])

    assert type(raised.value) is expected


def test_a_client_without_an_observer_still_works() -> None:
    client = ModelClient(
        httpx.Client(
            base_url="http://gateway.invalid",
            transport=httpx.MockTransport(answer(200)),
        ),
        tenant="claims-triage",
        agent="claims-triage",
        run_id=RUN_ID,
        max_calls=1,
    )

    assert client.chat([{"role": "user", "content": "hi"}]).text == "drafted"


# ── the instruments, without an app ─────────────────────────────────────────
def meters_over(reader: InMemoryMetricReader) -> RuntimeMeters:
    return RuntimeMeters(common_metrics.make_meter_provider("agent-runtime", reader))


def identity() -> runs.RunIdentity:
    return runs.RunIdentity(
        run_id=RUN_ID,
        thread_id=uuid.uuid4(),
        agent="claims-triage",
        tenant="claims-triage",
        reference="CLM-0001",
    )


def test_a_leg_is_counted_by_outcome_and_a_failed_one_with_its_reason() -> None:
    reader = InMemoryMetricReader()
    meters = meters_over(reader)

    meters.leg_ended(identity(), runs.RunOutcome("Completed", None), None)
    meters.leg_ended(identity(), runs.RunOutcome("AwaitingApproval", None), None)
    meters.leg_ended(
        identity(), runs.RunOutcome("Failed", None), GraphFailure("some-code")
    )

    counted = {
        (a["meridian.outcome"], a.get("meridian.reason")): (a, v)
        for a, v in metric_points(reader, RUNS)
    }
    assert {key: value for key, (_, value) in counted.items()} == {
        ("completed", None): 1,
        ("paused", None): 1,
        ("failed", "graph-failure"): 1,
    }
    assert {frozenset(a) for a, _ in counted.values()} == {
        frozenset(IDENTITY | {"meridian.outcome": "x"}),
        frozenset(IDENTITY | {"meridian.outcome": "x", "meridian.reason": "x"}),
    }


def test_every_label_of_both_series_is_on_the_allowlist() -> None:
    reader = InMemoryMetricReader()
    meters = meters_over(reader)
    meters.leg_ended(identity(), runs.RunOutcome("Completed", None), None)
    meters.leg_ended(identity(), runs.RunOutcome("Failed", None), ValueError("x"))
    observe = meters.model_call_observer(identity())
    observe("completed", None)
    observe("failed", "unreachable")

    keys = {
        key
        for name in (RUNS, MODEL_CALLS)
        for attributes, _ in metric_points(reader, name)
        for key in attributes
    }

    assert keys and keys <= METRIC_ATTRIBUTE_KEYS
    assert "meridian.worker" not in keys


def test_a_model_call_is_counted_with_the_tenant_and_agent_of_its_run() -> None:
    reader = InMemoryMetricReader()
    observe = meters_over(reader).model_call_observer(identity())

    observe("failed", "timeout")

    assert metric_points(reader, MODEL_CALLS) == [
        ({"meridian.outcome": "failed", "meridian.reason": "timeout"} | IDENTITY, 1)
    ]


# ── the meter provider's lifecycle ──────────────────────────────────────────
class ShutdownSpy(MeterProvider):
    def __init__(self) -> None:
        super().__init__()
        self.shutdowns = 0

    def shutdown(self, *args: object, **kwargs: object) -> None:
        self.shutdowns += 1
        super().shutdown(*args, **kwargs)


def runtime_settings() -> RuntimeSettings:
    return RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url="http://gateway.invalid",
        database_url="postgresql://agent_runtime@db.invalid/x",
        tool_servers={},
    )


def test_an_injected_meter_provider_is_left_to_its_caller() -> None:
    spy = ShutdownSpy()

    with TestClient(runtime_app.create_app(runtime_settings(), meter_provider=spy)):
        pass

    assert spy.shutdowns == 0


def test_a_meter_provider_the_app_built_is_shut_down_with_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = ShutdownSpy()
    monkeypatch.setattr(runtime_app, "make_meter_provider", lambda _name: spy)

    with TestClient(runtime_app.create_app(runtime_settings())):
        assert spy.shutdowns == 0

    assert spy.shutdowns == 1


def test_without_the_collectors_address_nothing_is_exported_and_nothing_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    built: list[object] = []
    monkeypatch.setattr(
        common_metrics, "OTLPMetricExporter", lambda *a, **k: built.append((a, k))
    )

    with (
        caplog.at_level(logging.INFO, logger=common_metrics.__name__),
        TestClient(runtime_app.create_app(runtime_settings())) as client,
    ):
        assert client.get("/healthz").status_code == 200

    assert built == []
    assert [
        r.getMessage() for r in caplog.records if r.name == common_metrics.__name__
    ] == [
        "agent-runtime: metrics are not exported: "
        "OTEL_EXPORTER_OTLP_ENDPOINT is not set"
    ]


def test_the_app_sets_no_global_meter_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        otel_metrics, "set_meter_provider", lambda provider: calls.append("meter")
    )

    with TestClient(runtime_app.create_app(runtime_settings())) as client:
        assert client.get("/healthz").status_code == 200

    assert calls == []


# ── stand-ins for the graph and the gateway (as test_runtime_app has them) ──
class State(TypedDict, total=False):
    claim: dict
    output: Any


def graph_of(node: Callable[[State], State]) -> StateGraph:
    graph = StateGraph(State)
    graph.add_node("work", node)
    graph.add_edge(START, "work")
    graph.add_edge("work", END)
    return graph


def ok_factory(model: ModelClient, tools: ToolClient) -> StateGraph:
    return graph_of(
        lambda state: {
            "output": {"text": model.chat([{"role": "user", "content": "hi"}]).text}
        }
    )


def calling_factory(calls: int) -> Callable[[ModelClient, ToolClient], StateGraph]:
    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        def work(state: State) -> State:
            for _ in range(calls):
                model.chat([{"role": "user", "content": "hi"}])
            return {"output": {"calls": calls}}

        return graph_of(work)

    return factory


def resumable(
    after: Callable[[Any], None] | None = None,
) -> Callable[[ModelClient, ToolClient], StateGraph]:
    """A graph that pauses once in its second node; once resumed it calls
    ``after`` with the resume value."""

    def factory(model: ModelClient, tools: ToolClient) -> StateGraph:
        def decide(state: State) -> State:
            answer = interrupt("approve")
            if after is not None:
                after(answer)
            return {"output": {"answer": answer}}

        graph = StateGraph(State)
        graph.add_node("before", lambda state: {"output": {"stage": "before"}})
        graph.add_node("decide", decide)
        graph.add_edge(START, "before")
        graph.add_edge("before", "decide")
        graph.add_edge("decide", END)
        return graph

    return factory


class Gateway:
    """A stand-in gateway: it answers ``status`` or raises ``raises``."""

    def __init__(self, status: int = 200, raises: Exception | None = None) -> None:
        self.status = status
        self.raises = raises
        self.client = httpx.Client(
            base_url="http://gateway.invalid", transport=httpx.MockTransport(self)
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.raises is not None:
            raise self.raises
        return httpx.Response(
            self.status, json=GATEWAY_REPLY if self.status == 200 else {}
        )


def make_client(
    db: DatabaseHandle,
    gateway: Gateway | None = None,
    meters: InMemoryMetricReader | None = None,
) -> TestClient:
    settings = RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url="http://gateway.invalid",
        database_url=db.dsn("agent_runtime"),
        tool_servers={},
    )
    app = runtime_app.create_app(
        settings,
        meter_provider=common_metrics.make_meter_provider(
            "agent-runtime", meters or InMemoryMetricReader()
        ),
        http_client=(gateway or Gateway()).client,
    )
    return TestClient(app, raise_server_exceptions=False)


def start(client: TestClient, **overrides: Any) -> httpx.Response:
    body = {
        "agent": "claims-triage",
        "tenant": "claims-triage",
        "reference": "CLM-0001",
        "input": {"claim": {"n": 7}},
    } | overrides
    return client.post("/runs", json=body)


def resume(client: TestClient, run_id: str, **overrides: Any) -> httpx.Response:
    body = {
        "tenant": "claims-triage",
        "reference": "CLM-0001",
        "input": {"approved": True},
    } | overrides
    return client.post(f"/runs/{run_id}/resume", json=body)


def paused_run(client: TestClient) -> str:
    response = start(client)
    assert response.json()["status"] == "AwaitingApproval"
    return response.json()["run_id"]


def run_rows(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(db, "SELECT run_id, status FROM runtime.runs")


# ── legs through the real runtime and PostgreSQL ────────────────────────────
def counted_runs(reader: InMemoryMetricReader) -> dict[tuple, float]:
    return {
        (a["meridian.outcome"], a.get("meridian.reason")): v
        for a, v in metric_points(reader, RUNS)
    }


def test_a_run_that_completes_is_one_completed_leg_with_its_tenant_and_agent(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, ok_factory)
    meters = InMemoryMetricReader()

    start(make_client(fresh_database, meters=meters))

    assert metric_points(meters, RUNS) == [
        ({"meridian.outcome": "completed"} | IDENTITY, 1)
    ]


def test_a_paused_run_and_its_resume_are_two_legs(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable())
    meters = InMemoryMetricReader()
    client = make_client(fresh_database, meters=meters)

    run_id = paused_run(client)

    assert counted_runs(meters) == {("paused", None): 1}

    resume(client, run_id)

    assert counted_runs(meters) == {("paused", None): 1, ("completed", None): 1}


def test_a_resumed_leg_that_fails_is_one_failed_leg_and_the_next_resume_a_completed_one(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def fails_once(answer: Any) -> None:
        attempts.append("ran")
        if len(attempts) == 1:
            raise GraphFailure("not-yet")

    register(monkeypatch, resumable(after=fails_once))
    meters = InMemoryMetricReader()
    client = make_client(fresh_database, meters=meters)
    run_id = paused_run(client)

    failed = resume(client, run_id)

    # The run goes back to its pause; the leg is still a failed one.
    assert failed.json()["status"] == "AwaitingApproval"
    assert counted_runs(meters) == {("paused", None): 1, ("failed", "graph-failure"): 1}

    resume(client, run_id)

    assert counted_runs(meters) == {
        ("paused", None): 1,
        ("failed", "graph-failure"): 1,
        ("completed", None): 1,
    }


def raising(error: Exception) -> Callable[[Any, Any], StateGraph]:
    def factory(model: Any, tools: Any) -> StateGraph:
        def work(state: State) -> State:
            raise error

        return graph_of(work)

    return factory


@pytest.mark.parametrize(
    ("factory", "reason"),
    [
        (raising(ValueError("boom")), "unexpected"),
        (raising(GraphFailure("made-up-by-a-workload")), "graph-failure"),
        (calling_factory(runs.MAX_MODEL_CALLS_PER_RUN + 1), "model-call-limit"),
    ],
    ids=["unexpected", "graph-failure", "model-call-limit"],
)
def test_a_failed_leg_is_counted_once_under_the_word_of_its_failure(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable,
    reason: str,
) -> None:
    register(monkeypatch, factory)
    meters = InMemoryMetricReader()

    response = start(make_client(fresh_database, meters=meters))

    assert response.status_code == 502
    assert metric_points(meters, RUNS) == [
        ({"meridian.outcome": "failed", "meridian.reason": reason} | IDENTITY, 1)
    ]


@pytest.mark.parametrize(
    ("gateway", "call_reason", "run_reason_word"),
    [
        (Gateway(raises=httpx.ConnectError("down")), "unreachable", "model-error"),
        (Gateway(raises=httpx.ReadTimeout("slow")), "timeout", "model-timeout"),
        (Gateway(status=429), "refused", "model-error"),
        (Gateway(status=403), "refused", "model-error"),
        (Gateway(status=500), "error", "model-error"),
    ],
    ids=["unreachable", "timeout", "busy", "forbidden", "broken"],
)
def test_a_gateway_that_cannot_be_reached_or_that_answers_badly_is_told_apart(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    gateway: Gateway,
    call_reason: str,
    run_reason_word: str,
) -> None:
    register(monkeypatch, ok_factory)
    meters = InMemoryMetricReader()

    response = start(make_client(fresh_database, gateway, meters=meters))

    assert response.status_code in (502, 504)
    assert metric_points(meters, MODEL_CALLS) == [
        ({"meridian.outcome": "failed", "meridian.reason": call_reason} | IDENTITY, 1)
    ]
    # The run's own word is what the audit rows and the Claims API read.
    assert metric_points(meters, RUNS) == [
        (
            {"meridian.outcome": "failed", "meridian.reason": run_reason_word}
            | IDENTITY,
            1,
        )
    ]


def test_a_run_that_calls_the_model_past_its_limit_counts_each_call_once(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, calling_factory(runs.MAX_MODEL_CALLS_PER_RUN + 1))
    meters = InMemoryMetricReader()

    start(make_client(fresh_database, meters=meters))

    counted = {
        (a["meridian.outcome"], a.get("meridian.reason")): v
        for a, v in metric_points(meters, MODEL_CALLS)
    }
    assert counted == {
        ("completed", None): runs.MAX_MODEL_CALLS_PER_RUN,
        ("failed", "limit"): 1,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant": "no-such-tenant"},
        {"agent": "rogue-agent"},
        {"agent": "knowledge-ingestion"},
    ],
    ids=["unknown-tenant", "unknown-agent", "job-agent"],
)
def test_a_start_refused_before_a_run_exists_counts_nothing(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
) -> None:
    register(monkeypatch, ok_factory)
    meters = InMemoryMetricReader()

    response = start(make_client(fresh_database, meters=meters), **overrides)

    assert response.status_code == 403
    assert run_rows(fresh_database) == []
    assert metric_points(meters, RUNS) == []
    assert metric_points(meters, MODEL_CALLS) == []


def test_a_resume_that_finds_no_such_run_counts_nothing_more(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, resumable())
    meters = InMemoryMetricReader()
    client = make_client(fresh_database, meters=meters)
    run_id = paused_run(client)

    response = resume(client, run_id, reference="CLM-9999")

    assert response.status_code == 404
    assert counted_runs(meters) == {("paused", None): 1}


def test_no_series_holds_text_a_graph_or_a_caller_chose(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = "claimant-canary-text-77"
    register(monkeypatch, raising(GraphFailure("claimant-canary-text")))
    meters = InMemoryMetricReader()
    client = make_client(fresh_database, meters=meters)

    start(client, reference="CLM-0001", input={"claim": {"n": 1, "text": canary}})

    held = str(metric_points(meters, RUNS) + metric_points(meters, MODEL_CALLS))
    assert canary not in held
    assert "CLM-0001" not in held
    assert held.count("graph-failure") == 1
