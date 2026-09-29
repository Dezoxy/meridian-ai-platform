"""The claim flow in LangGraph."""

import uuid
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from claimflow import rules
from claimflow.rules import AdjusterDecision, RunResult

DB_FILE = "checkpoints.sqlite"


class ClaimState(TypedDict, total=False):
    claim_id: str
    claim: dict[str, Any]
    policy: dict[str, Any]
    policy_valid: bool
    proposal: rules.Proposal
    needs_approval: bool
    outcome: rules.Proposal
    adjuster_id: str


def validate_step(state: ClaimState) -> dict[str, Any]:
    validated = rules.validate(state["claim_id"])
    return {
        "claim": validated.claim,
        "policy": validated.policy,
        "policy_valid": validated.policy_valid,
    }


def assess_step(state: ClaimState) -> dict[str, Any]:
    assessment = rules.assess(
        state["claim_id"], state["claim"]["claimed_amount"], state["policy_valid"]
    )
    return {
        "proposal": assessment.proposal,
        "needs_approval": assessment.needs_approval,
    }


def approval_step(state: ClaimState) -> dict[str, Any]:
    decision = interrupt(
        {
            "claim_id": state["claim_id"],
            "proposal": state["proposal"],
            "claimed_amount": state["claim"]["claimed_amount"],
            "policy_valid": state["policy_valid"],
        },
        response_schema=AdjusterDecision,
    )
    return {"outcome": decision.decision, "adjuster_id": decision.adjuster_id}


def auto_complete_step(state: ClaimState) -> dict[str, Any]:
    return {"outcome": state["proposal"]}


def route_after_assess(state: ClaimState) -> Literal["approval", "auto_complete"]:
    return "approval" if state["needs_approval"] else "auto_complete"


def build_graph(saver: SqliteSaver) -> CompiledStateGraph:
    builder = StateGraph(ClaimState)
    builder.add_node("validate", validate_step)
    builder.add_node("assess", assess_step)
    builder.add_node("approval", approval_step)
    builder.add_node("auto_complete", auto_complete_step)
    builder.add_edge(START, "validate")
    builder.add_edge("validate", "assess")
    builder.add_conditional_edges("assess", route_after_assess)
    builder.add_edge("approval", END)
    builder.add_edge("auto_complete", END)
    return builder.compile(checkpointer=saver)


def start(claim_id: str, store_dir: Path) -> RunResult:
    run_ref = str(uuid.uuid4())
    return _invoke({"claim_id": claim_id}, run_ref, store_dir)


def resume(run_ref: str, decision: dict[str, Any], store_dir: Path) -> RunResult:
    return _invoke(Command(resume=decision), run_ref, store_dir)


def _invoke(payload: Any, run_ref: str, store_dir: Path) -> RunResult:
    store_dir.mkdir(parents=True, exist_ok=True)
    config = {"configurable": {"thread_id": run_ref}}
    with SqliteSaver.from_conn_string(str(store_dir / DB_FILE)) as saver:
        graph = build_graph(saver)
        graph.invoke(payload, config)
        snapshot = graph.get_state(config)
    values = snapshot.values
    if snapshot.next:
        return RunResult(
            "awaiting_approval",
            run_ref,
            values["claim_id"],
            values["proposal"],
            None,
            None,
        )
    return RunResult(
        "completed",
        run_ref,
        values["claim_id"],
        values["proposal"],
        values["outcome"],
        values.get("adjuster_id"),
    )
