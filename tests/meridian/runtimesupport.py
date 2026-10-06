"""Publishing a stand-in graph for the Agent Runtime's tests (S037).

The runtime loads one graph for every agent of kind ``graph`` that the registry
holds, so a test that fakes the graph of the agent under test must leave the
others to their real entry points: the registry holds more than one runnable
agent, and the runtime under test must start with all of them, as in production.
"""

import importlib
import importlib.metadata
from collections.abc import Iterable

import pytest
from servicesupport import REPO_ROOT

from meridian.runtime import graphs
from meridian.runtime.graphs import GraphFactory

AGENT_UNDER_TEST = "claims-triage"


class FakeEntryPoint:
    """The entry point of the agent under test, loading ``factory``.

    It claims the module of the real graph (``meridian.workloads.<agent>.graph``),
    which the loader locates before it loads: the stand-in graphs live under
    tests/, outside the meridian package.
    """

    class dist:
        name = "meridian"

    def __init__(
        self,
        factory: GraphFactory,
        name: str = AGENT_UNDER_TEST,
        module: str | None = None,
    ) -> None:
        self.factory = factory
        self.name = name
        # An agent whose module is not ``graph`` (the second host's workloads
        # publish a ``workflow``) names the module its entry point claims.
        self.module = module or f"meridian.workloads.{name.replace('-', '_')}.graph"
        self.value = f"{self.module}:build"

    def load(self) -> GraphFactory:
        return self.factory


def register_agents(monkeypatch: pytest.MonkeyPatch, *entries: FakeEntryPoint) -> None:
    """Publish each of ``entries`` for the next ``make_client``: a test of two
    agents publishes both in one call, as the next call would replace the
    first's. Every other name keeps the entry point the installed package really
    publishes (see ``register`` for the loader's checks)."""
    names = {entry.name for entry in entries}

    def entry_points(*, group: str) -> Iterable[object]:
        real = importlib.metadata.entry_points(group=group)
        return [*entries, *(e for e in real if e.name not in names)]

    monkeypatch.setattr(graphs, "entry_points", entry_points)
    for entry in entries:
        importlib.import_module(entry.module)
    monkeypatch.setattr(graphs, "TRUSTED_ROOT", REPO_ROOT)


def register(
    monkeypatch: pytest.MonkeyPatch,
    factory: GraphFactory,
    agent: str = AGENT_UNDER_TEST,
) -> None:
    """Publish ``factory`` as the graph of ``agent`` for the next ``make_client``.

    Every other name keeps the entry point the installed package really
    publishes. The loader's package-directory check is pointed at the repository
    for these tests, which holds both the real workloads and the stand-ins, and
    the real module is imported so that the loader finds it loaded; the check
    itself is tested in test_graphs.py.
    """
    register_agents(monkeypatch, FakeEntryPoint(factory, agent))
