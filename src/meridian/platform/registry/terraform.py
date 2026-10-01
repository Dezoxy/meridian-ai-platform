"""Compare the registry's Azure deployments with Terraform's outputs (T-12).

The input is ``terraform output -json openai_deployments``: an object keyed by
``<location-alias>/<model>``. Keys beyond the compared ones are ignored, so the
live output (with account names and endpoints) and the committed snapshot
(without) both work.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from meridian.platform.registry.checks import MODELS
from meridian.platform.registry.loader import RegistryError
from meridian.platform.registry.models import Deployment, Registry

MISSING = "<missing>"
TOKENS_PER_MINUTE_PER_CAPACITY = 1000


def normalise_region(region: str) -> str:
    """Lower-case and drop spaces: "Sweden Central" and "swedencentral" match."""
    return region.lower().replace(" ", "")


def read_terraform_outputs(path: Path) -> Mapping[str, Any]:
    """Read the JSON file; a missing or malformed file is a ``RegistryError``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise RegistryError([f"{path}: cannot read Terraform outputs: {exc}"]) from exc
    except json.JSONDecodeError as exc:
        raise RegistryError([f"{path}: not valid JSON: {exc}"]) from exc
    if not isinstance(data, dict):
        raise RegistryError([f"{path}: expected a JSON object keyed by deployment"])
    return data


def _comparable(field: str, value: object) -> object:
    if field == "region" and isinstance(value, str):
        return normalise_region(value)
    return value


def _mismatches(dep: Deployment, entry: Mapping[str, Any]) -> list[str]:
    pairs = (
        ("model", dep.model, "model_name"),
        ("version", dep.version, "model_version"),
        ("sku", dep.sku, "sku_name"),
        ("deployment_name", dep.deployment_name, "deployment_name"),
        ("purpose", dep.purpose, "purpose"),
        ("region", dep.region, "location"),
    )
    errors: list[str] = []
    for field, ours, key in pairs:
        theirs = entry.get(key, MISSING)
        if _comparable(field, ours) != _comparable(field, theirs):
            errors.append(
                f"{MODELS}: deployment {dep.id!r}: {field} is {ours!r} in the "
                f"registry but {theirs!r} in Terraform (key {dep.terraform_key!r})"
            )
    return errors + _capacity_mismatches(dep, entry)


def _capacity_mismatches(dep: Deployment, entry: Mapping[str, Any]) -> list[str]:
    """Azure derives a deployment's token limit from its capacity: one capacity
    unit is 1,000 tokens per minute. An entry without ``capacity`` (a snapshot
    from before it was recorded) is not compared."""
    if "capacity" not in entry or dep.rate_limits is None:
        return []  # a missing rate_limits is reported by check_provider_fields
    capacity = entry["capacity"]
    where = f"{MODELS}: deployment {dep.id!r}"
    if isinstance(capacity, bool) or not isinstance(capacity, int):
        return [
            f"{where}: capacity is {capacity!r} in Terraform, not a whole number "
            f"(key {dep.terraform_key!r})"
        ]
    theirs = capacity * TOKENS_PER_MINUTE_PER_CAPACITY
    ours = dep.rate_limits.tokens_per_minute
    if ours == theirs:
        return []
    return [
        f"{where}: rate_limits.tokens_per_minute is {ours!r} in the registry but "
        f"{theirs!r} in Terraform (capacity {capacity} x "
        f"{TOKENS_PER_MINUTE_PER_CAPACITY}; key {dep.terraform_key!r})"
    ]


def azure_deployments(registry: Registry) -> tuple[Deployment, ...]:
    """The deployments Terraform is expected to know about."""
    return tuple(
        dep
        for dep in registry.deployments
        if (provider := registry.provider(dep.provider))
        and provider.kind == "azure-openai"
    )


def compare_with_terraform(
    registry: Registry, outputs: Mapping[str, Any]
) -> tuple[str, ...]:
    """Messages for every disagreement; empty when registry and Terraform match."""
    errors: list[str] = []
    referenced: set[str] = set()
    for dep in azure_deployments(registry):
        if not dep.terraform_key:
            errors.append(
                f"{MODELS}: deployment {dep.id!r}: an azure-openai deployment "
                "needs a terraform_key to be compared with Terraform"
            )
            continue
        referenced.add(dep.terraform_key)
        entry = outputs.get(dep.terraform_key)
        if not isinstance(entry, Mapping):
            errors.append(
                f"{MODELS}: deployment {dep.id!r}: terraform_key "
                f"{dep.terraform_key!r} is not in the Terraform outputs"
            )
            continue
        errors += _mismatches(dep, entry)
    errors += [
        f"terraform outputs: {key!r} is deployed but not in the registry"
        for key in sorted(set(outputs) - referenced)
    ]
    return tuple(errors)
