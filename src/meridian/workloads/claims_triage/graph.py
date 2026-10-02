"""The claims triage graph (S014): five nodes, the tools in a fixed order.

The graph's own code calls the tools and asks the model at most once; the model
never chooses a tool or an argument, and ``rules.decide`` chooses the route. The
graph reads and writes nothing: the approval path is S015. The state holds plain
data only (the runtime runs LangGraph in strict msgpack mode), and each node
validates what it reads back into the typed models.

A platform that cannot answer fails the run, and the claim can be triaged again:
no tool or model error is caught here. Only two things become a proposal for a
person: no such policy, and a model answer that cannot be trusted (that one is
turned into an assessment by ``assessment.py``). No log line, exception message
or span attribute of the graph holds claim text, a tool result or model text:
a tool result that does not fit its model raises an error that says which
answer, not what it held.
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient

from .assessment import assess
from .models import ClaimFacts, DraftedBy
from .proposal import Citation, TriageProposal
from .rules import (
    Assessment,
    Facts,
    HistoryEntry,
    PolicyRecord,
    decide,
    needs_assessment,
    policy_state,
)
from .wording import Terms, probes, select_terms


class ClaimState(TypedDict):
    claim: dict[str, Any]
    policy: dict[str, Any] | None
    history: list[dict[str, Any]]
    history_truncated: bool
    chunks: list[dict[str, Any]]
    assessed: dict[str, Any] | None
    output: dict[str, Any]


def _fitted[M: BaseModel](model: type[M], answer: Any, what: str) -> M:
    """``answer`` as ``model``. pydantic's own error quotes the value that did
    not fit, and that value came from a tool."""
    try:
        return model.model_validate(answer)
    except ValidationError:
        raise ValueError(f"the {what} does not fit its model") from None


def _policy_of(state: ClaimState) -> PolicyRecord:
    """The policy of a run that went past ``lookup_policy`` with one."""
    if state["policy"] is None:
        raise ValueError("a node that needs a policy ran without one")
    return PolicyRecord.model_validate(state["policy"])


def _terms_of(claim: ClaimFacts, state: ClaimState) -> Terms:
    return select_terms(claim.peril, state["chunks"])


def _assessed_to_state(
    assessment: Assessment, rationale: str | None, drafted_by: DraftedBy | None
) -> dict[str, Any]:
    return {
        "status": assessment.status,
        "clause": assessment.clause,
        "rationale": rationale,
        "drafted_by": drafted_by.model_dump(mode="json") if drafted_by else None,
    }


def _no_policy_next(state: ClaimState) -> str:
    return "propose" if state["policy"] is None else "load_history"


def build(model: ModelClient, tools: ToolClient) -> StateGraph:
    """The workload's graph factory, published as the ``claims-triage`` entry
    point. Returned uncompiled: the runtime compiles it with its checkpointer."""

    def lookup_policy(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        found = tools.call("policy_lookup", {"policy_number": claim.policy_number}).data
        # The later keys start empty so that every node reads a key that is set,
        # also on the path that skips them.
        empty: dict[str, Any] = {
            "policy": None,
            "history": [],
            "history_truncated": False,
            "chunks": [],
            "assessed": None,
        }
        if not found["found"]:
            return empty
        policy = _fitted(PolicyRecord, found["policy"], "policy record")
        return {**empty, "policy": policy.model_dump(mode="json")}

    def load_history(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        found = tools.call("claim_history", {"policy_number": claim.policy_number}).data
        entries = [
            _fitted(HistoryEntry, entry, "history entry") for entry in found["entries"]
        ]
        return {
            "history": [entry.model_dump(mode="json") for entry in entries],
            "history_truncated": found["truncated"],
        }

    def retrieve_terms(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        policy = _policy_of(state)
        in_force = policy_state(policy, claim.loss_date) == "in_force"
        chunks: list[dict[str, Any]] = []
        for query in probes(claim.peril, in_force=in_force):
            found = tools.call(
                "wording_search", {"query": query, "product": policy.product}
            ).data
            if (
                found["product"] != policy.product
                or found["wording_version"] != policy.wording_version
            ):
                # The platform contradicts itself: the clauses are not the
                # policy's. Neither value is repeated: both came from a tool.
                raise ValueError("the wording search answered for another wording")
            chunks.extend(dict(chunk) for chunk in found["chunks"])
        return {"chunks": chunks}

    def assess_exclusions(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        policy = _policy_of(state)
        terms = _terms_of(claim, state)
        if not needs_assessment(claim, policy, terms):
            not_needed = _assessed_to_state(Assessment("not_needed"), None, None)
            return {"assessed": not_needed}
        assessed = assess(
            model, claim, policy.product, policy.wording_version, terms.candidates
        )
        return {
            "assessed": _assessed_to_state(
                assessed.assessment, assessed.rationale, assessed.drafted_by
            )
        }

    def propose(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        policy = (
            PolicyRecord.model_validate(state["policy"])
            if state["policy"] is not None
            else None
        )
        assessed = state["assessed"] or _assessed_to_state(
            Assessment("not_needed"), None, None
        )
        assessment = Assessment(assessed["status"], assessed["clause"])
        decision = decide(
            Facts(
                claim=claim,
                policy=policy,
                history=tuple(HistoryEntry.model_validate(e) for e in state["history"]),
                history_truncated=state["history_truncated"],
                terms=_terms_of(claim, state) if policy is not None else None,
                assessment=assessment,
            )
        )
        citations = (
            ()
            if policy is None
            else tuple(
                Citation(
                    product=policy.product,
                    wording_version=policy.wording_version,
                    clause=clause,
                )
                for clause in decision.citations
            )
        )
        drafted_by = assessed["drafted_by"]
        proposal = TriageProposal(
            route=decision.route,
            reason=decision.reason,
            recommendation=decision.recommendation,
            payable_amount=decision.payable_amount,
            exclusion_clause=decision.exclusion_clause,
            fraud_indicators=decision.fraud_indicators,
            missing_documents=decision.missing_documents,
            citations=citations,
            gaps=decision.gaps,
            assessment=assessment.status,
            rationale=assessed["rationale"],
            drafted_by=DraftedBy.model_validate(drafted_by) if drafted_by else None,
        )
        return {"output": proposal.model_dump(mode="json")}

    graph = StateGraph(ClaimState)
    graph.add_node("lookup_policy", lookup_policy)
    graph.add_node("load_history", load_history)
    graph.add_node("retrieve_terms", retrieve_terms)
    graph.add_node("assess", assess_exclusions)
    graph.add_node("propose", propose)
    graph.add_edge(START, "lookup_policy")
    graph.add_conditional_edges(
        "lookup_policy",
        _no_policy_next,
        {"propose": "propose", "load_history": "load_history"},
    )
    graph.add_edge("load_history", "retrieve_terms")
    graph.add_edge("retrieve_terms", "assess")
    graph.add_edge("assess", "propose")
    graph.add_edge("propose", END)
    return graph
