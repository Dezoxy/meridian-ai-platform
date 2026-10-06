"""A failed write says which write failed; the undo never overwrites a save (S076).

``WRITE_FAILED`` and ``ROLLBACK_FAILED`` name the kind of write from a closed set
of fixed words, never a path the command created (those hold the workload's name)
and never the error's own text. ``_put_back`` writes the old bytes back only over
the bytes this command wrote. Every test builds a small tree in ``tmp_path`` (the
``root`` fixture).
"""

import errno
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meridian.platform.cli import scaffold_writes
from meridian.platform.cli.scaffold import (
    AN_INTERRUPT,
    AN_UNEXPECTED_ERROR,
    CHECKING_THE_PLAN,
    LEFT_AS_SAVED,
    LEFT_BEHIND,
    PLAN_CHANGED,
    ROLLBACK_FAILED,
    WRITE_FAILED,
    ScaffoldWriteError,
    plan_workload,
    write_plan,
)

NAME = "fraud-review"
MODULE = "fraud_review"
AGENTS = "config/registry/agents.yaml"
SERVICES = "config/registry/services.yaml"
PYPROJECT = "pyproject.toml"
LEAKED = "a-path-or-a-name-the-error-must-not-repeat"
PERSONS_SAVE = b"# saved by the person while the command was writing\n"
DENIED = f"PermissionError: {os.strerror(errno.EACCES)}"
NO_SPACE = f"OSError: {os.strerror(errno.ENOSPC)}"
MAKING_A_DIRECTORY = "making a directory"
CREATING_A_FILE = "creating a new file"


def snapshot(root: Path) -> dict[str, object]:
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


