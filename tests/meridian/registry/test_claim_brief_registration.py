"""The second workload, ``claim-brief``, as the registry names it (S037, W1b):
its agent entry, the tenant and the services that may name it, and the two
refusals the registry has for an agent of the second host, each shown once with
this agent. The workload's own constants are held equal to what the registry
says of it.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from meridian.platform.registry import RegistryError, load_registry
from meridian.workloads.claim_brief import AGENT
from meridian.workloads.claim_brief.prompt import (
    BRIEF_DATA_CLASS,
    BRIEF_OUTPUT_TOKENS,
)

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]

BRIEF_TOOLS = (
    "policy_lookup",
    "claim_history",
    "request_approval",
    "approval_outcome",
    "add_claim_note",
)
# The agent's own entry ends with its last tool; a key written after it belongs
# to this agent, and so does a tool added there. The line break and the six
# spaces keep the claims-triage's workers (ten spaces in) from matching.
LAST_TOOL = "\n      - approval_outcome\n      - add_claim_note\n"
RUNS_IT = {"agent-runtime", "claims-api"}


def test_the_registry_names_the_agent_with_the_second_host_and_its_five_tools(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)

    agent = registry.agent(AGENT)

    assert agent is not None
    assert agent.kind == "graph"
    assert agent.host == "agent-framework"
    assert agent.structured_outputs is False
    assert agent.tools == BRIEF_TOOLS
    assert agent.workers == ()
    assert agent.description


def test_the_agent_is_dumped_with_its_host_and_without_workers(
    real_registry: Path,
) -> None:
    agent = load_registry(real_registry).agent(AGENT)

    assert agent is not None
    assert agent.model_dump(mode="json") == {
        "id": "claim-brief",
        "description": agent.description,
        "kind": "graph",
        "tools": list(BRIEF_TOOLS),
        "structured_outputs": False,
        "host": "agent-framework",
    }


def test_the_agent_does_not_list_the_wording_search(real_registry: Path) -> None:
    agent = load_registry(real_registry).agent(AGENT)

    assert agent is not None
    assert "wording_search" not in agent.tools


def test_only_the_claims_tenant_lists_the_agent(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    listing = {t.id for t in registry.tenants if AGENT in t.agents}

    # evaluation lists the agents it runs and grades: the brief has no grader,
    # so it is not run there; development lists the triage alone.
    assert listing == {"claims-triage"}


def test_the_agent_runtime_hosts_it_and_the_claims_api_starts_it_and_no_service_else(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)

    naming = {s.id for s in registry.services if AGENT in s.agents}

    assert naming == RUNS_IT


def test_the_workloads_data_class_and_token_cap_fit_every_tenant_that_lists_it(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)
    tenants = [t for t in registry.tenants if AGENT in t.agents]

    assert tenants
    for tenant in tenants:
        assert tenant.data_class == BRIEF_DATA_CLASS
        assert tenant.limits.tokens_per_minute >= BRIEF_OUTPUT_TOKENS


def test_workers_on_the_agent_are_refused_because_it_is_on_the_second_host(
    plant: Plant, load_errors: LoadErrors
) -> None:
    # One worker that holds every tool, so that the host is the only refusal.
    workers = (
        "    workers:\n"
        "      - id: briefer\n"
        "        description: Does all of it.\n"
        "        tools:\n" + "".join(f"          - {tool}\n" for tool in BRIEF_TOOLS)
    )
    directory = plant(("agents.yaml", LAST_TOOL, LAST_TOOL + workers))

    errors = load_errors(directory)

    assert len(errors) == 1
    assert errors[0].startswith("agents.yaml: agents[3].workers: agent 'claim-brief' ")
    assert "has host 'agent-framework' and declares workers" in errors[0]


def test_a_tool_the_registry_does_not_know_is_refused_on_the_agent(
    plant: Plant,
) -> None:
    directory = plant(("agents.yaml", LAST_TOOL, LAST_TOOL + "      - wording_find\n"))

    with pytest.raises(RegistryError) as refused:
        load_registry(directory)

    assert refused.value.errors == (
        "agents.yaml: agents[3].tools[5]: unknown tool 'wording_find'",
    )
