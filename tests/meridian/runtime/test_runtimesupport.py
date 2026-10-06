"""The helper that publishes a stand-in graph for the runtime's tests (S037):
the stand-in for the agent under test, the real entry point for every other."""

import importlib.metadata
import sys
from types import ModuleType, SimpleNamespace

import pytest
from runtimesupport import AGENT_UNDER_TEST, register
from servicesupport import REPO_ROOT

from meridian.platform.common.entry_points import GRAPHS_GROUP
from meridian.runtime import graphs


def real_entry(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, value=f"meridian.workloads.{name}:build")


def stand_in(model: object, tools: object) -> None:
    """Stands in for a workload's factory."""


@pytest.fixture
def really_published(monkeypatch: pytest.MonkeyPatch) -> list[SimpleNamespace]:
    """What the installed package publishes: the agent under test and another."""
    entries = [real_entry(AGENT_UNDER_TEST), real_entry("other-agent")]
    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda *, group: entries, raising=True
    )
    return entries


def test_the_stand_in_replaces_the_real_entry_of_the_agent_under_test_only(
    monkeypatch: pytest.MonkeyPatch, really_published: list[SimpleNamespace]
) -> None:
    register(monkeypatch, stand_in)

    published = list(graphs.entry_points(group=GRAPHS_GROUP))

    assert [e.name for e in published] == [AGENT_UNDER_TEST, "other-agent"]
    assert published[0].load() is stand_in
    assert published[1] is really_published[1]


def test_the_agent_under_test_is_a_parameter_and_every_other_name_stays_real(
    monkeypatch: pytest.MonkeyPatch, really_published: list[SimpleNamespace]
) -> None:
    module = "meridian.workloads.other_agent.graph"
    monkeypatch.setitem(sys.modules, module, ModuleType(module))

    register(monkeypatch, stand_in, agent="other-agent")

    published = list(graphs.entry_points(group=GRAPHS_GROUP))
    assert [e.name for e in published] == ["other-agent", AGENT_UNDER_TEST]
    assert published[0].load() is stand_in
    assert published[0].value == f"{module}:build"
    assert published[1] is really_published[0]


def test_the_loader_is_pointed_at_the_repository(
    monkeypatch: pytest.MonkeyPatch, really_published: list[SimpleNamespace]
) -> None:
    register(monkeypatch, stand_in)

    assert graphs.TRUSTED_ROOT == REPO_ROOT


def test_the_helper_asks_the_installed_package_when_the_runtime_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(monkeypatch, stand_in)

    published = {e.name for e in graphs.entry_points(group=GRAPHS_GROUP)}

    really = {e.name for e in importlib.metadata.entry_points(group=GRAPHS_GROUP)}
    assert published == really | {AGENT_UNDER_TEST}
