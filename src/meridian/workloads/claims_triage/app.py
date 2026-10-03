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
only reads it (T-31). It answers 200 only when the run completed.
"""

import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Path
from fastapi.responses import JSONResponse
from opentelemetry import propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer
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
from meridian.platform.common.logredaction import install_log_redaction
from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.platform.guardrails import EMAIL_PLACEHOLDER, redact
from meridian.runtime.models import RunResponse, RunState
from meridian.workloads.claims_triage.adjuster import (
    NO_SUCH_CLAIM_DETAIL,
    add_adjuster_pages,
)
from meridian.workloads.claims_triage.lifecycle import (
    ADJUSTER_APPROVED,
    ADJUSTER_REJECTED,
    ADJUSTER_REQUESTED_DOCUMENTS,
    RULES_APPROVED,
    RULES_REFERRED,
    RULES_REQUESTED_DOCUMENTS,
    SERVICE_NAME,
    TRIAGE_FAILED,
    TRIAGE_RECLAIMED,
    TRIAGE_RETRIED,
    TRIAGE_STARTED,
    LifecycleState,
    Transition,
    move_claim,
)
from meridian.workloads.claims_triage.models import (
    Claimant,
    ClaimDecision,
    ClaimErrorBody,
    ClaimResponse,
    ClaimSubmission,
    Decision,
    DecisionFailure,
    DecisionResponse,
    ProposalSummary,
    Route,
)
from meridian.workloads.claims_triage.proposal import TriageProposal
from meridian.workloads.claims_triage.settings import ClaimsSettings

AGENT = "claims-triage"
RUNTIME_TIMEOUT_SECONDS = 60.0
# How long a claim may stay ``triaging`` before another post takes the triage
# over: twice the longest a runtime call lasts, so a live request is not robbed.
TRIAGE_LEASE_SECONDS = 2 * RUNTIME_TIMEOUT_SECONDS
RUN_FAILED_DETAIL = "the triage run did not complete; the claim is stored"
RUN_TIMEOUT_DETAIL = "the triage run timed out; the claim is stored"
PROPOSAL_LOST_DETAIL = "the proposal could not be stored; the claim is stored"
HAS_PROPOSAL_DETAIL = "the claim already has a triage proposal"
BEING_TRIAGED_DETAIL = "the claim is being triaged"
TAKEN_OVER_DETAIL = "the triage was taken over by another request"
DIFFERENT_SUBMISSION_DETAIL = "the claim exists with a different submission"
NOT_WAITING_DETAIL = "the claim does not wait for an adjuster"
DECIDED_OTHERWISE_DETAIL = "the claim was decided otherwise"
RESUME_FAILED_DETAIL = "the decision is recorded; the run did not complete"
BEING_APPLIED_DETAIL = "the decision is being applied by another request"
HTTP_GATEWAY_TIMEOUT = 504

# What a run's answer means for the claim: its status and the proposal's route.
RUN_OUTCOMES: Mapping[tuple[RunState, Route], Transition] = {
    ("Completed", "auto_approve"): RULES_APPROVED,
    ("Completed", "request_documents"): RULES_REQUESTED_DOCUMENTS,
    ("AwaitingApproval", "adjuster"): RULES_REFERRED,
}
DECISION_TRANSITIONS: Mapping[Decision, Transition] = {
    "approve": ADJUSTER_APPROVED,
    "reject": ADJUSTER_REJECTED,
    "request_documents": ADJUSTER_REQUESTED_DOCUMENTS,
}

logger = logging.getLogger(__name__)

NAME_PLACEHOLDER = "[name]"
# The shortest part of a name that is replaced on its own: a shorter one ("Li",
# "Jr.") is also an ordinary word or an initial.
MIN_NAME_PART_LETTERS = 3


CURLY_APOSTROPHE = chr(0x2019)
# The characters a name is split on into the parts that are replaced on their
# own: white space, hyphens, apostrophes (straight and curly) and dots.
NAME_PART_SEPARATORS = re.compile(r"[\s\-'" + CURLY_APOSTROPHE + r".]+")
# A name is matched as a whole word, and never next to a square bracket: a
# placeholder is "[word]", and a part that is that word ("Name", "Email") must
# not turn it into "[[name]]".
NAME_BOUNDARY_BEFORE = r"(?<![\w\[\]])"
NAME_BOUNDARY_AFTER = r"(?![\w\[\]])"


def _name_alternatives(name: str) -> list[str]:
    """The patterns for a claimant's name, the longest first: the full name (any
    white space between its words), then each part, each with at least three
    letters. No alternative is empty: an empty one matches at every boundary.
    The minimum also bounds the copy's growth: a one-letter name would turn
    every "A" into ``[name]``."""
    alternatives = (
        [r"\s+".join(re.escape(word) for word in name.split())]
        if sum(char.isalpha() for char in name) >= MIN_NAME_PART_LETTERS
        else []
    )
    parts = {part for part in NAME_PART_SEPARATORS.split(name) if part}
    alternatives += [
        re.escape(part)
        for part in sorted(parts, key=len, reverse=True)
        if sum(char.isalpha() for char in part) >= MIN_NAME_PART_LETTERS
    ]
    return [alternative for alternative in alternatives if alternative]


def description_for_run(description: str, claimant: Claimant) -> str:
    """The description the run is sent (S047), in three steps: the claimant's
    e-mail address, ignoring case, becomes ``[email]``; ``redact`` replaces what
    it finds (so a third party's address that shares the claimant's surname is
    one address, not cut by a name); then the full name and each part of it of
    at least three letters become ``[name]``, each as a whole word and ignoring
    case. A pattern cannot find a name, and this API is the one place that knows
    it. The claimant's values are escaped: they are matched, never read as a
    pattern. The name is replaced in one pass, and never next to a square
    bracket, so no placeholder is matched or nested. The copy can be longer than
    the submission (``MAX_RUN_DESCRIPTION_CHARS``)."""
    emailless = re.sub(
        re.escape(claimant.email), EMAIL_PLACEHOLDER, description, flags=re.IGNORECASE
    )
    redacted = redact(emailless).text
    alternatives = _name_alternatives(claimant.name)
    if not alternatives:
        return redacted
    whole = "(?:" + "|".join(alternatives) + ")"
    pattern = NAME_BOUNDARY_BEFORE + whole + NAME_BOUNDARY_AFTER
    return re.sub(pattern, NAME_PLACEHOLDER, redacted, flags=re.IGNORECASE)


class RuntimeCallError(Exception):
    """The runtime gave no usable answer to a call.

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


