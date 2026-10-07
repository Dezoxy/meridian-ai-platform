"""The reviews' findings in the Agent Runtime's metrics (S064, contract F2a).

A meter never fails the work it counts; a leg is counted after its status is
written, by what the leg did, unless nothing could be written (``not-saved``);
a run that could not start is counted (``not-started``); every model call is
counted under the right word; a service without a collector address says so;
and a provider built the way production builds it exports ``runs``.

Most tests need no database: the runtime's writes (``runs.start_run`` and the
rest) are replaced, so the order of the count and the write is what is read.
The helpers are the ones ``test_runtime_meters`` has, kept small here (that
file is near the size limit).
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypedDict

import httpx
import psycopg
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricExportResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from runtimesupport import register
from servicesupport import (
    GATEWAY_REPLY,
    REGISTRY_DIR,
    audit_events,
    metric_points,
    owner_rows,
)

from meridian.platform.common import http as common_http
from meridian.platform.common import metrics as common_metrics
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.runtime import app as runtime_app
from meridian.runtime import meters as runtime_meters
from meridian.runtime import runs
from meridian.runtime.failures import GraphFailure
from meridian.runtime.meters import RuntimeMeters
from meridian.runtime.model_client import ModelCallError, ModelClient
from meridian.runtime.models import RunStatus
from meridian.runtime.settings import RuntimeSettings
from meridian.runtime.tool_client import ToolClient

RUNS = "meridian.runtime.runs"
MODEL_CALLS = "meridian.runtime.model_calls"
IDENTITY = {"meridian.tenant": "claims-triage", "meridian.agent": "claims-triage"}
RUN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000cc")
BROKEN = "the counter broke"
ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"


# ── a meter provider whose counters raise ───────────────────────────────────
class RaisingCounter:
    def add(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError(BROKEN)


class BrokenProvider(MeterProvider):
    """A provider whose every counter raises when it is added to."""

    def get_meter(self, *args: Any, **kwargs: Any) -> Any:
        meter = super().get_meter(*args, **kwargs)
        meter.create_counter = lambda *a, **k: RaisingCounter()
        return meter


def identity() -> runs.RunIdentity:
    return runs.RunIdentity(
        run_id=RUN_ID,
        thread_id=uuid.uuid4(),
        agent="claims-triage",
        tenant="claims-triage",
        reference="CLM-0001",
    )


def counted(reader: InMemoryMetricReader, name: str = RUNS) -> dict[tuple, float]:
    return {
        (a["meridian.outcome"], a.get("meridian.reason")): v
        for a, v in metric_points(reader, name)
    }


# ── the meter never raises into the work it counts ──────────────────────────
def test_no_instrument_of_the_runtime_raises_into_its_caller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    meters = RuntimeMeters(BrokenProvider())

    with caplog.at_level(logging.WARNING, logger=common_metrics.__name__):
        meters.leg_ended(identity(), runs.RunOutcome("Completed", None), None)
        meters.model_call_observer(identity())("completed", None)
        meters.not_started("claims-triage", "claims-triage")

    warnings = [r.getMessage() for r in caplog.records]
    assert [w.split(" in ")[1] for w in warnings] == [
        "RuntimeMeters.leg_ended",
        "RuntimeMeters.model_call_observer.<locals>.observe",
        "RuntimeMeters.not_started",
    ]
    assert all("RuntimeError" in w and BROKEN not in w for w in warnings)


def client_over(
    respond: Callable[[httpx.Request], httpx.Response], observer: Any
) -> ModelClient:
    return ModelClient(
        httpx.Client(
            base_url="http://gateway.invalid", transport=httpx.MockTransport(respond)
        ),
        tenant="claims-triage",
        agent="claims-triage",
        run_id=RUN_ID,
        max_calls=5,
        on_call=observer,
    )


def refuses(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("the transport's own message")


def test_a_broken_counter_does_not_chain_onto_the_transport_error() -> None:
    observer = RuntimeMeters(BrokenProvider()).model_call_observer(identity())
    client = client_over(refuses, observer)

    with pytest.raises(ModelCallError) as raised:
        client.chat([{"role": "user", "content": "hi"}])

    chain: list[BaseException] = []
    link: BaseException | None = raised.value
    while link is not None:
        chain.append(link)
        link = link.__cause__ or link.__context__
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert BROKEN not in " ".join(str(e) for e in chain)


def test_a_broken_counter_does_not_fail_a_call_the_gateway_answered() -> None:
    observer = RuntimeMeters(BrokenProvider()).model_call_observer(identity())
    client = client_over(lambda r: httpx.Response(200, json=GATEWAY_REPLY), observer)

    result = client.chat([{"role": "user", "content": "hi"}])

    assert result.text == "drafted"


# ── the statuses a leg can end on ───────────────────────────────────────────
@pytest.mark.parametrize("status", ["Running", "Failed"])
def test_a_status_execute_never_returns_is_a_failed_leg_not_a_completed_one(
    status: Any,
) -> None:
    reader = InMemoryMetricReader()
    meters = RuntimeMeters(common_metrics.make_meter_provider("agent-runtime", reader))

    meters.leg_ended(identity(), runs.RunOutcome(status, None), None)

    assert counted(reader) == {("failed", "unexpected"): 1}


def test_a_leg_that_could_not_be_saved_is_failed_with_not_saved() -> None:
    reader = InMemoryMetricReader()
    meters = RuntimeMeters(common_metrics.make_meter_provider("agent-runtime", reader))

    meters.leg_ended(identity(), runs.RunOutcome("Completed", None), None, saved=False)
    meters.leg_ended(
        identity(), runs.RunOutcome("Failed", None), GraphFailure("x"), saved=False
    )

    # The reason is the write's, not the leg's own failure.
    assert counted(reader) == {("failed", "not-saved"): 2}


def test_a_run_that_could_not_start_is_counted_with_the_tenant_and_agent_it_named() -> (
    None
):
    reader = InMemoryMetricReader()
    meters = RuntimeMeters(common_metrics.make_meter_provider("agent-runtime", reader))

    meters.not_started("claims-triage", "claims-triage")

    assert metric_points(reader, RUNS) == [
        ({"meridian.outcome": "failed", "meridian.reason": "not-started"} | IDENTITY, 1)
    ]


# ── stand-ins for the graph, the gateway and the runtime's writes ───────────
class State(TypedDict, total=False):
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


def failing_factory(model: ModelClient, tools: ToolClient) -> StateGraph:
    def work(state: State) -> State:
        raise ValueError("boom")

    return graph_of(work)


def make_client(
    dsn: str = "postgresql://agent_runtime@db.invalid/x",
    meters: MeterProvider | None = None,
    saver: bool = True,
) -> TestClient:
    settings = RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url="http://gateway.invalid",
        database_url=dsn,
        tool_servers={},
    )
    gateway = httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=GATEWAY_REPLY)
        ),
    )
    app = runtime_app.create_app(
        settings,
        tracer_provider=make_tracer_provider("agent-runtime", InMemorySpanExporter()),
        meter_provider=meters,
        http_client=gateway,
        checkpointer=MemorySaver() if saver else None,
    )
    return TestClient(app, raise_server_exceptions=False)


def start(client: TestClient) -> httpx.Response:
    return client.post(
        "/runs",
        json={
            "agent": "claims-triage",
            "tenant": "claims-triage",
            "reference": "CLM-0001",
            "input": {},
        },
    )


def resume(client: TestClient) -> httpx.Response:
    return client.post(
        f"/runs/{RUN_ID}/resume",
        json={"tenant": "claims-triage", "reference": "CLM-0001", "input": {}},
    )


def a_run(status: Any) -> RunStatus:
    now = datetime.now(UTC)
    return RunStatus(
        run_id=RUN_ID,
        agent="claims-triage",
        tenant="claims-triage",
        reference="CLM-0001",
        status=status,
        created_at=now,
        updated_at=now,
    )


def paused_run(dsn: str, run_id: uuid.UUID) -> RunStatus:
    return a_run("AwaitingApproval")


def no_database_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime's writes all succeed, on no database."""
    monkeypatch.setattr(runs, "start_run", lambda dsn, identity: identity)
    monkeypatch.setattr(runs, "finish_run", lambda dsn, identity, status, **why: True)


