"""The Claims API app: ``POST /claims`` (S009) and the decision (S015).

A claim is stored, then triaged through the Agent Runtime, then its triage
proposal is stored. Every claim is in one state of the lifecycle
(``lifecycle.py``) and moves only along its listed transitions, each change
audited. A triage of a claim runs one at a time: the request that moves the
claim to ``triaging`` owns it. A failure after the claim is stored moves it to
``triage_failed`` and answers 502, 503 or 504 with the claim's ID (and the
run's, when there is one); the same claim posted again, unchanged, is triaged
again. A claim the rules refer to an adjuster waits in ``awaiting_adjuster``
with its run paused; ``POST /claims/{claim_id}/decision`` records the
adjuster's decision and then resumes that run, which reads the decision from
the record (the resume carries none). The API records the decision and the run
only reads it (T-31). It answers 200 only when the run completed. The triage
machinery is in ``triaging.py``.
"""

import logging
from collections.abc import Mapping
from typing import Annotated
from uuid import UUID

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Path
from fastapi.responses import JSONResponse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer

from meridian.platform.common.db import connect
from meridian.platform.common.http import (
    SMALL_BODY_LIMIT_BYTES,
    create_service_app,
    database_failure,
    error_responses,
)
from meridian.platform.common.logredaction import install_log_redaction
from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.workloads.claims_triage.adjuster import (
    NO_SUCH_CLAIM_DETAIL,
    add_adjuster_pages,
)
from meridian.workloads.claims_triage.lifecycle import (
    ADJUSTER_APPROVED,
    ADJUSTER_REJECTED,
    ADJUSTER_REQUESTED_DOCUMENTS,
    SERVICE_NAME,
    LifecycleState,
    Transition,
    move_claim,
)
from meridian.workloads.claims_triage.models import (
    ClaimDecision,
    ClaimErrorBody,
    ClaimResponse,
    ClaimSubmission,
    Decision,
    DecisionFailure,
    DecisionResponse,
)
from meridian.workloads.claims_triage.settings import ClaimsSettings
from meridian.workloads.claims_triage.triaging import (
    HTTP_GATEWAY_TIMEOUT,
    RUNTIME_TIMEOUT_SECONDS,
    RuntimeCallError,
    answer,
    facts_for_run,
    resume_run,
    store_claim,
    triage_claim,
)

NOT_WAITING_DETAIL = "the claim does not wait for an adjuster"
DECIDED_OTHERWISE_DETAIL = "the claim was decided otherwise"
RESUME_FAILED_DETAIL = "the decision is recorded; the run did not complete"
BEING_APPLIED_DETAIL = "the decision is being applied by another request"

DECISION_TRANSITIONS: Mapping[Decision, Transition] = {
    "approve": ADJUSTER_APPROVED,
    "reject": ADJUSTER_REJECTED,
    "request_documents": ADJUSTER_REQUESTED_DOCUMENTS,
}

logger = logging.getLogger(__name__)


def _record_decision(
    dsn: str, tenant: str, claim_id: str, decision: Decision
) -> tuple[LifecycleState, UUID]:
    """Record the adjuster's decision and move the claim; the claim's state and
    the run to resume. A decision made again is recorded once."""
    transition = DECISION_TRANSITIONS[decision]
    with connect(dsn, SERVICE_NAME) as conn:
        row = conn.execute(
            "SELECT state, run_id FROM claims.claims "
            "WHERE claim_id = %s AND tenant = %s FOR NO KEY UPDATE",
            (claim_id, tenant),
        ).fetchone()
        if row is None:
            raise HTTPException(404, NO_SUCH_CLAIM_DETAIL)
        state, run_id = row
        if run_id is None:
            raise HTTPException(409, NOT_WAITING_DETAIL)
        if state == "awaiting_adjuster":
            conn.execute(
                "INSERT INTO claims.decisions (claim_id, run_id, decision) "
                "VALUES (%s, %s, %s)",
                (claim_id, run_id, decision),
            )
            moved = move_claim(
                conn, transition, claim_id=claim_id, tenant=tenant, run_id=run_id
            )
            if moved is None:
                raise HTTPException(409, NOT_WAITING_DETAIL)
            return transition.target, run_id
        # Not waiting: only a claim this run's decision already moved may go on
        # to the resume again (the answer to the first one was lost).
        decided = conn.execute(
            "SELECT decision FROM claims.decisions WHERE claim_id = %s AND run_id = %s",
            (claim_id, run_id),
        ).fetchone()
        if decided is None:
            raise HTTPException(409, NOT_WAITING_DETAIL)
        if decided[0] != decision:
            raise HTTPException(409, DECIDED_OTHERWISE_DETAIL)
        return state, run_id


