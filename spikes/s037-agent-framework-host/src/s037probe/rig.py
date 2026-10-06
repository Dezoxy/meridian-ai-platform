"""What a probe needs to run legs of the workflow against the PostgreSQL store:
a rig per run (the store's DSN and a thread ID the host chose), a workflow built
from nothing for each leg, and raw calls on it. No decision of a host is made
here; the probes look at what the framework does.
"""

import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent_framework import Workflow, WorkflowCheckpoint, WorkflowRunResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from s037probe.flow import (
    CHECKPOINT_TYPES,
    CLAIM,
    WORKFLOW_NAME,
    Deps,
    build_workflow,
)
from s037probe.store import PostgresCheckpointStorage
from s037probe.support import (
    StandIn,
    gateway_client,
    model_client,
    run_in_plain_thread,
    tool_client,
    tracer_of,
)


def plain_deps(exporter: InMemorySpanExporter | None = None, **dials: Any) -> Deps:
    """Deps over an in-process stand-in tool server and a stub gateway, built
    from nothing; ``exporter`` takes the tool client's spans; ``dials`` are
    ``Deps`` fields."""
    exporter = exporter or InMemorySpanExporter()
    http, _ = gateway_client()
    server = StandIn().server
    tools = tool_client({"policy-mcp": server, "claims-mcp": server}, exporter)
    return Deps(tools, model_client(http), tracer_of(exporter), **dials)


@dataclass
class Rig:
    """One run: the database and the thread ID its checkpoints are keyed by."""

    dsn: str
    thread_id: uuid.UUID
    used_storage: PostgresCheckpointStorage | None = field(default=None, init=False)

    def storage(self) -> PostgresCheckpointStorage:
        return PostgresCheckpointStorage(self.dsn, self.thread_id, CHECKPOINT_TYPES)

    def deps(self, **dials: Any) -> Deps:
        """Deps over in-process stand-ins, built from nothing."""
        return plain_deps(**dials)

    def workflow(
        self,
        deps: Deps | None = None,
        storage: PostgresCheckpointStorage | None = None,
        **kwargs: Any,
    ) -> Workflow:
        """A workflow built from nothing, over this rig's store (or ``storage``);
        the store it used is kept in ``used_storage`` for the probe to read."""
        self.used_storage = storage or self.storage()
        return build_workflow(deps or self.deps(), self.used_storage, **kwargs)


def leg[Result](work: Callable[[], Awaitable[Result]]) -> Result:
    """One leg as the runtime runs it: a new loop in a plain thread."""
    return run_in_plain_thread(lambda: asyncio.run(_await(work)))


async def _await[Result](work: Callable[[], Awaitable[Result]]) -> Result:
    return await work()


def start(rig: Rig, deps: Deps | None = None) -> tuple[Workflow, WorkflowRunResult]:
    workflow = rig.workflow(deps)
    return workflow, leg(lambda: workflow.run(CLAIM))


async def latest(rig: Rig) -> WorkflowCheckpoint | None:
    return await rig.storage().get_latest(workflow_name=WORKFLOW_NAME)


def latest_sync(rig: Rig) -> WorkflowCheckpoint | None:
    return leg(lambda: latest(rig))


def all_checkpoints(rig: Rig) -> list[WorkflowCheckpoint]:
    return leg(lambda: rig.storage().list_checkpoints(workflow_name=WORKFLOW_NAME))


def resume(
    rig: Rig,
    response: Any,
    deps: Deps | None = None,
    *,
    checkpoint_id: str | None = None,
) -> tuple[Deps, WorkflowRunResult]:
    """Resume in a workflow built from nothing: send ``response`` to the pending
    requests of the checkpoint (the latest one unless ``checkpoint_id`` says)."""
    used = deps or rig.deps()
    workflow = rig.workflow(used)

    async def go() -> WorkflowRunResult:
        chosen = checkpoint_id
        if chosen is None:
            found = await latest(rig)
            if found is None:
                raise LookupError("no checkpoint")
            chosen = found.checkpoint_id
        stored = await rig.storage().load(chosen)
        ids = list(stored.pending_request_info_events)
        return await workflow.run(
            responses={rid: response for rid in ids}, checkpoint_id=chosen
        )

    return used, leg(go)


def continue_from(
    rig: Rig, checkpoint_id: str, deps: Deps | None = None
) -> tuple[Deps, WorkflowRunResult]:
    """Restore a checkpoint and run on, with no responses."""
    used = deps or rig.deps()
    workflow = rig.workflow(used)
    return used, leg(lambda: workflow.run(checkpoint_id=checkpoint_id))


def describe(result: WorkflowRunResult) -> dict[str, Any]:
    """What a leg's result says, as JSON a second process can print."""
    requests = result.get_request_info_events()
    return {
        "paused": bool(requests),
        "request_ids": [event.request_id for event in requests],
        "outputs": result.get_outputs(),
        "final_state": str(result.get_final_state()),
    }


def main(argv: list[str]) -> int:
    """``python -m s037probe.rig <start|resume> <dsn> <thread_id> [response]``:
    a leg in a process of its own, sharing only the store with the others."""
    action, dsn, thread = argv[0], argv[1], uuid.UUID(argv[2])
    rig = Rig(dsn, thread)
    try:
        if action == "start":
            deps = rig.deps()
            _, result = start(rig, deps)
        else:
            deps = rig.deps()
            response: Any = json.loads(argv[3])
            _, result = resume(rig, response, deps)
    except BaseException as error:
        print(json.dumps({"raised": type(error).__name__, "message": str(error)[:300]}))
        return 0
    print(json.dumps({**describe(result), "counts": dict(deps.counts)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
