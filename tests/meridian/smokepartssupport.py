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

from kindsupport import SMOKE_PART_FIRST_LINE, SMOKE_PART_HINT, SMOKE_PART_LINE

COMMON_LINE = '. "$(dirname "${BASH_SOURCE[0]}")/common.sh"'
PART_LINE = '. "${{KIND_DIR}}/smoke.d/{name}"'
NEED_TOOLS = "need_tools "
FUNCTION_ONE_LINE = re.compile(r"^([A-Za-z_]\w*)\(\) \{ .* \}$")
FUNCTION_OPEN = re.compile(r"^([A-Za-z_]\w*)\(\) \{$")
READONLY = re.compile(r"^readonly ([A-Za-z_]\w*)=")
GLOBAL = re.compile(r"^([A-Za-z_]\w*)=")
HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")
SUBSTITUTION = re.compile(r"\$\((?!\()|`")
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


def statement_end(lines: list[str], first: int) -> tuple[int | None, str, bool]:
    """The assignment that begins at ``lines[first]``: the index of its last line
    (``None`` when it never ends), its code without a trailing comment, and
    whether a second command follows it on a line. Single quotes, double quotes
    (with their backslashes: an apostrophe inside them opens nothing), the
    parentheses of an array or a substitution and a backslash at the end of a
    line carry a statement over to the next line."""
    single = double = chained = False
    depth = 0
    code: list[str] = []
    for index in range(first, len(lines)):
        line = lines[index]
        joined, position = False, 0
        while position < len(line):
            char = line[position]
            position += 1
            if single:
                single = char != "'"
            elif char == "\\":
                joined = position == len(line)
                position += 1
            elif double:
                double = char != '"'
            elif char == "'":
                single = True
            elif char == '"':
                double = True
            elif char == "#" and (position == 1 or line[position - 2] in " \t"):
                break
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char in ";&|" and depth == 0:
                chained = True
            code.append(char)
        if not (single or double or depth > 0 or joined):
            return index, "".join(code), chained
    return None, "".join(code), chained


def function_end(lines: list[str], first: int) -> int | None:
    """The index of the line ``}`` at column 0 that closes the function opened at
    ``lines[first]``, or ``None``. A heredoc's lines are not read: they may hold
    a ``}`` at column 0."""
    waiting: list[tuple[str, bool]] = []  # (delimiter, indented by tabs) in order
    for index in range(first + 1, len(lines)):
        line = lines[index]
        if waiting:
            delimiter, tabs = waiting[0]
            if (line.lstrip("\t") if tabs else line) == delimiter:
                waiting.pop(0)
        elif line == "}":
            return index
        elif not line.lstrip().startswith("#"):
            waiting.extend((m[3], m[1] == "-") for m in HEREDOC.finditer(line))
    return None


def read_definitions(text: str, first_line: int = 1) -> Definitions:
    """Read ``text`` as a part is meant to read: comment lines and blank lines;
    ``readonly NAME=...``; ``name=...`` at column 0 (no command substitution:
    that would run when the part is sourced); ``name() {`` ... ``}`` and the
    one-line function. Anything else is a problem, and the line it is on."""
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
            found.functions.append(one[1])
        elif opened:
            end = function_end(lines, index)
            if end is None:
                complain(index, "a function that is never closed")
                break
            found.functions.append(opened[1])
            step = end + 1 - index
        elif assignment := readonly or plain:
            end, code, chained = statement_end(lines, index)
            if end is None:
                complain(index, "an assignment that is never closed")
                break
            (found.constants if readonly else found.globals).append(assignment[1])
            if chained:
                complain(index, "a second command on the line of an assignment")
            if plain and SUBSTITUTION.search(code):
                complain(index, "a command substitution in a global runs at once")
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
