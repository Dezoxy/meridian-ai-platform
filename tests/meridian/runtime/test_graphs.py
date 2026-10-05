"""Finding a workload's graph factory by the registry's agent ID (T-40)."""

import importlib
import importlib.metadata
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from servicesupport import REGISTRY_DIR, REPO_ROOT

from meridian.platform.registry import load_registry
from meridian.runtime import graphs
from meridian.runtime.graphs import GraphLoadError, load_graph_factory


@dataclass
class FakeDist:
    name: str


@dataclass
class FakeEntryPoint:
    name: str
    dist: FakeDist | None
    loads: Callable[[], Any]
    value: str = "meridian.workloads.claims_triage.graph:build"

    def load(self) -> Any:
        return self.loads()


def build(model: object, tools: object) -> None:
    """Stands in for a workload's factory."""


def published(monkeypatch: pytest.MonkeyPatch, *entries: FakeEntryPoint) -> None:
    def fake_entry_points(*, group: str) -> list[FakeEntryPoint]:
        assert group == "meridian.graphs"
        return list(entries)

    monkeypatch.setattr(graphs, "entry_points", fake_entry_points)
    # The stand-in entry points claim the real module's value, which the loader
    # locates before it loads: so the trusted root is the repository, holding
    # both that module (imported here: the loader reads it once loaded) and the
    # stand-in factory below, which lives in tests/. The package-directory check
    # has its own tests at the end of this file.
    importlib.import_module("meridian.workloads.claims_triage.graph")
    monkeypatch.setattr(graphs, "TRUSTED_ROOT", REPO_ROOT)


def entry(
    name: str = "claims-triage", dist: str | None = "meridian", loads: Any = None
) -> FakeEntryPoint:
    return FakeEntryPoint(
        name=name,
        dist=None if dist is None else FakeDist(dist),
        loads=loads or (lambda: build),
    )


REGISTRY = load_registry(REGISTRY_DIR)


def test_the_factory_of_the_meridian_distribution_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published(monkeypatch, entry())

    assert load_graph_factory("claims-triage", REGISTRY) is build


def test_only_the_entry_point_named_by_the_agent_id_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published(monkeypatch, entry(name="other-agent", loads=lambda: 1 / 0), entry())

    assert load_graph_factory("claims-triage", REGISTRY) is build


def test_the_normalised_distribution_name_is_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published(monkeypatch, entry(dist="Meridian"))

    assert load_graph_factory("claims-triage", REGISTRY) is build


@pytest.mark.parametrize("dist", ["meridian-evil", "evil", "meridian2", None])
def test_an_entry_point_of_another_distribution_is_refused(
    monkeypatch: pytest.MonkeyPatch, dist: str | None
) -> None:
    published(monkeypatch, entry(dist=dist))

    with pytest.raises(GraphLoadError, match="distribution"):
        load_graph_factory("claims-triage", REGISTRY)


def test_a_name_published_twice_is_refused_even_by_a_second_meridian_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published(monkeypatch, entry(), entry())

    with pytest.raises(GraphLoadError, match="more than once"):
        load_graph_factory("claims-triage", REGISTRY)


def test_a_foreign_duplicate_of_the_name_is_refused_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published(monkeypatch, entry(), entry(dist="evil"))

    with pytest.raises(GraphLoadError, match="more than once"):
        load_graph_factory("claims-triage", REGISTRY)


def test_an_agent_that_is_not_in_the_registry_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published(monkeypatch, entry(name="rogue-agent"))

    with pytest.raises(GraphLoadError, match="registry"):
        load_graph_factory("rogue-agent", REGISTRY)


def test_an_agent_without_an_entry_point_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published(monkeypatch)

    with pytest.raises(GraphLoadError, match="no graph"):
        load_graph_factory("claims-triage", REGISTRY)


def test_a_refusal_the_wording_does_not_know_is_an_error_not_silence() -> None:
    unknown = SimpleNamespace(reason="a reason added later", distribution=None)

    with pytest.raises(AssertionError):
        graphs._refusal_message("claims-triage", unknown)


def test_an_entry_point_that_fails_to_import_is_a_load_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> None:
        raise ImportError("secret path /opt/x")

    published(monkeypatch, entry(loads=broken))

    with pytest.raises(GraphLoadError):
        load_graph_factory("claims-triage", REGISTRY)


def test_an_entry_point_that_is_not_callable_is_a_load_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published(monkeypatch, entry(loads=lambda: "not callable"))

    with pytest.raises(GraphLoadError, match="callable"):
        load_graph_factory("claims-triage", REGISTRY)


def build_with_the_old_signature(model: object) -> None:
    """The factory of a step that gave a graph no tools."""


def build_with_a_third_argument(model: object, tools: object, extra: object) -> None:
    """A factory the runtime would call with two arguments."""


def build_with_keyword_only_tools(model: object, *, tools: object) -> None:
    """A factory the runtime would call with two positional arguments."""


