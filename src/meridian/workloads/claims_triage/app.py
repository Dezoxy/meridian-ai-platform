"""The Claims API app: ``POST /claims`` (S009).

A claim is stored, then triaged through the Agent Runtime, then its triage
proposal is stored. A failure after the claim is stored leaves the claim row in
place and answers 502, 503 or 504 with the claim's ID (and the run's, when
there is one). The same claim posted again, unchanged and still without a
proposal, is triaged again; the claim lifecycle in S015 will replace this.
"""

import logging
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from opentelemetry import propagate
from opentelemetry.sdk.trace import TracerProvider
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from meridian.platform.common.db import connect
from meridian.platform.common.http import (
    SMALL_BODY_LIMIT_BYTES,
    create_service_app,
    database_failure,
    error_answer,
    error_responses,
)
from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.runtime.models import RunResponse
from meridian.workloads.claims_triage.models import (
    ClaimErrorBody,
    ClaimResponse,
    ClaimSubmission,
    ProposalSummary,
    TriageProposal,
)
from meridian.workloads.claims_triage.settings import ClaimsSettings

SERVICE_NAME = "claims-api"
AGENT = "claims-triage"
RUNTIME_TIMEOUT_SECONDS = 60.0
RUN_FAILED_DETAIL = "the triage run did not complete; the claim is stored"
RUN_TIMEOUT_DETAIL = "the triage run timed out; the claim is stored"
PROPOSAL_LOST_DETAIL = "the proposal could not be stored; the claim is stored"
HTTP_GATEWAY_TIMEOUT = 504
# Until S014's deterministic rules exist, the graph can only say "adjuster".
# S014 widens this once its rule check exists; a route the rules have not
# vouched for is a contract violation, not a proposal.
ROUTES_ACCEPTED_UNTIL_S014 = frozenset({"adjuster"})

ClaimStanding = Literal["new", "retry", "has_proposal", "differs"]

logger = logging.getLogger(__name__)


