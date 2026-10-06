"""The trust checks the two entry-point groups share (S061).

``meridian.graphs`` and ``meridian.evaluations`` are loaded by two wrappers that
word their own errors; the checks and their order live in one function.
"""

import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import meridian
from meridian.platform.common import entry_points as shared
from meridian.platform.common.entry_points import (
    EVALUATIONS_GROUP,
    GRAPHS_GROUP,
    EntryPointRefused,
    Refusal,
    load_trusted_entry_point,
)

REAL_VALUE = "meridian.workloads.claims_triage.evaluation_http:EVALUATION"
GROUP = "meridian.test-group"


def entry(
    name: str = "claims-triage",
    *,
    value: str = REAL_VALUE,
    dist: str | None = "meridian",
    loads: Callable[[], object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        value=value,
        dist=None if dist is None else SimpleNamespace(name=dist),
        # Like the real one, it imports the module the entry names.
        load=Loads() if loads is None else loads,
    )


def published(*entries: SimpleNamespace) -> Callable[..., list[SimpleNamespace]]:
    def entry_points(*, group: str) -> list[SimpleNamespace]:
        assert group == GROUP
        return list(entries)

    return entry_points


def load(*entries: SimpleNamespace, **options: Any) -> object:
    return load_trusted_entry_point(
        GROUP, "claims-triage", entry_points=published(*entries), **options
    )


def refusal_of(*entries: SimpleNamespace, **options: Any) -> EntryPointRefused:
    with pytest.raises(EntryPointRefused) as refused:
        load(*entries, **options)
    return refused.value


class Loads:
    """An entry point's ``load`` that says whether it was called. Like the real
    one it imports the module the entry names, so the loader finds it loaded."""

    def __init__(self, module: str = REAL_VALUE.partition(":")[0]) -> None:
        self.calls = 0
        self.module = module

    def __call__(self) -> object:
        self.calls += 1
        importlib.import_module(self.module)
        return "loaded"


@pytest.fixture
def plugin_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Callable[[str], str]:
    """Write a module that sets a marker when it runs; return its name."""

    def make(name: str) -> str:
        (tmp_path / f"{name}.py").write_text(
            "import sys\nsys.modules['ran_' + __name__] = True\nVALUE = 1\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()
        request.addfinalizer(lambda: sys.modules.pop(name, None))
        request.addfinalizer(lambda: sys.modules.pop("ran_" + name, None))
        return name

    return make


def test_the_two_group_names_are_the_published_ones() -> None:
    assert GRAPHS_GROUP == "meridian.graphs"
    assert EVALUATIONS_GROUP == "meridian.evaluations"


def test_an_entry_of_the_trusted_distribution_in_the_package_is_loaded() -> None:
    loads = Loads()

    assert load(entry(loads=loads)) == "loaded"
    assert loads.calls == 1


def test_only_the_entry_point_of_the_name_is_loaded() -> None:
    other = Loads()

    load(entry(name="other", loads=other), entry())

    assert other.calls == 0


def test_the_normalised_distribution_name_is_compared() -> None:
    assert load(entry(dist="Meridian")) == "loaded"


def test_a_name_published_twice_is_refused_even_by_a_foreign_second_entry() -> None:
    loads = Loads()

    refused = refusal_of(entry(loads=loads), entry(dist="evil", loads=loads))

    assert refused.reason is Refusal.PUBLISHED_TWICE
    assert loads.calls == 0


def test_a_name_that_is_not_published_is_refused_with_the_trusted_names() -> None:
    refused = refusal_of(
        entry(name="b-workload"),
        entry(name="a-workload"),
        entry(name="planted", dist="someone-else"),
    )

    assert refused.reason is Refusal.NOT_PUBLISHED
    assert refused.known == ("a-workload", "b-workload")


def test_no_trusted_name_at_all_comes_with_an_empty_list() -> None:
    refused = refusal_of()

    assert refused.reason is Refusal.NOT_PUBLISHED
    assert refused.known == ()


@pytest.mark.parametrize("dist", ["meridian-evil", "evil", "meridian2", None])
def test_an_entry_of_another_distribution_is_refused_and_names_it(
    dist: str | None,
) -> None:
    loads = Loads()

    refused = refusal_of(entry(dist=dist, loads=loads))

    assert refused.reason is Refusal.OTHER_DISTRIBUTION
    assert refused.distribution == dist
    assert loads.calls == 0


@pytest.mark.parametrize(
    "value",
    [
        "evil.graph:build",
        "meridian:build",
        "meridian.workloadsevil.graph:build",
        "meridian.platform.registry.loader:load_registry",
    ],
)
def test_a_value_outside_the_workloads_is_refused_before_it_loads(value: str) -> None:
    loads = Loads()

    refused = refusal_of(entry(value=value, loads=loads))

    assert refused.reason is Refusal.OUTSIDE_WORKLOADS
    assert loads.calls == 0


def test_the_value_prefix_is_a_parameter() -> None:
    loads = Loads()

    refused = refusal_of(entry(loads=loads), value_prefix="some.other.")

    assert refused.reason is Refusal.OUTSIDE_WORKLOADS
    assert loads.calls == 0


def test_a_module_outside_the_trusted_root_is_refused_before_its_code_runs(
    plugin_module: Callable[[str], str], tmp_path: Path
) -> None:
    name = plugin_module("outside_root_plugin")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    loads = Loads()

    refused = refusal_of(
        entry(value=f"{name}:VALUE", loads=loads),
        value_prefix=name,
        trusted_root=elsewhere,
    )

    assert refused.reason is Refusal.OUTSIDE_ROOT
    assert loads.calls == 0
    assert name not in sys.modules
    assert ("ran_" + name) not in sys.modules


@pytest.mark.parametrize(
    "value",
    [
        # find_spec finds no module of this name.
        "meridian.workloads.no_such_module:EVALUATION",
        # ... also under a package that exists.
        "meridian.workloads.claims_triage.no_such_module:EVALUATION",
    ],
)
def test_a_module_that_is_not_found_is_refused_before_it_loads(value: str) -> None:
    loads = Loads()

    refused = refusal_of(entry(value=value, loads=loads))

    assert refused.reason is Refusal.OUTSIDE_ROOT
    assert loads.calls == 0


@pytest.mark.parametrize(
    "value",
    [
        # find_spec cannot even import the parent of this one.
        "meridian.workloads.no_such_package.module:EVALUATION",
        # A name that is not a module name at all.
        "meridian.workloads..module:EVALUATION",
    ],
)
def test_a_module_whose_spec_cannot_be_read_is_refused_before_it_loads(
    value: str,
) -> None:
    loads = Loads()

    refused = refusal_of(entry(value=value, loads=loads))

    assert refused.reason is Refusal.UNLOCATABLE
    assert loads.calls == 0


def test_a_parent_package_that_fails_to_import_leaves_its_class_as_the_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "broken_parent_plugin"
    package.mkdir()
    (package / "__init__.py").write_text("def (:\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    loads = Loads()

    refused = refusal_of(
        entry(value="broken_parent_plugin.module:VALUE", loads=loads),
        value_prefix="broken_parent_plugin.",
    )

    assert refused.reason is Refusal.UNLOCATABLE
    assert isinstance(refused.__cause__, shared.LoadFailure)
    assert str(refused.__cause__) == "SyntaxError"
    assert loads.calls == 0


def test_a_module_with_no_file_is_refused_before_it_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A namespace package (a directory with no __init__.py) has no origin file.
    (tmp_path / "namespace_plugin").mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    loads = Loads()

    refused = refusal_of(
        entry(value="namespace_plugin:VALUE", loads=loads),
        value_prefix="namespace_plugin",
    )

    assert refused.reason is Refusal.OUTSIDE_ROOT
    assert loads.calls == 0


def test_the_trusted_root_is_a_parameter(
    plugin_module: Callable[[str], str], tmp_path: Path
) -> None:
    name = plugin_module("inside_root_plugin")
    loads = Loads(name)

    loaded = load(
        entry(value=f"{name}:VALUE", loads=loads),
        value_prefix=name,
        trusted_root=tmp_path,
    )

    assert loaded == "loaded"
    assert loads.calls == 1


def test_a_trusted_root_given_as_a_link_admits_a_module_inside_it(
    plugin_module: Callable[[str], str], tmp_path: Path
) -> None:
    name = plugin_module("linked_root_plugin")
    linked = tmp_path / "linked-root"
    linked.symlink_to(tmp_path, target_is_directory=True)
    loads = Loads(name)

    loaded = load(
        entry(value=f"{name}:VALUE", loads=loads),
        value_prefix=name,
        trusted_root=linked,
    )

    assert loaded == "loaded"
    assert loads.calls == 1


def test_a_trusted_root_given_as_a_link_still_refuses_a_module_outside_it(
    plugin_module: Callable[[str], str], tmp_path: Path
) -> None:
    name = plugin_module("outside_linked_root_plugin")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    linked = tmp_path / "linked-elsewhere"
    linked.symlink_to(elsewhere, target_is_directory=True)
    loads = Loads(name)

    refused = refusal_of(
        entry(value=f"{name}:VALUE", loads=loads),
        value_prefix=name,
        trusted_root=linked,
    )

    assert refused.reason is Refusal.OUTSIDE_ROOT
    assert loads.calls == 0


def test_an_entry_that_fails_to_import_is_refused_with_the_class_as_cause() -> None:
    def broken() -> object:
        raise ImportError("a message that stays out of the refusal")

    refused = refusal_of(entry(loads=broken))

    assert refused.reason is Refusal.FAILED_TO_IMPORT
    assert isinstance(refused.__cause__, shared.LoadFailure)
    assert str(refused.__cause__) == "ImportError"


def test_a_module_that_is_somewhere_else_once_loaded_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The first check reads the module's spec; the second reads what loaded.
    module = REAL_VALUE.split(":")[0]
    loads = Loads()

    def moved() -> object:
        monkeypatch.setitem(
            sys.modules, module, SimpleNamespace(__file__=str(tmp_path / "x.py"))
        )
        return loads()

    refused = refusal_of(entry(loads=moved))

    assert refused.reason is Refusal.MOVED_OUTSIDE_ROOT
    assert loads.calls == 1


def test_a_module_that_has_no_file_once_loaded_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = REAL_VALUE.split(":")[0]

    def fileless() -> object:
        monkeypatch.setitem(sys.modules, module, SimpleNamespace())
        return "loaded"

    refused = refusal_of(entry(loads=fileless))

    assert refused.reason is Refusal.MOVED_OUTSIDE_ROOT


def test_the_trusted_root_is_the_installed_package_directory() -> None:
    assert Path(meridian.__file__).resolve().parent == shared.TRUSTED_ROOT
    assert shared.TRUSTED_VALUE_PREFIX == "meridian.workloads."
    assert shared.TRUSTED_DISTRIBUTION == "meridian"
