"""The generated JSON Schemas: committed files equal the models' output."""

import json
from pathlib import Path

from meridian.platform.registry.models import FILE_MODELS
from meridian.platform.registry.schemas import (
    render_schemas,
    stale_schemas,
    write_schemas,
)


def test_committed_schemas_equal_the_generated_ones(real_registry: Path) -> None:
    for name, text in render_schemas().items():
        committed = (real_registry / "schemas" / name).read_text(encoding="utf-8")
        assert committed == text, f"{name} is stale: run `meridian registry schemas`"


def test_there_is_one_schema_per_registry_file(real_registry: Path) -> None:
    committed = sorted(p.name for p in (real_registry / "schemas").iterdir())

    assert committed == sorted(f"{stem}.schema.json" for stem in FILE_MODELS)


def test_every_schema_forbids_unknown_keys() -> None:
    for name, text in render_schemas().items():
        assert json.loads(text)["additionalProperties"] is False, name


def test_every_registry_file_points_at_its_schema(real_registry: Path) -> None:
    for stem in FILE_MODELS:
        first_line = (real_registry / f"{stem}.yaml").read_text().splitlines()[0]

        assert first_line == (
            f"# yaml-language-server: $schema=schemas/{stem}.schema.json"
        )


def test_stale_schema_is_detected_and_write_repairs_it(registry_copy: Path) -> None:
    target = registry_copy / "schemas" / "tools.schema.json"
    target.write_text("{}\n", encoding="utf-8")
    (registry_copy / "schemas" / "tenants.schema.json").unlink()

    assert stale_schemas(registry_copy) == ("tools.schema.json", "tenants.schema.json")
    assert write_schemas(registry_copy) == ("tools.schema.json", "tenants.schema.json")
    assert stale_schemas(registry_copy) == ()
