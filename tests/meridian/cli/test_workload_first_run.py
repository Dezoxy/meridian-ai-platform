"""The step's "done when": a developer's first run of ``meridian workload new``.

The real flow, in an installed copy of this tree: scaffold a workload, then
validate, lint, run and test it with the commands the scaffold prints. Entry
points are read from the installed distribution's metadata, so a ``PYTHONPATH``
copy (what ``tests/meridian/test_import_contracts.py`` does) cannot prove it: the
copy needs an install of its own, with the new ``pyproject.toml``. Every command
is ``uv run --locked --offline`` in the copy, which creates the copy's own
``.venv`` and reinstalls the copy when ``pyproject.toml`` changed.

``--offline`` works because the environment the tests run in was installed from
the same lock, so uv's cache holds every wheel. A cold cache fails here for that
reason, not because of the scaffold.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from meridian.platform.cli.evaluation import NO_CASES
from meridian.platform.registry.loader import load_registry

REPO = Path(__file__).resolve().parents[3]
NAME = "first-run-probe"
MODULE = "first_run_probe"
TIMEOUT_SECONDS = 300
AGENTS = "config/registry/agents.yaml"
PYPROJECT = "pyproject.toml"
WORKLOAD_SOURCE = f"src/meridian/workloads/{MODULE}"
WORKLOAD_TESTS = f"tests/meridian/workloads/{MODULE}"
# Variables that would put the real tree, or its environment, on the copy's path.
DROPPED = ("VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT")
AGENT_COUNT = re.compile(r"(\d+) agents?")
SUMMARY = re.compile(r"Contracts: (?P<kept>\d+) kept, (?P<broken>\d+) broken")

LOAD_GRAPHS = f"""
from pathlib import Path
from meridian.platform.registry.loader import load_registry
from meridian.runtime.graphs import load_graph_factory

registry = load_registry(Path('config/registry'))
claims = load_graph_factory('claims-triage', registry)
factory = load_graph_factory('{NAME}', registry)
where = Path(factory.__code__.co_filename).resolve()
inside = where.is_relative_to(Path.cwd().resolve())
output = factory(None, None).compile().invoke({{'request': {{}}}})['output']
print('graph:', claims.__name__, inside, output)
"""
EXPECTED_GRAPH = "graph: build True {'agent': 'first-run-probe'}"
LOAD_EVALUATION = f"""
import sys
from meridian.platform.evaluation.workload import load_evaluation
load_evaluation('{NAME}')
framework = sorted(
    name for name in sys.modules
    if name in ('langgraph', 'langgraph_sdk', 'langchain')
    or name.startswith(('langgraph.', 'langchain'))
)
print('framework:' + ','.join(framework))
"""


def find_uv() -> str:
    uv = os.environ.get("UV") or shutil.which("uv")
    if uv is None:
        pytest.fail("uv is not on the path: this test installs a copy with it")
    return uv


def copy_of_the_tree(destination: Path) -> None:
    shutil.copytree(
        REPO / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(REPO / "config" / "registry", destination / "config" / "registry")
    for file in (PYPROJECT, "uv.lock", ".python-version"):
        shutil.copy(REPO / file, destination / file)


def agent_count(stdout: str) -> int:
    found = AGENT_COUNT.search(stdout)
    assert found is not None, stdout
    return int(found.group(1))


def test_a_new_workload_validates_lints_runs_and_tests_in_an_installed_copy(
    tmp_path: Path,
) -> None:
    # Arrange
    uv = find_uv()
    tree = tmp_path / "tree"
    copy_of_the_tree(tree)
    environment = {k: v for k, v in os.environ.items() if k not in DROPPED}
    repo_before = {path: (REPO / path).read_bytes() for path in (PYPROJECT, AGENTS)}
    repo_agents = len(load_registry(REPO / "config" / "registry").agents)

    def run(label: str, *command: str, expect: int = 0) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            [uv, "run", "--locked", "--offline", *command],
            cwd=tree,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
        )
        assert completed.returncode == expect, (
            f"{label}: exit {completed.returncode}, expected {expect}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
        return completed

    # Act and assert, in the order a developer takes them.
    run("1 workload new", "meridian", "workload", "new", NAME)
    after_first = {path: (tree / path).read_bytes() for path in (PYPROJECT, AGENTS)}

    validated = run("2 registry validate", "meridian", "registry", "validate")
    assert agent_count(validated.stdout) == repo_agents + 1, validated.stdout

    linted = run("3 lint-imports", "lint-imports", "--no-cache")
    summary = SUMMARY.search(linted.stdout + linted.stderr)
    assert summary is not None, linted.stdout
    assert int(summary["kept"]) >= 1, linted.stdout
    assert int(summary["broken"]) == 0, linted.stdout

    paths = (WORKLOAD_SOURCE, WORKLOAD_TESTS)
    run("4a ruff check", "ruff", "check", *paths)
    run("4b ruff format --check", "ruff", "format", "--check", *paths)

    graph = run("5 graph factory", "python", "-c", LOAD_GRAPHS)
    assert graph.stdout.strip() == EXPECTED_GRAPH, graph.stdout

    evaluation = run("6 load evaluation", "python", "-c", LOAD_EVALUATION)
    assert evaluation.stdout.strip() == "framework:", evaluation.stdout

    evaluated = run(
        "7 eval run",
        "meridian",
        "eval",
        "run",
        "--workload",
        NAME,
        "--golden-set",
        f"data/evaluation/{NAME}/golden",
        "--base-url",
        "http://127.0.0.1:9",
        "--report",
        "report.json",
    )
    assert evaluated.stdout == f"{NO_CASES}\neval run: passed\n", evaluated.stdout
    assert "ERROR" not in evaluated.stderr, evaluated.stderr
    assert not (tree / "report.json").exists()

    tested = run("8 pytest", "pytest", WORKLOAD_TESTS, "-q", "-p", "no:cacheprovider")
    assert "3 passed" in tested.stdout, tested.stdout

    run("9 workload new again", "meridian", "workload", "new", NAME, expect=2)
    for path, content in after_first.items():
        assert (tree / path).read_bytes() == content, path

    # The repository's own tree is what it was.
    for path, content in repo_before.items():
        assert (REPO / path).read_bytes() == content, path
