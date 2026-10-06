"""The claim-brief workload as registered (S037, W1b): its two entry points, the
empty evaluation it publishes, the tools the registry lists against the tools
the workflow calls, and the rule that every build gives new steps.

The registry's own entry and its refusals are in
``tests/meridian/registry/test_claim_brief_registration.py``.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from claimbriefsupport import (
    ScriptedTools,
    claim_input,
    policy_answer,
    record_decision,
    resume_leg,
    start_leg,
)
from hostsupport import BriefWorld
from servicesupport import REPO_ROOT

from meridian.platform.evaluation.fingerprints import golden_set_of
from meridian.platform.evaluation.report import ReportError
from meridian.platform.evaluation.workload import (
    WorkloadEvaluation,
    load_evaluation,
)
from meridian.platform.registry import load_registry
from meridian.runtime.agent_framework_host import AgentFrameworkHost, executors_of
from meridian.runtime.graphs import load_graph_factory
from meridian.workloads.claim_brief import AGENT, evaluation
from meridian.workloads.claim_brief.workflow import build

GOLDEN_SET = REPO_ROOT / "data" / "evaluation" / "claim-brief" / "golden"
EMPTY_HISTORY = {"entries": [], "truncated": False}
LOADS_THE_EVALUATION = (
    "import sys\n"
    "from meridian.platform.evaluation.workload import load_evaluation\n"
    "load_evaluation('claim-brief')\n"
    "print(sorted(m for m in ('agent_framework', 'langgraph') if m in sys.modules))\n"
)


# ── the entry points ────────────────────────────────────────────────────────
def test_the_runtimes_loader_finds_the_workflow_factory_by_the_agents_name() -> None:
    registry = load_registry(REPO_ROOT / "config" / "registry")

    factory = load_graph_factory(AGENT, registry)

    assert factory is build


def test_the_evaluations_loader_finds_the_empty_evaluation_by_the_workloads_name() -> (
    None
):
    assert load_evaluation(AGENT) is evaluation.EVALUATION


def test_loading_the_evaluation_brings_in_no_agent_framework() -> None:
    result = subprocess.run(
        [sys.executable, "-c", LOADS_THE_EVALUATION],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )

    assert result.stdout.strip() == "[]"


# ── the empty evaluation, in the form `meridian workload new` writes ────────
def test_the_evaluation_is_published_and_the_golden_set_holds_no_case() -> None:
    fingerprint = golden_set_of(GOLDEN_SET / "manifest.json")

    assert isinstance(evaluation.EVALUATION, WorkloadEvaluation)
    assert evaluation.EVALUATION.workload == AGENT
    assert tuple(fingerprint.files) == ("cases.json",)
    assert evaluation.EVALUATION.submissions(GOLDEN_SET) == ()


def test_the_evaluation_has_no_graders_yet() -> None:
    with pytest.raises(ReportError) as refused:
        evaluation.EVALUATION.report({}, GOLDEN_SET, None)  # type: ignore[arg-type]

    assert str(refused.value) == evaluation.NO_GRADERS


def test_a_golden_set_with_one_case_is_refused_while_nothing_grades(
    tmp_path: Path,
) -> None:
    (tmp_path / "cases.json").write_text('[{"case": "case-1"}]', encoding="utf-8")

    with pytest.raises(ReportError) as refused:
        evaluation.EVALUATION.submissions(tmp_path)

    assert str(refused.value) == evaluation.NO_GRADERS


# ── every build gives new steps ─────────────────────────────────────────────
def test_two_builds_give_distinct_executors_and_the_same_state_types() -> None:
    first = build(object(), object())  # type: ignore[arg-type]
    second = build(object(), object())  # type: ignore[arg-type]

    first_steps, second_steps = executors_of(first), executors_of(second)

    assert len(first_steps) == len(second_steps) == 4
    assert not {id(step) for step in first_steps} & {id(step) for step in second_steps}
    assert first.start is not second.start
    assert first.state_types == second.state_types


def test_the_host_takes_the_steps_of_each_build_and_refuses_one_it_handed_out() -> None:
    host = AgentFrameworkHost(build, dsn="postgresql://unused.invalid/none")
    one = build(object(), object())  # type: ignore[arg-type]

    taken = host._take(one)
    second_build = host._take(build(object(), object()))  # type: ignore[arg-type]
    with pytest.raises(TypeError) as refused:
        host._take(one)

    assert taken is one
    assert second_build is not one
    assert "new executors on every call" in str(refused.value)


# ── the registry's tools and the workflow's calls ───────────────────────────
def test_the_workflow_calls_exactly_the_tools_the_registry_lists_for_the_agent(
    world: BriefWorld,
) -> None:
    tools = ScriptedTools(policy_answer(), EMPTY_HISTORY)
    listed = world.registry.agent(AGENT)
    assert listed is not None

    start_leg(world, tools, claim_input())
    record_decision(world, "approve")
    resume_leg(world, tools)

    assert {tool for tool, _, _ in tools.calls} == set(listed.tools)
