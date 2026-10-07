"""``services_edit`` taken alone, on the shapes the registry's loader would refuse.

The command refuses an anchor, an alias or a merge key in ``services.yaml``
earlier, in the registry's loader; ``services_edit`` composes the text itself,
so it must refuse them by itself, with a fixed text that names the line and
never the agent (S076, T-81). These tests call the function directly.
"""

import pytest
import yaml

from meridian.platform.cli import scaffold, scaffold_services
from meridian.platform.cli.scaffold_services import (
    SERVICES_AGENTS_UNUSABLE,
    SERVICES_NOT_YAML,
    SERVICES_NOT_YAML_AT,
    SERVICES_RUNTIME_TWICE,
    SERVICES_SHARED_NODE,
    ServicesEditError,
    services_edit,
)

NAME = "fraud-review"
CANARY = "CANARY-content-of-the-persons-file"


def lines_of(*lines: str) -> str:
    return "\n".join([*lines, ""])


PLAIN = lines_of(
    "# the platform's services, with an & and a << in a comment",
    "services:",
    "  - id: claims-api",
    "    description: Takes a claim & more, even '<<'.",
    "    calls: [agent-runtime]",
    "    tenants: [claims-triage]",
    "    agents: [claims-triage]",
    "  - id: agent-runtime",
    "    description: Runs the agent graphs.",
    "    calls: []",
    "    tenants: [claims-triage]",
    "    agents: [claims-triage]",
)
A_SHARED_LIST = lines_of(
    "services:",
    "  - id: claims-api",
    "    calls: [agent-runtime]",
    "    agents: &shared [claims-triage]",
    "  - id: agent-runtime",
    "    calls: []",
    "    agents: *shared",
)
A_MERGE_KEY_AFTER_AGENTS = lines_of(
    "services:",
    "  - &rt",
    "    id: agent-runtime",
    "    calls: []",
    "    agents: [claims-triage]",
    "  - id: claims-api",
    "    agents: [claims-triage]",
    "    <<: *rt",
)
A_MERGE_KEY_THAT_SUPPLIES_AGENTS = lines_of(
    "base: &b {calls: [], agents: [claims-triage]}",
    "services:",
    "  - id: agent-runtime",
    "    <<: *b",
)
A_MERGE_KEY_WITH_NO_ANCHOR = lines_of(
    "services:",
    "  - id: agent-runtime",
    "    calls: []",
    "    <<: {agents: [claims-triage]}",
)
AN_ANCHOR_NOTHING_USES = lines_of(
    "services:",
    "  - id: claims-api",
    "    agents: &unused [claims-triage]",
    "  - id: agent-runtime",
    "    agents: [claims-triage]",
)


def line_of(text: str, fragment: str) -> int:
    """The 1-based number of the one line of ``text`` that holds ``fragment``."""
    found = [i + 1 for i, line in enumerate(text.split("\n")) if fragment in line]
    assert len(found) == 1, fragment
    return found[0]


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        pytest.param(A_SHARED_LIST, "&shared", id="a shared list"),
        pytest.param(A_MERGE_KEY_AFTER_AGENTS, "- &rt", id="a merge key after agents"),
        pytest.param(
            A_MERGE_KEY_THAT_SUPPLIES_AGENTS, "base: &b", id="a merge key with agents"
        ),
        pytest.param(A_MERGE_KEY_WITH_NO_ANCHOR, "<<:", id="a merge key, no anchor"),
        pytest.param(AN_ANCHOR_NOTHING_USES, "&unused", id="an anchor no alias uses"),
    ],
)
def test_the_edit_refuses_an_anchor_an_alias_or_a_merge_key_with_its_line(
    text: str, fragment: str
) -> None:
    # Arrange
    expected = SERVICES_SHARED_NODE.format(line_of(text, fragment))

    # Act
    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    # Assert
    assert str(refused.value) == expected
    assert NAME not in str(refused.value)
    assert NAME.replace("-", "_") not in str(refused.value)


