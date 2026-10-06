"""The claim-brief package's names and import fences (S037, W1a).

The package imports the second framework and the runtime, a little of the
platform, and nothing of the Claims Triage App: its constant for the agent's ID
is held equal to the app's by a test, not by an import.
"""

import ast
import inspect
import subprocess
import sys
from pathlib import Path
from typing import get_args

from meridian.workloads.claim_brief import AGENT
from meridian.workloads.claim_brief import facts as facts_module
from meridian.workloads.claim_brief.workflow import build
from meridian.workloads.claims_triage.lifecycle import BRIEF_AGENT
from meridian.workloads.claims_triage.models import Peril

PACKAGE = Path(__file__).resolve().parents[4] / "src/meridian/workloads/claim_brief"
REPO_ROOT = PACKAGE.parents[3]
IMPORT_SECONDS = 120
# What the package may import of the platform, and nothing more.
PLATFORM_ALLOWED = {"meridian.platform.registry.models"}
FORBIDDEN_PREFIXES = (
    "meridian.workloads.claims_triage",
    "langgraph",
    "langchain",
    "openai",
    "azure",
    "anthropic",
    "mistralai",
    "boto3",
    "litellm",
)


def imported_modules() -> set[str]:
    """Every module the package's source files import, by name."""
    names: set[str] = set()
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{path.name} imports by a relative path"
                names.add(node.module or "")
    return names


def test_the_agent_s_id_is_the_one_the_claims_triage_app_starts() -> None:
    assert AGENT == BRIEF_AGENT == "claim-brief"


def test_the_package_s_perils_are_the_claims_perils() -> None:
    assert set(get_args(facts_module.Peril)) == set(get_args(Peril))


def test_the_entry_point_takes_a_model_and_tools_and_calls_nothing_when_built() -> None:
    signature = inspect.signature(build)

    assert list(signature.parameters) == ["model", "tools"]


def test_the_package_imports_the_framework_and_the_runtime_and_little_else() -> None:
    modules = imported_modules()

    assert any(name.startswith("agent_framework") for name in modules)
    assert any(name.startswith("meridian.runtime") for name in modules)
    for name in modules:
        assert not name.startswith(FORBIDDEN_PREFIXES), name
        if name.startswith("meridian.platform"):
            assert name in PLATFORM_ALLOWED, name


def test_importing_the_package_loads_nothing_of_the_app_and_no_provider_sdk() -> None:
    code = (
        "import sys\n"
        "import meridian.workloads.claim_brief.workflow\n"
        "watched = ('openai', 'azure', 'anthropic', 'mistralai', 'boto3', 'litellm')\n"
        "loaded = [m for m in sys.modules if m.split('.')[0] in watched]\n"
        "loaded += [m for m in sys.modules if m.startswith("
        "'meridian.workloads.claims_triage')]\n"
        "print('loaded:' + ','.join(sorted(loaded)))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=IMPORT_SECONDS,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "loaded:", completed.stdout


def test_the_package_has_its_readme() -> None:
    assert (PACKAGE / "README.md").is_file()
