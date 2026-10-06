"""The claim brief on the Claims API: start one, decide it, read it (S037).

The claim brief is a second, small claims workflow on the same platform
contract. It decides nothing about the claim and never moves its state: the
Claims API starts a run of the agent ``claim-brief`` for a stored claim, the run
drafts a short plain-text brief with one model call and pauses, an adjuster
decides whether the brief is filed, and the run, resumed, files a note or
writes nothing. The brief lives in ``claims.briefs`` (migration 0024), one row
per brief, apart from the claim's own ``run_id`` and state.

- ``POST /claims/{claim_id}/brief`` inserts the brief as ``drafting`` and commits
  before the run starts, so a request that dies leaves a row the runtime's lease
  covers; it then stores the run's ID and text. One open brief per claim.
- ``POST /claims/{claim_id}/brief/decision`` records the decision for the
  brief's own run in ``claims.decisions`` and resumes the run. The resume carries
  no decision: the workload reads the recorded one through ``approval_outcome``,
  so the Claims API records it and the run only reads it (T-31).
- ``GET /claims/{claim_id}/brief`` reads the claim's latest brief.

The routes take what the JSON decision route beside them takes: no sign-in and
no same-origin check (T-69; the pages carry that one), a JSON body that a browser
cannot send without a preflight (T-01) and the app's tenant. The model's text is
returned by these three answers and nowhere else: never in a log line, a span, an
audit row or an error (T-03). The runtime's output is validated here as
``BriefOutput``; whatever else it answers is a failure with fixed text.
"""

import logging
from typing import Annotated, Any, Literal, NamedTuple
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from opentelemetry.trace import Span, Tracer
from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)

from meridian.platform.common.audit import AuditEvent, record_event
from meridian.platform.common.db import connect
from meridian.platform.common.http import INTERNAL_ERROR, error_responses
from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.platform.common.wire import NoNul, WireModel
from meridian.runtime.models import RunResponse
from meridian.runtime.sweep import RUNNING_LEASE_SECONDS
from meridian.workloads.claims_triage.adjuster import NO_SUCH_CLAIM_DETAIL, ClaimId
from meridian.workloads.claims_triage.lifecycle import BRIEF_AGENT, SERVICE_NAME
from meridian.workloads.claims_triage.models import (
    ClaimErrorBody,
    ClaimMoveRequest,
    DecisionFailure,
)
from meridian.workloads.claims_triage.moves import (
    RECORD_OUTCOME_SQL,
    StoredSubmissionInvalid,
    _submission,
)
from meridian.workloads.claims_triage.triaging import (
    HTTP_GATEWAY_TIMEOUT,
    RuntimeCallError,
    _call_runtime,
    answer,
    arrived_documents,
    claim_database_failure,
    facts_for_run,
    resume_run,
)

logger = logging.getLogger(__name__)

BriefState = Literal["drafting", "awaiting_decision", "filed", "rejected", "failed"]
BriefDecisionWord = Literal["approve", "reject"]
# The bound of the text, in characters: the table's CHECK says the same.
MAX_BRIEF_CHARS = 4000

NO_SUCH_BRIEF_DETAIL = "the claim has no brief"
BRIEF_AWAITING_DETAIL = "the claim has a brief that waits for a decision"
BRIEF_DRAFTING_DETAIL = "the claim's brief is being drafted"
BRIEF_FAILED_DETAIL = "the brief run did not complete"
BRIEF_TIMEOUT_DETAIL = "the brief run timed out"
STALE_BRIEF_DETAIL = "the claim has another brief now; read it again"
BRIEF_NOT_WAITING_DETAIL = "the brief does not wait for a decision"
BRIEF_DECIDED_OTHERWISE_DETAIL = "the brief was decided otherwise"
# The two texts of the triage's decision (``app.py``), said again here because
# ``app.py`` imports this module; a test holds them equal to the originals.
RESUME_FAILED_DETAIL = "the decision is recorded; the run did not complete"
BEING_APPLIED_DETAIL = "the decision is being applied by another request"
# The audit event of a decision: a closed vocabulary of this module, in the form
# of ``move_claim``'s (the claim as the reference, the run's ID, no text).
BRIEF_DECIDED_EVENT = "brief.decided"
BRIEF_DECIDED_REASON = "adjuster-decision"

