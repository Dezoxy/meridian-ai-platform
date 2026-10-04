"""Prove that the repository's real import contracts detect a violation.

Each case copies the repository's ``src/`` and ``pyproject.toml`` into a
temporary directory, adds one module that breaks a rule and runs
``lint-imports`` there. A contract that is missing, misspelled or too narrow
lets the probe through and fails the test, so the deliberate-violation check
does not depend on someone remembering to try one.
"""

import ast
import os
import re
import shutil
import subprocess
import sys
import tomllib
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
            "from langgraph.checkpoint.postgres import PostgresSaver\n",
            "langgraph",
            id="platform-imports-the-langgraph-postgres-checkpointer",
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


# ── provider SDKs belong to the gateway's adapter (hard rule 4, T-19) ─────────
SDK_CONTRACT = "only the gateway imports a provider SDK (hard rule 4, T-19)"
ADAPTER_CONTRACT = (
    "inside the gateway only the Azure OpenAI adapter imports a provider SDK"
)
ADAPTER_MODULE = "meridian.platform.gateway.providers.azure_openai"
GATEWAY_PACKAGE = "meridian.platform.gateway"


def add_probe_in(project: Path, package: str, source: str) -> str:
    """Write ``_probe.py`` into ``package`` of the copy; return its module name."""
    folder = project / "src" / Path(*package.split("."))
    (folder / "_probe.py").write_text(source, encoding="utf-8")
    return f"{package}._probe"


@pytest.mark.parametrize(
    ("package", "sdk"),
    [
        pytest.param("meridian.runtime", "openai", id="runtime-imports-openai"),
        pytest.param("meridian.workloads", "openai", id="workloads-import-openai"),
        pytest.param(
            "meridian.platform.registry", "openai", id="registry-imports-openai"
        ),
        pytest.param("meridian.platform.common", "openai", id="common-imports-openai"),
        pytest.param("meridian.platform.cli", "openai", id="cli-imports-openai"),
        pytest.param(
            "meridian.platform.policy_mcp", "openai", id="policy-mcp-imports-openai"
        ),
        pytest.param(
            "meridian.platform.toolserver", "openai", id="toolserver-imports-openai"
        ),
        pytest.param(
            "meridian.platform.migrations", "openai", id="migrations-import-openai"
        ),
        pytest.param("meridian.runtime", "anthropic", id="runtime-imports-anthropic"),
        pytest.param("meridian.runtime", "mistralai", id="runtime-imports-mistralai"),
        pytest.param("meridian.runtime", "boto3", id="runtime-imports-boto3"),
        pytest.param("meridian.runtime", "botocore", id="runtime-imports-botocore"),
        pytest.param("meridian.runtime", "litellm", id="runtime-imports-litellm"),
    ],
)
def test_an_import_of_a_provider_sdk_outside_the_gateway_breaks_a_contract(
    project_copy: Path, package: str, sdk: str
) -> None:
    # Arrange
    probe = add_probe_in(project_copy, package, f"import {sdk}\n")

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert SDK_CONTRACT in output, output
    assert f"{probe} -> {sdk}" in output, output


def test_a_chain_to_the_sdk_through_the_gateway_adapter_breaks_the_contract(
    project_copy: Path,
) -> None:
    # Arrange: the probe imports the adapter, which imports openai.
    probe = add_probe_in(project_copy, "meridian.runtime", f"import {ADAPTER_MODULE}\n")

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert: indirect imports count for everything outside the gateway.
    assert exit_code != 0, output
    assert f"{probe} -> {ADAPTER_MODULE}" in output, output
    assert f"{ADAPTER_MODULE} -> openai" in output, output


@pytest.mark.parametrize(
    "sdk", ["anthropic", "mistralai", "boto3", "botocore", "litellm"]
)
@pytest.mark.parametrize(
    "package",
    [
        pytest.param(GATEWAY_PACKAGE, id="gateway"),
        pytest.param(f"{GATEWAY_PACKAGE}.providers", id="another-provider-module"),
    ],
)
def test_a_gateway_module_other_than_the_adapter_importing_another_sdk_breaks_it(
    project_copy: Path, package: str, sdk: str
) -> None:
    # Arrange: walk.py is such a module; it must not reach for another SDK.
    probe = add_probe_in(project_copy, package, f"import {sdk}\n")

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert ADAPTER_CONTRACT in output, output
    assert f"{probe} -> {sdk}" in output, output


