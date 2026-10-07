"""The layout of ``smoke.sh`` and its parts as rules (S074).

``infra/kind/smoke.sh`` is being split: the entry file keeps the preconditions,
the traps, the ``check_*`` calls and the summary, and ``infra/kind/smoke.d/``
holds one part per check plus ``shared.sh``, each sourced by the entry file. The
rules below hold today, when no part exists, and bind every part that comes:

- a part is sourced once, by the two lines ``# shellcheck source=smoke.d/<file>``
  and ``. "${KIND_DIR}/smoke.d/<file>"``, after ``common.sh``'s line and before
  the first precondition, ``shared.sh`` first, and no ``.`` line stands among
  the ``check_*`` calls;
- a part is not executable, starts with ``# shellcheck shell=bash`` and holds
  DEFINITIONS ONLY (comments, blank lines, ``readonly NAME=...``, ``name=...``
  at column 0 and functions), so sourcing it runs nothing and the order of the
  ``.`` lines does not matter; no part holds the text ``need_tools ``, which a
  test reads the first one of; no name is defined twice.

The rules are functions that return what is wrong, so that each has a toy under
``tmp_path`` that breaks it and a test that the rule says so. The reader of
definitions is run over the whole of today's script as a second proof that it
reads real text. ``smoke_text`` (kindsupport.py), which puts the script back in
one text for the tests that cut it, is tested on toys here too.
"""

import re
import stat
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from kindsupport import (
    KIND_DIR,
    SMOKE_PART_FIRST_LINE,
    SMOKE_PART_HINT,
    SMOKE_PART_LINE,
    SMOKE_SH,
    smoke_text,
)

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


# ── toys ─────────────────────────────────────────────────────────────────────
def sourcing(*names: str) -> str:
    """The two lines of each part, as the entry file has them."""
    return "".join(
        f"{SMOKE_PART_HINT.format(name=name)}\n{PART_LINE.format(name=name)}\n"
        for name in names
    )


def entry_file(
    *,
    paragraphs: str = "",
    before: str = "",
    after: str = "",
    calls: str = "",
    tools: bool = True,
) -> str:
    """A toy ``smoke.sh``: ``paragraphs`` are the numbered paragraphs of the
    checks it still holds, ``before`` stands ahead of ``common.sh``'s line,
    ``after`` between it and the preconditions, ``calls`` among the ``check_*``
    calls."""
    precondition = "need_tools docker kubectl\n" if tools else ""
    return (
        "#!/usr/bin/env bash\n"
        "# Opening sentence.\n"
        "# Parts: smoke.d/ (shared.sh, 01-first.sh, 02-second.sh)\n"
        f"{paragraphs}"
        "# Prints one line per check.\n"
        "set -euo pipefail\n"
        "\n"
        f"{before}"
        "# shellcheck source=common.sh\n"
        f"{COMMON_LINE}\n"
        "\n"
        f"{after}"
        "\n"
        f"{precondition}"
        "trap cleanup EXIT\n"
        "\n"
        "check_first\n"
        f"{calls}"
        "check_second\n"
        "if ((failures > 0)); then\n"
        "  exit 1\n"
        "fi\n"
    )


PART_NAMES = ("shared.sh", "01-first.sh", "02-second.sh")
ENTRY = entry_file(after=sourcing(*PART_NAMES))
SHARED = """# shellcheck shell=bash

readonly POLL_TIMEOUT=30
failures=0   # set by fail
pass() { printf 'PASS  %s\\n' "$*"; }
cleanup() {
  echo cleanup
}
"""
FIRST = """# shellcheck shell=bash
#   1. first:     does the first thing
#                 and goes on a second line.

# The URL of the first check.
readonly FIRST_URL=http://127.0.0.1/
check_first() {
  pass first
}
"""
SECOND = """# shellcheck shell=bash
#   2. second:    does the second thing

second_done=""
check_second() {
  pass second
}
"""
PARTS = {"shared.sh": SHARED, "01-first.sh": FIRST, "02-second.sh": SECOND}


