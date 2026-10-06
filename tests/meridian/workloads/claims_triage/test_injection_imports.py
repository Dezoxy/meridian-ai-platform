"""The shape of the injection grader's imports (S061, S076).

The grader reads another module's private name only by accident of history; a
rename there would then break it without a public interface to point at.

``private_imports`` is a reading aid for this one test, not a proof. What it
reads: ``from X import _n`` (with ``as``, relative), a private module in an
imported path, ``import a.b._x``, a module bound by ``import``, ``import ... as``
or ``from p import m [as x]`` and then used as ``x._y``, ``importlib.import_module``
and ``__import__`` with a string literal (also through an alias, or assigned to
a name first), ``getattr(module, "_x")`` with a literal, and ``from X import *``
of a watched module. A star import is reported because the test cannot know
without importing the module that its ``__all__`` lists no private name, and the
star hides every later use. A dunder (``__name__``, ``__file__``, ``__all__``,
``__doc__``) is not a private name.

Scope: a function parameter, a name a function assigns, a ``for``/``with``/
``except`` target in it, and a comprehension's or lambda's own names shadow a
module-level alias inside that scope; an assignment of an ``import_module``
result in a scope makes a local alias. Module level never forgets an alias, so
a rebinding (``ev = object()`` after the import) is still flagged: a harmless
false positive.

What it cannot see: a non-literal argument of ``import_module``, ``__import__``
or ``getattr``; ``__import__(..., fromlist=[...])``; ``builtins.__import__``;
``sys.modules[...]``; an attribute reached through any call but a literal
``import_module``; an alias made through more than one assignment or by tuple
unpacking; a class body's own names; the names a ``match`` pattern captures;
``global`` and ``nonlocal``; ``eval`` and ``exec``. A private name of a package
nobody watches is ignored by design.
"""

import ast
import inspect
from collections.abc import Iterator

import pytest

from meridian.workloads.claims_triage import injection

