"""The scaffold's writes and their undo (S039, S076, T-81).

``write_plan`` writes what ``scaffold.plan_workload`` planned: the new files, then
the edited ones. T-81's promise is the measure: after it fails, the tree is as it
was, or every difference is named to the person. Every write is noted before it
is made, so an interrupt between the note and the write is undone too; the undo
puts back every file that still holds the bytes this command wrote, leaves a file
the person saved since and says so, and names each path it could not restore. When
something ends the undo itself (a second interrupt, an exit, an error), what it had
done is known to the caller, which says so and names every path of the plan that is
not known to be settled, to be checked by hand. A temporary file ``_replace`` makes
is noted before it exists, removed by the undo and named like any other path when
it stays. The fixed line "check the working tree" is written before the paths are
listed.

What cannot be closed: an interrupt that comes before that first line is written
(after the undo has returned, while the error is built) leaves the person no
line, and a kill the process cannot catch leaves nothing said at all; and an
error the undo raises after an interrupt ends the command as a write error
(exit 1), not as the interrupt.

The errors and the plan are here and not in ``scaffold``, which imports this
module: the write needs them and must not import its importer. ``scaffold``
re-exports them, so callers keep importing from there. Fixed texts: a kind of
write, never a path the command created, except the paths of files left behind or
to be checked by hand (they hold the workload's name, which is the person's own
argument and the only way to find them), and for an error nothing but its class.

The block moved here whole from ``scaffold`` with two changes in what it does:
``_StepFailed`` holds the failure's text (``failure``, a string) where it held the
``OSError`` (``cause``), and ``_writing`` computes that text when it raises, not
when the failure is reported.
"""

import hashlib
import os
import secrets
import stat
import sys
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import partial
from itertools import chain
from pathlib import Path
from typing import NoReturn

from meridian.platform.cli.scaffold_services import SERVICES_PATH

AGENTS_PATH = "config/registry/agents.yaml"
PYPROJECT_PATH = "pyproject.toml"
PATH_EXISTS = "a file or directory the workload would create already exists"
STALE_PLAN = (
    "agents.yaml, services.yaml or pyproject.toml changed after the plan was made, "
    "or cannot be read again: nothing was written; run the command again"
)
# The fields are the kind of write that failed, one of the fixed words below, and
# what ``_failure`` says of the error: never a path, never the error's own text.
WRITE_FAILED = "writing the workload failed ({}, {}); what was written has been removed"
ROLLBACK_FAILED = (
    "writing the workload failed ({}, {}) and could not be fully undone: "
    "check the working tree"
)
# The field of each is a path relative to the checkout. The first: the undo could
# not put it back or remove it. The second: it no longer holds what this command
# wrote, so it is the person's and was not touched.
LEFT_BEHIND = "left behind: {}"
LEFT_AS_SAVED = "changed since this command wrote it, left as it is: {}"
# The undo itself was ended, by an interrupt, an exit or an error: the field is the
# class of what ended it, never its text. The lines after it are the paths it left
# (the two above) and, for each other path of the plan the undo had not finished
# with, the second text: whether it was put back is not known.
UNDO_UNFINISHED = "the undo did not finish ({}): check the working tree"
CHECK_BY_HAND = "not known to be undone, check by hand: {}"
# What stopped a write that no ``OSError`` stopped: the two fields of the texts
# above, a kind and a failure. The class's name is all that is said of an error.
CHECKING_THE_PLAN = "checking a file against the plan"
PLAN_CHANGED = "a file changed since the plan was made or cannot be read again"
AN_UNEXPECTED_ERROR = "an unexpected error"
AN_INTERRUPT = "an interrupt"
# The kinds of write a failure is named by: a closed set of fixed words. A created
# path would hold the workload's name, which no message of the scaffold quotes; only
# the lines about a path left behind or to be checked by hand carry one.
MAKING_A_DIRECTORY = "making a directory"
CREATING_A_FILE = "creating a new file"
REPLACING = {
    AGENTS_PATH: "replacing agents.yaml",
    SERVICES_PATH: "replacing services.yaml",
    PYPROJECT_PATH: "replacing pyproject.toml",
}
# The order of the writes. A half-written tree must still validate and must never
# let the runtime name an agent that does not exist: the agent comes first, then
# the runtime's right to name it, then the entry points that load the workload.
WRITE_ORDER = (AGENTS_PATH, SERVICES_PATH, PYPROJECT_PATH)


