"""What the second host will run, and what it refuses to (S037, R4a): the check
of a workload's factory that the wiring calls when the service starts, and the
modules that may import the framework."""

import ast
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from agent_framework import Executor, WorkflowContext, handler
from dbsupport import DatabaseHandle
from hostflows import Dials, Filing, Gather, brief_factory
from hostsupport import BriefWorld, in_leg_thread, make_world
from langgraph.graph import StateGraph
from servicesupport import REPO_ROOT
from toolsupport import tracer_of

from meridian.runtime.agent_framework_host import (
    AgentFrameworkHost,
    WorkflowDefinition,
    check_factory,
)
from meridian.runtime.failures import GraphFailure
from meridian.workloads.claims_triage.graph import build as langgraph_factory

RUNTIME_DIR = REPO_ROOT / "src" / "meridian" / "runtime"
HOST_MODULE = "meridian.runtime.agent_framework_host"
STORE_MODULE = "meridian.runtime.workflow_checkpoints"
PROCESS_SECONDS = 120
# What a host may never call on a stored document: the framework restores one
# only through the store's codec.
RESTORERS = {"from_dict", "from_json", "loads", "load", "deserialize_type"}


class Idle(Executor):
    def __init__(self, name: str) -> None:
        super().__init__(id=name)

    @handler
    async def run(self, message: dict, ctx: WorkflowContext[dict]) -> None:
        await ctx.send_message(message)


def returning(value: Any) -> Callable[..., Any]:
    return lambda model, tools: value


# ── the check ───────────────────────────────────────────────────────────────
def test_a_workloads_factory_passes_the_check_and_its_definition_is_returned() -> None:
    definition = check_factory(brief_factory(Dials()))

    assert isinstance(definition, WorkflowDefinition)
    assert definition.start.id == "gather"
    assert len(definition.edges) == 3


def test_the_real_langgraph_factory_is_refused_with_a_sentence_that_says_why() -> None:
    with pytest.raises(TypeError, match="could not be called"):
        check_factory(langgraph_factory)


def test_a_factory_that_returns_a_langgraph_graph_names_its_type() -> None:
    with pytest.raises(TypeError, match="not StateGraph"):
        check_factory(returning(StateGraph(dict)))


@pytest.mark.parametrize("value", [None, "a workflow", 7, (), {"start": Idle("x")}])
def test_a_factory_that_returns_anything_else_is_refused(value: Any) -> None:
    with pytest.raises(TypeError, match="must return a WorkflowDefinition"):
        check_factory(returning(value))


def test_an_entry_point_that_is_not_a_factory_is_refused() -> None:
    with pytest.raises(TypeError, match="must be a factory"):
        check_factory(None)


def test_a_factory_that_raises_is_refused_with_the_class_and_not_the_text() -> None:
    def broken(model: Any, tools: Any) -> Any:
        raise KeyError("a claimant's name")

    with pytest.raises(TypeError, match="KeyError") as raised:
        check_factory(broken)

    assert "claimant" not in str(raised.value)


def test_a_definition_with_a_start_edges_or_types_that_are_not_what_they_say() -> None:
    step = Idle("step")
    bad: list[dict[str, Any]] = [
        {"start": "step", "edges": ()},
        {"start": step, "edges": ((step,),)},
        {"start": step, "edges": ((step, "other"),)},
        {"start": step, "edges": (), "state_types": (dict,)},
        {"start": step, "edges": (), "state_types": ("Gathered",)},
    ]

    for fields in bad:
        with pytest.raises(TypeError):
            WorkflowDefinition(**fields)


def test_edges_the_framework_will_not_connect_are_refused_at_the_check() -> None:
    def mismatched(model: Any, tools: Any) -> WorkflowDefinition:
        gather = Gather(Dials(), tools)
        return WorkflowDefinition(
            start=gather,
            edges=((gather, _TakesFiling()),),
        )

    with pytest.raises(TypeError, match="does not build"):
        check_factory(mismatched)


class _TakesFiling(Executor):
    def __init__(self) -> None:
        super().__init__(id="takes-filing")

    @handler
    async def run(self, filing: Filing, ctx: WorkflowContext) -> None:
        return None


def test_the_check_calls_no_client_and_runs_nothing() -> None:
    dials = Dials()

    check_factory(brief_factory(dials))

    assert dict(dials.counts) == {}


# ── a type the definition did not register ──────────────────────────────────
def test_a_state_type_left_out_of_the_definition_fails_the_leg_at_its_first_save(
    fresh_database: DatabaseHandle, plant: Callable[..., Path]
) -> None:
    world: BriefWorld = make_world(fresh_database, plant)
    dials = Dials()
    host = AgentFrameworkHost(brief_factory(dials, state_types=()), dsn=world.dsn())

    # The check cannot see it: the definition is valid, its messages are not
    # storable. The first checkpoint that holds one (after the first step) is
    # refused, and the leg stops before the second step begins.
    check_factory(brief_factory(dials, state_types=()))
    with pytest.raises(GraphFailure) as raised:
        in_leg_thread(
            lambda: host.start(
                world.identity,
                world.model(),
                world.tools(),
                tracer_of(world.exporter),
                {"claim_id": "CLM-0001"},
            )
        )

    assert raised.value.code == "checkpoint-not-saved"
    assert dict(dials.counts) == {"gather": 1}


# ── the modules that may import the framework ───────────────────────────────
def imported_modules(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module.split(".")[0])
    return found


def test_only_the_host_and_the_store_import_the_framework_in_the_runtime() -> None:
    importers = {
        path.name
        for path in RUNTIME_DIR.glob("*.py")
        if "agent_framework" in imported_modules(path)
    }

    assert importers == {"agent_framework_host.py", "workflow_checkpoints.py"}


def test_the_host_calls_nothing_that_restores_a_stored_document() -> None:
    tree = ast.parse((RUNTIME_DIR / "agent_framework_host.py").read_text("utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert not called & RESTORERS
    assert "pickle" not in imported_modules(RUNTIME_DIR / "agent_framework_host.py")


def loaded_after(imports: str) -> list[str]:
    code = (
        f"import json, sys\n{imports}"
        "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] == "
        f"'agent_framework' or m in ('{HOST_MODULE}', '{STORE_MODULE}'))))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize(
    "imports",
    [
        "import meridian.runtime\n",
        "import meridian.runtime.sweep\n",
        "import meridian.runtime.hosts\n",
        "import meridian.workloads.claims_triage.sweep\n",
    ],
)
def test_the_runtime_package_the_sweep_and_the_protocol_do_not_load_the_host(
    imports: str,
) -> None:
    assert loaded_after(imports) == []


def test_importing_the_host_loads_it_the_store_and_the_framework() -> None:
    loaded = loaded_after(f"import {HOST_MODULE}\n")

    assert HOST_MODULE in loaded
    assert STORE_MODULE in loaded
    assert "agent_framework" in loaded