def toy(
    tmp_path: Path, entry: str = ENTRY, parts: dict[str, str] | None = None
) -> Path:
    """A ``kind_dir`` with ``smoke.sh`` and its ``smoke.d/``, mode 644."""
    (tmp_path / "smoke.d").mkdir(parents=True)
    (tmp_path / "smoke.sh").write_text(entry, encoding="utf-8")
    for name, text in (PARTS if parts is None else parts).items():
        (tmp_path / "smoke.d" / name).write_text(text, encoding="utf-8")
        (tmp_path / "smoke.d" / name).chmod(0o644)
    return tmp_path


def problems_of(kind_dir: Path) -> list[str]:
    return [
        *layout_problems(kind_dir),
        *(p for path in part_files(kind_dir) for p in part_problems(path)),
        *duplicate_names(kind_dir),
    ]


# ── the layout, on the repository and on toys ────────────────────────────────
def test_the_layout_of_smoke_and_its_parts_holds_in_the_repository() -> None:
    assert layout_problems(KIND_DIR) == []
    assert [p for path in part_files(KIND_DIR) for p in part_problems(path)] == []
    assert duplicate_names(KIND_DIR) == []


def test_the_toy_that_follows_every_rule_has_no_problem(tmp_path: Path) -> None:
    assert problems_of(toy(tmp_path)) == []


SECOND_DOT = PART_LINE.format(name="02-second.sh")
WRONG_FORMS = {
    "source for the dot": ENTRY.replace(SECOND_DOT, f"source {SECOND_DOT[2:]}"),
    "a path of its own": ENTRY.replace(
        SECOND_DOT, '. "$(dirname "${BASH_SOURCE[0]}")/smoke.d/02-second.sh"'
    ),
}


@pytest.mark.parametrize(
    ("entry", "parts", "says"),
    [
        pytest.param(
            ENTRY,
            {**PARTS, "03-third.sh": FIRST.replace("first", "third")},
            "03-third.sh is not sourced",
            id="a part nothing sources",
        ),
        pytest.param(
            entry_file(after=sourcing(*PART_NAMES, "02-second.sh")),
            PARTS,
            "02-second.sh is sourced 2 times",
            id="a part sourced twice",
        ),
        pytest.param(
            ENTRY,
            {"shared.sh": SHARED, "01-first.sh": FIRST},
            "02-second.sh is sourced but is not there",
            id="a part that is not there",
        ),
        pytest.param(
            ENTRY.replace(SMOKE_PART_HINT.format(name="01-first.sh") + "\n", ""),
            PARTS,
            "no `# shellcheck source=smoke.d/01-first.sh` above",
            id="a . line with no hint",
        ),
        pytest.param(
            ENTRY.replace(f"\n{SECOND_DOT}", f"\n\n{SECOND_DOT}"),
            PARTS,
            "no `# shellcheck source=smoke.d/02-second.sh` above",
            id="a blank line between the hint and the . line",
        ),
        pytest.param(
            WRONG_FORMS["source for the dot"],
            PARTS,
            "a part is sourced in another form",
            id="source for the dot",
        ),
        pytest.param(
            WRONG_FORMS["a path of its own"],
            PARTS,
            "a part is sourced in another form",
            id="a path of its own",
        ),
        pytest.param(
            entry_file(before=sourcing("shared.sh"), after=sourcing(*PART_NAMES[1:])),
            PARTS,
            "shared.sh is sourced before common.sh",
            id="a part before common.sh",
        ),
        pytest.param(
            entry_file(after=sourcing(*PART_NAMES[:2])).replace(
                "trap cleanup EXIT", sourcing("02-second.sh") + "trap cleanup EXIT"
            ),
            PARTS,
            "02-second.sh is sourced after the preconditions",
            id="a part after the preconditions",
        ),
        pytest.param(
            entry_file(after=sourcing(*PART_NAMES), calls=f"{SECOND_DOT}\n"),
            PARTS,
            "a . line among the check_* calls",
            id="a . line among the calls",
        ),
        pytest.param(
            entry_file(after=sourcing("01-first.sh", "shared.sh", "02-second.sh")),
            PARTS,
            "shared.sh is not the first part sourced",
            id="shared.sh not first",
        ),
        pytest.param(
            entry_file(after=sourcing(*PART_NAMES), tools=False),
            PARTS,
            "no need_tools line",
            id="no precondition to place them before",
        ),
        pytest.param(
            ENTRY.replace(COMMON_LINE, "true"),
            PARTS,
            "not common.sh's line",
            id="no common.sh line",
        ),
    ],
)
def test_a_layout_rule_says_what_a_broken_toy_breaks(
    tmp_path: Path, entry: str, parts: dict[str, str], says: str
) -> None:
    problems = layout_problems(toy(tmp_path, entry, parts))

    assert any(says in problem for problem in problems), problems


