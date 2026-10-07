"""The rule for tests that run the real ``jq`` (S073 K10).

About two thousand lines of tests of ``infra/kind/smoke.sh`` and its neighbours
carry ``requires_jq``. A mark that only skips would let a CI image without ``jq``
stay green with all of them skipped, so the rule is the one the Docker test of
the rate store follows: a missing ``jq`` skips on a developer's machine and FAILS
under ``GITHUB_ACTIONS=true``. ``tests/meridian/conftest.py`` runs
``stop_without_jq`` as the fixture ``jq_installed``, which ``requires_jq``
(``kindsupport.py``) names.
"""

import os
import shutil

import pytest

CI_ENV = "GITHUB_ACTIONS"
MISSING = "jq is not installed"


def in_ci() -> bool:
    return os.environ.get(CI_ENV) == "true"


def stop_without_jq() -> None:
    """Return when ``jq`` is on the path; fail in CI and skip elsewhere when it
    is not."""
    if shutil.which("jq") is not None:
        return
    if in_ci():
        pytest.fail(f"{MISSING}, and the tests that run it must not be skipped in CI")
    pytest.skip(MISSING)
