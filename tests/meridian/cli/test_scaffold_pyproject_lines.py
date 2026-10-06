"""The scaffold's refusals about ``pyproject.toml`` name the line (S076, T-81).

An indented table header is accepted at both entry-point tables, as TOML allows
(the edit finds it and the walk of the table stops at it); a file that is not
TOML is refused with the line the parser gave, as a number alone. Every test
builds a small tree in ``tmp_path`` (the ``root`` fixture).
"""

import tomllib
from pathlib import Path

import pytest

from meridian.platform.cli.scaffold import (
    PYPROJECT_NOT_TOML,
    PYPROJECT_NOT_TOML_AT,
    ScaffoldError,
    plan_workload,
    write_plan,
)

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
