"""Probe 11: what the framework imports. The repository's test
``test_importing_the_services_loads_no_provider_sdk_or_credential_library`` will
look at the same names once the framework is imported by the runtime."""

import importlib.metadata
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from s037probe.probe_imports import WATCHED

SPIKE_ROOT = Path(__file__).resolve().parents[1]
PROCESS_SECONDS = 120
PROVIDER_NAMES = {
    "openai",
    "azure",
    "azure.identity",
    "azure.core",
    "anthropic",
    "boto3",
    "litellm",
    "mistralai",
}


def stage(name: str) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, "-m", "s037probe.probe_imports", name],
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
        check=True,
        cwd=SPIKE_ROOT,
        env={**os.environ, "PYTHONPATH": str(SPIKE_ROOT / "src")},
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_importing_the_framework_loads_none_of_the_watched_modules() -> None:
    assert stage("framework")["after"] == []


def test_building_and_running_a_pausing_workflow_loads_none_of_them_either() -> None:
    assert stage("framework-run")["after"] == []


def test_the_framework_adds_nothing_to_what_the_runtime_already_loads() -> None:
    runtime = stage("runtime")["after"]
    both = stage("both")

    # The runtime's own imports bring langgraph and langchain_core (it is the
    # one place that may); no provider SDK or credential library, with or
    # without the framework, and nothing new after it and the probe workflow.
    assert set(runtime) == {"langgraph", "langchain_core"}
    assert both["before_framework"] == runtime
    assert both["after"] == runtime
    assert not PROVIDER_NAMES & set(both["after"])


def test_the_watched_names_are_the_ones_the_contract_lists() -> None:
    assert set(WATCHED) == PROVIDER_NAMES | {"langgraph", "langchain_core"}


def test_the_dependencies_of_the_core_package_at_1_19_0() -> None:
    assert importlib.metadata.version("agent-framework-core") == "1.19.0"

    requires = importlib.metadata.requires("agent-framework-core") or []
    unconditional = sorted(
        re.split(r"[<>=~! ;]", r, maxsplit=1)[0]
        for r in requires
        if "extra ==" not in r
    )

    # The extras (``all``) name the provider packages; none is installed.
    assert unconditional == [
        "msgspec",
        "opentelemetry-api",
        "pydantic",
        "python-dotenv",
        "pyyaml",
        "typing-extensions",
    ]
    installed = {d.metadata["Name"].lower() for d in importlib.metadata.distributions()}
    assert not {n for n in installed if n.startswith("agent-framework-")} - {
        "agent-framework-core"
    }
