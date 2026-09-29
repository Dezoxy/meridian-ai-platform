"""The claim flow in Microsoft Agent Framework (agent-framework-core)."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

from agent_framework import (
    Case,
    Default,
    Executor,
    FileCheckpointStorage,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowRunResult,
    executor,
    handler,
    response_handler,
)

from claimflow import rules
from claimflow.rules import (
    AdjusterDecision,
    AssessedClaim,
    RunOutcome,
    RunResult,
    ValidatedClaim,
)

WORKFLOW_NAME = "claimflow"

# FileCheckpointStorage unpickles only an allowlist. Every application type that
# lands in a message, a request or a response has to be named here.
ALLOWED_CHECKPOINT_TYPES = [
    "claimflow.rules:ValidatedClaim",
    "claimflow.rules:Assessment",
    "claimflow.rules:AssessedClaim",
    "claimflow.maf_flow:ApprovalRequest",
    "claimflow.rules:AdjusterDecision",
]


@dataclass
class ApprovalRequest:
    """What the adjuster is shown."""

    claim_id: str
    proposal: rules.Proposal
    claimed_amount: int
    policy_valid: bool


@executor(id="validate")
async def validate_step(claim_id: str, ctx: WorkflowContext[ValidatedClaim]) -> None:
    await ctx.send_message(rules.validate(claim_id))


@executor(id="assess")
async def assess_step(
    validated: ValidatedClaim, ctx: WorkflowContext[AssessedClaim]
) -> None:
    assessment = rules.assess_validated(validated)
    await ctx.send_message(AssessedClaim(validated, assessment))


@executor(id="auto_complete")
async def auto_complete_step(
    assessed: AssessedClaim, ctx: WorkflowContext[Never, RunOutcome]
) -> None:
    await ctx.yield_output(rules.auto_outcome(assessed))


class ApprovalStep(Executor):
    """Pauses for an adjuster, then records the decision.

    Pausing needs a class: `@response_handler` registers on the class, and the
    request and its response have to be handled by the same executor.
    """

    def __init__(self) -> None:
        super().__init__(id="approval")

    @handler
    async def ask(self, assessed: AssessedClaim, ctx: WorkflowContext) -> None:
        claim = assessed.validated.claim
        request = ApprovalRequest(
            claim_id=assessed.assessment.claim_id,
            proposal=assessed.assessment.proposal,
            claimed_amount=claim["claimed_amount"],
            policy_valid=assessed.validated.policy_valid,
        )
        await ctx.request_info(request, AdjusterDecision)

    @response_handler
    async def record(
        self,
        original_request: ApprovalRequest,
        response: AdjusterDecision,
        ctx: WorkflowContext[Never, RunOutcome],
    ) -> None:
        outcome = RunOutcome(
            claim_id=original_request.claim_id,
            proposal=original_request.proposal,
            outcome=response.decision,
            adjuster_id=response.adjuster_id,
        )
        await ctx.yield_output(outcome)


def _storage(store_dir: Path) -> FileCheckpointStorage:
    return FileCheckpointStorage(
        store_dir, allowed_checkpoint_types=ALLOWED_CHECKPOINT_TYPES
    )


def build_workflow(store_dir: Path) -> Workflow:
    approval = ApprovalStep()
    return (
        WorkflowBuilder(
            name=WORKFLOW_NAME,
            start_executor=validate_step,
            checkpoint_storage=_storage(store_dir),
        )
        .add_edge(validate_step, assess_step)
        .add_switch_case_edge_group(
            assess_step,
            [
                Case(
                    condition=lambda assessed: assessed.assessment.needs_approval,
                    target=approval,
                ),
                Default(target=auto_complete_step),
            ],
        )
        .build()
    )


def start(claim_id: str, store_dir: Path) -> RunResult:
    return asyncio.run(_start(claim_id, store_dir))


def resume(run_ref: str, decision: dict[str, Any], store_dir: Path) -> RunResult:
    return asyncio.run(_resume(run_ref, decision, store_dir))


async def _start(claim_id: str, store_dir: Path) -> RunResult:
    workflow = build_workflow(store_dir)
    result = await workflow.run(claim_id)
    return await _to_run_result(workflow, result)


async def _resume(run_ref: str, decision: dict[str, Any], store_dir: Path) -> RunResult:
    workflow = build_workflow(store_dir)
    checkpoint = await _storage(store_dir).load(run_ref)
    request_ids = list(checkpoint.pending_request_info_events)
    result = await workflow.run(
        responses={request_id: decision for request_id in request_ids},
        checkpoint_id=run_ref,
    )
    return await _to_run_result(workflow, result)


async def _to_run_result(workflow: Workflow, result: WorkflowRunResult) -> RunResult:
    requests = result.get_request_info_events()
    if requests:
        request: ApprovalRequest = requests[0].data
        run_ref = await workflow.resolve_pause_checkpoint_id(
            [event.request_id for event in requests]
        )
        if run_ref is None:
            raise RuntimeError("the pause was not checkpointed; the run cannot resume")
        return RunResult(
            "awaiting_approval", run_ref, request.claim_id, request.proposal, None, None
        )
    outputs = result.get_outputs()
    if not outputs:
        raise RuntimeError(f"the workflow ended in {result.get_final_state()}")
    outcome: RunOutcome = outputs[0]
    return RunResult(
        "completed",
        workflow.get_last_checkpoint_id() or "",
        outcome.claim_id,
        outcome.proposal,
        outcome.outcome,
        outcome.adjuster_id,
    )
