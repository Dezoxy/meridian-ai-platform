"""What the Agent Runtime asks of a host, and the two clients' asynchronous faces
(S037).

A *host* runs a workload's agent in one agent framework. The runtime's neutral
code (the run API, the run rows, resume-once, leases, audit, limits, metrics)
asks a host for three things and nothing else: start a leg with an input, resume
a leg, forget a run's checkpoints. A leg ends ``Completed`` or ``AwaitingApproval``
with the output so far, or raises; the caller records the failure. The protocol
names no framework's type, so a host for any framework fits it.

The two clients a workload is given are synchronous: the tool client without a
kept transport drives an event loop of its own, and a step of an asynchronous
framework runs on a loop already. ``AsyncToolClient`` and ``AsyncModelClient``
are the same two objects, awaited: each call is ``asyncio.to_thread`` of the
synchronous call with the same arguments, so the clients' limits, audit rows,
refusals and exceptions are theirs and nothing here adds a second of any. The
thread is given a copy of the caller's context, so a client's span and the
trace header stay under whichever span is current where the face is awaited.

This module imports no agent framework (a test holds that in a fresh
interpreter), and the run types are imported for the type checker only:
``runtime.runs`` loads LangGraph.
"""

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from opentelemetry.trace import Tracer

from meridian.platform.registry.models import DataClass
from meridian.runtime.model_client import ChatResult, ModelClient
from meridian.runtime.tool_client import ToolClient, ToolResult

if TYPE_CHECKING:
    from meridian.runtime.runs import RunIdentity, RunOutcome


@runtime_checkable
class Host(Protocol):
    """One agent's host. It is built for the agent (it holds the workload's
    factory), and a call is one leg of one run.

    ``model`` and ``tools`` are the run's two clients, built by the caller for
    the leg with their limits. ``tracer`` makes the leg's step spans. A host
    raises whatever its framework or the workload raises; a ``GraphFailure``
    names a failure of the host's or the workload's own contract. ``start`` and
    ``resume`` never delete the run's checkpoints: the caller does, through
    ``forget``, once the run is recorded as ended.
    """

    def start(
        self,
        identity: "RunIdentity",
        model: ModelClient,
        tools: ToolClient,
        tracer: Tracer,
        run_input: dict[str, Any],
    ) -> "RunOutcome":
        """Run the first leg with ``run_input``. A leg that pauses answers
        ``AwaitingApproval`` with the output so far, if it has one."""
        ...

    def resume(
        self,
        identity: "RunIdentity",
        model: ModelClient,
        tools: ToolClient,
        tracer: Tracer,
        value: dict[str, Any],
    ) -> "RunOutcome":
        """Continue the paused run's thread. A thread with nothing to resume
        raises ``GraphFailure("no-pending-pause")``, and one with several
        pauses ``GraphFailure("several-pending-pauses")``, before anything runs.
        """
        ...

    def forget(self, identity: "RunIdentity") -> None:
        """Delete every checkpoint of the run's thread."""
        ...


class AsyncModelClient:
    """The model client, awaited. ``chat`` has the client's arguments and
    returns its result."""

    def __init__(self, client: ModelClient) -> None:
        self._client = client

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int | None = None,
        data_class: DataClass | None = None,
        response_schema: Mapping[str, Any] | None = None,
    ) -> ChatResult:
        return await asyncio.to_thread(
            self._client.chat,
            messages,
            max_output_tokens=max_output_tokens,
            data_class=data_class,
            response_schema=response_schema,
        )


class AsyncToolClient:
    """The tool client, awaited. ``call`` has the client's arguments and returns
    its result. There is no worker's view: an agent on a host that offers these
    faces declares no workers, and the registry holds that."""

    def __init__(self, client: ToolClient) -> None:
        self._client = client

    async def call(
        self, tool: str, arguments: Mapping[str, Any], *, step: str | None = None
    ) -> ToolResult:
        return await asyncio.to_thread(self._client.call, tool, arguments, step=step)
