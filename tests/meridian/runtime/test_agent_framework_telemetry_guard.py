"""The second framework's own telemetry stays off (S037, F3r, boundary L2).

The framework turns its instrumentation on by default and reads two environment
variables once, when its observability module is first imported. With no global
tracer provider on the platform's side nothing would be exported, but a provider
installed later would carry workflow spans, and spans with message content when
``ENABLE_SENSITIVE_DATA`` is set. ``meridian.runtime`` therefore sets both to
``false`` before anything imports the framework, as it does for LangSmith's
variables (see ``test_langsmith_guard.py``): the step spans are the host's own,
made from the framework's events.
"""

import os
import subprocess
import sys

import pytest

TELEMETRY_VARIABLES = ("ENABLE_INSTRUMENTATION", "ENABLE_SENSITIVE_DATA")
PROCESS_SECONDS = 60
READ_SETTINGS = """
import sys
if sys.argv[1] == "guarded":
    import meridian.runtime
if sys.argv[1] == "workload-first":
    # The workload imports the framework before it imports the runtime's host, so
    # the framework reads its two variables before the runtime has set them.
    import meridian.workloads.claim_brief.workflow
from agent_framework.observability import OBSERVABILITY_SETTINGS as settings
print(settings.enable_instrumentation, settings.enable_sensitive_data)
"""
PRINT_VARIABLES = f"""
import os
import meridian.runtime
print([os.environ[name] for name in {TELEMETRY_VARIABLES!r}])
"""


def run_python(code: str, *args: str, value: str | None = None) -> str:
    """The output of ``code`` in a fresh interpreter whose two telemetry
    variables are ``value``, or absent when it is ``None``."""
    env = {k: v for k, v in os.environ.items() if k not in TELEMETRY_VARIABLES}
    if value is not None:
        env |= dict.fromkeys(TELEMETRY_VARIABLES, value)
    done = subprocess.run(
        [sys.executable, "-c", code, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.mark.parametrize("value", ["true", "1"])
def test_without_the_guard_the_framework_would_instrument_and_share_content(
    value: str,
) -> None:
    # The control: proves the probe can see the setting, so the test below means
    # something.
    assert run_python(READ_SETTINGS, "bare", value=value) == "True True"


@pytest.mark.parametrize("value", ["true", "1", "yes"])
def test_with_the_runtime_imported_first_the_framework_has_both_switched_off(
    value: str,
) -> None:
    assert run_python(READ_SETTINGS, "guarded", value=value) == "False False"


@pytest.mark.parametrize("value", ["true", "1", None])
def test_with_the_workload_imported_first_the_framework_has_both_switched_off(
    value: str | None,
) -> None:
    # The variables are read too late here, so the host sets the framework's own
    # settings object off when it imports the framework (F4, low 2).
    assert run_python(READ_SETTINGS, "workload-first", value=value) == "False False"


def test_the_runtime_sets_both_variables_to_false_when_they_were_not_set() -> None:
    assert run_python(PRINT_VARIABLES) == "['false', 'false']"
