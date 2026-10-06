"""Find a workload's graph factory by the registry's agent ID (T-40).

A workload publishes ``build(model, tools) -> StateGraph`` (or, for an agent on
the second host, a factory that returns a workflow definition) in the entry-point
group ``meridian.graphs`` under its agent ID. The runtime loads only what the
registry names, only from the ``meridian`` distribution, only when exactly
one entry point carries the name, only when the entry point's value names a
module under ``meridian.workloads`` and only when that module's file lies in
the installed ``meridian`` package. The module's location is read before its
code runs, and again once it has loaded (the shared checks are in
``meridian.platform.common.entry_points``). A second installed package, or a
distribution that calls itself ``meridian`` and hides the real one, cannot
substitute a graph for an agent.

A refusal is worded as the evaluations' loader words it: the agent's ID and the
shared fixed sentence of the reason. The distribution that published an entry
and an import error's text are another party's and are quoted nowhere; the
exception's cause is the error's class and nothing else. The factory's own
module is checked here as well (``_in_trusted_root``), which the shared checks
do not do: an entry point under the workloads can re-export a factory defined
elsewhere.
"""

import inspect
import sys
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from langgraph.graph import StateGraph

from meridian.platform.common.entry_points import (
    GRAPHS_GROUP,
    TRUSTED_ROOT,
    TRUSTED_VALUE_PREFIX,
    EntryPointRefused,
    Refusal,
    fixed_text,
    load_trusted_entry_point,
    reason_text,
)
from meridian.platform.registry import Registry
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient

GraphFactory = Callable[[ModelClient, ToolClient], StateGraph]
# What the loader returns, which is what either host takes: a LangGraph agent's
# factory returns a ``StateGraph``, an agent-framework agent's a workflow
# definition and its clients are the asynchronous faces of the two. The registry's
# ``host`` says which; the host checks it at start (``host_wiring``).
AgentFactory = Callable[[Any, Any], object]
# The caller's word in the shared sentences (the evaluations' loader says
# "evaluation"). A workflow of the second host is published in the same group
# and is a "graph" here too.
SUBJECT = "graph"


class GraphLoadError(Exception):
    """The agent's graph cannot be loaded; the message is for logs, not clients."""


def _refusal_message(agent_id: str, refused: EntryPointRefused) -> str:
    """The agent's ID (the registry's, checked before the loader runs) and the
    shared fixed sentence, as the evaluations' loader words it. The distribution
    that published an entry and an import error's text are another party's and
    are never quoted; the refusal's cause, which the caller chains, is the
    error's class and nothing else."""
    return f"agent {agent_id!r}: {fixed_text(refused, SUBJECT)}"


def load_graph_factory(agent_id: str, registry: Registry) -> AgentFactory:
    if not registry.has_agent(agent_id):
        raise GraphLoadError(f"agent {agent_id!r} is not in the registry")
    try:
        factory = load_trusted_entry_point(
            GRAPHS_GROUP,
            agent_id,
            entry_points=entry_points,
            trusted_root=TRUSTED_ROOT,
            value_prefix=TRUSTED_VALUE_PREFIX,
        )
    except EntryPointRefused as refused:
        message = _refusal_message(agent_id, refused)
        raise GraphLoadError(message) from refused.__cause__
    if not callable(factory):
        raise GraphLoadError(f"graph {agent_id!r} is not callable")
    if not _takes_model_and_tools(factory):
        raise GraphLoadError(
            f"graph {agent_id!r} has a signature that does not take a model and tools"
        )
    if not _in_trusted_root(factory):
        words = reason_text(Refusal.MOVED_OUTSIDE_ROOT, SUBJECT)
        raise GraphLoadError(f"agent {agent_id!r}: {words}")
    return factory


def _takes_model_and_tools(factory: Callable[..., object]) -> bool:
    """Whether ``factory(model, tools)`` binds. A factory with the signature of
    an earlier step would otherwise fail on the first run, not at the start."""
    try:
        inspect.signature(factory).bind(None, None)
    except (TypeError, ValueError):
        return False
    return True


def _in_trusted_root(factory: Callable[..., object]) -> bool:
    module = sys.modules.get(getattr(factory, "__module__", ""))
    file = getattr(module, "__file__", None)
    return file is not None and (
        Path(file).resolve().is_relative_to(TRUSTED_ROOT.resolve())
    )