@pytest.mark.parametrize(
    ("package", "source"),
    [
        pytest.param(GATEWAY_PACKAGE, "import openai\n", id="gateway-imports-openai"),
        pytest.param(
            GATEWAY_PACKAGE,
            "from openai.types.chat import ChatCompletion\n",
            id="gateway-imports-an-openai-submodule",
        ),
        pytest.param(
            f"{GATEWAY_PACKAGE}.providers",
            "import openai\n",
            id="another-provider-module-imports-openai",
        ),
    ],
)
def test_a_gateway_module_other_than_the_adapter_importing_openai_breaks_a_contract(
    project_copy: Path, package: str, source: str
) -> None:
    # Arrange
    probe = add_probe_in(project_copy, package, source)

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert ADAPTER_CONTRACT in output, output
    assert f"{probe} -> openai" in output, output


# `azure` is a namespace package. import-linter names an external package by its
# top-level module and refuses a subpackage ("subpackages of external packages
# are not valid"), so one forbidden module, `azure`, is what catches
# `azure.identity`, `azure.core` and every other `azure.*` distribution; the
# report names the edge `<module> -> azure`.
AZURE_IMPORTS = [
    pytest.param("import azure.identity\n", id="import-azure-identity"),
    pytest.param(
        "from azure.identity import AzureCliCredential\n", id="from-azure-identity"
    ),
    pytest.param("import azure.core.exceptions\n", id="import-azure-core"),
    pytest.param(
        "from azure.core.exceptions import AzureError\n", id="from-azure-core"
    ),
]


@pytest.mark.parametrize("source", AZURE_IMPORTS)
@pytest.mark.parametrize(
    "package",
    [
        pytest.param("meridian.runtime", id="runtime"),
        pytest.param("meridian.workloads", id="workloads"),
        pytest.param("meridian.platform.common", id="common"),
        pytest.param("meridian.platform.registry", id="registry"),
    ],
)
def test_an_azure_import_outside_the_gateway_breaks_the_sdk_contract(
    project_copy: Path, package: str, source: str
) -> None:
    # Arrange
    probe = add_probe_in(project_copy, package, source)

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert SDK_CONTRACT in output, output
    assert f"{probe} -> azure" in output, output


@pytest.mark.parametrize("source", AZURE_IMPORTS)
@pytest.mark.parametrize(
    "package",
    [
        pytest.param(GATEWAY_PACKAGE, id="gateway"),
        pytest.param(f"{GATEWAY_PACKAGE}.providers", id="another-provider-module"),
    ],
)
def test_an_azure_import_in_a_gateway_module_other_than_the_adapter_breaks_a_contract(
    project_copy: Path, package: str, source: str
) -> None:
    # Arrange
    probe = add_probe_in(project_copy, package, source)

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert ADAPTER_CONTRACT in output, output
    assert f"{probe} -> azure" in output, output


def test_a_chain_to_azure_through_the_adapter_breaks_the_sdk_contract(
    project_copy: Path,
) -> None:
    # Arrange: the probe imports the adapter, which imports azure.
    probe = add_probe_in(project_copy, "meridian.runtime", f"import {ADAPTER_MODULE}\n")

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert f"{ADAPTER_MODULE} -> azure" in output, output
    assert f"{probe} -> {ADAPTER_MODULE}" in output, output


@pytest.mark.parametrize(
    "init",
    [
        pytest.param(Path("src/meridian/__init__.py"), id="meridian"),
        pytest.param(Path("src/meridian/platform/__init__.py"), id="platform"),
    ],
)
def test_the_package_inits_that_no_contract_lists_import_nothing(init: Path) -> None:
    # They are in no contract's source_modules, so an import there is unchecked.
    tree = ast.parse((REPO_ROOT / init).read_text(encoding="utf-8"))

    imports = [
        node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)
    ]

    assert imports == []


# ── the gateway does not depend on the evaluation or the CLI (S050, T-78) ─────
GATEWAY_ISOLATION_CONTRACT = (
    "the gateway imports neither the evaluation package nor the CLI"
)


@pytest.mark.parametrize(
    "forbidden",
    [
        "meridian.platform.evaluation",
        "meridian.platform.evaluation.report",
        "meridian.platform.cli",
        "meridian.platform.cli.evaluation",
    ],
)
@pytest.mark.parametrize(
    "package",
    [
        pytest.param(GATEWAY_PACKAGE, id="gateway"),
        pytest.param(f"{GATEWAY_PACKAGE}.providers", id="another-provider-module"),
    ],
)
def test_a_gateway_module_importing_the_evaluation_or_the_cli_breaks_a_contract(
    project_copy: Path, package: str, forbidden: str
) -> None:
    # Arrange
    probe = add_probe_in(project_copy, package, f"import {forbidden}\n")

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert GATEWAY_ISOLATION_CONTRACT in output, output
    # The report wraps a long line at 80 columns: compare it unwrapped.
    assert f"{probe} -> {forbidden}" in " ".join(output.split()), output


