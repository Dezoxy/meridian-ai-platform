"""The Terraform comparison (T-12): registry facts against the outputs."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from meridian.platform.registry import (
    RegistryError,
    compare_with_terraform,
    load_registry,
)
from meridian.platform.registry.models import Registry
from meridian.platform.registry.terraform import azure_deployments

GPT4O = "sdc/gpt-4o"


@pytest.fixture
def registry(real_registry: Path) -> Registry:
    return load_registry(real_registry)


@pytest.fixture
def outputs(snapshot_path: Path) -> dict[str, Any]:
    return json.loads(snapshot_path.read_text(encoding="utf-8"))


def test_matching_outputs_produce_no_messages(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    assert compare_with_terraform(registry, outputs) == ()


def test_sku_mismatch_names_the_field_and_both_values(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    changed = copy.deepcopy(outputs)
    changed[GPT4O]["sku_name"] = "DataZoneStandard"

    messages = compare_with_terraform(registry, changed)

    assert messages == (
        "models.yaml: deployment 'aoai-sdc-gpt-4o': sku is 'Standard' in the "
        "registry but 'DataZoneStandard' in Terraform (key 'sdc/gpt-4o')",
    )


@pytest.mark.parametrize(
    ("output_key", "value", "field"),
    [
        ("model_name", "gpt-4.1-mini", "model"),
        ("model_version", "2025-04-14", "version"),
        ("deployment_name", "chat", "deployment_name"),
        ("purpose", "embedding", "purpose"),
        ("location", "westeurope", "region"),
    ],
)
def test_each_compared_field_reports_its_own_mismatch(
    registry: Registry,
    outputs: dict[str, Any],
    output_key: str,
    value: str,
    field: str,
) -> None:
    changed = copy.deepcopy(outputs)
    changed[GPT4O][output_key] = value

    messages = compare_with_terraform(registry, changed)

    assert len(messages) == 1
    assert f": {field} is " in messages[0]
    assert f"but {value!r} in Terraform" in messages[0]


def test_missing_terraform_key_is_reported(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    changed = {k: v for k, v in outputs.items() if k != GPT4O}

    messages = compare_with_terraform(registry, changed)

    assert messages == (
        "models.yaml: deployment 'aoai-sdc-gpt-4o': terraform_key 'sdc/gpt-4o' "
        "is not in the Terraform outputs",
    )


def test_output_key_not_in_the_registry_is_reported(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    changed = {**outputs, "gwc/gpt-4o": outputs[GPT4O]}

    messages = compare_with_terraform(registry, changed)

    assert messages == (
        "terraform outputs: 'gwc/gpt-4o' is deployed but not in the registry",
    )


def test_extra_keys_in_live_output_are_ignored(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    live = copy.deepcopy(outputs)
    for entry in live.values():
        entry["account_name"] = "example-account"
        entry["endpoint"] = "https://example.invalid/"

    assert compare_with_terraform(registry, live) == ()


def test_azure_location_spelling_is_normalised(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    changed = copy.deepcopy(outputs)
    changed[GPT4O]["location"] = "Sweden Central"

    assert compare_with_terraform(registry, changed) == ()


def test_missing_field_in_an_output_entry_is_a_mismatch(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    changed = copy.deepcopy(outputs)
    del changed[GPT4O]["sku_name"]

    messages = compare_with_terraform(registry, changed)

    assert len(messages) == 1
    assert (
        "sku is 'Standard' in the registry but '<missing>' in Terraform" in messages[0]
    )


def test_reading_a_missing_file_is_a_registry_error(tmp_path: Path) -> None:
    from meridian.platform.registry.terraform import read_terraform_outputs

    with pytest.raises(RegistryError) as raised:
        read_terraform_outputs(tmp_path / "absent.json")

    assert "cannot read Terraform outputs" in raised.value.errors[0]


@pytest.mark.parametrize(
    ("content", "fragment"),
    [("{not json", "not valid JSON"), ("[1, 2]", "expected a JSON object")],
)
def test_reading_a_malformed_file_is_a_registry_error(
    tmp_path: Path, content: str, fragment: str
) -> None:
    from meridian.platform.registry.terraform import read_terraform_outputs

    path = tmp_path / "outputs.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(RegistryError) as raised:
        read_terraform_outputs(path)

    assert fragment in raised.value.errors[0]


def test_azure_deployment_without_a_terraform_key_is_an_error(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    keyless = tuple(
        d.model_copy(update={"terraform_key": None}) if d.id == "aoai-sdc-gpt-4o" else d
        for d in registry.deployments
    )
    bypassed = registry.model_copy(update={"deployments": keyless})

    messages = compare_with_terraform(bypassed, outputs)

    assert "an azure-openai deployment needs a terraform_key" in messages[0]
    assert (
        "terraform outputs: 'sdc/gpt-4o' is deployed but not in the registry"
        in messages
    )


def test_compared_deployments_are_exactly_the_azure_ones_in_the_outputs(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    compared = azure_deployments(registry)

    assert {d.terraform_key for d in compared} == set(outputs)
    assert all(d.provider == "azure-openai" for d in compared)
    assert len(compared) < len(registry.deployments)  # replay is not compared
