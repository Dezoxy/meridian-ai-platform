"""Where the scaffold ends the entry-points table of ``pyproject.toml`` (S076, T-81).

The edit finds the end of a table by asking the TOML parser, and only for a line
that can be a header: a line that, with a trailing comment and the space around it
removed, starts with ``[`` and ends with ``]``. Every other line costs no parse, so
a long file is not read once per line. The shapes of a file the fourth review tried
are cases here: each is edited correctly or refused with a line that helps, and the
check that the new file parses to the old one plus two entries holds in every case.
"""

import tomllib
from pathlib import Path

import pytest

from meridian.platform.cli import scaffold
from meridian.platform.cli.scaffold import (
    PYPROJECT_HEADER_UNUSABLE,
    ScaffoldError,
    plan_workload,
)

NAME = "fraud-review"
MODULE = "fraud_review"
PYPROJECT = "pyproject.toml"
GRAPHS = "meridian.graphs"
EVALUATIONS = "meridian.evaluations"
GRAPHS_HEADER = f'[project.entry-points."{GRAPHS}"]\n'
OLD_GRAPH_LINE = 'claims-triage = "meridian.workloads.claims_triage.graph:build"\n'
GRAPH_LINE = f'{NAME} = "meridian.workloads.{MODULE}.graph:build"'
EVALUATION_LINE = f'{NAME} = "meridian.workloads.{MODULE}.evaluation:EVALUATION"'
MINIMAL = (
    '[project]\nname = "meridian"\n\n'
    f'[project.entry-points."{GRAPHS}"]\na = "x:y"\n\n'
    f'[project.entry-points."{EVALUATIONS}"]\na = "x:z"'
)


def first_in_graphs(shape: str) -> str:
    """The shape right after the graphs header, before its key."""
    return GRAPHS_HEADER + shape


def after_the_graphs_table(shape: str) -> str:
    """The shape after the last key of the graphs table, before the next table."""
    return OLD_GRAPH_LINE + shape


def entry_points(text: str, group: str) -> dict[str, str]:
    return tomllib.loads(text)["project"]["entry-points"][group]


def edit(root: Path, old: str, replaced: str, new: str, ending: str = "\n") -> str:
    """The plan's text for an ``old`` in which ``replaced`` became ``new``."""
    text = old.replace(replaced, new, 1)
    assert replaced in old and text != old  # the arrangement must change the file
    (root / PYPROJECT).write_bytes(text.replace("\n", ending).encode("utf-8"))
    return plan_workload(root, NAME).changed[PYPROJECT]


EDITED_CORRECTLY = [
    pytest.param(
        first_in_graphs('doc = """\n[x]\ny"""\n'), id="a bracket line in a string"
    ),
    pytest.param(
        first_in_graphs("doc = '''\n[x]\n'''\n"), id="a bracket line in a literal"
    ),
    pytest.param(
        first_in_graphs("doc = '''\n  [x]\n'''\n"), id="an indented one in a literal"
    ),
    pytest.param(
        first_in_graphs('doc = """\n[tool.other]\nx = 1\n"""\n'),
        id="a string holding what looks like another table's header",
    ),
    pytest.param(
        first_in_graphs("many = [\n  [1],\n  [2],\n]\n"), id="an array of arrays"
    ),
    pytest.param(
        first_in_graphs("inline = { a = [1], b = 2 }\n"), id="an inline table"
    ),
    pytest.param(first_in_graphs("a.b = 1\n"), id="a dotted key"),
    pytest.param(
        after_the_graphs_table("[[tool.x]]\nk = 1\n"), id="an array of tables"
    ),
    pytest.param(
        after_the_graphs_table("[tool.x]  # a comment\nk = 1\n"),
        id="a next header with a trailing comment",
    ),
    pytest.param(
        after_the_graphs_table('["tool"."x y"]\nk = 1\n'), id="a quoted next table"
    ),
    pytest.param(
        after_the_graphs_table("[tool.x.y]\nk = 1\n"), id="a dotted next table"
    ),
    pytest.param(
        after_the_graphs_table('["a#b"]\nk = 1\n'), id="a hash inside a quoted header"
    ),
]


@pytest.mark.parametrize("ending", ["\n", "\r\n"], ids=["LF", "CRLF"])
@pytest.mark.parametrize("shape", EDITED_CORRECTLY)
def test_a_file_shape_is_edited_correctly_and_nothing_else_moves(
    root: Path, shape: str, ending: str
) -> None:
    # Arrange: the replacement stands for what a person's file might hold.
    old = (root / PYPROJECT).read_text(encoding="utf-8")
    replaced = GRAPHS_HEADER if shape.startswith(GRAPHS_HEADER) else OLD_GRAPH_LINE

    # Act
    new = edit(root, old, replaced, shape, ending)

    # Assert: two lines were added, each in its own table, and no other line moved.
    assert [
        line for line in new.splitlines() if line not in (GRAPH_LINE, EVALUATION_LINE)
    ] == old.replace(replaced, shape, 1).splitlines()
    assert entry_points(new, GRAPHS)[NAME].endswith("graph:build")
    assert entry_points(new, EVALUATIONS)[NAME].endswith("evaluation:EVALUATION")
    assert new.count("\r\n") == (new.count("\n") if ending == "\r\n" else 0)


