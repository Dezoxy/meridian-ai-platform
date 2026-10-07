"""The Claims API app: ``POST /claims`` (S009), the decision (S015) and the
routes that move a claim a person's way (S048).

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
only reads it (T-31). It answers 200 only when the run completed. A decision on
a claim with no paused run (its triage failed, or documents arrived at the
triage cap) refers the claim first when it must, records the decision with no
run and resumes nothing. The triage machinery is in ``triaging.py``; the routes
below are in ``moves.py``.

- ``POST /claims/{claim_id}/triage`` triages a claim again: a claim waiting for
  an adjuster is sent back to triage and its paused run ended, a claim whose
  triage failed is tried again; at most five triages per claim.
- ``POST /claims/{claim_id}/withdrawal`` withdraws a claim waiting for an
  adjuster or for documents, ending its paused run, and answers the same when
  posted again.
- ``POST /claims/{claim_id}/documents`` takes the names of the documents that
  arrived for a claim waiting for them, and triages the claim with them (or
  refers it to an adjuster, with no run, at the triage cap).

The first two take no input, and both require a JSON body of ``{}``: a route
with no body never checks the content type, and a cross-site form could reach it
(T-01).
"""

import logging
import ssl
from collections.abc import Callable, Mapping
from datetime import date
from typing import Annotated
from uuid import UUID

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Path
from fastapi.responses import JSONResponse
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer

