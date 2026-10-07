"""What the workload scaffold's refusals say (S061).

A refusal names the kind of thing that holds a taken name, or the line of the
file it cannot edit, and never quotes the workload's name or a path that
contains it. Every test builds a small tree in ``tmp_path``: a copy of this
repository's ``pyproject.toml`` and registry and an empty workloads directory.
"""

import json
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from meridian.platform.cli import scaffold
from meridian.platform.cli.scaffold import (
    AGENTS_EDIT_UNVERIFIED,
    AGENTS_NO_FINAL_NEWLINE,
    PYPROJECT_HEADER_UNUSABLE,
    PYPROJECT_TABLE_UNVERIFIED,
    ScaffoldError,
    plan_workload,
)
from meridian.platform.registry.loader import load_registry

NAME = "fraud-review"
MODULE = "fraud_review"
GRAPHS = "meridian.graphs"
EVALUATIONS = "meridian.evaluations"
AGENT_ONLY = "knowledge-ingestion"
AGENTS_FILE = "config/registry/agents.yaml"
PREFIX = "the name is taken: held by "


def edit(path: Path, change: Callable[[str], str]) -> None:
    text = path.read_bytes().decode("utf-8")
    path.write_bytes(change(text).encode("utf-8"))


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"))
    }


def refusal(root: Path, name: str = NAME) -> str:
    with pytest.raises(ScaffoldError) as refused:
        plan_workload(root, name)
    return str(refused.value)


def line_of(root: Path, relative: str, wanted: str) -> int:
    """The 1-based number of the first line of the file that is ``wanted``."""
    lines = (root / relative).read_text(encoding="utf-8").split("\n")
    return lines.index(wanted) + 1


def occupy_directory(path: Path) -> None:
    path.mkdir(parents=True)


def occupy_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def publish(root: Path, group: str, name: str = NAME) -> None:
    header = f'[project.entry-points."{group}"]\n'
    edit(
        root / "pyproject.toml",
        lambda t: t.replace(header, header + f'{name} = "x.y:z"\n'),
    )


def test_a_name_an_agent_holds_is_refused_naming_the_agent(root: Path) -> None:
    before = snapshot(root)

    message = refusal(root, AGENT_ONLY)

    assert message == PREFIX + "an agent of the registry"
    assert AGENT_ONLY not in message
    assert snapshot(root) == before


@pytest.mark.parametrize(
    ("group", "holder"),
    [
        (GRAPHS, "an entry point of the meridian.graphs group"),
        (EVALUATIONS, "an entry point of the meridian.evaluations group"),
    ],
)
def test_a_name_an_entry_point_holds_is_refused_naming_its_group(
    root: Path, group: str, holder: str
) -> None:
    publish(root, group)

    message = refusal(root)

    assert message == PREFIX + holder
    assert NAME not in message


@pytest.mark.parametrize(
    ("taken", "occupy", "holder"),
    [
        (
            f"src/meridian/workloads/{MODULE}",
            occupy_directory,
            "the workload's package",
        ),
        (
            f"src/meridian/workloads/{MODULE}.py",
            occupy_file,
            "the workload's module file",
        ),
        (
            f"tests/meridian/workloads/{MODULE}",
            occupy_directory,
            "the workload's tests",
        ),
        (
            f"data/evaluation/{NAME}",
            occupy_directory,
            "the workload's evaluation data",
        ),
    ],
)
def test_a_name_a_path_holds_is_refused_naming_the_kind_of_path(
    root: Path, taken: str, occupy: Callable[[Path], None], holder: str
) -> None:
    occupy(root / taken)

    message = refusal(root)

    assert message == PREFIX + holder
    assert NAME not in message
    assert MODULE not in message
    assert "src/meridian" not in message


def test_a_name_held_by_an_agent_and_both_entry_points_names_each(root: Path) -> None:
    message = refusal(root, "claims-triage")

    assert message == (
        PREFIX
        + "an agent of the registry, an entry point of the meridian.graphs group "
        + "and an entry point of the meridian.evaluations group"
    )