# A brief that has been drafting for longer than the runtime's lease is a request
# that died: it reads as failed, and a new one may start. Every statement says the
# columns of the view in the same order: claim, state, text, run, two times.
CLAIM_SQL = "SELECT 1 FROM claims.claims WHERE claim_id = %s AND tenant = %s"
LOCK_CLAIM_SQL = (
    "SELECT 1 FROM claims.claims WHERE claim_id = %s AND tenant = %s FOR NO KEY UPDATE"
)
OPEN_BRIEF_SQL = (
    "SELECT brief_id, state, "
    "state_changed_at < clock_timestamp() - make_interval(secs => %(lease)s) "
    "FROM claims.briefs "
    "WHERE claim_id = %(claim)s AND tenant = %(tenant)s "
    "AND state IN ('drafting', 'awaiting_decision')"
)
LAPSE_SQL = (
    "UPDATE claims.briefs SET state = 'failed', state_changed_at = clock_timestamp() "
    "WHERE brief_id = %s AND state = 'drafting'"
)
INSERT_BRIEF_SQL = (
    "INSERT INTO claims.briefs (brief_id, claim_id, tenant, state) "
    "VALUES (%s, %s, %s, 'drafting')"
)
STORE_BRIEF_SQL = (
    "UPDATE claims.briefs SET run_id = %s, brief = %s, state = 'awaiting_decision', "
    "state_changed_at = clock_timestamp() WHERE brief_id = %s AND state = 'drafting' "
    "RETURNING claim_id, state, brief, run_id, created_at, state_changed_at"
)
FAIL_BRIEF_SQL = (
    "UPDATE claims.briefs SET run_id = COALESCE(%s, run_id), state = 'failed', "
    "state_changed_at = clock_timestamp() WHERE brief_id = %s AND state = 'drafting'"
)
READ_LATEST_SQL = (
    "SELECT claim_id, CASE WHEN state = 'drafting' AND state_changed_at < "
    "clock_timestamp() - make_interval(secs => %(lease)s) "
    "THEN 'failed' ELSE state END, "
    "brief, run_id, created_at, state_changed_at FROM claims.briefs "
    "WHERE claim_id = %(claim)s AND tenant = %(tenant)s "
    "ORDER BY created_at DESC, brief_id DESC LIMIT 1"
)
LOCK_LATEST_SQL = (
    "SELECT state, run_id FROM claims.briefs "
    "WHERE claim_id = %s AND tenant = %s "
    "ORDER BY created_at DESC, brief_id DESC LIMIT 1 FOR NO KEY UPDATE"
)
DECISION_OF_RUN_SQL = (
    "SELECT decision FROM claims.decisions WHERE claim_id = %s AND run_id = %s"
)
CLOSE_BRIEF_SQL = (
    "UPDATE claims.briefs SET state = %s, state_changed_at = clock_timestamp() "
    "WHERE run_id = %s AND claim_id = %s AND tenant = %s "
    "AND state = 'awaiting_decision' "
    "RETURNING claim_id, state, brief, run_id, created_at, state_changed_at"
)
# What the resumed run's ``filed`` must say for each decision.
FILED_BY_DECISION: dict[BriefDecisionWord, bool] = {"approve": True, "reject": False}
STATE_BY_DECISION: dict[BriefDecisionWord, BriefState] = {
    "approve": "filed",
    "reject": "rejected",
}


class BriefOutput(WireModel):
    """What the runtime's run of ``claim-brief`` outputs: the brief's text, and
    after the resume whether it was filed. Strict, so no type is coerced."""

    model_config = ConfigDict(strict=True)

    brief: Annotated[
        str, StringConstraints(min_length=1, max_length=MAX_BRIEF_CHARS), NoNul
    ]
    filed: bool | None = None


class BriefDecision(WireModel):
    """What ``POST /claims/{claim_id}/brief/decision`` takes: the word, and the
    run of the brief it was read on (a decision for another run is a 409)."""

    model_config = ConfigDict(strict=True)

    decision: BriefDecisionWord
    run: Annotated[UUID, Field(strict=False)]


class BriefView(WireModel):
    """A brief as the routes answer it: the one place its text is returned."""

    claim_id: str
    state: BriefState
    brief: str | None
    run_id: UUID | None
    created_at: AwareDatetime
    state_changed_at: AwareDatetime


class _Taken(NamedTuple):
    """A brief this request inserted as ``drafting``, and the facts to run it."""

    brief_id: UUID
    facts: dict[str, Any]


def _view(row: tuple[Any, ...]) -> BriefView:
    claim_id, state, brief, run_id, created_at, changed_at = row
    return BriefView(
        claim_id=claim_id,
        state=state,
        brief=brief,
        run_id=run_id,
        created_at=created_at,
        state_changed_at=changed_at,
    )


