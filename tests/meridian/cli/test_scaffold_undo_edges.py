"""The last gaps round the scaffold's undo (S076, T-81, the fifth review).

A temporary file that ``_replace`` leaves behind is named like any other path left
behind, and the command never says everything was removed while one stays. What is
left is worked out where an interrupt still leads to the lines, and the line that
matters most is written before the paths are listed. Every test builds a small tree
in ``tmp_path`` (the ``root`` fixture).
"""

import errno
import fnmatch
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meridian.platform.cli import scaffold_writes
from meridian.platform.cli.scaffold import (
    AN_INTERRUPT,
    LEFT_BEHIND,
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
EDITED = (AGENTS, SERVICES, PYPROJECT)
LEAKED = "a-path-or-a-name-the-error-must-not-repeat"
READ_ONLY = f"OSError: {os.strerror(errno.EROFS)}"
TEMPORARY = ".{}.*.tmp"


def snapshot(root: Path) -> dict[str, object]:
    found: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        found[relative] = None if path.is_dir() else path.read_bytes()
    return found


def differing(before: dict[str, object], after: dict[str, object]) -> set[str]:
    """The paths that are not as they were: changed, new or gone."""
    missing = object()
    return {
        path
        for path in before.keys() | after.keys()
        if before.get(path, missing) != after.get(path, missing)
    }


def interrupt() -> None:
    raise KeyboardInterrupt


def on_call(number: int, action: Callable[[], None]) -> Callable[[], None]:
    """A hook that runs ``action`` on its ``number``-th call and no other."""
    calls: list[None] = []

    def hook() -> None:
        calls.append(None)
        if len(calls) == number:
            action()

    return hook


def hook_replacements(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    before: dict[str, Callable[[], None]],
) -> None:
    """Run a callable just before the real replacement of the file it is keyed by
    (a path relative to ``root``)."""
    real = scaffold_writes._replace

    def replace(path: Path, data: bytes, *rest: Any) -> None:
        before.get(path.relative_to(root).as_posix(), lambda: None)()
        real(path, data, *rest)

    monkeypatch.setattr(scaffold_writes, "_replace", replace)


def fail_replace_of(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    """A read-only file system at the replacement of ``target``."""
    real = os.replace

    def replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        if Path(destination).name == target:
            raise OSError(errno.EROFS, LEAKED, str(destination))
        real(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def fail_unlink_of_temporaries(monkeypatch: pytest.MonkeyPatch) -> None:
    real = Path.unlink

    def unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name.endswith(".tmp"):
            raise PermissionError(errno.EACCES, LEAKED)
        real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)


def interrupt_once_made(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    """An interrupt right after the first temporary file for ``target`` exists."""
    real = os.open
    done: list[str] = []

    def open_(path: Any, *args: Any, **kwargs: Any) -> int:
        descriptor = real(path, *args, **kwargs)
        if fnmatch.fnmatch(Path(path).name, TEMPORARY.format(target)) and not done:
            done.append(str(path))
            os.close(descriptor)
            raise KeyboardInterrupt
        return descriptor

    monkeypatch.setattr(os, "open", open_)


def the_temporaries(paths: set[str]) -> list[str]:
    return [path for path in paths if path.endswith(".tmp")]


def named_in(lines: list[str]) -> set[str]:
    """The paths of the lines on standard error that name one, left behind or to be
    checked by hand; any other line fails."""
    texts = (LEFT_BEHIND.format(""), scaffold_writes.CHECK_BY_HAND.format(""))
    named = set()
    for line in lines:
        text = line.removeprefix("ERROR ")
        (prefix,) = [each for each in texts if text.startswith(each)]
        named.add(text.removeprefix(prefix))
    return named


def test_a_temporary_that_stays_is_named_and_the_message_does_not_say_removed(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: a read-only file system at the replacement of pyproject.toml, and the
    # temporary file beside it cannot be removed either.
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    fail_replace_of(monkeypatch, PYPROJECT)
    fail_unlink_of_temporaries(monkeypatch)

    # Act
    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    # Assert
    message = str(refused.value)
    left = the_temporaries(differing(before, snapshot(root)))
    assert message == ROLLBACK_FAILED.format("replacing pyproject.toml", READ_ONLY)
    assert "has been removed" not in message
    assert len(left) == 1
    assert fnmatch.fnmatch(left[0], TEMPORARY.format(PYPROJECT))
    assert refused.value.details == (LEFT_BEHIND.format(left[0]),)
    assert differing(before, snapshot(root)) == set(left)
    assert LEAKED not in message + "".join(refused.value.details)


def test_a_temporary_that_goes_leaves_the_plain_message_that_all_was_removed(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other side of the rule: the same failure, and the temporary can be removed.
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    fail_replace_of(monkeypatch, PYPROJECT)

    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    assert str(refused.value) == WRITE_FAILED.format(
        "replacing pyproject.toml", READ_ONLY
    )
    assert refused.value.details == ()
    assert snapshot(root) == before


@pytest.mark.parametrize("target", EDITED)
def test_an_interrupt_right_after_a_temporary_is_made_leaves_the_tree_as_it_was(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target: str,
) -> None:
    # Arrange
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    interrupt_once_made(monkeypatch, Path(target).name)

    # Act
    with pytest.raises(KeyboardInterrupt):
        write_plan(root, plan)

    # Assert: nothing is left, so nothing is said.
    assert snapshot(root) == before
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("target", EDITED)
def test_a_temporary_left_by_an_interrupt_is_named_on_standard_error(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target: str,
) -> None:
    # Arrange: the interrupt comes right after the temporary is made, and it cannot
    # be removed.
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    interrupt_once_made(monkeypatch, Path(target).name)
    fail_unlink_of_temporaries(monkeypatch)

    # Act
    with pytest.raises(KeyboardInterrupt):
        write_plan(root, plan)

    # Assert
    left = the_temporaries(differing(before, snapshot(root)))
    assert len(left) == 1
    assert differing(before, snapshot(root)) == set(left)
    assert capsys.readouterr().err == (
        "ERROR "
        + ROLLBACK_FAILED.format(AN_INTERRUPT, "KeyboardInterrupt")
        + f"\nERROR {LEFT_BEHIND.format(left[0])}\n"
    )


def test_a_temporary_is_checked_by_hand_when_the_undo_is_ended_before_it(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange: pyproject.toml fails and its temporary stays; then the undo is
    # interrupted at the put-back of services.yaml, before it reaches temporaries.
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    fail_replace_of(monkeypatch, PYPROJECT)
    fail_unlink_of_temporaries(monkeypatch)
    hook_replacements(monkeypatch, root, {SERVICES: on_call(2, interrupt)})

    # Act
    with pytest.raises(KeyboardInterrupt):
        write_plan(root, plan)

    # Assert: every path that differs is named, the temporary among them.
    prefix = "ERROR " + scaffold_writes.CHECK_BY_HAND.format("")
    lines = capsys.readouterr().err.splitlines()
    named = {line.removeprefix(prefix) for line in lines[1:]}
    assert lines[0] == "ERROR " + scaffold_writes.UNDO_UNFINISHED.format(
        "KeyboardInterrupt"
    )
    assert named == differing(before, snapshot(root))
    assert the_temporaries(named)


class OnceFalse(str):
    """A line that raises an interrupt the first time it is tested for truth."""

    raised = False

    def __bool__(self) -> bool:
        if not OnceFalse.raised:
            OnceFalse.raised = True
            raise KeyboardInterrupt
        return True


def test_an_interrupt_after_the_undo_returned_still_names_what_it_left(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange: graph.py cannot be removed, so the undo ends with paths left; the
    # interrupt comes when those are collected, just after the undo returned.
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    real_unlink = Path.unlink
    real_undo = scaffold_writes._undo
    OnceFalse.raised = False

    def unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name == "graph.py":
            raise PermissionError(errno.EACCES, LEAKED)
        real_unlink(self, *args, **kwargs)

    def undo(*args: Any) -> None:
        real_undo(*args)
        outcome = args[-1]
        for path, line in outcome.items():
            if line:
                outcome[path] = OnceFalse(line)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(scaffold_writes, "_undo", undo)
    hook_replacements(monkeypatch, root, {PYPROJECT: interrupt})

    # Act
    with pytest.raises(KeyboardInterrupt):
        write_plan(root, plan)

    # Assert: the lines were written, and they name every path that differs.
    lines = capsys.readouterr().err.splitlines()
    assert OnceFalse.raised
    assert lines[0] == "ERROR " + scaffold_writes.UNDO_UNFINISHED.format(
        "KeyboardInterrupt"
    )
    assert named_in(lines[1:]) == differing(before, snapshot(root))


class InterruptedWhenFormatted(str):
    """The fixed text of a line to check by hand, whose formatting is interrupted."""

    def format(self, *args: Any, **kwargs: Any) -> str:
        raise KeyboardInterrupt


def test_the_first_line_is_written_before_the_paths_are_listed(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange: a second interrupt ends the undo at the put-back of services.yaml,
    # and a third one comes while the first path to check is being worded.
    plan = plan_workload(root, NAME)
    hook_replacements(
        monkeypatch, root, {SERVICES: on_call(2, interrupt), PYPROJECT: interrupt}
    )
    monkeypatch.setattr(
        scaffold_writes,
        "CHECK_BY_HAND",
        InterruptedWhenFormatted(scaffold_writes.CHECK_BY_HAND),
    )

    # Act
    with pytest.raises(KeyboardInterrupt):
        write_plan(root, plan)

    # Assert: the one line that matters reached the person.
    assert capsys.readouterr().err == (
        "ERROR " + scaffold_writes.UNDO_UNFINISHED.format("KeyboardInterrupt") + "\n"
    )
