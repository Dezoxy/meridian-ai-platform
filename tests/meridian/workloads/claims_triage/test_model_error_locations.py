"""A guard for ``triaging.invalid_fields`` (S060).

A log line that lists a validation error's fields is safe only while every
location in it is a name the model declares. A field typed as a mapping with free
keys puts the data's own key in an error's location, and a discriminated union
puts the data's own tag there; ``invalid_fields`` knows only ``extra_forbidden``
(it replaces that key by ``*``). So the models whose errors reach it must declare
neither, and this walks them, and what they nest, to say so.
"""

import typing
from collections.abc import Mapping
from enum import Enum
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Discriminator, Field
from pydantic.fields import FieldInfo

from meridian.workloads.claims_triage.models import ClaimFacts, ClaimSubmission
from meridian.workloads.claims_triage.proposal import TriageProposal

# The models whose ``ValidationError`` reaches ``invalid_fields``: the stored
# submission, the facts the graph is sent and the proposal the run returns.
LOGGED_MODELS = (ClaimSubmission, ClaimFacts, TriageProposal)


def key_is_declared(key: object) -> bool:
    """Whether a mapping's keys come from the model: a ``Literal`` or an enum."""
    if typing.get_origin(key) is Literal:
        return True
    return isinstance(key, type) and issubclass(key, Enum)


def discriminates(metadata: object) -> bool:
    if isinstance(metadata, Discriminator):
        return True
    return isinstance(metadata, FieldInfo) and metadata.discriminator is not None


def annotation_problems(annotation: object, where: str, seen: set[type]) -> list[str]:
    """What in ``annotation`` would put the data's own key or tag in a location."""
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        base, *metadata = typing.get_args(annotation)
        found = [
            f"{where}: a discriminated union" for m in metadata if discriminates(m)
        ]
        return found + annotation_problems(base, where, seen)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return model_problems(annotation, seen)
    if origin is Literal:
        return []
    if annotation is dict or (isinstance(origin, type) and issubclass(origin, Mapping)):
        args = typing.get_args(annotation)
        found = [] if args and key_is_declared(args[0]) else [f"{where}: a free key"]
        return found + [
            p for a in args[1:] for p in annotation_problems(a, where, seen)
        ]
    return [
        p
        for a in typing.get_args(annotation)
        for p in annotation_problems(a, where, seen)
    ]


def model_problems(model: type[BaseModel], seen: set[type] | None = None) -> list[str]:
    """The fields of ``model`` and of the models it nests that declare a mapping
    with free keys or a discriminated union."""
    seen = set() if seen is None else seen
    if model in seen:
        return []
    seen.add(model)
    problems: list[str] = []
    for name, info in model.model_fields.items():
        where = f"{model.__name__}.{name}"
        if info.discriminator is not None:
            problems.append(f"{where}: a discriminated union")
        problems += [
            f"{where}: a discriminated union" for m in info.metadata if discriminates(m)
        ]
        problems += annotation_problems(info.annotation, where, seen)
    return problems


@pytest.mark.parametrize("model", LOGGED_MODELS, ids=lambda m: m.__name__)
def test_a_model_whose_errors_are_logged_by_field_declares_no_free_key_and_no_tag(
    model: type[BaseModel],
) -> None:
    assert model_problems(model) == []


# ── the guard can fail ──────────────────────────────────────────────────────
class Cat(BaseModel):
    kind: Literal["cat"]


class Dog(BaseModel):
    kind: Literal["dog"]


class FreeKeys(BaseModel):
    labels: dict[str, int]


class FreeKeysInAList(BaseModel):
    labels: tuple[Mapping[str, str], ...]


class FreeKeysBelow(BaseModel):
    inner: FreeKeys | None


class TaggedByField(BaseModel):
    pet: Cat | Dog = Field(discriminator="kind")


class TaggedInAList(BaseModel):
    pets: list[Annotated[Cat | Dog, Field(discriminator="kind")]]


class TaggedByDiscriminator(BaseModel):
    pet: Annotated[Cat | Dog, Discriminator("kind")]


class DeclaredKeys(BaseModel):
    counts: dict[Literal["a", "b"], int]
    pet: Cat | Dog


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        pytest.param(FreeKeys, "FreeKeys.labels: a free key", id="a-dict"),
        pytest.param(
            FreeKeysInAList, "FreeKeysInAList.labels: a free key", id="a-mapping"
        ),
        pytest.param(FreeKeysBelow, "FreeKeys.labels: a free key", id="a-nested-model"),
        pytest.param(
            TaggedByField, "TaggedByField.pet: a discriminated union", id="a-field"
        ),
        pytest.param(
            TaggedInAList,
            "TaggedInAList.pets: a discriminated union",
            id="inside-a-list",
        ),
        pytest.param(
            TaggedByDiscriminator,
            "TaggedByDiscriminator.pet: a discriminated union",
            id="a-discriminator",
        ),
    ],
)
def test_the_guard_finds_a_free_key_or_a_tag_however_it_is_declared(
    model: type[BaseModel], expected: str
) -> None:
    assert expected in model_problems(model)


def test_the_guard_leaves_declared_keys_and_a_plain_union_alone() -> None:
    assert model_problems(DeclaredKeys) == []
