"""Support for the tests of the claim-brief workload (S037, W1a).

The workload runs through the second host (``AgentFrameworkHost``) against
PostgreSQL, the real policy and claims tool servers and a stub gateway, all of
which ``hostsupport`` builds from a scratch registry that plants ``claim-brief``.
This module adds what the brief's tests need on top: the input the Claims
Triage App sends, a decision row, the three stand-ins for the tool client (a
real client that fails once after a write, one that refuses a named tool, and
one that answers from a script, so that a test can plant text in a result the
real servers would not return) and small readers of the tables a leg writes.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from dbsupport import OWNER
from hostsupport import CLAIM, POLICY, BriefWorld, in_leg_thread
from servicesupport import GATEWAY_REPLY, claim_with_id, owner_rows
from toolsupport import tracer_of

from meridian.platform.common.db import connect
from meridian.runtime.agent_framework_host import AgentFrameworkHost
from meridian.runtime.runs import RunOutcome
from meridian.runtime.tool_client import ToolResult
from meridian.workloads.claim_brief.workflow import build

CLAIM_ROW = "SELECT (c.*)::text FROM claims.claims AS c WHERE c.claim_id = %s"
NOTES = "SELECT note FROM claims.notes ORDER BY created_at"
REQUESTS = "SELECT reason FROM claims.approval_requests ORDER BY created_at"
CHECKPOINTS = "SELECT count(*) FROM runtime.workflow_checkpoints WHERE thread_id = %s"
# The tools with an idempotency key: the only ones that take a ``step`` word.
WRITES = frozenset({"request_approval", "add_claim_note"})
# ``build`` is the workload's entry point; the host is built from it, as the
# runtime will build it from the registry's entry point.
FACTORY = build


def claim_input(**changes: Any) -> dict[str, Any]:
    """The run's input, as the Claims Triage App sends it: ``{"claim": facts}``
    with the facts of a triage (the claim of claims.json without its claimant)
    and ``changes`` laid over them."""
    facts = {k: v for k, v in claim_with_id(CLAIM).items() if k != "claimant"}
    return {"claim": {**facts, **changes}}


def host_for(world: BriefWorld) -> AgentFrameworkHost:
    return AgentFrameworkHost(FACTORY, dsn=world.dsn())


def start_leg(
    world: BriefWorld, tools: Any = None, run_input: dict[str, Any] | None = None
) -> RunOutcome:
    """The first leg, in a thread of its own with a time limit. ``tools`` is the
    tool client (the real one over the real servers when None)."""
    host = host_for(world)
    return in_leg_thread(
        lambda: host.start(
            world.identity,
            world.model(),
            world.tools() if tools is None else tools,
            tracer_of(world.exporter),
            claim_input() if run_input is None else run_input,
        )
    )


def resume_leg(
    world: BriefWorld, tools: Any = None, value: dict[str, Any] | None = None
) -> RunOutcome:
    """A resume by a host built from nothing, as a new process would have;
    ``value`` is what the runtime's resume carries."""
    host = host_for(world)
    return in_leg_thread(
        lambda: host.resume(
            world.identity,
            world.model(),
            world.tools() if tools is None else tools,
            tracer_of(world.exporter),
            {} if value is None else value,
        )
    )


def reply_with(world: BriefWorld, text: str, finish_reason: str = "stop") -> None:
    """The text the stub gateway answers the next call with."""
    world.gateway.reply = {
        **GATEWAY_REPLY,
        "output": {"text": text, "finish_reason": finish_reason},
    }


def record_decision(world: BriefWorld, word: str) -> None:
    """The row the Claims API writes when an adjuster decides, for this run."""
    with connect(world.db.dsn(OWNER), "test-seed") as conn:
        conn.execute(
            "INSERT INTO claims.decisions (claim_id, run_id, decision) "
            "VALUES (%s, %s, %s)",
            (CLAIM, world.run_id, word),
        )


