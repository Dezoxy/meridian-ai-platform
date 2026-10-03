"""What the registry declares about structured outputs (S051) and its check.

A deployment says whether it honours a JSON schema for the answer; an agent
says whether it may send one. When an agent does, every chat candidate and the
chat replay deployment must honour it, or the gateway would meet a schema it
cannot serve at run time. The boundary of each rule gets both sides.
"""

from collections.abc import Callable
from pathlib import Path

from meridian.platform.registry import load_registry
from meridian.platform.registry.models import Agent, Deployment

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]
Edit = tuple[str, str, str]

DECLARED = "    structured_outputs: true\n"
EMBEDDING_ANCHOR = "    dimensions: 1024\n"
REPLAY_EMBEDDING_ANCHOR = '    model: replay-embedding\n    version: "1"\n'
AGENT_DECLARED: Edit = (
    "agents.yaml",
    "    description: Triages a new claim and prepares it for an adjuster.\n"
    + DECLARED,
    "    description: Triages a new claim and prepares it for an adjuster.\n",
)
JUDGE_DECLARED: Edit = (
    "agents.yaml",
    "    tools: []\n" + DECLARED,
    "    tools: []\n",
)
# Both agents that declare it, taken off: nobody asks.
NO_AGENT_ASKS = (AGENT_DECLARED, JUDGE_DECLARED)
GPT4O = "aoai-sdc-gpt-4o"
GPT4O_B = "aoai-sdc-gpt-4o-b"
REPLAY_CHAT = "replay-chat"
RECORDED_CHAT = "recorded-chat"


def undeclare(directory: Path, *deployments: str) -> Path:
    """Take the declaration off each named deployment of the copy."""
    path = directory / "models.yaml"
    text = path.read_text(encoding="utf-8")
    for name in deployments:
        head, found, tail = text.partition("  - id: " + name + "\n")
        assert DECLARED in tail, f"{name} declares nothing to take off"
        text = head + found + tail.replace(DECLARED, "", 1)
    path.write_text(text, encoding="utf-8")
    return directory


def strip_every_declaration(plant: Plant) -> Path:
    """A registry in which nothing declares structured outputs."""
    directory = plant(*NO_AGENT_ASKS)
    return undeclare(directory, GPT4O, GPT4O_B, REPLAY_CHAT, RECORDED_CHAT)


ROUTE_ROLE = "a candidate of the 'chat' route"
REPLAY_ROLE = "the replay deployment for purpose 'chat'"


ASKING = "agents 'claims-triage' and 'evaluation-judge' declare"


def required_error(index: int, deployment: str, role: str, asking: str = ASKING) -> str:
    return (
        f"models.yaml: deployments[{index}].structured_outputs: required because "
        f"{asking} structured_outputs and this deployment is "
        f"{role} (deployment {deployment!r})"
    )


# ── the fields ──────────────────────────────────────────────────────────────
def test_the_fields_default_to_false() -> None:
    assert Deployment.model_fields["structured_outputs"].default is False
    assert Agent.model_fields["structured_outputs"].default is False


def test_the_real_registry_declares_it_for_the_chat_deployments_and_two_agents(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)

    assert {d.id: d.structured_outputs for d in registry.deployments} == {
        "aoai-sdc-gpt-4o": True,
        "aoai-sdc-gpt-4o-b": True,
        "aoai-sdc-text-embedding-3-large": False,
        "replay-chat": True,
        "replay-embedding": False,
        "recorded-chat": True,
    }
    assert {a.id: a.structured_outputs for a in registry.agents} == {
        "claims-triage": True,
        "knowledge-ingestion": False,
        "evaluation-judge": True,
    }


# ── a deployment of purpose embedding cannot honour a schema ────────────────
def test_an_embedding_deployment_that_declares_it_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(
        plant(("models.yaml", EMBEDDING_ANCHOR, EMBEDDING_ANCHOR + DECLARED))
    )

    assert errors == (
        "models.yaml: deployments[2].structured_outputs: must not be set for "
        "purpose 'embedding' (deployment 'aoai-sdc-text-embedding-3-large')",
    )


def test_a_replay_embedding_deployment_that_declares_it_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(
        plant(
            (
                "models.yaml",
                REPLAY_EMBEDDING_ANCHOR,
                REPLAY_EMBEDDING_ANCHOR + DECLARED,
            )
        )
    )

    assert errors == (
        "models.yaml: deployments[4].structured_outputs: must not be set for "
        "purpose 'embedding' (deployment 'replay-embedding')",
    )


def test_an_embedding_deployment_that_declares_it_is_reported_with_no_agent_asking(
    plant: Plant, load_errors: LoadErrors
) -> None:
    strip_every_declaration(plant)
    errors = load_errors(
        plant(("models.yaml", EMBEDDING_ANCHOR, EMBEDDING_ANCHOR + DECLARED))
    )

    assert len(errors) == 1
    assert errors[0].startswith("models.yaml: deployments[2].structured_outputs: ")


# ── an agent that declares it needs every chat deployment to honour it ──────
def test_a_chat_candidate_that_does_not_declare_it_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(undeclare(plant(), GPT4O_B))

    assert errors == (required_error(1, GPT4O_B, ROUTE_ROLE),)


def test_the_chat_replay_deployment_that_does_not_declare_it_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(undeclare(plant(), REPLAY_CHAT))

    assert errors == (required_error(3, REPLAY_CHAT, REPLAY_ROLE),)


def test_every_chat_deployment_that_does_not_declare_it_is_reported_once(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(undeclare(plant(), GPT4O, GPT4O_B, REPLAY_CHAT))

    assert errors == (
        required_error(0, GPT4O, ROUTE_ROLE),
        required_error(1, GPT4O_B, ROUTE_ROLE),
        required_error(3, REPLAY_CHAT, REPLAY_ROLE),
    )


# ── nobody asks, nothing is required ────────────────────────────────────────
def test_no_agent_and_no_deployment_declaring_it_is_accepted(plant: Plant) -> None:
    directory = strip_every_declaration(plant)

    registry = load_registry(directory)

    assert not any(a.structured_outputs for a in registry.agents)
    assert not any(d.structured_outputs for d in registry.deployments)


def test_deployments_declaring_it_with_no_agent_asking_are_accepted(
    plant: Plant,
) -> None:
    directory = plant(*NO_AGENT_ASKS)

    registry = load_registry(directory)

    assert not any(a.structured_outputs for a in registry.agents)
    assert registry.deployment("aoai-sdc-gpt-4o") is not None


# ── a missing deployment is another check's error ───────────────────────────
def test_a_route_candidate_that_does_not_exist_is_reported_by_the_reference_check_only(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(
        plant(
            (
                "policies.yaml",
                "candidates: [aoai-sdc-gpt-4o, aoai-sdc-gpt-4o-b]",
                "candidates: [aoai-sdc-gpt-4o, ghost]",
            )
        )
    )

    assert not any("structured_outputs" in error for error in errors)
    assert (
        "policies.yaml: routes[0].candidates[1]: unknown deployment 'ghost'" in errors
    )


def test_a_replay_entry_that_does_not_exist_is_reported_by_the_replay_check_only(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(
        plant(("policies.yaml", "deployment: replay-chat", "deployment: ghost"))
    )

    assert not any("structured_outputs" in error for error in errors)
    assert "policies.yaml: replay[0].deployment: unknown deployment 'ghost'" in errors
