"""Every test is stopped after a fixed time, so a hang fails instead of stalling (S074).

The limit lives in ``pyproject.toml`` (``timeout`` and ``timeout_method`` under
``[tool.pytest.ini_options]``, read by pytest-timeout). The first test compares
it with the constant below, which is where a person who changes it has to say so.
The rest run a pytest of their own in a subprocess, in a scratch directory with
no ``conftest.py`` and no ``addopts`` of this repository's, so a test that
sleeps past a small limit fails there and this suite stays green.
"""

import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

from servicesupport import REPO_ROOT

# The limit and its method, as measured and chosen in S074: see the pyproject's
# comment beside ``timeout`` for the arithmetic. A change to the pyproject
# without a change here fails the first test.
TIMEOUT_SECONDS = 600
TIMEOUT_METHOD = "signal"
# What a test that is slower by design gets: pytest-timeout reads 0 as no limit.
NO_LIMIT = 0

PYTEST_OPTIONS = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))[
    "tool"
]["pytest"]["ini_options"]


def test_the_configured_limit_is_the_constant_this_file_names() -> None:
    assert PYTEST_OPTIONS["timeout"] == TIMEOUT_SECONDS
    assert PYTEST_OPTIONS["timeout_method"] == TIMEOUT_METHOD


def test_the_limit_is_not_under_two_minutes() -> None:
    # A loaded machine must not fail a healthy test (the plan's rows on tests
    # that failed under load): the floor is a minimum, not a goal.
    assert PYTEST_OPTIONS["timeout"] >= 120


def run_pytest(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    """A pytest of its own on ``source``, with the project's method and a limit
    of the test file's choosing (the marker); its own ini file, no repository
    conftest, no cache."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_scratch.py").write_text(textwrap.dedent(source), encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COVERAGE", "COV_CORE"))
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-o",
            f"timeout_method={PYTEST_OPTIONS['timeout_method']}",
            "--rootdir",
            str(tmp_path),
            str(tmp_path / "test_scratch.py"),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_test_that_sleeps_past_its_limit_fails_with_the_plugins_message(
    tmp_path: Path,
) -> None:
    done = run_pytest(
        tmp_path,
        """
        import time

        import pytest


        @pytest.mark.timeout(1)
        def test_sleeps_past_the_limit():
            time.sleep(30)


        def test_the_next_one_still_runs():
            pass
        """,
    )

    assert done.returncode == 1, done.stdout
    assert "Timeout (>1.0s)" in done.stdout
    assert "1 failed, 1 passed" in done.stdout


def test_a_test_under_its_limit_passes(tmp_path: Path) -> None:
    done = run_pytest(
        tmp_path,
        """
        import time

        import pytest


        @pytest.mark.timeout(10)
        def test_sleeps_under_the_limit():
            time.sleep(0.2)
        """,
    )

    assert done.returncode == 0, done.stdout
    assert "Timeout" not in done.stdout


def test_the_limit_covers_a_fixture_that_hangs(tmp_path: Path) -> None:
    done = run_pytest(
        tmp_path,
        """
        import time

        import pytest


        @pytest.fixture
        def hangs():
            time.sleep(30)


        @pytest.mark.timeout(1)
        def test_waits_for_the_fixture(hangs):
            pass
        """,
    )

    assert done.returncode == 1, done.stdout
    assert "Timeout (>1.0s)" in done.stdout


def test_no_limit_leaves_a_slow_test_alone(tmp_path: Path) -> None:
    done = run_pytest(
        tmp_path,
        f"""
        import time

        import pytest


        @pytest.mark.timeout({NO_LIMIT})
        def test_slower_by_design():
            time.sleep(1.5)
        """,
    )

    assert done.returncode == 0, done.stdout


def test_each_opt_in_recording_test_carries_no_limit() -> None:
    """A recording paces forty live calls over minutes, and the default would
    stop it: every test behind the opt-in switch carries its own marker."""
    text = (REPO_ROOT / "tests" / "meridian" / "test_evaluation_stack.py").read_text(
        encoding="utf-8"
    )

    assert text.count("@OPT_IN") == 2
    assert text.count(f"@pytest.mark.timeout({NO_LIMIT})\n@OPT_IN") == 2


def test_the_live_azure_module_carries_no_limit() -> None:
    text = (
        REPO_ROOT / "tests" / "meridian" / "gateway" / "test_live_azure.py"
    ).read_text(encoding="utf-8")

    assert f"pytestmark = [\n    pytest.mark.timeout({NO_LIMIT})," in text
