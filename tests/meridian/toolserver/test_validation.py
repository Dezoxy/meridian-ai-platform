"""Argument checks that need no database: size before pattern, what storage
and the hash can take, and a schema that is not JSON Schema."""

import math
from typing import Any

import pytest

from meridian.platform.common.env import SettingsError
from meridian.platform.toolserver.validation import build_validator, fits, storable


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        "",
        "café \U0001f600",
        "line\nbreak\ttab",
        0,
        -3,
        1.5,
        True,
        None,
        [],
        {},
        {"a": ["b", {"c": 2.5}]},
    ],
)
def test_a_value_the_database_and_the_hash_can_take_is_storable(value: Any) -> None:
    assert storable(value) is True


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("a\x00b", id="nul"),
        pytest.param("\x00", id="only-nul"),
        pytest.param("\ud800", id="lone-high-surrogate"),
        pytest.param("\udfff", id="lone-low-surrogate"),
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="infinity"),
        pytest.param(-math.inf, id="minus-infinity"),
        pytest.param(["ok", "a\x00"], id="nul-in-a-list"),
        pytest.param({"note": "\ud800"}, id="surrogate-in-an-object"),
        pytest.param({"a\x00": "ok"}, id="nul-in-a-key"),
        pytest.param({"\ud800": "ok"}, id="surrogate-in-a-key"),
        pytest.param({"a": [{"b": [math.nan]}]}, id="nan-deep-inside"),
    ],
)
def test_a_value_the_database_or_the_hash_cannot_take_is_not_storable(
    value: Any,
) -> None:
    assert storable(value) is False


def test_a_schema_that_is_not_json_schema_raises_a_settings_error() -> None:
    with pytest.raises(SettingsError, match="JSON Schema"):
        build_validator({"type": "object", "required": ["a", "a"]})


def test_the_error_of_a_bad_schema_names_what_was_asked_for() -> None:
    with pytest.raises(SettingsError, match="the input schema of tool 'x'"):
        build_validator(
            {"type": "object", "required": ["a", "a"]},
            what="the input schema of tool 'x'",
        )


def test_a_valid_schema_builds_a_validator_that_stops_at_the_first_error() -> None:
    validator = build_validator(
        {
            "type": "object",
            "properties": {"a": {"type": "string", "maxLength": 3}},
            "additionalProperties": False,
        }
    )

    assert fits(validator, {"a": "abc"}) is True
    assert fits(validator, {"a": "abcd"}) is False
