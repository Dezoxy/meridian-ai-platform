"""The subset of JSON Schema a response schema may use (S051).

Pure functions, no database: the rules, each bound on both sides, the values
that must never be a property name or an enum value, and the promise that an
error message quotes nothing a caller wrote.
"""

import copy
import json
import random
import string
from typing import Any

import pytest
from pydantic import ValidationError

from meridian.platform.gateway.models import (
    RESPONSE_SCHEMA_REFUSAL,
    ChatRequest,
    EmbeddingRequest,
)
from meridian.platform.gateway.response_schema import (
    MAX_DEPTH,
    MAX_ENUM_VALUES,
    MAX_PROPERTIES,
    response_schema_errors,
)
from meridian.platform.guardrails import redact

# The claims triage's answer schema, copied as a literal.
TRIAGE_SCHEMA: dict[str, Any] = json.loads(
    '{"type":"object","properties":{"verdict":{"type":"string",'
    '"enum":["applies","none","unsure"]},"clause":{"type":["string","null"]},'
    '"rationale":{"type":"string"}},"required":["verdict","clause","rationale"],'
    '"additionalProperties":false}'
)
CANARY = "CANARY_caller_value"
# The documented fictional values the redaction knows (RFC 2606, the standard
# test card, an IBAN that passes the checksum, a "+" number).
PERSONAL_VALUES = [
    "ana.kovacs@example.com",
    "HU42 1177 3016 1111 1018 0000 0000",
    "4111 1111 1111 1111",
    "+36 30 123 4567",
]


