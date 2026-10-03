"""The order of the four data classes and the higher of two."""

import itertools
from typing import get_args

import pytest

from meridian.platform.guardrails import (
    DATA_CLASS_ORDER,
    higher_class,
    parse_data_class,
)
from meridian.platform.registry.models import DataClass


def test_the_order_holds_exactly_the_members_of_the_data_class_type() -> None:
    assert set(DATA_CLASS_ORDER) == set(get_args(DataClass))
    assert len(DATA_CLASS_ORDER) == len(get_args(DataClass))


def test_the_order_runs_from_least_to_most_sensitive() -> None:
    assert DATA_CLASS_ORDER == ("synthetic", "internal", "personal", "special")


@pytest.mark.parametrize(
    ("a", "b"), list(itertools.product(DATA_CLASS_ORDER, repeat=2))
)
def test_higher_class_returns_the_more_sensitive_of_every_pair(
    a: DataClass, b: DataClass
) -> None:
    expected = DATA_CLASS_ORDER[
        max(DATA_CLASS_ORDER.index(a), DATA_CLASS_ORDER.index(b))
    ]
    assert higher_class(a, b) == expected
    assert higher_class(b, a) == expected


def test_higher_class_covers_all_sixteen_pairs() -> None:
    assert len(list(itertools.product(DATA_CLASS_ORDER, repeat=2))) == 16


def test_higher_class_never_lowers_a_class() -> None:
    assert higher_class("special", "synthetic") == "special"
    assert higher_class("internal", "personal") == "personal"


@pytest.mark.parametrize("name", DATA_CLASS_ORDER)
def test_parse_data_class_accepts_each_exact_name(name: str) -> None:
    assert parse_data_class(name) == name


@pytest.mark.parametrize(
    "value", ["Personal", " personal", "personal ", "SPECIAL", "", "public", "none"]
)
def test_parse_data_class_refuses_anything_but_an_exact_name(value: str) -> None:
    assert parse_data_class(value) is None
