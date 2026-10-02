"""The one-node triage graph (S009 placeholder routing)."""

from importlib.metadata import entry_points
from typing import Any

import pytest
from pydantic import ValidationError
from servicesupport import REGISTRY_DIR, claim_with_id, synthetic_claims

from meridian.platform.registry import load_registry
from meridian.runtime.graphs import load_graph_factory
from meridian.runtime.model_client import ChatResult
from meridian.workloads.claims_triage.graph import build, route_for

CLAIM = synthetic_claims()[0]  # CLM-0001
# What the runtime is sent: the claimant's name and email stay in the Claims API.
FACTS = {k: v for k, v in CLAIM.items() if k != "claimant"}
REASON = (
    "S009 walking skeleton: every claim goes to an adjuster until the triage "
    "rules exist (S014)."
)


class StubModel:
    """Stands in for ModelClient; remembers what it was asked."""

    def __init__(self, text: str = "a draft") -> None:
        self.text = text
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: list[dict[str, str]]) -> ChatResult:
        self.calls.append(messages)
        return ChatResult(
            text=self.text,
            deployment="replay-chat",
            provider="replay",
            model="replay-chat",
            mode="replay",
            input_tokens=1,
            output_tokens=1,
        )


class StubTools:
    """Stands in for ToolClient; the graph calls no tool before S014."""


def run_graph(model: StubModel, claim: dict[str, Any] = FACTS) -> dict[str, Any]:
    return build(model, StubTools()).compile().invoke({"claim": claim})


def test_the_graph_drafts_a_proposal_routed_to_an_adjuster() -> None:
    model = StubModel("a draft")

    result = run_graph(model)

    assert result["output"] == {
        "route": "adjuster",
        "reason": REASON,
        "draft": "a draft",
        "drafted_by": {
            "deployment": "replay-chat",
            "provider": "replay",
            "mode": "replay",
        },
    }
    assert len(model.calls) == 1


def test_the_prompt_leaves_out_the_claimants_name_and_email() -> None:
    model = StubModel()

    run_graph(model)

    system, user = model.calls[0]
    assert system["role"] == "system"
    assert user["role"] == "user"
    everything = system["content"] + user["content"]
    assert CLAIM["claimant"]["name"] not in everything
    assert CLAIM["claimant"]["email"] not in everything
    for needed in (
        CLAIM["peril"],
        str(CLAIM["claimed_amount"]),
        CLAIM["loss_date"],
        CLAIM["reported_on"],
        CLAIM["loss_location"]["city"],
        CLAIM["description"],
    ):
        assert needed in user["content"]


def test_a_long_draft_is_cut_to_2000_characters() -> None:
    result = run_graph(StubModel("d" * 2500))

    assert len(result["output"]["draft"]) == 2000


@pytest.mark.parametrize("claim", synthetic_claims(), ids=lambda c: c["claim_id"])
def test_every_claim_goes_to_an_adjuster(claim: dict[str, Any]) -> None:
    assert route_for(claim) == "adjuster"


def test_a_claim_that_is_not_valid_facts_fails_the_run() -> None:
    facts = {k: v for k, v in claim_with_id("CLM-9002").items() if k != "claimant"}

    with pytest.raises(ValueError, match="validation error"):
        run_graph(StubModel(), {**facts, "peril": "meteor"})


def test_a_claim_that_still_carries_the_claimant_is_refused_by_the_graph() -> None:
    model = StubModel()

    with pytest.raises(ValidationError):
        run_graph(model, CLAIM)

    assert model.calls == []


def test_a_draft_the_proposal_model_would_refuse_fails_inside_the_graph() -> None:
    with pytest.raises(ValidationError):
        run_graph(StubModel(""))  # an empty draft is not a TriageProposal


def test_the_graph_is_returned_uncompiled() -> None:
    assert not hasattr(build(StubModel(), StubTools()), "invoke")


def test_the_entry_point_is_installed_from_the_meridian_distribution() -> None:
    (entry,) = [
        e for e in entry_points(group="meridian.graphs") if e.name == "claims-triage"
    ]

    assert entry.value == "meridian.workloads.claims_triage.graph:build"
    assert entry.dist is not None and entry.dist.name == "meridian"


def test_the_runtime_loads_the_workload_graph_through_the_registry() -> None:
    factory = load_graph_factory("claims-triage", load_registry(REGISTRY_DIR))

    assert factory is build
