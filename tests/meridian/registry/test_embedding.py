"""What the registry holds for embeddings: dimensions and the checks on them (S045).

Vectors of different models, versions or sizes are not comparable (T-54), and
an embedding route's candidates add to the tenants' windows like a chat
route's do (T-55). Each check gets a planted violation, and the boundary of
each rule gets both sides.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from registrysupport import (
    Change,
    add_field,
    apply_changes,
    remove_field,
    set_field,
)
from servicesupport import REPLAY_ENTRY, SECOND_EMBEDDING_YAML, pin_embedding_route

from meridian.platform.registry import load_registry

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]

# The entries these tests edit, found by their keys (registrysupport).
EMBEDDING = "aoai-sdc-text-embedding-3-large"
SECOND = "aoai-sdc-text-embedding-3-large-second"
GPT4O_B = "aoai-sdc-gpt-4o-b"
AZURE_CHAT = "aoai-sdc-gpt-4o"
REPLAY_CHAT = "replay-chat"
REPLAY_EMBEDDING = "replay-embedding"


def with_second_candidate(plant: Plant, *changes: Change) -> Path:
    """A registry whose embedding route lists the real deployment and a second
    one; each change edits a field of the second one's entry (its key is
    ``SECOND``)."""
    directory = plant(
        ("models.yaml", REPLAY_ENTRY, SECOND_EMBEDDING_YAML + REPLAY_ENTRY)
    )
    pin_embedding_route(directory, EMBEDDING, SECOND)
    return apply_changes(directory, *changes)


# ── the real registry ───────────────────────────────────────────────────────
def test_the_seeded_embedding_deployments_carry_1024_and_the_chat_ones_none(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)

    by_id = {d.id: d.dimensions for d in registry.deployments}

    assert by_id == {
        "aoai-sdc-gpt-4o": None,
        "aoai-sdc-gpt-4o-b": None,
        "aoai-sdc-text-embedding-3-large": 1024,
        "replay-chat": None,
        "replay-embedding": 1024,
        "recorded-chat": None,
    }


# ── the field ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("dimensions", [1, 2000])
def test_dimensions_at_the_limits_are_accepted(plant: Plant, dimensions: int) -> None:
    directory = apply_changes(
        plant(),
        set_field(EMBEDDING, "dimensions", str(dimensions)),
        # the replay deployment must say the same, or the replay check refuses
        set_field(REPLAY_EMBEDDING, "dimensions", str(dimensions)),
    )

    registry = load_registry(directory)

    found = registry.deployment(EMBEDDING)
    assert found is not None
    assert found.dimensions == dimensions


@pytest.mark.parametrize("dimensions", ["0", "-1", "2001", "1.5", "wide"])
def test_dimensions_outside_one_to_2000_or_not_a_whole_number_fail_to_load(
    plant: Plant, load_errors: LoadErrors, dimensions: str
) -> None:
    errors = load_errors(
        apply_changes(plant(), set_field(EMBEDDING, "dimensions", dimensions))
    )

    assert len(errors) == 1
    assert errors[0].startswith("models.yaml: deployments[2].dimensions: ")


# ── an embedding deployment has dimensions, a chat one has none ─────────────
@pytest.mark.parametrize(
    ("change", "index", "deployment"),
    [
        pytest.param(remove_field(EMBEDDING, "dimensions"), 2, EMBEDDING, id="azure"),
        pytest.param(
            remove_field(REPLAY_EMBEDDING, "dimensions"),
            4,
            "replay-embedding",
            id="replay",
        ),
    ],
)
def test_an_embedding_deployment_without_dimensions_is_reported(
    plant: Plant,
    load_errors: LoadErrors,
    change: Change,
    index: int,
    deployment: str,
) -> None:
    errors = load_errors(apply_changes(plant(), change))

    assert errors == (
        f"models.yaml: deployments[{index}].dimensions: required for purpose "
        f"'embedding' (deployment {deployment!r})",
    )


@pytest.mark.parametrize(
    ("change", "index", "deployment"),
    [
        pytest.param(
            add_field(AZURE_CHAT, "dimensions", "1024"),
            0,
            "aoai-sdc-gpt-4o",
            id="azure",
        ),
        pytest.param(
            add_field(REPLAY_CHAT, "dimensions", "1024"),
            3,
            "replay-chat",
            id="replay",
        ),
    ],
)
def test_a_chat_deployment_with_dimensions_is_reported(
    plant: Plant,
    load_errors: LoadErrors,
    change: Change,
    index: int,
    deployment: str,
) -> None:
    errors = load_errors(apply_changes(plant(), change))

    assert errors == (
        f"models.yaml: deployments[{index}].dimensions: must not be set for "
        f"purpose 'chat' (deployment {deployment!r})",
    )


# ── one model, one version, one size on the embedding route (T-54) ──────────
def test_a_second_candidate_of_the_same_model_version_and_size_is_accepted(
    plant: Plant,
) -> None:
    registry = load_registry(with_second_candidate(plant))

    route = registry.route("embedding")
    assert route is not None
    assert route.candidates == (EMBEDDING, SECOND)


@pytest.mark.parametrize(
    ("change", "field", "value", "first_value"),
    [
        pytest.param(
            set_field(SECOND, "model", "other"),
            "model",
            "'other'",
            "'text-embedding-3-large'",
            id="model",
        ),
        pytest.param(
            set_field(SECOND, "version", '"2"'),
            "version",
            "'2'",
            "'1'",
            id="version",
        ),
        pytest.param(
            set_field(SECOND, "dimensions", "2000"),
            "dimensions",
            "2000",
            "1024",
            id="dimensions",
        ),
    ],
)
def test_candidates_of_the_embedding_route_that_differ_are_reported(
    plant: Plant,
    load_errors: LoadErrors,
    change: Change,
    field: str,
    value: str,
    first_value: str,
) -> None:
    errors = load_errors(with_second_candidate(plant, change))

    assert (
        f"policies.yaml: routes[1].candidates[1]: deployment {SECOND!r} has "
        f"{field} {value}, but the route's first candidate {EMBEDDING!r} has "
        f"{first_value}; vectors of different models or sizes are not "
        "comparable (T-54)"
    ) in errors


def test_every_difference_is_reported_not_only_the_first(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = with_second_candidate(
        plant,
        set_field(SECOND, "model", "other"),
        set_field(SECOND, "version", '"2"'),
    )

    errors = load_errors(directory)

    assert len(errors) == 2
    assert "has model 'other'" in errors[0]
    assert "has version '2'" in errors[1]


def test_a_chat_route_may_mix_models(plant: Plant) -> None:
    # T-54 is about vectors; two chat models answer the same question.
    directory = apply_changes(
        plant(),
        set_field(GPT4O_B, "model", "gpt-4o-mini"),
        set_field(GPT4O_B, "version", '"2024-07-18"'),
    )

    registry = load_registry(directory)

    route = registry.route("chat")
    assert route is not None
    found = [registry.deployment(name) for name in route.candidates]
    assert {d.model for d in found if d is not None} == {"gpt-4o", "gpt-4o-mini"}


# ── replay returns vectors of the route's size (T-54) ───────────────────────
def test_a_replay_embedding_of_another_size_than_the_route_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = apply_changes(plant(), set_field(REPLAY_EMBEDDING, "dimensions", "768"))

    errors = load_errors(directory)

    assert errors == (
        "policies.yaml: replay[1].deployment: deployment 'replay-embedding' has "
        f"dimensions 768, but route candidate {EMBEDDING!r} has 1024; simulated "
        "vectors must be the size of the route's (T-54)",
    )


def test_a_replay_embedding_is_compared_with_every_candidate(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = with_second_candidate(plant, set_field(SECOND, "dimensions", "2000"))

    errors = load_errors(directory)

    # The candidates differ from each other, and replay differs from the second.
    assert len(errors) == 2
    assert errors[0].startswith("policies.yaml: routes[1].candidates[1]: ")
    assert errors[1].startswith("policies.yaml: replay[1].deployment: ")
    assert f"route candidate {SECOND!r} has 2000" in errors[1]


# ── the tenants' windows fit every route's candidates (T-55) ────────────────
@pytest.mark.parametrize(
    ("field", "limit"),
    [("requests_per_10_seconds", 20), ("tokens_per_minute", 20000)],
)
def test_an_embedding_candidate_the_tenants_could_exhaust_is_reported(
    plant: Plant, load_errors: LoadErrors, field: str, limit: int
) -> None:
    lowered = set_field(EMBEDDING, f"rate_limits.{field}", str(limit - 1))

    errors = load_errors(apply_changes(plant(), lowered))

    assert errors == (
        f"policies.yaml: routes[1].candidates[0]: deployment {EMBEDDING!r} allows "
        f"{limit - 1} {field} but the tenants' limits add up to {limit}",
    )


def test_every_candidate_of_the_embedding_route_is_compared(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = with_second_candidate(
        plant, set_field(SECOND, "rate_limits.requests_per_10_seconds", "5")
    )

    errors = load_errors(directory)

    assert errors == (
        f"policies.yaml: routes[1].candidates[1]: deployment {SECOND!r} allows 5 "
        "requests_per_10_seconds but the tenants' limits add up to 20",
    )