def test_a_file_in_a_sub_directory_of_smoke_d_is_not_sourced(tmp_path: Path) -> None:
    kind_dir = toy(tmp_path)
    (kind_dir / "smoke.d" / "more").mkdir()
    (kind_dir / "smoke.d" / "more" / "x.sh").write_text(SHARED, encoding="utf-8")

    assert "more/x.sh is not sourced" in layout_problems(kind_dir)


# ── a part, on toys ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("part", "says"),
    [
        pytest.param(
            FIRST.replace("# shellcheck shell=bash\n", ""),
            "first line",
            id="no first line",
        ),
        pytest.param("\n" + FIRST, "first line", id="first line not first"),
        pytest.param(FIRST + "need_tools docker\n", "`need_tools `", id="need_tools"),
        pytest.param(
            FIRST + "# need_tools docker\n",
            "`need_tools `",
            id="a comment that says it",
        ),
        pytest.param(FIRST + "check_first\n", "line 10: not a definition", id="a call"),
        pytest.param(
            FIRST + "  # indented\n", "not a definition", id="an indented comment"
        ),
        pytest.param(
            FIRST + "readonly A=1; echo hi\n", "second command", id="a second command"
        ),
        pytest.param(
            FIRST + "a=1 && echo hi\n", "second command", id="a chained command"
        ),
        pytest.param(
            FIRST + "a=$(date)\n", "command substitution", id="a global that runs"
        ),
        pytest.param(FIRST + "b=`date`\n", "command substitution", id="a backtick"),
        pytest.param(
            FIRST + "f() {\n  echo\n", "never closed", id="a function with no end"
        ),
        pytest.param(
            FIRST + "readonly A='open\n", "never closed", id="a quote with no end"
        ),
        pytest.param(
            FIRST + "f() { echo; }; g\n",
            "not a definition",
            id="a command after a function",
        ),
        pytest.param(FIRST + "trap cleanup EXIT\n", "not a definition", id="a trap"),
    ],
)
def test_a_part_rule_says_what_a_broken_toy_part_breaks(
    tmp_path: Path, part: str, says: str
) -> None:
    kind_dir = toy(tmp_path, parts={**PARTS, "01-first.sh": part})

    problems = part_problems(kind_dir / "smoke.d" / "01-first.sh")

    assert any(says in problem for problem in problems), problems


def test_an_executable_part_is_a_problem(tmp_path: Path) -> None:
    kind_dir = toy(tmp_path)
    (kind_dir / "smoke.d" / "01-first.sh").chmod(0o755)

    (problem,) = part_problems(kind_dir / "smoke.d" / "01-first.sh")

    assert "executable" in problem


