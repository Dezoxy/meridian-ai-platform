"""The shape of the injection grader's imports (S061, S076).

The grader reads another module's private name only by accident of history; a
rename there would then break it without a public interface to point at.

``private_imports`` is a reading aid for this one test, not a proof. What it
reads: ``from X import _n`` (with ``as``, relative), a private module in an
imported path, ``import a.b._x``, a module bound by ``import``, ``import ... as``
or ``from p import m [as x]`` and then used as ``x._y``, ``importlib.import_module``
and ``__import__`` with a string literal (as the first argument or as ``name=``,
also through an alias, or assigned to a name first), ``getattr(module, "_x")``
with a literal, and ``from X import *`` of a watched module. A star import is
reported because the test cannot know without importing the module that its
``__all__`` lists no private name, and the star hides every later use. A dunder
(``__name__``, ``__file__``, ``__all__``, ``__doc__``) is not a private name.

Scope: a function parameter, a name a function assigns (plain, annotated,
walrus or chained), a ``for``/``with``/``except`` target in it, and a
comprehension's or lambda's own names shadow a module-level alias inside that
scope. A class body is a scope of its own, and, as in Python, its names are not
seen by the methods in it, which see the aliases of the scope around the class.
Annotations of parameters, the return annotation, type-parameter bounds,
defaults, decorators and a class's bases are read in the scope around the
definition. An assignment of an ``import_module`` result (plain, annotated,
walrus or chained) makes a local alias, and assignments are taken in source
order, so ``b = a`` after ``a = import_module(...)`` is followed. Module level
never forgets an alias, so a rebinding (``ev = object()`` after the import) is
still flagged: a harmless false positive.

What it cannot see: a non-literal argument of ``import_module``, ``__import__``
or ``getattr``; ``__import__(..., fromlist=[...])``; ``builtins.__import__``;
``sys.modules[...]``; an attribute reached through any call but a literal
``import_module``; an alias made by tuple unpacking or stored on an object
(``self.ev = evaluation``); a private name reached by a string through
``vars()``, ``__dict__`` or ``setattr``; ``global`` and ``nonlocal``; ``eval``
and ``exec``. False positives besides the rebinding: a name a ``match`` pattern
captures does not shadow the alias, and a walrus inside a comprehension binds in
the function around it but is not seen there, so the alias stays. A private name
of a package nobody watches is ignored by design.
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
SCOPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    *COMPREHENSIONS,
)
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
    if isinstance(
        scope, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ):
        return list(scope.body)
    if isinstance(scope, ast.Lambda):
        return [scope.body]
    if isinstance(scope, ast.DictComp):
        return [scope.key, scope.value, *scope.generators]
    assert isinstance(scope, ast.ListComp | ast.SetComp | ast.GeneratorExp)
    return [scope.elt, *scope.generators]


def type_parameter_parts(scope: ast.AST) -> list[ast.AST]:
    """The bounds and defaults of a definition's type parameters."""
    parts = []
    for param in getattr(scope, "type_params", []):
        parts.extend(
            [getattr(param, "bound", None), getattr(param, "default_value", None)]
        )
    return [part for part in parts if part is not None]


def annotations(args: ast.arguments) -> list[ast.AST]:
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return [arg.annotation for arg in every if arg is not None and arg.annotation]


def enclosing_parts(scope: ast.AST) -> list[ast.AST]:
    """What a nested scope's node evaluates in the scope around it: decorators,
    defaults, annotations, the return annotation, type-parameter bounds and a
    class's bases of a definition, the first loop's iterable of a comprehension."""
    if isinstance(scope, ast.ClassDef):
        return [
            *scope.decorator_list,
            *scope.bases,
            *scope.keywords,
            *type_parameter_parts(scope),
        ]
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        args = scope.args
        defaults = [*args.defaults, *(d for d in args.kw_defaults if d is not None)]
        returns = getattr(scope, "returns", None)
        return [
            *getattr(scope, "decorator_list", []),
            *defaults,
            *annotations(args),
            *([returns] if returns is not None else []),
            *type_parameter_parts(scope),
        ]
    assert isinstance(scope, COMPREHENSIONS)
    return [scope.generators[0].iter]


