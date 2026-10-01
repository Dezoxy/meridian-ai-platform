"""Find a workload's graph factory by the registry's agent ID (T-40).

A workload publishes ``build(model, tools) -> StateGraph`` in the entry-point group
``meridian.graphs`` under its agent ID. The runtime loads only what the
registry names, only from the ``meridian`` distribution, only when exactly
one entry point carries the name, only when the entry point's value names a
module under ``meridian.workloads`` and only when that module's file lies in
the installed ``meridian`` package. A second installed package, or a
distribution that calls itself ``meridian`` and hides the real one, cannot
substitute a graph for an agent.
"""

import inspect
import re
import sys
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path

from langgraph.graph import StateGraph

import meridian
from meridian.platform.registry import Registry
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient

GRAPH_GROUP = "meridian.graphs"
TRUSTED_DISTRIBUTION = "meridian"
TRUSTED_VALUE_PREFIX = "meridian.workloads."
# The directory of the installed package. Tests that load a stand-in graph from
# outside it point this at their own directory.
TRUSTED_ROOT = Path(meridian.__file__).resolve().parent

GraphFactory = Callable[[ModelClient, ToolClient], StateGraph]


class GraphLoadError(Exception):
    """The agent's graph cannot be loaded; the message is for logs, not clients."""


def _normalised(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load_graph_factory(agent_id: str, registry: Registry) -> GraphFactory:
    if not registry.has_agent(agent_id):
        raise GraphLoadError(f"agent {agent_id!r} is not in the registry")
    named = [ep for ep in entry_points(group=GRAPH_GROUP) if ep.name == agent_id]
    if len(named) > 1:
        raise GraphLoadError(f"graph {agent_id!r} is published more than once")
    if not named:
        raise GraphLoadError(f"no graph is published for agent {agent_id!r}")
    (entry,) = named
    dist = entry.dist.name if entry.dist is not None else None
    if dist is None or _normalised(dist) != TRUSTED_DISTRIBUTION:
        raise GraphLoadError(
            f"graph {agent_id!r} comes from distribution {dist!r}, "
            f"not {TRUSTED_DISTRIBUTION!r}"
        )
    if not entry.value.startswith(TRUSTED_VALUE_PREFIX):
        raise GraphLoadError(
            f"graph {agent_id!r} names a module outside {TRUSTED_VALUE_PREFIX!r}"
        )
    try:
        factory = entry.load()
    except Exception as exc:
        raise GraphLoadError(f"graph {agent_id!r} failed to import") from exc
    if not callable(factory):
        raise GraphLoadError(f"graph {agent_id!r} is not callable")
    if not _takes_model_and_tools(factory):
        raise GraphLoadError(
            f"graph {agent_id!r} has a signature that does not take a model and tools"
        )
    if not _in_trusted_root(factory):
        raise GraphLoadError(
            f"graph {agent_id!r} comes from a file outside the meridian package"
        )
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
    return file is not None and Path(file).resolve().is_relative_to(TRUSTED_ROOT)