def test_a_name_defined_in_two_places_is_named(tmp_path: Path) -> None:
    twice = {**PARTS, "02-second.sh": SECOND + "pass() { :; }\nPOLL_TIMEOUT=1\n"}
    kind_dir = toy(tmp_path, parts=twice)

    assert duplicate_names(kind_dir) == [
        "POLL_TIMEOUT is defined in 02-second.sh, shared.sh",
        "pass is defined in 02-second.sh, shared.sh",
    ]


def test_a_name_defined_in_a_part_and_the_entry_is_named(tmp_path: Path) -> None:
    entry = ENTRY.replace(
        "trap cleanup EXIT\n", "readonly POLL_TIMEOUT=5\ntrap cleanup EXIT\n"
    )

    assert duplicate_names(toy(tmp_path, entry)) == [
        "POLL_TIMEOUT is defined in smoke.sh, shared.sh"
    ]


# ── the reader, shape by shape ───────────────────────────────────────────────
def test_the_reader_reads_a_constant_whose_quotes_span_lines() -> None:
    text = (
        "readonly PROBE='import sys\n"
        "def main():\n"
        "}\n"
        "# not a comment: inside the quotes\n"
        "readonly NOT_ONE=1\n"
        "print(1)\n"
        "'\n"
        "readonly NEXT=2\n"
    )

    found = read_definitions(text)

    assert (found.constants, found.problems) == (["PROBE", "NEXT"], [])


def test_the_reader_reads_a_function_with_a_heredoc_that_has_column_zero_lines() -> (
    None
):
    text = (
        "start_job() {\n"
        "  kctl create -f - >/dev/null <<EOF\n"
        "apiVersion: batch/v1\n"
        "{\n"
        "}\n"
        "name: x\n"
        "EOF\n"
        "  echo done\n"
        "}\n"
        "after() {\n"
        "  cat <<-'END'\n"
        "\t}\n"
        "\tEND\n"
        "}\n"
    )

    found = read_definitions(text)

    assert (found.functions, found.problems) == (["start_job", "after"], [])


def test_the_reader_does_not_take_a_heredoc_in_a_comment_or_a_here_string() -> None:
    text = (
        "f() {\n"
        "  # cat <<EOF would swallow the rest\n"
        '  read -r _ <<<"${x}"\n'
        "}\n"
        "g() { :; }\n"
    )

    found = read_definitions(text)

    assert (found.functions, found.problems) == (["f", "g"], [])


def test_the_reader_reads_an_apostrophe_inside_double_quotes_and_comments() -> None:
    text = (
        "readonly BANNER=\"the user's page: it's here\"\n"
        'readonly ESCAPED="a \\" quote and an \' apostrophe"\n'
        'name=""   # set by the check\'s first line\n'
        "readonly NEXT=1\n"
    )

    found = read_definitions(text)

    assert (found.constants, found.globals, found.problems) == (
        ["BANNER", "ESCAPED", "NEXT"],
        ["name"],
        [],
    )


def test_the_reader_reads_the_four_one_line_functions_and_the_rest_around_them() -> (
    None
):
    text = (
        "pass() { printf 'PASS  %s\\n' \"$*\"; }\n"
        "fail() { printf 'FAIL  %s\\n' \"$*\"; failures=$((failures + 1)); }\n"
        "skip() { printf 'SKIP  %s\\n' \"$*\"; skips=$((skips + 1)); }\n"
        "clean_lines() { printf '%s' \"$1\" | LC_ALL=C tr -cd '[:print:]\\n' |"
        " paste -sd ';' -; }\n"
        "\n"
        "# a comment\n"
        "next() {\n"
        "  :\n"
        "}\n"
    )

    found = read_definitions(text)

    assert found.functions == ["pass", "fail", "skip", "clean_lines", "next"]
    assert found.problems == []


