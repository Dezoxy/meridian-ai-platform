"""Prove that the three import contracts of the sign-in modules (S021) detect a
violation: ``jwt`` only from the three modules that check a token, the sign-in
modules not from the services or the graph code, and ``common`` not importing
the gateway.

The method is that of ``test_import_contracts.py``, whose helpers are used: a
copy of the sources gets one probe module (or one added line) that breaks a
rule, and ``lint-imports`` must name the probe's import. The unmodified copy
passing is the other side of every case.
"""

import shutil
import tomllib
from pathlib import Path
from typing import Any

import pytest
from test_import_contracts import REPO_ROOT, add_probe_in, run_lint_imports

JWT_CONTRACT = "only the sign-in modules import the JWT library (S021)"
SIGNIN_CONTRACT = "services and graph code do not import the sign-in modules (S021)"
COMMON_CONTRACT = "the shared platform code does not import the gateway"
COMMON = "meridian.platform.common"
SIGNIN_MODULES = [
    f"{COMMON}.signin",
    f"{COMMON}.signinkeys",
    f"{COMMON}.signinsession",
    f"{COMMON}.signinguard",
    f"{COMMON}.signinflow",
    f"{COMMON}.signinstate",
    f"{COMMON}.signinidtoken",
]
CLAIMS_TRIAGE = "meridian.workloads.claims_triage"


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


def contract_named(name: str) -> dict[str, Any]:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return next(
        c for c in config["tool"]["importlinter"]["contracts"] if c["name"] == name
    )


def platform_packages_missing_from(project: Path, contract: str) -> set[str]:
    """Modules and packages under ``meridian.platform`` that the contract's
    ``source_modules`` does not name."""
    config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    sources = next(
        c for c in config["tool"]["importlinter"]["contracts"] if c["name"] == contract
    )["source_modules"]
    platform = project / "src" / "meridian" / "platform"
    present = {
        f"meridian.platform.{child.stem}"
        for child in platform.iterdir()
        if (child.is_dir() and (child / "__init__.py").exists())
        or (child.suffix == ".py" and child.name != "__init__.py")
    }
    return present - set(sources)


# ── the JWT library ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import jwt\n", id="import-jwt"),
        pytest.param("from jwt.algorithms import RSAAlgorithm\n", id="from-a-name"),
        pytest.param("import jwt.exceptions\n", id="import-a-submodule"),
    ],
)
@pytest.mark.parametrize(
    "package",
    [
        pytest.param("meridian.runtime", id="runtime"),
        pytest.param("meridian.workloads", id="workloads"),
        pytest.param(CLAIMS_TRIAGE, id="claims-triage"),
        pytest.param("meridian.platform.gateway", id="gateway"),
        pytest.param("meridian.platform.toolserver", id="toolserver"),
        pytest.param("meridian.platform.registry", id="registry"),
        pytest.param(COMMON, id="common-but-not-the-sign-in-modules"),
    ],
)
def test_an_import_of_jwt_outside_the_three_sign_in_modules_breaks_the_contract(
    project_copy: Path, package: str, source: str
) -> None:
    # Arrange
    probe = add_probe_in(project_copy, package, source)

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert JWT_CONTRACT in output, output
    assert f"{probe} -> jwt" in output, output


def test_the_three_sign_in_modules_do_import_jwt_and_keep_the_contract(
    project_copy: Path,
) -> None:
    # Arrange: the three ignored edges are real ones, so the contract is not
    # kept only because nobody imports the library.
    common = project_copy / "src" / "meridian" / "platform" / "common"
    for module in ("signin", "signinkeys", "signinidtoken"):
        source = (common / f"{module}.py").read_text(encoding="utf-8")
        assert "import jwt" in source or "from jwt" in source

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code == 0, output
    assert f"{JWT_CONTRACT} KEPT (3 ignored imports)" in output, output


def test_the_jwt_contract_ignores_exactly_the_three_modules_that_check_a_token() -> (
    None
):
    contract = contract_named(JWT_CONTRACT)

    assert contract["type"] == "forbidden"
    assert contract["forbidden_modules"] == ["jwt"]
    assert set(contract["ignore_imports"]) == {
        f"{COMMON}.signin -> jwt",
        f"{COMMON}.signinkeys -> jwt",
        f"{COMMON}.signinidtoken -> jwt",
    }
    # Indirect imports count: no allowance for them.
    assert not contract.get("allow_indirect_imports", False)
    assert {"meridian.runtime", "meridian.workloads", COMMON} <= set(
        contract["source_modules"]
    )
    assert "meridian.platform" not in contract["source_modules"]


def test_the_jwt_contract_names_every_platform_package() -> None:
    assert platform_packages_missing_from(REPO_ROOT, JWT_CONTRACT) == set()


@pytest.mark.parametrize("name", ["newpackage", "newmodule.py"])
def test_a_new_platform_package_or_module_is_reported_missing_from_the_jwt_contract(
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
    missing = platform_packages_missing_from(project_copy, JWT_CONTRACT)

    # Assert
    assert missing == {f"meridian.platform.{Path(name).stem}"}


# ── the sign-in modules ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "package",
    [
        pytest.param("meridian.platform.gateway", id="gateway"),
        pytest.param("meridian.runtime", id="runtime"),
        pytest.param("meridian.platform.toolserver", id="toolserver"),
        pytest.param("meridian.platform.knowledge_mcp", id="knowledge-mcp"),
        pytest.param("meridian.platform.policy_mcp", id="policy-mcp"),
        pytest.param("meridian.workloads.claim_brief", id="claim-brief"),
        pytest.param("meridian.platform.claims_mcp", id="claims-mcp"),
    ],
)
def test_a_service_or_graph_package_importing_the_guard_breaks_the_contract(
    project_copy: Path, package: str
) -> None:
    # Arrange
    probe = add_probe_in(project_copy, package, f"import {COMMON}.signinguard\n")

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert SIGNIN_CONTRACT in output, output
    assert f"{probe} -> {COMMON}.signinguard" in " ".join(output.split()), output


