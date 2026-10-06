"""The claims triage graph (S014, S015, S031): a supervisor and four workers.

The supervisor is this graph. Fixed code and the rules route: it holds no tool
client and no model client, only the compiled workers of ``workers.py``, and no
model chooses a worker, a tool or a route. Its nodes, in order:

- ``intake``: the policy and the claim history (a worker's subgraph);
- ``terms``: the wording clauses of the peril (a worker's subgraph);
- ``assessor``: the model's one fact, when the rules need it (a worker's
  subgraph, the only one that gets the model client and the only one that gets
  no tool);
- ``propose``: ``rules.decide``, the supervisor's own code;
- ``request_approval``: the approval request (a worker's subgraph);
- ``await_decision``: the pause, and after it the outcome worker, run by this
  node itself.

A claim with no policy goes from ``intake`` straight to ``propose``, and only a
claim the rules route to an adjuster goes on to ``request_approval``. ``build``
hands each worker's builder what that worker needs: the view of its own worker
(``tools.for_worker(...)``) for the three that have tools, the model client for
the assessor, and neither client to the supervisor's own nodes. The model never
chooses a tool or an argument: the workers' code calls the tools in a fixed
order, each through the view of the worker the registry gives the tool, which
the runtime and the tool servers enforce (T-21). The state holds plain data only
(the runtime runs LangGraph in strict msgpack mode), and each node validates
what it reads back into the typed models. Every graph of the run, this one and
each worker's, counts its own steps for each leg against the runtime's limit
(``test_runtime_subgraphs.py``): the supervisor has at most six steps in a leg.

The graph writes in one place: a claim the rules route to an adjuster. After
``propose``, ``request_approval`` records an approval request through the claims
tool server, and ``await_decision`` pauses the run (``interrupt``). The decision
is read from the record the Claims API committed before it resumed the run: the
resume value carries nothing and is ignored, so nothing that can call the
runtime can decide. The outcome worker reads the record through the
``approval_outcome`` tool and fits the answer strictly
(``approval-outcome-unfit``); with no decision recorded it fails
(``decision-not-recorded``) before a write, and the runtime leaves the run
paused. It then adds a note from a fixed table, so the note holds nothing the
caller wrote. The Claims API checks that the caller may decide and records it;
the graph only reads it (T-31). The reason sent with the request is the
proposal's reason code, never claim text. Both writes carry a step and send the
same payload when their node runs again, so the tool server's idempotency key
makes a rerun answer with the stored ID (T-23).

The outcome worker relies on a decision never changing once recorded: after a
failure in its note node, the next resume skips the read and writes the note
for the decision its first leg read. The record cannot change. ``claims.decisions``
holds one row for each run (``test_a_run_is_decided_once``), the Claims API can
only insert and read it (``test_claims_api_may_neither_change_nor_delete_a_decision``)
and the tool server's role can only read three columns of it
(``test_claims_mcp_may_not_insert_update_or_delete_a_decision``), all in
``tests/meridian/db``. A job that deletes or rewrites decisions (a retention
rule) would break this assumption and must change the worker first.

The pause and the work after it are one node on purpose. LangGraph consumes an
interrupt when its node finishes, so a failure in a later node would leave the
thread with no pending pause, and the runtime would end the run ``Failed`` on
the next resume instead of leaving it paused (``test_runtime_subgraphs.py``).
Inside one node, a resumed leg that fails leaves the pause pending. That node
runs again from its first line on every resume, so nothing but the ``interrupt``
comes before the worker. What the outcome worker does on a resume was
measured: it runs as a subgraph of the node under the checkpoint namespace of
its own, so after a failure in its last node the next resume does not read the
outcome again; it continues at the node that failed.
The pause is not any worker's step: the span of ``await_decision`` carries no
worker, and the worker's own nodes carry ``approvals``.

A platform that cannot answer fails the run, and the claim can be triaged again:
no tool or model error is caught here. Only two things become a proposal for a
person: no such policy, and an assessment that is unavailable: a model answer
that cannot be trusted, a description that holds special-category data or
addresses the model, or a request the provider's content filter refused (S047;
``assessment.py`` turns each into an assessment). A wording clause that
addresses the model is platform data, not the claim's: it fails the run
(``wording-addresses-the-model``). So does a policy whose wording the table of
exclusion counts does not know (``wording-version-unknown``, S067), where the
rules would read the count: a referral as ``unverified`` would hide the cause. A
claim that is not valid facts fails the run (``claim-not-valid``) after a log
line of its fields and kinds of error. No log line, exception message
or span attribute of the graph holds claim text, a tool result or model text:
a tool result that does not fit its model raises a ``GraphFailure`` whose code
says which answer, not what it held (the graph's own violations all do). One
exception, in ``workers.py``: the line for an unknown wording repeats the
policy's product and version when, and only when, each is a closed identifier
(a product of the catalogue, a version of the form ``2026-01``).
"""

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from meridian.runtime.failures import GraphFailure
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient

