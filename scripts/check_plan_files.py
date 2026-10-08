#!/usr/bin/env python3
"""Fail when the plan's step folders and backlog files contradict it (S097, S100, S101).

Each step's section is a file, ``docs/plan/steps/S0NN.md``, kept in a folder of 20
steps (``steps/S020-S039/S021.md``), and the follow-up backlog is two files,
``docs/plan/backlog.md`` (open rows) and ``docs/plan/backlog-closed.md``. This is
the part of ``make docs`` that keeps them and ``docs/meridian-plan.md`` one story:

- ``steps/`` holds only folders named for a range of 20 (``S000-S019``); a step
  file sits in the folder of its own number, is named ``S0NN.md``, starts with its
  own step's heading and has a row in Part B (a row holds the step's number and no
  status: a Status column in a step table is a finding);
- a step's status is one line of its own file, on line 2 or 3 and nowhere else:
  ``**Status:** <word> · **Started:** <date|—> · **Finished:** <date|—>`` and an
  optional `` · <note>``; the word is one of ``todo``, ``doing``, ``done``,
  ``blocked`` and ``dropped``; ``done`` has a Finished date and the others a dash;
- Part F, the plan's last part, holds two marker lines; the lines between them are
  generated from the step files ("Finished steps", "In flight") and a block that
  differs from what they say is a finding. ``--write`` (``make plan-progress``)
  rewrites those lines and nothing else in the plan;
- the backlog table is not in the plan; each backlog file has the table's header
  once, rows of at least four cells (status the cell before the last, home the
  last), no closed row in ``backlog.md`` and no open row in
  ``backlog-closed.md``, and every open row names a step as its home;
- the plan has its Part B to F headings, in that order (a renamed one would switch
  a check off), holds no step section (a heading ``S0NN`` with a dash, at level two
  to four) and, in Part E, no entry of any label; ``docs/plan/changelog`` does not
  exist, because the change log ended with S100;
- the plan, a step file and a backlog file hold no conflict marker;
- an odd file (not UTF-8, a directory, a broken link) is a finding, not a
  traceback.

The line-width and link rules need nothing here: they read every Markdown file
under ``docs/``. A missing plan is skipped, as with the other checks, and so is a
missing backlog file (the link check reports a dead link to it).

Python 3 standard library only. Run from anywhere:
python3 scripts/check_plan_files.py [--write]
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
BACKLOG_COLUMNS = ["Item", "Raised in", "Status", "Home"]

FENCE_OPEN = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
FENCE_CLOSE = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})\s*$")
PART_HEADING = re.compile(r"^## Part ([A-Z]) — ")
# A step is S and three digits, as in Part B. The day a step S1000 exists, this
# check says so for the file: widen SECTION_HEADING, STEP_FILE and STEP_FOLDER (and
# folder_of) then.
SECTION_HEADING = re.compile(r"^### (S\d{3}) — (\S.*)$")
STEP_FILE = re.compile(r"^S\d{3}\.md$")
STEP_FOLDER = re.compile(r"^S(\d{3})-S(\d{3})$")
# A step's status: one line, line 2 or 3 of its file (S101).
STATUSES = ("todo", "doing", "done", "blocked", "dropped")
NO_DATE = "—"
STATUS_FORM = "**Status:** <word> · **Started:** <date|—> · **Finished:** <date|—>"
_DATE = r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|—)"
STATUS_LINE = re.compile(
    r"^\*\*Status:\*\* (?P<word>\w+) · \*\*Started:\*\* (?P<started>" + _DATE + r")"
    r" · \*\*Finished:\*\* (?P<finished>" + _DATE + r")(?: · \S.*)?$"
)
# The two lines Part F is generated between.
BEGIN_MARKER = (
    '<!-- plan-progress: begin (written by "make plan-progress", never by hand) -->'
)
END_MARKER = "<!-- plan-progress: end -->"
USAGE = "usage: check_plan_files.py [--write]"
# Any list item that opens like an entry, whatever its label: v0.NN, #N,
# PLAN-VERSION, #XXXX.
ANY_ENTRY = re.compile(r"^- \*\*[^,*\n]+, \d{4}-\d{2}-\d{2}:\*\*")
# A step heading left in the old place: level two to four, a dash of any kind.
OLD_SECTION = re.compile(r"^#{2,4} S\d{3}\s+[—\N{EN DASH}-]\s")
CONFLICT = re.compile(r"^(?:<{7}|>{7})(?: |$)")
STEP_ID = re.compile(r"S\d{3}")
STRUCK = re.compile(r"^~~.*?~~\s*")
SEPARATOR = re.compile(r"^\|\s*:?-{3,}")
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
ITEM_WIDTH = 40
CHANGELOG_ENDED = "the change log ended with S100 (2026-10-08)"


class PlanError(Exception):
    """The plan is not shaped as this check reads it."""


class Findings(list):
    """The findings in the order found. ``blocking`` holds those that make the
    generated block unknowable (an unreadable or malformed step file, a missing
    marker): ``--write`` refuses on them and on no other."""

    def __init__(self) -> None:
        super().__init__()
        self.blocking: list[str] = []

    def block(self, detail: str) -> None:
        self.append(detail)
        self.blocking.append(detail)


@dataclass(frozen=True)
class Step:
    """What a step file says of itself, for the plan's Part F."""

    step: str
    word: str
    started: str
    finished: str
    title: str


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


