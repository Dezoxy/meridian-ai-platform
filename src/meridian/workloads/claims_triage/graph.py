"""The claims triage graph (S014, S015): seven nodes, the tools in a fixed order.

The graph's own code calls the tools and asks the model at most once; the model
never chooses a tool or an argument, and ``rules.decide`` chooses the route. Each
call goes through the view of the worker the registry gives the tool (``intake``,
``terms`` or ``approvals``), which the runtime and the tool servers enforce
(S031); the nodes themselves are not yet split by worker. The
state holds plain data only (the runtime runs LangGraph in strict msgpack mode),
and each node validates what it reads back into the typed models.

The graph writes in one place: a claim the rules route to an adjuster. After
``propose``, ``request_approval`` records an approval request through the claims
tool server, and ``await_decision`` pauses the run (``interrupt``). The decision
is read from the record the Claims API committed before it resumed the run: the
resume value carries nothing and is ignored, so nothing that can call the
runtime can decide. The node reads the record through the ``approval_outcome``
tool and fits the answer strictly (``approval-outcome-unfit``); with no
decision recorded it fails (``decision-not-recorded``) before a write, and the
runtime leaves the run paused. It then adds a note from a fixed table, so the
note holds nothing the caller wrote. The Claims API checks that the caller may
decide and records it; the graph only reads it (T-31). The reason sent with the
request is the proposal's reason code, never claim text. Both writes carry a
step and send the same payload when their node runs again, so the tool server's
idempotency key makes a rerun answer with the stored ID (T-23):
``await_decision`` runs again from its start on every resume, so nothing but
the ``interrupt`` and the read of the record comes before its first write.

A platform that cannot answer fails the run, and the claim can be triaged again:
no tool or model error is caught here. Only two things become a proposal for a
person: no such policy, and an assessment that is unavailable: a model answer
that cannot be trusted, a description that holds special-category data or
addresses the model, or a request the provider's content filter refused (S047;
``assessment.py`` turns each into an assessment). A wording clause that
addresses the model is platform data, not the claim's: it fails the run
(``wording-addresses-the-model``). No log line, exception message
or span attribute of the graph holds claim text, a tool result or model text:
a tool result that does not fit its model raises a ``GraphFailure`` whose code
says which answer, not what it held (the graph's own violations all do).
"""

from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from meridian.platform.common.wire import WireModel
from meridian.runtime.failures import GraphFailure
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient

from .assessment import Assessed, assess
from .models import DECISION_NOTES, ClaimFacts, DraftedBy, Outcome
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


def _policy_of(state: ClaimState) -> PolicyRecord:
    """The policy of a run that went past ``lookup_policy`` with one."""
    if state["policy"] is None:
        raise GraphFailure("missing-policy")
    return PolicyRecord.model_validate(state["policy"])


def _terms_of(claim: ClaimFacts, policy: PolicyRecord, state: ClaimState) -> Terms:
    return select_terms(
        claim.peril,
        state["chunks"],
        product=policy.product,
        wording_version=policy.wording_version,
    )


NOT_NEEDED = Assessed(Assessment("not_needed"), None, None, None)


def _assessed_to_state(assessed: Assessed) -> dict[str, Any]:
    drafted_by = assessed.drafted_by
    return {
        "status": assessed.assessment.status,
        "clause": assessed.assessment.clause,
        "rationale": assessed.rationale,
        "drafted_by": drafted_by.model_dump(mode="json") if drafted_by else None,
        "unavailable_because": assessed.unavailable_because,
    }


def _no_policy_next(state: ClaimState) -> str:
    return "propose" if state["policy"] is None else "load_history"


def _after_propose(state: ClaimState) -> str:
    """Only a claim the rules route to an adjuster waits for one."""
    route = TriageProposal.model_validate(state["output"]).route
    return "request_approval" if route == "adjuster" else END


