"""Prove that a split of a shell file moved lines and changed none (S074).

``split_proof.py`` proves a move of Python units by ``ast``. A shell file is
moved as RANGES OF LINES, and one check's text may come from three regions of
the old file (its paragraph in the header, its constants, its functions), so the
proof here is a manifest of ranges, byte for byte and order included.

Command, run from the repository's root::

    python3 scripts/shell_split_proof.py --ref REF --manifest FILE \\
        --scope PATH [--scope PATH ...]

The manifest is plain text. It lists every file the cut WROTE (changed or new),
each as the exact sequence of its lines. One line of the manifest is one of:

    == PATH        opens a file as it is on disk now; the lines that follow,
                   until the next ``==``, are that file's lines in order
    OLDPATH A-B    lines A to B (1-based, inclusive, A <= B) of OLDPATH as it
                   is at REF; OLDPATH has no space in it
    + TEXT         one ADDED line: the text after ``+ ``, exactly (trailing
                   spaces kept); ``+`` alone is an added empty line
    (blank line)   ignored, as is a line that starts with ``#``

A line of a file is the bytes between two newlines; ``\\r`` is part of the
line. A file is compared as bytes. The old side is read with
``git show REF:OLDPATH``.

Exit 0 and a short account only when ALL hold:

1. every file opened by ``==`` equals, byte for byte, the sequence the
   manifest gives for it (the file, the first differing line, both lines);
2. every line of every OLDPATH the manifest names is used EXACTLY once across
   the whole manifest (an unused line is a deletion, a line used twice is a
   copy: the ranges are named);
3. (a NOTE, never a failure) every place where one target's ranges of one
   OLDPATH do not rise is listed: the paragraph, constants and functions of a
   check normally come in rising order, so a fall is worth a second look;
4. every file under each ``--scope`` (a file or a directory), as on disk now
   or at REF, is opened by ``==``, or is an OLDPATH that is gone from disk (its
   lines are held to point 2), or is byte-identical to REF. A file that is new
   in scope, changed or removed, and not in the manifest, fails; so does an
   OLDPATH that is still on disk without an ``==``: its lines would then be in
   two places;
5. file modes are not looked at.

The final newline: a file's lines are the text between newlines and whether it
ends with a newline is compared on its own. A target ends with a newline unless
its last line is the last line of an old file that had none. An old last line
with no newline that lands inside a target gets one: nothing else is possible.

The account on success: per target the ranges, lines moved and lines added; the
old files with their line counts and "each used once"; EVERY added line with its
target and line number; the notes of point 3. One line per problem (``FAIL``)
and exit 1 otherwise. Exit 2 when the manifest cannot be read (the line is
named), when an old file or a range is not at REF, or when git fails.
"""

import argparse
import os
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass, field

RANGE = re.compile(r"(\S+) (\d+)-(\d+)")


class Unreadable(Exception):
    """The manifest, or what it points at, cannot be read: exit 2."""


@dataclass
class Item:
    number: int  # manifest line number
    old: str | None = None  # OLDPATH of a range
    start: int = 0
    end: int = 0
    text: bytes = b""  # the text of an added line


@dataclass
class Target:
    path: str
    number: int
    items: list[Item] = field(default_factory=list)


@dataclass
class OldFile:
    lines: list[bytes]
    eol: bool


def split_lines(data: bytes) -> tuple[list[bytes], bool]:
    """The lines of ``data`` and whether it ends with a newline (empty: yes)."""
    if not data:
        return [], True
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines.pop()
        return lines, True
    return lines, False


def parse_manifest(source: str) -> list[Target]:
    targets: list[Target] = []
    errors: list[str] = []
    for number, raw in enumerate(source.split("\n"), start=1):
        line = raw
        if not line.strip() or line.startswith("#"):
            continue
        if line.startswith("== "):
            path = posixpath.normpath(line[3:].strip())
            if any(t.path == path for t in targets):
                errors.append(f"manifest line {number}: {path} is opened twice")
            targets.append(Target(path, number))
        elif line == "+" or line.startswith("+ "):
            if not targets:
                errors.append(f"manifest line {number}: a line before any ==")
                continue
            text = line[2:].encode("utf-8")
            targets[-1].items.append(Item(number, text=text))
        else:
            found = RANGE.fullmatch(line.rstrip())
            if found is None:
                errors.append(f"manifest line {number}: cannot read {line!r}")
            elif not targets:
                errors.append(f"manifest line {number}: a line before any ==")
            else:
                start, end = int(found[2]), int(found[3])
                if start < 1 or end < start:
                    errors.append(f"manifest line {number}: bad range {line!r}")
                    continue
                old = posixpath.normpath(found[1])
                targets[-1].items.append(Item(number, old, start, end))
    if errors:
        raise Unreadable("\n".join(errors))
    return targets