def own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a scope's own body: a scope nested in it is yielded, but
    only the parts of it that run in this scope are walked."""
    # Pre-order, in source order: the stack is filled in reverse.
    stack = scope_roots(scope)[::-1]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, SCOPES):
            stack.extend(enclosing_parts(node)[::-1])
        else:
            stack.extend(list(ast.iter_child_nodes(node))[::-1])


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
    named = [keyword.value for keyword in call.keywords if keyword.arg == "name"]
    given = [*call.args[:1], *named[:1]]
    if not is_import or not given:
        return None
    first = given[0]
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


def assignment(node: ast.AST) -> tuple[list[ast.expr], ast.expr | None]:
    """The targets and the value of an assignment (plain, chained, annotated
    with a value, or walrus); no targets for any other node."""
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign | ast.NamedExpr):
        return [node.target], node.value
    return [], None


def scope_bindings(scope: ast.AST, outer: Bindings, *, module: bool) -> Bindings:
    """What the names of ``scope`` stand for: the outer bindings, minus the
    names the scope binds to anything else (never at module level, which keeps
    every alias), plus its own imports and the ``import_module`` results it
    assigns to a name."""
    nodes = list(own_nodes(scope))
    shadowed = parameters(scope) | (set() if module else stored_names(nodes))
    bound = {name: path for name, path in outer.items() if name not in shadowed}
    bound.update(import_bindings(nodes))
    for targets, value in (assignment(node) for node in nodes):
        owner = resolve(value, bound) if value is not None else None
        if owner is not None:
            bound.update({t.id: owner for t in targets if isinstance(t, ast.Name)})
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
    """The paths a scope and the scopes nested in it import or reach. A class
    body does not enclose the scopes nested in it: its methods see the bindings
    around the class, not the class's own."""
    bound = scope_bindings(scope, outer, module=module)
    seen_by_nested = outer if isinstance(scope, ast.ClassDef) else bound
    paths: set[str] = set()
    for node in own_nodes(scope):
        paths.update(paths_of(node, bound))
        if isinstance(node, SCOPES):
            paths.update(reached(node, seen_by_nested))
    return paths


def private_imports(source: str) -> list[str]:
    """Every private name of a watched module that ``source`` imports or
    reaches, by any import form this test knows."""
    paths = reached(ast.parse(source), {}, module=True)
    return sorted(path for path in paths if private_part(path) is not None)


EVALUATION = f"{WORKLOAD}.evaluation"
FROM_WORKLOAD = f"from {WORKLOAD} import evaluation\n"
USE = "    return evaluation._x\n"

