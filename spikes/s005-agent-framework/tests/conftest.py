"""Shared fixtures: one suite, parametrized over both frameworks."""

import importlib
from pathlib import Path
from types import ModuleType

import pytest
from support import FLOW_MODULES, FRAMEWORKS


@pytest.fixture(params=FRAMEWORKS)
def framework(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def flow(framework: str) -> ModuleType:
    return importlib.import_module(FLOW_MODULES[framework])


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    return tmp_path / "store"