class ScaffoldError(Exception):
    """Refused; the message is a fixed text and never quotes the name. ``details``
    are further lines the caller prints after it: the registry's own messages, or
    the paths a rollback could not put back or that are to be checked by hand (these
    quote the name, as a created path holds it)."""

    def __init__(self, message: str, *, details: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.details = details


class ScaffoldWriteError(ScaffoldError):
    """A write failed: not a refusal, the plan was sound."""


@dataclass(frozen=True, slots=True)
class Plan:
    name: str  # the agent ID and the entry points' name
    module: str  # name with "-" replaced by "_"
    created: Mapping[str, str]  # relative POSIX path -> text of a new file
    changed: Mapping[str, str]  # relative POSIX path -> whole new text
    # relative POSIX path -> SHA-256 of the bytes a changed file was planned from
    base: Mapping[str, str]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _refuse_a_stale_plan(root: Path, plan: Plan) -> None:
    """Refuse when a file the plan edits is not what it was planned from."""
    try:
        stale = any(
            _digest((root / relative).read_bytes().decode("utf-8")) != digest
            for relative, digest in plan.base.items()
        )
    except (OSError, UnicodeDecodeError):
        stale = True
    if stale:
        raise ScaffoldError(STALE_PLAN)


def _replace(path: Path, data: bytes, temporaries: list[Path]) -> None:
    """Replace ``path`` with ``data`` in one step, keeping its mode: the bytes go
    to a temporary file beside it, which then takes its place. The temporary's path
    is chosen and noted in ``temporaries`` (the caller's) before the file exists, so
    that the undo can remove it whatever ends this call; it is taken out of the list
    once it has replaced ``path`` or has been removed. A failure removing it is not
    this call's to report: it stays noted, and the undo tries again and names it."""
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporaries.append(temporary)
    try:
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # not ours, and not to be removed
            temporaries.remove(temporary)
            raise
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporaries.remove(temporary)
    except BaseException:
        if temporary in temporaries:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
                temporaries.remove(temporary)
        raise


class _StepFailed(Exception):
    """An ``OSError`` met while writing: the kind of write it was met in and what
    ``_failure`` says of it, both fixed words."""

    def __init__(self, kind: str, failure: str) -> None:
        super().__init__(kind)
        self.kind = kind
        self.failure = failure


@contextmanager
def _writing(kind: str) -> Iterator[None]:
    """Make an ``OSError`` of the block a ``_StepFailed`` of ``kind``."""
    try:
        yield
    except OSError as exc:
        raise _StepFailed(kind, _failure(exc)) from exc


def _make_directories(directory: Path, made: list[Path]) -> None:
    """Create ``directory`` and the parents that are missing, noting each one
    this call made in ``made``, outermost first. A directory is noted before it
    is made, so that an interrupt between the two is undone; one that turns out to
    exist already is not this call's, and is taken out again."""
    missing = []
    current = directory
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    for each in reversed(missing):
        made.append(each)
        with _writing(MAKING_A_DIRECTORY):
            try:
                each.mkdir()
            except FileExistsError:
                made.remove(each)
                raise


def _failure(exc: OSError) -> str:
    """What the error says of an ``OSError``: its type and, when it carries an
    errno, the operating system's text for it. Never a path or the name."""
    if exc.errno is None:
        return type(exc).__name__
    return f"{type(exc).__name__}: {os.strerror(exc.errno)}"


def _put_back(path: Path, old: bytes, new: bytes, temporaries: list[Path]) -> bool:
    """Make ``path`` hold ``old`` again, only over ``new``, the bytes this command
    wrote; ``False`` when it holds anything else and was left as it is. A file
    that already holds ``old`` (its replacement never happened, perhaps because it
    is the one that failed) is left alone and counts as back: the same failure
    would otherwise be met again and reported as an undo that failed. Anything
    else is a save of the person's, which the undo must not overwrite. A file that
    cannot be read raises ``OSError``.

    This closes the window of the undo and no other: a save that lands between the
    read here and the replacement just after it, or between a file's own check in
    ``_read_as_planned`` and its replacement, is lost to the replacement. Closing
    them takes a lock the person's editor would have to honour; none is built."""
    current = path.read_bytes()
    if current == old:
        return True
    if current != new:
        return False
    _replace(path, old, temporaries)
    return True


def _settle(
    root: Path, outcome: dict[str, str | None], path: Path, template: str | None = None
) -> None:
    """Note in ``outcome`` how ``path`` ended: ``None``, or ``template`` with it."""
    relative = path.relative_to(root).as_posix()
    outcome[relative] = None if template is None else template.format(relative)


def _undo(
    root: Path,
    files: list[Path],
    directories: list[Path],
    replaced: list[tuple[Path, bytes, bytes]],
    temporaries: list[Path],
    outcome: dict[str, str | None],
) -> None:
    """Put back what a failed write changed. ``outcome`` is the caller's, filled as
    the undo goes, so that it holds what was done if something ends the undo midway:
    each path (relative to the checkout) in the order tried, ``None`` when it is
    back as it was, else the line that says which of two it is. Everything is noted
    before it is done, so an entry may be one whose change never happened:
    ``replaced`` holds each edited file with its old bytes and the new bytes this
    command writes to it, in the order they were replaced, and goes back in the
    reverse order. Every file that still holds the bytes this command wrote is put
    back, whatever happened to the others: a file not yet replaced already holds
    the old bytes and is left alone; a file that holds neither was saved by the
    person and is the only kind left as it is (``LEFT_AS_SAVED``: what the person
    saved decides what is consistent); one the undo could not read or replace, a
    file in ``files`` or a directory in ``directories`` that would not go, is
    ``LEFT_BEHIND``. A file or directory that was never created is skipped. The
    temporary files ``_replace`` noted in ``temporaries`` are removed last, the
    undo's own among them, and one that will not go is ``LEFT_BEHIND`` too."""

    settle = partial(_settle, root, outcome)
    for path, old, new in reversed(replaced):
        try:
            put_back = _put_back(path, old, new, temporaries)
        except OSError:
            settle(path, LEFT_BEHIND)
        else:
            settle(path, None if put_back else LEFT_AS_SAVED)
    for file in (*files, *temporaries):
        try:
            file.unlink(missing_ok=True)
        except OSError:
            settle(file, LEFT_BEHIND)
        else:
            settle(file)
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except FileNotFoundError:
            settle(directory)
        except OSError:
            settle(directory, LEFT_BEHIND)
        else:
            settle(directory)


def _create_files(
    targets: list[tuple[Path, str]], files: list[Path], directories: list[Path]
) -> None:
    """Create each file of ``targets`` with its text, and the directories it
    needs, noting what this call made in ``files`` and ``directories``. A file is
    noted before it is created, so that an interrupt between the two is undone;
    one that turns out to exist already is not this call's, and is taken out
    again so that the undo leaves it alone."""
    for path, text in targets:
        _make_directories(path.parent, directories)
        files.append(path)
        with _writing(CREATING_A_FILE):
            try:
                with open(path, "x", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
            except FileExistsError:  # only the exclusive creation can say so
                files.remove(path)
                raise


def _read_as_planned(path: Path, digest: str) -> bytes:
    """The bytes of ``path``, which must be what the plan was made from: raise the
    stale plan's own error when it cannot be read or hashes to something else."""
    try:
        old = path.read_bytes()
    except OSError:
        raise ScaffoldError(STALE_PLAN) from None
    if hashlib.sha256(old).hexdigest() != digest:
        raise ScaffoldError(STALE_PLAN)
    return old


def _replace_files(
    root: Path,
    plan: Plan,
    replaced: list[tuple[Path, bytes, bytes]],
    temporaries: list[Path],
) -> None:
    """Replace the edited files in ``WRITE_ORDER``. Each is noted, with the bytes
    it was planned from and the bytes it is given, before it is replaced, so that
    an interrupt between the two is undone; a file that is not what the plan was
    made from (saved after the check at the start of ``write_plan``) is not
    touched. ``temporaries`` is where ``_replace`` notes its temporary files."""
    for relative in WRITE_ORDER:
        path = root / relative
        old = _read_as_planned(path, plan.base[relative])
        new = plan.changed[relative].encode("utf-8")
        replaced.append((path, old, new))
        with _writing(REPLACING[relative]):
            _replace(path, new, temporaries)


def _what_stopped(exc: BaseException) -> tuple[str, str]:
    """The two fields of ``ROLLBACK_FAILED`` for what ended a write: fixed words,
    and for an error nothing but its class's name, never its text."""
    if isinstance(exc, _StepFailed):
        return exc.kind, exc.failure
    if isinstance(exc, ScaffoldError):  # the only refusal met mid-write: STALE_PLAN
        return CHECKING_THE_PLAN, PLAN_CHANGED
    kind = AN_UNEXPECTED_ERROR if isinstance(exc, Exception) else AN_INTERRUPT
    return kind, type(exc).__name__


def _say(message: str, lines: Iterable[str]) -> None:
    """Write ``message`` and then ``lines`` to standard error, as the command prints
    an error. ``lines`` may be built as it is written: ``message`` is out before the
    first of them is worked out. A stream that cannot be written (a pipe the reader
    closed) says nothing and must not replace what ended the command, so its error
    is dropped."""
    try:
        sys.stderr.write(f"ERROR {message}\n")
        for line in lines:
            sys.stderr.write(f"ERROR {line}\n")
    except (OSError, ValueError):
        pass


def _end_unfinished(
    root: Path,
    plan: Plan,
    directories: list[Path],
    temporaries: list[Path],
    outcome: dict[str, str | None],
    ended: BaseException,
) -> NoReturn:
    """End ``write_plan`` after ``ended`` (an interrupt, an exit or an error) ended
    the undo itself. What it left is in ``outcome``; the plan knows every path it
    creates or replaces, and ``temporaries`` those of the files being replaced, so
    each one the undo had not settled is named to be checked by hand. An error
    becomes a ``ScaffoldWriteError`` with the lines as details, an interrupt or an
    exit is said on standard error and goes on: the fixed line first, then the paths,
    which are worked out only as they are written."""
    message = UNDO_UNFINISHED.format(type(ended).__name__)
    lines = chain(
        (line for line in outcome.values() if line),
        _to_check(root, plan, (*directories, *temporaries), outcome),
    )
    if isinstance(ended, Exception):
        raise ScaffoldWriteError(message, details=tuple(lines)) from ended
    _say(message, lines)
    raise ended


def _to_check(
    root: Path, plan: Plan, made: tuple[Path, ...], outcome: dict[str, str | None]
) -> Iterator[str]:
    """The lines to check by hand: each path of the plan, and each directory or
    temporary file in ``made``, that ``outcome`` does not hold."""
    relative = (path.relative_to(root).as_posix() for path in made)
    for path in (*plan.created, *relative, *WRITE_ORDER):
        if path not in outcome:
            yield CHECK_BY_HAND.format(path)


def write_plan(root: Path, plan: Plan) -> None:
    """Write ``plan`` under ``root``: the new files, then ``agents.yaml``, then
    ``services.yaml`` (``WRITE_ORDER``), then ``pyproject.toml``, whose entry
    points are what makes the platform load the workload. Refuse, before the first
    write, a plan whose three edited files are no longer what it was planned from;
    each is checked again just before its own replacement, and a file that
    changed since ends the write with the same refusal. When a write fails, or
    the process is interrupted, remove what this call made and put the edited
    files back (a file the person saved in the meantime is left as it is). When
    that leaves nothing behind, an ``OSError`` becomes a ``ScaffoldWriteError``
    that names the kind of write that failed and anything else propagates
    unchanged. When something is left, whatever ended the write says so: a
    ``ScaffoldWriteError`` with the paths as ``details`` (the stale plan's refusal
    included, for it no longer holds that nothing was written), and for an
    interrupt the same lines on standard error before it propagates. When
    something ends the undo itself, ``_end_unfinished`` says so in the same two
    ways, with every path not known to be settled, and the cause is the error."""
    targets = [(root / relative, text) for relative, text in plan.created.items()]
    if any(os.path.lexists(path) for path, _ in targets):
        raise ScaffoldError(PATH_EXISTS)
    _refuse_a_stale_plan(root, plan)
    files: list[Path] = []
    directories: list[Path] = []
    replaced: list[tuple[Path, bytes, bytes]] = []
    temporaries: list[Path] = []
    try:
        _create_files(targets, files, directories)
        _replace_files(root, plan, replaced, temporaries)
    except BaseException as exc:
        outcome: dict[str, str | None] = {}
        try:
            _undo(root, files, directories, replaced, temporaries, outcome)
            left = tuple(line for line in outcome.values() if line)
        except BaseException as ended:
            _end_unfinished(root, plan, directories, temporaries, outcome, ended)
        if not left:
            if isinstance(exc, _StepFailed):
                raise ScaffoldWriteError(
                    WRITE_FAILED.format(exc.kind, exc.failure)
                ) from exc
            raise
        message = ROLLBACK_FAILED.format(*_what_stopped(exc))
        if isinstance(exc, Exception):
            raise ScaffoldWriteError(message, details=left) from exc
        # An interrupt must still end the command as one does: what is left is said
        # on standard error first, as the command prints an error, and it goes on.
        _say(message, left)
        raise
