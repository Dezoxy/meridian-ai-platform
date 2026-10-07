#!/usr/bin/env python3
"""Fail when a source file passes the 800-line ceiling without an exception (S074).

The ceiling is the soft maintainability limit of
.claude/rules/ecc/common/coding-style.md. This is its mechanical half: a file
the ceiling applies to may not pass it, unless ``scripts/file-size-exceptions.txt``
names the file with the line count it has now and a reason.

What is checked: the files git tracks (``git ls-files``) that are source, that
is Python under ``src/``, ``tests/``, ``scripts/``, ``data/synthetic/generator/``
and ``spikes/``, and shell under ``infra/`` and ``scripts/``. The vendored kit
(``.claude/``, ``.agents/``, ``.codex/``), generated files and data are not
source for this check, and no path outside the prefixes is read.

The exceptions file is a ratchet: an entry can only be lowered or removed. One
line per file: the path, the line count the file has, and a reason of five words
or more, separated by spaces; blank lines and lines that start with ``#`` are
ignored. A listed file that is over its count fails (it grew), and so does one
that is under it while still over the ceiling (it shrank: lower the entry to
the count it has now, so that it cannot grow back). A listed file that is now
800 lines or under, that is gone, or that is not source for this check, fails
too, with the line to remove, so the list can only get shorter.

Lines are counted as ``wc -l`` counts them (newlines, plus one for a last line
with no newline).

Run from anywhere: python3 scripts/check_file_sizes.py [--root DIR]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

CEILING = 800
MIN_REASON_WORDS = 5
EXCEPTIONS_FILE = "scripts/file-size-exceptions.txt"

# A path is source for this check when it ends with the suffix and starts with
# one of the prefixes. Nothing else is read, so a vendored, generated or data
# file never counts, whatever its extension.
SOURCE_PREFIXES: dict[str, tuple[str, ...]] = {
    ".py": ("src/", "tests/", "scripts/", "data/synthetic/generator/", "spikes/"),
    ".sh": ("infra/", "scripts/"),
}
VENDORED_PREFIXES = (".claude/", ".agents/", ".codex/")


def is_source(path: str) -> bool:
    if path.startswith(VENDORED_PREFIXES):
        return False
    prefixes = SOURCE_PREFIXES.get(Path(path).suffix, ())
    return path.startswith(prefixes)


def tracked_files(root: Path) -> list[str]:
    """Every path git tracks under ``root``, as ``git ls-files`` prints it."""
    done = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise SystemExit(f"check_file_sizes: git ls-files failed in {root}: {detail}")
    return [p for p in done.stdout.decode("utf-8").split("\0") if p]


def count_lines(path: Path) -> int | None:
    """The file's lines as ``wc -l`` counts them; None when it is not on disk."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def read_exceptions(text: str) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """The exceptions as ``{path: (allowed lines, line number in the file)}``.

    The list that comes with it holds one message per line that is not a valid
    exception.
    """
    allowed: dict[str, tuple[int, int]] = {}
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        where = f"{EXCEPTIONS_FILE}:{number}"
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdecimal():
            problems.append(
                f"{where}: a line is `path count reason`, with a whole number "
                f"for the count; this one is not"
            )
            continue
        path, count, reason = parts[0], int(parts[1]), parts[2:]
        if len(reason) < MIN_REASON_WORDS:
            problems.append(
                f"{where}: {path} has a reason of {len(reason)} words; write at "
                f"least {MIN_REASON_WORDS}, saying why the file is over the ceiling"
            )
        if path in allowed:
            problems.append(
                f"{where}: {path} is listed twice (first at line "
                f"{allowed[path][1]}); remove one"
            )
            continue
        allowed[path] = (count, number)
    return allowed, problems


def check(root: Path, files: list[str], exceptions_text: str) -> list[str]:
    """Every failure, one line each; an empty list is a pass."""
    allowed, failures = read_exceptions(exceptions_text)
    sizes: dict[str, int] = {}
    for path in files:
        if not is_source(path):
            continue
        lines = count_lines(root / path)
        if lines is not None:
            sizes[path] = lines

    for path, lines in sorted(sizes.items()):
        if lines <= CEILING:
            continue
        if path not in allowed:
            failures.append(
                f"{path}: {lines} lines, over the ceiling of {CEILING}; split it "
                f"along its sections, or add `{path} {lines} <reason>` to "
                f"{EXCEPTIONS_FILE} with a reason"
            )
        elif lines > allowed[path][0]:
            failures.append(
                f"{path}: {lines} lines, over the {allowed[path][0]} recorded in "
                f"{EXCEPTIONS_FILE}:{allowed[path][1]}; an excepted file may "
                f"shrink and may not grow: cut it back, do not raise the count"
            )
        elif lines < allowed[path][0]:
            failures.append(
                f"{path}: {lines} lines, under the {allowed[path][0]} recorded in "
                f"{EXCEPTIONS_FILE}:{allowed[path][1]}; the file shrank: lower "
                f"the entry to {lines}, so that it cannot grow back"
            )

    for path, (recorded, number) in sorted(allowed.items(), key=lambda i: i[1][1]):
        where = f"{EXCEPTIONS_FILE}:{number}"
        if path not in sizes:
            failures.append(
                f"{where}: {path} is not a tracked source file on disk (gone, "
                f"renamed or out of scope); remove this line"
            )
        elif sizes[path] <= CEILING:
            failures.append(
                f"{where}: {path} is now {sizes[path]} lines, under the ceiling "
                f"of {CEILING} (recorded {recorded}); remove this line"
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="the repository's root (default: this script's)",
    )
    root = parser.parse_args(argv).root.resolve()

    try:
        text = (root / EXCEPTIONS_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    files = tracked_files(root)
    failures = check(root, files, text)
    for failure in failures:
        print(f"check_file_sizes: {failure}", file=sys.stderr)
    if failures:
        print(f"check_file_sizes: {len(failures)} problem(s)", file=sys.stderr)
        return 1
    checked = sum(1 for path in files if is_source(path))
    print(
        f"check_file_sizes: {checked} source files, none over {CEILING} lines "
        f"without an exception ({len(read_exceptions(text)[0])} listed)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
