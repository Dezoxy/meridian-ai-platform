"""What ``meridian workload new`` says when the registry cannot be read (S076).

``load_registry`` turns an ``OSError`` into a ``RegistryError`` itself, so the
scaffold's own handler is not reached through it. It is reached when the
scaffold's check of the tree stats the registry's agents file and may not: the
person gets one line and no traceback. The directory is made unreadable the way
``directorysupport`` does it, without ``chmod``, which the suite may run as root
over.
"""

from pathlib import Path

import pytest
from directorysupport import CANARY, FAILURES, Failure, make_unreadable
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli.scaffold import (
    REGISTRY_INVALID,
    REGISTRY_UNREADABLE,
    ScaffoldError,
    ScaffoldWriteError,
    plan_workload,
)

runner = CliRunner()
NAME = "fraud-review"
REGISTRY = "config/registry"
EXIT_REFUSED = 2


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def test_a_registry_that_cannot_be_statted_is_refused_in_one_line_and_no_traceback(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    before = snapshot(root)
    make_unreadable(monkeypatch, root / REGISTRY, "stat")

    # Act
    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(root)])

    # Assert
    assert result.exit_code == EXIT_REFUSED
    assert result.stdout == ""
    assert (
        result.stderr == "ERROR " + REGISTRY_UNREADABLE.format("PermissionError") + "\n"
    )
    assert CANARY not in result.output
    assert NAME not in result.output
    assert not isinstance(result.exception, PermissionError)
    monkeypatch.undo()  # the snapshot reads the tree the way the person can
    assert snapshot(root) == before


def test_the_refusal_is_a_scaffold_refusal_and_never_quotes_the_errors_text(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_unreadable(monkeypatch, root / REGISTRY, "stat")

    with pytest.raises(ScaffoldError) as refused:
        plan_workload(root, NAME)

    assert str(refused.value) == REGISTRY_UNREADABLE.format("PermissionError")
    assert CANARY not in str(refused.value)
    assert not isinstance(refused.value, ScaffoldWriteError)
    assert refused.value.details == ()


def test_a_registry_directory_that_cannot_be_listed_is_the_registrys_own_refusal(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `load_registry` turns the OSError into a RegistryError: the scaffold says
    # the registry does not validate and prints the registry's line under it.
    make_unreadable(monkeypatch, root / REGISTRY, "listing")

    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(root)])

    assert result.exit_code == EXIT_REFUSED
    assert result.stdout == ""
    first, *details = result.stderr.splitlines()
    assert first == "ERROR " + REGISTRY_INVALID
    assert len(details) == 1
    assert details[0].endswith("registry directory cannot be read: PermissionError")
    assert CANARY not in result.output
    assert not isinstance(result.exception, PermissionError)


@pytest.mark.parametrize("failure", FAILURES)
def test_no_way_of_not_reading_the_registry_ends_in_a_traceback(
    root: Path, monkeypatch: pytest.MonkeyPatch, failure: Failure
) -> None:
    make_unreadable(monkeypatch, root / REGISTRY, failure)

    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(root)])

    assert result.exit_code == EXIT_REFUSED
    assert result.stderr.startswith("ERROR ")
    assert not isinstance(result.exception, OSError)