@pytest.mark.parametrize(
    "package",
    [
        pytest.param("meridian.runtime", id="runtime"),
        pytest.param("meridian.platform.toolserver", id="toolserver"),
        pytest.param("meridian.workloads.claim_brief", id="claim-brief"),
        pytest.param("meridian.platform.claims_mcp", id="claims-mcp"),
    ],
)
def test_a_service_or_graph_package_importing_the_flow_breaks_the_contract(
    project_copy: Path, package: str
) -> None:
    # Arrange: the page flow holds the client's credential and a person's
    # transaction; no service imports it.
    probe = add_probe_in(project_copy, package, f"import {COMMON}.signinflow\n")

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert SIGNIN_CONTRACT in output, output
    assert f"{probe} -> {COMMON}.signinflow" in " ".join(output.split()), output


@pytest.mark.parametrize("module", SIGNIN_MODULES)
def test_each_of_the_seven_sign_in_modules_is_forbidden_to_the_gateway(
    project_copy: Path, module: str
) -> None:
    # Arrange
    gateway = "meridian.platform.gateway"
    probe = add_probe_in(project_copy, gateway, f"import {module}\n")

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert SIGNIN_CONTRACT in output, output
    assert f"{probe} -> {module}" in " ".join(output.split()), output


def test_the_claims_triage_graph_importing_the_session_breaks_the_contract(
    project_copy: Path,
) -> None:
    # Arrange: graph.py is a module, not a package, so the line is added to it.
    graph = project_copy / "src/meridian/workloads/claims_triage/graph.py"
    graph.write_text(
        graph.read_text(encoding="utf-8") + f"\nimport {COMMON}.signinsession\n",
        encoding="utf-8",
    )

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert SIGNIN_CONTRACT in output, output
    assert f"{CLAIMS_TRIAGE}.graph -> {COMMON}.signinsession" in " ".join(
        output.split()
    ), output


@pytest.mark.parametrize(
    ("package", "module"),
    [
        pytest.param(CLAIMS_TRIAGE, "signinguard", id="claims-api-guard"),
        pytest.param(CLAIMS_TRIAGE, "signinsession", id="claims-api-session"),
        pytest.param(CLAIMS_TRIAGE, "signinflow", id="claims-api-flow"),
        pytest.param(CLAIMS_TRIAGE, "signinstate", id="claims-api-state"),
        pytest.param(CLAIMS_TRIAGE, "signinidtoken", id="claims-api-id-token"),
        pytest.param(COMMON, "signin", id="common-itself"),
    ],
)
def test_the_claims_api_and_common_may_import_the_sign_in_modules(
    project_copy: Path, package: str, module: str
) -> None:
    # Arrange: the Claims API's own modules are where people sign in.
    add_probe_in(project_copy, package, f"import {COMMON}.{module}\n")

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code == 0, output


def test_the_sign_in_contract_forbids_exactly_the_seven_modules() -> None:
    contract = contract_named(SIGNIN_CONTRACT)

    assert contract["type"] == "forbidden"
    assert set(contract["forbidden_modules"]) == set(SIGNIN_MODULES)
    assert not contract.get("allow_indirect_imports", False)
    assert {
        "meridian.platform.gateway",
        "meridian.runtime",
        "meridian.platform.toolserver",
        "meridian.platform.knowledge_mcp",
        "meridian.platform.policy_mcp",
    } <= set(contract["source_modules"])
    # The Claims API wires the guard in: it is not a source of this contract.
    assert not any(
        source in (CLAIMS_TRIAGE, "meridian.workloads")
        for source in contract["source_modules"]
    )


# ── common does not import the gateway ──────────────────────────────────────
@pytest.mark.parametrize(
    "target",
    [
        pytest.param("meridian.platform.gateway.settings", id="a-submodule"),
        pytest.param("meridian.platform.gateway", id="the-package"),
    ],
)
def test_a_common_module_importing_the_gateway_breaks_the_contract(
    project_copy: Path, target: str
) -> None:
    # Arrange
    probe = add_probe_in(project_copy, COMMON, f"import {target}\n")

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code != 0, output
    assert COMMON_CONTRACT in output, output
    assert f"{probe} -> {target}" in " ".join(output.split()), output


def test_the_gateway_importing_common_is_the_direction_that_is_allowed(
    project_copy: Path,
) -> None:
    # Arrange
    add_probe_in(project_copy, "meridian.platform.gateway", f"import {COMMON}.env\n")

    # Act
    exit_code, output = run_lint_imports(project_copy)

    # Assert
    assert exit_code == 0, output


def test_the_common_contract_has_common_as_its_only_source() -> None:
    contract = contract_named(COMMON_CONTRACT)

    assert contract["type"] == "forbidden"
    assert contract["source_modules"] == [COMMON]
    assert contract["forbidden_modules"] == ["meridian.platform.gateway"]
    assert not contract.get("allow_indirect_imports", False)
