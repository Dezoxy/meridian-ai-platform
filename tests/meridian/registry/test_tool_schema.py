"""Structural rules for a tool's input_schema (hard rule 6)."""

from collections.abc import Callable
from pathlib import Path

import pytest

from meridian.platform.registry import load_registry

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]
Edit = tuple[str, str, str]

TOP_K = "        top_k: {type: integer, minimum: 1, maximum: 10}\n"
QUERY = "        query: {type: string, minLength: 1, maxLength: 500}\n"
NOTE = "        note: {type: string, minLength: 1, maxLength: 2000}\n"


def wording(path: str, problem: str) -> str:
    return f"tools.yaml: tools[2].input_schema{path}: {problem} (tool 'wording_search')"


def policy(path: str, problem: str) -> str:
    return f"tools.yaml: tools[0].input_schema{path}: {problem} (tool 'policy_lookup')"


BAD_SCHEMAS = [
    pytest.param(
        ("tools.yaml", "      additionalProperties: false\n", ""),
        policy("", "object must set additionalProperties: false"),
        id="root-open-because-absent",
    ),
    pytest.param(
        ("tools.yaml", "additionalProperties: false", "additionalProperties: true"),
        policy("", "object must set additionalProperties: false"),
        id="root-open-because-true",
    ),
    pytest.param(
        ("tools.yaml", "      type: object\n", "      type: array\n"),
        policy(".type", "must be 'object'"),
        id="root-not-an-object",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            TOP_K + "        nested: {type: object, properties: {}}\n",
        ),
        wording(".properties.nested", "object must set additionalProperties: false"),
        id="nested-open-object",
    ),
    pytest.param(
        ("tools.yaml", TOP_K, '        top_k: {"$ref": "#/defs/k"}\n'),
        wording(".properties.top_k.$ref", "keyword is not allowed"),
        id="ref",
    ),
    pytest.param(
        ("tools.yaml", TOP_K, "        top_k: {anyOf: [{type: integer}]}\n"),
        wording(".properties.top_k.anyOf", "keyword is not allowed"),
        id="anyof",
    ),
    pytest.param(
        ("tools.yaml", TOP_K, "        top_k: {minimum: 1, maximum: 10}\n"),
        wording(
            ".properties.top_k.type",
            "must be one of "
            "['array', 'boolean', 'integer', 'number', 'object', 'string']",
        ),
        id="missing-type",
    ),
    pytest.param(
        ("tools.yaml", QUERY, "        query: {type: string, minLength: 1}\n"),
        wording(".properties.query", "string needs maxLength or enum"),
        id="unbounded-string",
    ),
    pytest.param(
        ("tools.yaml", TOP_K, "        top_k: {type: integer, minimum: 1}\n"),
        wording(".properties.top_k.maximum", "integer needs a maximum"),
        id="unbounded-integer",
    ),
    pytest.param(
        ("tools.yaml", TOP_K, "        top_k: {type: number, maximum: 1}\n"),
        wording(".properties.top_k.minimum", "number needs a minimum"),
        id="unbounded-number",
    ),
    pytest.param(
        (
            "tools.yaml",
            "required: [query, product]",
            "required: [query, product, ghost]",
        ),
        wording(".required", "'ghost' is not in properties"),
        id="required-names-unknown-property",
    ),
    pytest.param(
        (
            "tools.yaml",
            NOTE,
            NOTE + "        idempotency_key: {type: string, maxLength: 64}\n",
        ),
        "tools.yaml: tools[3].input_schema.properties.idempotency_key: the runtime "
        "injects the idempotency key (tool 'add_claim_note')",
        id="idempotency-key-property",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            "        tags: {type: array, items: {type: string, maxLength: 5}}\n",
        ),
        wording(".properties.tags.maxItems", "array needs a maxItems"),
        id="array-without-maxitems",
    ),
    pytest.param(
        ("tools.yaml", TOP_K, "        tags: {type: array, maxItems: 3}\n"),
        wording(".properties.tags.items", "array needs items"),
        id="array-without-items",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            "        tags: {type: array, maxItems: 3, items: {type: object}}\n",
        ),
        wording(
            ".properties.tags.items", "object must set additionalProperties: false"
        ),
        id="open-object-inside-items",
    ),
    pytest.param(
        ("tools.yaml", '"^POL-[0-9]{4}$", maxLength: 8}', '"(", maxLength: 8}'),
        policy(".properties.policy_number.pattern", "not a valid regular expression"),
        id="invalid-pattern",
    ),
    pytest.param(
        ("tools.yaml", QUERY, "        query: {type: string, maxLength: 999999999}\n"),
        wording(".properties.query.maxLength", "must be an integer from 1 to 10000"),
        id="max-length-far-too-large",
    ),
    pytest.param(
        ("tools.yaml", QUERY, "        query: {type: string, maxLength: 0}\n"),
        wording(".properties.query.maxLength", "must be an integer from 1 to 10000"),
        id="max-length-zero",
    ),
    pytest.param(
        ("tools.yaml", QUERY, "        query: {type: string, maxLength: true}\n"),
        wording(".properties.query.maxLength", "must be an integer from 1 to 10000"),
        id="max-length-boolean",
    ),
    pytest.param(
        ("tools.yaml", QUERY, "        query: {type: string, maxLength: 5.5}\n"),
        wording(".properties.query.maxLength", "must be an integer from 1 to 10000"),
        id="max-length-float",
    ),
    pytest.param(
        (
            "tools.yaml",
            QUERY,
            "        query: {type: string, minLength: 9, maxLength: 5}\n",
        ),
        wording(".properties.query.minLength", "must be an integer from 0 to 5"),
        id="min-length-above-max",
    ),
    pytest.param(
        (
            "tools.yaml",
            QUERY,
            "        query: {type: string, minLength: -1, maxLength: 5}\n",
        ),
        wording(".properties.query.minLength", "must be an integer from 0 to 5"),
        id="min-length-negative",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            "        tags: {type: array, maxItems: 101, "
            "items: {type: string, maxLength: 5}}\n",
        ),
        wording(".properties.tags.maxItems", "must be an integer from 1 to 100"),
        id="max-items-above-cap",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            "        tags: {type: array, maxItems: 3, minItems: 4, "
            "items: {type: string, maxLength: 5}}\n",
        ),
        wording(".properties.tags.minItems", "must be an integer from 0 to 3"),
        id="min-items-above-max",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            '        top_k: {type: integer, minimum: "a", maximum: 10}\n',
        ),
        wording(".properties.top_k.minimum", "must be a finite number"),
        id="minimum-a-string",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            "        top_k: {type: integer, minimum: 1, maximum: true}\n",
        ),
        wording(".properties.top_k.maximum", "must be a finite number"),
        id="maximum-a-boolean",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            "        top_k: {type: integer, minimum: 1, maximum: .inf}\n",
        ),
        wording(".properties.top_k.maximum", "must be a finite number"),
        id="maximum-infinite",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            "        top_k: {type: integer, minimum: 11, maximum: 10}\n",
        ),
        wording(".properties.top_k", "minimum must not exceed maximum"),
        id="minimum-above-maximum",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            "        top_k: {type: integer, minimum: 1, maximum: 10, enum: []}\n",
        ),
        wording(".properties.top_k.enum", "must be a non-empty list"),
        id="empty-enum",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            '        top_k: {type: integer, minimum: 1, maximum: 10, enum: [1, "x"]}\n',
        ),
        wording(".properties.top_k.enum[1]", "must be of type integer"),
        id="enum-value-of-the-wrong-type",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            "        top_k: {type: integer, minimum: 1, maximum: 10, enum: [true]}\n",
        ),
        wording(".properties.top_k.enum[0]", "must be of type integer"),
        id="enum-boolean-for-an-integer",
    ),
    pytest.param(
        (
            "tools.yaml",
            TOP_K,
            "        top_k: {type: integer, minimum: 1, maximum: 10, enum: [1, 1]}\n",
        ),
        wording(".properties.top_k.enum", "values must be unique"),
        id="enum-duplicates",
    ),
    pytest.param(
        (
            "tools.yaml",
            NOTE,
            NOTE + "        idempotencyKey: {type: string, maxLength: 64}\n",
        ),
        "tools.yaml: tools[3].input_schema.properties.idempotencyKey: the runtime "
        "injects the idempotency key (tool 'add_claim_note')",
        id="idempotency-key-camel-case",
    ),
    pytest.param(
        (
            "tools.yaml",
            NOTE,
            NOTE + "        Idempotency-Key: {type: string, maxLength: 64}\n",
        ),
        "tools.yaml: tools[3].input_schema.properties.Idempotency-Key: the runtime "
        "injects the idempotency key (tool 'add_claim_note')",
        id="idempotency-key-header-style",
    ),
]


