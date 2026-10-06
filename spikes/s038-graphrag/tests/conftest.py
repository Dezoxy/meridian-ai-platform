"""Shared fixtures. The spike is not a package of its own and adds no
dependency, so its source folder is put on the path here; the root
environment runs it, with the path of these tests passed explicitly."""

import sys
from pathlib import Path

import pytest

SPIKE_SRC = Path(__file__).resolve().parents[1] / "src"
if str(SPIKE_SRC) not in sys.path:
    sys.path.insert(0, str(SPIKE_SRC))

from claimgraph.build import build_graph  # noqa: E402
from claimgraph.model import Graph  # noqa: E402
from claimgraph.variant import build_variant  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "synthetic"


# The comparison needs the repository's own database fixtures
# (`fresh_database`, `gateway`). They live in conftest files that pytest does
# not load from here, so they are registered as plugins, not copied: both
# modules resolve through the root `pythonpath` (tests/meridian). Without
# MERIDIAN_TEST_DATABASE_URL the fixtures skip, as they do under tests/.
pytest_plugins = ["conftest", "knowledge_mcp.conftest"]


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return DATA_DIR


@pytest.fixture(scope="session")
def graph(data_dir: Path) -> Graph:
    return build_graph(data_dir)


@pytest.fixture(scope="session")
def variant(data_dir: Path) -> Graph:
    return build_variant(data_dir)