def build(model: ModelClient, tools: ToolClient) -> StateGraph:
    """The workload's graph factory, published as the ``claims-triage`` entry
    point. Returned uncompiled: the runtime compiles it with its checkpointer."""

    def lookup_policy(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        intake = tools.for_worker("intake")
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
        intake = tools.for_worker("intake")
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

    def retrieve_terms(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        policy = _policy_of(state)
        in_force = policy_state(policy, claim.loss_date) == "in_force"
        terms = tools.for_worker("terms")
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

    def assess_exclusions(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        policy = _policy_of(state)
        terms = _terms_of(claim, policy, state)
        if not needs_assessment(claim, policy, terms):
            return {"assessed": _assessed_to_state(NOT_NEEDED)}
        assessed = assess(
            model, claim, policy.product, policy.wording_version, terms.candidates
        )
        return {"assessed": _assessed_to_state(assessed)}

    def propose(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        policy = (
            PolicyRecord.model_validate(state["policy"])
            if state["policy"] is not None
            else None
        )
        # With no policy no node assessed anything. With one, ``assess`` always
        # ran: a state without its assessment is a bug, not a default.
        if policy is None:
            assessed = _assessed_to_state(NOT_NEEDED)
        elif state["assessed"] is None:
            raise GraphFailure("missing-assessment")
        else:
            assessed = state["assessed"]
        assessment = Assessment(assessed["status"], assessed["clause"])
        decision = decide(
            Facts(
                claim=claim,
                policy=policy,
                history=tuple(HistoryEntry.model_validate(e) for e in state["history"]),
                history_truncated=state["history_truncated"],
                terms=_terms_of(claim, policy, state) if policy is not None else None,
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
            unavailable_because=assessed["unavailable_because"],
            rationale=assessed["rationale"],
            drafted_by=DraftedBy.model_validate(drafted_by) if drafted_by else None,
        )
        return {"output": proposal.model_dump(mode="json")}

    def request_approval(state: ClaimState) -> dict[str, Any]:
        claim = ClaimFacts.model_validate(state["claim"])
        # Read back from the checkpointed state, so a rerun sends the same reason.
        proposal = TriageProposal.model_validate(state["output"])
        approvals = tools.for_worker("approvals")
        answer = approvals.call(
            "request_approval",
            {"claim_id": claim.claim_id, "reason": proposal.reason},
            step="request-approval",
        ).data
        requested = _fitted(ApprovalRequested, answer, "approval-request-unfit")
        return {"request_id": str(requested.request_id)}

    def await_decision(state: ClaimState) -> dict[str, Any]:
        # Nothing before the pause: this node runs again from its start on resume.
        # The resume value is ignored: what the caller sent is not the decision,
        # the Claims API's record is.
        interrupt({"request_id": state["request_id"]})
        claim = ClaimFacts.model_validate(state["claim"])
        approvals = tools.for_worker("approvals")
        decision = _fitted(
            ApprovalOutcome,
            approvals.call("approval_outcome", {"claim_id": claim.claim_id}).data,
            "approval-outcome-unfit",
        ).outcome
        if decision is None:
            # Resumed with no decision recorded: leave the run paused.
            raise GraphFailure("decision-not-recorded")
        answer = approvals.call(
            "add_claim_note",
            {"claim_id": claim.claim_id, "note": DECISION_NOTES[decision]},
            step="decision-note",
        ).data
        _fitted(NoteAdded, answer, "decision-note-unfit")
        return {"decision": decision}

    graph = StateGraph(ClaimState)
    graph.add_node("lookup_policy", lookup_policy)
    graph.add_node("load_history", load_history)
    graph.add_node("retrieve_terms", retrieve_terms)
    graph.add_node("assess", assess_exclusions)
    graph.add_node("propose", propose)
    graph.add_node("request_approval", request_approval)
    graph.add_node("await_decision", await_decision)
    graph.add_edge(START, "lookup_policy")
    graph.add_conditional_edges(
        "lookup_policy",
        _no_policy_next,
        {"propose": "propose", "load_history": "load_history"},
    )
    graph.add_edge("load_history", "retrieve_terms")
    graph.add_edge("retrieve_terms", "assess")
    graph.add_edge("assess", "propose")
    graph.add_conditional_edges(
        "propose",
        _after_propose,
        {"request_approval": "request_approval", END: END},
    )
    graph.add_edge("request_approval", "await_decision")
    graph.add_edge("await_decision", END)
    return graph