@pytest.mark.parametrize("ending", ["\n", "\r\n"], ids=["LF", "CRLF"])
def test_a_table_last_in_the_file_with_no_final_newline_gets_one(
    root: Path, ending: str
) -> None:
    # Arrange: the evaluations table is last and its last line has no line ending.
    old = MINIMAL.replace("\n", ending)

    # Act
    (root / PYPROJECT).write_bytes(old.encode("utf-8"))
    new = plan_workload(root, NAME).changed[PYPROJECT]

    # Assert: the line ending is the file's, and the inserted line, now last, has none.
    assert new.endswith(f'a = "x:z"{ending}{EVALUATION_LINE}')
    assert entry_points(new, GRAPHS) == {"a": "x:y", NAME: GRAPH_LINE.split('"')[1]}
    assert NAME in entry_points(new, EVALUATIONS)


def test_a_header_followed_by_a_comment_is_refused_naming_its_line(root: Path) -> None:
    # Arrange: the scaffold needs the header alone on its line.
    old = (root / PYPROJECT).read_text(encoding="utf-8")
    text = old.replace(GRAPHS_HEADER, GRAPHS_HEADER.rstrip("\n") + "  # a comment\n")
    (root / PYPROJECT).write_text(text, encoding="utf-8")
    line = old[: old.index(GRAPHS_HEADER)].count("\n") + 1

    # Act
    with pytest.raises(ScaffoldError) as refused:
        plan_workload(root, NAME)

    # Assert
    assert str(refused.value) == PYPROJECT_HEADER_UNUSABLE.format(
        GRAPHS_HEADER.strip(), line
    )


@pytest.mark.parametrize(
    ("line", "candidate"),
    [
        pytest.param("[x]\n", True, id="a header"),
        pytest.param("  [x]  \n", True, id="an indented one"),
        pytest.param("\t[[x]]\r\n", True, id="an array of tables, CRLF"),
        pytest.param("[x]  # a comment\n", True, id="a trailing comment"),
        pytest.param("[x] # a # b\n", True, id="a comment with a hash in it"),
        pytest.param('["a#b"]\n', True, id="a hash in a quoted key"),
        pytest.param("[x]", True, id="no line ending"),
        pytest.param("  [1],\n", False, id="an array element"),
        pytest.param("x = [1]\n", False, id="a key with an array"),
        pytest.param("[1, 2,\n", False, id="the start of an array"),
        pytest.param("# [x]\n", False, id="a comment"),
        pytest.param("\n", False, id="a blank line"),
        pytest.param("[x] y\n", False, id="text after the bracket"),
    ],
)
def test_only_a_line_that_can_be_a_header_is_a_candidate(
    line: str, candidate: bool
) -> None:
    assert scaffold._could_be_a_header(line) is candidate


def counted_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, bool]]:
    """Record each text ``scaffold`` parses and whether it parsed."""
    calls: list[tuple[str, bool]] = []
    real = scaffold._parsed

    def parsed(text: str) -> object:
        result = real(text)
        calls.append((text, result is not None))
        return result

    monkeypatch.setattr(scaffold, "_parsed", parsed)
    return calls


def test_a_line_that_cannot_be_a_header_costs_no_parse(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: thousands of comment lines and of array elements in the table. Not a
    # timing: the number of parses is what used to grow with the lines.
    filler = "# a comment\n" * 3000 + "nested = [\n" + "  [1],\n" * 3000 + "]\n"
    old = (root / PYPROJECT).read_text(encoding="utf-8")
    (root / PYPROJECT).write_text(
        old.replace(GRAPHS_HEADER, GRAPHS_HEADER + filler), encoding="utf-8"
    )
    calls = counted_parses(monkeypatch)

    # Act
    plan = plan_workload(root, NAME)

    # Assert: the next header of each table and the two checks, and nothing more.
    assert NAME in entry_points(plan.changed[PYPROJECT], GRAPHS)
    assert len(calls) <= 6


def test_a_header_inside_a_string_is_a_candidate_and_the_prefix_parse_rejects_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: a line of a multi-line string that also ends with "]".
    shape = first_in_graphs('doc = """\n[tool.other]\nx = 1\n"""\n')
    old = (root / PYPROJECT).read_text(encoding="utf-8")
    (root / PYPROJECT).write_text(old.replace(GRAPHS_HEADER, shape), encoding="utf-8")
    calls = counted_parses(monkeypatch)

    # Act
    plan = plan_workload(root, NAME)

    # Assert: that line was parsed as a candidate and refused as a header, and the
    # entry still landed in its own table.
    rejected = [text for text, parsed in calls if not parsed]
    assert len(rejected) == 1
    assert rejected[0].endswith('doc = """\n')
    assert NAME in entry_points(plan.changed[PYPROJECT], GRAPHS)
    assert NAME in entry_points(plan.changed[PYPROJECT], EVALUATIONS)
