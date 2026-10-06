"""What the claim-brief package may import of the framework and of the network
(S037, F4, the security review's medium 2).

The host refuses a step whose class the framework defines (its agent executor, a
nested workflow, a function executor), so the framework's own steps cannot call a
model from inside a leg. It cannot refuse a step of the workload's own class that
HOLDS an agent or a chat client of the framework and calls it: the framework's
client would be asked, not the Model Gateway (hard rule 4). Workload code runs in
the runtime's process (ADR 2), so this is held where imports are held, by two
fences:

* the package imports four names of ``agent_framework`` (the ones the workflow
  uses) and nothing else of it: no other name, no submodule, no bare
  ``import agent_framework`` through which any class could be reached;
* the package imports no HTTP client, so a step cannot post to a model itself
  (the import contract in ``pyproject.toml`` holds the third-party ones, this
  test holds the standard library's too).

Composition is held only here and by review.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[4] / "src/meridian/workloads/claim_brief"
ALLOWED_FRAMEWORK_NAMES = frozenset(
    {"Executor", "WorkflowContext", "handler", "response_handler"}
)
HTTP_CLIENTS = ("httpx", "requests", "aiohttp", "urllib.request", "http.client")
# A way round an import statement: the package names no module by a string.
DYNAMIC_IMPORTS = ("importlib", "__import__")


def _is_under(name: str, roots: tuple[str, ...]) -> bool:
    return any(name == root or name.startswith(f"{root}.") for root in roots)


def _imports_of(node: ast.AST) -> list[str]:
    """The dotted names one import statement can bring in: a module, and for a
    ``from`` import each ``module.name`` too (``from urllib import request``)."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return [module, *(f"{module}.{alias.name}" for alias in node.names)]
    return []


def _framework_violations(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        # ``import agent_framework`` reaches every class by attribute.
        return [
            f"import {alias.name}"
            for alias in node.names
            if alias.name == "agent_framework"
            or alias.name.startswith("agent_framework.")
        ]
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module.startswith("agent_framework."):
            return [f"from {module} import ..."]
        if module == "agent_framework":
            return [
                f"from agent_framework import {alias.name}"
                for alias in node.names
                if alias.name not in ALLOWED_FRAMEWORK_NAMES
            ]
    return []


def violations(source: str) -> list[str]:
    """What ``source`` imports that the package may not."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        found += _framework_violations(node)
        found += [
            f"import of {name}"
            for name in _imports_of(node)
            if _is_under(name, HTTP_CLIENTS) or _is_under(name, DYNAMIC_IMPORTS)
        ]
        if isinstance(node, ast.Name) and node.id == "__import__":
            found.append("a call of __import__")
    return found


def test_the_package_imports_four_names_of_the_framework_and_no_http_client() -> None:
    paths = sorted(PACKAGE.glob("*.py"))

    found = {path.name: violations(path.read_text(encoding="utf-8")) for path in paths}

    assert paths
    assert {name: seen for name, seen in found.items() if seen} == {}


def test_the_workflow_imports_all_four_names_so_the_allowlist_is_not_slack() -> None:
    tree = ast.parse((PACKAGE / "workflow.py").read_text(encoding="utf-8"))

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "agent_framework"
        for alias in node.names
    }

    assert imported == ALLOWED_FRAMEWORK_NAMES


@pytest.mark.parametrize(
    "source",
    [
        "from agent_framework import Agent",
        "from agent_framework import Executor, BaseChatClient",
        "from agent_framework import *",
        "from agent_framework import Executor as Step, Agent as Helper",
        "from agent_framework.openai import OpenAIChatClient",
        "from agent_framework._workflows import Workflow",
        "import agent_framework",
        "import agent_framework as af",
        "import agent_framework.observability",
        "import httpx",
        "from httpx import Client",
        "import httpx as web",
        "import requests",
        "from requests import post",
        "import aiohttp",
        "import urllib.request",
        "from urllib import request",
        "from urllib.request import urlopen",
        "import http.client",
        "from http import client",
        "import importlib",
        "from importlib import import_module",
        "__import__('httpx')",
    ],
)
def test_the_check_refuses_what_the_package_may_not_import(source: str) -> None:
    assert violations(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "from agent_framework import Executor, WorkflowContext, handler",
        "from agent_framework import response_handler",
        "import json\nfrom dataclasses import dataclass",
        "from urllib.parse import quote",
        "import urllib.parse",
        "from http import HTTPStatus",
        "from meridian.runtime.hosts import AsyncModelClient",
    ],
)
def test_the_check_lets_the_allowed_imports_through(source: str) -> None:
    assert violations(source) == []
