"""S071: neither paid run starts on one variable, and neither starts the other.

Split from ``test_injection_record.py`` (the file size check): the tests of the
two opt-in pairs, ``MERIDIAN_LIVE_AZURE`` with ``MERIDIAN_EVAL_INJECTION_RECORD``
for the injection run and with ``MERIDIAN_EVAL_RECORD`` for the golden
recording. Nothing here calls a model: the functions are asked with a mapping,
and a pytest run in a clean environment shows the paid tests skipped.
"""

import os
import subprocess
import sys

import pytest
from evalsupport import golden_run_enabled
from injectionrecordsupport import LIVE_ENV, RECORD_ENV, injection_run_enabled
from servicesupport import REPO_ROOT


def test_the_injection_variable_alone_does_not_start_the_paid_run() -> None:
    assert not injection_run_enabled({})
    assert not injection_run_enabled({RECORD_ENV: "1"})
    assert not injection_run_enabled({LIVE_ENV: "1"})
    assert not injection_run_enabled({LIVE_ENV: "1", "MERIDIAN_EVAL_RECORD": "1"})
    assert not injection_run_enabled({LIVE_ENV: "1", RECORD_ENV: "0"})
    assert injection_run_enabled({LIVE_ENV: "1", RECORD_ENV: "1"})


def _pytest_in_a_clean_environment(target: str, name: str, **variables: str) -> str:
    """The summary lines of a pytest run of the tests named ``name`` in
    ``target``, with every ``MERIDIAN_*`` variable removed and ``variables`` set.
    No endpoint and no database address is given, so a test that started would
    stop before any call."""
    environment = {k: v for k, v in os.environ.items() if not k.startswith("MERIDIAN_")}
    environment.update(variables)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            target,
            "-k",
            name,
            "-rs",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return result.stdout


def test_setting_the_golden_recordings_variable_does_not_start_the_paid_run() -> None:
    output = _pytest_in_a_clean_environment(
        "tests/meridian/test_injection_record.py",
        "record_the_injection_cases_with_the_live_model",
        MERIDIAN_EVAL_RECORD="1",
        MERIDIAN_LIVE_AZURE="1",
    )

    assert "1 skipped" in output, output[-400:]
    assert "make eval-injection-record" in output
    assert "spends money" in output


def test_setting_the_paid_runs_variables_does_not_start_the_golden_recording() -> None:
    output = _pytest_in_a_clean_environment(
        "tests/meridian/test_evaluation_stack.py",
        "test_record_the",
        MERIDIAN_EVAL_INJECTION_RECORD="1",
        MERIDIAN_LIVE_AZURE="1",
    )

    assert "2 skipped" in output, output[-400:]
    assert "make eval-record" in output


def test_the_golden_recording_needs_both_variables_as_the_injection_run_does() -> None:
    assert not golden_run_enabled({})
    assert not golden_run_enabled({"MERIDIAN_EVAL_RECORD": "1"})
    assert not golden_run_enabled({LIVE_ENV: "1"})
    assert not golden_run_enabled({LIVE_ENV: "1", "MERIDIAN_EVAL_RECORD": "0"})
    assert not golden_run_enabled({LIVE_ENV: "0", "MERIDIAN_EVAL_RECORD": "1"})
    assert golden_run_enabled({LIVE_ENV: "1", "MERIDIAN_EVAL_RECORD": "1"})


@pytest.mark.parametrize(
    "variables",
    [{"MERIDIAN_EVAL_RECORD": "1"}, {"MERIDIAN_LIVE_AZURE": "1"}],
    ids=["its own variable alone", "the live variable alone"],
)
def test_one_variable_alone_does_not_start_the_golden_recording(
    variables: dict[str, str],
) -> None:
    output = _pytest_in_a_clean_environment(
        "tests/meridian/test_evaluation_stack.py", "test_record_the", **variables
    )

    assert "2 skipped" in output, output[-400:]
    assert "make eval-record" in output
    assert "spends money" in output