def test_a_merge_key_supplying_agents_is_not_told_the_agents_key_is_missing() -> None:
    # The entry has no `agents` key of its own: before the shared-node check the
    # edit said so, which sends the person to add a key the merge already gives.
    text = A_MERGE_KEY_THAT_SUPPLIES_AGENTS

    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert "has no `agents` key" not in str(refused.value)


def test_an_anchor_is_named_before_a_merge_key() -> None:
    # The merge key is on line 8, the anchor on line 2 of this text: the anchor is
    # named, and the walk for merge keys is never made over a tree with aliases.
    text = A_MERGE_KEY_AFTER_AGENTS

    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert str(refused.value) == SERVICES_SHARED_NODE.format(2)


def test_an_ampersand_or_double_angle_bracket_in_a_value_or_comment_is_no_anchor() -> (
    None
):
    # Arrange: only the runtime's line, the last `agents`, changes.
    old = "agents: [claims-triage]"
    head, _, tail = PLAIN.rpartition(old)

    # Act
    new = services_edit(PLAIN, NAME)

    # Assert
    assert new == head + f"agents: [claims-triage, {NAME}]" + tail
    assert yaml.safe_load(new)["services"][1]["agents"] == ["claims-triage", NAME]


def test_the_shared_node_text_is_fixed_and_takes_only_a_line() -> None:
    assert SERVICES_SHARED_NODE.count("{}") == 1
    assert SERVICES_SHARED_NODE.replace("{}", "7").count("{") == 0
    assert "line {}" in SERVICES_SHARED_NODE


@pytest.mark.parametrize(
    ("text", "line"),
    [
        pytest.param(f"services:\n  - id: a\n\tbad: {CANARY}\n", 3, id="a tab"),
        pytest.param(f"services:\n  - id: a\x07{CANARY}\n", 2, id="a control char"),
        pytest.param(f"services:\n  - id: [{CANARY}\n  - id: b\n", 3, id="open list"),
    ],
)
def test_a_text_that_is_not_yaml_is_refused_with_its_line_and_none_of_its_content(
    text: str, line: int
) -> None:
    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert str(refused.value) == SERVICES_NOT_YAML_AT.format(line)
    assert CANARY not in str(refused.value)


def test_a_parse_error_that_gives_no_line_is_refused_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Changed on purpose (S076): `compose` is now given the safe loader.
    def fail(text: str, Loader: object = None) -> None:
        raise yaml.YAMLError(CANARY)

    monkeypatch.setattr(scaffold_services.yaml, "compose", fail)

    with pytest.raises(ServicesEditError) as refused:
        services_edit(PLAIN, NAME)

    assert str(refused.value) == SERVICES_NOT_YAML
    assert CANARY not in str(refused.value)


def test_the_text_is_composed_with_the_safe_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The default loader would resolve a python tag; it constructs nothing at
    # compose time, but the scan of the events already uses the safe one.
    real = yaml.compose
    loaders: list[object] = []

    def compose(text: str, Loader: object = yaml.Loader) -> yaml.Node | None:
        loaders.append(Loader)
        return real(text, Loader=Loader)  # type: ignore[arg-type]

    monkeypatch.setattr(scaffold_services.yaml, "compose", compose)

    services_edit(PLAIN, NAME)

    assert loaders == [yaml.SafeLoader]


# A note with a Unicode line separator in it: the parser counts it as a line break,
# an editor does not, and the line a person is told is the editor's.
LINE_SEPARATOR = chr(0x2028)
SEPARATED = f"# a note{LINE_SEPARATOR}# more of the same line\n"


def test_a_line_number_does_not_count_a_unicode_separator_as_a_break() -> None:
    # The error is on line 2 for an editor.
    text = f"a: 1 # note{LINE_SEPARATOR}# more\nb: ]\n"

    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert str(refused.value) == SERVICES_NOT_YAML_AT.format(2)


def test_the_lines_of_two_runtime_entries_are_the_editors_lines() -> None:
    text = (
        SEPARATED
        + "services:\n  - id: agent-runtime\n    agents: [a]\n"
        + "  - id: agent-runtime\n    agents: [b]\n"
    )

    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert str(refused.value) == SERVICES_RUNTIME_TWICE.format("3 and 5")


