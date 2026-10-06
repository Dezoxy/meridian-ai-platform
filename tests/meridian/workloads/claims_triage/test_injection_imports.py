"""The shape of the injection grader's imports (S061, S076).

The grader reads another module's private name only by accident of history; a
rename there would then break it without a public interface to point at.
"""

import ast
import inspect

import pytest

from meridian.workloads.claims_triage import injection

WORKLOAD = "meridian.workloads.claims_triage"
SCREENING_PACKAGE = "meridian.platform.guardrails"
# A name under one of these, or any relative name, is another module's.
WATCHED = (WORKLOAD, SCREENING_PACKAGE)
IMPORT_MODULE = "importlib.import_module"


def private_part(path: str) -> str | None:
    """The first private part of a dotted path below a watched package (any
    part of a relative path), or None."""
    if path.startswith("."):
        parts = [part for part in path.split(".") if part]
    else:
        below = [
            path[len(watched) :]
            for watched in WATCHED
            if path == watched or path.startswith(watched + ".")
        ]
        if not below:
            return None
        parts = [part for part in below[0].split(".") if part]
    return next((part for part in parts if part.startswith("_")), None)


def from_import_path(node: ast.ImportFrom, name: str) -> str:
    base = "." * node.level + (node.module or "")
    return base + name if base.endswith(".") or not base else f"{base}.{name}"


def import_bindings(tree: ast.AST) -> dict[str, str]:
    """The dotted path each imported name stands for: ``import a.b`` binds
    ``a``, ``import a.b as x`` binds ``x`` to ``a.b``, ``from p import m as x``
    binds ``x`` to ``p.m``."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    bound[alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound[alias.asname or alias.name] = from_import_path(node, alias.name)
    return bound


def literal_module(call: ast.Call, bound: dict[str, str]) -> str | None:
    """The module a call to ``import_module`` or ``__import__`` names with a
    string literal, or None for any other call."""
    func = resolve(call.func, bound)
    is_import = func == IMPORT_MODULE or (
        isinstance(call.func, ast.Name) and call.func.id == "__import__"
    )
    if not is_import or not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def resolve(node: ast.AST, bound: dict[str, str]) -> str | None:
    """The dotted path an expression stands for when it is a name bound by an
    import, an attribute of one, or a call that imports a literal."""
    if isinstance(node, ast.Name):
        return bound.get(node.id)
    if isinstance(node, ast.Attribute):
        owner = resolve(node.value, bound)
        return None if owner is None else f"{owner}.{node.attr}"
    if isinstance(node, ast.Call):
        return literal_module(node, bound)
    return None


def private_imports(source: str) -> list[str]:
    """Every private name of a watched module that ``source`` imports or
    reaches, by any import form this test knows."""
    tree = ast.parse(source)
    bound = import_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, owner = node.targets[0], resolve(node.value, bound)
            if isinstance(target, ast.Name) and owner is not None:
                bound[target.id] = owner

    paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            paths.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            paths.update(from_import_path(node, a.name) for a in node.names)
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            owner = resolve(node.value, bound)
            if owner is not None:
                paths.add(f"{owner}.{node.attr}")
        elif (
            isinstance(node, ast.Call)
            and (imported := literal_module(node, bound)) is not None
        ):
            paths.add(imported)
    return sorted(path for path in paths if private_part(path) is not None)


PLANTED = [
    pytest.param(
        f"from {WORKLOAD}.evaluation import _helper\n",
        id="from-import",
    ),
    pytest.param(
        f"from {WORKLOAD}.evaluation import _helper as helper\n",
        id="from-import-renamed",
    ),
    pytest.param("from .evaluation import _helper\n", id="relative-from-import"),
    pytest.param(
        f"from {WORKLOAD}._hidden import helper\n",
        id="from-a-private-module",
    ),
    pytest.param(
        "from meridian.platform.guardrails.screening import _normalise\n",
        id="from-the-screening-module",
    ),
    pytest.param(
        f"import {WORKLOAD}.evaluation._helper\n",
        id="plain-import-of-a-private-module",
    ),
    pytest.param(
        f"import {WORKLOAD}.evaluation\n{WORKLOAD}.evaluation._helper()\n",
        id="plain-import-then-attribute",
    ),
    pytest.param(
        f"import {WORKLOAD}.evaluation as ev\nev._helper()\n",
        id="plain-import-with-alias",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation as ev\nev._helper()\n",
        id="from-import-of-a-module-with-alias",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\nevaluation._helper()\n",
        id="from-import-of-a-module",
    ),
    pytest.param(
        "from . import evaluation as ev\nev._helper()\n",
        id="relative-module-with-alias",
    ),
    pytest.param(
        "from meridian.platform.guardrails import screening\nscreening._normalise\n",
        id="the-screening-module-as-an-attribute",
    ),
    pytest.param(
        f'import importlib\nimportlib.import_module("{WORKLOAD}._hidden")\n',
        id="import-module-with-a-literal",
    ),
    pytest.param(
        'import importlib\nimportlib.import_module(".evaluation._helper")\n',
        id="import-module-with-a-relative-literal",
    ),
    pytest.param(
        f'import importlib as il\nil.import_module("{WORKLOAD}._hidden")\n',
        id="import-module-through-an-alias-of-importlib",
    ),
    pytest.param(
        "from importlib import import_module as load\n"
        f'load("{WORKLOAD}.evaluation._helper")\n',
        id="import-module-through-an-alias-of-the-function",
    ),
    pytest.param(
        f'import importlib\nm = importlib.import_module("{WORKLOAD}.evaluation")\n'
        "m._helper()\n",
        id="import-module-bound-then-attribute",
    ),
    pytest.param(
        f'__import__("{WORKLOAD}._hidden")\n',
        id="dunder-import-with-a-literal",
    ),
]

ALLOWED = [
    pytest.param(
        f"from {WORKLOAD}.evaluation import Report\n",
        id="from-import-of-a-public-name",
    ),
    pytest.param("from .evaluation import report\n", id="relative-public-name"),
    pytest.param(
        f"import {WORKLOAD}.evaluation\n{WORKLOAD}.evaluation.report()\n",
        id="plain-import-then-public-attribute",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation as ev\nev.report()\n",
        id="alias-then-public-attribute",
    ),
    pytest.param(
        "import json\njson._default_encoder\n",
        id="private-attribute-of-another-package",
    ),
    pytest.param(
        "from json import _default_encoder\n",
        id="private-name-from-another-package",
    ),
    pytest.param(
        "class A:\n    def f(self):\n        return self._x\n",
        id="private-attribute-of-self",
    ),
    pytest.param(
        f'import importlib\nimportlib.import_module("{WORKLOAD}.evaluation")\n',
        id="import-module-of-a-public-module",
    ),
    pytest.param(
        'import importlib\nimportlib.import_module("json._hidden")\n',
        id="import-module-of-another-package",
    ),
]


@pytest.mark.parametrize("source", PLANTED)
def test_a_private_name_is_found_in_every_form_it_can_be_imported_in(
    source: str,
) -> None:
    found = private_imports(source)

    assert found != []


@pytest.mark.parametrize("source", ALLOWED)
def test_a_public_name_or_another_packages_private_one_is_not_found(
    source: str,
) -> None:
    found = private_imports(source)

    assert found == []


def test_the_injection_grader_imports_no_private_name_from_the_workload() -> None:
    found = private_imports(inspect.getsource(injection))

    assert found == []
