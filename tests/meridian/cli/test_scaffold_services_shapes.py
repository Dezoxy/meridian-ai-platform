"""``services_edit`` taken alone, on the shapes the registry's loader would refuse.

The command refuses an anchor, an alias or a merge key in ``services.yaml``
earlier, in the registry's loader; ``services_edit`` composes the text itself,
so it must refuse them by itself, with a fixed text that names the line and
never the agent (S076, T-81). These tests call the function directly.
"""

import pytest
import yaml

from meridian.platform.cli import scaffold_services
from meridian.platform.cli.scaffold_services import (
    SERVICES_NOT_YAML,
    SERVICES_NOT_YAML_AT,
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
    def fail(text: str) -> None:
        raise yaml.YAMLError(CANARY)

    monkeypatch.setattr(scaffold_services.yaml, "compose", fail)

    with pytest.raises(ServicesEditError) as refused:
        services_edit(PLAIN, NAME)

    assert str(refused.value) == SERVICES_NOT_YAML
    assert CANARY not in str(refused.value)
