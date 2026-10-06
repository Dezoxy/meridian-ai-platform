"""What the second host's store may be imported by (S037, R3b).

The store imports the second agent framework. The runtime's sweep, which the
scheduled sweep job loads with PostgreSQL and nothing else, must not reach it:
each case runs in a fresh interpreter and reads ``sys.modules``. A control
imports the store itself, so the check can fail.
"""

import json
import subprocess
import sys

import pytest

STORE = "meridian.runtime.workflow_checkpoints"
PROCESS_SECONDS = 120
# Prints, as JSON, the framework's modules and the store's that are loaded.
REPORT = (
    "import json, sys\n"
    "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] == "
    f"'agent_framework' or m == '{STORE}')))\n"
)


def loaded_after(imports: str) -> list[str]:
    completed = subprocess.run(
        [sys.executable, "-c", imports + REPORT],
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    "imports",
    [
        "import meridian.runtime\n",
        "import meridian.runtime.sweep\n",
        "import meridian.workloads.claims_triage.sweep\n",
    ],
)
def test_the_sweep_and_the_runtime_package_load_neither_the_store_nor_the_framework(
    imports: str,
) -> None:
    assert loaded_after(imports) == []


def test_importing_the_store_loads_it_and_the_framework() -> None:
    loaded = loaded_after(f"import {STORE}\n")

    assert STORE in loaded
    assert "agent_framework" in loaded
