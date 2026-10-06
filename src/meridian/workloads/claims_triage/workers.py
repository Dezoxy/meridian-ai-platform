"""The four workers of the claims triage (S031), each a compiled subgraph built
by a function of its own that takes what the worker needs and no more.

``intake`` reads the policy and the claim history, ``terms`` searches the
wording, ``assessor`` asks the model its one question and ``approvals`` asks for
the approval and, after the pause, reads the outcome and writes the note. The
registry (``config/registry/agents.yaml``) says which tools each holds. A
builder that needs tools gets the view of its own worker
(``tools.for_worker(...)``), which the runtime and the tool servers enforce, and
the assessor's builder gets the model client and no tool client at all: its
nodes could not call a tool if they tried. No builder gets both.

The nodes are the ones the graph had before the split, with their bodies,
failure codes and idempotency step labels unchanged. Every node is labelled with
its worker (``meridian.worker`` in the node's metadata), which the runtime puts
on the node's span. The state is one ``ClaimState`` for the supervisor and every
worker: plain data only, as the runtime runs LangGraph in strict msgpack mode.
"""

from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from meridian.platform.common.wire import WireModel
from meridian.runtime.failures import GraphFailure
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient
from meridian.runtime.tracing import WORKER_KEY

from .assessment import Assessed, assess
from .models import DECISION_NOTES, ClaimFacts, Outcome
from .proposal import TriageProposal
from .rules import (
    Assessment,
    HistoryEntry,
    PolicyRecord,
    needs_assessment,
    policy_state,
)
from .wording import Terms, probes, select_terms

# The worker IDs of the registry's ``claims-triage`` entry. The registry check
# and a test keep them the same.
INTAKE = "intake"
TERMS = "terms"
ASSESSOR = "assessor"
APPROVALS = "approvals"


class ClaimState(TypedDict):
    claim: dict[str, Any]
    policy: dict[str, Any] | None
    history: list[dict[str, Any]]
    history_truncated: bool
    chunks: list[dict[str, Any]]
    assessed: dict[str, Any] | None
    output: dict[str, Any]
    request_id: str | None
    decision: str | None


class ApprovalRequested(WireModel):
    """The answer of ``request_approval``."""

    request_id: UUID
    replayed: StrictBool


