"""The refusal audit of the tool servers (S069, T-49): the count of a flood's
last window, a shed call's run, and the word of a call that ended with no result.

A tool server throttles its refusal rows, so a flood leaves one per window and
counts the rest. What the last window suppressed is carried by no row, so a
``suppressed`` row is written once the flood has been quiet for two windows,
with the next call (in the call's own worker thread: never on the event loop)
and when the server closes. A shed call's row names its run when the run can be
checked in the row's own thread. The real policy server over the real audit
table on the throwaway PostgreSQL (``make pytest-db``).
"""

import logging
import threading
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import mcp_types as types
import psycopg
import pytest
from callersupport import CALLER_HEADER, PREFIX, as_caller_named_by_header
from dbsupport import DatabaseHandle
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from servicesupport import REGISTRY_DIR, FakeClock, metric_points
from starlette.testclient import TestClient
from toolsupport import (
    CANARY,
    HOSTS,
    POLICY,
    World,
    add_run,
    audit_rows,
    run_call,
    seed_world,
    settings_for,
    with_client,
)

from meridian.platform.common import audit
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.throttle import (
    REFUSAL_AUDIT_SECONDS,
    REFUSAL_SUMMARY_SECONDS,
    RefusalAuditThrottle,
)
from meridian.platform.policy_mcp.app import HANDLERS, create_app
from meridian.platform.registry import load_registry
from meridian.platform.toolserver import pipeline as pipeline_module
from meridian.platform.toolserver import server as server_module
from meridian.platform.toolserver.meters import CALLS
from meridian.platform.toolserver.pipeline import Call, Pipeline, build_entries
from meridian.platform.toolserver.settings import ToolServerSettings
from meridian.platform.toolserver.wire import META_RUN, META_WORKER

SERVER = "policy-mcp"
TOOL = "policy_lookup"
LOOKUP = {"policy_number": POLICY}
KEY = f"{TOOL}/unknown-run"
FLOOD = 4
SUPPRESSED_BY_THE_FLOOD = FLOOD - 1
EXCEPTION_TEXT = "the-text-of-the-exception"
CALLER_KEY = "claims-api/caller-not-allowed"


@pytest.fixture
def world(fresh_database: DatabaseHandle) -> World:
    return seed_world(fresh_database)


def summaries(world: World) -> list[dict[str, Any]]:
    return [row for row in audit_rows(world.db) if row["outcome"] == "suppressed"]


def refused(world: World) -> list[dict[str, Any]]:
    return [row for row in audit_rows(world.db) if row["outcome"] == "refused"]


def a_server(world: World, clock: FakeClock, **kwargs: Any) -> Any:
    return create_app(settings_for(world.db, "policy_mcp"), clock=clock, **kwargs)


def an_unknown_run(server: Any) -> None:
    """A call of a run that is not there: refused, one row a window."""
    result = run_call(server, TOOL, LOOKUP, run_id=uuid.uuid4())
    assert result.is_error


def a_good_call(server: Any, world: World) -> None:
    result = run_call(server, TOOL, LOOKUP, run_id=world.run_id)
    assert not result.is_error


def flood(server: Any, times: int = FLOOD) -> None:
    for _ in range(times):
        an_unknown_run(server)


# ── the count of a flood's last window ──────────────────────────────────────
def test_the_count_of_a_flood_is_written_with_the_next_call_once_it_is_quiet(
    world: World,
) -> None:
    clock = FakeClock()
    server = a_server(world, clock).server
    flood(server)
    assert [row["suppressed"] for row in refused(world)] == [0]
    assert summaries(world) == []
    clock.advance(REFUSAL_SUMMARY_SECONDS)

    a_good_call(server, world)

    # The event the tool server's rows use, the key as the reason, and nothing
    # of a run, a call, an agent or a reference: it counts calls of many runs.
    (row,) = summaries(world)
    assert (row["service"], row["event"], row["tenant"]) == (SERVER, "tool.call", None)
    assert (row["reason"], row["suppressed"]) == (KEY, SUPPRESSED_BY_THE_FLOOD)
    assert (row["run_id"], row["agent"], row["reference"], row["call_id"]) == (
        None,
        None,
        None,
        None,
    )


