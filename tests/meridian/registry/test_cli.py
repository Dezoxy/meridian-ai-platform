"""``meridian registry`` through Typer's test runner."""

import json
import os
import re
import shutil
from pathlib import Path

import pytest
from directorysupport import (
    CANARY,
    FAILURES,
    WRITE_FAILURES,
    Failure,
    WriteFailure,
    make_unreadable,
    make_unwritable,
)
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli import registry as cli_registry
from meridian.platform.registry import load_registry

runner = CliRunner()
FIX_AND_RERUN = "fix the directory, then run `meridian registry schemas` again"
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
    # Read by content: a NOTE line (an agent the runtime may name and no tenant
    # lists) may sit between the two, and none of them is this test's business.
    lines = result.stdout.splitlines()
    assert len([line for line in lines if SUMMARY.fullmatch(line)]) == 1, lines
    terraform = [line for line in lines if line.startswith("terraform outputs")]
    assert terraform == [f"terraform outputs OK: {len(snapshot)} deployments match"]


def test_a_note_between_the_summary_and_the_terraform_line_is_not_a_failure(
    real_registry: Path, snapshot_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_registry, "unlisted_runtime_agents", lambda registry: ("fraud-review",)
    )

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
    lines = result.stdout.splitlines()
    assert len(lines) == 3, lines
    assert len([line for line in lines if SUMMARY.fullmatch(line)]) == 1, lines
    assert len([line for line in lines if line.startswith("NOTE: ")]) == 1, lines
    assert len([line for line in lines if line.startswith("terraform outputs OK")]) == 1


def test_validate_without_terraform_outputs_prints_only_the_summary(
    real_registry: Path,
) -> None:
    result = runner.invoke(
        app, ["registry", "validate", "--registry-dir", str(real_registry)]
    )

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert len([line for line in lines if SUMMARY.fullmatch(line)]) == 1, lines
    assert not [line for line in lines if line.startswith("terraform outputs")]


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


