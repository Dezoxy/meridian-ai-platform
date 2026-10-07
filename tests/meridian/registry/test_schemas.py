"""The generated JSON Schemas: committed files equal the models' output."""

import json
from pathlib import Path

import pytest

from meridian.platform.registry.models import FILE_MODELS
from meridian.platform.registry.schemas import (
    SchemaPathIsALink,
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


def test_the_agents_schema_knows_workers_and_refuses_an_unknown_key_in_one(
    real_registry: Path,
) -> None:
    schema = json.loads(
        (real_registry / "schemas" / "agents.schema.json").read_text(encoding="utf-8")
    )

    worker = schema["$defs"]["Worker"]

    assert "workers" in schema["$defs"]["Agent"]["properties"]
    assert "workers" not in schema["$defs"]["Agent"].get("required", [])
    assert sorted(worker["required"]) == ["description", "id", "tools"]
    assert worker["additionalProperties"] is False


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


def test_the_write_refuses_a_schemas_directory_that_is_a_link_and_writes_nothing(
    registry_copy: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    schemas = registry_copy / "schemas"
    for file in schemas.iterdir():
        file.unlink()
    schemas.rmdir()
    schemas.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SchemaPathIsALink):
        write_schemas(registry_copy)

    assert list(outside.iterdir()) == []
    assert schemas.is_symlink()


def test_the_write_refuses_a_schemas_link_to_nothing_and_creates_nothing(
    registry_copy: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "not-there"
    schemas = registry_copy / "schemas"
    for file in schemas.iterdir():
        file.unlink()
    schemas.rmdir()
    schemas.symlink_to(missing, target_is_directory=True)

    with pytest.raises(SchemaPathIsALink):
        write_schemas(registry_copy)

    assert not missing.exists()
