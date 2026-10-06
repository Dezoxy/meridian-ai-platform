"""The scaffold's refusals about ``pyproject.toml`` name the line (S076, T-81).

An indented table header is accepted at both entry-point tables, as TOML allows
(the edit finds it and the walk of the table stops at it); a file that is not
TOML is refused with the line the parser gave, as a number alone. Every test
builds a small tree in ``tmp_path`` (the ``root`` fixture).
"""

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli.scaffold import (
    PYPROJECT_NOT_TOML,
    PYPROJECT_NOT_TOML_AT,
    PYPROJECT_TABLE_UNVERIFIED,
    ScaffoldError,
    plan_workload,
    write_plan,
)

runner = CliRunner()
EXIT_REFUSED = 2
NAME = "fraud-review"
MODULE = "fraud_review"
PYPROJECT = "pyproject.toml"
GRAPHS = "meridian.graphs"
EVALUATIONS = "meridian.evaluations"
CANARY = "CANARY-content-of-the-persons-file"


def indent_header(root: Path, group: str, indent: str) -> None:
    header = f'[project.entry-points."{group}"]'
    text = (root / PYPROJECT).read_bytes().decode("utf-8")
    assert text.count("\n" + header) == 1  # the arrangement must find its header
    (root / PYPROJECT).write_bytes(
        text.replace("\n" + header, "\n" + indent + header).encode("utf-8")
    )


def entry_points(text: str, group: str) -> dict[str, str]:
    return tomllib.loads(text)["project"]["entry-points"][group]


@pytest.mark.parametrize("indent", ["  ", "\t"], ids=["spaces", "a tab"])
@pytest.mark.parametrize(
    "indented",
    [(GRAPHS,), (EVALUATIONS,), (GRAPHS, EVALUATIONS)],
    ids=["graphs", "evaluations", "both"],
)
def test_an_indented_table_header_is_accepted_and_the_entry_lands_in_its_table(
    root: Path, indented: tuple[str, ...], indent: str
) -> None:
    # Arrange
    old = (root / PYPROJECT).read_text(encoding="utf-8")
    for group in indented:
        indent_header(root, group, indent)

    # Act
    plan = plan_workload(root, NAME)

    # Assert
    new = plan.changed[PYPROJECT]
    assert entry_points(new, GRAPHS) == {
        **entry_points(old, GRAPHS),
        NAME: f"meridian.workloads.{MODULE}.graph:build",
    }
    assert entry_points(new, EVALUATIONS) == {
        **entry_points(old, EVALUATIONS),
        NAME: f"meridian.workloads.{MODULE}.evaluation:EVALUATION",
    }


def test_the_plan_with_an_indented_header_is_written_and_parses(root: Path) -> None:
    indent_header(root, EVALUATIONS, "    ")
    plan = plan_workload(root, NAME)

    write_plan(root, plan)

    written = (root / PYPROJECT).read_text(encoding="utf-8")
    assert written == plan.changed[PYPROJECT]
    assert NAME in entry_points(written, EVALUATIONS)


@pytest.mark.parametrize(
    ("text", "line"),
    [
        pytest.param(f"[project\nname = {CANARY}\n", 1, id="a table declaration"),
        pytest.param(f'a = 1\nb = "{CANARY}\n', 2, id="a line break in a string"),
        pytest.param(f"a = 1\na = {CANARY}\n", 2, id="an illegal value"),
    ],
)
def test_a_pyproject_that_is_not_toml_is_refused_with_the_line_and_nothing_else(
    root: Path, text: str, line: int
) -> None:
    (root / PYPROJECT).write_bytes(text.encode("utf-8"))

    with pytest.raises(ScaffoldError) as refused:
        plan_workload(root, NAME)

    assert str(refused.value) == PYPROJECT_NOT_TOML_AT.format(line)
    assert CANARY not in str(refused.value)