def read_file(
    root: Path, path: Path, found: Findings, blocking: bool = False
) -> str | None:
    """A regular UTF-8 file's text, or None with a finding saying why not."""
    name = relative(root, path)
    add = found.block if blocking else found.append
    if not path.is_file():
        add(f"{name}: not a regular file (a directory or a broken link)")
        return None
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        add(f"{name}: cannot be read as UTF-8 text ({type(error).__name__})")
        return None


def listing(folder: Path, found: Findings, root: Path) -> list[Path]:
    if not exists(folder):
        return []
    try:
        return sorted(folder.iterdir())
    except OSError as error:
        # Blocking: step files nobody could list must not read as "no step".
        found.block(f"{relative(root, folder)}: cannot be listed ({error.strerror})")
        return []


def regions(text: str, found: Findings) -> dict[str, str]:
    """Part B, Part C, Part E and Part F of the plan, each from its heading.

    A missing heading is a finding: the checks that read the part would
    otherwise pass on nothing. Part E ends where Part F starts, and Part F is
    the last part.
    """
    try:
        parts = heading_offsets(text)
    except PlanError as error:
        found.block(f"the plan: {error}")
        return {}
    missing = [k for k in "BCDEF" if k not in parts]
    for letter in missing:
        found.block(f"the plan has no '## Part {letter} — ' heading")
    if missing:
        return {}
    if not parts["B"] < parts["C"] < parts["D"] < parts["E"] < parts["F"]:
        found.block("the plan's Parts B, C, D, E and F are not in that order")
        return {}
    ends = {"B": parts["C"], "C": parts["D"], "E": parts["F"], "F": len(text)}
    return {k: text[parts[k] : ends[k]] for k in "BCEF"}


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


def step_rows(part_b: str) -> set[str]:
    """The steps with a row in Part B. A row holds no status (S101)."""
    rows: set[str] = set()
    for line in unfenced(part_b):
        if match := re.match(r"^\| (S\d{3}) \|", line):
            rows.add(match[1])
    return rows


def step_path(step: str) -> str:
    """Where a step's file is, as the plan links it (relative to ``docs/``)."""
    return f"plan/steps/{folder_of(step)}/{step}.md"


def read_status(name: str, text: str) -> tuple[str, str, str] | str:
    """The (word, started, finished) of the status line on line 2 or 3, or the
    finding that says what is wrong with it."""
    hits = [m for m in (STATUS_LINE.match(x) for x in text.split("\n")[1:3]) if m]
    form = f" The form is '{STATUS_FORM}' and an optional ' · <note>'."
    if not hits:
        return f"{name}: line 2 or 3 must be the status line.{form}"
    if len(hits) > 1:
        return f"{name}: the status line is on both line 2 and line 3.{form}"
    word, started, finished = hits[0]["word"], hits[0]["started"], hits[0]["finished"]
    if word not in STATUSES:
        return f"{name}: the status '{word}' is none of {', '.join(STATUSES)}.{form}"
    if word == "done" and finished == NO_DATE:
        return f"{name}: a 'done' step needs a Finished date, not '{NO_DATE}'.{form}"
    if word != "done" and finished != NO_DATE:
        return (
            f"{name}: a '{word}' step has Finished '{NO_DATE}'; only 'done' has a "
            f"date.{form}"
        )
    return word, started, finished


def is_range_folder(path: Path) -> bool:
    """A directory named for a range of 20: S + 3 digits, a multiple of 20, -S, +19."""
    match = STEP_FOLDER.match(path.name)
    if not match or not path.is_dir():
        return False
    low, high = int(match[1]), int(match[2])
    return low % 20 == 0 and high == low + 19


