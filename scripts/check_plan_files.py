#!/usr/bin/env python3
"""Fail when the plan's step folders and backlog files contradict the plan (S097, S100).

Each step's section is a file, ``docs/plan/steps/S0NN.md``, kept in a folder of 20
steps (``steps/S020-S039/S021.md``), and the follow-up backlog is two files,
``docs/plan/backlog.md`` (open rows) and ``docs/plan/backlog-closed.md``. This is
the part of ``make docs`` that keeps them and ``docs/meridian-plan.md`` one story:

- ``steps/`` holds only folders named for a range of 20 (``S000-S019``); a step
  file sits in the folder of its own number, is named ``S0NN.md``, starts with its
  own step's heading, has a row in Part B and a row in Part C's index; every index
  row links its file at that path, once; a step in Part B that is ``done`` or
  ``doing`` has a file;
- the backlog table is not in the plan; each backlog file has the table's header
  once, rows of at least four cells (status the cell before the last, home the
  last), no closed row in ``backlog.md`` and no open row in
  ``backlog-closed.md``, and every open row names a step as its home;
- the plan has its Part B, C, D and E headings (a renamed one would switch a
  check off), holds no step section (a heading ``S0NN`` with a dash, at level two
  to four) and, in Part E, no entry of any label; ``docs/plan/changelog`` does not
  exist, because the change log ended with S100;
- the plan, a step file and a backlog file hold no conflict marker;
- an odd file (not UTF-8, a directory, a broken link) is a finding, not a
  traceback.

The line-width and link rules need nothing here: they read every Markdown file
under ``docs/``. A missing plan is skipped, as with the other checks, and so is a
missing backlog file (the link check reports a dead link to it).

Python 3 standard library only. Run from anywhere: python3 scripts/check_plan_files.py
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLAN = "docs/meridian-plan.md"
STEPS = "docs/plan/steps"
CHANGELOG = "docs/plan/changelog"
BACKLOG = "docs/plan/backlog.md"
BACKLOG_CLOSED = "docs/plan/backlog-closed.md"
BACKLOG_HEADER = "| Item | Raised in | Status | Home |"

FENCE_OPEN = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
FENCE_CLOSE = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})\s*$")
PART_HEADING = re.compile(r"^## Part ([A-Z]) — ")
# A step is S and three digits, as in Part B. The day a step S1000 exists, this
# check says so for the file: widen SECTION_HEADING, STEP_FILE, INDEX_ROW and
# STEP_FOLDER (and folder_of) then.
SECTION_HEADING = re.compile(r"^### (S\d{3}) — (\S.*)$")
STEP_FILE = re.compile(r"^S\d{3}\.md$")
STEP_FOLDER = re.compile(r"^S(\d{3})-S(\d{3})$")
INDEX_ROW = re.compile(r"^\| (S\d{3}) \| .* \| \[([^\]]*)\]\(([^)]*)\) \|$")
# Any list item that opens like an entry, whatever its label: v0.NN, #N,
# PLAN-VERSION, #XXXX.
ANY_ENTRY = re.compile(r"^- \*\*[^,*\n]+, \d{4}-\d{2}-\d{2}:\*\*")
# A step heading left in the old place: level two to four, a dash of any kind.
OLD_SECTION = re.compile(r"^#{2,4} S\d{3}\s+[—\N{EN DASH}-]\s")
CONFLICT = re.compile(r"^(?:<{7}|>{7})(?: |$)")
OPEN_STATUS = ("done", "doing")
STEP_ID = re.compile(r"S\d{3}")
STRUCK = re.compile(r"^~~.*?~~\s*")
SEPARATOR = re.compile(r"^\|\s*:?-{3,}")
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
ITEM_WIDTH = 40
CHANGELOG_ENDED = "the change log ended with S100 (2026-10-08)"


class PlanError(Exception):
    """The plan is not shaped as this check reads it."""


@dataclass
class Line:
    start: int
    text: str
    fenced: bool


def scan(text: str) -> list[Line]:
    """Each line with its offset and whether a fence covers it (fence lines too)."""
    lines: list[Line] = []
    opened: str | None = None
    pos = 0
    for raw in text.split("\n"):
        covered = opened is not None
        if opened is None:
            if opening := FENCE_OPEN.match(raw):
                opened, covered = opening["marker"], True
        elif (close := FENCE_CLOSE.match(raw)) and (
            close["marker"][0] == opened[0] and len(close["marker"]) >= len(opened)
        ):
            opened = None
        lines.append(Line(pos, raw, covered))
        pos += len(raw) + 1
    return lines


def heading_offsets(text: str) -> dict[str, int]:
    """The offset of each ``## Part X`` heading outside a fence; one each."""
    found: dict[str, int] = {}
    for line in scan(text):
        match = PART_HEADING.match(line.text)
        if match and not line.fenced:
            if match[1] in found:
                raise PlanError(f"Part {match[1]} has two headings")
            found[match[1]] = line.start
    return found