PLANTED = [
    pytest.param(
        f"from {WORKLOAD}.evaluation import _helper\n",
        f"{EVALUATION}._helper",
        id="from-import",
    ),
    pytest.param(
        f"from {WORKLOAD}.evaluation import _helper as helper\n",
        f"{EVALUATION}._helper",
        id="from-import-renamed",
    ),
    pytest.param(
        "from .evaluation import _helper\n",
        ".evaluation._helper",
        id="relative-from-import",
    ),
    pytest.param(
        f"from {WORKLOAD}._hidden import helper\n",
        f"{WORKLOAD}._hidden.helper",
        id="from-a-private-module",
    ),
    pytest.param(
        "from meridian.platform.guardrails.screening import _normalise\n",
        "meridian.platform.guardrails.screening._normalise",
        id="from-the-screening-module",
    ),
    pytest.param(
        "from meridian.platform.evaluation.report import _helper\n",
        "meridian.platform.evaluation.report._helper",
        id="from-the-evaluation-package",
    ),
    pytest.param(
        f"import {WORKLOAD}.evaluation._helper\n",
        f"{EVALUATION}._helper",
        id="plain-import-of-a-private-module",
    ),
    pytest.param(
        f"import {WORKLOAD}.evaluation\n{WORKLOAD}.evaluation._helper()\n",
        f"{EVALUATION}._helper",
        id="plain-import-then-attribute",
    ),
    pytest.param(
        f"import {WORKLOAD}.evaluation as ev\nev._helper()\n",
        f"{EVALUATION}._helper",
        id="plain-import-with-alias",
    ),
    pytest.param(
        f"from {WORKLOAD} import evaluation as ev\nev._helper()\n",
        f"{EVALUATION}._helper",
        id="from-import-of-a-module-with-alias",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}evaluation._helper()\n",
        f"{EVALUATION}._helper",
        id="from-import-of-a-module",
    ),
    pytest.param(
        "from . import evaluation as ev\nev._helper()\n",
        ".evaluation._helper",
        id="relative-module-with-alias",
    ),
    pytest.param(
        "from meridian.platform.guardrails import screening\nscreening._normalise\n",
        "meridian.platform.guardrails.screening._normalise",
        id="the-screening-module-as-an-attribute",
    ),
    pytest.param(
        "from meridian.platform import evaluation\nevaluation.report._helper\n",
        "meridian.platform.evaluation.report._helper",
        id="the-evaluation-package-as-an-attribute",
    ),
    pytest.param(
        f'import importlib\nimportlib.import_module("{WORKLOAD}._hidden")\n',
        f"{WORKLOAD}._hidden",
        id="import-module-with-a-literal",
    ),
    pytest.param(
        'import importlib\nimportlib.import_module(".evaluation._helper")\n',
        ".evaluation._helper",
        id="import-module-with-a-relative-literal",
    ),
    pytest.param(
        f'import importlib as il\nil.import_module("{WORKLOAD}._hidden")\n',
        f"{WORKLOAD}._hidden",
        id="import-module-through-an-alias-of-importlib",
    ),
    pytest.param(
        "from importlib import import_module as load\n"
        f'load("{WORKLOAD}.evaluation._helper")\n',
        f"{EVALUATION}._helper",
        id="import-module-through-an-alias-of-the-function",
    ),
    pytest.param(
        f'import importlib\nm = importlib.import_module("{EVALUATION}")\nm._helper()\n',
        f"{EVALUATION}._helper",
        id="import-module-bound-then-attribute",
    ),
    pytest.param(
        f'__import__("{WORKLOAD}._hidden")\n',
        f"{WORKLOAD}._hidden",
        id="dunder-import-with-a-literal",
    ),
    pytest.param(
        f'{FROM_WORKLOAD}getattr(evaluation, "_helper")\n',
        f"{EVALUATION}._helper",
        id="getattr-with-a-literal",
    ),
    pytest.param(
        f"from {WORKLOAD}.evaluation import *\n",
        f"{EVALUATION}.*",
        id="star-import-of-a-watched-module",
    ),
    pytest.param(
        "from .evaluation import *\n", ".evaluation.*", id="relative-star-import"
    ),
    pytest.param("from . import *\n", ".*", id="star-import-of-the-package-itself"),
    pytest.param(
        f"{FROM_WORKLOAD}def use():\n    return evaluation._helper()\n",
        f"{EVALUATION}._helper",
        id="alias-used-inside-a-function",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}"
        "def other(evaluation):\n    return evaluation._helper\n"
        "def use():\n    return evaluation._helper()\n",
        f"{EVALUATION}._helper",
        id="another-functions-parameter-does-not-hide-the-use",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}"
        "def use():\n    return [evaluation._helper for _ in range(2)]\n",
        f"{EVALUATION}._helper",
        id="alias-used-inside-a-comprehension",
    ),
    pytest.param(
        f"import importlib\ndef use():\n"
        f'    m = importlib.import_module("{EVALUATION}")\n'
        "    return m._helper()\n",
        f"{EVALUATION}._helper",
        id="import-module-bound-inside-a-function",
    ),
    pytest.param(
        f"def use():\n    from {WORKLOAD} import evaluation\n"
        "    return evaluation._helper()\n",
        f"{EVALUATION}._helper",
        id="import-inside-a-function",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}ev = object()\nevaluation._helper()\n",
        f"{EVALUATION}._helper",
        id="a-rebinding-of-another-name-hides-nothing",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f(x: evaluation._T):\n    pass\n",
        f"{EVALUATION}._T",
        id="annotation-of-a-parameter",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f(a: evaluation._A, /, b):\n    pass\n",
        f"{EVALUATION}._A",
        id="annotation-of-a-positional-only-parameter",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f(*c: evaluation._B):\n    pass\n",
        f"{EVALUATION}._B",
        id="annotation-of-a-star-parameter",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f(*, d: evaluation._C):\n    pass\n",
        f"{EVALUATION}._C",
        id="annotation-of-a-keyword-only-parameter",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f(**e: evaluation._D):\n    pass\n",
        f"{EVALUATION}._D",
        id="annotation-of-a-double-star-parameter",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f() -> evaluation._T:\n    pass\n",
        f"{EVALUATION}._T",
        id="return-annotation",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}async def f() -> evaluation._T:\n    pass\n",
        f"{EVALUATION}._T",
        id="return-annotation-of-an-async-function",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f[T: evaluation._T]():\n    pass\n",
        f"{EVALUATION}._T",
        id="type-parameter-bound",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f(evaluation: evaluation._T):\n{USE}",
        f"{EVALUATION}._T",
        id="a-parameters-annotation-is-read-before-the-parameter-shadows",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}class A(evaluation._Base):\n    pass\n",
        f"{EVALUATION}._Base",
        id="base-of-a-class",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}@evaluation._deco\nclass A:\n    pass\n",
        f"{EVALUATION}._deco",
        id="decorator-of-a-class",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f():\n    class A:\n        evaluation = 1\n{USE}",
        f"{EVALUATION}._x",
        id="a-class-body-name-does-not-shadow-the-function-around-it",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}class A:\n    evaluation = 1\n    def m(self):\n    {USE}",
        f"{EVALUATION}._x",
        id="a-method-sees-the-alias-not-the-class-attribute",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}class A:\n    def m(self):\n        return evaluation._x\n",
        f"{EVALUATION}._x",
        id="alias-used-in-a-method",
    ),
    pytest.param(
        "import importlib\ndef f():\n"
        f'    a = importlib.import_module("{EVALUATION}")\n'
        "    b = a\n    return b._x\n",
        f"{EVALUATION}._x",
        id="an-alias-of-an-alias-in-a-function",
    ),
    pytest.param(
        f'import importlib\na = importlib.import_module("{EVALUATION}")\n'
        "b = a\nc = b\nc._x\n",
        f"{EVALUATION}._x",
        id="a-chain-of-aliases-at-module-level",
    ),
    pytest.param(
        f'import importlib\nm: object = importlib.import_module("{EVALUATION}")\n'
        "m._x\n",
        f"{EVALUATION}._x",
        id="annotated-assignment-of-import-module",
    ),
    pytest.param(
        f'import importlib\n(m := importlib.import_module("{EVALUATION}"))\nm._x\n',
        f"{EVALUATION}._x",
        id="walrus-assignment-of-import-module",
    ),
    pytest.param(
        f'import importlib\na = b = importlib.import_module("{EVALUATION}")\nb._x\n',
        f"{EVALUATION}._x",
        id="chained-assignment-of-import-module",
    ),
    pytest.param(
        f'import importlib\nimportlib.import_module(name="{WORKLOAD}._hidden")\n',
        f"{WORKLOAD}._hidden",
        id="import-module-with-the-name-keyword",
    ),
    pytest.param(
        f'__import__(name="{WORKLOAD}._hidden")\n',
        f"{WORKLOAD}._hidden",
        id="dunder-import-with-the-name-keyword",
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
        f'{FROM_WORKLOAD}getattr(evaluation, "report")\ngetattr(evaluation, name)\n',
        id="getattr-of-a-public-name-or-a-non-literal",
    ),
    pytest.param(
        'import json\ngetattr(json, "_default_encoder")\n',
        id="getattr-of-another-packages-private-name",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}"
        "evaluation.__name__\nevaluation.__file__\nevaluation.__all__\n"
        "evaluation.__doc__\n",
        id="dunder-attributes",
    ),
    pytest.param(
        f"from {WORKLOAD}.evaluation import __doc__\n",
        id="dunder-name-in-a-from-import",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f(evaluation):\n{USE}",
        id="function-parameter-named-like-an-alias",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}"
        "def f(*evaluation):\n    return evaluation._x\n"
        "def g(**evaluation):\n    return evaluation._x\n"
        "def h(a, /, b, *, evaluation):\n    return evaluation._x\n",
        id="every-kind-of-parameter",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}async def f(evaluation):\n{USE}",
        id="async-function-parameter",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}"
        "def f(things):\n    evaluation = things.pop()\n    return evaluation._x\n",
        id="name-a-function-assigns",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f():\n    evaluation: int = 1\n{USE}",
        id="annotated-name-a-function-assigns",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f(t):\n"
        "    if (evaluation := t):\n        return evaluation._x\n",
        id="walrus-name-in-a-function",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}"
        "def f(things):\n    for evaluation in things:\n        evaluation._x()\n",
        id="loop-target-in-a-function",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}"
        "def f(things):\n    with things as evaluation:\n        evaluation._x()\n",
        id="with-target-in-a-function",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}"
        "def f():\n    try:\n        pass\n    except OSError as evaluation:\n"
        "        evaluation._x\n",
        id="except-name-in-a-function",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}x = [evaluation._x for evaluation in things]\n",
        id="comprehension-variable",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}f = lambda evaluation: evaluation._x\n",
        id="lambda-parameter",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}"
        "def f():\n    def inner(evaluation):\n        return evaluation._x\n"
        "    return inner\n",
        id="nested-function-parameter",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}class A:\n    evaluation = 1\n    y = evaluation._x\n",
        id="class-body-name-shadows-inside-the-class-body",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}def f(x: evaluation.Public):\n    pass\n",
        id="annotation-naming-a-public-name",
    ),
    pytest.param(
        "import importlib\nimportlib.import_module(name=other)\n",
        id="import-module-with-a-non-literal-keyword",
    ),
    pytest.param(
        'import importlib\nimportlib.import_module(name="json._hidden")\n',
        id="import-module-keyword-of-another-package",
    ),
]