def git_bytes(ref: str, path: str) -> bytes | None:
    done = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, check=False
    )
    return done.stdout if done.returncode == 0 else None


def load_old(ref: str, targets: list[Target]) -> dict[str, OldFile]:
    old: dict[str, OldFile] = {}
    for target in targets:
        for item in target.items:
            if item.old is None:
                continue
            if item.old not in old:
                data = git_bytes(ref, item.old)
                if data is None:
                    raise Unreadable(
                        f"manifest line {item.number}: {item.old} is not in {ref}"
                    )
                lines, eol = split_lines(data)
                old[item.old] = OldFile(lines, eol)
            if item.end > len(old[item.old].lines):
                raise Unreadable(
                    f"manifest line {item.number}: {item.old} has "
                    f"{len(old[item.old].lines)} lines at {ref}, not {item.end}"
                )
    return old


def ranges_of(numbers: list[int]) -> str:
    """``[3, 4, 5, 9]`` as ``3-5, 9``."""
    parts: list[str] = []
    first = last = numbers[0]
    for number in [*numbers[1:], None]:
        if number is not None and number == last + 1:
            last = number
            continue
        parts.append(str(first) if first == last else f"{first}-{last}")
        if number is not None:
            first = last = number
    return ", ".join(parts)


def expected_of(target: Target, old: dict[str, OldFile]) -> tuple[list[bytes], bool]:
    lines: list[bytes] = []
    eol = True
    for item in target.items:
        if item.old is None:
            lines.append(item.text)
            eol = True
        else:
            lines.extend(old[item.old].lines[item.start - 1 : item.end])
            eol = old[item.old].eol or item.end < len(old[item.old].lines)
    return lines, eol


def shown(line: bytes | None) -> str:
    return "<end of file>" if line is None else repr(line.decode("utf-8", "replace"))


def check_target(target: Target, old: dict[str, OldFile]) -> list[str]:
    try:
        with open(target.path, "rb") as handle:
            data = handle.read()
    except OSError as error:
        return [f"{target.path}: cannot be read on disk ({error.strerror})"]
    got, got_eol = split_lines(data)
    want, want_eol = expected_of(target, old)
    for index in range(max(len(got), len(want))):
        have = got[index] if index < len(got) else None
        need = want[index] if index < len(want) else None
        if have != need:
            return [
                f"{target.path}: line {index + 1} differs: disk {shown(have)}, "
                f"manifest {shown(need)}"
            ]
    if got_eol != want_eol:
        return [
            f"{target.path}: the file "
            f"{'ends' if got_eol else 'does not end'} with a newline, the "
            f"manifest wants it to {'end' if want_eol else 'not end'} with one"
        ]
    return []


def check_usage(targets: list[Target], old: dict[str, OldFile]) -> list[str]:
    problems: list[str] = []
    for path, held in old.items():
        count = [0] * len(held.lines)
        for target in targets:
            for item in target.items:
                if item.old == path:
                    for number in range(item.start, item.end + 1):
                        count[number - 1] += 1
        unused = [n for n, c in enumerate(count, start=1) if c == 0]
        twice = [n for n, c in enumerate(count, start=1) if c > 1]
        if unused:
            problems.append(f"{path}: lines {ranges_of(unused)} are in no file")
        if twice:
            users = [
                f"{t.path} {i.start}-{i.end}"
                for t in targets
                for i in t.items
                if i.old == path and any(i.start <= n <= i.end for n in twice)
            ]
            problems.append(
                f"{path}: lines {ranges_of(twice)} are used twice "
                f"(ranges: {'; '.join(users)})"
            )
    return problems


