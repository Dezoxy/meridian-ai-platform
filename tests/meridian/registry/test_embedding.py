"""What the registry holds for embeddings: dimensions and the checks on them (S045).

Vectors of different models, versions or sizes are not comparable (T-54), and
an embedding route's candidates add to the tenants' windows like a chat
route's do (T-55). Each check gets a planted violation, and the boundary of
each rule gets both sides.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from servicesupport import REPLAY_ENTRY, SECOND_EMBEDDING_YAML, pin_embedding_route

from meridian.platform.registry import load_registry

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]
Edit = tuple[str, str, str]

EMBEDDING = "aoai-sdc-text-embedding-3-large"
SECOND = "aoai-sdc-text-embedding-3-large-second"
DIMENSIONS_LINE = "    dimensions: 1024\n"
# The anchors of the first chat deployment and of the two replay ones.
AZURE_CHAT_VERSION = '    version: "2024-11-20"\n'
REPLAY_CHAT_MODEL = "    model: replay-chat\n"
REPLAY_EMBEDDING_ANCHOR = '    model: replay-embedding\n    version: "1"\n'
REPLAY_EMBEDDING_DIMENSIONS = REPLAY_EMBEDDING_ANCHOR + DIMENSIONS_LINE
EMBEDDING_RATE_LIMITS = (
    "    terraform_key: sdc/text-embedding-3-large\n"
    "    rate_limits:\n"
    "      requests_per_10_seconds: 20\n"
    "      tokens_per_minute: 20000\n"
)


def with_second_candidate(plant: Plant, *edits: Edit) -> Path:
    """A registry whose embedding route lists the real deployment and a second
    one; each edit changes the first match inside the second one's entry."""
    directory = plant(
        ("models.yaml", REPLAY_ENTRY, SECOND_EMBEDDING_YAML + REPLAY_ENTRY)
    )
    pin_embedding_route(directory, EMBEDDING, SECOND)
    path = directory / "models.yaml"
    for _name, old, new in edits:
        text = path.read_text(encoding="utf-8")
        head, found, tail = text.partition("  - id: " + SECOND)
        assert old in tail, f"the {SECOND} entry has no {old!r} to replace"
        path.write_text(head + found + tail.replace(old, new, 1), encoding="utf-8")
    return directory


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
    directory = plant(
        ("models.yaml", DIMENSIONS_LINE, f"    dimensions: {dimensions}\n"),
        # the replay deployment must say the same, or the replay check refuses
        (
            "models.yaml",
            REPLAY_EMBEDDING_DIMENSIONS,
            REPLAY_EMBEDDING_ANCHOR + f"    dimensions: {dimensions}\n",
        ),
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
        plant(("models.yaml", DIMENSIONS_LINE, f"    dimensions: {dimensions}\n"))
    )

    assert len(errors) == 1
    assert errors[0].startswith("models.yaml: deployments[2].dimensions: ")


# ── an embedding deployment has dimensions, a chat one has none ─────────────
@pytest.mark.parametrize(
    ("edit", "index", "deployment"),
    [
        pytest.param(("models.yaml", DIMENSIONS_LINE, ""), 2, EMBEDDING, id="azure"),
        pytest.param(
            ("models.yaml", REPLAY_EMBEDDING_DIMENSIONS, REPLAY_EMBEDDING_ANCHOR),
            4,
            "replay-embedding",
            id="replay",
        ),
    ],
)
def test_an_embedding_deployment_without_dimensions_is_reported(
    plant: Plant,
    load_errors: LoadErrors,
    edit: Edit,
    index: int,
    deployment: str,
) -> None:
    errors = load_errors(plant(edit))

    assert errors == (
        f"models.yaml: deployments[{index}].dimensions: required for purpose "
        f"'embedding' (deployment {deployment!r})",
    )


@pytest.mark.parametrize(
    ("edit", "index", "deployment"),
    [
        pytest.param(
            ("models.yaml", AZURE_CHAT_VERSION, AZURE_CHAT_VERSION + DIMENSIONS_LINE),
            0,
            "aoai-sdc-gpt-4o",
            id="azure",
        ),
        pytest.param(
            ("models.yaml", REPLAY_CHAT_MODEL, REPLAY_CHAT_MODEL + DIMENSIONS_LINE),
            3,
            "replay-chat",
            id="replay",
        ),
    ],
)
def test_a_chat_deployment_with_dimensions_is_reported(
    plant: Plant,
    load_errors: LoadErrors,
    edit: Edit,
    index: int,
    deployment: str,
) -> None:
    errors = load_errors(plant(edit))

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
    ("edit", "field", "value", "first_value"),
    [
        pytest.param(
            (
                "models.yaml",
                "    model: text-embedding-3-large\n",
                "    model: other\n",
            ),
            "model",
            "'other'",
            "'text-embedding-3-large'",
            id="model",
        ),
        pytest.param(
            ("models.yaml", '    version: "1"\n', '    version: "2"\n'),
            "version",
            "'2'",
            "'1'",
            id="version",
        ),
        pytest.param(
            ("models.yaml", DIMENSIONS_LINE, "    dimensions: 2000\n"),
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
    edit: Edit,
    field: str,
    value: str,
    first_value: str,
) -> None:
    errors = load_errors(with_second_candidate(plant, edit))

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
        ("models.yaml", "    model: text-embedding-3-large\n", "    model: other\n"),
        ("models.yaml", '    version: "1"\n', '    version: "2"\n'),
    )

    errors = load_errors(directory)

    assert len(errors) == 2
    assert "has model 'other'" in errors[0]
    assert "has version '2'" in errors[1]


def test_a_chat_route_may_mix_models(plant: Plant) -> None:
    # T-54 is about vectors; two chat models answer the same question.
    directory = plant(
        (
            "models.yaml",
            '    model: gpt-4o\n    version: "2024-11-20"\n'
            "    deployment_name: gpt-4o-b\n",
            '    model: gpt-4o-mini\n    version: "2024-07-18"\n'
            "    deployment_name: gpt-4o-b\n",
        )
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
    directory = plant(
        (
            "models.yaml",
            REPLAY_EMBEDDING_DIMENSIONS,
            REPLAY_EMBEDDING_ANCHOR + "    dimensions: 768\n",
        )
    )

    errors = load_errors(directory)

    assert errors == (
        "policies.yaml: replay[1].deployment: deployment 'replay-embedding' has "
        f"dimensions 768, but route candidate {EMBEDDING!r} has 1024; simulated "
        "vectors must be the size of the route's (T-54)",
    )


def test_a_replay_embedding_is_compared_with_every_candidate(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = with_second_candidate(
        plant, ("models.yaml", DIMENSIONS_LINE, "    dimensions: 2000\n")
    )

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
    lowered = EMBEDDING_RATE_LIMITS.replace(
        f"{field}: {limit}", f"{field}: {limit - 1}"
    )

    errors = load_errors(plant(("models.yaml", EMBEDDING_RATE_LIMITS, lowered)))

    assert errors == (
        f"policies.yaml: routes[1].candidates[0]: deployment {EMBEDDING!r} allows "
        f"{limit - 1} {field} but the tenants' limits add up to {limit}",
    )


def test_every_candidate_of_the_embedding_route_is_compared(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = with_second_candidate(
        plant,
        (
            "models.yaml",
            "      requests_per_10_seconds: 20\n",
            "      requests_per_10_seconds: 5\n",
        ),
    )

    errors = load_errors(directory)

    assert errors == (
        f"policies.yaml: routes[1].candidates[1]: deployment {SECOND!r} allows 5 "
        "requests_per_10_seconds but the tenants' limits add up to 20",
    )
