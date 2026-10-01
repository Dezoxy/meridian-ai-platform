"""Fixtures: a scratch copy of the real registry to plant one violation in
(``registry_copy`` and ``plant`` live in the parent conftest)."""

from collections.abc import Callable
from pathlib import Path

import pytest

from meridian.platform.registry import RegistryError, load_registry


@pytest.fixture
def snapshot_path(real_registry: Path) -> Path:
    return real_registry / "snapshots" / "terraform-openai-deployments.json"


@pytest.fixture
def load_errors() -> Callable[[Path], tuple[str, ...]]:
    """Load a directory that must fail; return every error message."""

    def load(directory: Path) -> tuple[str, ...]:
        with pytest.raises(RegistryError) as raised:
            load_registry(directory)
        return raised.value.errors

    return load
