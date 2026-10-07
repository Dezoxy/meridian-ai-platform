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
failure codes and idempotency step labels unchanged (S067 added three codes,
``claim-not-valid``, ``wording-version-unknown`` and ``posted-flag-not-valid``,
and read the claim through ``claim_of``). Every node is labelled with
its worker (``meridian.worker`` in the node's metadata), which the runtime puts
on the node's span. The state is one ``ClaimState`` for the supervisor and every
worker: plain data only, as the runtime runs LangGraph in strict msgpack mode.

The workers run one after another. A worker's subgraph answers with the whole
state, so two workers in parallel would both write every key and LangGraph would
refuse the step; parallel workers would need an output schema each.
"""

import logging
from typing import Any, NotRequired, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError
from pydantic_core import ErrorDetails

from meridian.platform.common.wire import WireModel
from meridian.runtime.failures import GraphFailure
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient
from meridian.runtime.tracing import WORKER_KEY

from .assessment import Assessed, assess
from .models import DECISION_NOTES, ClaimFacts, Outcome
from .posted_text import POSTED_TEXT_FLAG
from .proposal import TriageProposal
from .rules import (
    Assessment,
    HistoryEntry,
    PolicyRecord,
    needs_assessment,
    policy_state,
    reads_exclusion_count,
)
from .wording import (
    Terms,
    catalogue_wording,
    exclusions_counted,
    probes,
    select_terms,
)

logger = logging.getLogger(__name__)

# What stands for a key of the data in a location: ``extra="forbid"`` puts the
# key an unknown field was sent under in the error's location, and the key is
# the caller's. The same character as ``triaging.DATA_KEY``.
DATA_KEY = "*"
# The line of a policy whose wording the table of exclusion counts does not know.
NO_COUNT = "the table of exclusion clauses has no count for the wording"

# The worker IDs of the registry's ``claims-triage`` entry. The registry check
# and a test keep them the same.
INTAKE = "intake"
TERMS = "terms"
ASSESSOR = "assessor"
APPROVALS = "approvals"


class ClaimState(TypedDict):
    claim: dict[str, Any]
    # The Claims API's screen of the description as posted (S067), beside the
    # claim: absent means false. ``posted_flag_of`` reads it.
    posted_text_addresses_the_model: NotRequired[bool]
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


def invalid_fields(exc: ValidationError) -> tuple[tuple[str, str], ...]:
    """What failed to validate, as ``(dotted location, error type)`` pairs and
    nothing else: never the message, the input or the context, which quote the
    claim. The same as the Claims API's own (``triaging.invalid_fields``, which
    a test compares this with); it is not imported, as the API module pulls the
    web framework and the database driver into the graph. A key of the data is
    replaced (``DATA_KEY``): it is the caller's."""
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    return tuple((_dotted(error), error["type"]) for error in errors)


def _dotted(error: ErrorDetails) -> str:
    parts = [str(part) for part in error["loc"]]
    if error["type"] == "extra_forbidden" and parts:
        parts[-1] = DATA_KEY
    return ".".join(parts)


def claim_of(state: ClaimState) -> ClaimFacts:
    """The run's claim as facts, in every node that reads it. A claim that is not
    valid fails the run with a fixed code and a log line of the fields and kinds
    of error: pydantic's own error quotes the value that did not fit, and the
    run was started with it."""
    try:
        return ClaimFacts.model_validate(state["claim"])
    except ValidationError as exc:
        logger.error(
            "the claim of the run is not valid: %s %s",
            type(exc).__name__,
            invalid_fields(exc),
        )
        raise GraphFailure("claim-not-valid") from None


def posted_flag_of(state: ClaimState) -> bool:
    """The Claims API's screen of the description as posted (S067): absent is
    false, and anything that is not a boolean fails the run with a fixed code,
    never read as a truth value: the input is the caller's. A flag can only make
    a run more careful, so a caller that sends true for clean text sends the
    claim to an adjuster, which is what the screen's own hit does."""
    flag = state.get(POSTED_TEXT_FLAG, False)
    if not isinstance(flag, bool):
        raise GraphFailure("posted-flag-not-valid")
    return flag


def _unknown_wording(policy: PolicyRecord) -> GraphFailure:
    """The failure of a policy whose wording the table of exclusion counts does
    not know, after one log line that says so."""
    if catalogue_wording(policy.product, policy.wording_version):
        # An exception to the graph's rule that no tool result is repeated: the
        # product and the version came from the policy tool, but each is a closed
        # identifier once checked (a product of the catalogue, a version of the
        # form "2026-01"), so neither can carry a sentence or a value of the
        # claim. Anything else is said in fixed words.
        logger.error("%s %s %s", NO_COUNT, policy.product, policy.wording_version)
    else:
        logger.error("%s of a product or version outside the catalogue", NO_COUNT)
    return GraphFailure("wording-version-unknown")