def _decide(
    dsn: str,
    tenant: str,
    http: httpx.Client,
    tracer: Tracer,
    claim_id: str,
    decision: Decision,
) -> DecisionResponse | DecisionFailure:
    """The one path of a decision, for the JSON route and the adjuster's page:
    record it, move the claim and audit it in one transaction, then resume the
    run. A refusal (404, 409) is raised as ``HTTPException``; every other
    failure is a ``DecisionFailure`` and the decision, once recorded, stays."""
    with start_span(tracer, "claims.decide") as span:
        set_span_attributes(
            span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
        )
        try:
            state, run_id = _record_decision(dsn, tenant, claim_id, decision)
        except psycopg.Error as exc:
            mark_error(span, exc)
            return DecisionFailure(*database_failure(exc))
        set_span_attributes(span, {"meridian.run_id": str(run_id)})
        try:
            run = resume_run(http, tenant, claim_id, run_id)
        except RuntimeCallError as exc:
            logger.error(
                "resume of run %s for claim %s failed: %s (runtime status %s)",
                run_id,
                claim_id,
                type(exc).__name__,
                exc.status_code,
            )
            mark_error(span, exc)
            return DecisionFailure(
                HTTP_GATEWAY_TIMEOUT if exc.timed_out else 502,
                RESUME_FAILED_DETAIL,
                run_id,
            )
        if run.status == "Running":
            # Another request is applying this decision right now.
            logger.info(
                "resume of run %s for claim %s: another request is applying it",
                run_id,
                claim_id,
            )
            return DecisionFailure(409, BEING_APPLIED_DETAIL, run_id)
        if run.status != "Completed":
            # The decision stays recorded; a post of it again resumes again.
            logger.error(
                "resume of run %s for claim %s did not complete: run status %s",
                run_id,
                claim_id,
                run.status,
            )
            return DecisionFailure(502, RESUME_FAILED_DETAIL, run_id)
        return DecisionResponse(
            claim_id=claim_id, state=state, run_id=run_id, run_status=run.status
        )


def create_app(
    settings: ClaimsSettings,
    *,
    tracer_provider: TracerProvider | None = None,
    http_client: httpx.Client | None = None,
) -> FastAPI:
    # trust_env=False: a proxy variable must not reroute claimant data.
    http = http_client or httpx.Client(
        base_url=settings.runtime_url,
        timeout=RUNTIME_TIMEOUT_SECONDS,
        trust_env=False,
    )
    dsn, tenant = settings.database_url, settings.tenant
    service = create_service_app(
        title="Meridian Claims API",
        description=(
            "Takes a claim, has it triaged, keeps the proposal and records the "
            "adjuster's decision."
        ),
        service_name=SERVICE_NAME,
        tracer_name="meridian.claims",
        max_body_bytes=SMALL_BODY_LIMIT_BYTES,
        tracer_provider=tracer_provider,
        close=http.close if http_client is None else None,
    )
    app, tracer = service.app, service.tracer

    @app.post(
        "/claims",
        status_code=201,
        response_model=ClaimResponse,
        tags=["claims"],
        summary="Store a claim and have the triage agent propose a route.",
        responses=error_responses(409, 413)
        | error_responses(500, 502, 503, 504, model=ClaimErrorBody),
    )
    def submit_claim(submission: ClaimSubmission) -> ClaimResponse | JSONResponse:
        claim = submission.model_dump(mode="json")
        facts = facts_for_run(submission)
        claim_id = submission.claim_id
        with start_span(tracer, "claims.submit") as span:
            set_span_attributes(
                span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
            )
            try:
                store_claim(dsn, tenant, claim)
            except psycopg.Error as exc:
                mark_error(span, exc)
                return answer(*database_failure(exc), claim_id)
            return triage_claim(dsn, tenant, http, span, claim_id, facts)

    @app.post(
        "/claims/{claim_id}/decision",
        response_model=DecisionResponse,
        tags=["claims"],
        summary="Record an adjuster's decision and resume the claim's paused run.",
        responses=error_responses(404, 409, 413)
        | error_responses(500, 502, 503, 504, model=ClaimErrorBody),
    )
    def decide_claim(
        claim_id: Annotated[str, Path(pattern=r"^CLM-[0-9]{4}$")], body: ClaimDecision
    ) -> DecisionResponse | JSONResponse:
        result = _decide(dsn, tenant, http, tracer, claim_id, body.decision)
        if isinstance(result, DecisionFailure):
            return answer(result.status, result.detail, claim_id, result.run_id)
        return result

    add_adjuster_pages(
        app,
        dsn=dsn,
        tenant=tenant,
        tracer=tracer,
        decide=lambda claim_id, decision: _decide(
            dsn, tenant, http, tracer, claim_id, decision
        ),
    )
    return app


def create_app_from_env() -> FastAPI:
    """The factory S041 runs under ``uvicorn --factory``."""
    install_log_redaction()
    return create_app(ClaimsSettings.from_env())