def _call_runtime(http: httpx.Client, path: str, body: dict[str, Any]) -> RunResponse:
    """Post to the runtime with the trace context; raise ``RuntimeCallError``
    for any failure."""
    headers: dict[str, str] = {}
    propagate.inject(headers)
    try:
        response = http.post(path, json=body, headers=headers)
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


def _start_run(
    http: httpx.Client, tenant: str, reference: str, facts: dict[str, Any]
) -> RunResponse:
    return _call_runtime(
        http,
        "/runs",
        {
            "agent": AGENT,
            "tenant": tenant,
            "reference": reference,
            "input": {"claim": facts},
        },
    )


def _resume_run(
    http: httpx.Client, tenant: str, claim_id: str, run_id: UUID
) -> RunResponse:
    """Resume the paused run. The resume carries no decision: the run reads the
    one recorded here (T-31), so a caller of the runtime cannot make one up."""
    return _call_runtime(
        http,
        f"/runs/{run_id}/resume",
        {"tenant": tenant, "reference": claim_id, "input": {}},
    )


def _triage_outcome(run: RunResponse) -> tuple[TriageProposal, Transition]:
    """The run's proposal and the claim's move it leads to; anything else the
    runtime answered is outside the contract."""
    try:
        proposal = TriageProposal.model_validate(run.output)
    except ValidationError:
        raise RuntimeCallError(
            "the runtime's output is not a triage proposal", run_id=run.run_id
        ) from None
    transition = RUN_OUTCOMES.get((run.status, proposal.route))
    if transition is None:
        raise RuntimeCallError(
            "the runtime's answer does not fit its proposal", run_id=run.run_id
        )
    return proposal, transition


