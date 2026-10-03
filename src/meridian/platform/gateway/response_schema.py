"""Structural rules for a response schema, the shape of a model's answer (S051).

A small, closed subset of JSON Schema, checked without a schema library and
stricter than ``registry/tool_schema.py``: Azure OpenAI's strict structured
outputs refuse keywords that file allows. The root is a closed object; every
node says its ``type`` and nothing else, except an object (``properties``,
``required``, ``additionalProperties``), an array (``items``) and a string
that lists an ``enum``. Every property is required and a nullable value is
``[type, "null"]``, because strict mode has no optional property.

Property names and enum values are short lower-case words. The schema reaches
the provider as it is: redaction passes over messages, not over the schema
(T-75), so the word pattern is what keeps an e-mail address, a card number or
a "+" phone number from travelling as a name. An error message says what is
wrong and, at most, the path of names that already passed the pattern: it never
quotes a name, a value or a keyword a caller wrote, because a 422 carries it.
The check never raises, and stops at the depth bound instead of recursing.

Pure, with no I/O.
"""

import re

# The bounds sit inside Azure's own for strict structured outputs (100
# properties, 5 levels of nesting, 500 enum values, 15,000 characters of
# property names and enum values in all): at most 20 properties in the whole
# schema, 3 levels (the root is 1, each nested object or array adds 1) and 16
# values per enum. With names and values of at most 32 characters, 20
# properties and their enums stay near 11,000 characters.
MAX_PROPERTIES = 20
MAX_DEPTH = 3
MAX_ENUM_VALUES = 16
# Whole-string match: ``$`` would let a trailing newline through.
WORD = re.compile(r"[a-z][a-z0-9_]{0,31}")

ROOT_PATH = "response_schema"
SCALAR_TYPES = ("string", "integer", "number", "boolean")
CONTAINER_TYPES = ("object", "array")
NULL_TYPE = "null"
# Per type: the keywords it may carry, and of those the ones it may leave out.
ALLOWED_KEYWORDS = {
    "object": frozenset({"type", "properties", "required", "additionalProperties"}),
    "array": frozenset({"type", "items"}),
    "string": frozenset({"type", "enum"}),
}
SCALAR_KEYWORDS = frozenset({"type"})
OPTIONAL_KEYWORDS = frozenset({"enum"})

# What a walk found: its error messages and the properties it counted.
Walk = tuple[list[str], int]


def response_schema_errors(schema: object) -> list[str]:
    """Why ``schema`` is outside the subset, one message each; empty when it
    is inside. Paths start at ``response_schema``."""
    errors, properties = _node(schema, ROOT_PATH, 1, root=True)
    if properties > MAX_PROPERTIES:
        errors.append(f"{ROOT_PATH}: more than {MAX_PROPERTIES} properties in all")
    return errors


def _is_word(value: object) -> bool:
    return isinstance(value, str) and WORD.fullmatch(value) is not None


def _kind(value: object) -> tuple[str, bool] | None:
    """The type a ``type`` value names and whether it is nullable, or ``None``
    when it is neither a type nor ``[scalar type, "null"]``."""
    if isinstance(value, str):
        known = value in (*SCALAR_TYPES, *CONTAINER_TYPES)
        return (value, False) if known else None
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and value[0] in SCALAR_TYPES
        and value[1] == NULL_TYPE
    ):
        return value[0], True
    return None


def _keyword_errors(
    node: dict[str, object], path: str, kind: str, nullable: bool
) -> list[str]:
    if nullable or kind not in ALLOWED_KEYWORDS:
        allowed = SCALAR_KEYWORDS
    else:
        allowed = ALLOWED_KEYWORDS[kind]
    errors = []
    if not set(node) <= allowed:
        errors.append(f"{path}: carries a keyword outside the allowed subset")
    errors += [
        f"{path}: lacks the keyword {keyword!r}"
        for keyword in sorted(allowed - OPTIONAL_KEYWORDS - node.keys())
    ]
    return errors


def _node(node: object, path: str, depth: int, *, root: bool = False) -> Walk:
    if not isinstance(node, dict):
        return [f"{path}: must be an object"], 0
    if not all(isinstance(key, str) for key in node):
        return [f"{path}: keys must be strings"], 0
    kind = _kind(node.get("type"))
    if kind is None or (root and kind != ("object", False)):
        expected = "'object'" if root else "a scalar, an object or an array"
        return [f"{path}.type: must be {expected}"], 0
    base, nullable = kind
    if base in CONTAINER_TYPES and not nullable and depth > MAX_DEPTH:
        return [f"{path}: nested deeper than {MAX_DEPTH} levels"], 0
    errors = _keyword_errors(node, path, base, nullable)
    if nullable or base in SCALAR_TYPES:
        return errors + _enum_errors(node, path, base), 0
    if base == "array":
        found, count = _array(node, path, depth)
    else:
        found, count = _object(node, path, depth)
    return errors + found, count


def _array(node: dict[str, object], path: str, depth: int) -> Walk:
    if "items" not in node:
        return [], 0
    return _node(node["items"], f"{path}.items", depth + 1)


def _object(node: dict[str, object], path: str, depth: int) -> Walk:
    errors: list[str] = []
    if "additionalProperties" in node and node["additionalProperties"] is not False:
        errors.append(f"{path}: additionalProperties must be false")
    properties = node.get("properties")
    if "properties" not in node:
        return errors, 0
    if not isinstance(properties, dict) or not properties:
        return [*errors, f"{path}.properties: must be a non-empty object"], 0
    if len(properties) > MAX_PROPERTIES:
        return [*errors, f"{path}.properties: more than {MAX_PROPERTIES}"], 0
    count = len(properties)
    for name, child in properties.items():
        if not _is_word(name):
            errors.append(f"{path}: a property name is not a lower-case word")
            continue
        found, nested = _node(child, f"{path}.{name}", depth + 1)
        errors += found
        count += nested
    if "required" in node:
        errors += _required_errors(node["required"], properties, path)
    return errors, count


def _required_errors(required: object, properties: dict, path: str) -> list[str]:
    if not isinstance(required, list) or not all(isinstance(n, str) for n in required):
        return [f"{path}.required: must be a list of property names"]
    if len(set(required)) != len(required):
        return [f"{path}.required: names must be unique"]
    if set(required) != set(properties):
        return [f"{path}.required: must name every property and no other"]
    return []


def _enum_errors(node: dict[str, object], path: str, kind: str) -> list[str]:
    if "enum" not in node:
        return []
    values = node["enum"]
    if kind != "string":
        return []  # the keyword check has reported it
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_ENUM_VALUES:
        return [f"{path}.enum: must be a list of 1 to {MAX_ENUM_VALUES} values"]
    if not all(_is_word(value) for value in values):
        return [f"{path}.enum: every value must be a lower-case word"]
    if len(set(values)) != len(values):
        return [f"{path}.enum: values must be unique"]
    return []