def test_a_summary_waits_until_the_flood_has_been_quiet_for_two_windows(
    world: World,
) -> None:
    clock = FakeClock()
    server = a_server(world, clock).server
    flood(server)
    clock.advance(REFUSAL_SUMMARY_SECONDS - 1)

    a_good_call(server, world)

    assert summaries(world) == []
    clock.advance(1)
    a_good_call(server, world)
    assert [row["suppressed"] for row in summaries(world)] == [SUPPRESSED_BY_THE_FLOOD]


def test_a_summary_is_written_once(world: World) -> None:
    clock = FakeClock()
    server = a_server(world, clock).server
    flood(server)
    clock.advance(REFUSAL_SUMMARY_SECONDS)

    a_good_call(server, world)
    a_good_call(server, world)
    clock.advance(REFUSAL_SUMMARY_SECONDS)
    a_good_call(server, world)

    assert len(summaries(world)) == 1


def test_a_flood_that_goes_on_has_its_count_in_its_own_row_and_no_summary(
    world: World,
) -> None:
    clock = FakeClock()
    server = a_server(world, clock).server
    flood(server)
    clock.advance(REFUSAL_AUDIT_SECONDS)
    flood(server, 1)  # a window later: its row carries the count
    clock.advance(REFUSAL_SUMMARY_SECONDS)

    a_good_call(server, world)

    assert [row["suppressed"] for row in refused(world)] == [0, SUPPRESSED_BY_THE_FLOOD]
    assert summaries(world) == []


@pytest.fixture
def written_in(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """The thread of every summary row's write."""
    threads: list[int] = []
    real = pipeline_module.write_audit

    def recording(dsn: str, event: Any) -> None:
        if event.outcome == "suppressed":
            threads.append(threading.get_ident())
        real(dsn, event)

    monkeypatch.setattr(pipeline_module, "write_audit", recording)
    yield threads


def test_the_summary_of_a_call_is_written_in_a_thread_and_not_on_the_event_loop(
    world: World, written_in: list[int]
) -> None:
    clock = FakeClock()
    server = a_server(world, clock).server
    flood(server)
    clock.advance(REFUSAL_SUMMARY_SECONDS)
    loop_thread: list[int] = []

    async def drive(client: Any) -> None:
        loop_thread.append(threading.get_ident())
        meta = {META_RUN: str(world.run_id), META_WORKER: "intake"}
        await client.call_tool(TOOL, LOOKUP, meta=meta)

    with_client(server, drive)

    assert len(written_in) == 1
    assert written_in != loop_thread


def test_closing_the_app_writes_the_count_of_a_window_that_has_not_ended(
    world: World,
) -> None:
    clock = FakeClock()
    app = a_server(world, clock)
    flood(app.server)
    clock.advance(REFUSAL_AUDIT_SECONDS / 2)
    assert summaries(world) == []

    async def serve_and_stop() -> None:
        async with app.app.router.lifespan_context(app.app):
            pass

    anyio.run(serve_and_stop)

    assert [(row["reason"], row["suppressed"]) for row in summaries(world)] == [
        (KEY, SUPPRESSED_BY_THE_FLOOD)
    ]


def test_closing_the_app_writes_the_summary_in_a_thread_and_not_on_the_event_loop(
    world: World, written_in: list[int]
) -> None:
    clock = FakeClock()
    app = a_server(world, clock)
    flood(app.server)
    loop_thread: list[int] = []

    async def serve_and_stop() -> None:
        async with app.app.router.lifespan_context(app.app):
            loop_thread.append(threading.get_ident())

    anyio.run(serve_and_stop)

    assert len(written_in) == 1
    assert written_in != loop_thread


def test_closing_the_app_writes_nothing_when_nothing_was_suppressed(
    world: World,
) -> None:
    app = a_server(world, FakeClock())
    an_unknown_run(app.server)

    async def serve_and_stop() -> None:
        async with app.app.router.lifespan_context(app.app):
            pass

    anyio.run(serve_and_stop)

    assert summaries(world) == []


@pytest.fixture
def summary_write_fails(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bool]]:
    """Make the audit write raise for a ``suppressed`` row only, while the
    returned list holds a true value."""
    failing = [True]
    real = audit.write_audit

    def write(dsn: str, event: audit.AuditEvent) -> None:
        if failing and event.outcome == "suppressed":
            raise RuntimeError(EXCEPTION_TEXT)
        real(dsn, event)

    monkeypatch.setattr(pipeline_module, "write_audit", write)
    yield failing