def folder_of(step: str) -> str:
    """The folder of 20 a step file sits in: ``S021`` -> ``S020-S039``."""
    low = int(step[1:]) // 20 * 20
    return f"S{low:03d}-S{low + 19:03d}"


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def read_file(root: Path, path: Path, found: list[str]) -> str | None:
    """A regular UTF-8 file's text, or None with a finding saying why not."""
    name = relative(root, path)
    if not path.is_file():
        found.append(f"{name}: not a regular file (a directory or a broken link)")
        return None
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        found.append(f"{name}: cannot be read as UTF-8 text ({type(error).__name__})")
        return None


def listing(folder: Path, found: list[str], root: Path) -> list[Path]:
    if not exists(folder):
        return []
    try:
        return sorted(folder.iterdir())
    except OSError as error:
        found.append(f"{relative(root, folder)}: cannot be listed ({error.strerror})")
        return []


def regions(text: str, found: list[str]) -> dict[str, str]:
    """Part B, Part C and Part E of the plan, each from its heading.

    A missing heading is a finding: the checks that read the part would
    otherwise pass on nothing.
    """
    try:
        parts = heading_offsets(text)
    except PlanError as error:
        found.append(f"the plan: {error}")
        return {}
    missing = [k for k in "BCDE" if k not in parts]
    for letter in missing:
        found.append(f"the plan has no '## Part {letter} — ' heading")
    if missing:
        return {}
    if not parts["B"] < parts["C"] < parts["D"] < parts["E"]:
        found.append("the plan's Parts B, C, D and E are not in that order")
        return {}
    ends = {"B": parts["C"], "C": parts["D"], "E": len(text)}
    return {k: text[parts[k] : ends[k]] for k in "BCE"}


def unfenced(text: str) -> list[str]:
    return [line.text for line in scan(text) if not line.fenced]


def conflict_markers(name: str, text: str) -> list[str]:
    """One finding for a file with a conflict marker line (or a ======= between)."""
    hit, inside = [], False
    for number, line in enumerate(text.split("\n"), 1):
        if CONFLICT.match(line):
            hit.append(number)
            inside = line.startswith("<")
        elif inside and line == "=======":
            hit.append(number)
    if not hit:
        return []
    where = ", ".join(str(n) for n in hit[:5]) + (" …" if len(hit) > 5 else "")
    return [f"{name}: conflict marker at line {where}; resolve the merge"]


def step_rows(part_b: str) -> dict[str, str | None]:
    """Each step id with a row in Part B -> its status (None: a row with none)."""
    rows: dict[str, str | None] = {}
    for line in unfenced(part_b):
        match = re.match(r"^\| (S\d{3}) \|", line)
        if not match:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        status = cells[-2] if len(cells) >= 5 else None
        rows[match[1]] = status or rows.get(match[1])
    return rows


def step_path(step: str) -> str:
    """Where the plan's index links a step's file from the plan (under ``docs/``)."""
    return f"plan/steps/{folder_of(step)}/{step}.md"


def is_range_folder(path: Path) -> bool:
    """A directory named for a range of 20: S + 3 digits, a multiple of 20, -S, +19."""
    match = STEP_FOLDER.match(path.name)
    if not match or not path.is_dir():
        return False
    low, high = int(match[1]), int(match[2])
    return low % 20 == 0 and high == low + 19


def check_step_file(
    root: Path,
    path: Path,
    rows: dict[str, str | None],
    index: dict[str, str],
    found: list[str],
) -> None:
    name = relative(root, path)
    step = path.stem
    text = read_file(root, path, found)
    if text is None:
        return
    found.extend(conflict_markers(name, text))
    heading = SECTION_HEADING.match(text.split("\n", 1)[0])
    if not heading or heading[1] != step:
        found.append(f"{name}: the first line must be the heading '### {step} — …'")
    if step not in rows:
        found.append(f"{name}: {step} has no row in Part B")
    if step not in index:
        found.append(f"{name}: {step} is not in Part C's index of the plan")


def check_steps(root: Path, plan: dict[str, str], found: list[str]) -> None:
    rows = step_rows(plan.get("B", ""))
    index: dict[str, str] = {}
    for line in unfenced(plan.get("C", "")):
        row = INDEX_ROW.match(line)
        if not row:
            continue
        step, text, path = row[1], row[2], row[3]
        if text != f"{step}.md" or path != step_path(step):
            found.append(
                f"the plan's index row of {step} names {path}; "
                f"it must be [{step}.md]({step_path(step)})"
            )
        if step in index:
            found.append(f"the plan's index lists {step} twice")
        index[step] = path
    on_disk = set()
    for entry in listing(root / STEPS, found, root):
        name = relative(root, entry)
        if is_range_folder(entry):
            for path in listing(entry, found, root):
                if not STEP_FILE.match(path.name):
                    found.append(
                        f"{relative(root, path)}: only S0NN.md files belong in {name}/"
                    )
                    continue
                on_disk.add(path.stem)
                if entry.name != folder_of(path.stem):
                    found.append(
                        f"{relative(root, path)}: {path.stem} belongs in "
                        f"{STEPS}/{folder_of(path.stem)}/"
                    )
                check_step_file(root, path, rows, index, found)
        elif STEP_FILE.match(entry.name):
            found.append(
                f"{name}: a step file sits in its folder of 20; move it to "
                f"{STEPS}/{folder_of(entry.stem)}/{entry.name}"
            )
            on_disk.add(entry.stem)
            check_step_file(root, entry, rows, index, found)
        else:
            found.append(
                f"{name}: only folders named for a range of 20 steps "
                f"(S000-S019, S020-S039, …) belong in {STEPS}/"
            )
    for step in sorted(set(index) - on_disk):
        found.append(
            f"the plan's index lists {step}, which has no file in "
            f"{STEPS}/{folder_of(step)}/"
        )
    for step, status in sorted(rows.items()):
        if status and status.startswith(OPEN_STATUS) and step not in on_disk:
            found.append(f"Part B has {step} as '{status}', and {step} has no file")


