"""The reader of definitions, shape by shape, and over today's script (S074).

The reader (``smokepartssupport.py``) is what the rules about ``smoke.sh``'s parts
stand on: a part holds definitions only, so the reader must tell a definition from
a statement on every shape the script has. Each shape has a toy here, and the
whole of today's script is read as a second proof that it reads real text.
"""

import re

import pytest
from kindsupport import KIND_DIR, SMOKE_SH
from smokepartssupport import (
    COMMON_LINE,
    Definitions,
    part_files,
    read_definitions,
)


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
        "count=0\n"
    )

    found = read_definitions(text)

    assert found.problems == []
    assert found.constants == ["LIST", "MARGIN", "TWO", "JOINED", "PATH_OF"]
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
    # What else stands among the definitions: the preconditions, and no more (the
    # switch's check is the second, S021 Y2c: it reads no cluster).
    assert [problem.split(": ", 1)[1] for problem in found.problems] == [
        "not a definition: need_tools docker kubectl curl jq base64 openssl timeout",
        "not a definition: identity_switch_check",
        "not a definition: require_local_docker",
        "not a definition: need_cluster",
    ]


# ── what the review ran through the reader unseen (M3) ───────────────────────
RUNS_AT_SOURCE_TIME = [
    pytest.param(
        "a=1 echo x\n", "a word after the value", id="a command after a global"
    ),
    pytest.param(
        "readonly A=1 echo x\n",
        "a word after the value",
        id="a command after a constant",
    ),
    pytest.param('a="" > /tmp/f\n', "a word after the value", id="a redirection"),
    pytest.param('a="">/tmp/f\n', "a redirection", id="a redirection with no space"),
    pytest.param("a=1 <<<x\n", "a redirection", id="a here-string"),
    pytest.param(
        "f() { :; } && { echo x; }\n",
        "after the closing brace",
        id="a group after a function",
    ),
    pytest.param(
        "f() { :; }; echo x\n",
        "after the closing brace",
        id="a command after a function",
    ),
    pytest.param(
        "f() {\n  :\n}; echo x\n", "after the closing brace", id="a command after }"
    ),
    pytest.param(
        "f() {\n  :\n} && echo x\n", "after the closing brace", id="&& after }"
    ),
    pytest.param(
        "a=1 \\\necho x\n", "a word after the value", id="a continued command"
    ),
    pytest.param("a=1 \\\n  echo x\n", "a word after the value", id="an indented one"),
    pytest.param(
        "readonly X=$(date)\n", "command substitution", id="a constant that runs"
    ),
    pytest.param(
        "readonly X=`date`\n", "command substitution", id="a backtick constant"
    ),
    pytest.param(
        'readonly X="$(date)"\n', "command substitution", id="in double quotes"
    ),
    pytest.param(
        'readonly X="a-`date`"\n', "command substitution", id="backtick quoted"
    ),
    pytest.param("x=$(date)\n", "command substitution", id="a global that runs"),
]


@pytest.mark.parametrize(("text", "says"), RUNS_AT_SOURCE_TIME)
def test_the_reader_reports_what_would_run_when_the_part_is_sourced(
    text: str, says: str
) -> None:
    found = read_definitions(text)

    assert any(says in problem for problem in found.problems), found.problems


def test_a_command_after_a_closing_brace_does_not_hide_the_next_function() -> None:
    found = read_definitions("f() {\n  :\n}; echo x\ng() {\n  :\n}\n")

    assert found.functions == ["f", "g"]
    assert len(found.problems) == 1


@pytest.mark.parametrize(
    "text",
    [
        "readonly P='a $(b) `c` d'\n",
        "readonly M=$((A * 2 / 3))\n",
        "name=1  # a comment, with a word\n",
        "readonly A=1 # c\n",
        "readonly A=( a b )\n",
        "readonly A=(\n  a\n  b\n)\n",
        'readonly A="x \\$(not run)"\n',
        "f() { :; } # note\n",
        'f() { echo "${x}"; }\n',
        "f() { x=${y:-a}; z=${#y}; }\n",
        "f() { { :; }; }\n",
        'f() { echo "a } b"; }\n',
        "readonly A=a\\\nb\n",
    ],
)
def test_the_reader_does_not_report_a_definition_that_runs_nothing(text: str) -> None:
    assert read_definitions(text).problems == []


# ── the entry and the parts, read as files ───────────────────────────────────
def read_files() -> dict[str, Definitions]:
    return {
        path.name: read_definitions(path.read_text(encoding="utf-8"))
        for path in [KIND_DIR / "smoke.sh", *part_files(KIND_DIR)]
    }


def test_the_reader_finds_the_same_names_in_the_files_as_in_the_rebuilt_text() -> None:
    lines = SMOKE_SH.splitlines()
    start = lines.index(COMMON_LINE) + 1
    stop = next(i for i, line in enumerate(lines) if line.startswith("trap "))
    whole = read_definitions("\n".join(lines[start:stop]))

    files = read_files().values()

    # The entry and the parts, each read alone, give the names the rebuilt text
    # gives (105 functions, 106 constants and 24 globals on 2026-10-08): nothing
    # a cut moved is lost from the reader's sight, or read twice.
    assert sorted(n for d in files for n in d.functions) == sorted(whole.functions)
    assert sorted(n for d in files for n in d.constants) == sorted(whole.constants)
    assert sorted(n for d in files for n in d.globals) == sorted(whole.globals)
    assert len(whole.functions) >= 105
    assert len(whole.constants) >= 106
    assert len(whole.globals) >= 24


def test_no_constant_or_global_of_the_entry_or_the_parts_runs_a_command() -> None:
    # The count behind read_definitions' docstring: a `readonly` whose value
    # holds a command substitution would run it when the part is sourced.
    runs = [
        problem
        for found in read_files().values()
        for problem in found.problems
        if "substitution" in problem
    ]

    assert runs == []
