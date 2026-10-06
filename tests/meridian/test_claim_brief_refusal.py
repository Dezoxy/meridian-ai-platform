"""A step of ``claim-brief`` that calls a tool the agent does not list (S037, F3w:
the boundary review's L3).

Through the real wiring: the Claims API, the Agent Runtime started with the
registry's own ``claim-brief`` (second host, five tools, no ``wording_search``),
its tool client and its audit, and the three tool servers. Only the workflow is a
stand-in: a step that does what a mistaken or hostile workload would do, and
asks for ``wording_search``, a tool of the registry that the triage lists and the
brief does not. The tool client refuses it before anything is sent, the runtime
audits the refusal, the run fails and no tool server is asked.
"""

import uuid
from typing import Never

import pytest
from agent_framework import Executor, WorkflowContext, handler
from dbsupport import DatabaseHandle
from runtimesupport import FakeEntryPoint, register_agents
from servicesupport import audit_events
from stacksupport import Stack, build_stack
from test_claim_brief_stack import (
    BRIEF_URL,
    Models,
    runs,
    triage_paused,
)
from test_claim_brief_stack import models as models  # a fixture: pytest finds it here

from meridian.runtime.agent_framework_host import WorkflowDefinition
from meridian.runtime.hosts import AsyncModelClient, AsyncToolClient
from meridian.workloads.claims_triage.briefs import BRIEF_FAILED_DETAIL

UNLISTED_TOOL = "wording_search"


class Rogue(Executor):
    """The one step: it asks for a tool the agent does not list."""

    def __init__(self, tools: AsyncToolClient) -> None:
        super().__init__(id="gather")
        self._tools = tools

    @handler
    async def gather(self, run_input: dict, ctx: WorkflowContext[Never, dict]) -> None:
        await self._tools.call(UNLISTED_TOOL, {"claim_id": "CLM-0011"})
        await ctx.yield_output({"brief": "never reached"})


def rogue_factory(
    model: AsyncModelClient, tools: AsyncToolClient
) -> WorkflowDefinition:
    return WorkflowDefinition(start=Rogue(tools), edges=())


@pytest.fixture
def stack(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle, models: Models
) -> Stack:
    entry = FakeEntryPoint(
        rogue_factory, "claim-brief", module="meridian.workloads.claim_brief.workflow"
    )
    register_agents(monkeypatch, entry)
    return build_stack(fresh_database, runtime_http=models.http())


def only_brief_run(db: DatabaseHandle) -> uuid.UUID:
    (run_id,) = [r for r, (agent, _) in runs(db).items() if agent == "claim-brief"]
    return run_id


def test_a_step_that_calls_an_unlisted_tool_is_refused_audited_and_asks_no_server(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    triage_paused(stack)

    response = stack.client.post(BRIEF_URL, json={})

    run_id = only_brief_run(fresh_database)
    events = audit_events(fresh_database, run_id)
    tool_calls = [e for e in events if e["event"] == "tool.call"]
    assert response.status_code == 502
    assert response.json()["detail"] == BRIEF_FAILED_DETAIL
    assert runs(fresh_database)[run_id] == ("claim-brief", "Failed")
    # One refusal row, the runtime's own, naming the tool and the agent; the
    # tool servers wrote no row for this run, so none was asked.
    assert [
        (e["service"], e["outcome"], e["reason"], e["tool"], e["agent"])
        for e in tool_calls
    ] == [
        ("agent-runtime", "refused", "tool-not-allowed", UNLISTED_TOOL, "claim-brief")
    ]
    assert {e["service"] for e in events} <= {"agent-runtime", "claims-api"}


def test_the_service_starts_with_the_stand_in_and_the_triage_beside_it_still_pauses(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    # The control: the refusal above is the allowlist's and not a loading
    # failure, because the stand-in started (the service started with it) and the
    # triage beside it still runs.
    triage_run = triage_paused(stack)

    assert runs(fresh_database) == {triage_run: ("claims-triage", "AwaitingApproval")}
