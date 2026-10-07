# What these tests are for (S074, backlog row 879): the import walker of
# test_injection_imports.py is a tripwire, not a proof, and its docstring lists
# what it cannot see and what it over-reports. Here each listed limit is a small
# source text and the walker's answer to it, as it is today. A change of the
# walker that closes one limit, or opens another, shows as a test below that
# must be changed on purpose, together with the docstring's list.
#
# Nothing in a source text runs. ``private_imports`` hands the text to
# ``ast.parse`` and walks the tree it returns; the text is never compiled,
# imported or passed to ``eval`` or ``exec``, and ``eval`` and ``exec`` appear
# only inside strings. The walker is loaded from its file by path, because
# ``tests/meridian/workloads/claims_triage`` is not on the import path; no test
# of that file is imported with it.

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

WALKER_FILE = Path(__file__).with_name("test_injection_imports.py")


def load_walker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("import_walker", WALKER_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WALKER = load_walker()
WORKLOAD = WALKER.WORKLOAD
EVALUATION = WALKER.EVALUATION
FROM_WORKLOAD = WALKER.FROM_WORKLOAD
HIDDEN = f"{WORKLOAD}._hidden"

# Each source reaches, or would reach if it ran, a private name of a watched
# module by a route the walker does not read.
BLIND_SPOTS = [
    pytest.param(
        f'import builtins\nbuiltins.__import__("{HIDDEN}")\n',
        id="builtins-dunder-import",
    ),
    pytest.param(
        f'import sys\nsys.modules["{EVALUATION}"]._x\n',
        id="sys-modules-subscript",
    ),
    pytest.param(
        f"def load():\n    global ev\n    from {WORKLOAD} import evaluation as ev\n"
        "def use():\n    return ev._x\n",
        id="an-alias-made-through-global",
    ),
    pytest.param(
        "def outer():\n    def load():\n        nonlocal ev\n"
        f"        from {WORKLOAD} import evaluation as ev\n"
        "    ev = None\n    load()\n    return ev._x\n",
        id="an-alias-made-through-nonlocal",
    ),
    pytest.param(f'{FROM_WORKLOAD}eval("evaluation._x")\n', id="eval"),
    pytest.param(f'exec("from {EVALUATION} import _x")\n', id="exec"),
    pytest.param(
        f'import importlib\nname = "{HIDDEN}"\nimportlib.import_module(name)\n',
        id="import-module-with-a-non-literal-argument",
    ),
    pytest.param(
        f'name = "{HIDDEN}"\n__import__(name)\n',
        id="dunder-import-with-a-non-literal-argument",
    ),
    pytest.param(
        f'{FROM_WORKLOAD}attribute = "_x"\ngetattr(evaluation, attribute)\n',
        id="getattr-with-a-non-literal-argument",
    ),
    pytest.param(
        f'__import__("{WORKLOAD}", fromlist=["_hidden"])\n',
        id="dunder-import-with-a-private-name-in-fromlist",
    ),
    pytest.param(
        f"import importlib\n{FROM_WORKLOAD}importlib.reload(evaluation)._x\n",
        id="an-attribute-reached-through-a-call-other-than-import-module",
    ),
]

# Each source uses no private name of the other module, and the walker reports
# one anyway.
FALSE_POSITIVES = [
    pytest.param(
        f"{FROM_WORKLOAD}evaluation = object()\nevaluation._x\n",
        id="an-alias-rebound-at-module-level",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f(value):\n    match value:\n        case evaluation:\n"
        "            return evaluation._x\n",
        id="a-match-capture-named-like-the-alias",
    ),
]


def test_every_blind_spot_source_names_a_watched_module_and_a_private_name() -> None:
    for param in BLIND_SPOTS:
        (source,) = param.values

        assert WORKLOAD in source, param.id
        assert "_x" in source or "_hidden" in source, param.id


@pytest.mark.parametrize("source", BLIND_SPOTS)
def test_what_the_walker_cannot_see_it_reports_as_nothing(source: str) -> None:
    found = WALKER.private_imports(source)

    assert found == []


@pytest.mark.parametrize("source", FALSE_POSITIVES)
def test_what_the_walker_over_reports_it_reports_as_a_private_name(
    source: str,
) -> None:
    found = WALKER.private_imports(source)

    assert found == [f"{EVALUATION}._x"]


def test_the_route_hides_a_name_the_walker_finds_by_a_plain_literal_import() -> None:
    literal = f'__import__("{HIDDEN}")\n'
    through_builtins = f'import builtins\nbuiltins.__import__("{HIDDEN}")\n'

    found = [WALKER.private_imports(literal), WALKER.private_imports(through_builtins)]

    assert found == [[HIDDEN], []]
