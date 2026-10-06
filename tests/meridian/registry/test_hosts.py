"""The host that runs an agent: said by the registry, per agent (S037, R2).

The declaration, its two checks and its schema only; nothing reads the field at
run time yet. The default is not written into an agent's dump, so the
evaluation's ``tools`` fingerprint of an agent that does not name a host does
not move.
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from meridian.platform.evaluation.fingerprints import (
    canonical_sha256,
    tools_fingerprint,
)
from meridian.platform.registry import load_registry
from meridian.platform.registry.models import Agent

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]

# Computed on the step branch at beac023, before the model knew a host: the
# SHA-256 of each real agent's ``model_dump(mode="json")`` and the agent's
# ``tools`` fingerprint. Neither may move when the default is the host.
DUMPS_BEFORE_THE_HOST = {
    "claims-triage": (
        "7156b01593eb057310f99d8b9cadcedaf90c21f6b3d9ed30360a55370219a828",
        "73afde3252eb4818228f29188e8a63c8f0e733d9193c66da2179b7a30290dd41",
    ),
    "knowledge-ingestion": (
        "bf8f8a46b6a749f71e7a15b9601e8f727f659bf5fc2757cd096152acd185dfd0",
        "657d05288fca6e6d4ce8f280b771e75368fea1828c7ae60b6dc52c3a46f16978",
    ),
    "evaluation-judge": (
        "c2111a477301b098642d2f9cebb26f818f34ba2144f519bec33cc202cf4a1985",
        "5ff6bdd037c44aa4290031ca6e528a21c779127bd7a33dfe44a06f5173689013",
    ),
}

TRIAGE = "claims-triage"
JOB = "knowledge-ingestion"
# The agent's own line after its kind sits four spaces in; a key added after it
# is a key of that agent.
JOB_KIND = "    kind: job\n    tools: []\n"
TRIAGE_STRUCTURED = "    structured_outputs: true\n    tools:\n      - policy_lookup\n"
# A graph agent with no tool and no worker: what the scaffold appends, and the
# agent the framework's host may run before it carries workers.
BRIEF_BEFORE_THE_JOB = (
    "agents.yaml",
    "  - id: knowledge-ingestion\n",
    "  - id: brief\n    description: Writes a brief.\n{host}    tools: []\n"
    "  - id: knowledge-ingestion\n",
)

JOB_WITH_A_HOST = (
    "agents[1].host: job agent 'knowledge-ingestion' declares host {word!r}; a "
    "job has no entry point, so no host runs it"
)
WORKERS_ON_THE_SECOND_HOST = (
    "agents.yaml: agents[0].workers: agent 'claims-triage' has host "
    "'agent-framework' and declares workers; the worker view of the tool client "
    "is built and tested for the langgraph host only, so the registry refuses "
    "workers on another host (this check goes when the agent-framework host "
    "carries workers)"
)


def brief_with(host_line: str) -> tuple[str, str, str]:
    name, old, new = BRIEF_BEFORE_THE_JOB
    return name, old, new.format(host=host_line)


def an_agent(**fields: object) -> Agent:
    return Agent.model_validate(
        {"id": "brief", "description": "Writes a brief.", "tools": [], **fields}
    )


# ── the field ───────────────────────────────────────────────────────────────
def test_an_agent_that_names_no_host_runs_on_langgraph() -> None:
    agent = an_agent()

    assert agent.host == "langgraph"
    assert "host" not in agent.model_fields_set


@pytest.mark.parametrize("host", ["langgraph", "agent-framework"])
def test_an_agent_may_name_either_host(host: str) -> None:
    agent = an_agent(host=host)

    assert agent.host == host
    assert "host" in agent.model_fields_set


def test_a_host_that_is_not_one_of_the_two_is_refused_with_its_file_and_path(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(brief_with("    host: carrier-pigeon\n"))

    errors = load_errors(directory)

    assert len(errors) == 1
    assert errors[0].startswith("agents.yaml: agents[1].host: ")
    assert "'langgraph' or 'agent-framework'" in errors[0]


@pytest.mark.parametrize("host", ["langgraph", "agent-framework"])
def test_a_graph_agent_on_either_host_loads(host: str, plant: Plant) -> None:
    directory = plant(brief_with(f"    host: {host}\n"))

    registry = load_registry(directory)

    agent = registry.agent("brief")
    assert agent is not None
    assert agent.host == host


# ── the default is not serialised ───────────────────────────────────────────
@pytest.mark.parametrize("agent_id", sorted(DUMPS_BEFORE_THE_HOST))
def test_every_real_agent_dumps_and_fingerprints_as_it_did_before_the_host(
    agent_id: str, real_registry: Path
) -> None:
    registry = load_registry(real_registry)
    agent = registry.agent(agent_id)

    assert agent is not None
    dump_hash, tools_hash = DUMPS_BEFORE_THE_HOST[agent_id]
    assert canonical_sha256(agent.model_dump(mode="json")) == dump_hash
    assert tools_fingerprint(registry, agent_id) == tools_hash


def test_the_real_registry_has_no_agent_this_test_does_not_pin(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)

    assert sorted(a.id for a in registry.agents) == sorted(DUMPS_BEFORE_THE_HOST)


def test_a_default_host_is_left_out_of_the_dump_even_when_it_is_written() -> None:
    written = an_agent(host="langgraph")
    left_out = an_agent()

    assert "host" not in written.model_dump(mode="json")
    assert written.model_dump(mode="json") == left_out.model_dump(mode="json")
    assert "host" not in written.model_dump()


def test_the_second_host_is_dumped() -> None:
    agent = an_agent(host="agent-framework")

    assert agent.model_dump(mode="json")["host"] == "agent-framework"
    assert agent.model_dump()["host"] == "agent-framework"


def test_the_dump_still_leaves_out_empty_workers_beside_the_host_rule() -> None:
    agent = an_agent(host="agent-framework")

    assert "workers" not in agent.model_dump(mode="json")


def test_naming_the_second_host_moves_the_tools_fingerprint(plant: Plant) -> None:
    before = tools_fingerprint(load_registry(plant(brief_with(""))), "brief")
    after = tools_fingerprint(
        load_registry(
            plant(
                (
                    "agents.yaml",
                    "  - id: brief\n    description: Writes a brief.\n",
                    "  - id: brief\n    description: Writes a brief.\n"
                    "    host: agent-framework\n",
                )
            )
        ),
        "brief",
    )

    assert after != before


# ── the two checks ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("word", ["agent-framework", "langgraph"])
def test_a_job_that_declares_a_host_is_refused_whatever_the_word(
    word: str, plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(("agents.yaml", JOB_KIND, JOB_KIND + f"    host: {word}\n"))

    errors = load_errors(directory)

    assert errors == (f"agents.yaml: {JOB_WITH_A_HOST.format(word=word)}",)


def test_a_job_that_names_no_host_is_not_refused(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    job = registry.agent(JOB)

    assert job is not None
    assert job.kind == "job"
    assert "host" not in job.model_fields_set


def test_workers_on_the_second_host_are_refused(
    plant: Plant, load_errors: LoadErrors
) -> None:
    second_host = "    host: agent-framework\n"
    directory = plant(
        ("agents.yaml", TRIAGE_STRUCTURED, second_host + TRIAGE_STRUCTURED)
    )

    errors = load_errors(directory)

    assert errors == (WORKERS_ON_THE_SECOND_HOST,)


def test_the_second_host_without_workers_is_not_refused(plant: Plant) -> None:
    directory = plant(brief_with("    host: agent-framework\n"))

    registry = load_registry(directory)

    agent = registry.agent("brief")
    assert agent is not None
    assert agent.workers == ()


def test_workers_on_the_default_host_are_not_refused(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    triage = registry.agent(TRIAGE)

    assert triage is not None
    assert triage.workers
    assert triage.host == "langgraph"


# ── the schema ──────────────────────────────────────────────────────────────
def test_the_agents_schema_lists_the_two_hosts_and_the_default(
    real_registry: Path,
) -> None:
    schema = json.loads(
        (real_registry / "schemas" / "agents.schema.json").read_text(encoding="utf-8")
    )

    host = schema["$defs"]["Agent"]["properties"]["host"]

    assert host["enum"] == ["langgraph", "agent-framework"]
    assert host["default"] == "langgraph"
    assert "host" not in schema["$defs"]["Agent"].get("required", [])
