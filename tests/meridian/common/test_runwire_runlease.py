"""The run's shared models and the sweep's two constants have modules of their
own (S082), so that the Claims API and the sweep job need not import the
runtime for them. What each module may load, and that the names the runtime had
are the same objects."""

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from meridian.platform.common import runlease, runwire
from meridian.runtime import models as runtime_models
from meridian.runtime import runs
from meridian.runtime import sweep as runtime_sweep
from meridian.workloads.claims_triage import adjuster, briefs, runtime_calls, triaging
from meridian.workloads.claims_triage import models as claims_models


def _new_modules_after_importing(module: str) -> list[str]:
    # A fresh interpreter: sys.modules here holds whatever other tests imported.
    # ``json`` is imported first so that it is not counted as the module's own.
    code = (
        "import json, sys; before = set(sys.modules); "
        f"import {module}; "
        "print(json.dumps(sorted(set(sys.modules) - before)))"
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


def test_the_lease_module_loads_no_module_but_the_packages_above_it() -> None:
    loaded = _new_modules_after_importing("meridian.platform.common.runlease")

    assert loaded == [
        "meridian",
        "meridian.platform",
        "meridian.platform.common",
        "meridian.platform.common.runlease",
    ]


def test_the_models_module_loads_no_web_stack_and_nothing_of_the_runtime() -> None:
    loaded = _new_modules_after_importing("meridian.platform.common.runwire")

    web_stack = ("fastapi", "httpx", "starlette")
    assert [m for m in loaded if m.split(".")[0] in web_stack] == []
    assert [m for m in loaded if m.startswith("meridian.runtime")] == []
    assert "meridian.platform.common.http" not in loaded
    assert "meridian.platform.common.wire" in loaded


def test_the_runtime_re_exports_the_models_it_had() -> None:
    assert runtime_models.RunResponse is runwire.RunResponse
    assert runtime_models.RunState is runwire.RunState


def test_the_runtime_re_exports_the_constants_it_had() -> None:
    assert runtime_sweep.RUNNING_LEASE_SECONDS is runlease.RUNNING_LEASE_SECONDS
    assert runtime_sweep.ABANDONED_REASON is runlease.ABANDONED_REASON
    assert runs.RUNNING_LEASE_SECONDS is runlease.RUNNING_LEASE_SECONDS


def _modules_imported_by(module: ModuleType) -> set[str]:
    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }


def test_the_claims_api_reads_the_models_and_constants_from_the_new_modules() -> None:
    # The names are the runtime's too (the tests above), so identity cannot tell
    # where a file reads them from; its import lines do.
    for module in (claims_models, runtime_calls, triaging, briefs):
        imported = _modules_imported_by(module)
        assert "meridian.platform.common.runwire" in imported, module.__name__
        assert "meridian.runtime.models" not in imported, module.__name__
    for module in (briefs, adjuster):
        imported = _modules_imported_by(module)
        assert "meridian.platform.common.runlease" in imported, module.__name__
        assert "meridian.runtime.sweep" not in imported, module.__name__


def test_the_constants_keep_their_values() -> None:
    assert runlease.RUNNING_LEASE_SECONDS == 600
    assert runlease.ABANDONED_REASON == "abandoned"