def test_a_summary_that_cannot_be_written_leaves_the_call_and_is_tried_later(
    world: World,
    summary_write_fails: list[bool],
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    server = a_server(world, clock).server
    flood(server)
    clock.advance(REFUSAL_SUMMARY_SECONDS)

    with caplog.at_level(logging.WARNING):
        a_good_call(server, world)  # answered: the summary is not its business

    assert summaries(world) == []
    assert "RuntimeError" in caplog.text
    assert EXCEPTION_TEXT not in caplog.text
    summary_write_fails.clear()  # the database is back
    a_good_call(server, world)  # inside the two windows after the failure
    assert summaries(world) == []
    clock.advance(REFUSAL_SUMMARY_SECONDS)

    a_good_call(server, world)

    assert [row["suppressed"] for row in summaries(world)] == [SUPPRESSED_BY_THE_FLOOD]


def test_a_caller_check_flood_is_summarised_when_the_server_closes(
    world: World,
) -> None:
    clock = FakeClock()
    settings = ToolServerSettings(
        registry_dir=REGISTRY_DIR,
        database_url=world.db.dsn("policy_mcp"),
        allowed_hosts=HOSTS,
        identity_prefix=PREFIX,
    )
    app = create_app(settings, clock=clock)
    client = TestClient(
        as_caller_named_by_header(app.app),
        base_url=f"http://{HOSTS[0]}",
        raise_server_exceptions=False,
    )

    with client:
        for _ in range(FLOOD):
            response = client.post(
                "/mcp", json={}, headers={CALLER_HEADER: "claims-api"}
            )
            assert response.status_code == httpx.codes.FORBIDDEN

    # The key is the caller the registry maps and the reason; no tenant.
    assert [
        (row["event"], row["tenant"], row["reason"], row["suppressed"])
        for row in summaries(world)
    ] == [("tool.call", None, CALLER_KEY, SUPPRESSED_BY_THE_FLOOD)]


# ── a shed call's row names its run where that can be checked (R8) ──────────
def pipeline_of(world: World, clock: FakeClock | None = None) -> Pipeline:
    registry = load_registry(REGISTRY_DIR)
    return Pipeline(
        dsn=settings_for(world.db, "policy_mcp").database_url,
        registry=registry,
        service_name=SERVER,
        entries=build_entries(registry, SERVER, HANDLERS),
        throttle=RefusalAuditThrottle(clock or FakeClock()),
    )


def timed_out(world: World) -> list[dict[str, Any]]:
    return [row for row in audit_rows(world.db) if row["reason"] == "timed-out"]


def shed(pipeline: Pipeline, run_id: uuid.UUID | None) -> Call:
    call = Call(uuid.uuid4(), TOOL)
    write = pipeline.timed_out_row(call, run_id)
    assert write is not None
    write()
    return call


def test_a_shed_call_of_a_run_that_exists_is_named_by_the_run_rows_own_values(
    world: World,
) -> None:
    run_id = add_run(world.db, tenant="evaluation", agent="claims-triage")
    pipeline = pipeline_of(world)

    call = shed(pipeline, run_id)

    (row,) = timed_out(world)
    assert (row["run_id"], row["tenant"], row["agent"], row["reference"]) == (
        run_id,
        "evaluation",
        "claims-triage",
        "CLM-0001",
    )
    assert (row["tool"], row["suppressed"], row["call_id"]) == (TOOL, 0, call.call_id)
    # The span reads the call: it follows the row.
    assert (call.run_id, call.tenant, call.agent) == (
        run_id,
        "evaluation",
        "claims-triage",
    )


def test_a_shed_call_of_a_run_that_is_not_there_is_not_named(world: World) -> None:
    call = shed(pipeline_of(world), uuid.uuid4())

    (row,) = timed_out(world)
    assert (row["run_id"], row["tenant"], row["agent"], row["reference"]) == (None,) * 4
    assert call.run_id is None


def test_a_shed_call_with_no_well_formed_run_id_reads_nothing_and_is_not_named(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_connection(*args: object, **kwargs: object) -> None:
        raise AssertionError("no run to read")

    monkeypatch.setattr(pipeline_module, "connect", no_connection)

    shed(pipeline_of(world), None)

    (row,) = timed_out(world)
    assert (row["run_id"], row["tenant"]) == (None, None)


def test_a_shed_row_that_stands_for_other_calls_is_not_named_whatever_the_run(
    world: World,
) -> None:
    clock = FakeClock()
    pipeline = pipeline_of(world, clock)
    shed(pipeline, None)
    for _ in range(2):  # two more in the window: counted, no row
        assert pipeline.timed_out_row(Call(uuid.uuid4(), TOOL), world.run_id) is None
    clock.advance(REFUSAL_AUDIT_SECONDS)

    shed(pipeline, world.run_id)  # the run exists, and the row stands for two more

    first, second = timed_out(world)
    assert (first["suppressed"], second["suppressed"]) == (0, 2)
    assert (second["run_id"], second["tenant"], second["agent"]) == (None, None, None)


def test_a_shed_call_whose_run_cannot_be_read_still_writes_its_row(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuses(*args: object, **kwargs: object) -> None:
        raise psycopg.OperationalError(CANARY)

    monkeypatch.setattr(pipeline_module, "connect", refuses)

    with caplog.at_level(logging.WARNING):
        call = shed(pipeline_of(world), world.run_id)

    (row,) = timed_out(world)
    assert (row["run_id"], row["tenant"], row["suppressed"]) == (None, None, 0)
    assert call.run_id is None
    assert "OperationalError" in caplog.text
    assert CANARY not in caplog.text


def test_the_run_of_a_shed_call_is_read_in_a_thread_and_not_on_the_event_loop(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_in: list[int] = []
    real = pipeline_module.connect

    def recording(dsn: str, application_name: str) -> psycopg.Connection:
        read_in.append(threading.get_ident())
        return real(dsn, application_name)

    monkeypatch.setattr(pipeline_module, "connect", recording)
    pipeline = pipeline_of(world)

    async def drive() -> tuple[int, list[int]]:
        write = pipeline.timed_out_row(Call(uuid.uuid4(), TOOL), world.run_id)
        assert write is not None
        await anyio.to_thread.run_sync(write)
        return threading.get_ident(), read_in

    loop_thread, threads = anyio.run(drive)

    assert len(threads) == 1
    assert threads != [loop_thread]


# ── a call that ends with no result is counted as what it was (B16) ─────────
PARAMS = types.CallToolRequestParams(name=TOOL, arguments=LOOKUP)


class Stop(BaseException):
    """What no ``except Exception`` catches, and is not a cancellation."""


def a_call_whose_runner_raises(
    world: World, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> InMemoryMetricReader:
    reader = InMemoryMetricReader()
    app = create_app(
        settings_for(world.db, "policy_mcp"),
        meter_provider=make_meter_provider(SERVER, reader),
    )
    entry = app.server.get_request_handler("tools/call")
    assert entry is not None

    async def runner_raises(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(server_module._CallRunner, "run", runner_raises)
    meta = {META_RUN: str(world.run_id)}

    with pytest.raises(type(error)):
        anyio.run(entry.handler, SimpleNamespace(meta=meta), PARAMS)
    return reader


@pytest.mark.parametrize("error", [RuntimeError("boom"), Stop()], ids=["error", "stop"])
def test_a_call_that_exits_with_no_result_and_was_not_cancelled_is_unexpected(
    world: World, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    reader = a_call_whose_runner_raises(world, monkeypatch, error)

    points = metric_points(reader, CALLS)

    assert points == [
        ({"meridian.outcome": "failed", "meridian.reason": "unexpected"}, 1)
    ]
