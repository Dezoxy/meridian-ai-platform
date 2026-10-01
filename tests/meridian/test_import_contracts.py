"""Prove that the repository's real import contracts detect a violation.

Each case copies the repository's ``src/`` and ``pyproject.toml`` into a
temporary directory, adds one module that breaks a rule and runs
``lint-imports`` there. A contract that is missing, misspelled or too narrow
lets the probe through and fails the test, so the deliberate-violation check
does not depend on someone remembering to try one.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_MODULE = "meridian.platform._probe"
LINT_TIMEOUT_SECONDS = 60
SUMMARY = re.compile(r"Contracts: (?P<kept>\d+) kept, (?P<broken>\d+) broken")


@pytest.fixture
def project_copy(tmp_path: Path) -> Path:
    """A copy of the package sources and pyproject.toml, safe to add probes to."""
    shutil.copytree(
        REPO_ROOT / "src",
        tmp_path / "src",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def run_lint_imports(project: Path) -> tuple[int, str]:
    """Run lint-imports in the copy; return the exit code and all its output."""
    executable = shutil.which("lint-imports", path=str(Path(sys.executable).parent))
    if executable is None:
        pytest.fail(
            "lint-imports is not next to the test interpreter: uv sync --locked"
        )
    # The editable install points at the real src/. PYTHONPATH comes first on
    # sys.path, so the copy is the tree that gets analysed.
    src = str(project / "src")
    inherited = os.environ.get("PYTHONPATH")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [src, inherited]))}
    completed = subprocess.run(
        [executable, "--no-cache"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=LINT_TIMEOUT_SECONDS,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def add_probe(project: Path, source: str) -> None:
    probe = project / "src" / "meridian" / "platform" / "_probe.py"
    probe.write_text(source, encoding="utf-8")


def test_unmodified_copy_passes_with_at_least_one_kept_contract(
    project_copy: Path,
) -> None:
    # Arrange: the fixture yields the repository's sources and config unchanged.

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert: exit 0 alone is not enough, lint-imports also exits 0 with no contracts.
    assert exit_code == 0, output
    summary = SUMMARY.search(output)
    assert summary is not None, output
    assert int(summary["kept"]) >= 1, f"no contract is enforced:\n{output}"


@pytest.mark.parametrize(
    ("probe_source", "forbidden_module"),
    [
        pytest.param(
            "import langgraph\n", "langgraph", id="platform-imports-langgraph"
        ),
        pytest.param(
            "from langgraph.graph import StateGraph\n",
            "langgraph",
            id="platform-imports-a-langgraph-submodule",
        ),
        pytest.param(
            "import langchain\n", "langchain", id="platform-imports-langchain"
        ),
        pytest.param(
            "import langchain_core\n",
            "langchain_core",
            id="platform-imports-langchain-core",
        ),
        # ADR 2 says langchain*, but import-linter wildcards replace whole
        # module names only, so each distribution must be listed. These two
        # are the ones a gateway or retrieval module would reach for.
        pytest.param(
            "from langchain_openai import ChatOpenAI\n",
            "langchain_openai",
            id="platform-imports-langchain-openai",
        ),
        pytest.param(
            "from langchain_community.vectorstores import PGVector\n",
            "langchain_community",
            id="platform-imports-langchain-community",
        ),
        pytest.param(
            "import meridian.workloads\n",
            "meridian.workloads",
            id="platform-imports-workloads",
        ),
        pytest.param(
            "import meridian.runtime\n",
            "meridian.runtime",
            id="platform-imports-runtime",
        ),
    ],
)
def test_platform_import_of_forbidden_module_breaks_a_contract(
    project_copy: Path, probe_source: str, forbidden_module: str
) -> None:
    # Arrange
    add_probe(project_copy, probe_source)

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert: a config error would also exit non-zero, so require the import itself.
    assert exit_code != 0, output
    assert f"{PROBE_MODULE} -> {forbidden_module}" in output, output
