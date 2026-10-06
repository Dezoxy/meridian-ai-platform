"""Each agent's host, picked by the registry and checked at start (S037).

The registry says, per agent, which framework runs it (``host: langgraph``, the
default, or ``host: agent-framework``). The service builds that host for each
agent when it starts and keeps it for the life of the service (see
``HostScope``), and it refuses to start when an agent's entry point is not what
its host runs: a LangGraph agent whose factory returns a workflow definition, or
an agent-framework agent whose factory returns a graph. The refusal names the
agent and the host and says what the factory returned. It never copies a
factory's own message (it could hold anything) or chains the framework's
traceback.

The check calls each factory once, with clients that are never used: for the
second host, faces over clients that refuse every call (``check_factory``); for
LangGraph, a model client and a tool client of the real classes (a graph's
factory asks the tool client for its workers' views, so a stand-in would have to
be one). The probe clients allow no call (``max_calls=0``) and audit no refusal.
"""

import ssl
import uuid
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from typing import cast

import httpx
from opentelemetry.trace import Tracer

from meridian.platform.registry.models import Agent, Registry
from meridian.runtime.agent_framework_host import (
    AgentFrameworkHost,
    WorkflowFactory,
    check_factory,
)
from meridian.runtime.graphs import AgentFactory, GraphFactory, GraphLoadError
from meridian.runtime.hosts import HostScope
from meridian.runtime.langgraph_host import (
    SaverScope,
    check_graph_factory,
    langgraph_scope,
)
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient, ToolTarget
from meridian.runtime.tool_transport import ToolTransport

# The tenant a probe's model client carries: it sends nothing.
PROBE_TENANT = "start-check"
# Makes, for an agent, the two clients its LangGraph factory is checked with.
type Probe = Callable[[str], tuple[ModelClient, ToolClient]]


class HostRefused(GraphLoadError):
    """An agent's entry point is not what its host runs; the message is for
    logs, not clients."""


def build_host_scopes(
    registry: Registry,
    factories: Mapping[str, AgentFactory],
    *,
    dsn: str,
    saver_scope: SaverScope,
    http: httpx.Client,
    servers: Mapping[str, ToolTarget],
    tracer: Tracer,
    verify: ssl.SSLContext | bool,
    transport: ToolTransport,
) -> dict[str, HostScope]:
    """The scope of each agent in ``factories``, by agent ID, or ``HostRefused``
    for the first agent whose entry point its host does not run. ``dsn`` is the
    runtime role's, for the second host's checkpoint store; the rest build the
    clients a LangGraph factory is checked with."""

    def probe(agent_id: str) -> tuple[ModelClient, ToolClient]:
        run_id = uuid.uuid4()
        model = ModelClient(
            http, tenant=PROBE_TENANT, agent=agent_id, run_id=run_id, max_calls=0
        )
        tools = ToolClient(
            servers,
            registry=registry,
            agent=agent_id,
            run_id=run_id,
            tracer=tracer,
            on_refusal=lambda tool: None,
            on_worker_refusal=lambda tool, reason, worker: None,
            max_calls=0,
            verify=verify,
            transport=transport,
        )
        return model, tools

    scopes: dict[str, HostScope] = {}
    for agent in registry.agents:
        factory = factories.get(agent.id)
        if factory is not None:
            scopes[agent.id] = _scope_of(agent, factory, dsn, saver_scope, probe)
    return scopes


def _scope_of(
    agent: Agent,
    factory: AgentFactory,
    dsn: str,
    saver_scope: SaverScope,
    probe: Probe,
) -> HostScope:
    try:
        if agent.host == "agent-framework":
            check_factory(factory)
            host = AgentFrameworkHost(cast(WorkflowFactory, factory), dsn=dsn)
            return lambda: nullcontext(host)
        model, tools = probe(agent.id)
        check_graph_factory(factory, model, tools)
        return langgraph_scope(cast(GraphFactory, factory), saver_scope)
    except TypeError as error:
        raise HostRefused(
            f"agent {agent.id!r} is on host {agent.host!r}, and its entry point "
            f"is not one that host runs: {error}"
        ) from None