WORKLOAD = "meridian.workloads.claims_triage"
SCREENING_PACKAGE = "meridian.platform.guardrails"
# The grader imports from the evaluation package too (the fingerprints, the
# report).
EVALUATION_PACKAGE = "meridian.platform.evaluation"
# A name under one of these, or any relative name, is another module's.
WATCHED = (WORKLOAD, SCREENING_PACKAGE, EVALUATION_PACKAGE)
IMPORT_MODULE = "importlib.import_module"
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, *COMPREHENSIONS)
# Nodes that bind a name by their own ``name`` (not by a ``Name`` in a store).
NAMED = (ast.ExceptHandler, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
Bindings = dict[str, str]


def is_dunder(part: str) -> bool:
    return len(part) > 4 and part.startswith("__") and part.endswith("__")


def is_private(part: str) -> bool:
    """A name that starts with an underscore and is no dunder; ``*`` (a star
    import) counts, because it may import any private name."""
    return part == "*" or (part.startswith("_") and not is_dunder(part))


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
    return next((part for part in parts if is_private(part)), None)


def from_import_path(node: ast.ImportFrom, name: str) -> str:
    base = "." * node.level + (node.module or "")
    return base + name if base.endswith(".") or not base else f"{base}.{name}"


def scope_roots(scope: ast.AST) -> list[ast.AST]:
    """The top nodes of what a scope runs: a body, a lambda's expression, a
    comprehension's element and loops. The module is a scope too."""
    if isinstance(scope, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef):
        return list(scope.body)
    if isinstance(scope, ast.Lambda):
        return [scope.body]
    if isinstance(scope, ast.DictComp):
        return [scope.key, scope.value, *scope.generators]
    assert isinstance(scope, ast.ListComp | ast.SetComp | ast.GeneratorExp)
    return [scope.elt, *scope.generators]


def enclosing_parts(scope: ast.AST) -> list[ast.AST]:
    """What a nested scope's node evaluates in the scope around it: decorators
    and defaults of a function, the first loop's iterable of a comprehension."""
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        args = scope.args
        defaults = [*args.defaults, *(d for d in args.kw_defaults if d is not None)]
        decorators = getattr(scope, "decorator_list", [])
        return [*decorators, *defaults]
    assert isinstance(scope, COMPREHENSIONS)
    return [scope.generators[0].iter]


def own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a scope's own body: a scope nested in it is yielded, but
    only the parts of it that run in this scope are walked."""
    stack = scope_roots(scope)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, SCOPES):
            stack.extend(enclosing_parts(node))
        else:
            stack.extend(ast.iter_child_nodes(node))


def parameters(scope: ast.AST) -> set[str]:
    if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        return set()
    args = scope.args
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return {arg.arg for arg in every if arg is not None}


def import_bindings(nodes: list[ast.AST]) -> Bindings:
    """The dotted path each imported name stands for: ``import a.b`` binds
    ``a``, ``import a.b as x`` binds ``x`` to ``a.b``, ``from p import m as x``
    binds ``x`` to ``p.m``."""
    bound: Bindings = {}
    for node in nodes:
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


def literal_module(call: ast.Call, bound: Bindings) -> str | None:
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


def resolve(node: ast.AST, bound: Bindings) -> str | None:
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


def stored_names(nodes: list[ast.AST]) -> set[str]:
    """Every name a scope binds other than by an import: assigned, a loop or
    ``with`` target, an ``except`` name, a nested function or class."""
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, NAMED) and node.name:
            names.add(node.name)
    return names


def scope_bindings(scope: ast.AST, outer: Bindings, *, module: bool) -> Bindings:
    """What the names of ``scope`` stand for: the outer bindings, minus the
    names the scope binds to anything else (never at module level, which keeps
    every alias), plus its own imports and the ``import_module`` results it
    assigns to a name."""
    nodes = list(own_nodes(scope))
    shadowed = parameters(scope) | (set() if module else stored_names(nodes))
    bound = {name: path for name, path in outer.items() if name not in shadowed}
    bound.update(import_bindings(nodes))
    for node in nodes:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, owner = node.targets[0], resolve(node.value, bound)
            if isinstance(target, ast.Name) and owner is not None:
                bound[target.id] = owner
    return bound


def paths_of(node: ast.AST, bound: Bindings) -> list[str]:
    """The dotted paths ``node`` itself imports or reaches (not its children)."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [from_import_path(node, alias.name) for alias in node.names]
    if isinstance(node, ast.Attribute) and is_private(node.attr):
        owner = resolve(node.value, bound)
        return [] if owner is None else [f"{owner}.{node.attr}"]
    if isinstance(node, ast.Call):
        return call_paths(node, bound)
    return []


def call_paths(call: ast.Call, bound: Bindings) -> list[str]:
    imported = literal_module(call, bound)
    if imported is not None:
        return [imported]
    is_getattr = isinstance(call.func, ast.Name) and call.func.id == "getattr"
    if is_getattr and len(call.args) >= 2:
        owner, name = resolve(call.args[0], bound), call.args[1]
        if owner is not None and isinstance(name, ast.Constant):
            return [f"{owner}.{name.value}"] if isinstance(name.value, str) else []
    return []


def reached(scope: ast.AST, outer: Bindings, *, module: bool = False) -> set[str]:
    """The paths a scope and the scopes nested in it import or reach."""
    bound = scope_bindings(scope, outer, module=module)
    paths: set[str] = set()
    for node in own_nodes(scope):
        paths.update(paths_of(node, bound))
        if isinstance(node, SCOPES):
            paths.update(reached(node, bound))
    return paths


def private_imports(source: str) -> list[str]:
    """Every private name of a watched module that ``source`` imports or
    reaches, by any import form this test knows."""
    paths = reached(ast.parse(source), {}, module=True)
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
        "from meridian.platform.evaluation.report import _helper\n",
        id="from-the-evaluation-package",
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
        "from meridian.platform import evaluation\nevaluation.report._helper\n",
        id="the-evaluation-package-as-an-attribute",
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
    pytest.param(
        f'from {WORKLOAD} import evaluation\ngetattr(evaluation, "_helper")\n',
        id="getattr-with-a-literal",
    ),
    pytest.param(
        f"from {WORKLOAD}.evaluation import *\n",
        id="star-import-of-a-watched-module",
    ),
    pytest.param("from .evaluation import *\n", id="relative-star-import"),
    pytest.param("from . import *\n", id="star-import-of-the-package-itself"),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "def use():\n    return evaluation._helper()\n",
        id="alias-used-inside-a-function",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "def other(evaluation):\n    return evaluation._helper\n"
        "def use():\n    return evaluation._helper()\n",
        id="another-functions-parameter-does-not-hide-the-use",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "def use():\n    return [evaluation._helper for _ in range(2)]\n",
        id="alias-used-inside-a-comprehension",
    ),
    pytest.param(
        f"import importlib\ndef use():\n"
        f'    m = importlib.import_module("{WORKLOAD}.evaluation")\n'
        "    return m._helper()\n",
        id="import-module-bound-inside-a-function",
    ),
    pytest.param(
        f"def use():\n    from {WORKLOAD} import evaluation\n"
        "    return evaluation._helper()\n",
        id="import-inside-a-function",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\nev = object()\nevaluation._helper()\n",
        id="a-rebinding-of-another-name-hides-nothing",
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
    pytest.param("from json import *\n", id="star-import-of-another-package"),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        'getattr(evaluation, "report")\ngetattr(evaluation, name)\n',
        id="getattr-of-a-public-name-or-a-non-literal",
    ),
    pytest.param(
        'import json\ngetattr(json, "_default_encoder")\n',
        id="getattr-of-another-packages-private-name",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "evaluation.__name__\nevaluation.__file__\nevaluation.__all__\n"
        "evaluation.__doc__\n",
        id="dunder-attributes",
    ),
    pytest.param(
        f"from {WORKLOAD}.evaluation import __doc__\n",
        id="dunder-name-in-a-from-import",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "def f(evaluation):\n    return evaluation._x\n",
        id="function-parameter-named-like-an-alias",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "def f(*evaluation):\n    return evaluation._x\n"
        "def g(**evaluation):\n    return evaluation._x\n"
        "def h(a, /, b, *, evaluation):\n    return evaluation._x\n",
        id="every-kind-of-parameter",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "async def f(evaluation):\n    return evaluation._x\n",
        id="async-function-parameter",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "def f(things):\n    evaluation = things.pop()\n    return evaluation._x\n",
        id="name-a-function-assigns",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "def f(things):\n    for evaluation in things:\n        evaluation._x()\n",
        id="loop-target-in-a-function",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "def f(things):\n    with things as evaluation:\n        evaluation._x()\n",
        id="with-target-in-a-function",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "def f():\n    try:\n        pass\n    except OSError as evaluation:\n"
        "        evaluation._x\n",
        id="except-name-in-a-function",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "x = [evaluation._x for evaluation in things]\n",
        id="comprehension-variable",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\nf = lambda evaluation: evaluation._x\n",
        id="lambda-parameter",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation\n"
        "def f():\n    def inner(evaluation):\n        return evaluation._x\n"
        "    return inner\n",
        id="nested-function-parameter",
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


def test_a_star_import_is_reported_as_the_star_of_its_module() -> None:
    found = private_imports(f"from {WORKLOAD}.evaluation import *\n")

    assert found == [f"{WORKLOAD}.evaluation.*"]


def test_the_injection_grader_imports_no_private_name_from_the_workload() -> None:
    found = private_imports(inspect.getsource(injection))

    assert found == []
