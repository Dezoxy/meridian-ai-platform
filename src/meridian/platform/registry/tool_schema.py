"""Structural rules for a tool's ``input_schema`` (hard rule 6).

A small, closed subset of JSON Schema, checked without a schema library: the
model sees the schema, so every field must be typed and really bounded, every
object closed, and nothing may pull in another schema (``$ref``, ``anyOf`` ...).
A tool's ``output_schema`` is held to the same rules.

One conditional is admitted, in an output schema only and in one shape, so that
an answer can promise a field when a flag says so: ``if`` constants on declared
properties, ``then`` the declared properties it requires, on an object. An input
schema is what the model sees, and a provider's strict tool-schema mode does not
take conditionals, so there they are refused. Nothing else conditional is
admitted anywhere.

There is no ReDoS analysis of ``pattern``: S013 validates arguments with the
length caps first, so a pattern only ever sees a bounded string. That holds only
if a string with a ``pattern`` has a ``maxLength``, which is checked.
"""

import json
import math
import re
from collections.abc import Mapping
from typing import Any

ALLOWED_KEYWORDS = frozenset(
    {
        "type",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "enum",
        "const",
        "if",
        "then",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
    }
)
ALLOWED_TYPES = frozenset({"string", "integer", "number", "boolean", "object", "array"})
# The runtime injects the key; a model-facing argument would let a model pick it.
FORBIDDEN_PROPERTY = "idempotencykey"
MAX_DEPTH = 8
MAX_STRING_LENGTH = 10000
MAX_ITEMS = 100


def input_schema_errors(
    schema: Mapping[str, Any],
    root: str = "input_schema",
    *,
    allow_conditionals: bool = False,
) -> list[str]:
    """Messages as ``<path>: <problem>``, paths starting at ``root``.
    ``allow_conditionals`` is for an output schema only."""
    if schema.get("type") != "object":
        return [f"{root}.type: must be 'object'"]
    return _node_errors(schema, root, 0, allow_conditionals)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return isinstance(value, int) or math.isfinite(value)


def _is_forbidden_property(name: object) -> bool:
    """``idempotency_key`` in any spelling: idempotencyKey, Idempotency-Key ..."""
    return re.sub(r"[_-]", "", str(name).lower()) == FORBIDDEN_PROPERTY


def _node_errors(
    node: Any, path: str, depth: int, conditionals: bool = False
) -> list[str]:
    if not isinstance(node, Mapping):
        return [f"{path}: must be a mapping"]
    if depth > MAX_DEPTH:
        return [f"{path}: nested deeper than {MAX_DEPTH} levels"]
    errors = [
        f"{path}.{key}: keyword is not allowed"
        for key in node
        if key not in ALLOWED_KEYWORDS
    ]
    kind = node.get("type")
    if not isinstance(kind, str) or kind not in ALLOWED_TYPES:
        return [*errors, f"{path}.type: must be one of {sorted(ALLOWED_TYPES)}"]
    if "enum" in node:
        errors += _enum_errors(node["enum"], kind, path)
    errors += _conditional_errors(node, kind, path, conditionals)
    if kind == "object":
        errors += _object_errors(node, path, depth, conditionals)
    elif kind == "array":
        errors += _array_errors(node, path, depth, conditionals)
    elif kind == "string":
        errors += _string_errors(node, path)
    elif kind in {"integer", "number"}:
        errors += _number_errors(node, kind, path)
    return errors


def _object_errors(
    node: Mapping[str, Any], path: str, depth: int, conditionals: bool
) -> list[str]:
    errors: list[str] = []
    if node.get("additionalProperties") is not False:
        errors.append(f"{path}: object must set additionalProperties: false")
    properties = node.get("properties", {})
    if not isinstance(properties, Mapping):
        return [*errors, f"{path}.properties: must be a mapping"]
    for name, child in properties.items():
        child_path = f"{path}.properties.{name}"
        if _is_forbidden_property(name):
            errors.append(f"{child_path}: the runtime injects the idempotency key")
        errors += _node_errors(child, child_path, depth + 1, conditionals)
    required = node.get("required", [])
    if not isinstance(required, list):
        return [*errors, f"{path}.required: must be a list"]
    errors += [
        f"{path}.required: {name!r} is not in properties"
        for name in required
        if not isinstance(name, str) or name not in properties
    ]
    return errors


def _conditional_errors(
    node: Mapping[str, Any], kind: str, path: str, allowed: bool
) -> list[str]:
    """The one conditional the subset admits, in an output schema only: ``if``
    constants on declared properties, ``then`` names declared properties that
    become required.

    Neither body is walked as a schema node, so no keyword gets in that the
    shape does not list. ``else`` is not in ``ALLOWED_KEYWORDS``."""
    if "if" not in node and "then" not in node:
        return []
    if not allowed:
        # The model sees an input schema, and a provider's strict tool-schema
        # mode does not take conditionals; an answer's schema is not shown.
        return [f"{path}: 'if' and 'then' are allowed only in an output schema"]
    if "if" not in node or "then" not in node:
        return [f"{path}: 'if' and 'then' must appear together"]
    if kind != "object":
        return [f"{path}: 'if' and 'then' are allowed only on an object"]
    properties = node.get("properties", {})
    declared = properties if isinstance(properties, Mapping) else {}
    return [
        *_if_errors(node["if"], f"{path}.if", declared),
        *_then_errors(node["then"], f"{path}.then", declared),
    ]


