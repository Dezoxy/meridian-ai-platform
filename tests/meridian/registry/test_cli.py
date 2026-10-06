"""``meridian registry`` through Typer's test runner."""

import json
import re
from pathlib import Path

import pytest
from directorysupport import CANARY, FAILURES, Failure, make_unreadable
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli import registry as cli_registry
from meridian.platform.registry import load_registry

runner = CliRunner()
SUMMARY = re.compile(
    r"registry OK: \d+ providers?, \d+ deployments?, \d+ tools?, \d+ agents?, "
    r"\d+ tenants?, \d+ services?"
)


def test_validate_passes_on_the_committed_registry_with_the_snapshot(
    real_registry: Path, snapshot_path: Path
) -> None:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    result = runner.invoke(
        app,
        [
            "registry",
            "validate",
            "--registry-dir",
            str(real_registry),
            "--terraform-outputs",
            str(snapshot_path),
        ],
    )

    assert result.exit_code == 0, result.output
    summary, terraform = result.stdout.splitlines()
    assert SUMMARY.fullmatch(summary), summary
    assert terraform == f"terraform outputs OK: {len(snapshot)} deployments match"


def test_validate_without_terraform_outputs_prints_only_the_summary(
    real_registry: Path,
) -> None:
    result = runner.invoke(
        app, ["registry", "validate", "--registry-dir", str(real_registry)]
    )

    assert result.exit_code == 0, result.output
    assert SUMMARY.fullmatch(result.stdout.strip()), result.stdout


