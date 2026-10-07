"""Shared by the tests of the tool servers' meter (S064), in
``tests/meridian/toolserver/test_toolserver_meter_*.py``: the constants, the
calls built by hand, the policy server over a meter provider of the test's own,
and the series read back from it."""

import uuid
from typing import Any

import psycopg
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from servicesupport import metric_points
from toolsupport import AGENT, CLAIM, POLICY, TENANT, World, settings_for

from meridian.platform.common import metrics as common_metrics
from meridian.platform.policy_mcp.app import create_app as create_policy_app
from meridian.platform.toolserver.handlers import Completed, ToolCall, ToolHandler
from meridian.platform.toolserver.meters import CALLS
from meridian.platform.toolserver.pipeline import Call
from meridian.platform.toolserver.server import create_tool_app

TOOL = "policy_lookup"
SERVER_TOOLS = frozenset({"policy_lookup", "claim_history"})
LOOKUP = {"policy_number": POLICY}
# The words the kit itself fails a call with, and the one a handler may add.
KIT_FAILURES = {"invalid-result", "database-unavailable", "unexpected"}
HANDLER_FAILURES = {"gateway-unavailable", "timed-out"}
# The word of a call whose caller gave up before it ran (the server's own, in
# ``on_call_tool``; the pipeline never fails a call with it).
CANCELLED = "cancelled"


def a_call(
    tool: str | None = TOOL, tenant: str | None = TENANT, agent: str | None = AGENT
) -> Call:
    call = Call(uuid.uuid4(), tool=tool)
    if tenant is not None or agent is not None:
        call.read(uuid.uuid4(), tenant, agent, CLAIM)
    return call


def policy_over(
    world: World, reader: InMemoryMetricReader, handlers: list[ToolHandler] | None
) -> Any:
    """The policy server's SDK ``Server``, its meter provider on ``reader``;
    with ``handlers`` the kit is built over them instead of the real two."""
    provider = common_metrics.make_meter_provider("policy-mcp", reader)
    settings = settings_for(world.db, "policy_mcp")
    if handlers is None:
        return create_policy_app(settings, meter_provider=provider).server
    return create_tool_app(
        settings,
        server_id="policy-mcp",
        service_name="policy-mcp",
        handlers=handlers,
        meter_provider=provider,
    ).server


def points(reader: InMemoryMetricReader) -> dict[tuple[tuple[str, str], ...], float]:
    return {
        tuple(sorted((key, str(value)) for key, value in attributes.items())): total
        for attributes, total in metric_points(reader, CALLS)
    }


def series(**labels: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


RUN_LABELS = {"meridian.tenant": TENANT, "meridian.agent": AGENT}


def failing_with(error: Exception) -> list[ToolHandler]:
    def run(conn: psycopg.Connection, call: ToolCall) -> Completed:
        raise error

    return [
        ToolHandler(
            tool=TOOL,
            scope="policy:read",
            bound_argument="policy_number",
            bound_to="policy_number",
            run=run,
        ),
        ToolHandler(
            tool="claim_history",
            scope="claims:history:read",
            bound_argument="policy_number",
            bound_to="policy_number",
            run=lambda conn, call: Completed({"entries": [], "truncated": False}),
        ),
    ]
