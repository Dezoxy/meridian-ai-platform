"""Finding a workload's graph factory by the registry's agent ID (T-40)."""

import importlib
import importlib.metadata
import logging
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from servicesupport import REGISTRY_DIR, REPO_ROOT

from meridian.platform.common.entry_points import (
    EntryPointRefused,
    Refusal,
    fixed_text,
)
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
# The shared loader's sentence for every reason that says "not ours", in the
# graphs' word for what it loads.
NOT_OURS = "the workload's graph does not come from the meridian package"
CANNOT_LOAD = "the workload's graph cannot be loaded"


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

    with pytest.raises(GraphLoadError, match=NOT_OURS):
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

    with pytest.raises(GraphLoadError, match=NOT_OURS):
        load_graph_factory("claims-triage", REGISTRY)


def test_a_factory_whose_module_lies_outside_the_package_directory_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    published(monkeypatch, entry())
    monkeypatch.setattr(graphs, "TRUSTED_ROOT", tmp_path)  # not where build lives

    with pytest.raises(GraphLoadError, match=NOT_OURS):
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

    with pytest.raises(GraphLoadError, match=f"claims-triage.*{NOT_OURS}"):
        load_graph_factory("claims-triage", REGISTRY)

    assert calls == []


def test_a_trusted_root_that_is_a_link_admits_the_factory_inside_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    published(monkeypatch, entry())
    link = tmp_path / "root-link"
    link.symlink_to(REPO_ROOT, target_is_directory=True)
    monkeypatch.setattr(graphs, "TRUSTED_ROOT", link)

    assert load_graph_factory("claims-triage", REGISTRY) is build


def test_a_factory_defined_outside_the_package_is_refused_whatever_its_entry_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reexported(model: object, tools: object) -> None:
        """A factory a trusted module imported from elsewhere."""

    reexported.__module__ = "json"  # the standard library: outside the repository
    published(monkeypatch, entry(loads=lambda: reexported))

    with pytest.raises(GraphLoadError, match=f"claims-triage.*{NOT_OURS}"):
        load_graph_factory("claims-triage", REGISTRY)


@pytest.mark.parametrize(
    ("value", "sentence"),
    [
        ("meridian.workloads.no_such_module:build", NOT_OURS),
        ("meridian.workloads.claims_triage.no_such_module:build", NOT_OURS),
        # The parent of this module cannot even be imported.
        ("meridian.workloads.no_such_package.module:build", CANNOT_LOAD),
        ("meridian.workloads..module:build", CANNOT_LOAD),
    ],
)
def test_a_graph_whose_module_cannot_be_found_never_runs(
    monkeypatch: pytest.MonkeyPatch, value: str, sentence: str
) -> None:
    calls: list[str] = []

    def loads() -> Any:
        calls.append("the module's code ran")
        return build

    published(
        monkeypatch, FakeEntryPoint("claims-triage", FakeDist("meridian"), loads, value)
    )

    with pytest.raises(GraphLoadError, match=f"claims-triage.*{sentence}"):
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

    with pytest.raises(GraphLoadError, match=NOT_OURS):
        load_graph_factory("claims-triage", REGISTRY)

    assert "shadow_graph_module" not in sys.modules


# ── the words of a refusal (T-40, T-80) ─────────────────────────────────────
PLANTED_DISTRIBUTION = "planted-distribution-name"
PLANTED_ERROR = "planted-error-text /opt/planted/module.py"
# Each reason's sentence, pinned once. The evaluations' loader says the same
# words with "evaluation" for "graph" (the next test holds that).
SENTENCES = {
    Refusal.PUBLISHED_TWICE: "the workload's graph is published more than once",
    Refusal.NOT_PUBLISHED: "no graph is published for this workload; known: none",
    Refusal.OTHER_DISTRIBUTION: NOT_OURS,
    Refusal.OUTSIDE_WORKLOADS: NOT_OURS,
    Refusal.UNLOCATABLE: CANNOT_LOAD,
    Refusal.OUTSIDE_ROOT: NOT_OURS,
    Refusal.FAILED_TO_IMPORT: CANNOT_LOAD,
    Refusal.MOVED_OUTSIDE_ROOT: NOT_OURS,
}


def test_every_reason_has_its_sentence_pinned_here() -> None:
    assert set(SENTENCES) == set(Refusal)


@pytest.mark.parametrize("reason", list(Refusal), ids=lambda reason: reason.name)
def test_a_refusal_is_the_agent_and_the_shared_fixed_sentence(
    reason: Refusal,
) -> None:
    refused = EntryPointRefused(reason, distribution=PLANTED_DISTRIBUTION)

    message = graphs._refusal_message("claims-triage", refused)

    assert message == f"agent 'claims-triage': {SENTENCES[reason]}"
    assert PLANTED_DISTRIBUTION not in message


@pytest.mark.parametrize("reason", list(Refusal), ids=lambda reason: reason.name)
def test_the_graphs_words_are_the_evaluations_with_the_subject_changed(
    reason: Refusal,
) -> None:
    refused = EntryPointRefused(reason)

    graph = fixed_text(refused, "graph")
    evaluation = fixed_text(refused, "evaluation")

    assert graph.replace("graph", "evaluation") == evaluation
    assert graphs._refusal_message("claims-triage", refused).endswith(graph)


def test_the_message_of_a_graph_published_by_another_distribution_quotes_no_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    published(monkeypatch, entry(dist=PLANTED_DISTRIBUTION))

    with caplog.at_level(logging.DEBUG), pytest.raises(GraphLoadError) as refused:
        load_graph_factory("claims-triage", REGISTRY)

    shown = "".join(traceback.format_exception(refused.value))
    assert str(refused.value) == f"agent 'claims-triage': {NOT_OURS}"
    assert PLANTED_DISTRIBUTION not in shown
    assert PLANTED_DISTRIBUTION not in caplog.text
    assert all(PLANTED_DISTRIBUTION not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("agent", ["claims-triage", "claim-brief"])
def test_an_import_error_leaves_its_text_in_no_message_chain_or_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, agent: str
) -> None:
    def broken() -> None:
        raise ImportError(PLANTED_ERROR)

    published(monkeypatch, entry(name=agent, loads=broken))

    with caplog.at_level(logging.DEBUG), pytest.raises(GraphLoadError) as refused:
        load_graph_factory(agent, REGISTRY)

    shown = "".join(traceback.format_exception(refused.value))
    assert str(refused.value) == f"agent {agent!r}: {CANNOT_LOAD}"
    assert "planted-error-text" not in shown
    assert "/opt/planted" not in shown
    assert "planted-error-text" not in caplog.text
    assert all("planted-error-text" not in r.getMessage() for r in caplog.records)
    # The class of the error is what the chain keeps, as the evaluations' does.
    assert str(refused.value.__cause__) == "ImportError"
    assert refused.value.__suppress_context__


def test_a_name_nobody_published_lists_only_names_the_registry_would_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published(monkeypatch, entry(name="Planted Name With Spaces"), entry(name="other"))

    with pytest.raises(GraphLoadError) as refused:
        load_graph_factory("claims-triage", REGISTRY)

    assert str(refused.value) == (
        "agent 'claims-triage': no graph is published for this workload; known: other"
    )
