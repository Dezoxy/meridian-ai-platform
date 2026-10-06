"""``meridian workload new``: the command's output, exit codes and refusals (S039).

Every test writes into the small tree of ``conftest.py`` in ``tmp_path``; none
touches the real checkout.
"""

import errno
import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meridian.platform.cli import app, scaffold
from meridian.platform.registry.loader import load_registry

runner = CliRunner()
NAME = "fraud-review"
MODULE = "fraud_review"
EXIT_REFUSED = 2
EXIT_FAILED = 1
# Typer styles its usage errors when GITHUB_ACTIONS is set, and the colour codes
# split even an option's name ("-" "-root").
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return ANSI.sub("", text)


def snapshot(root: Path) -> dict[str, object]:
    """Every path under ``root``: a file's bytes, a directory as ``None``."""
    found: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        found[relative] = None if path.is_dir() else path.read_bytes()
    return found


def expected_stdout(name: str, module: str) -> str:
    created = sorted(
        [
            f"src/meridian/workloads/{module}/__init__.py",
            f"src/meridian/workloads/{module}/graph.py",
            f"src/meridian/workloads/{module}/evaluation.py",
            f"tests/meridian/workloads/{module}/test_{module}_scaffold.py",
            f"data/evaluation/{name}/golden/cases.json",
            f"data/evaluation/{name}/golden/manifest.json",
        ]
    )
    lines = [f"created {path}" for path in created]
    lines += [
        "changed config/registry/agents.yaml",
        "changed config/registry/services.yaml",
        "changed pyproject.toml",
        "workload new: done",
        "generated: a graph with one node that calls no model and no tool, an "
        "evaluation with no grader, a golden set with no case and an agent with "
        "no tool and no worker",
        "still by hand: the agent in the `agents` of a tenant in tenants.yaml (no "
        "call for it is admitted before, and `meridian registry validate` will "
        "print a note until it is done), its tools and any workers (an edit of "
        "agents.yaml that config/registry/README.md describes), a prompt, "
        "synthetic cases from a seeded generator and their graders; for an API "
        "of the workload's "
        "own, an entry in services.yaml (`id`, `description`, `calls: "
        "[agent-runtime]`, `tenants: []` until a tenant lists the agent, and "
        "`agents` with the new agent) and a chart entry with a certificate",
        "first run, from the checkout's root:",
        "  uv run meridian registry validate",
        "  uv run lint-imports",
        f"  uv run meridian eval run --workload {name} "
        f"--golden-set data/evaluation/{name}/golden "
        f"--base-url http://localhost:8000 --report {name}-report.json "
        "--allow-empty",
    ]
    return "\n".join(lines) + "\n"


def test_a_new_workload_is_created_and_the_command_says_what_is_left(
    root: Path,
) -> None:
    # Arrange
    agents_before = len(load_registry(root / "config" / "registry").agents)

    # Act
    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(root)])

    # Assert
    assert result.exit_code == 0, result.stderr
    assert result.stdout == expected_stdout(NAME, MODULE)
    assert result.stderr == ""
    for line in result.stdout.splitlines():
        if line.startswith("created "):
            assert (root / line.removeprefix("created ")).is_file()
    registry = load_registry(root / "config" / "registry")
    assert len(registry.agents) == agents_before + 1
    agent = registry.agent(NAME)
    assert agent is not None
    assert agent.workers == ()


def test_the_root_defaults_to_the_working_directory(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["workload", "new", NAME])

    assert result.exit_code == 0, result.stderr
    assert (root / "src" / "meridian" / "workloads" / MODULE / "graph.py").is_file()


@pytest.mark.parametrize("name", ["../x", "class", "yes", "claims-triage"])
def test_a_refused_name_writes_nothing_and_is_not_quoted(root: Path, name: str) -> None:
    # Arrange
    before = snapshot(root)

    # Act
    result = runner.invoke(app, ["workload", "new", name, "--root", str(root)])

    # Assert
    assert result.exit_code == EXIT_REFUSED
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("ERROR ")
    assert name not in result.stderr
    assert snapshot(root) == before


