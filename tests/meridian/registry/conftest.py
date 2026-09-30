"""Fixtures: a scratch copy of the real registry to plant one violation in."""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from meridian.platform.registry import RegistryError, load_registry

# (file name, text to find, replacement); the first occurrence is replaced.
Edit = tuple[str, str, str]


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@pytest.fixture
def real_registry(repo_root: Path) -> Path:
    return repo_root / "config" / "registry"


@pytest.fixture
def snapshot_path(real_registry: Path) -> Path:
    return real_registry / "snapshots" / "terraform-openai-deployments.json"


@pytest.fixture
def registry_copy(real_registry: Path, tmp_path: Path) -> Path:
    """The repository's registry directory, copied where tests may edit it."""
    return shutil.copytree(real_registry, tmp_path / "registry")


@pytest.fixture
def plant(registry_copy: Path) -> Callable[..., Path]:
    """Apply edits to the copy and return its directory.

    Failing when the text is absent keeps a test from passing because its
    violation was never planted.
    """

    def apply(*edits: Edit) -> Path:
        for name, old, new in edits:
            path = registry_copy / name
            text = path.read_text(encoding="utf-8")
            assert old in text, f"{name} has no {old!r} to replace"
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return registry_copy

    return apply


@pytest.fixture
def load_errors() -> Callable[[Path], tuple[str, ...]]:
    """Load a directory that must fail; return every error message."""

    def load(directory: Path) -> tuple[str, ...]:
        with pytest.raises(RegistryError) as raised:
            load_registry(directory)
        return raised.value.errors

    return load