def test_the_reader_reads_arrays_arithmetic_and_continued_lines() -> None:
    text = (
        "readonly LIST=(a b c)\n"
        "readonly MARGIN=$((DAYS * 86400 / 2))\n"
        'readonly TWO="one \\\n'
        'two"\n'
        "readonly JOINED=a\\\n"
        "b\n"
        'readonly PATH_OF="${KIND_DIR}/x.json"\n'
        'readonly ROOT="$(cd "$(dirname "$0")" && pwd)"\n'
        "count=0\n"
    )

    found = read_definitions(text)

    assert found.problems == []
    assert found.constants == ["LIST", "MARGIN", "TWO", "JOINED", "PATH_OF", "ROOT"]
    assert found.globals == ["count"]


def test_the_reader_names_the_line_it_cannot_call_a_definition() -> None:
    found = read_definitions("# c\n\nneed_tools docker\nreadonly A=1\n", first_line=10)

    assert found.problems == ["line 12: not a definition: need_tools docker"]


def test_the_reader_finds_what_a_regex_counts_in_todays_whole_script() -> None:
    lines = SMOKE_SH.splitlines()
    start = lines.index(COMMON_LINE) + 1
    stop = next(i for i, line in enumerate(lines) if line.startswith("trap "))
    region = "\n".join(lines[start:stop])

    found = read_definitions(region, first_line=start + 1)

    # Counted by regex, which does not tile quotes: a reader that took a constant's
    # span, a heredoc or line 1011's apostrophe wrong would miss a name or add one.
    # The probes' own column-0 lines (`err=...`, `status=$?`, inside quotes) are
    # why the globals are counted by their shape, `name=""` or `name=0`.
    shape = r'^[a-z_]+=(?:""|0)(?: +#.*)?$'
    assert len(found.functions) == len(re.findall(r"^\w+\(\) \{", region, re.M))
    assert len(found.constants) == len(re.findall(r"^readonly \w+=", region, re.M))
    assert len(found.globals) == len(re.findall(shape, region, re.M))
    assert len(set(found.names())) == len(found.names())
    # What else stands among the definitions: the three preconditions, and no more.
    assert [problem.split(": ", 1)[1] for problem in found.problems] == [
        "not a definition: need_tools docker kubectl curl jq base64 openssl timeout",
        "not a definition: require_local_docker",
        "not a definition: need_cluster",
    ]


# ── smoke_text: the script in one text, as it was before the split ───────────
def test_smoke_text_puts_the_paragraphs_in_the_header_and_the_definitions_in_place(
    tmp_path: Path,
) -> None:
    text = smoke_text(toy(tmp_path))

    assert text == "".join(
        [
            "#!/usr/bin/env bash\n",
            "# Opening sentence.\n",
            "# Parts: smoke.d/ (shared.sh, 01-first.sh, 02-second.sh)\n",
            "#   1. first:     does the first thing\n",
            "#                 and goes on a second line.\n",
            "#   2. second:    does the second thing\n",
            "# Prints one line per check.\n",
            "set -euo pipefail\n",
            "\n",
            "# shellcheck source=common.sh\n",
            f"{COMMON_LINE}\n",
            "\n",
            SHARED.split("\n", 1)[1],
            "\n".join(FIRST.splitlines()[3:]) + "\n",
            "\n".join(SECOND.splitlines()[2:]) + "\n",
            "\n",
            "need_tools docker kubectl\n",
            "trap cleanup EXIT\n",
            "\n",
            "check_first\n",
            "check_second\n",
            "if ((failures > 0)); then\n",
            "  exit 1\n",
            "fi\n",
        ]
    )
    assert "shellcheck source=smoke.d" not in text
    assert '. "${KIND_DIR}/smoke.d' not in text


