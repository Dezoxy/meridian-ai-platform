"""The marker of the tests that run ``terraform`` (S079).

A pytest mark cannot fail a test, so ``needs_terraform`` is a decorator. Where
the program is installed it returns the test untouched. Where it is missing it
skips the test on a development machine, and under ``GITHUB_ACTIONS=true`` it
makes the test fail with one line: the python workflow installs Terraform, and
a runner without it must not pass by skipping what it was meant to prove. The
shellcheck test of ``test_aws_kubeadm_bootstrap`` holds the same rule.

Both the program and the variable are read when the decorator runs, which is
when a test module is imported.
"""

import functools
import os
import shutil
from collections.abc import Callable
from typing import Any

import pytest

MISSING = "terraform is not installed"
PIPELINE_NEEDS_IT = f"{MISSING}, and the pipeline needs it here"


def needs_terraform[F: Callable[..., Any]](test: F) -> F:
    """The test, skipped or failed where ``terraform`` is not installed."""
    if shutil.which("terraform") is not None:
        return test
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return pytest.mark.skip(reason=MISSING)(test)

    @functools.wraps(test)
    def fails(*args: Any, **kwargs: Any) -> None:
        pytest.fail(PIPELINE_NEEDS_IT, pytrace=False)

    return fails  # type: ignore[return-value]