def terms_of(claim: ClaimFacts, policy: PolicyRecord, state: ClaimState) -> Terms:
    """The terms of the claim's peril. Where the rules would read "are the
    exclusions complete" and the table has no count for the policy's wording, the
    run fails (S067): a referral as ``unverified`` would hide the cause. Both the
    assessor and the supervisor call this, so the run fails before the model is
    asked. ``select_terms`` stays pure: it is called for every claim, and a claim
    on a lapsed or expired policy, or on a peril the product does not cover, never
    reads the count (``reads_exclusion_count``)."""
    terms = select_terms(
        claim.peril,
        state["chunks"],
        product=policy.product,
        wording_version=policy.wording_version,
    )
    if reads_exclusion_count(claim, policy, terms) and not exclusions_counted(
        policy.product, policy.wording_version
    ):
        raise _unknown_wording(policy)
    return terms


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


def label(worker: str) -> dict[str, Any]:
    """The metadata of a node of ``worker``: the runtime puts it on the node's
    span. The supervisor labels the worker nodes it adds with the same form."""
    return {WORKER_KEY: worker}


def _policy_found(state: ClaimState) -> str:
    return END if state["policy"] is None else "load_history"


def build_intake(intake: ToolClient) -> CompiledStateGraph:
    """The policy, then the claim history of a claim that has a policy. ``intake``
    is the view of the ``intake`` worker."""

    def lookup_policy(state: ClaimState) -> dict[str, Any]:
        claim = claim_of(state)
        posted_flag_of(state)  # the run's input is checked once, before any call
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
        claim = claim_of(state)
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
    graph.add_node("lookup_policy", lookup_policy, metadata=label(INTAKE))
    graph.add_node("load_history", load_history, metadata=label(INTAKE))
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
        claim = claim_of(state)
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
    graph.add_node("retrieve_terms", retrieve_terms, metadata=label(TERMS))
    graph.add_edge(START, "retrieve_terms")
    graph.add_edge("retrieve_terms", END)
    return graph.compile()


def build_assessor(model: ModelClient) -> CompiledStateGraph:
    """The model's one fact, when the rules need it. It gets the model client and
    no tool client: the worker holds no tool."""

    def assess_exclusions(state: ClaimState) -> dict[str, Any]:
        claim = claim_of(state)
        # Read here and nowhere else; only a claim that is asked about uses it.
        posted = posted_flag_of(state)
        policy = policy_of(state)
        terms = terms_of(claim, policy, state)
        if not needs_assessment(claim, policy, terms):
            return {"assessed": assessed_to_state(NOT_NEEDED)}
        assessed = assess(
            model,
            claim,
            policy.product,
            policy.wording_version,
            terms.candidates,
            posted_text_addresses_the_model=posted,
        )
        return {"assessed": assessed_to_state(assessed)}

    graph = StateGraph(ClaimState)
    graph.add_node("assess", assess_exclusions, metadata=label(ASSESSOR))
    graph.add_edge(START, "assess")
    graph.add_edge("assess", END)
    return graph.compile()


def build_request_approval(approvals: ToolClient) -> CompiledStateGraph:
    """The approval request of a claim the rules route to an adjuster.
    ``approvals`` is the view of the ``approvals`` worker."""

    def request_approval(state: ClaimState) -> dict[str, Any]:
        claim = claim_of(state)
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
    graph.add_node("request_approval", request_approval, metadata=label(APPROVALS))
    graph.add_edge(START, "request_approval")
    graph.add_edge("request_approval", END)
    return graph.compile()


def build_outcome(approvals: ToolClient) -> CompiledStateGraph:
    """What follows the pause: the outcome the Claims API recorded, then the
    note of a fixed text. Two nodes, so that a note that failed is written again
    without reading the outcome again. ``approvals`` is the view of the
    ``approvals`` worker."""

    def read_outcome(state: ClaimState) -> dict[str, Any]:
        claim = claim_of(state)
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
        claim = claim_of(state)
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
    graph.add_node("read_outcome", read_outcome, metadata=label(APPROVALS))
    graph.add_node("write_note", write_note, metadata=label(APPROVALS))
    graph.add_edge(START, "read_outcome")
    graph.add_edge("read_outcome", "write_note")
    graph.add_edge("write_note", END)
    return graph.compile()