@pytest.mark.parametrize(("edit", "expected"), BAD_SCHEMAS)
def test_input_schema_violation_is_reported(
    plant: Plant, load_errors: LoadErrors, edit: Edit, expected: str
) -> None:
    errors = load_errors(plant(edit))

    assert expected in errors


def test_nested_closed_object_and_bounded_array_are_accepted(plant: Plant) -> None:
    nested = (
        "        filter:\n"
        "          type: object\n"
        "          properties:\n"
        "            tags:\n"
        "              {type: array, maxItems: 3,\n"
        "               items: {type: string, maxLength: 5}}\n"
        "          required: [tags]\n"
        "          additionalProperties: false\n"
    )
    directory = plant(("tools.yaml", TOP_K, TOP_K + nested))

    tool = load_registry(directory).tool("wording_search")

    assert tool is not None
    assert "filter" in tool.input_schema["properties"]


def test_schema_nested_beyond_the_depth_limit_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    inner = "{type: string, maxLength: 3}"
    for _ in range(10):
        inner = (
            f"{{type: object, additionalProperties: false, properties: {{a: {inner}}}}}"
        )
    directory = plant(("tools.yaml", TOP_K, TOP_K + f"        deep: {inner}\n"))

    errors = load_errors(directory)

    assert any("nested deeper than 8 levels" in e for e in errors), errors


def test_committed_ids_are_bounded_so_a_trailing_newline_cannot_match(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)

    for tool_id, prop in [
        ("policy_lookup", "policy_number"),
        ("add_claim_note", "claim_id"),
    ]:
        tool = registry.tool(tool_id)
        assert tool is not None
        assert tool.input_schema["properties"][prop]["maxLength"] == 8


def test_bounds_at_the_caps_are_accepted(plant: Plant) -> None:
    directory = plant(
        (
            "tools.yaml",
            QUERY,
            "        query: {type: string, minLength: 1, maxLength: 10000}\n",
        ),
        (
            "tools.yaml",
            TOP_K,
            "        top_k: {type: number, minimum: -0.5, maximum: 10.5}\n",
        ),
    )

    tool = load_registry(directory).tool("wording_search")

    assert tool is not None
    assert tool.input_schema["properties"]["query"]["maxLength"] == 10000


def test_enum_of_matching_unique_values_is_accepted(plant: Plant) -> None:
    directory = plant(
        (
            "tools.yaml",
            TOP_K,
            "        top_k: {type: integer, minimum: 1, maximum: 10, enum: [1, 5]}\n",
        )
    )

    assert load_registry(directory).tool("wording_search") is not None