class ApprovalOutcome(WireModel):
    """The answer of ``approval_outcome``: the outcome word the Claims API
    recorded for this run, none while there is none. Strict, so no type is
    coerced into a word."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Outcome | None = None


class NoteAdded(WireModel):
    """The answer of ``add_claim_note``."""

    note_id: UUID
    replayed: StrictBool


def _fitted[M: BaseModel](model: type[M], answer: Any, code: str) -> M:
    """``answer`` as ``model``, or a ``GraphFailure`` with ``code``. pydantic's
    own error quotes the value that did not fit, and that value came from a
    tool."""
    try:
        return model.model_validate(answer)
    except ValidationError:
        raise GraphFailure(code) from None


def policy_of(state: ClaimState) -> PolicyRecord:
    """The policy of a run that went past ``lookup_policy`` with one."""
    if state["policy"] is None:
        raise GraphFailure("missing-policy")
    return PolicyRecord.model_validate(state["policy"])


def terms_of(claim: ClaimFacts, policy: PolicyRecord, state: ClaimState) -> Terms:
    return select_terms(
        claim.peril,
        state["chunks"],
        product=policy.product,
        wording_version=policy.wording_version,
    )


NOT_NEEDED = Assessed(Assessment("not_needed"), None, None, None)


def assessed_to_state(assessed: Assessed) -> dict[str, Any]:
    drafted_by = assessed.drafted_by
    return {
        "status": assessed.assessment.status,
        "clause": assessed.assessment.clause,
        "rationale": assessed.rationale,
        "drafted_by": drafted_by.model_dump(mode="json") if drafted_by else None,
        "unavailable_because": assessed.unavailable_because,
    }


def _label(worker: str) -> dict[str, Any]:
    return {WORKER_KEY: worker}


def _policy_found(state: ClaimState) -> str:
    return END if state["policy"] is None else "load_history"


def build_intake(intake: ToolClient) -> CompiledStateGraph:
    """The policy, then the claim history of a claim that has a policy. ``intake``
    is the view of the ``intake`` worker."""

    def lookup_policy(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        number = {"policy_number": claim.policy_number}
        found = intake.call("policy_lookup", number).data
        # The later keys start empty so that every node reads a key that is set,
        # also on the path that skips them.
        empty: dict[str, Any] = {
            "policy": None,
            "history": [],
            "history_truncated": False,
            "chunks": [],
            "assessed": None,
            "request_id": None,
            "decision": None,
        }
        if not found["found"]:
            return empty
        policy = _fitted(PolicyRecord, found["policy"], "policy-record-unfit")
        return {**empty, "policy": policy.model_dump(mode="json")}

    def load_history(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        number = {"policy_number": claim.policy_number}
        found = intake.call("claim_history", number).data
        entries = [
            _fitted(HistoryEntry, entry, "history-entry-unfit")
            for entry in found["entries"]
        ]
        return {
            "history": [entry.model_dump(mode="json") for entry in entries],
            "history_truncated": found["truncated"],
        }

    graph = StateGraph(ClaimState)
    graph.add_node("lookup_policy", lookup_policy, metadata=_label(INTAKE))
    graph.add_node("load_history", load_history, metadata=_label(INTAKE))
    graph.add_edge(START, "lookup_policy")
    graph.add_conditional_edges(
        "lookup_policy", _policy_found, {END: END, "load_history": "load_history"}
    )
    graph.add_edge("load_history", END)
    return graph.compile()


def build_terms(terms: ToolClient) -> CompiledStateGraph:
    """The wording clauses of the claim's peril. ``terms`` is the view of the
    ``terms`` worker."""

    def retrieve_terms(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        policy = policy_of(state)
        in_force = policy_state(policy, claim.loss_date) == "in_force"
        chunks: list[dict[str, Any]] = []
        for query in probes(claim.peril, in_force=in_force):
            found = terms.call(
                "wording_search", {"query": query, "product": policy.product}
            ).data
            if (
                found["product"] != policy.product
                or found["wording_version"] != policy.wording_version
            ):
                # The platform contradicts itself: the clauses are not the
                # policy's. Neither value is repeated: both came from a tool.
                raise GraphFailure("other-wording")
            chunks.extend(dict(chunk) for chunk in found["chunks"])
        return {"chunks": chunks}

    graph = StateGraph(ClaimState)
    graph.add_node("retrieve_terms", retrieve_terms, metadata=_label(TERMS))
    graph.add_edge(START, "retrieve_terms")
    graph.add_edge("retrieve_terms", END)
    return graph.compile()


def build_assessor(model: ModelClient) -> CompiledStateGraph:
    """The model's one fact, when the rules need it. It gets the model client and
    no tool client: the worker holds no tool."""

    def assess_exclusions(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        policy = policy_of(state)
        terms = terms_of(claim, policy, state)
        if not needs_assessment(claim, policy, terms):
            return {"assessed": assessed_to_state(NOT_NEEDED)}
        assessed = assess(
            model, claim, policy.product, policy.wording_version, terms.candidates
        )
        return {"assessed": assessed_to_state(assessed)}

    graph = StateGraph(ClaimState)
    graph.add_node("assess", assess_exclusions, metadata=_label(ASSESSOR))
    graph.add_edge(START, "assess")
    graph.add_edge("assess", END)
    return graph.compile()


def build_request_approval(approvals: ToolClient) -> CompiledStateGraph:
    """The approval request of a claim the rules route to an adjuster.
    ``approvals`` is the view of the ``approvals`` worker."""

    def request_approval(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        # Read back from the checkpointed state, so a rerun sends the same reason.
        proposal = TriageProposal.model_validate(state["output"])
        answer = approvals.call(
            "request_approval",
            {"claim_id": claim.claim_id, "reason": proposal.reason},
            step="request-approval",
        ).data
        requested = _fitted(ApprovalRequested, answer, "approval-request-unfit")
        return {"request_id": str(requested.request_id)}

    graph = StateGraph(ClaimState)
    graph.add_node("request_approval", request_approval, metadata=_label(APPROVALS))
    graph.add_edge(START, "request_approval")
    graph.add_edge("request_approval", END)
    return graph.compile()


def build_outcome(approvals: ToolClient) -> CompiledStateGraph:
    """What follows the pause: the outcome the Claims API recorded, then the
    note of a fixed text. Two nodes, so that a note that failed is written again
    without reading the outcome again. ``approvals`` is the view of the
    ``approvals`` worker."""

    def read_outcome(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        decision = _fitted(
            ApprovalOutcome,
            approvals.call("approval_outcome", {"claim_id": claim.claim_id}).data,
            "approval-outcome-unfit",
        ).outcome
        if decision is None:
            # Resumed with no decision recorded: leave the run paused.
            raise GraphFailure("decision-not-recorded")
        return {"decision": decision}

    def write_note(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        decision = state["decision"]
        if decision is None:
            raise GraphFailure("missing-decision")
        answer = approvals.call(
            "add_claim_note",
            {"claim_id": claim.claim_id, "note": DECISION_NOTES[decision]},
            step="decision-note",
        ).data
        _fitted(NoteAdded, answer, "decision-note-unfit")
        return {}

    graph = StateGraph(ClaimState)
    graph.add_node("read_outcome", read_outcome, metadata=_label(APPROVALS))
    graph.add_node("write_note", write_note, metadata=_label(APPROVALS))
    graph.add_edge(START, "read_outcome")
    graph.add_edge("read_outcome", "write_note")
    graph.add_edge("write_note", END)
    return graph.compile()
