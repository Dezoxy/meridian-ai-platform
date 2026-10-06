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


class SchemaPathIsALink(OSError):
    """A schema's path is a symbolic link. The write would follow it and replace
    the file it points to, which may lie outside the registry directory, so the
    write refuses; the class name is all the command prints of it."""


def _differs(path: Path, text: str) -> bool:
    """Whether the file is absent, is a link (never read through: its target is
    not the committed file), is not UTF-8 text or holds other text."""
    if path.is_symlink() or not path.is_file():
        return True
    try:
        return path.read_text(encoding="utf-8") != text
    except UnicodeDecodeError:
        return True


def stale_schemas(registry_dir: Path) -> tuple[str, ...]:
    """Names of committed schema files that differ from the models, are absent,
    are links or cannot be decoded as text (the write replaces such a file, but
    refuses a link)."""
    target = registry_dir / SCHEMAS_SUBDIR
    return tuple(
        name for name, text in render_schemas().items() if _differs(target / name, text)
    )


def schemas_not_updated(registry_dir: Path, exc: OSError) -> str:
    """The problem of a schemas directory the process cannot update (the write
    reads each file before it replaces it, so the failure may be either, and a
    path that is a link is refused): the error's class, never its text (which
    holds a path). The write is not all-or-nothing, and a rerun repairs only what
    a failure that has passed left behind, so the line says to fix the directory
    first."""
    return (
        f"{registry_dir / SCHEMAS_SUBDIR}: schemas cannot be updated: "
        f"{type(exc).__name__}; "
        "fix the directory, then run `meridian registry schemas` again"
    )


def write_schemas(registry_dir: Path) -> tuple[str, ...]:
    """Write every schema; return the names of the files that changed. Raise
    ``SchemaPathIsALink`` before writing any of them when a path is a link."""
    target = registry_dir / SCHEMAS_SUBDIR
    target.mkdir(exist_ok=True)
    changed = stale_schemas(registry_dir)
    if any((target / name).is_symlink() for name in changed):
        raise SchemaPathIsALink
    rendered = render_schemas()
    for name in changed:
        (target / name).write_text(rendered[name], encoding="utf-8")
    return changed
