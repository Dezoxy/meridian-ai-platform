"""Structural rules for a tool's ``input_schema`` (hard rule 6).

A small, closed subset of JSON Schema, checked without a schema library: the
model sees the schema, so every field must be typed and really bounded, every
object closed, and nothing may pull in another schema (``$ref``, ``anyOf`` ...).
A tool's ``output_schema`` is held to the same rules.

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
    schema: Mapping[str, Any], root: str = "input_schema"
) -> list[str]:
    """Messages as ``<path>: <problem>``, paths starting at ``root``."""
    if schema.get("type") != "object":
        return [f"{root}.type: must be 'object'"]
    return _node_errors(schema, root, 0)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return isinstance(value, int) or math.isfinite(value)


def _is_forbidden_property(name: object) -> bool:
    """``idempotency_key`` in any spelling: idempotencyKey, Idempotency-Key ..."""
    return re.sub(r"[_-]", "", str(name).lower()) == FORBIDDEN_PROPERTY


def _node_errors(node: Any, path: str, depth: int) -> list[str]:
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
    if kind == "object":
        errors += _object_errors(node, path, depth)
    elif kind == "array":
        errors += _array_errors(node, path, depth)
    elif kind == "string":
        errors += _string_errors(node, path)
    elif kind in {"integer", "number"}:
        errors += _number_errors(node, kind, path)
    return errors


def _object_errors(node: Mapping[str, Any], path: str, depth: int) -> list[str]:
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
        errors += _node_errors(child, child_path, depth + 1)
    required = node.get("required", [])
    if not isinstance(required, list):
        return [*errors, f"{path}.required: must be a list"]
    errors += [
        f"{path}.required: {name!r} is not in properties"
        for name in required
        if not isinstance(name, str) or name not in properties
    ]
    return errors


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


def _array_errors(node: Mapping[str, Any], path: str, depth: int) -> list[str]:
    errors = _size_errors(node, path, "minItems", "maxItems", MAX_ITEMS)
    if "maxItems" not in node:
        errors.append(f"{path}.maxItems: array needs a maxItems")
    if "items" not in node:
        return [*errors, f"{path}.items: array needs items"]
    return errors + _node_errors(node["items"], f"{path}.items", depth + 1)


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