def _store_claim(dsn: str, tenant: str, claim: dict[str, Any]) -> None:
    """Store the claim. An existing one under this ID is left as it is; it is a
    409 unless it is the same submission."""
    with connect(dsn, SERVICE_NAME) as conn:
        cursor = conn.execute(
            "INSERT INTO claims.claims (claim_id, tenant, submission) "
            "VALUES (%s, %s, %s) ON CONFLICT (claim_id) DO NOTHING",
            (claim["claim_id"], tenant, Jsonb(claim)),
        )
        if cursor.rowcount == 1:
            return
        row = conn.execute(
            "SELECT submission, tenant FROM claims.claims WHERE claim_id = %s",
            (claim["claim_id"],),
        ).fetchone()
    # A claim of another tenant answers as a different submission does, so no
    # answer tells a caller that another tenant has the ID.
    if row is None or row[1] != tenant or row[0] != claim:
        raise HTTPException(409, DIFFERENT_SUBMISSION_DETAIL)


def _take_triage(
    dsn: str, tenant: str, claim_id: str
) -> tuple[datetime | None, LifecycleState]:
    """Move the claim to ``triaging`` if it may be triaged now.

    Returns the moment it moved (the request keeps it: its closing update
    matches on it) or ``None``, and the state the claim was found in.
    """
    with connect(dsn, SERVICE_NAME) as conn:
        row = conn.execute(
            "SELECT state, state_changed_at, "
            "state_changed_at < clock_timestamp() - make_interval(secs => %s) "
            "FROM claims.claims WHERE claim_id = %s AND tenant = %s "
            "FOR NO KEY UPDATE",
            (TRIAGE_LEASE_SECONDS, claim_id, tenant),
        ).fetchone()
        if row is None:
            # Not this tenant's (``_store_claim`` refuses that first) or gone.
            raise HTTPException(409, DIFFERENT_SUBMISSION_DETAIL)
        state, changed_at, lapsed = row
        if state == "submitted":
            transition = TRIAGE_STARTED
        elif state == "triage_failed":
            transition = TRIAGE_RETRIED
        elif state == "triaging" and lapsed:
            transition = TRIAGE_RECLAIMED
        else:
            return None, state
        moved_at = move_claim(
            conn, transition, claim_id=claim_id, tenant=tenant, changed_at=changed_at
        )
    return moved_at, state


def _insert_proposal(
    conn: psycopg.Connection, claim_id: str, run_id: UUID, proposal: TriageProposal
) -> None:
    conn.execute(
        "INSERT INTO claims.triage_proposals "
        "(proposal_id, claim_id, run_id, route, reason, proposal) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            uuid4(),
            claim_id,
            run_id,
            proposal.route,
            proposal.reason,
            Jsonb(proposal.model_dump(mode="json")),
        ),
    )


def _close_triage(
    dsn: str,
    tenant: str,
    claim_id: str,
    run: RunResponse,
    proposal: TriageProposal,
    transition: Transition,
    taken_at: datetime,
) -> bool:
    """Store the proposal and move the claim on, in one transaction.

    ``False`` when the claim is no longer the one this request took (its lease
    ran out and another request took the triage over); nothing is stored then.
    """
    with connect(dsn, SERVICE_NAME) as conn:
        moved = move_claim(
            conn,
            transition,
            claim_id=claim_id,
            tenant=tenant,
            run_id=run.run_id,
            changed_at=taken_at,
        )
        if moved is None:
            return False
        _insert_proposal(conn, claim_id, run.run_id, proposal)
    return True


