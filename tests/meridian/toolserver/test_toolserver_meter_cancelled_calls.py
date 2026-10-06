"""The tool servers' metrics (S064): a call that is cancelled is counted.

A call cancelled while it waits for a slot is one failure with the word
``cancelled``; a call cancelled while its thread runs is counted by what the
thread did. The constants and helpers are in ``metersupport``, the ``world``
fixture in the directory's ``conftest.py``.
"""

import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import anyio
import mcp_types as types
import psycopg
from metersupport import (
    CANCELLED,
    LOOKUP,
    RUN_LABELS,
    TOOL,
    failing_with,
    points,
    policy_over,
    series,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from toolsupport import World, worker_holding

from meridian.platform.toolserver import server as server_module
from meridian.platform.toolserver.handlers import Completed, ToolCall, ToolHandler
from meridian.platform.toolserver.wire import META_RUN, META_WORKER

# ── a call that is cancelled is counted (S064) ──────────────────────────────
SLOTS = server_module.MAX_CONCURRENT_CALLS
WAIT_SECONDS = 30
PARAMS = types.CallToolRequestParams(name=TOOL, arguments=LOOKUP)


class Held:
    """Handlers that hold their slot until ``gate`` is set. ``first`` is set
    when one is inside, ``full`` when as many are as the server has slots."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entered = 0
        self.gate = threading.Event()
        self.first = threading.Event()
        self.full = threading.Event()

    def run(self, conn: psycopg.Connection, call: ToolCall) -> Completed:
        with self.lock:
            self.entered += 1
            self.first.set()
            if self.entered >= SLOTS:
                self.full.set()
        assert self.gate.wait(timeout=WAIT_SECONDS)
        return Completed({"found": False})

    def handlers(self) -> list[ToolHandler]:
        others = failing_with(RuntimeError("not called"))[1:]
        return [
            ToolHandler(
                tool=TOOL,
                scope="policy:read",
                bound_argument="policy_number",
                bound_to="policy_number",
                run=self.run,
            ),
            *others,
        ]


def tools_call(server: Any) -> Callable[..., Any]:
    """The server's own ``tools/call`` handler, the one the SDK runs, so that a
    test can cancel the task that runs it."""
    entry = server.get_request_handler("tools/call")
    assert entry is not None
    return entry.handler


def call_context(world: World) -> Any:
    meta = {META_RUN: str(world.run_id), META_WORKER: worker_holding(TOOL)}
    return SimpleNamespace(meta=meta)


async def until(event: threading.Event) -> None:
    with anyio.fail_after(WAIT_SECONDS):
        assert await anyio.to_thread.run_sync(event.wait, WAIT_SECONDS)


def test_a_call_cancelled_while_it_waits_for_a_slot_is_one_cancelled_failure(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    held = Held()
    handle = tools_call(policy_over(world, reader, held.handlers()))

    async def drive() -> None:
        async with anyio.create_task_group() as holders:
            for _ in range(SLOTS):
                holders.start_soon(handle, call_context(world), PARAMS)
            await until(held.full)
            async with anyio.create_task_group() as ninth:
                ninth.start_soon(handle, call_context(world), PARAMS)
                # Every other task is in a thread or waiting: the ninth is
                # waiting for a slot, none being free.
                await anyio.wait_all_tasks_blocked()
                ninth.cancel_scope.cancel()
            held.gate.set()

    anyio.run(drive)

    completed = {"meridian.tool": TOOL, "meridian.outcome": "completed"}
    cancelled = {"meridian.outcome": "failed", "meridian.reason": CANCELLED}
    assert held.entered == SLOTS
    assert points(reader) == {
        series(**completed, **RUN_LABELS): SLOTS,
        series(**cancelled): 1,
    }


def test_a_call_cancelled_while_its_thread_runs_is_counted_by_what_the_thread_did(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    held = Held()
    handle = tools_call(policy_over(world, reader, held.handlers()))

    async def drive() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(handle, call_context(world), PARAMS)
            await until(held.first)
            group.cancel_scope.cancel()
            held.gate.set()

    anyio.run(drive)

    completed = {"meridian.tool": TOOL, "meridian.outcome": "completed"}
    assert points(reader) == {series(**completed, **RUN_LABELS): 1}