def check_step_file(
    root: Path, path: Path, rows: set[str], found: Findings
) -> Step | None:
    """Check one step file; its facts for Part F, or None when it has a finding
    that leaves them unknown (an unreadable file, a heading or status line wrong)."""
    name = relative(root, path)
    step = path.stem
    text = read_file(root, path, found, blocking=True)
    if text is None:
        return None
    found.extend(conflict_markers(name, text))
    heading = SECTION_HEADING.match(text.split("\n", 1)[0])
    if not heading or heading[1] != step:
        found.block(f"{name}: the first line must be the heading '### {step} — …'")
        heading = None
    if step not in rows:
        found.append(f"{name}: {step} has no row in Part B")
    status = read_status(name, text)
    if isinstance(status, str):
        found.block(status)
        return None
    if heading is None:
        return None
    return Step(step, *status, heading[2].rstrip())


def check_steps(root: Path, plan: dict[str, str], found: Findings) -> list[Step]:
    """Check the step folders; the steps in range folders whose files are right."""
    rows = step_rows(plan.get("B", ""))
    steps: list[Step] = []
    for entry in listing(root / STEPS, found, root):
        name = relative(root, entry)
        if is_range_folder(entry):
            for path in listing(entry, found, root):
                if not STEP_FILE.match(path.name):
                    found.append(
                        f"{relative(root, path)}: only S0NN.md files belong in {name}/"
                    )
                    continue
                if entry.name != folder_of(path.stem):
                    found.append(
                        f"{relative(root, path)}: {path.stem} belongs in "
                        f"{STEPS}/{folder_of(path.stem)}/"
                    )
                if step := check_step_file(root, path, rows, found):
                    steps.append(step)
        elif STEP_FILE.match(entry.name):
            found.append(
                f"{name}: a step file sits in its folder of 20; move it to "
                f"{STEPS}/{folder_of(entry.stem)}/{entry.name}"
            )
            check_step_file(root, entry, rows, found)
        else:
            found.append(
                f"{name}: only folders named for a range of 20 steps "
                f"(S000-S019, S020-S039, …) belong in {STEPS}/"
            )
    return steps


