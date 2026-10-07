"""The reader of definitions and the rules for ``smoke.sh``'s parts (S074).

Support for ``test_smoke_parts.py`` (the layout and ``smoke_text``) and
``test_smoke_parts_reader.py`` (the reader, shape by shape). The reader says what
a part holds: functions, ``readonly`` constants, ``name=...`` at column 0, and
each line that is none of them. The rules are functions that return what is
wrong with the layout of ``infra/kind/smoke.sh`` and ``infra/kind/smoke.d/``, so
that a toy can break each one. No test lives here.
"""

import re
import stat
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from kindsupport import (
    SMOKE_PARAGRAPH,
    SMOKE_PART_FIRST_LINE,
    SMOKE_PART_HINT,
    SMOKE_PART_LINE,
)

COMMON_LINE = '. "$(dirname "${BASH_SOURCE[0]}")/common.sh"'
PART_LINE = '. "${{KIND_DIR}}/smoke.d/{name}"'
NEED_TOOLS = "need_tools "
FUNCTION_ONE_LINE = re.compile(r"^([A-Za-z_]\w*)\(\) \{ ")
FUNCTION_OPEN = re.compile(r"^([A-Za-z_]\w*)\(\) \{$")
READONLY = re.compile(r"^readonly ([A-Za-z_]\w*)=")
GLOBAL = re.compile(r"^([A-Za-z_]\w*)=")
HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")
SOURCING = re.compile(r"^(\.|source) ")


# ── the reader of definitions ────────────────────────────────────────────────
@dataclass
class Definitions:
    """What ``read_definitions`` found, and each line it could not call a
    definition, as ``line N: why: the line``."""

    functions: list[str] = field(default_factory=list)
    constants: list[str] = field(default_factory=list)
    globals: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def names(self) -> list[str]:
        return [*self.functions, *self.constants, *self.globals]


def statement_end(
    lines: list[str], first: int, offset: int = 0
) -> tuple[int | None, list[str]]:
    """The assignment that begins at ``lines[first][offset:]`` (``offset`` is where
    ``NAME=`` ends): the index of its last line (``None`` when it never ends) and
    what in it would run something when the part is sourced. Single quotes,
    double quotes (with their backslashes: an apostrophe inside them opens
    nothing), the parentheses of an array or a substitution and a backslash at
    the end of a line carry a statement over to the next line. A problem is a
    second command (``;``, ``&``, ``|``), a redirection, a word after the value
    (``a=1 echo x``, also on a continued line) and a command substitution
    (``$(...)`` or a backtick) outside single quotes; ``$((...))`` is arithmetic
    and runs nothing."""
    single = double = joined = spaced = False
    depth = 0
    why: list[str] = []
    for index in range(first, len(lines)):
        line = lines[index]
        position = offset if index == first else 0
        joined = False
        while position < len(line):
            char = line[position]
            position += 1
            ahead = line[position : position + 2]
            if not single and (
                char == "`" or (char == "$" and ahead[:1] == "(" != ahead[1:])
            ):
                why.append("a command substitution runs when the part is sourced")
            if single:
                single = char != "'"
            elif double and char == "\\":
                position += 1
            elif double:
                double = char != '"'
            elif char == "\\":
                joined = position == len(line)
                if spaced and not joined:
                    why.append("a word after the value: a command")
                spaced = spaced and joined
                position += 1
            elif char in " \t":
                spaced = spaced or depth == 0
            elif char == "#" and (position == 1 or line[position - 2] in " \t"):
                break
            else:
                if depth == 0 and char in ";&|":
                    why.append("a second command on the line of an assignment")
                elif depth == 0 and char in "<>":
                    why.append("a redirection after the value")
                elif depth == 0 and spaced:
                    why.append("a word after the value: a command")
                spaced = False
                if char == "'":
                    single = True
                elif char == '"':
                    double = True
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
        if not (single or double or depth > 0 or joined):
            return index, list(dict.fromkeys(why))
    return None, list(dict.fromkeys(why))


def one_line_function_end(line: str) -> int | None:
    """For a line that begins ``name() { ``: the index of the ``}`` that closes
    the function, or ``None``. A ``}`` that ends a ``${...}`` or a group inside it
    does not close it, and nor does one inside quotes."""
    single = double = False
    depth = 1
    position = line.index("{") + 1
    while position < len(line):
        char = line[position]
        position += 1
        if single:
            single = char != "'"
        elif char == "\\":
            position += 1
        elif double:
            double = char != '"'
        elif char == "'":
            single = True
        elif char == '"':
            double = True
        elif char == "$" and line[position : position + 1] == "{":
            depth += 1
            position += 1
        elif (
            char == "{"
            and line[position - 2] in " ;"
            and line[position : position + 1] == " "
        ):
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return position - 1
    return None