def test_a_name_held_by_an_agent_and_by_two_paths_names_each(root: Path) -> None:
    occupy_directory(root / "data/evaluation" / AGENT_ONLY)
    occupy_file(root / "src/meridian/workloads/knowledge_ingestion.py")

    message = refusal(root, AGENT_ONLY)

    assert message == (
        PREFIX
        + "an agent of the registry, the workload's module file "
        + "and the workload's evaluation data"
    )
    assert AGENT_ONLY not in message
    assert "knowledge_ingestion" not in message


def table_header(group: str) -> str:
    return f'[project.entry-points."{group}"]'


@pytest.mark.parametrize("group", [GRAPHS, EVALUATIONS])
def test_a_table_header_with_a_trailing_comment_is_refused_with_its_line(
    root: Path, group: str
) -> None:
    header = table_header(group)
    line = line_of(root, "pyproject.toml", header)
    edit(root / "pyproject.toml", lambda t: t.replace(header, header + "  # note"))
    before = snapshot(root)

    message = refusal(root)

    assert message == (
        "the edit of pyproject.toml does not verify: the scaffold needs the table "
        f"header {header} alone on its line, and once; lines that start with it: "
        f"{line}; nothing was written"
    )
    assert snapshot(root) == before


def test_a_table_header_seen_twice_is_refused_with_both_lines(root: Path) -> None:
    header = table_header(GRAPHS)
    first = line_of(root, "pyproject.toml", header)
    edit(
        root / "pyproject.toml",
        lambda t: t + f'\n[tool.scaffold-test]\nnote = """\n{header}\n"""\n',
    )
    lines = (root / "pyproject.toml").read_text(encoding="utf-8").split("\n")
    second = len(lines) - lines[::-1].index(header)
    assert second != first

    message = refusal(root)

    assert message == (
        "the edit of pyproject.toml does not verify: the scaffold needs the table "
        f"header {header} alone on its line, and once; lines that start with it: "
        f"{first} and {second}; nothing was written"
    )


def test_an_agents_list_in_flow_style_is_refused_with_its_line(root: Path) -> None:
    path = root / AGENTS_FILE
    text = path.read_text(encoding="utf-8")
    head = text.split("agents:")[0]
    agents = yaml.safe_load(text)["agents"]
    path.write_text(head + "agents: " + json.dumps(agents) + "\n", encoding="utf-8")
    line = len(head.split("\n"))
    assert line == 6
    before = snapshot(root)

    message = refusal(root)

    assert message == (
        f"the edit of agents.yaml does not verify: the `agents` list starts on line "
        f"{line}; the text edit extends only a list in block style with `agents` "
        "last in the file; nothing was written"
    )
    assert snapshot(root) == before


# An agent of its own after every agent the registry holds, whatever they are:
# the test does not name the agent that is last in the committed file.
LAST_AGENT = "  - id: block-scalar-agent\n"
# The last agent's text ends the file inside a block scalar.
BLOCK_SCALAR_TAIL = (
    LAST_AGENT + "    kind: job\n    tools: []\n    structured_outputs: true\n"
    "    description: |\n      Grades whether a rationale is grounded"
)


def end_in_a_block_scalar(root: Path, final_newline: bool) -> None:
    path = root / AGENTS_FILE
    head = path.read_text(encoding="utf-8")
    tail = BLOCK_SCALAR_TAIL + ("\n" if final_newline else "")
    path.write_text(head + tail, encoding="utf-8")


