#!/usr/bin/env python3
"""Fail when the plan's step files and change-log files contradict the plan (S097).

Since S097 each step's section is a file, ``docs/plan/steps/S0NN.md``, and each
change-log entry is a file, ``docs/plan/changelog/``. This is the part of
``make docs`` that keeps those folders and ``docs/meridian-plan.md`` one story:

- a file in ``steps/`` is named ``S0NN.md``, starts with its own step's heading,
  has a row in Part B and a row in Part C's index; every index row has its file,
  once; a step in Part B that is ``done`` or ``doing`` has a file;
- a file in ``changelog/`` is named for its label (``v0.NN.md`` for ``v0.N``,
  ``pr-NNNN.md`` for ``#N``), holds one entry, and shares its label with no other
  file; ``pr-XXXX-<step>.md`` with the label ``#XXXX`` stands in until the pull
  request has a number: it passes on a machine and fails under CI
  (``GITHUB_ACTIONS=true``), so the rename cannot be forgotten;
- the plan has its Part B, C, D and E headings (a renamed one would switch a
  check off), holds no step section (a heading ``S0NN`` with a dash, at level two
  to four) and, in Part E, no entry of any label, so that nobody appends to the
  old place;
- the plan, a step file and a change-log file hold no conflict marker, which
  ``plan_port.py`` leaves for a person to resolve;
- an odd file (not UTF-8, a directory, a broken link) is a finding, not a
  traceback.

The line-width and link rules need nothing here: they read every Markdown file
under ``docs/``, the new folders included. A missing plan is skipped, as with the
other checks.

Run from anywhere: python3 scripts/check_plan_files.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# The parsers of the move, beside this script; they are not an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_split as split

REPO = Path(__file__).resolve().parents[1]

# A step is S and three digits, as in Part B. The day a step S1000 exists, or a
# pull request 10000, this check says so for the file: widen STEP_FILE and the
# `\d{4}` of CHANGELOG_FILE (and the padding rule in plan_split.py) then.
STEP_FILE = re.compile(r"^S\d{3}\.md$")
INDEX_ROW = re.compile(
    r"^\| (S\d{3}) \| .* \| \[(S\d{3})\.md\]\((plan/steps/S\d{3}\.md)\) \|$"
)
CHANGELOG_FILE = re.compile(
    r"^(?:v0\.(?P<v>\d{2,})|pr-(?P<pr>\d{4})|pr-XXXX-(?P<step>[a-z0-9][a-z0-9-]*))\.md$"
)
# Any list item that opens like an entry, whatever its label: v0.NN, #N,
# PLAN-VERSION, #XXXX.
ANY_ENTRY = re.compile(r"^- \*\*[^,*\n]+, \d{4}-\d{2}-\d{2}:\*\*")
# A step heading left in the old place: level two to four, a dash of any kind.
OLD_SECTION = re.compile(r"^#{2,4} S\d{3}\s+[—–-]\s")
CONFLICT = re.compile(r"^(?:<{7}|>{7})(?: |$)")
OPEN_STATUS = ("done", "doing")


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


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
    if not folder.exists() and not folder.is_symlink():
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
        parts = split.heading_offsets(text)
    except split.SplitError as error:
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
    return [line.text for line in split.scan(text) if not line.fenced]


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


def check_steps(root: Path, plan: dict[str, str], found: list[str]) -> None:
    folder = root / split.STEPS
    rows = step_rows(plan.get("B", ""))
    index: dict[str, str] = {}
    for line in unfenced(plan.get("C", "")):
        row = INDEX_ROW.match(line)
        if not row:
            continue
        if row[1] != row[2] or row[3] != f"plan/steps/{row[2]}.md":
            found.append(f"the plan's index row of {row[1]} names {row[3]}")
        if row[1] in index:
            found.append(f"the plan's index lists {row[1]} twice")
        index[row[1]] = row[3]
    on_disk = set()
    for path in listing(folder, found, root):
        name = relative(root, path)
        if not STEP_FILE.match(path.name):
            found.append(f"{name}: only S0NN.md files belong in {split.STEPS}/")
            continue
        step = path.stem
        on_disk.add(step)
        text = read_file(root, path, found)
        if text is None:
            continue
        found.extend(conflict_markers(name, text))
        heading = split.SECTION_HEADING.match(text.split("\n", 1)[0])
        if not heading or heading[1] != step:
            found.append(f"{name}: the first line must be the heading '### {step} — …'")
        if step not in rows:
            found.append(f"{name}: {step} has no row in Part B")
        if step not in index:
            found.append(f"{name}: {step} is not in Part C's index of the plan")
    for step in sorted(set(index) - on_disk):
        found.append(
            f"the plan's index lists {step}, which has no file in {split.STEPS}/"
        )
    for step, status in sorted(rows.items()):
        if status and status.startswith(OPEN_STATUS) and step not in on_disk:
            found.append(f"Part B has {step} as '{status}', and {step} has no file")


def check_old_places(text: str, plan: dict[str, str], found: list[str]) -> None:
    for line in unfenced(text):
        if OLD_SECTION.match(line):
            found.append(
                f"the plan holds a step section ('{line[:40]}'): it belongs in "
                f"{split.STEPS}/, one file a step"
            )
    for line in unfenced(plan.get("E", "")):
        if ANY_ENTRY.match(line):
            found.append(
                f"Part E of the plan holds an entry ('{line[:40]}'): it belongs in "
                f"{split.CHANGELOG}/, one file an entry"
            )


def label_of(first_line: str) -> str | None:
    match = split.ENTRY_HEAD.match(first_line)
    return match[1] if match else None


def check_changelog(root: Path, ci: bool, found: list[str]) -> list[str]:
    notes: list[str] = []
    seen: dict[str, str] = {}
    for path in listing(root / split.CHANGELOG, found, root):
        name = relative(root, path)
        parts = CHANGELOG_FILE.match(path.name)
        if not parts:
            found.append(
                f"{name}: a change-log file is v0.NN.md, pr-NNNN.md or "
                f"pr-XXXX-<step>.md"
            )
            continue
        text = read_file(root, path, found)
        if text is None:
            continue
        found.extend(conflict_markers(name, text))
        label = label_of(text.split("\n", 1)[0])
        if label is None:
            found.append(f"{name}: the first line must start '- **<label>, <date>:**'")
            continue
        if sum(1 for line in unfenced(text) if ANY_ENTRY.match(line)) != 1:
            found.append(f"{name}: a file holds one entry")
        if parts["v"]:
            minor = int(parts["v"])
            if label != f"v0.{minor}" or parts["v"] != f"{minor:02d}":
                found.append(f"{name}: named {parts['v']} but labelled {label}")
        elif parts["pr"]:
            if label != f"#{int(parts['pr'])}":
                found.append(f"{name}: named pr-{parts['pr']} but labelled {label}")
        else:
            if label != "#XXXX":
                found.append(f"{name}: a stand-in name needs the label #XXXX")
            todo = f"{name}: rename it for the pull request's number, label #NNNN"
            (found if ci else notes).append(todo)
            continue
        if label in seen:
            found.append(f"{name}: label {label} is also the label of {seen[label]}")
        seen[label] = name
    return notes


def problems(root: Path, ci: bool = False) -> tuple[list[str], list[str]]:
    """What is wrong, and what is only noted, in the plan and its folders."""
    plan_path = root / split.PLAN
    if not plan_path.exists() and not plan_path.is_symlink():
        return [], []
    found: list[str] = []
    text = read_file(root, plan_path, found)
    plan: dict[str, str] = {}
    if text is not None:
        found.extend(conflict_markers(split.PLAN, text))
        plan = regions(text, found)
        if plan:
            check_old_places(text, plan, found)
    check_steps(root, plan, found)
    notes = check_changelog(root, ci, found)
    return found, notes


def main() -> int:
    ci = os.environ.get("GITHUB_ACTIONS") == "true"
    found, notes = problems(REPO, ci)
    for note in notes:
        print(f"  [note] {note} (CI refuses this)")
    if not found:
        print("plan files: step files and change log agree with the plan")
        return 0
    print("plan files: the plan and its files contradict each other\n", file=sys.stderr)
    for detail in found:
        print(f"  [plan-files] {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