def check_old_places(text: str, plan: dict[str, str], found: list[str]) -> None:
    lines = unfenced(text)
    for line in lines:
        if OLD_SECTION.match(line):
            found.append(
                f"the plan holds a step section ('{line[:40]}'): it belongs in "
                f"{STEPS}/, one file a step"
            )
    if any(line.strip() == BACKLOG_HEADER for line in lines):
        found.append(
            f"{PLAN}: the follow-up table left the plan (S100); a follow-up is a "
            f"row of {BACKLOG}"
        )
    for line in unfenced(plan.get("E", "")):
        if ANY_ENTRY.match(line):
            found.append(
                f"Part E of the plan holds an entry ('{line[:40]}'): "
                f"{CHANGELOG_ENDED}; put the entry's text into the pull "
                f"request's description, which the squash commit carries"
            )


def cells_of(line: str) -> list[str]:
    """A table row's cells, split on pipes that no backslash escapes."""
    body = line.strip()[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [cell.strip() for cell in UNESCAPED_PIPE.split(body)]


def is_closed(status: str) -> bool:
    """A closed row's status starts ``closed`` or ``done`` after a struck-through
    beginning; ``closed in part`` and ``closed for `` are rows still open."""
    text = STRUCK.sub("", status, count=1).lower()
    if text.startswith(("closed in part", "closed for ")):
        return False
    return text.startswith(("closed", "done"))


def check_backlog(root: Path, name: str, closed_file: bool, found: list[str]) -> None:
    path = root / name
    if not exists(path):
        return
    text = read_file(root, path, found)
    if text is None:
        return
    found.extend(conflict_markers(name, text))
    lines = unfenced(text)
    headers = [i for i, line in enumerate(lines) if line.strip() == BACKLOG_HEADER]
    if len(headers) != 1:
        found.append(
            f"{name}: the table's header '{BACKLOG_HEADER}' must be there exactly "
            f"once, not {len(headers)} times"
        )
        if not headers:
            return
    for line in lines[headers[0] + 1 :]:
        if not line.startswith("|") or SEPARATOR.match(line):
            continue
        if line.strip() == BACKLOG_HEADER:
            continue
        cells = cells_of(line)
        item = cells[0][:ITEM_WIDTH]
        if len(cells) < 4:
            found.append(f"{name}: a row of fewer than four cells ('{item}')")
            continue
        status, home = cells[-2], cells[-1]
        if closed_file and not is_closed(status):
            found.append(
                f"{name}: an open row ('{item}') belongs in {BACKLOG}; a closed "
                f"row's status starts 'closed' or 'done'"
            )
        elif not closed_file and is_closed(status):
            found.append(
                f"{name}: a closed row ('{item}'): move it, whole, to the end of "
                f"{BACKLOG_CLOSED}"
            )
        elif not closed_file and not STEP_ID.search(home):
            found.append(
                f"{name}: an open row ('{item}') names no step as its home; "
                f"every open row names the step that will take it"
            )


def problems(root: Path) -> list[str]:
    """What is wrong in the plan and its folders."""
    plan_path = root / PLAN
    if not exists(plan_path):
        return []
    found: list[str] = []
    text = read_file(root, plan_path, found)
    plan: dict[str, str] = {}
    if text is not None:
        found.extend(conflict_markers(PLAN, text))
        plan = regions(text, found)
        if plan:
            check_old_places(text, plan, found)
    check_steps(root, plan, found)
    check_backlog(root, BACKLOG, False, found)
    check_backlog(root, BACKLOG_CLOSED, True, found)
    if exists(root / CHANGELOG):
        found.append(
            f"{CHANGELOG}: {CHANGELOG_ENDED}; delete the folder and put the "
            f"entry's text into the pull request's description, which the squash "
            f"commit carries"
        )
    return found


def main() -> int:
    found = problems(REPO)
    if not found:
        print("plan files: step folders and backlog agree with the plan")
        return 0
    print("plan files: the plan and its files contradict each other\n", file=sys.stderr)
    for detail in found:
        print(f"  [plan-files] {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