def _reply[Success](
    result: Success | DecisionFailure, claim_id: str
) -> Success | JSONResponse:
    if isinstance(result, DecisionFailure):
        return answer(result.status, result.detail, claim_id, result.run_id)
    return result


def _require_claim(
    conn: psycopg.Connection, tenant: str, claim_id: str, statement: str = CLAIM_SQL
) -> None:
    """A claim of this tenant (another tenant's is none), else 404."""
    if conn.execute(statement, (claim_id, tenant)).fetchone() is None:
        raise HTTPException(404, NO_SUCH_CLAIM_DETAIL)


def _refuse_open_brief(conn: psycopg.Connection, tenant: str, claim_id: str) -> None:
    """Refuse (409) a claim that has a brief open; mark a ``drafting`` one older
    than the lease failed, in the caller's transaction, so a new one may start."""
    params = {"claim": claim_id, "tenant": tenant, "lease": RUNNING_LEASE_SECONDS}
    row = conn.execute(OPEN_BRIEF_SQL, params).fetchone()
    if row is None:
        return
    brief_id, state, lapsed = row
    if state == "awaiting_decision":
        raise HTTPException(409, BRIEF_AWAITING_DETAIL)
    if not lapsed:
        raise HTTPException(409, BRIEF_DRAFTING_DETAIL)
    conn.execute(LAPSE_SQL, (brief_id,))


def _take_brief(dsn: str, tenant: str, claim_id: str) -> _Taken:
    """Insert the claim's brief as ``drafting`` and commit it, with the facts the
    run is sent (a triage's, with no claimant). A refusal is an ``HTTPException``."""
    with connect(dsn, SERVICE_NAME) as conn:
        _require_claim(conn, tenant, claim_id, LOCK_CLAIM_SQL)
        _refuse_open_brief(conn, tenant, claim_id)
        facts = facts_for_run(
            _submission(conn, claim_id), arrived_documents(conn, claim_id)
        )
        brief_id = uuid4()
        conn.execute(INSERT_BRIEF_SQL, (brief_id, claim_id, tenant))
    return _Taken(brief_id, facts)


def _output_of(run: RunResponse, *, resumed: bool) -> BriefOutput:
    """The run's output as a brief, or ``RuntimeCallError`` with fixed text: the
    first leg pauses, the resumed leg completes, and nothing else is accepted."""
    expected = "Completed" if resumed else "AwaitingApproval"
    if run.status != expected:
        raise RuntimeCallError(
            "the runtime's answer does not fit a brief", run_id=run.run_id
        )
    try:
        return BriefOutput.model_validate(run.output)
    except ValidationError:
        raise RuntimeCallError(
            "the runtime's output is not a brief", run_id=run.run_id
        ) from None


def _mark_failed(dsn: str, claim_id: str, brief_id: UUID, run_id: UUID | None) -> None:
    """Mark the brief failed and keep the run's ID when there is one. If that
    fails too the brief stays ``drafting`` until the lease runs out; the log says
    which claim and run it was."""
    try:
        with connect(dsn, SERVICE_NAME) as conn:
            conn.execute(FAIL_BRIEF_SQL, (run_id, brief_id))
    except psycopg.Error as exc:
        logger.error(
            "brief of claim %s (run %s) could not be marked failed: %s (sqlstate %s)",
            claim_id,
            run_id,
            type(exc).__name__,
            exc.sqlstate or "none",
        )


def _fail_first_leg(
    dsn: str, span: Span, claim_id: str, brief_id: UUID, exc: RuntimeCallError
) -> DecisionFailure:
    """A first leg that did not give a brief: fixed text, never the runtime's or
    the model's."""
    logger.error(
        "brief of %s failed: %s (runtime status %s, run %s)",
        claim_id,
        type(exc).__name__,
        exc.status_code,
        exc.run_id,
    )
    mark_error(span, exc)
    _mark_failed(dsn, claim_id, brief_id, exc.run_id)
    return DecisionFailure(
        HTTP_GATEWAY_TIMEOUT if exc.timed_out else 502,
        BRIEF_TIMEOUT_DETAIL if exc.timed_out else BRIEF_FAILED_DETAIL,
        exc.run_id,
    )


