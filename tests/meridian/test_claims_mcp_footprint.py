"""The claims tool server sits in the platform (S082, Move A), so importing it
loads neither the runtime, the agent framework nor the workloads. Before the
move, importing it ran the claims workload's ``__init__``, which imports the
runtime."""

import json
import subprocess
import sys

SERVER_APP = "meridian.platform.claims_mcp.app"
OLD_PLACE = "meridian.workloads.claims_triage.mcp_server"


def _watched_modules_after_importing(module: str) -> list[str]:
    # A fresh interpreter: sys.modules here holds whatever other tests imported.
    code = (
        "import json, sys\n"
        f"import {module}\n"
        "print(json.dumps(sorted(\n"
        "    m for m in sys.modules\n"
        "    if m.split('.')[0] in {'langgraph', 'langchain', 'agent_framework'}\n"
        "    or m.split('.')[0].startswith(('langchain_', 'langgraph_'))\n"
        "    or m == 'meridian.runtime' or m.startswith('meridian.runtime.')\n"
        "    or m == 'meridian.workloads' or m.startswith('meridian.workloads.')\n"
        ")))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_tool_server_app_loads_no_runtime_framework_or_workload() -> None:
    assert _watched_modules_after_importing(SERVER_APP) == []


def test_the_probe_sees_the_runtime_when_something_imports_it() -> None:
    # The probe can fail: the runtime's package is what the old place loaded.
    loaded = _watched_modules_after_importing("meridian.runtime")

    assert "meridian.runtime" in loaded


def test_the_old_place_is_gone_with_no_stub_behind() -> None:
    # A fresh interpreter, and the module itself rather than find_spec: a
    # leftover ``__pycache__`` folder in a checkout that once held the package
    # makes the folder a namespace package, which find_spec reports as found.
    result = subprocess.run(
        [sys.executable, "-c", f"import {OLD_PLACE}.app"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr, result.stderr
    # The old path itself is what is missing, not something its parent imports.
    assert f"No module named '{OLD_PLACE}" in result.stderr, result.stderr
