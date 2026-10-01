"""The S009 triage graph: one node that drafts a summary for an adjuster.

The graph never sets headers and never names a provider; it asks the model
client it is given. Routing is a placeholder until S014 (deterministic rules).
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from meridian.runtime.model_client import ModelClient
from meridian.workloads.claims_triage.models import (
    MAX_DRAFT_CHARS,
    ClaimFacts,
    DraftedBy,
    Route,
    TriageProposal,
)

SYSTEM_PROMPT = (
    "You assist an insurance claims adjuster. Write a short triage summary of "
    "the claim below for the adjuster: what happened, what is claimed and "
    "anything that needs checking. Do not decide the claim."
)
REASON = (
    "S009 walking skeleton: every claim goes to an adjuster until the triage "
    "rules exist (S014)."
)


class ClaimState(TypedDict):
    claim: dict[str, Any]
    output: dict[str, Any]


def route_for(claim: dict[str, Any]) -> Route:
    """The S009 placeholder: every claim goes to an adjuster.

    S014's deterministic rules replace this (C-02, T-30): a claim the rules do
    not support always goes to a person, so "adjuster" is the safe default.
    """
    return "adjuster"


def _user_message(claim: ClaimFacts) -> str:
    """The facts the model needs. The graph is not even sent the claimant's
    name and email (data minimisation, T-03)."""
    location = claim.loss_location
    return (
        f"Peril: {claim.peril}\n"
        f"Claimed amount: {claim.claimed_amount}\n"
        f"Loss date: {claim.loss_date.isoformat()}\n"
        f"Reported on: {claim.reported_on.isoformat()}\n"
        f"Loss location: {location.city}, {location.country}\n"
        f"Description: {claim.description}"
    )


def build(model: ModelClient) -> StateGraph:
    """The workload's graph factory, published as the ``claims-triage`` entry
    point. Returned uncompiled: the runtime compiles it with its checkpointer."""

    def draft_proposal(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        result = model.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_message(claim)},
            ]
        )
        # Built through the model the Claims API validates against, so a
        # change to one without the other fails here, inside the graph.
        proposal = TriageProposal(
            route=route_for(state["claim"]),
            reason=REASON,
            draft=result.text[:MAX_DRAFT_CHARS],
            drafted_by=DraftedBy(
                deployment=result.deployment,
                provider=result.provider,
                mode=result.mode,
            ),
        )
        return {"output": proposal.model_dump(mode="json")}

    graph = StateGraph(ClaimState)
    graph.add_node("draft_proposal", draft_proposal)
    graph.add_edge(START, "draft_proposal")
    graph.add_edge("draft_proposal", END)
    return graph