def fail_replace_on(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    real = os.replace

    def replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        if Path(destination).name == target:
            raise PermissionError(errno.EACCES, LEAKED, str(destination))
        real(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def fail_mkdir_of(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real = Path.mkdir

    def mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name == name:
            raise OSError(errno.ENOSPC, LEAKED)
        real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)


def fail_open_of(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real_open = open

    def fake_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path).name == name:
            raise OSError(errno.ENOSPC, LEAKED)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(scaffold_writes, "open", fake_open, raising=False)


@pytest.mark.parametrize(
    ("arrange", "kind", "failure"),
    [
        pytest.param(
            lambda mp: fail_mkdir_of(mp, "golden"),
            MAKING_A_DIRECTORY,
            NO_SPACE,
            id="a directory",
        ),
        pytest.param(
            lambda mp: fail_open_of(mp, "graph.py"),
            CREATING_A_FILE,
            NO_SPACE,
            id="a new file",
        ),
        pytest.param(
            lambda mp: fail_replace_on(mp, "agents.yaml"),
            "replacing agents.yaml",
            DENIED,
            id="agents.yaml",
        ),
        pytest.param(
            lambda mp: fail_replace_on(mp, "services.yaml"),
            "replacing services.yaml",
            DENIED,
            id="services.yaml",
        ),
        pytest.param(
            lambda mp: fail_replace_on(mp, "pyproject.toml"),
            "replacing pyproject.toml",
            DENIED,
            id="pyproject.toml",
        ),
    ],
)
def test_a_failed_write_names_its_kind_and_never_a_path_the_command_created(
    root: Path, monkeypatch: pytest.MonkeyPatch, arrange: Any, kind: str, failure: str
) -> None:
    # Arrange
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    arrange(monkeypatch)

    # Act
    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    # Assert
    message = str(refused.value)
    assert message == (
        f"writing the workload failed ({kind}, {failure}); "
        "what was written has been removed"
    )
    assert message == WRITE_FAILED.format(kind, failure)
    assert refused.value.details == ()
    assert LEAKED not in message
    assert NAME not in message
    assert MODULE not in message
    assert snapshot(root) == before


def test_a_rollback_that_fails_names_the_kind_of_the_write_that_failed(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: a new file cannot be created, and the one made before it cannot go.
    plan = plan_workload(root, NAME)
    fail_open_of(monkeypatch, "evaluation.py")
    real_unlink = Path.unlink

    def unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name == "graph.py":
            raise PermissionError(errno.EACCES, LEAKED)
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    # Act
    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    # Assert
    message = str(refused.value)
    assert message == ROLLBACK_FAILED.format(CREATING_A_FILE, NO_SPACE)
    assert message == (
        f"writing the workload failed ({CREATING_A_FILE}, {NO_SPACE}) and could not "
        "be fully undone: check the working tree"
    )
    assert LEAKED not in message
    assert NAME not in message
    assert any(line.endswith("graph.py") for line in refused.value.details)


def fail_services_after_saving(
    monkeypatch: pytest.MonkeyPatch, root: Path, saved: str
) -> None:
    """Make the first replacement of ``services.yaml`` fail, and have the person
    save ``saved`` (a path under ``root``) with their own bytes just before it
    does. Only that first call fails: the undo's own write is the real one, so a
    file the undo leaves alone is left by its rule and not by a failing write."""
    real = scaffold_writes._replace
    failed: list[Path] = []

    def replace(path: Path, data: bytes) -> None:
        if path == root / SERVICES and not failed:
            failed.append(path)
            (root / saved).write_bytes(PERSONS_SAVE)
            raise PermissionError(errno.EACCES, LEAKED)
        real(path, data)

    monkeypatch.setattr(scaffold_writes, "_replace", replace)


def test_a_save_of_the_file_being_replaced_is_left_as_it_is_and_named(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: services.yaml is saved by the person, then its replacement fails.
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    fail_services_after_saving(monkeypatch, root, SERVICES)

    # Act
    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    # Assert: the person's bytes stay and are named as theirs; agents.yaml, which
    # still holds this command's bytes, goes back (changed on purpose: it used
    # to be left, and the registry validated with an agent that had no graph).
    assert str(refused.value) == ROLLBACK_FAILED.format(
        "replacing services.yaml", DENIED
    )
    assert refused.value.details == (LEFT_AS_SAVED.format(SERVICES),)
    assert snapshot(root) == {**before, SERVICES: PERSONS_SAVE}


def test_a_save_of_a_file_replaced_earlier_is_left_as_it_is_and_named(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: agents.yaml was replaced, then the person saved it, then the
    # replacement of services.yaml failed.
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    fail_services_after_saving(monkeypatch, root, AGENTS)

    # Act
    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    # Assert
    assert str(refused.value) == ROLLBACK_FAILED.format(
        "replacing services.yaml", DENIED
    )
    assert refused.value.details == (LEFT_AS_SAVED.format(AGENTS),)
    assert snapshot(root) == {**before, AGENTS: PERSONS_SAVE}


def test_a_file_the_undo_cannot_read_is_left_as_it_is_and_named(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: once pyproject.toml fails, agents.yaml can no longer be read, so
    # the undo cannot tell that it still holds what the command wrote.
    plan = plan_workload(root, NAME)
    real_replace, real_read = os.replace, Path.read_bytes
    failed = []

    def replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        if Path(destination).name == "pyproject.toml":
            failed.append(destination)
            raise PermissionError(errno.EACCES, LEAKED)
        real_replace(source, destination, *args, **kwargs)

    def read_bytes(self: Path) -> bytes:
        if failed and self.name == "agents.yaml":
            raise PermissionError(errno.EACCES, LEAKED)
        return real_read(self)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    # Act
    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    # Assert
    monkeypatch.undo()
    assert str(refused.value) == ROLLBACK_FAILED.format(
        "replacing pyproject.toml", DENIED
    )
    assert refused.value.details == (LEFT_BEHIND.format(AGENTS),)
    assert LEAKED not in str(refused.value)
    assert (root / AGENTS).read_bytes() == plan.changed[AGENTS].encode()


def test_a_file_still_holding_what_the_command_wrote_is_put_back(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other side of the rule: no save in between, so the undo is complete.
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    fail_replace_on(monkeypatch, "pyproject.toml")

    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    assert refused.value.details == ()
    assert snapshot(root) == before


def hook_replacements(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    before: dict[str, Callable[[], None]] | None = None,
    after: dict[str, Callable[[], None]] | None = None,
) -> None:
    """Run a callable just before, or just after, the real replacement of the
    file it is keyed by (a path relative to ``root``)."""
    real = scaffold_writes._replace
    before = before or {}
    after = after or {}

    def replace(path: Path, data: bytes) -> None:
        relative = path.relative_to(root).as_posix()
        before.get(relative, lambda: None)()
        real(path, data)
        after.get(relative, lambda: None)()

    monkeypatch.setattr(scaffold_writes, "_replace", replace)


def interrupt() -> None:
    raise KeyboardInterrupt


def fail_unlink_of(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real = Path.unlink

    def unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name == name:
            raise PermissionError(errno.EACCES, LEAKED)
        real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)


def save_services(root: Path) -> None:
    (root / SERVICES).write_bytes(PERSONS_SAVE)


def test_a_stale_plan_met_mid_write_with_a_failing_undo_names_what_is_left(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: services.yaml is saved after agents.yaml was replaced (the refusal
    # that used to say "nothing was written"), and graph.py cannot be removed.
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    hook_replacements(monkeypatch, root, after={AGENTS: lambda: save_services(root)})
    fail_unlink_of(monkeypatch, "graph.py")

    # Act
    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    # Assert: a write failure with its details, not the refusal; the file and the
    # directories above it that could not go are named; agents.yaml went back.
    assert str(refused.value) == ROLLBACK_FAILED.format(CHECKING_THE_PLAN, PLAN_CHANGED)
    assert "nothing was written" not in str(refused.value)
    graph = f"src/meridian/workloads/{MODULE}/graph.py"
    assert LEFT_BEHIND.format(graph) in refused.value.details
    assert all(line.startswith("left behind: ") for line in refused.value.details)
    after = snapshot(root)
    assert after[AGENTS] == before[AGENTS]
    assert after[SERVICES] == PERSONS_SAVE
    assert graph in after


def test_an_interrupt_with_a_save_prints_what_is_left_and_still_interrupts(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange: Ctrl-C at the replacement of pyproject.toml, after services.yaml
    # was replaced and then saved by the person.
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    hook_replacements(
        monkeypatch,
        root,
        before={PYPROJECT: interrupt},
        after={SERVICES: lambda: save_services(root)},
    )

    # Act
    with pytest.raises(KeyboardInterrupt):
        write_plan(root, plan)

    # Assert
    assert capsys.readouterr().err == (
        "ERROR "
        + ROLLBACK_FAILED.format(AN_INTERRUPT, "KeyboardInterrupt")
        + f"\nERROR {LEFT_AS_SAVED.format(SERVICES)}\n"
    )
    assert snapshot(root) == {**before, SERVICES: PERSONS_SAVE}


def test_an_interrupt_with_nothing_left_prints_nothing_and_propagates(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The other side of the rule: the undo is complete, so nothing is said.
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    hook_replacements(monkeypatch, root, before={PYPROJECT: interrupt})

    with pytest.raises(KeyboardInterrupt):
        write_plan(root, plan)

    assert capsys.readouterr().err == ""
    assert snapshot(root) == before


def test_any_other_exception_with_a_save_is_a_write_failure_naming_its_class_only(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: a RuntimeError whose text must not reach the person.
    plan = plan_workload(root, NAME)
    before = snapshot(root)

    def fail() -> None:
        raise RuntimeError(LEAKED)

    hook_replacements(
        monkeypatch,
        root,
        before={PYPROJECT: fail},
        after={SERVICES: lambda: save_services(root)},
    )

    # Act
    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    # Assert
    assert str(refused.value) == ROLLBACK_FAILED.format(
        AN_UNEXPECTED_ERROR, "RuntimeError"
    )
    assert refused.value.details == (LEFT_AS_SAVED.format(SERVICES),)
    assert LEAKED not in str(refused.value)
    assert snapshot(root) == {**before, SERVICES: PERSONS_SAVE}


def test_any_other_exception_with_nothing_left_propagates_unchanged(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_workload(root, NAME)
    before = snapshot(root)

    def fail() -> None:
        raise RuntimeError(LEAKED)

    hook_replacements(monkeypatch, root, before={PYPROJECT: fail})

    with pytest.raises(RuntimeError, match=LEAKED):
        write_plan(root, plan)

    assert snapshot(root) == before