def claim_row(world: BriefWorld) -> str:
    """The claim's whole row as text: one cast sees every column."""
    return owner_rows(world.db, CLAIM_ROW, (CLAIM,))[0][0]


def notes(world: BriefWorld) -> list[str]:
    return [row[0] for row in owner_rows(world.db, NOTES)]


def approval_reasons(world: BriefWorld) -> list[str]:
    return [row[0] for row in owner_rows(world.db, REQUESTS)]


def checkpoint_rows(world: BriefWorld) -> int:
    return owner_rows(world.db, CHECKPOINTS, (str(world.thread_id),))[0][0]


def requests_sent(world: BriefWorld) -> list[str]:
    """The body of each request the stub gateway received, as text."""
    return [request.content.decode("utf-8") for request in world.gateway.seen]


class FailsOnceAfter:
    """The real tool client, except that the first call of ``tool`` that
    succeeds is followed by ``error``: the write was made and the step that made
    it failed, which is the step that runs twice. ``results`` is what each call
    of ``tool`` answered: ``replayed`` says whether the server found its key."""

    def __init__(self, real: Any, tool: str, error: Exception) -> None:
        self._real, self._tool, self._error = real, tool, error
        self._failed = False
        self.results: list[ToolResult] = []

    def call(
        self, tool: str, arguments: Mapping[str, Any], *, step: str | None = None
    ) -> ToolResult:
        result = self._real.call(tool, arguments, step=step)
        if tool == self._tool:
            self.results.append(result)
            if not self._failed:
                self._failed = True
                raise self._error
        return result


class Refuses:
    """The real tool client, except that a call of ``tool`` raises ``error``
    before anything is sent. ``called`` is every tool the step asked for."""

    def __init__(self, real: Any, tool: str, error: Exception) -> None:
        self._real, self._tool, self._error = real, tool, error
        self.called: list[str] = []

    def call(
        self, tool: str, arguments: Mapping[str, Any], *, step: str | None = None
    ) -> ToolResult:
        self.called.append(tool)
        if tool == self._tool:
            raise self._error
        return self._real.call(tool, arguments, step=step)


class ScriptedTools:
    """A tool client that answers from a script, so a test can put text in a
    result that the real servers (which check their answers against the
    contract) never return. It records each call: the tool, its arguments and
    its ``step`` word. The three writes and the outcome answer as the servers
    do, with the decision ``outcome`` (``approve`` unless the test says)."""

    def __init__(
        self, policy: dict[str, Any], history: dict[str, Any], outcome: str = "approve"
    ) -> None:
        self._answers: dict[str, dict[str, Any]] = {
            "policy_lookup": policy,
            "claim_history": history,
            "request_approval": {"request_id": str(uuid.uuid4()), "replayed": False},
            "approval_outcome": {"outcome": outcome},
            "add_claim_note": {"note_id": str(uuid.uuid4()), "replayed": False},
        }
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    def call(
        self, tool: str, arguments: Mapping[str, Any], *, step: str | None = None
    ) -> ToolResult:
        if step is not None and tool not in WRITES:
            # The real client's own refusal of a step word for a tool that has no
            # idempotency key.
            raise ValueError("a tool without an idempotency key takes no step")
        self.calls.append((tool, dict(arguments), step))
        return ToolResult(data=self._answers[tool], replayed=False, call_id=None)


def policy_answer(**changes: Any) -> dict[str, Any]:
    """The answer of ``policy_lookup`` for POL-0049 of the synthetic policies."""
    policy = {
        "policy_number": POLICY,
        "product": "HOME-PLUS",
        "wording_version": "2026-01",
        "start_date": "2026-05-14",
        "end_date": "2027-05-13",
        "status": "active",
        "deductible": 150,
        "limit": 190000,
        "sum_insured": 190000,
    }
    return {"found": True, "policy": {**policy, **changes}}
