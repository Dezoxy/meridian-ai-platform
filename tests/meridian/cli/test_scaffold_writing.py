"""The workload scaffold's writing, rollback and stale-plan check (S039, T-81).

Every test builds a small tree in ``tmp_path``: a copy of this repository's
``pyproject.toml`` and registry and an empty workloads directory. Nothing here
touches the real checkout.
"""

import errno
import hashlib
import json
import os
import stat
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from meridian.platform.cli import scaffold
from meridian.platform.cli.scaffold import (
    NAME_TAKEN,
    PATH_EXISTS,
    ROLLBACK_FAILED,
    STALE_PLAN,
    WRITE_FAILED,
    Plan,
    ScaffoldError,
    ScaffoldWriteError,
    plan_workload,
    write_plan,
)
from meridian.platform.registry.loader import load_registry

NAME = "fraud-review"
MODULE = "fraud_review"
GRAPHS = "meridian.graphs"
EVALUATIONS = "meridian.evaluations"
LONG_NAME = "a" * 20 + "-" + "b" * 19
RUFF_TIMEOUT_SECONDS = 60
REPO = Path(__file__).resolve().parents[3]
LEAKED = "a-path-or-a-name-the-error-must-not-repeat"
# The small tree has no ``meridian.platform`` for ruff's import sorter to find, so
# it is told what the real checkout's layout shows it: ``meridian`` is ours.
FIRST_PARTY = ("--config", 'lint.isort.known-first-party = ["meridian"]')


def created_paths(module: str = MODULE, name: str = NAME) -> set[str]:
    return {
        f"src/meridian/workloads/{module}/__init__.py",
        f"src/meridian/workloads/{module}/graph.py",
        f"src/meridian/workloads/{module}/evaluation.py",
        f"tests/meridian/workloads/{module}/test_{module}_scaffold.py",
        f"data/evaluation/{name}/golden/cases.json",
        f"data/evaluation/{name}/golden/manifest.json",
    }


def snapshot(root: Path) -> dict[str, object]:
    """Every path under ``root``: a file's bytes, a symlink's target, a
    directory as ``None``, so that a stray directory or temp file shows."""
    found: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[relative] = ("link", os.readlink(path))
        elif path.is_dir():
            found[relative] = None
        else:
            found[relative] = path.read_bytes()
    return found


def edit(path: Path, change: Callable[[str], str]) -> None:
    text = path.read_bytes().decode("utf-8")
    path.write_bytes(change(text).encode("utf-8"))


def comment_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.lstrip()[:1] == "#"]