# What the module docstring lists as unseen. A reader who makes the walker see
# one of these changes this test and the docstring together.
UNSEEN = [
    pytest.param(
        f'{FROM_WORKLOAD}vars(evaluation)["_x"]\n',
        id="a-private-name-through-vars",
    ),
    pytest.param(
        f'{FROM_WORKLOAD}evaluation.__dict__["_x"]\n',
        id="a-private-name-through-dict",
    ),
    pytest.param(
        f'{FROM_WORKLOAD}setattr(evaluation, "_x", 1)\n',
        id="a-private-name-through-setattr",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}class A:\n    def f(self):\n"
        "        self.ev = evaluation\n        return self.ev._x\n",
        id="an-alias-stored-on-an-object",
    ),
    pytest.param(
        f"{FROM_WORKLOAD}a, b = evaluation, 1\na._x\n",
        id="an-alias-made-by-tuple-unpacking",
    ),
]


@pytest.mark.parametrize(("source", "expected"), PLANTED)
def test_a_private_name_is_found_in_every_form_it_can_be_imported_in(
    source: str, expected: str
) -> None:
    found = private_imports(source)

    assert found == [expected]


@pytest.mark.parametrize("source", ALLOWED)
def test_a_public_name_or_another_packages_private_one_is_not_found(
    source: str,
) -> None:
    found = private_imports(source)

    assert found == []


@pytest.mark.parametrize("source", UNSEEN)
def test_what_the_docstring_lists_as_unseen_is_unseen(source: str) -> None:
    found = private_imports(source)

    assert found == []


def test_a_walrus_in_a_comprehension_is_a_listed_false_positive() -> None:
    source = f"{FROM_WORKLOAD}def f(t):\n    [(evaluation := a) for a in t]\n{USE}"

    found = private_imports(source)

    assert found == [f"{EVALUATION}._x"]


def test_a_star_import_is_reported_as_the_star_of_its_module() -> None:
    found = private_imports(f"from {WORKLOAD}.evaluation import *\n")

    assert found == [f"{WORKLOAD}.evaluation.*"]


def test_the_injection_grader_imports_no_private_name_from_the_workload() -> None:
    found = private_imports(inspect.getsource(injection))

    assert found == []