def test_a_second_run_with_the_same_name_is_refused_and_changes_nothing(
    root: Path,
) -> None:
    # Arrange
    first = runner.invoke(app, ["workload", "new", NAME, "--root", str(root)])
    assert first.exit_code == 0, first.stderr
    before = snapshot(root)

    # Act
    second = runner.invoke(app, ["workload", "new", NAME, "--root", str(root)])

    # Assert
    assert second.exit_code == EXIT_REFUSED
    assert second.stdout == ""
    assert second.stderr.startswith("ERROR ")
    assert snapshot(root) == before


def test_a_tree_that_is_no_checkout_is_refused(tmp_path: Path) -> None:
    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(tmp_path)])

    assert result.exit_code == EXIT_REFUSED
    assert result.stdout == ""
    assert result.stderr.startswith("ERROR ")
    assert os.listdir(tmp_path) == []


def test_a_root_that_does_not_exist_is_a_usage_error_and_writes_nothing(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"

    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(missing)])

    assert result.exit_code == EXIT_REFUSED
    assert result.stdout == ""
    assert "Usage" in plain(result.stderr)
    assert "--root" in plain(result.stderr)
    assert not missing.exists()
    assert os.listdir(tmp_path) == []


def test_a_root_that_is_a_file_is_a_usage_error(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.write_text("x", encoding="utf-8")

    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(file)])

    assert result.exit_code == EXIT_REFUSED
    assert "Usage" in plain(result.stderr)
    assert "--root" in plain(result.stderr)
    assert os.listdir(tmp_path) == ["file"]


def test_a_failed_write_exits_1_not_2_and_says_what_is_left_behind(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.replace
    agents_replacements = []

    def replace(source: object, destination: object, *args: object) -> None:
        if Path(str(destination)).name == "agents.yaml":
            agents_replacements.append(source)
        if (
            Path(str(destination)).name == "pyproject.toml"
            or len(agents_replacements) > 1
        ):
            raise PermissionError(errno.EACCES, "a-path-the-output-must-not-repeat")
        real(source, destination, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", replace)

    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(root)])

    assert result.exit_code == EXIT_FAILED
    assert result.stdout == ""
    assert result.stderr == (
        "ERROR "
        + scaffold.ROLLBACK_FAILED.format(
            "replacing pyproject.toml", f"PermissionError: {os.strerror(errno.EACCES)}"
        )
        + "\nERROR left behind: config/registry/agents.yaml\n"
    )


def test_a_write_that_was_undone_exits_1_with_one_line(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(root)
    real = os.replace

    def replace(source: object, destination: object, *args: object) -> None:
        if Path(str(destination)).name == "pyproject.toml":
            raise PermissionError(errno.EACCES, "a-path-the-output-must-not-repeat")
        real(source, destination, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", replace)

    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(root)])

    assert result.exit_code == EXIT_FAILED
    assert result.stdout == ""
    assert result.stderr == (
        "ERROR "
        + scaffold.WRITE_FAILED.format(
            "replacing pyproject.toml", f"PermissionError: {os.strerror(errno.EACCES)}"
        )
        + "\n"
    )
    assert snapshot(root) == before


def test_a_refusal_with_details_prints_each_on_its_own_line(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(root: Path, name: str) -> None:
        raise scaffold.ScaffoldError("the message", details=("one", "two"))

    monkeypatch.setattr("meridian.platform.cli.workload.plan_workload", refuse)

    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(root)])

    assert result.exit_code == EXIT_REFUSED
    assert result.stderr == "ERROR the message\nERROR one\nERROR two\n"


def test_the_meridian_help_lists_workload_and_its_help_lists_new() -> None:
    top = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["workload", "--help"])
    bare = runner.invoke(app, ["workload"])

    assert top.exit_code == 0
    assert "workload" in top.stdout
    assert group.exit_code == 0
    assert "new" in group.stdout
    assert bare.exit_code == EXIT_REFUSED
    assert "new" in bare.stdout
