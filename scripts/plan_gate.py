"""What keeps the plan's history out of the plan (S102), for check_plan_files.py.

The plan says what is to be built and reads the same the day after a step finished
as the day before (Part A, "What the plan holds, and what it does not"). This module
holds the rules that keep it so, as pure functions that take lines of text and give
back findings as strings; ``check_plan_files.py`` reads the files and calls them:

- a step row (Part B, a table headed ``ID | Step | Done when | Depends``) is one plan
  sentence: four cells, at most ``ROW_MAX`` characters, no date, no struck-through
  text, no quote of the owner and no word on what is built so far;
  ``ROW_EXCEPTIONS`` lists the steps that may keep a row that fails, and can only
  shrink;
- no status is typed by hand: no ``**Status:**`` line and no "Where the project
  stands" heading in the plan;
- Part D holds open questions only, none over ``QUESTION_ROW_MAX``; an answered one
  is a row of ``docs/plan/questions-closed.md`` and a number is in one row of the
  two tables;
- the plan's fixed text (its size less the step rows, the question rows and the
  generated block) stays under ``FIXED_TEXT_MAX`` and within ``FIXED_TEXT_SLACK``
  of it, so the ceiling follows the plan down. Raising it is the owner's decision.

Python 3 standard library only.
"""

from __future__ import annotations

import re

ROW_MAX = 500
QUESTION_ROW_MAX = 900
FIXED_TEXT_MAX = 47_600
FIXED_TEXT_SLACK = 2048
# Step -> one line why. A step whose row passes the gate is a finding, so it shrinks.
ROW_EXCEPTIONS: dict[str, str] = {
    "S021": "another session's step in flight (Y4) edits this row and its file; "
    "S102 left it",
}

STEP_COLUMNS = ["ID", "Step", "Done when", "Depends"]
QUESTION_COLUMNS = ["#", "Question", "Needed by", "Default if unanswered"]
CLOSED_FILE = "docs/plan/questions-closed.md"
OWNER_NOTE = "raising the ceiling is the owner's decision (Part A)"
ROW_REMEDY = "a row is one plan sentence: put the rest into the step's file"
STATUS_REMEDY = (
    "a step's status is the line in its own file, and what exists today is the "
    "root README's to say"
)

UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
SEPARATOR = re.compile(r"^\|\s*:?-{3,}")
STEP_CELL = re.compile(r"^S[0-9]{3}$")
# A first cell that names a step or a number and is not the bare form: bold, a
# link, lower case, a trailing colon. Its row would otherwise leave the rules.
LOOSE_STEP = re.compile(r"(?<![A-Za-z0-9])[Ss][0-9]{3}(?![0-9])")
LOOSE_NUMBER = re.compile(r"^[^A-Za-z0-9]*[0-9]+[^A-Za-z0-9]*$")
DATE = re.compile(r"20\d\d-\d\d-\d\d")
OWNER = re.compile(r"owner", re.IGNORECASE)
QUOTE = re.compile(r'"[^"]+"')
BUILD_LABEL = re.compile(
    r"\bBuilt as\b|\b[Nn]ot built\b|\b[Nn]ot met\b|\b[Nn]ot done\b|\bDesigned\b"
    r"|\bImplemented\b|\bimplemented and tested\b"
    r"|`(?:todo|doing|done|blocked|dropped)`"
)
# The mark of an answered question, as every closed row carries it in its
# Question cell: ``**Answered 2026-10-07: …**``. The word alone is ordinary text.
ANSWERED = re.compile(r"\*\*Answered\b")
STRUCK_TEXT = re.compile(r"~~|<del>|<s>")
STATUS_HEADING = re.compile(r"^#{1,6}\s+where the project stands\s*#*\s*$", re.I)


