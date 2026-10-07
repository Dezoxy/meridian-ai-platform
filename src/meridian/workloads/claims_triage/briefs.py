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
  covers; it then stores the run's ID and text. One open brief per claim. A claim
  that is ``approved``, ``rejected`` or ``withdrawn`` has no brief started (409),
  and neither has one that has had ``MAX_BRIEFS_PER_CLAIM`` (409): both are
  decided in the transaction that locks the claim's row, because the routes have
  no sign-in (T-69) and each brief costs a model call.
- ``POST /claims/{claim_id}/brief/decision`` records the decision for the
  brief's own run in ``claims.decisions`` and resumes the run. The resume carries
  no decision: the workload reads the recorded one through ``approval_outcome``,
  so the Claims API records it and the run only reads it (T-31). A claim that
  closed since the brief was started has the decision recorded as ``reject``
  whatever the body says (the audit reason is ``claim-closed``): the run reads
  it, files nothing and ends, and the brief closes as ``rejected``. An approval
  recorded while the claim was open and never carried out (its resume failed) is
  not carried out once the claim has closed: the brief is closed as ``rejected``
  without a resume, whichever word is posted, and the run is left to the sweep.
  The recorded decision is also the authority when the close was lost: a run the
  runtime says has ended, with a status and no output, closes the brief by the
  decision (``Completed``) or as ``failed`` (``Failed``), so a brief never waits
  for ever and a new one may start; so does a resume the runtime answers with a
  502 whose body says the run is ``Failed`` (the brief is closed as ``failed`` in
  that same request); an output that contradicts the decision is a 502 and the
  brief waits.
- ``GET /claims/{claim_id}/brief`` reads the claim's latest brief.