def database_down(*_args: object, **_kwargs: object) -> Any:
    raise psycopg.OperationalError("the database is down")


# ── a leg is counted after its status is written ────────────────────────────
@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (ok_factory, {("completed", None): 1}),
        (failing_factory, {("failed", "unexpected"): 1}),
    ],
    ids=["completed", "failed"],
)
def test_a_leg_whose_status_was_written_is_counted_by_what_it_did(
    monkeypatch: pytest.MonkeyPatch, factory: Callable, expected: dict
) -> None:
    register(monkeypatch, factory)
    no_database_down(monkeypatch)
    reader = InMemoryMetricReader()
    meters = common_metrics.make_meter_provider("agent-runtime", reader)

    start(make_client(meters=meters))

    assert counted(reader) == expected


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (ok_factory, {("completed", None): 1}),
        (failing_factory, {("failed", "unexpected"): 1}),
    ],
    ids=["completed", "failed"],
)
def test_a_leg_the_sweep_ended_meanwhile_still_counts_as_what_the_leg_did(
    monkeypatch: pytest.MonkeyPatch, factory: Callable, expected: dict
) -> None:
    # Its own write moved nothing and the stored status is another's: the
    # answer is the stored one, the count is the leg's own.
    register(monkeypatch, factory)
    no_database_down(monkeypatch)
    monkeypatch.setattr(runs, "finish_run", lambda dsn, identity, status, **why: False)
    monkeypatch.setattr(runs, "fetch_run", lambda dsn, run_id: a_run("Failed"))
    reader = InMemoryMetricReader()
    meters = common_metrics.make_meter_provider("agent-runtime", reader)

    response = start(make_client(meters=meters))

    assert response.json()["status"] == "Failed"
    assert counted(reader) == expected