def occupy_with_a_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def occupy_with_a_dangling_symlink(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(path.parent / "nowhere")


def written(root: Path, name: str = NAME) -> Plan:
    plan = plan_workload(root, name)
    write_plan(root, plan)
    return plan


def test_write_plan_writes_the_files_and_the_two_edits(root: Path) -> None:
    plan = written(root)

    for relative, text in {**plan.created, **plan.changed}.items():
        assert (root / relative).read_bytes() == text.encode("utf-8")


def test_the_entry_points_are_added_and_every_old_key_is_unchanged(
    root: Path,
) -> None:
    old = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    written(root)

    new = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    expected = json.loads(json.dumps(old))
    expected["project"]["entry-points"][GRAPHS][NAME] = (
        f"meridian.workloads.{MODULE}.graph:build"
    )
    expected["project"]["entry-points"][EVALUATIONS][NAME] = (
        f"meridian.workloads.{MODULE}.evaluation:EVALUATION"
    )
    assert new == expected


def test_every_comment_of_the_two_edited_files_is_still_there_in_order(
    root: Path,
) -> None:
    old = {
        name: (root / name).read_text(encoding="utf-8")
        for name in ("pyproject.toml", "config/registry/agents.yaml")
    }

    written(root)

    for name, text in old.items():
        assert comment_lines(text)  # the test would prove nothing otherwise
        new = (root / name).read_text(encoding="utf-8")
        assert comment_lines(new) == comment_lines(text)


def test_the_registry_with_the_new_agent_validates_and_grants_it_nothing(
    root: Path,
) -> None:
    old_tenants = (root / "config/registry/tenants.yaml").read_bytes()
    others = {
        path.name: path.read_bytes()
        for path in (root / "config/registry").glob("*.yaml")
        if path.name != "agents.yaml"
    }

    written(root)

    registry = load_registry(root / "config/registry")
    agent = registry.agent(NAME)
    assert agent is not None
    assert agent.kind == "graph"
    assert agent.tools == ()
    assert all(NAME not in tenant.agents for tenant in registry.tenants)
    assert (root / "config/registry/tenants.yaml").read_bytes() == old_tenants
    assert {
        path.name: path.read_bytes()
        for path in (root / "config/registry").glob("*.yaml")
        if path.name != "agents.yaml"
    } == others


def test_the_old_agents_are_unchanged_after_the_write(root: Path) -> None:
    before = yaml.safe_load((root / "config/registry/agents.yaml").read_text("utf-8"))

    written(root)

    after = yaml.safe_load((root / "config/registry/agents.yaml").read_text("utf-8"))
    assert after["agents"][:-1] == before["agents"]
    assert after["agents"][-1] == {
        "id": NAME,
        "description": (
            f"The {NAME} workload, generated by `meridian workload new`; "
            "it calls no tool."
        ),
        "tools": [],
    }


def test_the_files_replaced_keep_their_mode(root: Path) -> None:
    for relative in ("pyproject.toml", "config/registry/agents.yaml"):
        (root / relative).chmod(0o640)

    written(root)

    for relative in ("pyproject.toml", "config/registry/agents.yaml"):
        assert stat.S_IMODE((root / relative).stat().st_mode) == 0o640


def test_write_plan_leaves_no_temporary_file_beside_the_edited_files(
    root: Path,
) -> None:
    written(root)

    assert not list(root.rglob("*.tmp"))


def test_write_plan_refuses_when_a_created_path_appeared_after_planning(
    root: Path,
) -> None:
    plan = plan_workload(root, NAME)
    occupy_with_a_dangling_symlink(root / f"data/evaluation/{NAME}/golden/cases.json")
    before = snapshot(root)

    with pytest.raises(ScaffoldError) as refused:
        write_plan(root, plan)

    assert str(refused.value) == PATH_EXISTS
    assert snapshot(root) == before


def fail_replace_on(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    real = os.replace

    def replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        if Path(destination).name == target:
            raise PermissionError(errno.EACCES, LEAKED, str(destination))
        real(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def failure_text(template: str, exc_type: str, code: int | None) -> str:
    """What the scaffold says of an ``OSError``: its type and, when it carries an
    errno, the operating system's text for it; never the exception's own text."""
    if code is None:
        return template.format(exc_type)
    return template.format(f"{exc_type}: {os.strerror(code)}")


@pytest.mark.parametrize("target", ["pyproject.toml", "agents.yaml"])
def test_a_failed_replacement_leaves_the_tree_byte_identical(
    root: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    fail_replace_on(monkeypatch, target)

    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    assert str(refused.value) == failure_text(
        WRITE_FAILED, "PermissionError", errno.EACCES
    )
    assert refused.value.details == ()
    assert LEAKED not in str(refused.value)
    assert target not in str(refused.value)
    assert snapshot(root) == before


def test_a_plan_records_the_hash_of_each_file_it_was_planned_from(root: Path) -> None:
    plan = plan_workload(root, NAME)

    assert dict(plan.base) == {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in ("config/registry/agents.yaml", "pyproject.toml")
    }


@pytest.mark.parametrize("relative", ["config/registry/agents.yaml", "pyproject.toml"])
def test_a_plan_that_went_stale_is_refused_and_nothing_is_written(
    root: Path, relative: str
) -> None:
    plan = plan_workload(root, NAME)
    # Another agent, or an edit of the entry points, after the plan was made.
    edit(root / relative, lambda text: text + "\n# a later change\n")
    before = snapshot(root)

    with pytest.raises(ScaffoldError) as refused:
        write_plan(root, plan)

    assert str(refused.value) == STALE_PLAN
    assert not isinstance(refused.value, ScaffoldWriteError)
    assert snapshot(root) == before


def test_a_plan_whose_file_cannot_be_read_again_is_stale_too(root: Path) -> None:
    plan = plan_workload(root, NAME)
    (root / "config/registry/agents.yaml").unlink()

    with pytest.raises(ScaffoldError) as refused:
        write_plan(root, plan)

    assert str(refused.value) == STALE_PLAN
    assert not (root / "src/meridian/workloads" / MODULE).exists()


def test_a_refusal_is_not_a_write_error(root: Path) -> None:
    with pytest.raises(ScaffoldError) as refused:
        plan_workload(root, "claims-triage")

    assert not isinstance(refused.value, ScaffoldWriteError)
    assert refused.value.details == ()


def test_an_interrupt_at_the_last_replacement_undoes_everything_and_propagates(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    real = os.replace

    def replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        if Path(destination).name == "pyproject.toml":
            raise KeyboardInterrupt
        real(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(KeyboardInterrupt):
        write_plan(root, plan)

    assert snapshot(root) == before


def test_a_rollback_removes_only_the_directories_the_call_made(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "data/evaluation/other/golden").mkdir(parents=True)
    (root / "tests/meridian/workloads/claims_triage").mkdir(parents=True)
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    fail_replace_on(monkeypatch, "pyproject.toml")

    with pytest.raises(ScaffoldError):
        write_plan(root, plan)

    assert snapshot(root) == before


def test_a_failed_creation_is_rolled_back_too(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    real = Path.mkdir

    def mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        # After the package's and the test's directories exist.
        if self.name == "golden":
            raise OSError(errno.ENOSPC, LEAKED)
        real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)

    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    assert str(refused.value) == failure_text(WRITE_FAILED, "OSError", errno.ENOSPC)
    assert snapshot(root) == before


def test_an_oserror_without_an_errno_is_named_by_its_type_alone(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_workload(root, NAME)
    real = os.replace

    def replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        if Path(destination).name == "pyproject.toml":
            raise OSError(LEAKED)
        real(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    assert str(refused.value) == failure_text(WRITE_FAILED, "OSError", None)


class FailingStream:
    """A file opened for creation whose write fails after a few characters."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream

    def __enter__(self) -> "FailingStream":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stream.close()

    def write(self, text: str) -> int:
        self.stream.write(text[:5])
        raise OSError(errno.ENOSPC, LEAKED)


def test_a_write_that_fails_after_the_file_was_created_leaves_nothing_behind(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    real_open = open

    def fake_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        stream = real_open(path, *args, **kwargs)
        return FailingStream(stream) if Path(path).name == "graph.py" else stream

    monkeypatch.setattr(scaffold, "open", fake_open, raising=False)

    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    assert str(refused.value) == failure_text(WRITE_FAILED, "OSError", errno.ENOSPC)
    assert refused.value.details == ()
    assert snapshot(root) == before


def test_exclusive_creation_alone_protects_a_file_that_appeared_after_planning(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_workload(root, NAME)
    appeared = root / f"data/evaluation/{NAME}/golden/cases.json"
    occupy_with_a_file(appeared)
    appeared.write_text("not the plan's", encoding="utf-8")
    before = snapshot(root)
    real = os.path.lexists

    def lexists(path: Any) -> bool:
        # The pre-check of write_plan is blind to that file; nothing else is.
        return False if Path(path) == appeared else real(path)

    monkeypatch.setattr(scaffold.os.path, "lexists", lexists)

    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    assert str(refused.value) == failure_text(
        WRITE_FAILED, "FileExistsError", errno.EEXIST
    )
    assert appeared.read_text(encoding="utf-8") == "not the plan's"
    assert snapshot(root) == before


def test_a_rollback_that_fails_says_so_and_names_what_it_could_not_put_back(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_workload(root, NAME)
    real = os.replace
    agents_replacements = []

    def replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        # The pyproject replacement fails, and so does restoring agents.yaml.
        if Path(destination).name == "agents.yaml":
            agents_replacements.append(source)
        if Path(destination).name == "pyproject.toml" or len(agents_replacements) > 1:
            raise PermissionError(errno.EACCES, LEAKED)
        real(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    assert str(refused.value) == failure_text(
        ROLLBACK_FAILED, "PermissionError", errno.EACCES
    )
    assert LEAKED not in str(refused.value)
    assert refused.value.details == ("left behind: config/registry/agents.yaml",)


def test_a_rollback_names_a_file_it_could_not_remove_and_the_directories_it_holds(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_workload(root, NAME)
    agents_before = (root / "config/registry/agents.yaml").read_bytes()
    fail_replace_on(monkeypatch, "pyproject.toml")
    real = Path.unlink

    def unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name == "cases.json":
            raise PermissionError(errno.EACCES, LEAKED)
        real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    golden = f"data/evaluation/{NAME}/golden"
    assert str(refused.value).startswith("writing the workload failed (")
    assert refused.value.details == (
        f"left behind: {golden}/cases.json",
        f"left behind: {golden}",
        f"left behind: data/evaluation/{NAME}",
        "left behind: data/evaluation",
        "left behind: data",
    )
    # What could be put back was: only the files that would not go are left.
    assert (root / "config/registry/agents.yaml").read_bytes() == agents_before


def test_running_the_scaffold_twice_is_refused_and_changes_nothing(
    root: Path,
) -> None:
    written(root)
    before = snapshot(root)

    with pytest.raises(ScaffoldError) as refused:
        plan_workload(root, NAME)

    assert str(refused.value).startswith(NAME_TAKEN.format(""))
    assert snapshot(root) == before
