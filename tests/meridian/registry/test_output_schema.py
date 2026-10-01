"""A tool's ``output_schema`` is held to the rules of its input schema (S013)."""

from collections.abc import Callable
from pathlib import Path

import pytest

from meridian.platform.registry import load_registry

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]
Edit = tuple[str, str, str]

UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
NOTE_ID = (
    f'        note_id: {{type: string, pattern: "{UUID_PATTERN}", maxLength: 36}}\n'
)
ENTRIES = "        entries:\n          type: array\n          maxItems: 100\n"
NOTE_ROOT_END = (
    "      required: [note_id, replayed]\n      additionalProperties: false\n"
)

TOOLS_WITH_AN_OUTPUT_SCHEMA = (
    "policy_lookup",
    "claim_history",
    "add_claim_note",
    "request_approval",
)


def note(path: str, problem: str) -> str:
    return (
        f"tools.yaml: tools[3].output_schema{path}: {problem} (tool 'add_claim_note')"
    )


BAD_OUTPUT_SCHEMAS = [
    pytest.param(
        ("tools.yaml", NOTE_ROOT_END, "      required: [note_id, replayed]\n"),
        note("", "object must set additionalProperties: false"),
        id="root-open-because-absent",
    ),
    pytest.param(
        ("tools.yaml", NOTE_ID, "        note_id: {type: string}\n"),
        note(".properties.note_id", "string needs maxLength or enum"),
        id="unbounded-string",
    ),
    pytest.param(
        ("tools.yaml", NOTE_ID, '        note_id: {"$ref": "#/defs/id"}\n'),
        note(".properties.note_id.$ref", "keyword is not allowed"),
        id="ref",
    ),
    pytest.param(
        ("tools.yaml", NOTE_ID, "        note_id: {anyOf: [{type: string}]}\n"),
        note(".properties.note_id.anyOf", "keyword is not allowed"),
        id="anyof",
    ),
    pytest.param(
        (
            "tools.yaml",
            NOTE_ROOT_END,
            "      required: [note_id, replayed, ghost]\n"
            "      additionalProperties: false\n",
        ),
        note(".required", "'ghost' is not in properties"),
        id="required-names-unknown-property",
    ),
    pytest.param(
        (
            "tools.yaml",
            "    output_schema:\n      type: object\n",
            ("    output_schema:\n      type: array\n"),
        ),
        "tools.yaml: tools[0].output_schema.type: must be 'object' "
        "(tool 'policy_lookup')",
        id="root-not-an-object",
    ),
]


@pytest.mark.parametrize(("edit", "expected"), BAD_OUTPUT_SCHEMAS)
def test_output_schema_violation_is_reported_under_its_own_path(
    plant: Plant, load_errors: LoadErrors, edit: Edit, expected: str
) -> None:
    errors = load_errors(plant(edit))

    assert expected in errors


def test_an_open_nested_object_in_an_output_schema_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "tools.yaml",
            NOTE_ID,
            NOTE_ID + "        extra: {type: object, properties: {}}\n",
        )
    )

    errors = load_errors(directory)

    assert (
        note(".properties.extra", "object must set additionalProperties: false")
        in errors
    )


def test_an_array_in_an_output_schema_is_capped_at_one_hundred_items(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        ("tools.yaml", ENTRIES, ENTRIES.replace("maxItems: 100", "maxItems: 101"))
    )

    errors = load_errors(directory)

    assert (
        "tools.yaml: tools[1].output_schema.properties.entries.maxItems: must be an "
        "integer from 1 to 100 (tool 'claim_history')"
    ) in errors


def test_every_error_in_an_output_schema_starts_with_output_schema(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        ("tools.yaml", NOTE_ID, "        note_id: {type: string}\n"),
        ("tools.yaml", NOTE_ROOT_END, "      required: [note_id, replayed]\n"),
    )

    errors = [e for e in load_errors(directory) if "(tool 'add_claim_note')" in e]

    assert len(errors) == 2
    assert all(".output_schema" in e for e in errors)
    assert not any(".input_schema" in e for e in errors)


def test_the_four_tools_with_a_server_publish_a_clean_output_schema(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)

    for tool_id in TOOLS_WITH_AN_OUTPUT_SCHEMA:
        tool = registry.tool(tool_id)
        assert tool is not None, tool_id
        assert tool.output_schema is not None, tool_id
        assert tool.output_schema["type"] == "object", tool_id
        assert tool.output_schema["additionalProperties"] is False, tool_id


def test_wording_search_has_no_output_schema_until_its_server_exists(
    real_registry: Path,
) -> None:
    tool = load_registry(real_registry).tool("wording_search")

    assert tool is not None
    assert tool.output_schema is None


def test_the_output_schemas_name_the_agreed_fields(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    def schema(tool_id: str) -> dict:
        tool = registry.tool(tool_id)
        assert tool is not None
        assert tool.output_schema is not None
        return tool.output_schema

    lookup = schema("policy_lookup")
    policy = lookup["properties"]["policy"]
    history = schema("claim_history")
    entry = history["properties"]["entries"]["items"]

    assert lookup["required"] == ["found"]
    assert set(lookup["properties"]) == {"found", "policy"}
    assert set(policy["required"]) == set(policy["properties"]) - {
        "lapsed_on",
        "sum_insured",
    }
    assert set(policy["properties"]) == {
        "policy_number",
        "product",
        "wording_version",
        "start_date",
        "end_date",
        "status",
        "lapsed_on",
        "deductible",
        "sum_insured",
        "limit",
    }
    assert policy["properties"]["status"]["enum"] == ["active", "lapsed"]
    assert history["required"] == ["entries", "truncated"]
    assert history["properties"]["entries"]["maxItems"] == 100
    assert (
        set(entry["required"])
        == set(entry["properties"])
        == {
            "history_id",
            "loss_date",
            "peril",
            "paid_amount",
            "status",
        }
    )
    for tool_id, key in [
        ("add_claim_note", "note_id"),
        ("request_approval", "request_id"),
    ]:
        written = schema(tool_id)
        assert written["required"] == [key, "replayed"]
        assert written["properties"][key]["maxLength"] == 36
        assert written["properties"]["replayed"] == {"type": "boolean"}