def test_schemas_without_check_prints_how_many_files_it_changed(
    registry_copy: Path,
) -> None:
    (registry_copy / "schemas" / "models.schema.json").write_text(
        "{}\n", encoding="utf-8"
    )

    result = runner.invoke(
        app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "schemas written: 1 changed\n"
    assert result.stderr == ""


@pytest.mark.parametrize("failure", WRITE_FAILURES)
def test_schemas_that_cannot_be_written_end_in_an_error_line_not_a_traceback(
    registry_copy: Path, monkeypatch: pytest.MonkeyPatch, failure: WriteFailure
) -> None:
    (registry_copy / "schemas" / "models.schema.json").unlink()
    make_unwritable(monkeypatch, registry_copy / "schemas", failure)

    result = runner.invoke(
        app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == (
        f"ERROR {registry_copy / 'schemas'}: schemas cannot be updated: "
        f"PermissionError; {FIX_AND_RERUN}\n"
    )
    assert result.stdout == ""
    assert CANARY not in result.output
    assert not isinstance(result.exception, PermissionError)


def test_a_schemas_path_that_is_a_file_ends_in_an_error_line_not_a_traceback(
    registry_copy: Path,
) -> None:
    shutil.rmtree(registry_copy / "schemas")
    (registry_copy / "schemas").write_text("not a directory\n", encoding="utf-8")

    result = runner.invoke(
        app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == (
        f"ERROR {registry_copy / 'schemas'}: schemas cannot be updated: "
        f"FileExistsError; {FIX_AND_RERUN}\n"
    )
    assert result.stdout == ""
    assert not isinstance(result.exception, OSError)


def test_a_schema_file_that_cannot_be_read_is_not_called_a_write_failure(
    registry_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_read_text = Path.read_text
    schemas = registry_copy / "schemas"

    def unreadable(self: Path, *args: object, **kwargs: object) -> str:
        if self.parent == schemas:
            raise PermissionError(13, CANARY)
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", unreadable)

    result = runner.invoke(
        app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == (
        f"ERROR {schemas}: schemas cannot be updated: "
        f"PermissionError; {FIX_AND_RERUN}\n"
    )
    assert "written" not in result.stderr
    assert CANARY not in result.output


UNDECODABLE = b"\xff\xfe"


def test_a_schema_file_that_is_not_text_is_stale_and_the_check_names_it(
    registry_copy: Path,
) -> None:
    broken = registry_copy / "schemas" / "models.schema.json"
    broken.write_bytes(UNDECODABLE)

    result = runner.invoke(
        app, ["registry", "schemas", "--check", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1, result.output
    assert "schemas/models.schema.json is out of date" in result.stderr
    assert not isinstance(result.exception, ValueError)
    assert broken.read_bytes() == UNDECODABLE


def test_a_schema_file_that_is_not_text_is_repaired_by_the_write(
    registry_copy: Path,
) -> None:
    (registry_copy / "schemas" / "models.schema.json").write_bytes(UNDECODABLE)

    written = runner.invoke(
        app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
    )
    checked = runner.invoke(
        app, ["registry", "schemas", "--check", "--registry-dir", str(registry_copy)]
    )

    assert written.exit_code == 0, written.output
    assert written.stdout == "schemas written: 1 changed\n"
    assert written.stderr == ""
    assert checked.exit_code == 0, checked.output


def linked_to_a_file_outside(registry_copy: Path, outside: Path) -> Path:
    """Replace one schema file by a symbolic link to ``outside``, which holds the
    schema's own text (so the link's target would not differ) or other text."""
    link = registry_copy / "schemas" / "models.schema.json"
    link.unlink()
    link.symlink_to(outside)
    return link


def test_a_schema_path_that_is_a_link_is_refused_and_the_target_is_not_written(
    registry_copy: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside, not a schema\n")
    link = linked_to_a_file_outside(registry_copy, outside)

    result = runner.invoke(
        app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == (
        f"ERROR {registry_copy / 'schemas'}: schemas cannot be updated: "
        f"SchemaPathIsALink; {FIX_AND_RERUN}\n"
    )
    assert result.stdout == ""
    assert not isinstance(result.exception, OSError)
    assert outside.read_bytes() == b"outside, not a schema\n"
    assert link.is_symlink()
    assert str(outside) not in result.output


def test_a_link_refuses_the_whole_write_before_any_other_file_is_written(
    registry_copy: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside, not a schema\n")
    linked_to_a_file_outside(registry_copy, outside)
    other = registry_copy / "schemas" / "tools.schema.json"
    other.write_text("{}\n", encoding="utf-8")

    result = runner.invoke(
        app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1, result.output
    assert other.read_text(encoding="utf-8") == "{}\n"


def test_the_check_says_to_remove_a_link_even_when_its_target_is_right(
    registry_copy: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes((registry_copy / "schemas" / "models.schema.json").read_bytes())
    linked_to_a_file_outside(registry_copy, outside)

    result = runner.invoke(
        app, ["registry", "schemas", "--check", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == (
        "ERROR schemas/models.schema.json is a link: remove the link\n"
    )
    assert "meridian registry schemas" not in result.output
    assert str(outside) not in result.output


def test_the_check_gives_a_link_and_a_stale_file_each_a_line_of_its_own(
    registry_copy: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside, not a schema\n")
    linked_to_a_file_outside(registry_copy, outside)
    (registry_copy / "schemas" / "tools.schema.json").write_text("{}\n")

    result = runner.invoke(
        app, ["registry", "schemas", "--check", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1, result.output
    assert result.stderr.splitlines() == [
        "ERROR schemas/models.schema.json is a link: remove the link",
        "ERROR schemas/tools.schema.json is out of date: "
        "run `meridian registry schemas`",
    ]


def link_the_schemas_directory(registry_copy: Path, outside: Path) -> None:
    """Replace the ``schemas`` directory by a link to ``outside``."""
    schemas = registry_copy / "schemas"
    for file in schemas.iterdir():
        file.unlink()
    schemas.rmdir()
    schemas.symlink_to(outside, target_is_directory=True)


def test_a_schemas_directory_that_is_a_link_is_refused_and_nothing_is_written(
    registry_copy: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link_the_schemas_directory(registry_copy, outside)

    result = runner.invoke(
        app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == (
        f"ERROR {registry_copy / 'schemas'}: schemas cannot be updated: "
        f"SchemaPathIsALink; {FIX_AND_RERUN}\n"
    )
    assert result.stdout == ""
    assert list(outside.iterdir()) == []
    assert str(outside) not in result.output


def test_the_check_says_to_remove_a_schemas_directory_that_is_a_link(
    registry_copy: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    shutil.copytree(registry_copy / "schemas", outside)
    link_the_schemas_directory(registry_copy, outside)

    result = runner.invoke(
        app, ["registry", "schemas", "--check", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == "ERROR schemas is a link: remove the link\n"
    assert "meridian registry schemas" not in result.output
    assert str(outside) not in result.output


def test_a_link_to_nothing_is_refused_and_nothing_is_created_at_its_target(
    registry_copy: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "not-there.json"
    linked_to_a_file_outside(registry_copy, missing)

    result = runner.invoke(
        app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
    )

    assert result.exit_code == 1, result.output
    assert "SchemaPathIsALink" in result.stderr
    assert not missing.exists()


def test_a_write_that_fails_part_way_says_to_run_the_command_again(
    registry_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("models", "tools"):
        (registry_copy / "schemas" / f"{name}.schema.json").write_text(
            "{}\n", encoding="utf-8"
        )
    make_unwritable(monkeypatch, registry_copy / "schemas", "write", let_through=1)

    failed = runner.invoke(
        app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
    )
    monkeypatch.undo()
    checked = runner.invoke(
        app, ["registry", "schemas", "--check", "--registry-dir", str(registry_copy)]
    )
    repaired = runner.invoke(
        app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
    )

    assert failed.exit_code == 1, failed.output
    assert FIX_AND_RERUN in failed.stderr
    assert checked.exit_code == 1
    assert checked.stderr.count("is out of date") == 1
    assert repaired.exit_code == 0, repaired.output
    assert repaired.stdout == "schemas written: 1 changed\n"


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


@pytest.mark.skipif(os.geteuid() == 0, reason="a mode does not bind root")
def test_schemas_check_on_a_registry_dir_without_a_search_bit_ends_in_an_error_line(
    registry_copy: Path,
) -> None:
    registry_copy.chmod(0o600)
    try:
        result = runner.invoke(
            app,
            ["registry", "schemas", "--check", "--registry-dir", str(registry_copy)],
        )
    finally:
        registry_copy.chmod(0o700)

    assert result.exit_code == 1, result.output
    assert result.stderr == (
        f"ERROR {registry_copy}: registry directory cannot be read: PermissionError\n"
    )
    assert result.stdout == ""
    assert not isinstance(result.exception, PermissionError)


@pytest.mark.skipif(os.geteuid() == 0, reason="a mode does not bind root")
def test_the_schemas_write_on_a_registry_dir_without_a_search_bit_ends_in_an_error_line(
    registry_copy: Path,
) -> None:
    registry_copy.chmod(0o600)
    try:
        result = runner.invoke(
            app, ["registry", "schemas", "--registry-dir", str(registry_copy)]
        )
    finally:
        registry_copy.chmod(0o700)

    assert result.exit_code == 1, result.output
    assert result.stderr == (
        f"ERROR {registry_copy / 'schemas'}: schemas cannot be updated: "
        f"PermissionError; {FIX_AND_RERUN}\n"
    )
    assert result.stdout == ""
    assert not isinstance(result.exception, PermissionError)