def _store_brief(
    dsn: str, span: Span, claim_id: str, taken: _Taken, run: RunResponse, text: str
) -> BriefView | DecisionFailure:
    try:
        with connect(dsn, SERVICE_NAME) as conn:
            row = conn.execute(
                STORE_BRIEF_SQL, (run.run_id, text, taken.brief_id)
            ).fetchone()
    except psycopg.Error as exc:
        # The run paused and its brief is lost unless the log says which run it
        # was: the sweep ends a run no brief names, and the lease ends the row.
        mark_error(span, exc)
        status, detail = claim_database_failure(exc, claim_id)
        logger.error("brief of claim %s (run %s) not stored", claim_id, run.run_id)
        return DecisionFailure(status, detail, run.run_id)
    if row is None:
        logger.warning("brief of %s (run %s) was taken over", claim_id, run.run_id)
        return DecisionFailure(502, BRIEF_FAILED_DETAIL, run.run_id)
    return _view(row)


def start_brief(
    dsn: str, tenant: str, http: httpx.Client, tracer: Tracer, claim_id: str
) -> BriefView | DecisionFailure:
    """Start the claim's brief: insert it, run it, store what the run paused
    with. A refusal (404, 409) is raised as ``HTTPException``; every other
    failure is a ``DecisionFailure`` and the brief ends ``failed``."""
    with start_span(tracer, "claims.brief") as span:
        set_span_attributes(
            span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
        )
        try:
            taken = _take_brief(dsn, tenant, claim_id)
        except psycopg.Error as exc:
            mark_error(span, exc)
            return DecisionFailure(*claim_database_failure(exc, claim_id))
        except StoredSubmissionInvalid as exc:
            mark_error(span, exc)
            return DecisionFailure(500, INTERNAL_ERROR)
        try:
            run = _call_runtime(
                http,
                "/runs",
                {
                    "agent": BRIEF_AGENT,
                    "tenant": tenant,
                    "reference": claim_id,
                    "input": {"claim": taken.facts},
                },
            )
            output = _output_of(run, resumed=False)
        except RuntimeCallError as exc:
            return _fail_first_leg(dsn, span, claim_id, taken.brief_id, exc)
        set_span_attributes(span, {"meridian.run_id": str(run.run_id)})
        return _store_brief(dsn, span, claim_id, taken, run, output.brief)


def _record_decision(dsn: str, tenant: str, claim_id: str, body: BriefDecision) -> None:
    """Record the decision for the brief's run and audit it, in one transaction.
    The brief's row is locked; it must be the claim's latest, for the run the
    caller read, and awaiting its decision. A decision made again is recorded
    once; a different one is a 409. A refusal is an ``HTTPException``."""
    with connect(dsn, SERVICE_NAME) as conn:
        _require_claim(conn, tenant, claim_id)
        row = conn.execute(LOCK_LATEST_SQL, (claim_id, tenant)).fetchone()
        if row is None:
            raise HTTPException(404, NO_SUCH_BRIEF_DETAIL)
        state, run_id = row
        if run_id != body.run:
            raise HTTPException(409, STALE_BRIEF_DETAIL)
        if state != "awaiting_decision":
            raise HTTPException(409, BRIEF_NOT_WAITING_DETAIL)
        recorded = conn.execute(DECISION_OF_RUN_SQL, (claim_id, run_id)).fetchone()
        if recorded is not None:
            if recorded[0] != body.decision:
                raise HTTPException(409, BRIEF_DECIDED_OTHERWISE_DETAIL)
            return
        conn.execute(RECORD_OUTCOME_SQL, (claim_id, run_id, body.decision))
        record_event(
            conn,
            AuditEvent(
                service=SERVICE_NAME,
                event=BRIEF_DECIDED_EVENT,
                outcome=body.decision,
                reason=BRIEF_DECIDED_REASON,
                tenant=tenant,
                run_id=run_id,
                reference=claim_id,
            ),
        )


def _close_brief(
    dsn: str, tenant: str, claim_id: str, body: BriefDecision, run: RunResponse
) -> BriefView | DecisionFailure:
    """The resumed run ended: a valid output whose ``filed`` agrees with the
    decision closes the brief, anything else leaves it waiting (the decision
    stays recorded, and posting it again resumes again)."""
    try:
        output = _output_of(run, resumed=True)
    except RuntimeCallError as exc:
        logger.error(
            "resume of run %s for claim %s: %s", body.run, claim_id, type(exc).__name__
        )
        return DecisionFailure(502, RESUME_FAILED_DETAIL, body.run)
    if output.filed is not FILED_BY_DECISION[body.decision]:
        logger.error(
            "resume of run %s for claim %s: filed disagrees with the decision",
            body.run,
            claim_id,
        )
        return DecisionFailure(502, RESUME_FAILED_DETAIL, body.run)
    with connect(dsn, SERVICE_NAME) as conn:
        row = conn.execute(
            CLOSE_BRIEF_SQL,
            (STATE_BY_DECISION[body.decision], body.run, claim_id, tenant),
        ).fetchone()
    if row is None:
        return DecisionFailure(409, BRIEF_NOT_WAITING_DETAIL, body.run)
    return _view(row)