def function_end(lines: list[str], first: int) -> tuple[int | None, str]:
    """The index of the line that closes the function opened at ``lines[first]``
    (``}`` at column 0), or ``None``, and a complaint when that line starts with
    ``}`` and holds more (``}; cmd``, ``} && cmd``): it still ends the function,
    so the next function is not swallowed. A heredoc's lines are not read: they
    may hold a ``}`` at column 0."""
    waiting: list[tuple[str, bool]] = []  # (delimiter, indented by tabs) in order
    for index in range(first + 1, len(lines)):
        line = lines[index]
        if waiting:
            delimiter, tabs = waiting[0]
            if (line.lstrip("\t") if tabs else line) == delimiter:
                waiting.pop(0)
        elif line == "}":
            return index, ""
        elif line.startswith("}"):
            return index, "something after the closing brace"
        elif not line.lstrip().startswith("#"):
            waiting.extend((m[3], m[1] == "-") for m in HEREDOC.finditer(line))
    return None, ""


def read_definitions(text: str, first_line: int = 1) -> Definitions:
    """Read ``text`` as a part is meant to read: comment lines and blank lines;
    ``readonly NAME=...`` and ``name=...`` at column 0; ``name() {`` ... ``}`` and
    the one-line function. Anything else is a problem, and the line it is on.
    Sourcing a part must run nothing, so an assignment is also a problem when it
    holds a second command, a redirection, a word after its value or a command
    substitution outside single quotes, and so is something after a function's
    closing brace. ``readonly`` is held to the same rule as a global: counted on
    2026-10-08, no constant of ``smoke.sh`` or of its parts holds a command
    substitution, so nothing is allowed that runs. The inside of a function is
    not read: it runs only when called."""
    lines = text.splitlines()
    found = Definitions()

    def complain(at: int, why: str) -> None:
        found.problems.append(f"line {first_line + at}: {why}: {lines[at]}")

    index = 0
    while index < len(lines):
        line = lines[index]
        one, opened = FUNCTION_ONE_LINE.match(line), FUNCTION_OPEN.match(line)
        readonly, plain = READONLY.match(line), GLOBAL.match(line)
        step = 1
        if not line.strip() or line.startswith("#"):
            pass
        elif one:
            end = one_line_function_end(line)
            if end is None:
                complain(index, "a one-line function that is never closed")
            else:
                found.functions.append(one[1])
                rest = line[end + 1 :].strip()
                if rest and not rest.startswith("#"):
                    complain(index, "something after the closing brace")
        elif opened:
            end, why = function_end(lines, index)
            if end is None:
                complain(index, "a function that is never closed")
                break
            found.functions.append(opened[1])
            if why:
                complain(end, why)
            step = end + 1 - index
        elif assignment := readonly or plain:
            end, problems = statement_end(lines, index, assignment.end())
            if end is None:
                complain(index, "an assignment that is never closed")
                break
            (found.constants if readonly else found.globals).append(assignment[1])
            for why in problems:
                complain(index, why)
            step = end + 1 - index
        else:
            complain(index, "not a definition")
        index += step
    return found


# ── the rules ────────────────────────────────────────────────────────────────
def part_files(kind_dir: Path) -> list[Path]:
    return sorted(p for p in (kind_dir / "smoke.d").rglob("*") if p.is_file())


def layout_problems(kind_dir: Path) -> list[str]:
    """What is wrong with how ``smoke.sh`` of ``kind_dir`` sources the files of
    its ``smoke.d/``."""
    entry = (kind_dir / "smoke.sh").read_text(encoding="utf-8").splitlines()
    files = [
        path.relative_to(kind_dir / "smoke.d").as_posix()
        for path in part_files(kind_dir)
    ]
    problems: list[str] = []
    sourced: dict[str, list[int]] = defaultdict(list)
    for index, line in enumerate(entry):
        found, where = SMOKE_PART_LINE.match(line), f"line {index + 1}"
        if found:
            name = found[1]
            sourced[name].append(index)
            if entry[max(index - 1, 0)] != SMOKE_PART_HINT.format(name=name):
                problems.append(
                    f"{where}: no `{SMOKE_PART_HINT.format(name=name)}` above"
                )
        elif SOURCING.match(line) and "smoke.d" in line:
            problems.append(f"{where}: a part is sourced in another form: {line}")
        elif line.startswith("# shellcheck source=smoke.d/"):
            name = line.removeprefix("# shellcheck source=smoke.d/")
            if entry[index + 1 : index + 2] != [PART_LINE.format(name=name)]:
                problems.append(f"{where}: the hint of {name} is not above its . line")
    problems += [f"{name} is not sourced" for name in files if name not in sourced]
    problems += [
        f"{name} is sourced but is not there" for name in sourced if name not in files
    ]
    problems += [
        f"{n} is sourced {len(at)} times" for n, at in sourced.items() if len(at) > 1
    ]
    problems += place_problems(entry, sourced)
    problems += region_problems(entry)
    # The calls are the lines at column 0 that are a check_ name and nothing else.
    calls = [i for i, line in enumerate(entry) if re.fullmatch(r"check_\w+", line)]
    problems += [
        f"line {at + 1}: a . line among the check_* calls"
        for at, line in enumerate(entry)
        if calls and at > calls[0] and SOURCING.match(line)
    ]
    return problems


