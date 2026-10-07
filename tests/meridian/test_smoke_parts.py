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

The reader and the rules are in ``smokepartssupport.py``; the reader's own
tests are in ``test_smoke_parts_reader.py``.
"""

from pathlib import Path

import pytest
from kindsupport import (
    KIND_DIR,
    SMOKE_PART_HINT,
    smoke_text,
)
from smokepartssupport import (
    COMMON_LINE,
    PART_LINE,
    duplicate_names,
    layout_problems,
    part_files,
    part_problems,
)


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