The answers carry no ``Cache-Control``, as the triage's JSON routes carry none
(only the adjuster's pages say ``no-store``). The decision's audit event
(``brief.decided``) names the claim and so is in the claim's trail, on purpose; the
sweep's event for an abandoned run of the brief's agent names no claim, so it is
not (``runtime/sweep.py``).

The routes take what the JSON decision route beside them takes: no sign-in and
no same-origin check (T-69; the pages carry that one), a JSON body that a browser
cannot send without a preflight (T-01) and the app's tenant. The model's text is
returned by these three answers and nowhere else: never in a log line, a span, an
audit row or an error (T-03). It is redacted (``_redacted``) before it is stored,
so the row and every answer hold the redacted form. The runtime's output is
validated here as ``BriefOutput``; whatever else it answers is a failure with
fixed text.
"""

import logging
from collections.abc import Sequence
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
from meridian.platform.guardrails import redact
from meridian.runtime.models import RunResponse
from meridian.runtime.sweep import RUNNING_LEASE_SECONDS
from meridian.workloads.claims_triage.adjuster import NO_SUCH_CLAIM_DETAIL, ClaimId
from meridian.workloads.claims_triage.lifecycle import BRIEF_AGENT, SERVICE_NAME
from meridian.workloads.claims_triage.models import (
    ClaimErrorBody,
    ClaimMoveRequest,
    ClaimSubmission,
    DecisionFailure,
    invalid_fields,
)
from meridian.workloads.claims_triage.moves import (
    RECORD_OUTCOME_SQL,
    StoredSubmissionInvalid,
    _submission,
)
from meridian.workloads.claims_triage.triaging import (
    HTTP_GATEWAY_TIMEOUT,
    RuntimeCallError,
    answer,
    arrived_documents,
    claim_database_failure,
    resume_run,
    start_run,
)

logger = logging.getLogger(__name__)

BriefState = Literal["drafting", "awaiting_decision", "filed", "rejected", "failed"]
BriefDecisionWord = Literal["approve", "reject"]
# The bound of the text, in characters: the table's CHECK says the same.
MAX_BRIEF_CHARS = 4000
# How many briefs a claim may have had, whatever state each ended in: each is a
# model call, tool calls and, on approve, a note from the shared tenant's budget,
# and the routes have no sign-in (T-69). The triages per claim are bounded the
# same way (``MAX_TRIAGES_PER_CLAIM``, T-38).
MAX_BRIEFS_PER_CLAIM = 5
# The claim's states nothing more happens in, so a brief of it has no use: the
# lifecycle has no edge out of them. The others can still move (``triage_failed``
# is referred, ``documents_requested`` takes documents) or are in flight.
CLOSED_CLAIM_STATES = frozenset({"approved", "rejected", "withdrawn"})
# The claim's fields the brief's workflow reads (``claim_brief.facts.ClaimInput``
# names the same, plus the count of documents). The test that holds THIS list to
# the workflow's model, in ``test_claim_brief_start.py`` (what the route sends
# equals ``ClaimInput.model_fields``), is:
# test_the_input_is_what_the_workflow_validates_and_nothing_it_does_not_read
# The copy in the tests' support module (``claimbriefsupport.py``) is test input,
# and no test holds it equal.
BRIEF_INPUT_FIELDS = (
    "claim_id",
    "policy_number",
    "reported_on",
    "loss_date",
    "peril",
    "claimed_amount",
)

CLAIM_CLOSED_DETAIL = "the claim is closed; no brief can be started for it"
BRIEF_LIMIT_DETAIL = "the claim has had as many briefs as it may have"
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
# The reason of a decision that was recorded as a rejection because the claim had
# closed since the brief was started.
BRIEF_CLOSED_REASON = "claim-closed"

# A brief that has been drafting for longer than the runtime's lease is a request
# that died: it reads as failed, and a new one may start. Every statement says the
# columns of the view in the same order: claim, state, text, run, two times.
CLAIM_SQL = "SELECT 1 FROM claims.claims WHERE claim_id = %s AND tenant = %s"
LOCK_CLAIM_SQL = (
    "SELECT state FROM claims.claims "
    "WHERE claim_id = %s AND tenant = %s FOR NO KEY UPDATE"
)
COUNT_BRIEFS_SQL = (
    "SELECT count(*) FROM claims.briefs WHERE claim_id = %s AND tenant = %s"
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
) -> tuple[Any, ...]:
    """A claim of this tenant (another tenant's is none), else 404; the row the
    statement selected."""
    row = conn.execute(statement, (claim_id, tenant)).fetchone()
    if row is None:
        raise HTTPException(404, NO_SUCH_CLAIM_DETAIL)
    return row


def _refuse_closed_claim(state: str) -> None:
    """Refuse (409) a claim in a state nothing more happens in."""
    if state in CLOSED_CLAIM_STATES:
        raise HTTPException(409, CLAIM_CLOSED_DETAIL)


def _refuse_too_many_briefs(
    conn: psycopg.Connection, tenant: str, claim_id: str
) -> None:
    """Refuse (409) a claim that has had ``MAX_BRIEFS_PER_CLAIM`` briefs, counted
    in the caller's transaction, which holds the claim's row lock."""
    row = conn.execute(COUNT_BRIEFS_SQL, (claim_id, tenant)).fetchone()
    if row is not None and row[0] >= MAX_BRIEFS_PER_CLAIM:
        raise HTTPException(409, BRIEF_LIMIT_DETAIL)


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


def _brief_input(submission: ClaimSubmission, arrived: Sequence[str]) -> dict[str, Any]:
    """What the brief's run is sent: the fields its workflow reads
    (``claim_brief.facts.ClaimInput``) and no other. The run's input stays in the
    second host's checkpoint rows while the run lives, so what the workflow does
    not read is not sent: not the description, the city or the claimant (personal
    data), and not a document's name (the claimant's) but how many distinct
    documents there are, the submission's and those that arrived (S048)."""
    facts = submission.model_dump(mode="json", include=set(BRIEF_INPUT_FIELDS))
    facts["documents_received"] = len(dict.fromkeys([*submission.documents, *arrived]))
    return facts


def _take_brief(dsn: str, tenant: str, claim_id: str) -> _Taken:
    """Insert the claim's brief as ``drafting`` and commit it, with the facts the
    run is sent (``_brief_input``). A refusal is an ``HTTPException``."""
    with connect(dsn, SERVICE_NAME) as conn:
        (state,) = _require_claim(conn, tenant, claim_id, LOCK_CLAIM_SQL)
        _refuse_closed_claim(state)
        _refuse_open_brief(conn, tenant, claim_id)
        _refuse_too_many_briefs(conn, tenant, claim_id)
        facts = _brief_input(
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
    except ValidationError as exc:
        # The fields and their error types, never a value: the output is the
        # model's text.
        logger.warning(
            "the runtime's output of run %s is not a brief: %s %s",
            run.run_id,
            type(exc).__name__,
            invalid_fields(exc),
        )
        raise RuntimeCallError(
            "the runtime's output is not a brief", run_id=run.run_id
        ) from None


def _redacted(text: str) -> str:
    """The brief as it is stored and shown: the model saw only facts of the
    claim, but it can make up an e-mail address, an IBAN, a card or a phone
    number, so the text is redacted by the function that redacts the triage's
    rationale, then cut to the bound (a placeholder can be longer than the
    address it replaces, and the table's CHECK holds the bound; threat h)."""
    return redact(text).text[:MAX_BRIEF_CHARS]


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
            # The workflow reads the input's ``claim`` (``facts.read_claim``).
            run = start_run(
                http, tenant, claim_id, {"claim": taken.facts}, agent=BRIEF_AGENT
            )
            output = _output_of(run, resumed=False)
        except RuntimeCallError as exc:
            return _fail_first_leg(dsn, span, claim_id, taken.brief_id, exc)
        set_span_attributes(span, {"meridian.run_id": str(run.run_id)})
        return _store_brief(dsn, span, claim_id, taken, run, _redacted(output.brief))


def _audit_decision(
    conn: psycopg.Connection,
    tenant: str,
    claim_id: str,
    run_id: UUID,
    decision: BriefDecisionWord,
    reason: str,
) -> None:
    record_event(
        conn,
        AuditEvent(
            service=SERVICE_NAME,
            event=BRIEF_DECIDED_EVENT,
            outcome=decision,
            reason=reason,
            tenant=tenant,
            run_id=run_id,
            reference=claim_id,
        ),
    )


def _close_unfiled(
    conn: psycopg.Connection, tenant: str, claim_id: str, run_id: UUID
) -> BriefView:
    """Close the brief as rejected, without a resume, because its claim has closed
    and the approval recorded for it was never carried out (its resume failed).
    The approval stays in ``claims.decisions`` as what was posted; the audit row
    says the brief was closed as a rejection because the claim closed. The run is
    not resumed (it would read the approval and file a note), so it is left to the
    sweep, which ends the run of a brief that no longer awaits its decision once
    the lease has passed. The caller holds the claim's and the brief's row locks,
    and the brief awaits its decision."""
    row = conn.execute(
        CLOSE_BRIEF_SQL, ("rejected", run_id, claim_id, tenant)
    ).fetchone()
    if row is None:  # the brief's row is locked and awaiting: a never-path
        raise HTTPException(409, BRIEF_NOT_WAITING_DETAIL)
    _audit_decision(conn, tenant, claim_id, run_id, "reject", BRIEF_CLOSED_REASON)
    return _view(row)


def _record_decision(
    dsn: str, tenant: str, claim_id: str, body: BriefDecision
) -> BriefDecisionWord | BriefView:
    """Record the decision for the brief's run and audit it, in one transaction,
    and return the word recorded. The claim's row is locked first (as a start
    locks it, and as a move of the claim does) and its state read. A brief of a
    claim that is closed is never filed: a decision made now is recorded as
    ``reject`` whatever the body says, so that the run, which reads the recorded
    word, files nothing and the brief closes as rejected; and an approval that was
    recorded while the claim was open and never carried out is not carried out
    now: the brief is closed as rejected without a resume and its view is
    returned (``_close_unfiled``), whichever word is posted. The brief's row is
    locked; it must be the claim's latest, for the run the caller read, and
    awaiting its decision. For a claim that is open a decision made again is
    recorded once and the recorded one stands; a different one is a 409. For a
    closed claim whose recorded word is a rejection, an approval posted again is
    the rejection the first post recorded. A refusal is an ``HTTPException``."""
    with connect(dsn, SERVICE_NAME) as conn:
        (claim_state,) = _require_claim(conn, tenant, claim_id, LOCK_CLAIM_SQL)
        row = conn.execute(LOCK_LATEST_SQL, (claim_id, tenant)).fetchone()
        if row is None:
            raise HTTPException(404, NO_SUCH_BRIEF_DETAIL)
        state, run_id = row
        if run_id != body.run:
            raise HTTPException(409, STALE_BRIEF_DETAIL)
        if state != "awaiting_decision":
            raise HTTPException(409, BRIEF_NOT_WAITING_DETAIL)
        recorded = conn.execute(DECISION_OF_RUN_SQL, (claim_id, run_id)).fetchone()
        closed = claim_state in CLOSED_CLAIM_STATES
        if recorded is not None:
            if closed and recorded[0] == "approve":
                return _close_unfiled(conn, tenant, claim_id, run_id)
            # An approval posted again for a claim that has closed is the one the
            # first post had recorded as a rejection: it is not "otherwise".
            if recorded[0] != body.decision and not (
                closed and recorded[0] == "reject"
            ):
                raise HTTPException(409, BRIEF_DECIDED_OTHERWISE_DETAIL)
            return recorded[0]
        decision: BriefDecisionWord = body.decision
        reason = BRIEF_DECIDED_REASON
        if closed:
            decision = "reject"
            if body.decision != decision:
                reason = BRIEF_CLOSED_REASON
        conn.execute(RECORD_OUTCOME_SQL, (claim_id, run_id, decision))
        _audit_decision(conn, tenant, claim_id, run_id, decision, reason)
        return decision


def _state_to_close_with(
    claim_id: str, body: BriefDecision, run: RunResponse
) -> BriefState | None:
    """The state the resume's answer closes the brief in, or ``None`` when the
    brief must wait. The recorded decision is the authority (T-31): a run the
    runtime says ended, with a status and NO output (it answers so for any run
    that ended before this resume, whatever lost the first close), closes the
    brief by the decision when it completed and as ``failed`` when it did not,
    so a brief cannot wait for ever. An output is trusted only when it is a brief
    whose ``filed`` agrees with the decision: one that contradicts it, or is not
    a brief, is a fault to look at, and the brief waits."""
    if run.output is None and run.status in ("Completed", "Failed"):
        logger.warning(
            "resume of run %s for claim %s: the run had ended (%s) and answered no "
            "output; the brief is closed by the recorded decision",
            body.run,
            claim_id,
            run.status,
        )
        if run.status == "Failed":
            return "failed"
        return STATE_BY_DECISION[body.decision]
    try:
        output = _output_of(run, resumed=True)
    except RuntimeCallError as exc:
        logger.error(
            "resume of run %s for claim %s: %s", body.run, claim_id, type(exc).__name__
        )
        return None
    if output.filed is not FILED_BY_DECISION[body.decision]:
        logger.error(
            "resume of run %s for claim %s: filed disagrees with the decision",
            body.run,
            claim_id,
        )
        return None
    return STATE_BY_DECISION[body.decision]


def _close_brief(
    dsn: str, tenant: str, claim_id: str, body: BriefDecision, run: RunResponse
) -> BriefView | DecisionFailure:
    """The resumed run answered: close the brief in the state the answer allows
    (``_state_to_close_with``). A run that failed closes it ``failed`` and the
    answer is the failure's, so a new brief may start; any other answer that does
    not fit leaves it waiting (the decision stays recorded, and posting it again
    resumes again)."""
    state = _state_to_close_with(claim_id, body, run)
    if state is None:
        return DecisionFailure(502, RESUME_FAILED_DETAIL, body.run)
    with connect(dsn, SERVICE_NAME) as conn:
        row = conn.execute(
            CLOSE_BRIEF_SQL, (state, body.run, claim_id, tenant)
        ).fetchone()
    if row is None:
        return DecisionFailure(409, BRIEF_NOT_WAITING_DETAIL, body.run)
    if state == "failed":
        return DecisionFailure(502, RESUME_FAILED_DETAIL, body.run)
    return _view(row)


def _resume(
    http: httpx.Client, span: Span, tenant: str, claim_id: str, run_id: UUID
) -> RunResponse | DecisionFailure:
    """Resume the run. A resume the runtime answers with an error is a
    ``DecisionFailure`` (the run may resume again, and the brief waits), except
    that an error whose body says the run is ``Failed`` is the run's end: the
    answer is then a ``Failed`` run with no output, and ``_close_brief`` closes the
    brief as failed in this same request (a run that ended can never resume, and a
    brief that waited on it would refuse every later brief of the claim)."""
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
        if exc.run_status == "Failed" and exc.run_id == run_id:
            return RunResponse(run_id=run_id, status="Failed", output=None)
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
            recorded = _record_decision(dsn, tenant, claim_id, body)
        except psycopg.Error as exc:
            mark_error(span, exc)
            return DecisionFailure(*claim_database_failure(exc, claim_id))
        if isinstance(recorded, BriefView):
            # The claim closed after an approval was recorded and never carried
            # out: the brief is closed as rejected, and no run is resumed.
            return recorded
        # What the run reads, and so what closes the brief, is the word recorded:
        # a decision for a claim that has closed was recorded as a rejection.
        body = body.model_copy(update={"decision": recorded})
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