def obj(members: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A closed object over ``members``, every one required."""
    node: dict[str, Any] = {
        "type": "object",
        "properties": members,
        "required": list(members),
        "additionalProperties": False,
    }
    return node | changes


def nested_objects(levels: int) -> dict[str, Any]:
    """``levels`` objects inside one another, the root being the first."""
    node = obj({"leaf": {"type": "string"}})
    for _ in range(levels - 1):
        node = obj({"inner": node})
    return node


def with_property(name: str, node: dict[str, Any] | None = None) -> dict[str, Any]:
    return obj({name: node or {"type": "string"}})


def errors_of(schema: object) -> list[str]:
    return response_schema_errors(schema)


# ── what is allowed ─────────────────────────────────────────────────────────
def test_the_triage_schema_has_no_errors() -> None:
    assert errors_of(TRIAGE_SCHEMA) == []


@pytest.mark.parametrize("kind", ["string", "integer", "number", "boolean"])
def test_each_scalar_type_and_its_nullable_form_is_allowed(kind: str) -> None:
    schema = obj({"plain": {"type": kind}, "maybe": {"type": [kind, "null"]}})

    assert errors_of(schema) == []


def test_an_array_of_closed_objects_is_allowed() -> None:
    schema = obj(
        {
            "items": {
                "type": "array",
                "items": obj({"name": {"type": "string"}}),
            }
        }
    )

    assert errors_of(schema) == []


def test_an_array_of_nullable_scalars_is_allowed() -> None:
    schema = obj({"tags": {"type": "array", "items": {"type": ["string", "null"]}}})

    assert errors_of(schema) == []


# ── every rule has a failing case ───────────────────────────────────────────
def mutated(change: Any) -> dict[str, Any]:
    schema = copy.deepcopy(TRIAGE_SCHEMA)
    change(schema)
    return schema


BROKEN_SCHEMAS = {
    "root is not an object": ["not", "a", "dict"],
    "root is a string": "object",
    "root is none": None,
    "root type is array": {"type": "array", "items": {"type": "string"}},
    "root type is missing": {k: v for k, v in TRIAGE_SCHEMA.items() if k != "type"},
    "root is nullable": mutated(lambda s: s.update(type=["object", "null"])),
    "type is unknown": with_property("a", {"type": "null"}),
    "type is a number": with_property("a", {"type": 3}),
    "type is a dict": with_property("a", {"type": {"x": 1}}),
    "type list has one item": with_property("a", {"type": ["string"]}),
    "type list has three items": with_property(
        "a", {"type": ["string", "null", "integer"]}
    ),
    "type list is in the other order": with_property("a", {"type": ["null", "string"]}),
    "type list has no null": with_property("a", {"type": ["string", "integer"]}),
    "type list nulls an object": with_property("a", {"type": ["object", "null"]}),
    "type list nulls an array": with_property("a", {"type": ["array", "null"]}),
    "type list holds a dict": with_property("a", {"type": [{"x": 1}, "null"]}),
    "node is not a dict": with_property("a", ["string"]),  # type: ignore[arg-type]
    "node is a string": obj({"a": "string"}),
    "node has a non-str key": obj({"a": {"type": "string", 1: "x"}}),
    "object lacks properties": mutated(lambda s: s.pop("properties")),
    "object lacks required": mutated(lambda s: s.pop("required")),
    "object lacks additionalProperties": mutated(
        lambda s: s.pop("additionalProperties")
    ),
    "properties is empty": obj({}),
    "properties is a list": obj({}, properties=[]),
    "additionalProperties is true": mutated(
        lambda s: s.update(additionalProperties=True)
    ),
    "additionalProperties is zero": mutated(lambda s: s.update(additionalProperties=0)),
    "additionalProperties is a schema": mutated(
        lambda s: s.update(additionalProperties={"type": "string"})
    ),
    "required is a string": mutated(lambda s: s.update(required="verdict")),
    "required is a dict": mutated(lambda s: s.update(required={"verdict": 1})),
    "required misses a property": mutated(lambda s: s.update(required=["verdict"])),
    "required names a stranger": mutated(
        lambda s: s.update(required=[*s["required"], "stranger"])
    ),
    "required has a duplicate": mutated(
        lambda s: s.update(required=[*s["required"], "verdict"])
    ),
    "required has a non-string": mutated(
        lambda s: s.update(required=["verdict", "clause", 3])
    ),
    "required has a dict": mutated(
        lambda s: s.update(required=["verdict", "clause", {"x": 1}])
    ),
    "array lacks items": with_property("a", {"type": "array"}),
    "array has maxItems": with_property(
        "a", {"type": "array", "items": {"type": "string"}, "maxItems": 3}
    ),
    "array items is a list": with_property("a", {"type": "array", "items": []}),
    "array items is a string": with_property("a", {"type": "array", "items": "x"}),
    "enum is empty": with_property("a", {"type": "string", "enum": []}),
    "enum is a string": with_property("a", {"type": "string", "enum": "yes"}),
    "enum has a duplicate": with_property("a", {"type": "string", "enum": ["a", "a"]}),
    "enum has an integer": with_property("a", {"type": "string", "enum": ["a", 1]}),
    "enum has a bool": with_property("a", {"type": "string", "enum": [True]}),
    "enum has a dict": with_property("a", {"type": "string", "enum": [{"x": 1}]}),
    "enum on an integer": with_property("a", {"type": "integer", "enum": [1]}),
    "enum on a nullable string": with_property(
        "a", {"type": ["string", "null"], "enum": ["a"]}
    ),
    "enum value is upper case": with_property("a", {"type": "string", "enum": ["A"]}),
    "enum value starts with a digit": with_property(
        "a", {"type": "string", "enum": ["1a"]}
    ),
    "enum value has a hyphen": with_property("a", {"type": "string", "enum": ["a-b"]}),
    "enum value is empty": with_property("a", {"type": "string", "enum": [""]}),
    "enum value ends in a newline": with_property(
        "a", {"type": "string", "enum": ["ab\n"]}
    ),
    "enum value is 33 characters": with_property(
        "a", {"type": "string", "enum": ["a" * 33]}
    ),
    "property name is upper case": with_property("Verdict"),
    "property name starts with a digit": with_property("1verdict"),
    "property name has a hyphen": with_property("claim-id"),
    "property name has a space": with_property("claim id"),
    "property name is empty": with_property(""),
    "property name is 33 characters": with_property("a" * 33),
    "property name ends in a newline": with_property("name\n"),
    "property name is not text": {
        "type": "object",
        "properties": {1: {"type": "string"}},
        "required": [1],
        "additionalProperties": False,
    },
}
FORBIDDEN_KEYWORDS = {
    "description": "x",
    "title": "x",
    "default": "x",
    "const": "x",
    "pattern": "^a$",
    "format": "date",
    "minLength": 1,
    "maxLength": 5,
    "minimum": 1,
    "maximum": 5,
    "minItems": 1,
    "maxItems": 5,
    "anyOf": [{"type": "string"}],
    "oneOf": [{"type": "string"}],
    "allOf": [{"type": "string"}],
    "$ref": "#/$defs/x",
    "$defs": {},
    "definitions": {},
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "unevaluatedProperties": False,
}
for _keyword, _value in FORBIDDEN_KEYWORDS.items():
    BROKEN_SCHEMAS[f"root has {_keyword}"] = TRIAGE_SCHEMA | {_keyword: _value}
    BROKEN_SCHEMAS[f"a string has {_keyword}"] = with_property(
        "a", {"type": "string", _keyword: _value}
    )
    BROKEN_SCHEMAS[f"an integer has {_keyword}"] = with_property(
        "a", {"type": "integer", _keyword: _value}
    )
    BROKEN_SCHEMAS[f"an array has {_keyword}"] = with_property(
        "a", {"type": "array", "items": {"type": "string"}, _keyword: _value}
    )


@pytest.mark.parametrize("name", list(BROKEN_SCHEMAS))
def test_a_schema_outside_the_subset_has_an_error(name: str) -> None:
    assert errors_of(BROKEN_SCHEMAS[name]) != []


def test_a_broken_schema_never_raises() -> None:
    for schema in BROKEN_SCHEMAS.values():
        assert isinstance(errors_of(schema), list)


# ── the bounds: at the limit passes, one over fails ─────────────────────────
def test_twenty_properties_in_all_pass_and_twenty_one_fail() -> None:
    assert MAX_PROPERTIES == 20
    at_limit = obj({f"p{i}": {"type": "string"} for i in range(MAX_PROPERTIES)})
    over = obj({f"p{i}": {"type": "string"} for i in range(MAX_PROPERTIES + 1)})

    assert errors_of(at_limit) == []
    assert errors_of(over) != []


def test_the_property_limit_counts_nested_properties_too() -> None:
    def spread(extra: int) -> dict[str, Any]:
        # 1 property on the root, the rest (MAX_PROPERTIES - 1 + extra) inside.
        names = range(MAX_PROPERTIES - 1 + extra)
        return obj({"inner": obj({f"p{i}": {"type": "string"} for i in names})})

    assert errors_of(spread(0)) == []
    assert errors_of(spread(1)) != []


def test_the_property_limit_counts_the_properties_of_an_array_s_objects() -> None:
    def listing(count: int) -> dict[str, Any]:
        item = obj({f"p{i}": {"type": "string"} for i in range(count)})
        return obj({"rows": {"type": "array", "items": item}})

    assert errors_of(listing(MAX_PROPERTIES - 1)) == []
    assert errors_of(listing(MAX_PROPERTIES)) != []


def test_depth_three_passes_and_depth_four_fails() -> None:
    assert MAX_DEPTH == 3
    assert errors_of(nested_objects(MAX_DEPTH)) == []
    assert errors_of(nested_objects(MAX_DEPTH + 1)) != []


def test_an_array_adds_a_level_like_an_object() -> None:
    def chain(levels: int) -> dict[str, Any]:
        # The root is level 1; each array and each object below adds one.
        node: dict[str, Any] = {"type": "string"}
        for _ in range(levels - 1):
            node = {"type": "array", "items": node}
        return obj({"inner": node})

    assert errors_of(chain(MAX_DEPTH)) == []
    assert errors_of(chain(MAX_DEPTH + 1)) != []


def test_a_very_deep_schema_is_an_error_and_not_a_recursion_error() -> None:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(5000):
        schema = obj({"inner": schema})

    errors = errors_of(schema)

    assert errors != []
    assert len(errors) < 50


def test_sixteen_enum_values_pass_and_seventeen_fail() -> None:
    def with_values(count: int) -> dict[str, Any]:
        values = [f"v{i}" for i in range(count)]
        return with_property("pick", {"type": "string", "enum": values})

    assert MAX_ENUM_VALUES == 16
    assert errors_of(with_values(MAX_ENUM_VALUES)) == []
    assert errors_of(with_values(MAX_ENUM_VALUES + 1)) != []


def test_a_32_character_word_passes_as_a_name_and_as_a_value() -> None:
    word = "a" + "b" * 31
    schema = with_property(word, {"type": "string", "enum": [word]})

    assert errors_of(schema) == []


def test_a_one_character_word_passes() -> None:
    assert errors_of(with_property("a", {"type": "string", "enum": ["b"]})) == []


def test_digits_and_underscores_after_the_first_letter_pass() -> None:
    schema = with_property("claim_2_id", {"type": "string", "enum": ["a_1", "b2"]})

    assert errors_of(schema) == []


# ── a value a caller could hide personal data in ────────────────────────────
@pytest.mark.parametrize("value", PERSONAL_VALUES)
def test_a_personal_identifier_is_refused_as_an_enum_value(value: str) -> None:
    schema = with_property("pick", {"type": "string", "enum": [value]})

    assert errors_of(schema) != []


@pytest.mark.parametrize("value", PERSONAL_VALUES)
def test_a_personal_identifier_is_refused_as_a_property_name(value: str) -> None:
    assert errors_of(with_property(value)) != []


def test_redaction_leaves_a_sample_of_pattern_words_unchanged() -> None:
    generator = random.Random(5051)  # noqa: S311  # a seeded sample, not a secret
    rest = string.ascii_lowercase + string.digits + "_"

    def word() -> str:
        tail = generator.choices(rest, k=generator.randint(0, 31))
        return generator.choice(string.ascii_lowercase) + "".join(tail)

    words = [word() for _ in range(500)]

    assert all(errors_of(with_property(word)) == [] for word in words)
    for word in words:
        redaction = redact(word)
        assert redaction.text == word
        assert redaction.found == {}


# ── an error says what is wrong and quotes nothing ──────────────────────────
def canary_schemas() -> dict[str, Any]:
    """Each schema puts the canary where a caller chooses the text."""
    return {
        "property name": with_property(CANARY),
        "nested property name": obj({"a": with_property(CANARY)}),
        "enum value": with_property("a", {"type": "string", "enum": [CANARY]}),
        "second enum value": with_property(
            "a", {"type": "string", "enum": ["fine", CANARY]}
        ),
        "unknown keyword": with_property("a", {"type": "string", CANARY: 1}),
        "unknown root keyword": TRIAGE_SCHEMA | {CANARY: 1},
        "forbidden keyword value": with_property(
            "a", {"type": "string", "description": CANARY}
        ),
        "type word": with_property("a", {"type": CANARY}),
        "type list word": with_property("a", {"type": [CANARY, "null"]}),
        "required word": TRIAGE_SCHEMA | {"required": [CANARY]},
        "required next to a good one": TRIAGE_SCHEMA
        | {"required": [*TRIAGE_SCHEMA["required"], CANARY]},
        "additionalProperties": TRIAGE_SCHEMA | {"additionalProperties": CANARY},
        "properties value": TRIAGE_SCHEMA | {"properties": CANARY},
        "node value": obj({"a": CANARY}),
        "items value": with_property("a", {"type": "array", "items": CANARY}),
        "root": CANARY,
        "root type": {"type": CANARY},
        "enum text": with_property("a", {"type": "string", "enum": CANARY}),
        "enum dict value": with_property(
            "a", {"type": "string", "enum": [{CANARY: CANARY}]}
        ),
        "non-string key": obj({"a": {"type": "string", 7: CANARY}}),
    }


@pytest.mark.parametrize("name", list(canary_schemas()))
def test_no_error_message_contains_the_offending_value(name: str) -> None:
    errors = errors_of(canary_schemas()[name])

    assert errors != []
    assert "CANARY" not in " ".join(errors)
    assert "caller_value" not in " ".join(errors)


@pytest.mark.parametrize("value", PERSONAL_VALUES)
def test_no_error_message_contains_a_personal_identifier(value: str) -> None:
    schemas = [
        with_property(value),
        with_property("a", {"type": "string", "enum": [value]}),
        with_property("a", {"type": "string", value: 1}),
    ]
    fragments = [value, *value.split()]

    for schema in schemas:
        errors = " ".join(errors_of(schema))
        assert errors != ""
        assert not any(fragment in errors for fragment in fragments)


def test_a_path_in_a_message_holds_only_names_that_passed_the_pattern() -> None:
    schema = obj({"outer": with_property("inner", {"type": "integer", "enum": [1]})})

    (error,) = errors_of(schema)

    assert error.startswith("response_schema.outer.inner")


def test_the_input_is_not_changed() -> None:
    schema = copy.deepcopy(TRIAGE_SCHEMA)
    broken = copy.deepcopy(BROKEN_SCHEMAS["required has a duplicate"])
    before = copy.deepcopy(broken)

    errors_of(schema)
    errors_of(broken)

    assert schema == TRIAGE_SCHEMA
    assert broken == before


# ── the request model ───────────────────────────────────────────────────────
MESSAGES = [{"role": "user", "content": "A storm hit the roof."}]


def test_a_chat_request_has_no_response_schema_by_default() -> None:
    assert ChatRequest.model_validate({"messages": MESSAGES}).response_schema is None


def test_a_chat_request_keeps_a_schema_inside_the_subset_as_it_is() -> None:
    request = ChatRequest.model_validate(
        {"messages": MESSAGES, "response_schema": TRIAGE_SCHEMA}
    )

    assert request.response_schema == TRIAGE_SCHEMA


@pytest.mark.parametrize("name", ["property name", "enum value", "unknown keyword"])
def test_a_chat_request_refuses_a_schema_outside_the_subset_in_one_fixed_sentence(
    name: str,
) -> None:
    with pytest.raises(ValidationError) as caught:
        ChatRequest.model_validate(
            {"messages": MESSAGES, "response_schema": canary_schemas()[name]}
        )

    (problem,) = caught.value.errors()
    assert problem["loc"] == ("response_schema",)
    assert problem["msg"] == f"Value error, {RESPONSE_SCHEMA_REFUSAL}"
    assert "CANARY" not in problem["msg"]


@pytest.mark.parametrize("value", ["a string", ["a"], 3])
def test_a_chat_request_refuses_a_schema_that_is_not_an_object(value: object) -> None:
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"messages": MESSAGES, "response_schema": value})


def test_an_embedding_request_has_no_response_schema_field() -> None:
    with pytest.raises(ValidationError):
        EmbeddingRequest.model_validate(
            {"inputs": ["a"], "response_schema": TRIAGE_SCHEMA}
        )
