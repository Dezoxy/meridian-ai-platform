"""Find a workload's graph factory by the registry's agent ID (T-40).

A workload publishes ``build(model, tools) -> StateGraph`` in the entry-point group
``meridian.graphs`` under its agent ID. The runtime loads only what the
registry names, only from the ``meridian`` distribution, only when exactly
one entry point carries the name, only when the entry point's value names a
module under ``meridian.workloads`` and only when that module's file lies in
the installed ``meridian`` package. The module's location is read before its
code runs, and again once it has loaded (the shared checks are in
``meridian.platform.common.entry_points``). A second installed package, or a
distribution that calls itself ``meridian`` and hides the real one, cannot
substitute a graph for an agent.
"""

import inspect
import sys
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import assert_never

from langgraph.graph import StateGraph

from meridian.platform.common.entry_points import (
    GRAPHS_GROUP,
    TRUSTED_DISTRIBUTION,
    TRUSTED_ROOT,
    TRUSTED_VALUE_PREFIX,
    EntryPointRefused,
    Refusal,
    load_trusted_entry_point,
)
from meridian.platform.registry import Registry
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient

GraphFactory = Callable[[ModelClient, ToolClient], StateGraph]


class GraphLoadError(Exception):
    """The agent's graph cannot be loaded; the message is for logs, not clients."""


def _refusal_message(agent_id: str, refused: EntryPointRefused) -> str:
    match refused.reason:
        case Refusal.PUBLISHED_TWICE:
            return f"graph {agent_id!r} is published more than once"
        case Refusal.NOT_PUBLISHED:
            return f"no graph is published for agent {agent_id!r}"
        case Refusal.OTHER_DISTRIBUTION:
            return (
                f"graph {agent_id!r} comes from distribution "
                f"{refused.distribution!r}, not {TRUSTED_DISTRIBUTION!r}"
            )
        case Refusal.OUTSIDE_WORKLOADS:
            return f"graph {agent_id!r} names a module outside {TRUSTED_VALUE_PREFIX!r}"
        case Refusal.UNLOCATABLE | Refusal.OUTSIDE_ROOT:
            return (
                f"graph {agent_id!r} names a module that is missing or "
                "outside the meridian package"
            )
        case Refusal.FAILED_TO_IMPORT:
            return f"graph {agent_id!r} failed to import"
        case Refusal.MOVED_OUTSIDE_ROOT:
            return f"graph {agent_id!r} comes from a file outside the meridian package"
        case _:
            assert_never(refused.reason)


def load_graph_factory(agent_id: str, registry: Registry) -> GraphFactory:
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