@pytest.mark.parametrize(
    "factory",
    [
        build_with_the_old_signature,
        build_with_a_third_argument,
        build_with_keyword_only_tools,
    ],
)
def test_a_factory_that_does_not_take_a_model_and_tools_stops_the_start(
    monkeypatch: pytest.MonkeyPatch, factory: Any
) -> None:
    published(monkeypatch, entry(loads=lambda: factory))

    with pytest.raises(GraphLoadError, match="signature"):
        load_graph_factory("claims-triage", REGISTRY)


def test_a_factory_with_defaults_or_variadic_arguments_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build_flexible(model: object, tools: object = None, *rest: object) -> None:
        """Two positional arguments bind."""

    published(monkeypatch, entry(loads=lambda: build_flexible))

    assert load_graph_factory("claims-triage", REGISTRY) is build_flexible


# ── where the factory comes from (T-40) ─────────────────────────────────────
@pytest.mark.parametrize(
    "value",
    [
        "evil.graph:build",
        "meridian:build",
        "meridian.workloadsevil.graph:build",
        "meridian.platform.registry.loader:load_registry",
    ],
)
def test_an_entry_point_value_outside_the_workloads_package_is_refused(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    def must_not_load() -> Any:
        raise AssertionError("an untrusted entry point was imported")

    published(
        monkeypatch,
        FakeEntryPoint("claims-triage", FakeDist("meridian"), must_not_load, value),
    )

    with pytest.raises(GraphLoadError, match=r"meridian\.workloads"):
        load_graph_factory("claims-triage", REGISTRY)


def test_a_factory_whose_module_lies_outside_the_package_directory_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    published(monkeypatch, entry())
    monkeypatch.setattr(graphs, "TRUSTED_ROOT", tmp_path)  # not where build lives

    with pytest.raises(GraphLoadError, match="outside"):
        load_graph_factory("claims-triage", REGISTRY)


def test_a_graph_whose_module_lies_outside_the_package_directory_never_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def loads() -> Any:
        calls.append("the module's code ran")
        return build

    published(monkeypatch, entry(loads=loads))
    monkeypatch.setattr(graphs, "TRUSTED_ROOT", tmp_path)  # not where the module is

    with pytest.raises(GraphLoadError, match=r"'claims-triage'.*outside"):
        load_graph_factory("claims-triage", REGISTRY)

    assert calls == []


def test_a_factory_defined_outside_the_package_is_refused_whatever_its_entry_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reexported(model: object, tools: object) -> None:
        """A factory a trusted module imported from elsewhere."""

    reexported.__module__ = "json"  # the standard library: outside the repository
    published(monkeypatch, entry(loads=lambda: reexported))

    with pytest.raises(GraphLoadError, match=r"'claims-triage'.*comes from a file"):
        load_graph_factory("claims-triage", REGISTRY)


@pytest.mark.parametrize(
    "value",
    [
        "meridian.workloads.no_such_module:build",
        "meridian.workloads.claims_triage.no_such_module:build",
        # The parent of this module cannot even be imported.
        "meridian.workloads.no_such_package.module:build",
        "meridian.workloads..module:build",
    ],
)
def test_a_graph_whose_module_cannot_be_found_never_runs(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    calls: list[str] = []

    def loads() -> Any:
        calls.append("the module's code ran")
        return build

    published(
        monkeypatch, FakeEntryPoint("claims-triage", FakeDist("meridian"), loads, value)
    )

    with pytest.raises(GraphLoadError, match=r"'claims-triage'.*outside"):
        load_graph_factory("claims-triage", REGISTRY)

    assert calls == []


def test_the_real_workload_graph_passes_both_checks() -> None:
    factory = load_graph_factory("claims-triage", REGISTRY)

    assert factory.__module__ == "meridian.workloads.claims_triage.graph"


def test_a_fake_meridian_distribution_first_on_the_path_cannot_substitute_a_graph(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # importlib.metadata keeps the first distribution of a name it finds, so a
    # dist-info called "meridian" earlier on sys.path hides the real one and its
    # entry points; the loader must not follow them.
    dist_info = tmp_path / "meridian-9.9.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: meridian\nVersion: 9.9\n", encoding="utf-8"
    )
    (dist_info / "entry_points.txt").write_text(
        "[meridian.graphs]\nclaims-triage = shadow_graph_module:build\n",
        encoding="utf-8",
    )
    (tmp_path / "shadow_graph_module.py").write_text(
        "raise SystemExit('the shadow module was imported')\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    assert [
        ep.value
        for ep in importlib.metadata.entry_points(group="meridian.graphs")
        if ep.name == "claims-triage"
    ] == ["shadow_graph_module:build"]  # the premise: the real entry is hidden

    with pytest.raises(GraphLoadError, match=r"meridian\.workloads"):
        load_graph_factory("claims-triage", REGISTRY)

    assert "shadow_graph_module" not in sys.modules
