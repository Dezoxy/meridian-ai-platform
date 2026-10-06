"""Workers: a part of one agent, with a tool list of their own (S031).

The declaration and its checks only; nothing reads a worker at run time yet.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from meridian.platform.registry import load_registry, models
from meridian.platform.registry.models import ENTITY_ID_MAX_LENGTH, EntityId, Worker
from meridian.platform.toolserver.wire import WORKER_PATTERN

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]

TRIAGE = "claims-triage"
TRIAGE_WORKERS = {
    "intake": ("policy_lookup", "claim_history"),
    "terms": ("wording_search",),
    "assessor": (),
    "approvals": ("request_approval", "approval_outcome", "add_claim_note"),
}
# A worker's tool sits ten spaces in; the agent's own list, written first,
# sits six, so an edit on the six-space line lands in the agent's list.
JOB_WITH_A_WORKER = (
    "agents.yaml",
    "    kind: job\n    tools: []\n",
    "    kind: job\n    tools: []\n    workers:\n      - id: embedder\n"
    "        description: Embeds a wording.\n        tools: []\n",
)

CHECK_CASES = [
    pytest.param(
        [("agents.yaml", "      - id: terms\n", "      - id: intake\n")],
        "agents.yaml: agents[0].workers[1].id: worker id 'intake' is used twice "
        "in agent 'claims-triage'",
        id="duplicate-worker-id",
    ),
    pytest.param(
        [("agents.yaml", "      - id: assessor\n", "      - id: claims-triage\n")],
        "agents.yaml: agents[0].workers[2].id: worker id 'claims-triage' is the "
        "id of its own agent",
        id="worker-id-is-the-agents-id",
    ),
    pytest.param(
        [
            (
                "agents.yaml",
                "          - claim_history\n",
                "          - claim_history\n          - ghost_tool\n",
            )
        ],
        "agents.yaml: agents[0].workers[0].tools[2]: unknown tool 'ghost_tool'",
        id="unknown-tool",
    ),
    pytest.param(
        [("agents.yaml", "      - add_claim_note\n", "")],
        "agents.yaml: agents[0].workers[3].tools[2]: tool 'add_claim_note' is not "
        "in the tools of agent 'claims-triage'",
        id="tool-not-the-agents",
    ),
    pytest.param(
        [
            (
                "agents.yaml",
                "          - claim_history\n",
                "          - claim_history\n          - claim_history\n",
            )
        ],
        "agents.yaml: agents[0].workers[0].tools: tool 'claim_history' is listed twice",
        id="tool-twice-in-a-worker",
    ),
    pytest.param(
        [("agents.yaml", "          - add_claim_note\n", "")],
        "agents.yaml: agents[0].tools[3]: tool 'add_claim_note' of agent "
        "'claims-triage' is on no worker",
        id="agent-tool-on-no-worker",
    ),
    pytest.param(
        [
            (
                "agents.yaml",
                "          - wording_search\n",
                "          - wording_search\n          - policy_lookup\n",
            )
        ],
        "agents.yaml: agents[0].workers[1].tools[1]: tool 'policy_lookup' is "
        "already on worker 'intake' of agent 'claims-triage'; one tool has one "
        "worker",
        id="tool-on-two-workers",
    ),
    pytest.param(
        [JOB_WITH_A_WORKER],
        "agents.yaml: agents[1].workers[0]: job agent 'knowledge-ingestion' "
        "declares worker 'embedder'; a job has no run row, so no tool server "
        "could bind a worker's call",
        id="job-agent-with-a-worker",
    ),
]


@pytest.mark.parametrize(("edits", "message"), CHECK_CASES)
def test_a_worker_that_breaks_a_rule_is_reported(
    edits: list[tuple[str, str, str]],
    message: str,
    plant: Plant,
    load_errors: LoadErrors,
) -> None:
    directory = plant(*edits)

    errors = load_errors(directory)

    assert errors == (message,)


def test_the_real_registry_declares_the_four_workers_of_claims_triage(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)

    agent = registry.agent(TRIAGE)

    assert agent is not None
    assert {w.id: w.tools for w in agent.workers} == TRIAGE_WORKERS
    assert [w.id for w in agent.workers] == list(TRIAGE_WORKERS)
    assert all(w.description for w in agent.workers)


def test_the_workers_tools_together_are_the_agents_tools(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    agent = registry.agent(TRIAGE)

    assert agent is not None
    listed = [tool for worker in agent.workers for tool in worker.tools]
    assert sorted(listed) == sorted(agent.tools)
    assert len(listed) == len(set(listed))


def test_the_job_agents_declare_no_worker(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    jobs = [agent for agent in registry.agents if agent.kind == "job"]

    assert jobs
    assert all(agent.workers == () for agent in jobs)


def test_an_agent_without_workers_dumps_without_the_key(real_registry: Path) -> None:
    registry = load_registry(real_registry)
    judge = registry.agent("evaluation-judge")
    triage = registry.agent(TRIAGE)

    assert judge is not None
    assert triage is not None
    assert "workers" not in judge.model_dump(mode="json")
    assert "workers" in triage.model_dump(mode="json")


def test_a_worker_without_tools_may_declare_an_empty_list(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    agent = registry.agent(TRIAGE)

    assert agent is not None
    worker = agent.worker("assessor")
    assert worker is not None
    assert worker.tools == ()


def test_worker_returns_the_declared_worker(real_registry: Path) -> None:
    registry = load_registry(real_registry)
    agent = registry.agent(TRIAGE)

    assert agent is not None
    worker = agent.worker("intake")

    assert isinstance(worker, Worker)
    assert worker.id == "intake"
    assert worker.tools == ("policy_lookup", "claim_history")


def test_worker_finds_nothing_the_agent_does_not_declare(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)
    triage = registry.agent(TRIAGE)
    judge = registry.agent("evaluation-judge")

    assert triage is not None
    assert judge is not None
    assert triage.worker("ghost") is None
    assert judge.worker("intake") is None


def test_the_registry_has_one_way_to_find_a_worker(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    assert not hasattr(registry, "worker")
    assert not hasattr(models, "UnknownAgentError")
    assert not hasattr(models, "UnknownWorkerError")


# ── one length for an ID, here and on the wire ──────────────────────────────
def test_a_worker_id_of_the_longest_length_loads(plant: Plant) -> None:
    longest = "w" * ENTITY_ID_MAX_LENGTH
    directory = plant(
        ("agents.yaml", "      - id: terms\n", f"      - id: {longest}\n")
    )

    registry = load_registry(directory)

    triage = registry.agent(TRIAGE)
    assert triage is not None
    assert triage.worker(longest) is not None


def test_a_worker_id_one_longer_is_a_load_error(
    plant: Plant, load_errors: LoadErrors
) -> None:
    too_long = "w" * (ENTITY_ID_MAX_LENGTH + 1)
    directory = plant(
        ("agents.yaml", "      - id: terms\n", f"      - id: {too_long}\n")
    )

    errors = load_errors(directory)

    assert errors == (
        f"agents.yaml: agents[0].workers[1].id: String should have at most "
        f"{ENTITY_ID_MAX_LENGTH} characters",
    )


def test_any_other_id_one_over_the_length_is_a_load_error_too(
    plant: Plant, load_errors: LoadErrors
) -> None:
    too_long = "a" * (ENTITY_ID_MAX_LENGTH + 1)
    directory = plant(
        ("tenants.yaml", "  - id: claims-triage\n", f"  - id: {too_long}\n")
    )

    errors = load_errors(directory)

    assert any("String should have at most 64 characters" in e for e in errors)


def test_the_wire_accepts_exactly_the_ids_the_registry_accepts() -> None:
    entity = TypeAdapter(EntityId)
    for length in (1, ENTITY_ID_MAX_LENGTH, ENTITY_ID_MAX_LENGTH + 1):
        candidate = "a" * length

        in_registry = _accepts(entity, candidate)
        on_the_wire = WORKER_PATTERN.fullmatch(candidate) is not None

        assert in_registry == on_the_wire


def _accepts(adapter: TypeAdapter[str], candidate: str) -> bool:
    try:
        adapter.validate_python(candidate)
    except ValidationError:
        return False
    return True


# ── the shape of a worker, at load ──────────────────────────────────────────
@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        pytest.param(
            "      - id: terms\n",
            "      - id: terms\n        colour: red\n",
            "agents.yaml: agents[0].workers[1].colour: Extra inputs are not permitted",
            id="unknown-key",
        ),
        pytest.param(
            "      - id: terms\n",
            "      - id: Terms\n",
            "agents.yaml: agents[0].workers[1].id: String should match pattern "
            "'^[a-z0-9][a-z0-9-]*$'",
            id="id-not-an-entity-id",
        ),
        pytest.param(
            "        description: Searches the wording of the claim's own policy.\n",
            '        description: ""\n',
            "agents.yaml: agents[0].workers[1].description: String should have at "
            "least 1 character",
            id="empty-description",
        ),
        pytest.param(
            "          - wording_search\n",
            "          - Wording Search\n",
            "agents.yaml: agents[0].workers[1].tools[0]: String should match "
            "pattern '^[a-z][a-z0-9_]*$'",
            id="tool-not-a-tool-id",
        ),
    ],
)
def test_a_malformed_worker_is_a_load_error(
    old: str, new: str, message: str, plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(("agents.yaml", old, new))

    errors = load_errors(directory)

    assert message in errors


def test_a_worker_without_a_description_is_a_load_error(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "agents.yaml",
            "        description: Searches the wording of the claim's own policy.\n",
            "",
        )
    )

    errors = load_errors(directory)

    assert "agents.yaml: agents[0].workers[1].description: Field required" in errors


def test_an_agent_file_without_the_workers_key_loads_as_before(
    plant: Plant,
) -> None:
    directory = plant()
    text = (directory / "agents.yaml").read_text(encoding="utf-8")
    start = text.index("    workers:\n")
    end = text.index("  - id: knowledge-ingestion")
    (directory / "agents.yaml").write_text(text[:start] + text[end:], encoding="utf-8")

    registry = load_registry(directory)

    triage = registry.agent(TRIAGE)
    assert triage is not None
    assert triage.workers == ()