def _body_errors(body: Any, path: str, allowed: tuple[str, ...]) -> list[str]:
    """``body`` is a mapping with exactly the keys ``allowed``."""
    if not isinstance(body, Mapping):
        return [f"{path}: must be a mapping"]
    return [
        f"{path}.{key}: keyword is not allowed" for key in body if key not in allowed
    ] + [f"{path}.{key}: is required" for key in allowed if key not in body]


def _names_errors(names: Any, path: str, declared: Mapping[str, Any]) -> list[str]:
    """``names`` is a non-empty list of names, each of a declared property."""
    if not isinstance(names, list) or not names:
        return [f"{path}: must be a list of names"]
    return [
        f"{path}: {name!r} is not in properties"
        for name in names
        if not isinstance(name, str) or name not in declared
    ]


def _if_errors(body: Any, path: str, declared: Mapping[str, Any]) -> list[str]:
    errors = _body_errors(body, path, ("properties", "required"))
    if errors or not isinstance(body, Mapping):
        return errors
    constants = body["properties"]
    if not isinstance(constants, Mapping) or not constants:
        return [f"{path}.properties: must name at least one property"]
    for name, constant in constants.items():
        errors += _constant_errors(constant, f"{path}.properties.{name}")
        if name not in declared:
            errors.append(f"{path}.properties.{name}: {name!r} is not in properties")
    required = body["required"]
    if not isinstance(required, list) or sorted(map(str, required)) != sorted(
        map(str, constants)
    ):
        errors.append(f"{path}.required: must list exactly the names in properties")
    return errors


def _constant_errors(constant: Any, path: str) -> list[str]:
    if not isinstance(constant, Mapping) or set(constant) != {"const"}:
        return [f"{path}: must hold exactly const"]
    value = constant["const"]
    if isinstance(value, bool | str) or _is_number(value):
        return []
    return [f"{path}.const: must be a string, number or boolean"]


def _then_errors(body: Any, path: str, declared: Mapping[str, Any]) -> list[str]:
    errors = _body_errors(body, path, ("required",))
    if errors or not isinstance(body, Mapping):
        return errors
    return _names_errors(body["required"], f"{path}.required", declared)


def _size_errors(
    node: Mapping[str, Any], path: str, low: str, high: str, cap: int
) -> list[str]:
    """``high`` must be an integer in 1..cap; ``low`` in 0..high, when present."""
    errors: list[str] = []
    top = node.get(high)
    top_ok = _is_int(top) and 1 <= top <= cap
    if high in node and not top_ok:
        errors.append(f"{path}.{high}: must be an integer from 1 to {cap}")
    limit = top if top_ok else cap
    bottom = node.get(low)
    if low in node and not (_is_int(bottom) and 0 <= bottom <= limit):
        errors.append(f"{path}.{low}: must be an integer from 0 to {limit}")
    return errors


def _array_errors(
    node: Mapping[str, Any], path: str, depth: int, conditionals: bool
) -> list[str]:
    errors = _size_errors(node, path, "minItems", "maxItems", MAX_ITEMS)
    if "maxItems" not in node:
        errors.append(f"{path}.maxItems: array needs a maxItems")
    if "items" not in node:
        return [*errors, f"{path}.items: array needs items"]
    return errors + _node_errors(
        node["items"], f"{path}.items", depth + 1, conditionals
    )


def _string_errors(node: Mapping[str, Any], path: str) -> list[str]:
    errors = _size_errors(node, path, "minLength", "maxLength", MAX_STRING_LENGTH)
    pattern = node.get("pattern")
    if pattern is not None and "maxLength" not in node:
        # An enum bounds the values, not the string a pattern is run on.
        errors.append(f"{path}: string with a pattern needs maxLength")
    elif "maxLength" not in node and "enum" not in node:
        errors.append(f"{path}: string needs maxLength or enum")
    if pattern is not None:
        try:
            re.compile(pattern)
        except (re.error, TypeError):
            errors.append(f"{path}.pattern: not a valid regular expression")
    return errors


def _number_errors(node: Mapping[str, Any], kind: str, path: str) -> list[str]:
    errors: list[str] = []
    for bound in ("minimum", "maximum"):
        if bound not in node:
            errors.append(f"{path}.{bound}: {kind} needs a {bound}")
        elif not _is_number(node[bound]):
            errors.append(f"{path}.{bound}: must be a finite number")
    if not errors and node["minimum"] > node["maximum"]:
        errors.append(f"{path}: minimum must not exceed maximum")
    return errors


def _matches_type(value: object, kind: str) -> bool:
    checks = {
        "string": lambda v: isinstance(v, str),
        "integer": _is_int,
        "number": _is_number,
        "boolean": lambda v: isinstance(v, bool),
        "object": lambda v: isinstance(v, Mapping),
        "array": lambda v: isinstance(v, list),
    }
    return checks[kind](value)


def _enum_errors(values: object, kind: str, path: str) -> list[str]:
    if not isinstance(values, list) or not values:
        return [f"{path}.enum: must be a non-empty list"]
    errors = [
        f"{path}.enum[{i}]: must be of type {kind}"
        for i, value in enumerate(values)
        if not _matches_type(value, kind)
    ]
    encoded = [json.dumps(v, sort_keys=True, default=str) for v in values]
    if len(set(encoded)) != len(encoded):
        errors.append(f"{path}.enum: values must be unique")
    return errors
