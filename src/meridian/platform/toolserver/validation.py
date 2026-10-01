"""Check arguments and results against the registry's JSON Schemas, with the
size limits first.

``registry/tool_schema.py`` promises that a ``pattern`` only ever sees a
bounded string. ``jsonschema`` runs a node's keywords in the order the schema
lists them, so a schema with ``pattern`` before ``maxLength`` would run the
pattern on a string of any length. The validator is therefore built from a
copy of the schema in which every node's size keywords come first, and only the
first error is asked for: a string that is too long is refused before its
pattern runs.

The error object is never kept: its message quotes the offending value.

A schema cannot say that a string must be storable: PostgreSQL refuses a NUL
character in ``text`` and the payload hash cannot encode a lone surrogate, so
``storable`` is a second check on arguments that already fit.
"""

import math
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from meridian.platform.common.env import SettingsError

SIZE_KEYWORDS = ("minLength", "maxLength", "minItems", "maxItems")
NUL = "\x00"


def size_first(node: Any) -> Any:
    """A copy of ``node`` in which every mapping lists its size keywords
    before its other keys."""
    if isinstance(node, list):
        return [size_first(item) for item in node]
    if not isinstance(node, dict):
        return node
    # Recursing into the size values too is harmless (they are integers) and
    # keeps a property that happens to be named "maxLength" a reordered schema.
    ordered = {key: size_first(node[key]) for key in SIZE_KEYWORDS if key in node}
    for key, value in node.items():
        if key not in ordered:
            ordered[key] = size_first(value)
    return ordered


def build_validator(
    schema: dict[str, Any], what: str = "the schema"
) -> Draft202012Validator:
    """A validator for ``schema``; ``SettingsError`` naming ``what`` when the
    schema is not valid JSON Schema, so a start fails and not a call."""
    ordered = size_first(schema)
    try:
        Draft202012Validator.check_schema(ordered)
    except SchemaError as exc:
        # The schema is the registry's, not a caller's, but the message of the
        # error quotes a value of it; the path says where.
        where = "/".join(str(part) for part in exc.absolute_path) or "the root"
        raise SettingsError(f"{what} is not valid JSON Schema (at {where})") from None
    return Draft202012Validator(ordered)


def storable(value: Any) -> bool:
    """Whether every string, key and number in ``value`` can be stored and
    hashed: no NUL character, UTF-8 encodable, finite floats."""
    if isinstance(value, str):
        return NUL not in value and _encodes(value)
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list | tuple):
        return all(storable(item) for item in value)
    if isinstance(value, dict):
        return all(storable(key) and storable(item) for key, item in value.items())
    return True


def _encodes(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def fits(validator: Draft202012Validator, instance: Any) -> bool:
    """Whether ``instance`` satisfies the schema; stops at the first error."""
    return next(validator.iter_errors(instance), None) is None