def test_an_edit_that_changes_the_last_agent_is_refused_by_the_comparison_alone(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    end_in_a_block_scalar(root, final_newline=False)
    assert load_registry(root / "config/registry").agent("block-scalar-agent")
    before = snapshot(root)

    def never(*args: Any, **kwargs: Any) -> None:
        pytest.fail("the registry check ran after the comparison refused the edit")

    monkeypatch.setattr(scaffold, "_registry_with", never)

    message = refusal(root)

    assert message == AGENTS_NO_FINAL_NEWLINE
    assert "newline" in message
    assert snapshot(root) == before


def test_a_comparison_that_fails_with_a_final_newline_keeps_the_old_text() -> None:
    # A key after the list takes the appended entry for one of its own items:
    # the parse succeeds and the comparison fails, whatever the file ends with.
    old = "agents: []\nother:\n  - x\n"

    with pytest.raises(ScaffoldError) as refused:
        scaffold._agents_edit(old, NAME)

    assert str(refused.value) == AGENTS_EDIT_UNVERIFIED
    assert "block style" in AGENTS_EDIT_UNVERIFIED
    assert "newline" not in AGENTS_EDIT_UNVERIFIED


def test_the_same_comparison_without_a_final_newline_is_not_blamed_on_the_newline() -> (
    None
):
    # Ending the file with a newline would not change the value before the list.
    old = "agents: []\nother:\n  - x"

    with pytest.raises(ScaffoldError) as refused:
        scaffold._agents_edit(old, NAME)

    assert str(refused.value) == AGENTS_EDIT_UNVERIFIED


# More brackets than the parser's recursion allows (it gives up at about half the
# interpreter's limit of 1,000 frames).
TOO_DEEP = "[" * 1_000 + "]" * 1_000


def test_an_agents_file_nested_too_deep_to_parse_is_refused_not_raised() -> None:
    old = f"agents:\n  - id: a\n    tools: {TOO_DEEP}\n"

    with pytest.raises(ScaffoldError) as refused:
        scaffold._agents_edit(old, NAME)

    assert str(refused.value) == AGENTS_EDIT_UNVERIFIED


def test_the_line_of_an_agents_list_nested_too_deep_to_compose_is_none() -> None:
    old = f"agents:\n  - id: a\n    tools: {TOO_DEEP}\n"

    assert scaffold._agents_list_line(old) is None


def test_the_same_block_scalar_with_a_final_newline_is_accepted(root: Path) -> None:
    end_in_a_block_scalar(root, final_newline=True)
    agents_before = len(load_registry(root / "config/registry").agents)

    plan = plan_workload(root, NAME)

    new = yaml.safe_load(plan.changed[AGENTS_FILE])["agents"]
    assert len(new) == agents_before + 1
    assert new[-2]["description"] == "Grades whether a rationale is grounded\n"


TOML_WITH_A_HEADER_IN_A_STRING = (
    '[project]\nname = "meridian"\ndescription = """\n'
    '[project.entry-points."meridian.graphs"]\n"""\n\n'
    '[ project.entry-points."meridian.graphs" ]\nclaims-triage = "a.b:c"\n\n'
    '[project.entry-points."meridian.evaluations"]\nclaims-triage = "d.e:f"\n'
)
# The first header is the real one, so an edit that took it would be right: only
# the refusal of a header seen twice stops this one.
TOML_WITH_A_HEADER_TWICE = (
    '[project]\nname = "meridian"\n\n'
    '[project.entry-points."meridian.graphs"]\nclaims-triage = "a.b:c"\n\n'
    '[project.entry-points."meridian.evaluations"]\nclaims-triage = "d.e:f"\n\n'
    '[tool.x]\nnote = """\n[project.entry-points."meridian.graphs"]\n"""\n'
)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        # Changed on purpose (S076): the refusal names the table, by its header's
        # line (4, inside the string), instead of saying only "does not verify".
        (TOML_WITH_A_HEADER_IN_A_STRING, PYPROJECT_TABLE_UNVERIFIED.format(4)),
        (
            TOML_WITH_A_HEADER_TWICE,
            # The header is on lines 4 and 12 of the text.
            PYPROJECT_HEADER_UNUSABLE.format(
                '[project.entry-points."meridian.graphs"]', "4 and 12"
            ),
        ),
    ],
    ids=["insertion-lands-in-the-string", "header-seen-twice"],
)
def test_a_header_line_inside_a_string_fails_the_pyproject_edit(
    root: Path, text: str, message: str
) -> None:
    (root / "pyproject.toml").write_text(text, encoding="utf-8")
    before = snapshot(root)

    with pytest.raises(ScaffoldError) as refused:
        plan_workload(root, NAME)

    assert str(refused.value) == message
    assert snapshot(root) == before


def test_the_trees_with_a_header_line_in_a_string_are_valid_toml() -> None:
    for text in (TOML_WITH_A_HEADER_IN_A_STRING, TOML_WITH_A_HEADER_TWICE):
        document = tomllib.loads(text)
        assert document["project"]["entry-points"][GRAPHS] == {"claims-triage": "a.b:c"}