def _resume(
    http: httpx.Client, span: Span, tenant: str, claim_id: str, run_id: UUID
) -> RunResponse | DecisionFailure:
    try:
        return resume_run(http, tenant, claim_id, run_id)
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
            HTTP_GATEWAY_TIMEOUT if exc.timed_out else 502, RESUME_FAILED_DETAIL, run_id
        )


def decide_brief(
    dsn: str,
    tenant: str,
    http: httpx.Client,
    tracer: Tracer,
    claim_id: str,
    body: BriefDecision,
) -> BriefView | DecisionFailure:
    """Record the decision, then resume the run and close the brief by what it
    answers. A refusal (404, 409) is raised as ``HTTPException``; every other
    failure is a ``DecisionFailure`` and the decision, once recorded, stays."""
    with start_span(tracer, "claims.brief_decision") as span:
        set_span_attributes(
            span,
            {
                "meridian.claim_id": claim_id,
                "meridian.tenant": tenant,
                "meridian.run_id": str(body.run),
            },
        )
        try:
            _record_decision(dsn, tenant, claim_id, body)
        except psycopg.Error as exc:
            mark_error(span, exc)
            return DecisionFailure(*claim_database_failure(exc, claim_id))
        run = _resume(http, span, tenant, claim_id, body.run)
        if isinstance(run, DecisionFailure):
            return run
        if run.status == "Running":
            logger.info(
                "resume of run %s for claim %s: another request is applying it",
                body.run,
                claim_id,
            )
            return DecisionFailure(409, BEING_APPLIED_DETAIL, body.run)
        try:
            return _close_brief(dsn, tenant, claim_id, body, run)
        except psycopg.Error as exc:
            mark_error(span, exc)
            status, detail = claim_database_failure(exc, claim_id)
            return DecisionFailure(status, detail, body.run)


def read_brief(dsn: str, tenant: str, claim_id: str) -> BriefView:
    """The claim's latest brief; a ``drafting`` one older than the lease reads as
    ``failed``. 404 for a claim that is not the tenant's and for one with none."""
    with connect(dsn, SERVICE_NAME) as conn:
        _require_claim(conn, tenant, claim_id)
        params = {"claim": claim_id, "tenant": tenant, "lease": RUNNING_LEASE_SECONDS}
        row = conn.execute(READ_LATEST_SQL, params).fetchone()
    if row is None:
        raise HTTPException(404, NO_SUCH_BRIEF_DETAIL)
    return _view(row)


def add_brief_routes(
    app: FastAPI, *, dsn: str, tenant: str, http: httpx.Client, tracer: Tracer
) -> None:
    """Add the three routes of the claim brief to the Claims API."""

    @app.post(
        "/claims/{claim_id}/brief",
        status_code=201,
        response_model=BriefView,
        tags=["claims"],
        summary="Start a brief of a claim: a run drafts it and pauses for a decision.",
        responses=error_responses(404, 409, 413)
        | error_responses(500, 502, 503, 504, model=ClaimErrorBody),
    )
    def start_claim_brief(
        claim_id: ClaimId, body: ClaimMoveRequest
    ) -> BriefView | JSONResponse:
        return _reply(start_brief(dsn, tenant, http, tracer, claim_id), claim_id)

    @app.post(
        "/claims/{claim_id}/brief/decision",
        response_model=BriefView,
        tags=["claims"],
        summary="Record the decision on a claim's brief and resume its paused run.",
        responses=error_responses(404, 409, 413)
        | error_responses(500, 502, 503, 504, model=ClaimErrorBody),
    )
    def decide_claim_brief(
        claim_id: ClaimId, body: BriefDecision
    ) -> BriefView | JSONResponse:
        return _reply(decide_brief(dsn, tenant, http, tracer, claim_id, body), claim_id)

    @app.get(
        "/claims/{claim_id}/brief",
        response_model=BriefView,
        tags=["claims"],
        summary="Read the latest brief of a claim.",
        responses=error_responses(404)
        | error_responses(500, 503, model=ClaimErrorBody),
    )
    def read_claim_brief(claim_id: ClaimId) -> BriefView | JSONResponse:
        try:
            return read_brief(dsn, tenant, claim_id)
        except psycopg.Error as exc:
            return answer(*claim_database_failure(exc, claim_id), claim_id)