def check_old_places(text: str, plan: dict[str, str], found: Findings) -> None:
    lines = unfenced(text)
    for line in unfenced(plan.get("B", "")):
        cells = cells_of(line) if line.lstrip().startswith("|") else []
        if cells[:1] == ["ID"] and "Status" in cells:
            found.append(
                f"{PLAN}: a step table in Part B has a Status column; the step "
                f"tables hold no status (S101): a step's status is the line in "
                f"its own file"
            )
    for line in lines:
        if OLD_SECTION.match(line):
            found.append(
                f"the plan holds a step section ('{line[:40]}'): it belongs in "
                f"{STEPS}/, one file a step"
            )
    if any(is_header(line) for line in lines):
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
    # A pipe inside a code span is a cell boundary here: no status or home holds one.
    body = line.strip()[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [cell.strip() for cell in UNESCAPED_PIPE.split(body)]


def is_header(line: str) -> bool:
    """The backlog table's header: a row whose first four cells are the columns."""
    return line.lstrip().startswith("|") and cells_of(line)[:4] == BACKLOG_COLUMNS


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
    if scan(text + "\n")[-1].fenced:
        found.append(
            f"{name}: a code fence is opened and never closed; "
            f"the rows after it are not read"
        )
    lines = unfenced(text)
    headers = [i for i, line in enumerate(lines) if is_header(line)]
    if len(headers) != 1:
        found.append(
            f"{name}: the table's header '{BACKLOG_HEADER}' must be there exactly "
            f"once, not {len(headers)} times"
        )
        if not headers:
            return
    for line in lines[headers[0] + 1 :]:
        if not line.startswith("|") or SEPARATOR.match(line) or is_header(line):
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


def progress_span(
    text: str, plan: dict[str, str], found: Findings
) -> tuple[int, int] | None:
    """The line numbers (from 0) of Part F's two marker lines, or None with a
    finding. Only an unfenced line equal to a marker is a marker."""
    lines = scan(text)
    part_f = len(text) - len(plan["F"])
    inside = [i for i, x in enumerate(lines) if x.start >= part_f and not x.fenced]
    begin = [i for i in inside if lines[i].text == BEGIN_MARKER]
    end = [i for i in inside if lines[i].text == END_MARKER]
    for name, marker, hits in (
        ("begin", BEGIN_MARKER, begin),
        ("end", END_MARKER, end),
    ):
        if len(hits) != 1:
            found.block(
                f"the plan's Part F: the {name} marker line '{marker}' must be there "
                f"exactly once outside a code fence, not {len(hits)} times"
            )
    if len(begin) != 1 or len(end) != 1:
        return None
    if begin[0] > end[0]:
        found.block(
            "the plan's Part F: the begin marker must come before the end marker"
        )
        return None
    return begin[0], end[0]


def _sort_key(date: str, step: str) -> tuple[bool, str, int]:
    """Oldest date first, a dash after every date, then by step number."""
    return date == NO_DATE, date, int(step[1:])


def _row(step: Step, *cells: str) -> str:
    link = f"[{step.step}.md]({step_path(step.step)})"
    title = UNESCAPED_PIPE.sub(r"\\|", step.title)
    return "| " + " | ".join([cells[0], step.step, title, *cells[1:], link]) + " |"


def build_block(steps: list[Step]) -> list[str]:
    """The lines between Part F's markers, from the step files alone (S101)."""
    done = [s for s in steps if s.word == "done"]
    flight = [s for s in steps if s.word != "done"]
    done.sort(key=lambda s: _sort_key(s.finished, s.step))
    flight.sort(key=lambda s: _sort_key(s.started, s.step))
    return [
        "",
        "### Finished steps",
        "",
        "| Finished | Step | Title | File |",
        "|---|---|---|---|",
        *[_row(s, s.finished) for s in done],
        "",
        "### In flight",
        "",
        "| Started | Step | Title | Status | File |",
        "|---|---|---|---|---|",
        *[_row(s, s.started, s.word) for s in flight],
        "",
    ]


@dataclass
class Examined:
    found: Findings
    text: str | None
    span: tuple[int, int] | None
    steps: list[Step]


STALE = (
    'the plan\'s Part F: "Finished steps" and "In flight" are not what the step '
    "files say; run make plan-progress"
)


def examine(root: Path) -> Examined:
    """Everything the check reads, with the findings in the order found."""
    found = Findings()
    text = read_file(root, root / PLAN, found, blocking=True)
    plan: dict[str, str] = {}
    span = None
    if text is not None:
        found.extend(conflict_markers(PLAN, text))
        plan = regions(text, found)
        if plan:
            check_old_places(text, plan, found)
            span = progress_span(text, plan, found)
    blocked = len(found.blocking)
    steps = check_steps(root, plan, found)
    check_backlog(root, BACKLOG, False, found)
    check_backlog(root, BACKLOG_CLOSED, True, found)
    if exists(root / CHANGELOG):
        found.append(
            f"{CHANGELOG}: {CHANGELOG_ENDED}; delete the folder and put the "
            f"entry's text into the pull request's description, which the squash "
            f"commit carries"
        )
    if text is not None and span and len(found.blocking) == blocked:
        between = text.split("\n")[span[0] + 1 : span[1]]
        if between != build_block(steps):
            found.append(STALE)
    return Examined(found, text, span, steps)


def problems(root: Path) -> list[str]:
    """What is wrong in the plan and its folders."""
    if not exists(root / PLAN):
        return []
    return list(examine(root).found)


def report(found: list[str]) -> None:
    print("plan files: the plan and its files contradict each other\n", file=sys.stderr)
    for detail in found:
        print(f"  [plan-files] {detail}", file=sys.stderr)


def write_progress(root: Path) -> int:
    """Regenerate the lines between Part F's markers; touch nothing else."""
    if not exists(root / PLAN):
        report([f"{PLAN}: the plan does not exist"])
        return 1
    result = examine(root)
    if result.text is None or result.span is None or result.found.blocking:
        report(result.found.blocking)
        return 1
    lines = result.text.split("\n")
    begin, end = result.span
    text = "\n".join([*lines[: begin + 1], *build_block(result.steps), *lines[end:]])
    if text != result.text:
        try:
            (root / PLAN).write_bytes(text.encode("utf-8"))
        except OSError as error:
            report([f"{PLAN}: cannot be written ({error.strerror})"])
            return 1
    finished = sum(1 for s in result.steps if s.word == "done")
    flying = len(result.steps) - finished
    print(f"plan progress: {finished} finished, {flying} in flight")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv or []
    if args == ["--write"]:
        return write_progress(REPO)
    if args:
        print(USAGE, file=sys.stderr)
        return 2
    found = problems(REPO)
    if not found:
        print("plan files: step folders, statuses and backlog agree with the plan")
        return 0
    report(found)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