@pytest.mark.parametrize(
    "content",
    [b"x = [1, 2", b'a = """abc', b"\xff\xfe"],
    ids=["an unclosed array", "an unterminated string", "not utf-8"],
)
def test_a_pyproject_error_with_no_line_to_give_is_refused_without_one(
    root: Path, content: bytes
) -> None:
    # TOML reports "at end of document" for the first two: there is no line.
    (root / PYPROJECT).write_bytes(content)

    with pytest.raises(ScaffoldError) as refused:
        plan_workload(root, NAME)

    assert str(refused.value) == PYPROJECT_NOT_TOML


def test_a_pyproject_nested_too_deeply_is_refused_as_not_toml_with_no_traceback(
    root: Path,
) -> None:
    # The parser's recursion error is not a decode error, and gave no line.
    deep = "a = " + "[" * 5000 + "]" * 5000 + "\n"
    old = (root / PYPROJECT).read_text(encoding="utf-8")
    (root / PYPROJECT).write_text(old + deep, encoding="utf-8")

    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(root)])

    assert result.exit_code == EXIT_REFUSED
    assert result.stderr == f"ERROR {PYPROJECT_NOT_TOML}\n"
    assert not isinstance(result.exception, RecursionError)


NESTED_ARRAY = 'nested = [\n  ["a", "b"],\n  ["c"],\n]\n'
BRACKET_IN_A_STRING = 'doc = """\n  [x]\n  y"""\n'
ARRAY_OF_STRINGS = 'many = [\n  "a",\n  "b",\n]\ninline = { x = 1 }\n'


@pytest.mark.parametrize(
    "shape",
    [NESTED_ARRAY, BRACKET_IN_A_STRING, ARRAY_OF_STRINGS],
    ids=["a nested array", "a bracket line in a string", "an array of strings"],
)
def test_a_table_ends_where_the_document_says_not_at_a_line_that_looks_like_a_header(
    root: Path, shape: str
) -> None:
    # Arrange: the shape sits first in the graphs table, before the existing key.
    header = f'[project.entry-points."{GRAPHS}"]\n'
    old = (root / PYPROJECT).read_text(encoding="utf-8")
    old = old.replace(header, header + shape)
    (root / PYPROJECT).write_text(old, encoding="utf-8")

    # Act
    plan = plan_workload(root, NAME)

    # Assert: the entry lands in its table and nothing else of the file moves.
    new = plan.changed[PYPROJECT]
    graph = f'{NAME} = "meridian.workloads.{MODULE}.graph:build"\n'
    evaluation = f'{NAME} = "meridian.workloads.{MODULE}.evaluation:EVALUATION"\n'
    assert new.replace(graph, "", 1).replace(evaluation, "", 1) == old
    assert NAME in entry_points(new, GRAPHS)
    assert NAME in entry_points(new, EVALUATIONS)


def test_an_edit_that_does_not_verify_names_the_header_of_the_table(
    root: Path,
) -> None:
    # Arrange: the string holds a copy of the header, which the edit takes for the
    # real one (the real header is written with a space, so it is not found).
    old = (root / PYPROJECT).read_text(encoding="utf-8")
    header = f'[project.entry-points."{GRAPHS}"]'
    text = old.replace(
        header + "\n",
        'x = """\n' + header + '\n"""\n[ project.entry-points."' + GRAPHS + '" ]\n',
    )
    (root / PYPROJECT).write_text(text, encoding="utf-8")
    line = text.splitlines().index(header) + 1

    # Act
    with pytest.raises(ScaffoldError) as refused:
        plan_workload(root, NAME)

    # Assert
    assert str(refused.value) == PYPROJECT_TABLE_UNVERIFIED.format(line)


def test_a_parser_text_that_ends_like_a_position_is_not_read_as_one(
    root: Path,
) -> None:
    # The key is quoted in the parser's own text: "Cannot declare ('a (at line 9,
    # column 9)',) twice (at line 2, column 27)". Only the position at the very
    # end is the parser's own; a number in the file's content is never printed.
    key = '"a (at line 9, column 9)"'
    text = f"[{key}]\n[{key}]\n"
    (root / PYPROJECT).write_bytes(text.encode("utf-8"))

    with pytest.raises(ScaffoldError) as refused:
        plan_workload(root, NAME)

    assert str(refused.value) == PYPROJECT_NOT_TOML_AT.format(2)