def place_problems(entry: list[str], sourced: dict[str, list[int]]) -> list[str]:
    """Each part after ``common.sh``'s line and before the first precondition,
    ``shared.sh`` first."""
    if not sourced:
        return []
    if COMMON_LINE not in entry:
        return ["smoke.sh has parts and not common.sh's line"]
    tools = [i for i, line in enumerate(entry) if line.startswith(NEED_TOOLS)]
    if not tools:
        return ["smoke.sh has parts and no need_tools line"]
    after, before = entry.index(COMMON_LINE), tools[0]
    problems = [
        f"line {at + 1}: {name} is sourced "
        + ("before common.sh" if at < after else "after the preconditions")
        for name, lines in sourced.items()
        for at in lines
        if not after < at < before
    ]
    first = min(at for lines in sourced.values() for at in lines)
    if "shared.sh" in sourced and sourced["shared.sh"][0] != first:
        problems.append("shared.sh is not the first part sourced")
    return problems


def region_problems(entry: list[str]) -> list[str]:
    """Between ``common.sh``'s line and the first precondition the entry holds
    comment lines, blank lines, the two-line source pairs and definitions, and
    nothing else: a ``.`` line inside an ``if`` that never runs, or after an
    ``exit``, is "sourced once, in place" to the rules and never read by bash."""
    tools = [i for i, line in enumerate(entry) if line.startswith(NEED_TOOLS)]
    if COMMON_LINE not in entry or not tools:
        return []
    start = entry.index(COMMON_LINE) + 1
    region = [
        "" if SMOKE_PART_LINE.match(line) else line for line in entry[start : tools[0]]
    ]
    found = read_definitions("\n".join(region), first_line=start + 1)
    return [
        f"between common.sh's line and the first precondition, {problem}"
        for problem in found.problems
    ]


def leading_block_problems(lines: list[str]) -> list[str]:
    """The comment lines that follow a part's first line are hoisted into the
    header by ``smoke_text``, so they are either none or one numbered paragraph
    (``#   N. name:`` first), and a blank line follows them: a constant's own
    comment right there would silently leave its code."""
    end = 1
    while end < len(lines) and lines[end].startswith("#"):
        end += 1
    if end == 1:
        return []
    problems = []
    if not SMOKE_PARAGRAPH.match(lines[1]):
        problems.append(
            "its leading comment block does not begin with a numbered paragraph "
            f"(`#   N. name:`), and is hoisted into the header: {lines[1]}"
        )
    if end < len(lines) and lines[end].strip():
        problems.append(
            f"no blank line follows its leading comment block (line {end + 1}): "
            f"{lines[end]}"
        )
    if end == len(lines):
        problems.append("a blank line must follow its leading comment block")
    return problems


def part_problems(path: Path) -> list[str]:
    """What is wrong with one part: its mode, its first line, a ``need_tools``
    text, and every line that is not a definition."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    problems = []
    if stat.S_IMODE(path.stat().st_mode) & 0o111:
        problems.append("it is executable: a part is mode 644, and is sourced")
    if lines[:1] != [SMOKE_PART_FIRST_LINE]:
        problems.append(f"its first line is not `{SMOKE_PART_FIRST_LINE}`")
    if NEED_TOOLS in text:
        problems.append(f"it holds the text `{NEED_TOOLS}`")
    problems += leading_block_problems(lines)
    problems += read_definitions("\n".join(lines[1:]), first_line=2).problems
    return [f"{path.name}: {problem}" for problem in problems]


def duplicate_names(kind_dir: Path) -> list[str]:
    """Names defined twice across the entry file and the parts."""
    owners: dict[str, list[str]] = defaultdict(list)
    files = [kind_dir / "smoke.sh", *part_files(kind_dir)]
    for path in files:
        for name in read_definitions(path.read_text(encoding="utf-8")).names():
            owners[name].append(path.name)
    return [
        f"{name} is defined in {', '.join(files)}"
        for name, files in sorted(owners.items())
        if len(files) > 1
    ]