def scope_files(ref: str, scopes: list[str]) -> tuple[set[str], set[str]]:
    """Files under the scopes on disk now, and at ``ref``."""
    disk: set[str] = set()
    at_ref: set[str] = set()
    for scope in scopes:
        scope = os.path.normpath(scope)
        if os.path.isfile(scope):
            disk.add(scope.replace(os.sep, "/"))
        for folder, _, names in os.walk(scope):
            for name in names:
                disk.add(os.path.join(folder, name).replace(os.sep, "/"))
        done = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "-z", ref, "--", scope],
            capture_output=True,
            check=False,
        )
        if done.returncode != 0:
            raise Unreadable(f"git ls-tree {ref} -- {scope} failed")
        at_ref.update(name.decode("utf-8") for name in done.stdout.split(b"\0") if name)
    return disk, at_ref


def check_scope(
    ref: str, scopes: list[str], targets: list[Target], old: dict[str, OldFile]
) -> list[str]:
    problems: list[str] = []
    disk, at_ref = scope_files(ref, scopes)
    if not disk and not at_ref:
        problems.append(f"scope {', '.join(scopes)} holds no file on disk or at {ref}")
    opened = {t.path for t in targets}
    for path in sorted(disk | at_ref):
        if path in opened:
            continue
        if path in old:
            if path in disk:
                problems.append(
                    f"{path}: named as an old file, still on disk, and not "
                    "opened with == (its lines would be in two places)"
                )
            continue
        before = git_bytes(ref, path) if path in at_ref else None
        if path in disk:
            with open(path, "rb") as handle:
                now = handle.read()
            if before is None:
                problems.append(f"{path}: new in scope and not in the manifest")
            elif now != before:
                problems.append(f"{path}: changed and not in the manifest")
        else:
            problems.append(f"{path}: removed and not named by the manifest")
    return problems


def falls(targets: list[Target]) -> list[str]:
    notes: list[str] = []
    for target in targets:
        last: dict[str, Item] = {}
        for item in target.items:
            if item.old is None:
                continue
            before = last.get(item.old)
            if before is not None and item.start < before.end:
                notes.append(
                    f"{target.path}: {item.old} {item.start}-{item.end} (manifest "
                    f"line {item.number}) does not rise after "
                    f"{before.start}-{before.end} (line {before.number})"
                )
            last[item.old] = item
    return notes


def account(targets: list[Target], old: dict[str, OldFile], notes: list[str]) -> None:
    print("shell_split_proof: every line used once, every file byte for byte")
    added: list[str] = []
    for target in targets:
        moved = sum(i.end - i.start + 1 for i in target.items if i.old is not None)
        new = sum(1 for i in target.items if i.old is None)
        ranges = len(target.items) - new
        print(f"  {target.path}: {ranges} ranges, {moved} lines moved, {new} added")
        for position, item in enumerate(target.items):
            if item.old is not None:
                continue
            number = sum(
                i.end - i.start + 1 if i.old is not None else 1
                for i in target.items[:position]
            )
            text = item.text.decode("utf-8")
            added.append(f"  {target.path}:{number + 1}: {text}")
    for path, held in old.items():
        print(f"  old {path}: {len(held.lines)} lines, each used exactly once")
    print(f"added lines ({len(added)}):")
    for line in added:
        print(line)
    print(f"notes ({len(notes)}):")
    for note in notes:
        print(f"  NOTE {note}")
    print("PROOF HOLDS")


def prove(ref: str, manifest: str, scopes: list[str]) -> int:
    targets = parse_manifest(manifest)
    old = load_old(ref, targets)
    problems: list[str] = []
    for target in targets:
        problems += check_target(target, old)
    problems += check_usage(targets, old)
    problems += check_scope(ref, scopes, targets, old)
    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        return 1
    account(targets, old, falls(targets))
    return 0


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Prove a split of a shell file.")
    parser.add_argument("--ref", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--scope", action="append", required=True)
    args = parser.parse_args(arguments)
    try:
        with open(args.manifest, encoding="utf-8") as handle:
            manifest = handle.read()
        return prove(args.ref, manifest, args.scope)
    except (Unreadable, OSError, UnicodeDecodeError) as error:
        print(f"UNREADABLE {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