def _fail_triage(
    dsn: str, tenant: str, claim_id: str, run_id: UUID | None, taken_at: datetime
) -> None:
    """Move the claim to ``triage_failed`` so a post triages it again. If that
    fails too the claim stays ``triaging`` until its lease runs out; the log
    says which claim and run it was."""
    try:
        with connect(dsn, SERVICE_NAME) as conn:
            move_claim(
                conn,
                TRIAGE_FAILED,
                claim_id=claim_id,
                tenant=tenant,
                run_id=run_id,
                changed_at=taken_at,
            )
    except psycopg.Error as exc:
        logger.error(
            "claim %s (run %s) could not be marked triage_failed: %s (sqlstate %s)",
            claim_id,
            run_id,
            type(exc).__name__,
            exc.sqlstate or "none",
        )


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


def _answer(
    status: int, detail: str, claim_id: str, run_id: UUID | None = None
) -> JSONResponse:
    extra = {} if run_id is None else {"run_id": str(run_id)}
    return error_answer(status, detail, claim_id=claim_id, **extra)


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
            run = _resume_run(http, tenant, claim_id, run_id)
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
        # The runtime gets what the graph needs, not the claimant's name or email,
        # and a description with neither of them in it (S047).
        facts = submission.model_dump(mode="json", exclude={"claimant"})
        facts["description"] = description_for_run(
            submission.description, submission.claimant
        )
        claim_id = submission.claim_id
        with start_span(tracer, "claims.submit") as span:
            set_span_attributes(
                span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
            )
            try:
                _store_claim(dsn, tenant, claim)
                taken_at, found_in = _take_triage(dsn, tenant, claim_id)
            except psycopg.Error as exc:
                mark_error(span, exc)
                return _answer(*database_failure(exc), claim_id)
            if taken_at is None:
                raise HTTPException(
                    409,
                    BEING_TRIAGED_DETAIL
                    if found_in == "triaging"
                    else HAS_PROPOSAL_DETAIL,
                )
            try:
                run = _start_run(http, tenant, claim_id, facts)
                proposal, transition = _triage_outcome(run)
            except RuntimeCallError as exc:
                logger.error(
                    "triage of %s failed: %s (runtime status %s, run %s)",
                    claim_id,
                    type(exc).__name__,
                    exc.status_code,
                    exc.run_id,
                )
                mark_error(span, exc)
                _fail_triage(dsn, tenant, claim_id, exc.run_id, taken_at)
                return _answer(
                    HTTP_GATEWAY_TIMEOUT if exc.timed_out else 502,
                    RUN_TIMEOUT_DETAIL if exc.timed_out else RUN_FAILED_DETAIL,
                    claim_id,
                    exc.run_id,
                )
            set_span_attributes(span, {"meridian.run_id": str(run.run_id)})
            try:
                closed = _close_triage(
                    dsn, tenant, claim_id, run, proposal, transition, taken_at
                )
            except psycopg.Error as exc:
                # The run finished and its proposal is lost unless the log says
                # which run it was: a retry triages again.
                logger.error(
                    "proposal of claim %s (run %s) not stored: %s (sqlstate %s)",
                    claim_id,
                    run.run_id,
                    type(exc).__name__,
                    exc.sqlstate or "none",
                )
                mark_error(span, exc)
                _fail_triage(dsn, tenant, claim_id, run.run_id, taken_at)
                return _answer(503, PROPOSAL_LOST_DETAIL, claim_id, run.run_id)
            if not closed:
                logger.warning(
                    "triage of %s (run %s) was taken over; its proposal is dropped",
                    claim_id,
                    run.run_id,
                )
                raise HTTPException(409, TAKEN_OVER_DETAIL)
            return ClaimResponse(
                claim_id=claim_id,
                state=transition.target,
                run_id=run.run_id,
                run_status=run.status,
                proposal=ProposalSummary(
                    route=proposal.route, drafted_by=proposal.drafted_by
                ),
            )

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
            return _answer(result.status, result.detail, claim_id, result.run_id)
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