def test_a_gateway_module_importing_the_json_file_reader_keeps_the_contracts(
    project_copy: Path,
) -> None:
    # Arrange: what the recording reader needs lives in common.
    add_probe_in(
        project_copy,
        f"{GATEWAY_PACKAGE}.providers",
        "from meridian.platform.common.jsonfile import read_json_file\n",
    )

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code == 0, output


def test_the_isolation_contract_forbids_the_evaluation_and_the_cli() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contract = next(
        c
        for c in config["tool"]["importlinter"]["contracts"]
        if c["name"] == GATEWAY_ISOLATION_CONTRACT
    )

    assert contract["type"] == "forbidden"
    assert contract["source_modules"] == [GATEWAY_PACKAGE]
    assert set(contract["forbidden_modules"]) == {
        "meridian.platform.evaluation",
        "meridian.platform.cli",
    }
    # Indirect imports count: no allowance for them.
    assert not contract.get("allow_indirect_imports", False)


def test_a_gateway_module_importing_the_adapter_keeps_the_contracts(
    project_copy: Path,
) -> None:
    # Arrange: indirect imports inside the gateway are allowed (contract B's
    # app imports the adapter); only a direct openai import is not.
    add_probe_in(project_copy, GATEWAY_PACKAGE, f"import {ADAPTER_MODULE}\n")

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code == 0, output


def platform_packages_missing_from_the_sdk_contract(project: Path) -> set[str]:
    """Modules and packages under ``meridian.platform`` other than the gateway
    that the SDK contract's ``source_modules`` does not name."""
    config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    contract = next(
        c
        for c in config["tool"]["importlinter"]["contracts"]
        if c["name"] == SDK_CONTRACT
    )
    platform = project / "src" / "meridian" / "platform"
    present = {
        f"meridian.platform.{child.stem}"
        for child in platform.iterdir()
        if (child.is_dir() and (child / "__init__.py").exists())
        or (child.suffix == ".py" and child.name != "__init__.py")
    }
    return present - {GATEWAY_PACKAGE} - set(contract["source_modules"])


def test_the_sdk_contract_names_every_platform_package_but_the_gateway() -> None:
    assert platform_packages_missing_from_the_sdk_contract(REPO_ROOT) == set()


def test_the_sdk_contract_does_not_exempt_the_gateway_by_naming_its_parent() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contract = next(
        c
        for c in config["tool"]["importlinter"]["contracts"]
        if c["name"] == SDK_CONTRACT
    )

    assert "meridian.platform" not in contract["source_modules"]
    assert GATEWAY_PACKAGE not in contract["source_modules"]
    assert {"meridian.runtime", "meridian.workloads"} <= set(contract["source_modules"])


@pytest.mark.parametrize("name", ["newpackage", "newmodule.py"])
def test_a_new_platform_package_or_module_is_reported_as_missing(
    project_copy: Path, name: str
) -> None:
    # Arrange
    platform = project_copy / "src" / "meridian" / "platform"
    if name.endswith(".py"):
        (platform / name).write_text("", encoding="utf-8")
    else:
        (platform / name).mkdir()
        (platform / name / "__init__.py").write_text("", encoding="utf-8")

    # Act
    missing = platform_packages_missing_from_the_sdk_contract(project_copy)

    # Assert
    assert missing == {f"meridian.platform.{Path(name).stem}"}


def test_importing_the_services_loads_no_provider_sdk_or_credential_library() -> None:
    # Arrange: a fresh interpreter, so no earlier test has imported them.
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "import meridian.platform.gateway.app\n"
        "import meridian.runtime.app\n"
        "import meridian.workloads.claims_triage.app\n"
        "import meridian.platform.policy_mcp.app\n"
        "import meridian.workloads.claims_triage.mcp_server.app\n"
        "from meridian.platform.gateway.settings import GatewaySettings\n"
        "app = meridian.platform.gateway.app.create_app(GatewaySettings(\n"
        "    registry_dir=Path('config/registry'), mode='replay',\n"
        "    environment='test', database_url='postgresql://x@db.invalid/m'))\n"
        "assert app.title == 'Meridian Model Gateway'\n"
        "watched = ('openai', 'azure', 'azure.identity', 'azure.core')\n"
        "loaded = [m for m in watched if m in sys.modules]\n"
        "print('loaded:' + ','.join(loaded))\n"
    )

    # Act
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=LINT_TIMEOUT_SECONDS,
        check=False,
    )

    # Assert
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "loaded:", completed.stdout
