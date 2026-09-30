"""Generate the committed JSON Schemas from the Pydantic models."""

import json
from pathlib import Path

from meridian.platform.registry.models import FILE_MODELS

SCHEMAS_SUBDIR = "schemas"


def render_schemas() -> dict[str, str]:
    """File name to serialised schema; deterministic across runs."""
    return {
        f"{stem}.schema.json": json.dumps(
            model.model_json_schema(), indent=2, sort_keys=True
        )
        + "\n"
        for stem, model in FILE_MODELS.items()
    }


def stale_schemas(registry_dir: Path) -> tuple[str, ...]:
    """Names of committed schema files that differ from the models or are absent."""
    target = registry_dir / SCHEMAS_SUBDIR
    stale = []
    for name, text in render_schemas().items():
        path = target / name
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            stale.append(name)
    return tuple(stale)


def write_schemas(registry_dir: Path) -> tuple[str, ...]:
    """Write every schema; return the names of the files that changed."""
    target = registry_dir / SCHEMAS_SUBDIR
    target.mkdir(exist_ok=True)
    changed = stale_schemas(registry_dir)
    rendered = render_schemas()
    for name in changed:
        (target / name).write_text(rendered[name], encoding="utf-8")
    return changed