from meridian.platform.common.db import connect
from meridian.platform.common.http import (
    SMALL_BODY_LIMIT_BYTES,
    create_service_app,
    error_responses,
)
from meridian.platform.common.logformat import configure_logging
from meridian.platform.common.logredaction import install_log_redaction
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.platform.common.tls import verify_of
from meridian.workloads.claims_triage.adjuster import (
    NO_SUCH_CLAIM_DETAIL,
    ClaimId,
    add_adjuster_pages,
)
from meridian.workloads.claims_triage.briefs import add_brief_routes
from meridian.workloads.claims_triage.claimant import (
    add_claimant_pages,
    claimant_too_large,
)
from meridian.workloads.claims_triage.claimant_uploads import (
    TWIN_PATH,
    add_claimant_upload_twin,
)
from meridian.workloads.claims_triage.lifecycle import (
    ADJUSTER_APPROVED,
    ADJUSTER_REJECTED,
    ADJUSTER_REQUESTED_DOCUMENTS,
    SERVICE_NAME,
    TRIAGE_REFERRED,
    LifecycleState,
    Transition,
    move_claim,
)
from meridian.workloads.claims_triage.meters import ClaimsMeters
from meridian.workloads.claims_triage.models import (
    ClaimDecision,
    ClaimErrorBody,
    ClaimMoveRequest,
    ClaimMoveResponse,
    ClaimResponse,
    ClaimSubmission,
    Decision,
    DecisionFailure,
    DecisionResponse,
    DocumentsArrival,
)
from meridian.workloads.claims_triage.moves import (
    RECORD_OUTCOME_SQL,
    add_documents,
    refuse_stale_page,
    triage_again,
    withdraw,
)
from meridian.workloads.claims_triage.settings import ClaimsSettings
from meridian.workloads.claims_triage.triaging import (
    HTTP_GATEWAY_TIMEOUT,
    RuntimeCallError,
    answer,
    claim_database_failure,
    resume_run,
    runtime_timeout,
    store_claim,
    triage_claim,
)
from meridian.workloads.claims_triage.uploads import (
    UPLOAD_BODY_LIMIT_BYTES,
    UPLOAD_PATH,
    StoreLimits,
    add_upload_routes,
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

# The word of the latest decision recorded for the claim with no run (a claim
# referred with no paused run, then decided).
LATEST_RUNLESS_DECISION_SQL = (
    "SELECT decision FROM claims.decisions "
    "WHERE claim_id = %s AND run_id IS NULL "
    "ORDER BY decided_at DESC, decision_id DESC LIMIT 1"
)

logger = logging.getLogger(__name__)


def _reply[Success](
    result: Success | DecisionFailure, claim_id: str
) -> Success | JSONResponse:
    """What a JSON route answers for a function's result: the result itself, or
    a failure as the shared error answer."""
    if isinstance(result, DecisionFailure):
        return answer(result.status, result.detail, claim_id, result.run_id)
    return result


def _record_decision(
    dsn: str,
    tenant: str,
    claim_id: str,
    decision: Decision,
    page_run: str | None = None,
) -> tuple[LifecycleState, UUID | None]:
    """Record the adjuster's decision and move the claim; the claim's state and
    the run to resume, ``None`` for a claim with no paused run. A decision made
    again is recorded once. ``page_run`` is the run the adjuster's page showed
    (``None`` for the JSON route, which holds no page): a claim that has another
    run now is a 409 with nothing written (T-33)."""
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
        refuse_stale_page(page_run, run_id)
        # A claim with no paused run: its triage failed (it is referred first, so
        # the decision follows an edge of the lifecycle) or documents arrived at
        # the triage cap. The decision is recorded with no run; nothing resumes.
        if state == "triage_failed":
            if (
                move_claim(conn, TRIAGE_REFERRED, claim_id=claim_id, tenant=tenant)
                is None
            ):
                raise HTTPException(409, NOT_WAITING_DETAIL)
            conn.execute(RECORD_OUTCOME_SQL, (claim_id, None, decision))
            if move_claim(conn, transition, claim_id=claim_id, tenant=tenant) is None:
                raise HTTPException(409, NOT_WAITING_DETAIL)
            return transition.target, None
        if state == "awaiting_adjuster" and run_id is None:
            conn.execute(RECORD_OUTCOME_SQL, (claim_id, None, decision))
            if move_claim(conn, transition, claim_id=claim_id, tenant=tenant) is None:
                raise HTTPException(409, NOT_WAITING_DETAIL)
            return transition.target, None
        if run_id is None:
            # Decided with no run, and the answer to the first post lost: only a
            # claim still in this decision's own move is answered again.
            if state != transition.target:
                raise HTTPException(409, NOT_WAITING_DETAIL)
            runless = conn.execute(LATEST_RUNLESS_DECISION_SQL, (claim_id,)).fetchone()
            if runless is None:
                raise HTTPException(409, NOT_WAITING_DETAIL)
            if runless[0] != decision:
                raise HTTPException(409, DECIDED_OTHERWISE_DETAIL)
            return state, None
        if state == "awaiting_adjuster":
            conn.execute(RECORD_OUTCOME_SQL, (claim_id, run_id, decision))
            moved = move_claim(
                conn, transition, claim_id=claim_id, tenant=tenant, run_id=run_id
            )
            if moved is None:
                raise HTTPException(409, NOT_WAITING_DETAIL)
            return transition.target, run_id
        # Not waiting: only a claim this run's decision already moved may go on
        # to the resume again (the answer to the first one was lost). One the
        # claim has left since (withdrawn, sent back to triage) is not this
        # decision's move any more: nothing is resumed for it.
        decided = conn.execute(
            "SELECT decision FROM claims.decisions WHERE claim_id = %s AND run_id = %s",
            (claim_id, run_id),
        ).fetchone()
        if decided is None:
            raise HTTPException(409, NOT_WAITING_DETAIL)
        if decided[0] != decision:
            raise HTTPException(409, DECIDED_OTHERWISE_DETAIL)
        if state != transition.target:
            raise HTTPException(409, NOT_WAITING_DETAIL)
        return state, run_id


def _decide(
    dsn: str,
    tenant: str,
    http: httpx.Client,
    tracer: Tracer,
    claim_id: str,
    decision: Decision,
    page_run: str | None = None,
) -> DecisionResponse | DecisionFailure:
    """The one path of a decision, for the JSON route and the adjuster's page:
    record it, move the claim and audit it in one transaction, then resume the
    run. A refusal (404, 409) is raised as ``HTTPException``; every other
    failure is a ``DecisionFailure`` and the decision, once recorded, stays.
    The page passes the run it showed as ``page_run``; the JSON route none."""
    with start_span(tracer, "claims.decide") as span:
        set_span_attributes(
            span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
        )
        try:
            state, run_id = _record_decision(dsn, tenant, claim_id, decision, page_run)
        except psycopg.Error as exc:
            mark_error(span, exc)
            return DecisionFailure(*claim_database_failure(exc, claim_id))
        if run_id is None:
            # Recorded with no run: there is nothing to resume.
            return DecisionResponse(claim_id=claim_id, state=state)
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


def make_runtime_client(
    settings: ClaimsSettings, verify: ssl.SSLContext | bool
) -> httpx.Client:
    """The client of the Agent Runtime. ``verify`` is ``verify_of`` the settings'
    ``client_tls``: the context that presents the Claims API's certificate and
    trusts the runtime's CA, or the default verification when there is none."""
    # trust_env=False: a proxy variable must not reroute claimant data.
    return httpx.Client(
        base_url=settings.runtime_url,
        timeout=runtime_timeout(),
        trust_env=False,
        verify=verify,
    )


def create_app(
    settings: ClaimsSettings,
    *,
    tracer_provider: TracerProvider | None = None,
    meter_provider: MeterProvider | None = None,
    http_client: httpx.Client | None = None,
    today: Callable[[], date] | None = None,
) -> FastAPI:
    """Build the app. A ``meter_provider`` is its caller's to shut down; without
    one the app builds its own, which it shuts down with the app."""
    http = http_client or make_runtime_client(settings, verify_of(settings.client_tls))
    dsn, tenant = settings.database_url, settings.tenant
    owns_meter_provider = meter_provider is None
    app_meter_provider = (
        make_meter_provider(SERVICE_NAME) if meter_provider is None else meter_provider
    )
    meters = ClaimsMeters(app_meter_provider, tenant)

    def close() -> None:
        # The runtime client and the meter provider are closed only when the app
        # made them: an injected one is its owner's.
        try:
            if http_client is None:
                http.close()
        finally:
            if owns_meter_provider:
                try:
                    app_meter_provider.shutdown()
                except Exception as exc:
                    # The SDK raises a bare Exception when a reader fails, with
                    # the exporter's text: the class only. The lifespan goes on
                    # to flush the tracer provider, whose spans would be lost.
                    logger.warning(
                        "the meter provider did not shut down cleanly: %s",
                        type(exc).__name__,
                    )

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
        close=close,
        too_large=claimant_too_large,
        # The two routes that take a file, the JSON one and its HTML twin, have a
        # limit of their own, and only when they exist: with the switch off their
        # paths keep the 64 KiB of the rest.
        route_body_limits=(
            {
                ("POST", UPLOAD_PATH): UPLOAD_BODY_LIMIT_BYTES,
                ("POST", TWIN_PATH): UPLOAD_BODY_LIMIT_BYTES,
            }
            if settings.uploads_enabled
            else None
        ),
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
        claim_id = submission.claim_id
        with start_span(tracer, "claims.submit") as span:
            set_span_attributes(
                span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
            )
            try:
                store_claim(dsn, tenant, claim)
            except psycopg.Error as exc:
                mark_error(span, exc)
                return answer(*claim_database_failure(exc, claim_id), claim_id)
            return triage_claim(dsn, tenant, http, span, claim_id, submission, meters)

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
        return _reply(
            _decide(dsn, tenant, http, tracer, claim_id, body.decision), claim_id
        )

    @app.post(
        "/claims/{claim_id}/triage",
        response_model=ClaimMoveResponse,
        tags=["claims"],
        summary="Triage a claim again: send it back from an adjuster or retry it.",
        responses=error_responses(404, 409, 413)
        | error_responses(500, 502, 503, 504, model=ClaimErrorBody),
    )
    def triage_claim_again(
        claim_id: ClaimId, body: ClaimMoveRequest
    ) -> ClaimMoveResponse | JSONResponse:
        return _reply(
            triage_again(dsn, tenant, http, tracer, claim_id, meters=meters), claim_id
        )

    @app.post(
        "/claims/{claim_id}/withdrawal",
        response_model=ClaimMoveResponse,
        tags=["claims"],
        summary="Withdraw a claim that waits for an adjuster or for documents.",
        responses=error_responses(404, 409, 413)
        | error_responses(500, 503, model=ClaimErrorBody),
    )
    def withdraw_claim(
        claim_id: ClaimId, body: ClaimMoveRequest
    ) -> ClaimMoveResponse | JSONResponse:
        return _reply(withdraw(dsn, tenant, http, tracer, claim_id), claim_id)

    @app.post(
        "/claims/{claim_id}/documents",
        response_model=ClaimMoveResponse,
        tags=["claims"],
        summary="Take the names of the documents that arrived and triage the claim.",
        responses=error_responses(404, 409, 413)
        | error_responses(500, 502, 503, 504, model=ClaimErrorBody),
    )
    def report_documents(
        claim_id: ClaimId, body: DocumentsArrival
    ) -> ClaimMoveResponse | JSONResponse:
        return _reply(
            add_documents(dsn, tenant, http, tracer, claim_id, body.documents, meters),
            claim_id,
        )

    if settings.uploads_enabled:
        upload = add_upload_routes(
            app,
            dsn=dsn,
            tenant=tenant,
            tracer=tracer,
            limits=StoreLimits(
                ceiling_bytes=settings.uploads_ceiling_bytes,
                ceiling_rows=settings.uploads_ceiling_rows,
                rate_per_minute=settings.uploads_rate_per_minute,
            ),
        )
        # The claimant's form posts to the twin, which runs the same handler.
        add_claimant_upload_twin(app, upload)
    add_brief_routes(app, dsn=dsn, tenant=tenant, http=http, tracer=tracer)
    add_adjuster_pages(
        app,
        dsn=dsn,
        tenant=tenant,
        tracer=tracer,
        decide=lambda claim_id, decision, page_run: _decide(
            dsn, tenant, http, tracer, claim_id, decision, page_run
        ),
        triage_again=lambda claim_id, page_run: triage_again(
            dsn, tenant, http, tracer, claim_id, page_run=page_run, meters=meters
        ),
    )
    add_claimant_pages(
        app,
        dsn=dsn,
        tenant=tenant,
        http=http,
        tracer=tracer,
        today=today,
        deadline_days=settings.documents_deadline_days,
        meters=meters,
        uploads_enabled=settings.uploads_enabled,
    )
    return app


def create_app_from_env() -> FastAPI:
    """The factory S041 runs under ``uvicorn --factory``."""
    install_log_redaction()
    configure_logging(SERVICE_NAME)
    return create_app(ClaimsSettings.from_env())
