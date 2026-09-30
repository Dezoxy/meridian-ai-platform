"""The runtime package forces LangGraph's strict msgpack mode (ADR 2 appendix).

LangGraph reads the variable once, at first import, so the variable alone proves
nothing: these tests look at the flag LangGraph itself uses.
"""

import os
import subprocess
import sys

from langgraph.checkpoint.serde import _msgpack

PROBE = (
    "import meridian.runtime\n"
    "from langgraph.checkpoint.serde import _msgpack\n"
    "print(_msgpack.STRICT_MSGPACK_ENABLED)\n"
)


def test_langgraph_is_in_strict_msgpack_mode_in_this_process() -> None:
    # tests/meridian/conftest.py imports meridian.runtime before anything else.
    assert _msgpack.STRICT_MSGPACK_ENABLED is True


def test_the_runtime_overrides_a_weaker_setting_before_langgraph_reads_it() -> None:
    env = os.environ | {"LANGGRAPH_STRICT_MSGPACK": "false"}

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