def test_smoke_text_keeps_the_numbered_paragraphs_in_order_with_those_left_in_the_entry(
    tmp_path: Path,
) -> None:
    # Checks 1 and 3 moved, 2 stays in the entry (as 8 and 10 do for a while).
    second = "#   2. second:    does the second thing\n#                 in two lines\n"
    third = (
        "# shellcheck shell=bash\n#   3. third:     does the third\n\nthird() { :; }\n"
    )
    entry = entry_file(
        paragraphs=second, after=sourcing("shared.sh", "01-first.sh", "03-third.sh")
    )
    parts = {"shared.sh": SHARED, "01-first.sh": FIRST, "03-third.sh": third}

    header = smoke_text(toy(tmp_path, entry, parts)).split("set -euo pipefail")[0]

    assert header.splitlines()[3:] == [
        "#   1. first:     does the first thing",
        "#                 and goes on a second line.",
        "#   2. second:    does the second thing",
        "#                 in two lines",
        "#   3. third:     does the third",
        "# Prints one line per check.",
    ]


def test_smoke_text_puts_an_unnumbered_block_after_the_paragraphs_before_the_closing(
    tmp_path: Path,
) -> None:
    shared = "# shellcheck shell=bash\n# What every check shares.\n\nfailures=0\n"
    second = "#   2. second:    does the second thing\n"
    entry = entry_file(paragraphs=second, after=sourcing(*PART_NAMES[:2]))

    text = smoke_text(toy(tmp_path, entry, {**PARTS, "shared.sh": shared}))

    assert text.split("set -euo pipefail")[0].splitlines()[3:] == [
        "#   1. first:     does the first thing",
        "#                 and goes on a second line.",
        "#   2. second:    does the second thing",
        "# What every check shares.",
        "# Prints one line per check.",
    ]


def test_smoke_text_with_no_paragraph_left_in_the_entry_keeps_the_closing_last(
    tmp_path: Path,
) -> None:
    # The end state of the split: the entry holds the opening sentence, an index
    # line and the closing sentence, and every paragraph is in a part.
    header = smoke_text(toy(tmp_path)).split("set -euo pipefail")[0]

    assert header.splitlines()[2:] == [
        "# Parts: smoke.d/ (shared.sh, 01-first.sh, 02-second.sh)",
        "#   1. first:     does the first thing",
        "#                 and goes on a second line.",
        "#   2. second:    does the second thing",
        "# Prints one line per check.",
    ]


def test_smoke_text_of_an_entry_with_no_part_is_the_file_itself(tmp_path: Path) -> None:
    (tmp_path / "smoke.sh").write_text(entry_file(), encoding="utf-8")

    assert smoke_text(tmp_path) == entry_file()
    assert smoke_text(KIND_DIR).startswith("#!/usr/bin/env bash\n")


def test_smoke_text_refuses_a_part_without_its_first_line(tmp_path: Path) -> None:
    kind_dir = toy(tmp_path, parts={**PARTS, "01-first.sh": FIRST.split("\n", 1)[1]})

    with pytest.raises(ValueError, match="must start with"):
        smoke_text(kind_dir)


def test_smoke_text_hoists_nothing_from_a_part_that_starts_with_a_blank_line(
    tmp_path: Path,
) -> None:
    # The leading comment block ends at the first line that is not a comment: a
    # part with no paragraph puts a blank line right after its first line, or the
    # comment of its first definition would be taken for a paragraph.
    shared = (
        "# shellcheck shell=bash\n\n# The poll's bound.\nreadonly POLL_TIMEOUT=30\n"
    )

    text = smoke_text(toy(tmp_path, parts={**PARTS, "shared.sh": shared}))

    before, after = text.split("set -euo pipefail")
    assert "# The poll's bound.\nreadonly POLL_TIMEOUT=30\n" in after
    assert "poll's bound" not in before


def test_smoke_text_hoists_the_comment_of_a_first_definition_with_no_blank_line(
    tmp_path: Path,
) -> None:
    # What the rule above guards against, said once: without the blank line the
    # comment leaves its constant.
    shared = "# shellcheck shell=bash\n# The poll's bound.\nreadonly POLL_TIMEOUT=30\n"

    text = smoke_text(toy(tmp_path, parts={**PARTS, "shared.sh": shared}))

    assert "poll's bound" in text.split("set -euo pipefail")[0]
