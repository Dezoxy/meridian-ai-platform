"""The small tree the workload scaffold's tests write into."""

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copy(REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    shutil.copytree(REPO / "config" / "registry", tmp_path / "config" / "registry")
    (tmp_path / "src" / "meridian" / "workloads").mkdir(parents=True)
    return tmp_path