def cells_of(line: str) -> list[str]:
    """A table row's cells, split on pipes that no backslash escapes."""
    # A pipe inside a code span is a cell boundary here: no status or home holds one.
    body = line.strip()[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [cell.strip() for cell in UNESCAPED_PIPE.split(body)]


def step_table_rows(lines: list[str]) -> list[tuple[str, str]]:
    """The (step, line) of each step row among the unfenced lines of Part B: a row
    of a table headed by exactly the four step columns whose first cell is a step."""
    rows: list[tuple[str, str]] = []
    inside = False
    for line in lines:
        if not line.startswith("|"):
            inside = False
            continue
        cells = cells_of(line)
        if cells == STEP_COLUMNS:
            inside = True
        elif inside and STEP_CELL.match(cells[0]):
            rows.append((cells[0], line))
    return rows


def shape_findings(part_b: list[str], part_d: list[str]) -> list[str]:
    """Table lines the row readers would pass over: an indented one in Part B or
    Part D, a step table's row whose first cell is not the bare step ID, and a
    question whose first cell is not the bare number."""
    found = []
    inside = False
    for part, lines in (("B", part_b), ("D", part_d)):
        for line in lines:
            if line != line.lstrip() and line.lstrip().startswith("|"):
                found.append(
                    f"the plan's Part {part}: a table line is indented "
                    f"('{line.strip()[:40]}'); it is not read as a row: start it "
                    f"at the margin"
                )
    for line in part_b:
        if not line.startswith("|"):
            inside = False
            continue
        cells = cells_of(line)
        if cells == STEP_COLUMNS:
            inside = True
        elif inside and not STEP_CELL.match(cells[0]) and LOOSE_STEP.search(cells[0]):
            found.append(
                f"the plan's Part B: a step row's first cell is '{cells[0][:40]}'; "
                f"it is the bare step ID (S and three digits), or the row is held "
                f"to no rule"
            )
    for line in part_d:
        if not line.startswith("|"):
            continue
        cell = cells_of(line)[0]
        if not is_number(cell) and LOOSE_NUMBER.match(cell):
            found.append(
                f"Part D: a question's first cell is '{cell[:40]}'; it is the bare "
                f"number, or the row is held to no rule"
            )
    return found


def is_number(cell: str) -> bool:
    return cell.isascii() and cell.isdecimal()


def header_findings(lines: list[str]) -> list[str]:
    """A table of Part B headed ``ID`` with other columns than the four: its rows
    would not be read as step rows. (A Status column has its own finding.)"""
    return [
        f"the plan's Part B: a step table is headed '{line.strip()[:60]}'; its "
        f"columns are exactly '| {' | '.join(STEP_COLUMNS)} |', or no row of it "
        f"is held to the rules"
        for line in lines
        if line.startswith("|")
        and (cells := cells_of(line))[:1] == ["ID"]
        and cells != STEP_COLUMNS
        and "Status" not in cells
    ]


def row_faults(line: str) -> list[str]:
    """What is wrong with one step row, one entry for each fault."""
    faults = []
    if (count := len(cells_of(line))) != 4:
        faults.append(f"has {count} cells, not four")
    if len(line) > ROW_MAX:
        faults.append(f"is {len(line)} characters, over ROW_MAX ({ROW_MAX})")
    if match := DATE.search(line):
        faults.append(f"holds a date ('{match[0]}')")
    if match := STRUCK_TEXT.search(line):
        faults.append(f"holds struck-through text ('{match[0]}')")
    if OWNER.search(line) and QUOTE.search(line):
        faults.append("quotes the owner")
    if match := BUILD_LABEL.search(line):
        faults.append(f"carries a build label ('{match[0]}')")
    return faults


def row_findings(rows: list[tuple[str, str]]) -> list[str]:
    found: list[str] = []
    for step, line in rows:
        faults = row_faults(line)
        if step not in ROW_EXCEPTIONS:
            found.extend(
                f"the plan's Part B: the row of {step} {fault}; {ROW_REMEDY}"
                for fault in faults
            )
        elif not faults:
            found.append(
                f"the plan's Part B: {step} is in ROW_EXCEPTIONS "
                f"(scripts/plan_gate.py) and its row passes the gate; remove "
                f"{step} from ROW_EXCEPTIONS"
            )
    return found


def status_findings(lines: list[str]) -> list[str]:
    """A status line or the old heading, typed into the plan (unfenced lines)."""
    found = []
    for line in lines:
        if line.startswith("**Status:**"):
            found.append(
                f"the plan holds a status line ('{line[:40]}'): {STATUS_REMEDY}"
            )
        elif STATUS_HEADING.match(line):
            found.append(
                f"the plan holds a heading 'Where the project stands': {STATUS_REMEDY}"
            )
    return found


def question_rows(lines: list[str]) -> list[tuple[str, str]]:
    """The (number, line) of each question row among the unfenced lines of Part D:
    a table line whose first cell is a number."""
    return [
        (cell, line)
        for line in lines
        if line.startswith("|") and is_number(cell := cells_of(line)[0])
    ]


def question_findings(rows: list[tuple[str, str]]) -> list[str]:
    found = []
    for number, line in rows:
        if ANSWERED.search(line) or STRUCK_TEXT.search(line):
            found.append(
                f"Part D: question {number[:20]} is answered or struck through; move it, "
                f"whole, to the end of {CLOSED_FILE}"
            )
        if len(line) > QUESTION_ROW_MAX:
            found.append(
                f"Part D: question {number[:20]} is {len(line)} characters, over "
                f"QUESTION_ROW_MAX ({QUESTION_ROW_MAX}); a question is a sentence or "
                f"two: put the rest into the step's file"
            )
    return found


def closed_findings(lines: list[str], unclosed: bool) -> tuple[list[str], list[str]]:
    """The findings of the closed-questions file's unfenced lines, and the numbers
    of its rows. ``unclosed`` is whether a code fence is left open at its end."""
    found = []
    if unclosed:
        found.append(
            f"{CLOSED_FILE}: a code fence is opened and never closed; the rows "
            f"after it are not read"
        )
    headers = [
        i
        for i, line in enumerate(lines)
        if line.startswith("|") and cells_of(line)[:4] == QUESTION_COLUMNS
    ]
    if len(headers) != 1:
        found.append(
            f"{CLOSED_FILE}: the table's header '| {' | '.join(QUESTION_COLUMNS)} |' "
            f"must be there exactly once, not {len(headers)} times"
        )
        return found, []
    numbers = []
    for line in lines[headers[0] + 1 :]:
        if not line.startswith("|") or SEPARATOR.match(line):
            continue
        cell = cells_of(line)[0]
        if not is_number(cell):
            found.append(
                f"{CLOSED_FILE}: a row whose first cell is not a number ('{cell[:40]}')"
            )
            continue
        numbers.append(cell)
        if not ANSWERED.search(line):
            found.append(
                f"{CLOSED_FILE}: question {cell[:20]} is not answered (its Question "
                f"cell holds no '**Answered <date>: …**'); an open question "
                f"belongs in Part D of the plan"
            )
    return found, numbers


def number_findings(open_numbers: list[str], closed_numbers: list[str]) -> list[str]:
    """A question number is in exactly one row of the two tables."""
    every = [n.lstrip("0") or "0" for n in open_numbers + closed_numbers]
    return [
        f"question {number[:20]} must be in exactly one row of Part D and "
        f"{CLOSED_FILE}, not {every.count(number)}"
        for number in sorted(set(every), key=lambda n: (len(n), n))
        if every.count(number) > 1
    ]


def fixed_text(text: str, rows: list[str], between: list[str]) -> int:
    """The plan's bytes less each step row, question row and generated line (and
    the newline that ends each)."""
    return len(text.encode()) - sum(len(x.encode()) + 1 for x in [*rows, *between])


def ceiling_findings(fixed: int) -> list[str]:
    lead = f"the plan's fixed text is {fixed:,} bytes"
    if fixed > FIXED_TEXT_MAX:
        return [
            f"{lead}, over FIXED_TEXT_MAX ({FIXED_TEXT_MAX:,}); a new rule takes the "
            f"room of an old sentence, and {OWNER_NOTE}"
        ]
    if fixed < FIXED_TEXT_MAX - FIXED_TEXT_SLACK:
        lower = -(-fixed // 100) * 100
        return [
            f"{lead}, {FIXED_TEXT_MAX - fixed:,} under FIXED_TEXT_MAX "
            f"({FIXED_TEXT_MAX:,}), more than the slack of {FIXED_TEXT_SLACK:,}; "
            f"lower FIXED_TEXT_MAX to {lower:,} (scripts/plan_gate.py), so the "
            f"ceiling follows the plan down; {OWNER_NOTE}"
        ]
    return []


def plan_findings(
    text: str,
    lines: list[str],
    part_b: list[str],
    part_d: list[str],
    between: list[str] | None,
    closed: tuple[list[str], bool] | None,
) -> list[str]:
    """Every finding of the gate. ``lines``, ``part_b`` and ``part_d`` are the
    unfenced lines of the plan, of Part B and of Part D; ``between`` the lines
    between the progress markers (None when they are not both there); ``closed`` the
    closed-questions file's unfenced lines and whether a fence is left open in it
    (None when the file does not exist)."""
    steps = step_table_rows(part_b)
    questions = question_rows(part_d)
    found = header_findings(part_b)
    found.extend(shape_findings(part_b, part_d))
    found.extend(row_findings(steps))
    found.extend(status_findings(lines))
    found.extend(question_findings(questions))
    closed_found, closed_numbers = closed_findings(*closed) if closed else ([], [])
    found.extend(closed_found)
    found.extend(number_findings([n for n, _ in questions], closed_numbers))
    if between is not None:
        rows = [line for _, line in steps] + [line for _, line in questions]
        found.extend(ceiling_findings(fixed_text(text, rows, between)))
    return found