def test_the_line_of_an_anchor_is_the_editors_line() -> None:
    text = SEPARATED + "services:\n  - id: agent-runtime\n    agents: &x [a]\n"

    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert str(refused.value) == SERVICES_SHARED_NODE.format(4)


def test_the_line_of_a_merge_key_is_the_editors_line() -> None:
    text = SEPARATED + "services:\n  - id: agent-runtime\n    <<: {agents: [a]}\n"

    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert str(refused.value) == SERVICES_SHARED_NODE.format(4)


def test_the_line_of_the_agents_key_is_the_editors_line() -> None:
    # A block list, which the text edit does not extend.
    text = SEPARATED + "services:\n  - id: agent-runtime\n    agents:\n      - a\n"

    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert str(refused.value) == SERVICES_AGENTS_UNUSABLE.format(4)


def test_the_line_of_the_agents_list_of_agents_yaml_is_the_editors_line() -> None:
    text = SEPARATED + "agents:\n  - id: a\n"

    assert scaffold._agents_list_line(text) == 3


def runtime_with(agents: str) -> str:
    return lines_of(
        "services:",
        "  - id: agent-runtime",
        f"    agents: {agents}",
        "    calls: []",
    )


def test_a_flow_list_with_a_unicode_separator_in_an_item_is_extended() -> None:
    # One line for an editor: the parser counts the separator as a break, so its
    # own start and end lines differ, but the list is still extended in place.
    item = f'"a{LINE_SEPARATOR}b"'
    text = runtime_with(f"[{item}, claims-triage]")

    new = services_edit(text, NAME)

    assert new == runtime_with(f"[{item}, claims-triage, {NAME}]")
    expected = yaml.safe_load(text)
    expected["services"][0]["agents"].append(NAME)
    assert yaml.safe_load(new) == expected


def test_a_flow_list_over_two_lines_is_still_refused_with_its_line() -> None:
    text = runtime_with("[claims-triage,\n      other]")

    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert str(refused.value) == SERVICES_AGENTS_UNUSABLE.format(3)


def test_a_flow_list_over_two_lines_with_a_separator_is_still_refused() -> None:
    text = runtime_with(f'["a{LINE_SEPARATOR}b",\n      other]')

    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert str(refused.value) == SERVICES_AGENTS_UNUSABLE.format(3)


def a_mark_at(text: str, index: int) -> yaml.Mark:
    return yaml.Mark("services.yaml", index, 0, 0, None, None)


def test_a_mark_at_the_end_of_a_text_with_a_final_line_feed_is_on_the_last_line() -> (
    None
):
    text = "a: 1\nb: 2\n"

    line = scaffold_services.line_of(text, a_mark_at(text, len(text)))

    assert line == 2


def test_a_mark_inside_the_last_line_and_at_the_end_of_an_unterminated_text() -> None:
    text = "a: 1\nb: 2"

    inside = scaffold_services.line_of(text, a_mark_at(text, len(text) - 1))
    end = scaffold_services.line_of(text, a_mark_at(text, len(text)))

    assert (inside, end) == (2, 2)


def test_a_mark_on_a_line_feed_and_on_the_line_after_it_are_told_apart() -> None:
    text = "a: 1\nb: 2\n"

    on_the_feed = scaffold_services.line_of(text, a_mark_at(text, 4))
    after_it = scaffold_services.line_of(text, a_mark_at(text, 5))

    assert (on_the_feed, after_it) == (1, 2)


def test_a_parse_that_stops_at_the_end_of_a_text_names_the_last_line() -> None:
    text = "services:\n  - id: [a\n"

    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert str(refused.value) == SERVICES_NOT_YAML_AT.format(2)


# Deeper than the parser's recursion allows: `compose` gives up at about half the
# interpreter's limit of 1,000 frames, so this is twice that in brackets.
NESTING = 1_000


def test_a_text_nested_too_deep_to_compose_is_refused_as_not_yaml() -> None:
    text = "[" * NESTING + "]" * NESTING + "\n"

    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert str(refused.value) == SERVICES_NOT_YAML
