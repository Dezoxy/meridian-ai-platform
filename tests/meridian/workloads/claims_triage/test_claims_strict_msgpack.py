"""Importing the workload first must still leave LangGraph in strict mode."""

import os
import subprocess
import sys

PROBE = (
    "import meridian.workloads.claims_triage.graph\n"
    "from langgraph.checkpoint.serde import _msgpack\n"
    "print(_msgpack.STRICT_MSGPACK_ENABLED)\n"
)


def test_a_fresh_interpreter_that_imports_the_workload_gets_strict_msgpack() -> None:
    env = {k: v for k, v in os.environ.items() if k != "LANGGRAPH_STRICT_MSGPACK"}

    completed = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "True"