class RuntimeCallError(Exception):
    """The runtime gave no usable answer to a triage call.

    Carries the HTTP status it answered (0: none, it timed out, was unreachable
    or answered outside its contract) and its run ID when it named one. The
    message is fixed text: neither the runtime's body nor the claim is kept.
    """

    def __init__(
        self,
        reason: str,
        *,
        status_code: int = 0,
        run_id: UUID | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.run_id = run_id
        self.timed_out = timed_out or status_code == HTTP_GATEWAY_TIMEOUT


def _run_id_in(response: httpx.Response) -> UUID | None:
    try:
        return UUID(response.json()["run_id"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _start_run(
    http: httpx.Client, tenant: str, reference: str, facts: dict[str, Any]
) -> RunResponse:
    """Call the runtime; raise ``RuntimeCallError`` for any failure."""
    headers: dict[str, str] = {}
    propagate.inject(headers)
    try:
        response = http.post(
            "/runs",
            json={
                "agent": AGENT,
                "tenant": tenant,
                "reference": reference,
                "input": {"claim": facts},
            },
            headers=headers,
        )
    except httpx.TimeoutException:
        raise RuntimeCallError("the runtime timed out", timed_out=True) from None
    except httpx.HTTPError:
        raise RuntimeCallError("the runtime is unreachable") from None
    if not 200 <= response.status_code < 300:
        raise RuntimeCallError(
            "the runtime answered an error",
            status_code=response.status_code,
            run_id=_run_id_in(response),
        )
    try:
        return RunResponse.model_validate(response.json())
    except (ValueError, ValidationError):
        raise RuntimeCallError("the runtime answered outside its contract") from None


def _accepted_proposal(run: RunResponse) -> TriageProposal | None:
    """The run's proposal; ``None`` while the run is not ``Completed``."""
    if run.status != "Completed":
        return None
    try:
        proposal = TriageProposal.model_validate(run.output)
    except ValidationError:
        raise RuntimeCallError(
            "the runtime's output is not a triage proposal", run_id=run.run_id
        ) from None
    if proposal.route not in ROUTES_ACCEPTED_UNTIL_S014:
        raise RuntimeCallError(
            "the runtime proposed a route not accepted yet", run_id=run.run_id
        )
    return proposal


def _store_claim(dsn: str, tenant: str, claim: dict[str, Any]) -> ClaimStanding:
    """Store the claim, or say how an existing one stands against this post."""
    with connect(dsn, SERVICE_NAME) as conn:
        cursor = conn.execute(
            "INSERT INTO claims.claims (claim_id, tenant, submission) "
            "VALUES (%s, %s, %s) ON CONFLICT (claim_id) DO NOTHING",
            (claim["claim_id"], tenant, Jsonb(claim)),
        )
        if cursor.rowcount == 1:
            return "new"
        row = conn.execute(
            "SELECT c.submission, EXISTS "
            "(SELECT 1 FROM claims.triage_proposals p WHERE p.claim_id = c.claim_id) "
            "FROM claims.claims c WHERE c.claim_id = %s",
            (claim["claim_id"],),
        ).fetchone()
    if row is None or row[0] != claim:
        return "differs"
    return "has_proposal" if row[1] else "retry"


def _insert_proposal(
    dsn: str, claim_id: str, run_id: UUID, proposal: TriageProposal
) -> None:
    with connect(dsn, SERVICE_NAME) as conn:
        conn.execute(
            "INSERT INTO claims.triage_proposals "
            "(proposal_id, claim_id, run_id, route, reason, draft, "
            "drafted_by_deployment, drafted_by_provider, drafted_by_mode) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                uuid4(),
                claim_id,
                run_id,
                proposal.route,
                proposal.reason,
                proposal.draft,
                proposal.drafted_by.deployment,
                proposal.drafted_by.provider,
                proposal.drafted_by.mode,
            ),
        )


def _answer(
    status: int, detail: str, claim_id: str, run_id: UUID | None = None
) -> JSONResponse:
    extra = {} if run_id is None else {"run_id": str(run_id)}
    return error_answer(status, detail, claim_id=claim_id, **extra)


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
        description="Takes a claim, has it triaged and keeps the proposal.",
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
        # The runtime gets what the graph needs, not the claimant's name or email.
        facts = submission.model_dump(mode="json", exclude={"claimant"})
        claim_id = submission.claim_id
        with start_span(tracer, "claims.submit") as span:
            set_span_attributes(
                span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
            )
            try:
                standing = _store_claim(dsn, tenant, claim)
            except psycopg.Error as exc:
                mark_error(span, exc)
                return _answer(*database_failure(exc), claim_id)
            if standing == "has_proposal":
                raise HTTPException(409, "the claim already has a triage proposal")
            if standing == "differs":
                raise HTTPException(409, "the claim exists with a different submission")
            try:
                run = _start_run(http, tenant, claim_id, facts)
                proposal = _accepted_proposal(run)
            except RuntimeCallError as exc:
                logger.error(
                    "triage of %s failed: %s (runtime status %s, run %s)",
                    claim_id,
                    type(exc).__name__,
                    exc.status_code,
                    exc.run_id,
                )
                mark_error(span, exc)
                return _answer(
                    HTTP_GATEWAY_TIMEOUT if exc.timed_out else 502,
                    RUN_TIMEOUT_DETAIL if exc.timed_out else RUN_FAILED_DETAIL,
                    claim_id,
                    exc.run_id,
                )
            set_span_attributes(span, {"meridian.run_id": str(run.run_id)})
            if proposal is not None:
                try:
                    _insert_proposal(dsn, claim_id, run.run_id, proposal)
                except psycopg.Error as exc:
                    # The run finished and its proposal is lost unless the
                    # log says which run it was: a retry triages again.
                    logger.error(
                        "proposal of claim %s (run %s) not stored: %s (sqlstate %s)",
                        claim_id,
                        run.run_id,
                        type(exc).__name__,
                        exc.sqlstate or "none",
                    )
                    mark_error(span, exc)
                    return _answer(503, PROPOSAL_LOST_DETAIL, claim_id, run.run_id)
            return ClaimResponse(
                claim_id=claim_id,
                run_id=run.run_id,
                run_status=run.status,
                proposal=(
                    None
                    if proposal is None
                    else ProposalSummary(
                        route=proposal.route,
                        reason=proposal.reason,
                        drafted_by=proposal.drafted_by,
                    )
                ),
            )

    return app


def create_app_from_env() -> FastAPI:
    """The factory S041 runs under ``uvicorn --factory``."""
    return create_app(ClaimsSettings.from_env())
