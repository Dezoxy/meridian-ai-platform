"""Shared fixtures for the synthetic-data tests.

There is no ``__init__.py`` in this folder: the kit's ``make test`` runs
``unittest discover -s tests`` without pytest and must not collect these files.
Helpers are therefore fixtures, because test modules cannot import each other.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = REPO_ROOT / "data" / "synthetic"
GENERATOR_TIMEOUT_SECONDS = 120

GeneratorRunner = Callable[..., None]
TreeReader = Callable[[Path], dict[str, bytes]]


@pytest.fixture(scope="session")
def synthetic_dir() -> Path:
    """The committed output folder, data/synthetic."""
    return SYNTHETIC


@pytest.fixture(scope="session")
def run_generator() -> GeneratorRunner:
    """Run ``python -m generator`` in a fresh process, as the Makefile does."""

    def run(out: Path, *, hash_seed: str = "0", seed: int | None = None) -> None:
        env = {
            **os.environ,
            "PYTHONPATH": str(SYNTHETIC),
            "PYTHONHASHSEED": hash_seed,
        }
        command = [sys.executable, "-m", "generator", "--out", str(out)]
        if seed is not None:
            command += ["--seed", str(seed)]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
                env=env,
                timeout=GENERATOR_TIMEOUT_SECONDS,
            )
        except subprocess.CalledProcessError as exc:
            pytest.fail(
                f"generator exited with {exc.returncode}:\n{exc.stderr}",
                pytrace=False,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"generator did not finish in {GENERATOR_TIMEOUT_SECONDS} seconds",
                pytrace=False,
            )

    return run


@pytest.fixture(scope="session")
def read_tree() -> TreeReader:
    """Every file under a folder as {relative posix path: bytes}."""

    def read(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    return read


@pytest.fixture(scope="session")
def generated(
    tmp_path_factory: pytest.TempPathFactory,
    run_generator: GeneratorRunner,
    read_tree: TreeReader,
) -> dict[str, bytes]:
    """A fresh default-seed run, as {relative path: bytes}."""
    out = tmp_path_factory.mktemp("generated")
    run_generator(out)
    return read_tree(out)


def _load(name: str) -> list[dict]:
    return json.loads((SYNTHETIC / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def policies() -> list[dict]:
    return _load("policies.json")


@pytest.fixture(scope="session")
def history() -> list[dict]:
    return _load("claim-history.json")


@pytest.fixture(scope="session")
def claims() -> list[dict]:
    return _load("claims.json")


@pytest.fixture(scope="session")
def outcomes() -> list[dict]:
    return _load("expected-outcomes.json")


@pytest.fixture(scope="session")
def manifest() -> dict:
    return json.loads((SYNTHETIC / "manifest.json").read_text(encoding="utf-8"))