def test_validate_prints_every_error_to_stderr_and_exits_1(registry_copy: Path) -> None:
    (registry_copy / "tenants.yaml").unlink()
    (registry_copy / "agents.yaml").unlink()

    result = runner.invoke(
        app, ["registry", "validate", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        "ERROR agents.yaml: file is missing",
        "ERROR tenants.yaml: file is missing",
    ]
    assert result.stdout == ""


def test_validate_reports_a_planted_semantic_violation(
    registry_copy: Path,
) -> None:
    tools = registry_copy / "tools.yaml"
    tools.write_text(
        tools.read_text(encoding="utf-8").replace(
            "    effect: read\n", "    effect: decision\n", 1
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["registry", "validate", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1
    assert (
        "ERROR agents.yaml: agents[0].tools[0]: agent 'claims-triage' lists decision"
        in result.stderr
    )


def test_validate_fails_on_terraform_drift(
    real_registry: Path, tmp_path: Path, snapshot_path: Path
) -> None:
    drifted = tmp_path / "outputs.json"
    drifted.write_text(
        snapshot_path.read_text(encoding="utf-8").replace(
            '"Standard"', '"DataZoneStandard"'
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "registry",
            "validate",
            "--registry-dir",
            str(real_registry),
            "--terraform-outputs",
            str(drifted),
        ],
    )

    assert result.exit_code == 1
    assert "registry OK" in result.stdout
    assert "terraform outputs OK" not in result.stdout
    # One line per Azure deployment: two chat and one embedding.
    assert result.stderr.count("ERROR models.yaml: deployment") == 3


def test_validate_reports_a_missing_terraform_file_without_a_traceback(
    real_registry: Path, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "registry",
            "validate",
            "--registry-dir",
            str(real_registry),
            "--terraform-outputs",
            str(tmp_path / "absent.json"),
        ],
    )

    assert result.exit_code == 1
    assert "ERROR " in result.stderr
    assert "cannot read Terraform outputs" in result.stderr


def test_schemas_check_passes_on_the_committed_tree(real_registry: Path) -> None:
    result = runner.invoke(
        app, ["registry", "schemas", "--check", "--registry-dir", str(real_registry)]
    )

    assert result.exit_code == 0, result.output


def test_schemas_check_names_a_stale_file_and_writes_nothing(
    registry_copy: Path,
) -> None:
    stale = registry_copy / "schemas" / "models.schema.json"
    stale.write_text("{}\n", encoding="utf-8")

    result = runner.invoke(
        app, ["registry", "schemas", "--check", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1
    assert "schemas/models.schema.json is out of date" in result.stderr
    assert stale.read_text(encoding="utf-8") == "{}\n"


def test_schemas_without_check_rewrites_the_stale_file(registry_copy: Path) -> None:
    stale = registry_copy / "schemas" / "models.schema.json"
    stale.write_text("{}\n", encoding="utf-8")

    written = runner.invoke(
        app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
    )
    checked = runner.invoke(
        app, ["registry", "schemas", "--check", "--registry-dir", str(registry_copy)]
    )

    assert written.exit_code == 0, written.output
    assert checked.exit_code == 0, checked.output


def test_no_arguments_prints_help() -> None:
    result = runner.invoke(app, [])

    assert "registry" in result.output


def test_summary_uses_singular_and_plural_correctly(registry_copy: Path) -> None:
    tenants = registry_copy / "tenants.yaml"
    text = tenants.read_text(encoding="utf-8")
    tenants.write_text(
        text[: text.index("  - id: evaluation")].replace(
            "agents: [claims-triage, knowledge-ingestion, claim-brief]",
            "agents: [claims-triage]",
        ),
        encoding="utf-8",
    )
    agents = registry_copy / "agents.yaml"
    text = agents.read_text(encoding="utf-8")
    agents.write_text(
        text[: text.index("  - id: knowledge-ingestion")], encoding="utf-8"
    )
    # Every service that names an agent names the one agent that is left.
    services = registry_copy / "services.yaml"
    services.write_text(
        re.sub(
            r"agents: \[[^\]\s][^\]]*\]",
            "agents: [claims-triage]",
            services.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["registry", "validate", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 0, result.output
    assert "1 agent," in result.stdout
    assert "1 tenant," in result.stdout
    assert "1 tenants" not in result.stdout
    assert "1 agents" not in result.stdout


def test_terraform_summary_is_singular_for_one_deployment(
    monkeypatch: pytest.MonkeyPatch,
    real_registry: Path,
    snapshot_path: Path,
    tmp_path: Path,
) -> None:
    # A valid registry needs two Azure deployments (a route per purpose, and
    # replay is never a candidate), so the one-deployment registry is built
    # past the checks.
    full = load_registry(real_registry)
    dropped = {"aoai-sdc-gpt-4o-b", "aoai-sdc-text-embedding-3-large"}
    one = full.model_copy(
        update={
            "deployments": tuple(d for d in full.deployments if d.id not in dropped)
        }
    )
    monkeypatch.setattr(cli_registry, "load_registry", lambda _directory: one)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    outputs = tmp_path / "outputs.json"
    outputs.write_text(
        json.dumps({"sdc/gpt-4o": snapshot["sdc/gpt-4o"]}), encoding="utf-8"
    )

    result = runner.invoke(
        app,
        [
            "registry",
            "validate",
            "--registry-dir",
            str(real_registry),
            "--terraform-outputs",
            str(outputs),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        result.stdout.splitlines()[-1] == "terraform outputs OK: 1 deployment matches"
    )


@pytest.mark.parametrize("command", [["validate"], ["schemas"], ["schemas", "--check"]])
def test_a_mistyped_registry_dir_is_refused_and_nothing_is_created(
    tmp_path: Path, command: list[str]
) -> None:
    missing = tmp_path / "confg" / "registry"

    result = runner.invoke(app, ["registry", *command, "--registry-dir", str(missing)])

    assert result.exit_code != 0
    assert not missing.exists()
    assert not (tmp_path / "confg").exists()


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize(
    "command", [["validate"], ["contracts"], ["contracts", "--check"]]
)
def test_a_registry_dir_that_cannot_be_read_ends_in_an_error_line_not_a_traceback(
    registry_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    failure: Failure,
) -> None:
    make_unreadable(monkeypatch, registry_copy, failure)

    result = runner.invoke(
        app, ["registry", *command, "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == (
        f"ERROR {registry_copy}: registry directory cannot be read: PermissionError\n"
    )
    assert result.stdout == ""
    assert CANARY not in result.output
    assert not isinstance(result.exception, PermissionError)


def test_schemas_check_on_a_registry_dir_that_cannot_be_read_ends_in_an_error_line(
    registry_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_unreadable(monkeypatch, registry_copy, "stat")

    result = runner.invoke(
        app,
        ["registry", "schemas", "--check", "--registry-dir", str(registry_copy)],
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == (
        f"ERROR {registry_copy}: registry directory cannot be read: PermissionError\n"
    )
    assert result.stdout == ""
    assert not isinstance(result.exception, PermissionError)