from . import workers
from .models import DraftedBy
from .proposal import Citation, TriageProposal
from .rules import Assessment, Facts, HistoryEntry, PolicyRecord, decide


def _after_intake(state: workers.ClaimState) -> str:
    """No policy goes straight to the rules; the other claims go on to the
    wording."""
    return "propose" if state["policy"] is None else "terms"


def _after_propose(state: workers.ClaimState) -> str:
    """Only a claim the rules route to an adjuster waits for one."""
    route = TriageProposal.model_validate(state["output"]).route
    return "request_approval" if route == "adjuster" else END


def _citations(policy: PolicyRecord | None, clauses: tuple[str, ...]) -> tuple:
    if policy is None:
        return ()
    return tuple(
        Citation(
            product=policy.product,
            wording_version=policy.wording_version,
            clause=clause,
        )
        for clause in clauses
    )


def propose(state: workers.ClaimState) -> dict[str, Any]:
    """The supervisor's own step: the rules decide the route from the facts the
    workers gathered. No tool, no model."""
    claim = workers.claim_of(state)
    policy = (
        PolicyRecord.model_validate(state["policy"])
        if state["policy"] is not None
        else None
    )
    # With no policy no node assessed anything. With one, ``assess`` always
    # ran: a state without its assessment is a bug, not a default.
    if policy is None:
        assessed = workers.assessed_to_state(workers.NOT_NEEDED)
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
            terms=(
                workers.terms_of(claim, policy, state) if policy is not None else None
            ),
            assessment=assessment,
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
        citations=_citations(policy, decision.citations),
        gaps=decision.gaps,
        assessment=assessment.status,
        unavailable_because=assessed["unavailable_because"],
        rationale=assessed["rationale"],
        drafted_by=DraftedBy.model_validate(drafted_by) if drafted_by else None,
    )
    return {"output": proposal.model_dump(mode="json")}


def build(model: ModelClient, tools: ToolClient) -> StateGraph:
    """The workload's graph factory, published as the ``claims-triage`` entry
    point. Returned uncompiled: the runtime compiles it with its checkpointer.
    The workers' subgraphs are compiled here."""
    approvals = tools.for_worker(workers.APPROVALS)
    intake = workers.build_intake(tools.for_worker(workers.INTAKE))
    terms = workers.build_terms(tools.for_worker(workers.TERMS))
    assessor = workers.build_assessor(model)
    request_approval = workers.build_request_approval(approvals)
    outcome = workers.build_outcome(approvals)

    def await_decision(
        state: workers.ClaimState, config: RunnableConfig
    ) -> dict[str, Any]:
        # Nothing before the pause: this node runs again from its start on resume.
        # The resume value is ignored: what the caller sent is not the decision,
        # the Claims API's record is.
        interrupt({"request_id": state["request_id"]})
        # The node's own config goes to the worker by argument, so the callbacks,
        # the checkpointer and the step limit reach it whatever the interpreter
        # does with context variables. Only the key the worker wrote comes back,
        # not the whole state the subgraph answers with: this node's update is
        # what the supervisor's state keeps.
        return {"decision": outcome.invoke(state, config)["decision"]}

    graph = StateGraph(workers.ClaimState)
    graph.add_node("intake", intake, metadata=workers.label(workers.INTAKE))
    graph.add_node("terms", terms, metadata=workers.label(workers.TERMS))
    graph.add_node("assessor", assessor, metadata=workers.label(workers.ASSESSOR))
    graph.add_node("propose", propose)
    graph.add_node(
        "request_approval",
        request_approval,
        metadata=workers.label(workers.APPROVALS),
    )
    graph.add_node("await_decision", await_decision)
    graph.add_edge(START, "intake")
    graph.add_conditional_edges(
        "intake", _after_intake, {"propose": "propose", "terms": "terms"}
    )
    graph.add_edge("terms", "assessor")
    graph.add_edge("assessor", "propose")
    graph.add_conditional_edges(
        "propose",
        _after_propose,
        {"request_approval": "request_approval", END: END},
    )
    graph.add_edge("request_approval", "await_decision")
    graph.add_edge("await_decision", END)
    return graph
