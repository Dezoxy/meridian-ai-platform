"""The tool servers' metrics (S064): real calls through the kit over PostgreSQL
(``make pytest-db``), each counted once, by tool, outcome and reason.

The meter on calls built by hand is in ``test_toolserver_meter_by_hand.py``. The
constants and helpers they share are in ``metersupport``, the ``world`` fixture
in the directory's ``conftest.py``.
"""

import uuid
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from metersupport import (
    HANDLER_FAILURES,
    LOOKUP,
    RUN_LABELS,
    TOOL,
    failing_with,
    points,
    policy_over,
    series,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from servicesupport import metric_points
from toolsupport import (
    CANARY,
    CLAIM,
    KEY,
    TENANT,
    World,
    add_run,
    run_call,
    settings_for,
    worker_holding,
)

from meridian.platform.common import metrics as common_metrics
from meridian.platform.toolserver.handlers import (
    Refused,
    ToolFailed,
    ToolFailedReason,
    ToolHandler,
)
from meridian.platform.toolserver.meters import CALLS, UNLISTED
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app as create_claims_app,
)

NOTE = {"claim_id": CLAIM, "note": "Phone call with the claimant."}


# ── real calls through the kit ──────────────────────────────────────────────
def test_a_completed_call_is_one_completed_count_with_its_tool_tenant_and_agent(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, None)

    run_call(server, TOOL, LOOKUP, run_id=world.run_id)

    completed = {"meridian.tool": TOOL, "meridian.outcome": "completed"}
    assert points(reader) == {series(**completed, **RUN_LABELS): 1}


def test_the_same_write_twice_is_a_completed_count_and_a_replayed_one(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    provider = common_metrics.make_meter_provider("claims-mcp", reader)
    server = create_claims_app(
        settings_for(world.db, "claims_mcp"), meter_provider=provider
    ).server

    run_call(server, "add_claim_note", NOTE, run_id=world.run_id, key=KEY)
    run_call(server, "add_claim_note", NOTE, run_id=world.run_id, key=KEY)

    tool = {"meridian.tool": "add_claim_note"} | RUN_LABELS
    assert points(reader) == {
        series(**tool, **{"meridian.outcome": "completed"}): 1,
        series(**tool, **{"meridian.outcome": "replayed"}): 1,
    }


def test_a_refusal_after_the_run_was_read_has_the_run_rows_tenant_and_agent(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, None)

    run_call(server, TOOL, {"policy_number": "POL-0050"}, run_id=world.run_id)

    assert points(reader) == {
        series(
            **{
                "meridian.tool": TOOL,
                "meridian.outcome": "refused",
                "meridian.reason": "outside-claim",
            },
            **RUN_LABELS,
        ): 1
    }


@pytest.mark.parametrize("run", ["none", "not-a-uuid", "no-such-run"])
def test_a_refusal_for_a_run_that_is_not_found_has_neither_tenant_nor_agent(
    world: World, run: str
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, None)
    run_id = {"none": None, "not-a-uuid": "not-a-uuid", "no-such-run": uuid.uuid4()}[
        run
    ]

    run_call(server, TOOL, LOOKUP, run_id=run_id)

    assert points(reader) == {
        series(
            **{
                "meridian.tool": TOOL,
                "meridian.outcome": "refused",
                "meridian.reason": "unknown-run",
            }
        ): 1
    }


def test_a_tool_the_server_does_not_have_is_counted_with_no_trace_of_its_name(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, None)

    run_call(server, f"{CANARY}-tool", {"policy_number": CANARY}, run_id=world.run_id)
    run_call(server, "add_claim_note", NOTE, run_id=world.run_id)  # another server's

    assert points(reader) == {
        series(**{"meridian.outcome": "refused", "meridian.reason": "unknown-tool"}): 2
    }
    assert CANARY not in repr(metric_points(reader, CALLS))


def test_a_call_that_names_a_worker_is_counted_and_the_worker_is_no_label(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, None)

    run_call(server, TOOL, LOOKUP, run_id=world.run_id, worker=worker_holding(TOOL))

    ((attributes, _),) = metric_points(reader, CALLS)
    assert set(attributes) == {
        "meridian.tool",
        "meridian.outcome",
        "meridian.tenant",
        "meridian.agent",
    }
    assert str(worker_holding(TOOL)) not in repr(attributes)


def the_call_fails(server: Any, world: World) -> None:
    with pytest.raises(MCPError):
        run_call(server, TOOL, LOOKUP, run_id=world.run_id)


@pytest.mark.parametrize("word", sorted(HANDLER_FAILURES))
def test_a_handler_that_fails_with_a_word_of_the_set_is_counted_as_that_word(
    world: World, word: ToolFailedReason
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, failing_with(ToolFailed(word)))

    the_call_fails(server, world)

    assert points(reader) == {
        series(
            **{
                "meridian.tool": TOOL,
                "meridian.outcome": "failed",
                "meridian.reason": word,
            },
            **RUN_LABELS,
        ): 1
    }


def test_a_handler_that_fails_with_text_of_its_own_is_the_unlisted_word(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    error = ToolFailed(f"made up {CANARY}")  # type: ignore[arg-type]
    server = policy_over(world, reader, failing_with(error))

    the_call_fails(server, world)

    # The exception's own ``reason`` is text a handler chose; the kit audits it
    # as it came, and the meter does not carry it.
    ((attributes, _),) = metric_points(reader, CALLS)
    assert attributes["meridian.reason"] == UNLISTED
    assert CANARY not in repr(attributes)


def test_a_handler_that_raises_something_else_is_the_unexpected_word(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, failing_with(RuntimeError(CANARY)))

    the_call_fails(server, world)

    ((attributes, _),) = metric_points(reader, CALLS)
    assert (attributes["meridian.outcome"], attributes["meridian.reason"]) == (
        "failed",
        "unexpected",
    )
    assert CANARY not in repr(attributes)


def test_a_handler_that_refuses_with_text_of_its_own_is_the_unlisted_word(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    handlers = failing_with(RuntimeError("replaced below"))
    handlers[0] = ToolHandler(
        tool=TOOL,
        scope="policy:read",
        bound_argument="policy_number",
        bound_to="policy_number",
        run=lambda conn, call: Refused(f"made up {CANARY}"),  # type: ignore[arg-type]
    )
    server = policy_over(world, reader, handlers)

    run_call(server, TOOL, LOOKUP, run_id=world.run_id)

    ((attributes, _),) = metric_points(reader, CALLS)
    assert (attributes["meridian.outcome"], attributes["meridian.reason"]) == (
        "refused",
        UNLISTED,
    )
    assert CANARY not in repr(attributes)


def test_a_run_that_is_not_running_is_refused_under_its_own_tenant_and_agent(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, None)
    done = add_run(world.db, status="Completed")

    run_call(server, TOOL, LOOKUP, run_id=done)

    ((attributes, _),) = metric_points(reader, CALLS)
    assert attributes["meridian.reason"] == "run-not-running"
    assert attributes["meridian.tenant"] == TENANT
