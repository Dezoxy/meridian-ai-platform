"""A directory the process may not read, made without ``chmod`` (S061).

The suite may run as root in a container, where a real ``chmod 000`` still
lets every read through. ``make_unreadable`` patches the two ``pathlib`` calls
the registry loader makes instead, and only for paths under one directory, so
the rest of the test (and pytest itself) reads normally.
"""

import errno
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest

Failure = Literal["listing", "stat"]
FAILURES: tuple[Failure, ...] = ("listing", "stat")
# A message the loader must never repeat: it is the OSError's own text.
CANARY = "CANARY-os-error-text"


def _denied() -> PermissionError:
    return PermissionError(errno.EACCES, CANARY)


def make_unreadable(
    monkeypatch: pytest.MonkeyPatch, directory: Path, failure: Failure
) -> None:
    """``listing``: ``directory`` cannot be listed (no read bit). ``stat``:
    nothing under it can be stat'ed (no search bit)."""
    real_iterdir = Path.iterdir
    real_is_file = Path.is_file

    def iterdir(self: Path) -> Iterator[Path]:
        if failure == "listing" and self == directory:
            raise _denied()
        return real_iterdir(self)

    def is_file(self: Path, *, follow_symlinks: bool = True) -> bool:
        if failure == "stat" and directory in self.parents:
            raise _denied()
        return real_is_file(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "is_file", is_file)


WriteFailure = Literal["mkdir", "write"]
WRITE_FAILURES: tuple[WriteFailure, ...] = ("mkdir", "write")


def make_unwritable(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    failure: WriteFailure,
    *,
    let_through: int = 0,
) -> None:
    """The write side of ``make_unreadable`` (S076). ``mkdir``: ``directory``
    cannot be made. ``write``: no file can be written under it, after
    ``let_through`` writes have gone through."""
    real_mkdir = Path.mkdir
    real_write_text = Path.write_text
    written = 0

    def mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if failure == "mkdir" and self == directory:
            raise _denied()
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    def write_text(self: Path, data: str, *args: object, **kwargs: object) -> int:
        nonlocal written
        if failure == "write" and directory in self.parents:
            if written >= let_through:
                raise _denied()
            written += 1
        return real_write_text(self, data, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "write_text", write_text)