@pytest.mark.parametrize("factory", [ok_factory, failing_factory], ids=["ok", "failed"])
def test_a_leg_whose_status_could_not_be_written_is_counted_once_as_not_saved(
    monkeypatch: pytest.MonkeyPatch, factory: Callable
) -> None:
    register(monkeypatch, factory)
    no_database_down(monkeypatch)
    monkeypatch.setattr(runs, "finish_run", database_down)
    reader = InMemoryMetricReader()
    meters = common_metrics.make_meter_provider("agent-runtime", reader)

    response = start(make_client(meters=meters))

    assert response.status_code == 503
    assert metric_points(reader, RUNS) == [
        ({"meridian.outcome": "failed", "meridian.reason": "not-saved"} | IDENTITY, 1)
    ]


# ── a run that could not start is counted ───────────────────────────────────
def saver_refused(dsn: str) -> Any:
    raise psycopg.OperationalError("the saver cannot connect")


@pytest.mark.parametrize("step", ["start_run", "saver"])
def test_a_start_the_database_refuses_is_counted_as_not_started(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    node_calls: list[str] = []

    def counting(model: ModelClient, tools: ToolClient) -> StateGraph:
        return graph_of(lambda state: node_calls.append("ran") or {"output": {}})

    register(monkeypatch, counting)
    monkeypatch.setattr(runs, "start_run", database_down)
    monkeypatch.setattr(runtime_app, "open_saver", saver_refused)
    reader = InMemoryMetricReader()
    meters = common_metrics.make_meter_provider("agent-runtime", reader)

    # The saver is injected unless it is the step that fails.
    response = start(make_client(meters=meters, saver=step != "saver"))

    assert response.status_code == 503
    assert node_calls == []
    assert metric_points(reader, RUNS) == [
        ({"meridian.outcome": "failed", "meridian.reason": "not-started"} | IDENTITY, 1)
    ]


@pytest.mark.parametrize("step", ["claim", "saver"])
def test_a_resume_the_database_refuses_is_counted_as_not_started(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    monkeypatch.setattr(runs, "fetch_run", paused_run)
    monkeypatch.setattr(runs, "claim_paused_run", database_down)
    monkeypatch.setattr(runtime_app, "open_saver", saver_refused)
    register(monkeypatch, ok_factory)
    reader = InMemoryMetricReader()
    meters = common_metrics.make_meter_provider("agent-runtime", reader)

    response = resume(make_client(meters=meters, saver=step != "saver"))

    assert response.status_code == 503
    # The tenant and agent are the run's own, which the registry holds: the
    # resume's check ran before the claim.
    assert metric_points(reader, RUNS) == [
        ({"meridian.outcome": "failed", "meridian.reason": "not-started"} | IDENTITY, 1)
    ]


def test_a_resume_another_request_claimed_first_counts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runs, "fetch_run", paused_run)
    monkeypatch.setattr(runs, "claim_paused_run", lambda *args: None)
    register(monkeypatch, ok_factory)
    reader = InMemoryMetricReader()
    meters = common_metrics.make_meter_provider("agent-runtime", reader)

    response = resume(make_client(meters=meters))

    assert response.status_code == 200
    assert metric_points(reader, RUNS) == []


def test_a_run_whose_end_the_saver_cannot_close_is_not_counted_as_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the opening of the run is the start: a saver that fails on its way
    # out, after a leg that ran, must not add a second word to the leg.
    register(monkeypatch, ok_factory)
    no_database_down(monkeypatch)

    class FailsOnExit:
        def __enter__(self) -> MemorySaver:
            return MemorySaver()

        def __exit__(self, *exc: object) -> None:
            raise psycopg.OperationalError("the connection dropped")

    monkeypatch.setattr(runtime_app, "open_saver", lambda dsn: FailsOnExit())
    reader = InMemoryMetricReader()
    meters = common_metrics.make_meter_provider("agent-runtime", reader)

    start(make_client(meters=meters, saver=False))

    assert counted(reader) == {("completed", None): 1}


# ── a broken counter does not stop a leg from settling ──────────────────────
@pytest.mark.parametrize(
    ("factory", "status_code", "stored"),
    [(ok_factory, 200, "Completed"), (failing_factory, 502, "Failed")],
    ids=["completed", "failed"],
)
def test_a_leg_settles_when_the_counters_raise(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    factory: Callable,
    status_code: int,
    stored: str,
) -> None:
    register(monkeypatch, factory)
    client = make_client(fresh_database.dsn("agent_runtime"), meters=BrokenProvider())

    with caplog.at_level(logging.WARNING, logger=common_metrics.__name__):
        response = start(client)

    body = response.json()
    assert (response.status_code, body["status"]) == (status_code, stored)
    rows = owner_rows(fresh_database, "SELECT run_id, status FROM runtime.runs")
    assert [(str(run_id), status) for run_id, status in rows] == [
        (body["run_id"], stored)
    ]
    events = audit_events(fresh_database, uuid.UUID(body["run_id"]))
    assert [e["event"] for e in events] == [
        "run.started",
        "run.completed" if stored == "Completed" else "run.failed",
    ]
    assert any("RuntimeMeters.leg_ended" in r.getMessage() for r in caplog.records)
    assert BROKEN not in caplog.text
    if factory is ok_factory:
        assert body["output"] == {"text": "drafted"}


# ── the shutdown of the meter provider ──────────────────────────────────────
class FailingShutdown(MeterProvider):
    """Fails the app's shutdown; the SDK's exit hook finds it shut down."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = True

    def shutdown(self, *args: Any, **kwargs: Any) -> None:
        if self.armed:
            self.armed = False
            raise RuntimeError(BROKEN)
        super().shutdown(*args, **kwargs)


class TracerSpy(TracerProvider):
    def __init__(self) -> None:
        super().__init__()
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1
        super().shutdown()


def test_a_meter_provider_that_fails_to_shut_down_is_a_warning_and_stops_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tracer = TracerSpy()
    failing = FailingShutdown()
    monkeypatch.setattr(runtime_app, "make_meter_provider", lambda _name: failing)
    monkeypatch.setattr(common_http, "make_tracer_provider", lambda _name: tracer)
    settings = RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url="http://gateway.invalid",
        database_url="postgresql://agent_runtime@db.invalid/x",
        tool_servers={},
    )

    with (
        caplog.at_level(logging.WARNING, logger=runtime_meters.__name__),
        TestClient(runtime_app.create_app(settings)),
    ):
        pass

    assert tracer.shutdowns == 1
    (warning,) = [r for r in caplog.records if r.name == runtime_meters.__name__]
    assert "RuntimeError" in warning.getMessage()
    assert BROKEN not in caplog.text


# ── the production path: no injected provider, the collector's address set ──
def test_an_app_that_builds_its_own_provider_exports_its_runs_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exported: list[str] = []

    def export(self: object, metrics_data: Any, *args: object, **kwargs: object) -> Any:
        exported.extend(
            metric.name
            for resource in metrics_data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        )
        return MetricExportResult.SUCCESS

    monkeypatch.setenv(ENDPOINT, "http://127.0.0.1:4318")
    monkeypatch.setattr(common_metrics.OTLPMetricExporter, "export", export)
    register(monkeypatch, ok_factory)
    no_database_down(monkeypatch)

    with make_client() as client:
        assert start(client).status_code == 200

    assert MODEL_CALLS in exported
    assert RUNS in exported
