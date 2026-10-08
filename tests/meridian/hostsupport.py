"""Support for the tests of the Agent Runtime's hosts (S037, R4a).

The agent of these tests, ``claim-brief``, is the registry's own (it declares
the second host, lists the tools the workflow calls and no workers, and a tenant
lists it). The tool client and the real tool servers are both built from a
scratch copy of the registry, since each checks the agent against its own.
"""

import contextvars
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from dbsupport import OWNER, DatabaseHandle
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import GATEWAY_REPLY
from toolsupport import add_run, seed_world, settings_for, tracer_of

from meridian.platform.claims_mcp.app import (
    create_app as create_claims_app,
)
from meridian.platform.common.db import connect
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.policy_mcp.app import create_app as create_policy_app
from meridian.platform.registry import Registry, load_registry
from meridian.runtime.model_client import ModelClient
from meridian.runtime.runs import RunIdentity
from meridian.runtime.tool_client import ToolClient

AGENT = "claim-brief"
TENANT = "claims-triage"
CLAIM = "CLM-0001"
POLICY = "POL-0049"
NOTE_STEP = "file-note"
APPROVAL_STEP = "request-approval"
LEG_SECONDS = 60


def planted_registry(plant: Callable[..., Path]) -> tuple[Path, Registry]:
    """The scratch copy's directory and its loaded form. ``claim-brief`` is in
    the real registry since S037's registration (W1b), so nothing is planted:
    the copy is for the tests that edit it."""
    directory = plant()
    return directory, load_registry(directory)


class Gateway:
    """A stub gateway over ``httpx.MockTransport``: ``seen`` is every request it
    received; ``status`` and ``headers`` are what it answers next."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.status = 200
        self.headers: dict[str, str] = {}
        self.reply = GATEWAY_REPLY
        self.http = httpx.Client(
            base_url="http://gateway.invalid",
            transport=httpx.MockTransport(self._answer),
        )

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        body = self.reply if self.status == 200 else {}
        return httpx.Response(self.status, json=body, headers=self.headers)

    def model(self, run_id: uuid.UUID, max_calls: int = 4) -> ModelClient:
        return ModelClient(
            self.http,
            tenant=TENANT,
            agent=AGENT,
            run_id=run_id,
            max_calls=max_calls,
        )


@dataclass
class Refusals:
    """What the tool client told the runtime to audit."""

    tools: list[str | None]

    def __call__(self, tool: str | None) -> None:
        self.tools.append(tool)

    def worker(self, tool: str | None, reason: str, worker: str | None) -> None:
        self.tools.append(tool)


def tool_client(
    servers: dict[str, object],
    registry: Registry,
    exporter: InMemorySpanExporter,
    run_id: uuid.UUID,
    *,
    max_calls: int = 16,
) -> ToolClient:
    """A tool client for ``claim-brief`` with no database behind its refusal
    callbacks."""
    audited = Refusals([])
    return ToolClient(
        servers,  # type: ignore[arg-type]
        registry=registry,
        agent=AGENT,
        run_id=run_id,
        tracer=tracer_of(exporter),
        on_refusal=audited,
        on_worker_refusal=audited.worker,
        max_calls=max_calls,
    )


# ── the world of a leg: a database, real tool servers, a stub gateway ────────
def real_servers(
    db: DatabaseHandle, registry_dir: Path, exporter: InMemorySpanExporter
) -> dict[str, Any]:
    """The policy and claims tool servers over the database, reading the
    scratch registry, as the runtime's tool client reaches them in-process."""
    return {
        "policy-mcp": create_policy_app(
            settings_for(db, "policy_mcp", registry_dir),
            tracer_provider=make_tracer_provider("policy-mcp", exporter),
        ).server,
        "claims-mcp": create_claims_app(
            settings_for(db, "claims_mcp", registry_dir),
            tracer_provider=make_tracer_provider("claims-mcp", exporter),
        ).server,
    }


@dataclass
class BriefWorld:
    """One run of ``claim-brief``: its ``Running`` row (the tool servers read the
    tenant, agent and claim from it), the checkpoint thread the host is given,
    and what the clients are built from."""

    db: DatabaseHandle
    registry_dir: Path
    registry: Registry
    exporter: InMemorySpanExporter
    gateway: Gateway
    run_id: uuid.UUID
    thread_id: uuid.UUID

    @property
    def identity(self) -> RunIdentity:
        return RunIdentity(
            run_id=self.run_id,
            thread_id=self.thread_id,
            agent=AGENT,
            tenant=TENANT,
            reference=CLAIM,
        )

    def tools(
        self, *, max_calls: int = 16, servers: dict[str, Any] | None = None
    ) -> ToolClient:
        used = (
            real_servers(self.db, self.registry_dir, self.exporter)
            if servers is None
            else servers
        )
        return tool_client(
            used, self.registry, self.exporter, self.run_id, max_calls=max_calls
        )

    def model(self, *, max_calls: int = 4) -> ModelClient:
        return self.gateway.model(self.run_id, max_calls)

    def dsn(self) -> str:
        return self.db.dsn("agent_runtime")

    def set_run_status(self, status: str) -> None:
        with connect(self.db.dsn(OWNER), "test-seed") as conn:
            conn.execute(
                "UPDATE runtime.runs SET status = %s WHERE run_id = %s",
                (status, self.run_id),
            )


def make_world(db: DatabaseHandle, plant: Callable[..., Path]) -> BriefWorld:
    """A migrated database with the synthetic policies, claim CLM-0001 and a
    ``Running`` run of ``claim-brief`` for it."""
    directory, registry = planted_registry(plant)
    seed_world(db)
    return BriefWorld(
        db=db,
        registry_dir=directory,
        registry=registry,
        exporter=InMemorySpanExporter(),
        gateway=Gateway(),
        run_id=add_run(db, agent=AGENT, tenant=TENANT),
        thread_id=uuid.uuid4(),
    )


def in_leg_threads(*works: Callable[[], Any]) -> list[Any]:
    """Run each of ``works`` at the same time as the runtime runs legs: each in a
    thread of its own that holds a copy of the caller's context. The threads are
    given ``LEG_SECONDS`` and the test fails when one is still running: a
    workflow that never returns must not hang the suite. The results come back
    in order; the first error is raised."""
    boxes: list[dict[str, Any]] = [{} for _ in works]

    def runner(work: Callable[[], Any], box: dict[str, Any]) -> Callable[[], None]:
        context = contextvars.copy_context()

        def run() -> None:
            try:
                box["result"] = context.run(work)
            except BaseException as error:  # handed to the caller, raised there
                box["error"] = error

        return run

    threads = [
        threading.Thread(target=runner(work, box), daemon=True)
        for work, box in zip(works, boxes, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=LEG_SECONDS)
        if thread.is_alive():
            pytest.fail(f"a leg was still running after {LEG_SECONDS} seconds")
    for box in boxes:
        if "error" in box:
            raise box["error"]
    return [box["result"] for box in boxes]


def in_leg_thread[Result](work: Callable[[], Result]) -> Result:
    """One leg, as ``in_leg_threads`` runs it."""
    return in_leg_threads(work)[0]
