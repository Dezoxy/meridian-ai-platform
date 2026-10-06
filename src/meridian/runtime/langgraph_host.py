"""The Agent Runtime's host for LangGraph (ADR 2, S037).

What the runtime did with LangGraph before the hosts, behind the ``Host``
protocol with no change of behaviour: compile the workload's graph with the
saver, bound it to ``RECURSION_LIMIT`` steps, run it with ``durability="sync"``
under one span per node (``NodeSpans``), pause by ``interrupt``, answer with the
graph's ``output`` and forget a run's thread. A graph that changed while a run
waited has no pending pause (``resume_command`` raises ``no-pending-pause``,
which ends the run): LangGraph's own answer, which the second host's
``workflow-changed`` matches.

A host is bound to one checkpoint saver, and a saver is per request (a
connection of its own, S015), so the service keeps a scope for each agent
(``langgraph_scope``) and the request enters it: it opens the saver and yields a
host bound to it. ``fresh`` is how a second saver is opened, for the one retry of
a delete.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from opentelemetry.trace import Tracer

from meridian.runtime.failures import GraphFailure
from meridian.runtime.graphs import GraphFactory
from meridian.runtime.hosts import FactoryRefused, HostScope, log_forget_failure
from meridian.runtime.model_client import ModelClient
from meridian.runtime.models import RunState
from meridian.runtime.runs import (
    NO_PENDING_PAUSE,
    RECURSION_LIMIT,
    SEVERAL_PENDING_PAUSES,
    RunIdentity,
    RunOutcome,
)
from meridian.runtime.tool_client import ToolClient
from meridian.runtime.tracing import NodeSpans

type SaverScope = Callable[[], AbstractContextManager[BaseCheckpointSaver]]


class LangGraphHost:
    """The host of one agent whose workload is a LangGraph ``StateGraph``, bound
    to the saver of one request. ``factory`` is the workload's entry point."""

    def __init__(
        self, factory: GraphFactory, saver: BaseCheckpointSaver, fresh: SaverScope
    ) -> None:
        self._factory = factory
        self._saver = saver
        self._fresh = fresh

    def start(
        self,
        identity: RunIdentity,
        model: ModelClient,
        tools: ToolClient,
        tracer: Tracer,
        run_input: dict[str, Any],
    ) -> RunOutcome:
        graph = self._factory(model, tools).compile(checkpointer=self._saver)
        return _invoke(graph, _config(identity, tracer), run_input)

    def resume(
        self,
        identity: RunIdentity,
        model: ModelClient,
        tools: ToolClient,
        tracer: Tracer,
        value: dict[str, Any],
    ) -> RunOutcome:
        """The graph continues its paused thread and its one pending pause reads
        ``value`` verbatim (see ``resume_command``); a thread with no pending
        pause, or with several, raises a ``GraphFailure`` before any node runs."""
        graph = self._factory(model, tools).compile(checkpointer=self._saver)
        config = _config(identity, tracer)
        return _invoke(graph, config, resume_command(graph, config, value))

    def forget(self, identity: RunIdentity) -> None:
        """Drop the run's thread; a failure is logged and changes no answer. A
        failed delete is tried once more on ``fresh()``, a saver of its own (the
        injected one, when there is one). The run is already recorded, and a
        message could hold claim text, so each log line has the run ID, the
        exception class and the sqlstate only."""
        thread = str(identity.thread_id)
        try:
            self._saver.delete_thread(thread)
            return
        except Exception as exc:  # whatever the saver raises changes no answer
            log_forget_failure(identity, exc)
        try:
            with self._fresh() as retry:
                retry.delete_thread(thread)
        except Exception as exc:
            log_forget_failure(identity, exc)


def langgraph_scope(factory: GraphFactory, saver_scope: SaverScope) -> HostScope:
    """The scope the service keeps for a LangGraph agent: a request enters it to
    open its saver (a database that refuses the connection starts no run) and
    gets the host bound to that saver. The saver closes when the request leaves."""

    @contextmanager
    def scope() -> Iterator[LangGraphHost]:
        with saver_scope() as saver:
            yield LangGraphHost(factory, saver, saver_scope)

    return scope


def resume_command(
    graph: CompiledStateGraph, config: dict[str, Any], value: dict[str, Any]
) -> Command:
    """Address ``value`` to the one pending pause by its interrupt ID.

    LangGraph reads a bare dict whose keys all look like interrupt IDs as a map
    from IDs to values, an empty dict included (it is vacuously true), so a
    caller's ``{}`` would resume nothing. Keyed by the pause's own ID, the value
    reaches the pause verbatim whatever its keys. Raises before any node runs.
    """
    pending = graph.get_state(config).interrupts
    if not pending:
        raise GraphFailure(NO_PENDING_PAUSE)
    if len(pending) > 1:
        raise GraphFailure(SEVERAL_PENDING_PAUSES)
    return Command(resume={pending[0].id: value})


def _config(identity: RunIdentity, tracer: Tracer) -> dict[str, Any]:
    return {
        "recursion_limit": RECURSION_LIMIT,
        "configurable": {"thread_id": str(identity.thread_id)},
        "callbacks": [NodeSpans(tracer, run_id=identity.run_id, agent=identity.agent)],
    }


def _invoke(
    graph: CompiledStateGraph, config: dict[str, Any], graph_input: Any
) -> RunOutcome:
    graph.invoke(graph_input, config, durability="sync")
    snapshot = graph.get_state(config)
    output = snapshot.values.get("output")
    if output is not None and not isinstance(output, dict):
        raise TypeError("a graph's output must be an object")
    state: RunState = "AwaitingApproval" if snapshot.interrupts else "Completed"
    return RunOutcome(state, output)


def check_graph_factory(factory: object, model: ModelClient, tools: ToolClient) -> None:
    """Refuse an entry point that is not a LangGraph graph's: call it with a
    leg's two clients (built for the check, never used) and require the
    ``StateGraph`` the host compiles. A workflow definition of the second host, a
    function that returns nothing and a factory that raises are each a
    ``FactoryRefused`` with a clear sentence; the factory's own message is never
    copied, because it could hold anything."""
    if not callable(factory):
        raise FactoryRefused("a langgraph agent's entry point must be a factory")
    try:
        value = factory(model, tools)
    except Exception as error:
        raise FactoryRefused(
            "the entry point could not be called with a model client and a tool "
            f"client: {type(error).__name__}"
        ) from None
    if not isinstance(value, StateGraph):
        raise FactoryRefused(
            "a langgraph agent's factory must return a StateGraph, not "
            f"{type(value).__name__}"
        )
