"""The loader: files, YAML strictness, schema errors and error collection."""

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from registrysupport import Change, add_field, apply_changes, planted, set_field

from meridian.platform.registry import RegistryError, load_registry
from meridian.platform.registry.loader import UniqueKeyLoader
from meridian.platform.registry.terraform import (
    compare_with_terraform,
    read_terraform_outputs,
)

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]

# The first deployment, found by its key (registrysupport).
GPT4O = "aoai-sdc-gpt-4o"


def yaml_length(directory: Path, name: str, key: str) -> int:
    return len(yaml.safe_load((directory / name).read_text(encoding="utf-8"))[key])


def test_committed_registry_validates_clean(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    assert len(registry.providers) == yaml_length(
        real_registry, "providers.yaml", "providers"
    )
    assert len(registry.deployments) == yaml_length(
        real_registry, "models.yaml", "deployments"
    )
    assert len(registry.tools) == yaml_length(real_registry, "tools.yaml", "tools")
    assert len(registry.agents) == yaml_length(real_registry, "agents.yaml", "agents")
    assert len(registry.tenants) == yaml_length(
        real_registry, "tenants.yaml", "tenants"
    )
    assert len(registry.services) == yaml_length(
        real_registry, "services.yaml", "services"
    )
    assert registry.deployments


def test_committed_registry_matches_the_committed_terraform_snapshot(
    real_registry: Path, snapshot_path: Path
) -> None:
    registry = load_registry(real_registry)
    outputs = read_terraform_outputs(snapshot_path)

    assert compare_with_terraform(registry, outputs) == ()


def test_yaml_price_3_025_loads_as_exactly_3_025(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    deployment = registry.deployment("aoai-sdc-gpt-4o")

    assert deployment is not None
    price = deployment.price
    assert price.input_per_million_tokens == Decimal("3.025")
    assert price.output_per_million_tokens == Decimal("12.10")


def test_embedding_has_no_output_price(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    embedding = registry.deployment("aoai-sdc-text-embedding-3-large")

    assert embedding is not None
    assert embedding.price.output_per_million_tokens is None


def test_unknown_key_is_reported_with_file_and_path(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = apply_changes(plant(), add_field(GPT4O, "bogus", "1"))

    errors = load_errors(directory)

    assert errors == (
        "models.yaml: deployments[0].bogus: Extra inputs are not permitted",
    )


def test_repeated_yaml_key_is_reported_with_its_line(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "models.yaml",
            "  - id: aoai-sdc-gpt-4o\n",
            "  - id: aoai-sdc-gpt-4o\n    id: other\n",
        )
    )

    errors = load_errors(directory)

    assert errors == ("models.yaml: line 6: repeated key 'id'",)


def test_yaml_syntax_error_is_reported_not_raised(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(("agents.yaml", "agents:\n", "agents: [\n"))

    errors = load_errors(directory)

    assert len(errors) == 1
    assert errors[0].startswith("agents.yaml: line ")


def test_yaml_python_tags_are_refused(plant: Plant, load_errors: LoadErrors) -> None:
    directory = plant(
        ("agents.yaml", "agents:\n", "agents: !!python/object/apply:os.getcwd []\n")
    )

    errors = load_errors(directory)

    assert len(errors) == 1
    assert errors[0].startswith("agents.yaml: line ")


def test_unique_key_loader_is_a_safe_loader() -> None:
    assert issubclass(UniqueKeyLoader, yaml.SafeLoader)


def test_extra_yaml_file_is_reported(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    (registry_copy / "model.yml").write_text("deployments: []\n", encoding="utf-8")

    errors = load_errors(registry_copy)

    assert len(errors) == 1
    assert errors[0].startswith("model.yml: not a registry file")


def test_yaml_in_schemas_and_snapshots_subdirectories_is_ignored(
    registry_copy: Path,
) -> None:
    (registry_copy / "snapshots" / "note.yaml").write_text("a: 1\n", encoding="utf-8")
    (registry_copy / "schemas" / "note.yaml").write_text("a: 1\n", encoding="utf-8")

    registry = load_registry(registry_copy)

    assert registry.deployments


def test_missing_file_is_reported(registry_copy: Path, load_errors: LoadErrors) -> None:
    (registry_copy / "tenants.yaml").unlink()

    errors = load_errors(registry_copy)

    assert errors == ("tenants.yaml: file is missing",)


def test_missing_directory_is_reported(tmp_path: Path, load_errors: LoadErrors) -> None:
    errors = load_errors(tmp_path / "nowhere")

    assert len(errors) == 1
    assert "registry directory not found" in errors[0]


def test_invalid_id_is_reported(plant: Plant, load_errors: LoadErrors) -> None:
    directory = plant(("providers.yaml", "id: azure-openai", "id: Azure_OpenAI"))

    errors = load_errors(directory)

    assert any(e.startswith("providers.yaml: providers[0].id:") for e in errors)


def test_errors_from_several_files_are_all_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = planted(
        plant,
        set_field(GPT4O, "residency", "mars"),
        ("agents.yaml", "agents:\n", "agents:\n  - {id: x, tools: [], extra: 1}\n"),
    )

    errors = load_errors(directory)

    assert any(e.startswith("models.yaml: deployments[0].residency:") for e in errors)
    assert any(e.startswith("agents.yaml: agents[0]") for e in errors)


def test_semantic_checks_do_not_run_when_a_file_fails_validation(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        ("providers.yaml", "id: azure-openai", "id: Azure_OpenAI"),
        ("tenants.yaml", "agents: [claims-triage]", "agents: [ghost]"),
    )

    errors = load_errors(directory)

    assert all("unknown agent" not in e for e in errors)


def test_registry_error_carries_every_message_as_a_tuple(
    registry_copy: Path,
) -> None:
    (registry_copy / "tenants.yaml").unlink()
    (registry_copy / "agents.yaml").unlink()

    with pytest.raises(RegistryError) as raised:
        load_registry(registry_copy)

    assert raised.value.errors == (
        "agents.yaml: file is missing",
        "tenants.yaml: file is missing",
    )


def test_top_level_value_of_the_wrong_type_uses_the_file_path(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    (registry_copy / "agents.yaml").write_text("[]\n", encoding="utf-8")

    errors = load_errors(registry_copy)

    assert len(errors) == 1
    assert errors[0].startswith("agents.yaml: (file): ")


def test_empty_file_uses_the_file_path(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    (registry_copy / "agents.yaml").write_text("", encoding="utf-8")

    errors = load_errors(registry_copy)

    assert len(errors) == 1
    assert errors[0].startswith("agents.yaml: (file): ")


@pytest.mark.parametrize(
    ("edit", "path"),
    [
        pytest.param(
            set_field(GPT4O, "version", "1"),
            "models.yaml: deployments[0].version",
            id="int-for-version",
        ),
        pytest.param(
            set_field(GPT4O, "version", "2024-11-20"),
            "models.yaml: deployments[0].version",
            id="date-for-version",
        ),
        pytest.param(
            (
                "providers.yaml",
                "description: Azure OpenAI; Entra ID only, keys off (S007).",
                "description: no",
            ),
            "providers.yaml: providers[0].description",
            id="no-is-a-boolean",
        ),
        pytest.param(
            ("providers.yaml", "id: azure-openai", "id: yes"),
            "providers.yaml: providers[0].id",
            id="yes-is-a-boolean",
        ),
    ],
)
def test_yaml_scalars_are_not_coerced_into_strings(
    plant: Plant,
    load_errors: LoadErrors,
    edit: tuple[str, str, str] | Change,
    path: str,
) -> None:
    errors = load_errors(planted(plant, edit))

    assert any(
        e.startswith(f"{path}: Input should be a valid string") for e in errors
    ), errors


@pytest.mark.parametrize(
    ("edit", "bad_date"),
    [
        pytest.param(
            set_field(GPT4O, "retires", "2027-02-30"),
            "2027-02-30",
            id="february-30",
        ),
        pytest.param(
            set_field(GPT4O, "price.checked", "2026-13-45"),
            "2026-13-45",
            id="month-13",
        ),
    ],
)
def test_calendar_invalid_date_is_an_error_line_not_a_traceback(
    plant: Plant, load_errors: LoadErrors, edit: Change, bad_date: str
) -> None:
    errors = load_errors(apply_changes(plant(), edit))

    assert len(errors) == 1
    assert errors[0].startswith("models.yaml: line ")
    assert f"invalid date '{bad_date}'" in errors[0]


def test_deeply_nested_input_is_reported_not_raised(
    plant: Plant, load_errors: LoadErrors
) -> None:
    depth = 5000
    directory = plant(
        ("agents.yaml", "agents:\n", "agents: " + "[" * depth + "]" * depth + "\n# ")
    )

    errors = load_errors(directory)

    assert len(errors) == 1
    assert errors[0].startswith("agents.yaml: ")


def test_null_byte_is_reported_with_its_line(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(("agents.yaml", "agents:\n", "agents:\n# \x00\n"))

    errors = load_errors(directory)

    assert len(errors) == 1
    assert errors[0].startswith(
        "agents.yaml: line 4: special characters are not allowed"
    )


def test_invalid_utf8_is_reported_not_raised(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    (registry_copy / "agents.yaml").write_bytes(b"agents: \xff\xfe\n")

    errors = load_errors(registry_copy)

    assert len(errors) == 1
    assert errors[0].startswith("agents.yaml: cannot read: ")


@pytest.mark.parametrize(
    "edits",
    [
        pytest.param(
            [("tenants.yaml", "agents: [claims-triage]", "agents: &a [claims-triage]")],
            id="anchor",
        ),
        pytest.param(
            [("tenants.yaml", "agents: [claims-triage]", "agents: *a")],
            id="alias",
        ),
        pytest.param(
            [("tenants.yaml", "data_class: personal", "data_class: &c personal")],
            id="anchor-on-a-scalar",
        ),
    ],
)
def test_anchors_and_aliases_are_refused_with_their_line(
    plant: Plant, load_errors: LoadErrors, edits: list[tuple[str, str, str]]
) -> None:
    errors = load_errors(plant(*edits))

    assert len(errors) == 1
    assert errors[0].startswith("tenants.yaml: line ")
    assert errors[0].endswith(": anchors and aliases are not allowed")


def test_anchor_error_names_the_right_line(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "tenants.yaml",
            "    description: The claims workload in production use.\n",
            "    description: &d x\n",
        )
    )

    errors = load_errors(directory)

    assert errors == ("tenants.yaml: line 18: anchors and aliases are not allowed",)


def test_merge_keys_are_refused(plant: Plant, load_errors: LoadErrors) -> None:
    directory = plant(
        (
            "providers.yaml",
            "    kind: replay\n",
            "    kind: replay\n    <<: {description: x}\n",
        )
    )

    errors = load_errors(directory)

    assert len(errors) == 1
    assert errors[0].startswith("providers.yaml: line ")
    assert errors[0].endswith(": merge keys are not allowed")
