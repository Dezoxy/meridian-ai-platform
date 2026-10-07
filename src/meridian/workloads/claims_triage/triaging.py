"""How the Claims API triages a claim (moved from ``app.py``, S048).

The request that moves a claim to ``triaging`` owns its triage: it posts the
run's input to the Agent Runtime, then closes the triage in one transaction,
storing the proposal and moving the claim on. A failure moves the claim to
``triage_failed`` and answers 502, 503 or 504 with the claim's ID (and the
run's, when there is one). The run input carries neither the claimant's name
nor e-mail address (S047).
"""

import logging
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from opentelemetry.trace import Span
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from meridian.platform.common.db import connect
from meridian.platform.common.http import database_failure, error_answer
from meridian.platform.common.telemetry import mark_error, set_span_attributes
from meridian.runtime.models import RunResponse, RunState
from meridian.workloads.claims_triage.claimant_name import description_for_run
from meridian.workloads.claims_triage.lifecycle import (
    AGENT as AGENT,
)
from meridian.workloads.claims_triage.lifecycle import (
    MAX_TRIAGES_PER_CLAIM,
    RULES_APPROVED,
    RULES_REFERRED,
    RULES_REQUESTED_DOCUMENTS,
    SERVICE_NAME,
    TRIAGE_FAILED,
    TRIAGE_LEASE_SECONDS,
    TRIAGE_RECLAIMED,
    TRIAGE_RETRIED,
    TRIAGE_STARTED,
    LifecycleState,
    Transition,
    move_claim,
)
from meridian.workloads.claims_triage.lifecycle import (
    RUNTIME_TIMEOUT_SECONDS as RUNTIME_TIMEOUT_SECONDS,
)
from meridian.workloads.claims_triage.meters import ClaimsMeters

# ``invalid_fields`` and ``DATA_KEY`` moved to ``models`` (S069) so that
# ``runtime_calls``, which this module imports, can log with them; this module
# still offers ``DATA_KEY``, as it offered it before.
from meridian.workloads.claims_triage.models import (
    DATA_KEY as DATA_KEY,
)
from meridian.workloads.claims_triage.models import (
    ClaimFacts,
    ClaimResponse,
    ClaimSubmission,
    DecisionFailure,
    ProposalSummary,
    Route,
    invalid_fields,
)
from meridian.workloads.claims_triage.posted_text import (
    POSTED_TEXT_FLAG,
    input_for_run,
    posted_text_addresses_the_model,
)
from meridian.workloads.claims_triage.proposal import TriageProposal

# The calls to the runtime moved to ``runtime_calls`` (S037); this module still
# offers each name it offered, for the modules and tests that import them here.
from meridian.workloads.claims_triage.runtime_calls import (
    END_RUN_TIMEOUT_SECONDS as END_RUN_TIMEOUT_SECONDS,
)
from meridian.workloads.claims_triage.runtime_calls import (
    HTTP_GATEWAY_TIMEOUT as HTTP_GATEWAY_TIMEOUT,
)
from meridian.workloads.claims_triage.runtime_calls import (
    RuntimeCallError as RuntimeCallError,
)
from meridian.workloads.claims_triage.runtime_calls import (
    end_run as end_run,
)
from meridian.workloads.claims_triage.runtime_calls import (
    resume_run as resume_run,
)
from meridian.workloads.claims_triage.runtime_calls import (
    runtime_timeout as runtime_timeout,
)
from meridian.workloads.claims_triage.runtime_calls import (
    start_run as start_run,
)

RUN_FAILED_DETAIL = "the triage run did not complete; the claim is stored"
RUN_TIMEOUT_DETAIL = "the triage run timed out; the claim is stored"
PROPOSAL_LOST_DETAIL = "the proposal could not be stored; the claim is stored"
HAS_PROPOSAL_DETAIL = "the claim already has a triage proposal"
BEING_TRIAGED_DETAIL = "the claim is being triaged"
TAKEN_OVER_DETAIL = "the triage was taken over by another request"
DIFFERENT_SUBMISSION_DETAIL = "the claim exists with a different submission"
# The cap's number in words, so the text follows the constant (T-38).
NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight")
TRIAGE_CAP_DETAIL = (
    f"the claim has been triaged {NUMBER_WORDS[MAX_TRIAGES_PER_CLAIM]} times; "
    "an adjuster decides it"
)
# The names of the documents that arrived for a claim after its submission, in
# the order they arrived (those of one arrival, by name).
ARRIVED_DOCUMENTS_SQL = (
    "SELECT name FROM claims.claim_documents WHERE claim_id = %s "
    "ORDER BY received_at, name"
)

# What a run's answer means for the claim: its status and the proposal's route.
RUN_OUTCOMES: Mapping[tuple[RunState, Route], Transition] = {
    ("Completed", "auto_approve"): RULES_APPROVED,
    ("Completed", "request_documents"): RULES_REQUESTED_DOCUMENTS,
    ("AwaitingApproval", "adjuster"): RULES_REFERRED,
}

logger = logging.getLogger(__name__)


def claim_database_failure(exc: psycopg.Error, claim_id: str) -> tuple[int, str]:
    """``database_failure`` for a failure that has a claim: the same answer, and
    one line more that names the claim with the error's class and SQLSTATE (the
    shared line cannot). Never the message, the SQL or its parameters."""
    logger.error(
        "database error on claim %s: %s (sqlstate %s)",
        claim_id,
        type(exc).__name__,
        exc.sqlstate or "none",
    )
    return database_failure(exc)


def _facts_of_submission(submission: ClaimSubmission) -> dict[str, Any]:
    """The facts that the submission alone decides: everything but the arrived
    documents. The description is the replaced copy, which compiles a pattern
    from the claimant's name (the slowest pure computation of the Claims API)."""
    # The runtime gets what the graph needs, not the claimant's name or email,
    # and a description with neither of them in it (S047).
    facts = submission.model_dump(mode="json", exclude={"claimant"})
    facts["description"] = description_for_run(
        submission.description, submission.claimant
    )
    return facts


def _facts_with_documents(
    submission: ClaimSubmission, facts: dict[str, Any], arrived: Sequence[str]
) -> dict[str, Any]:
    """``facts`` (a copy is returned; ``facts`` is not changed) with the
    documents the run is sent, which the submission's facts leave as they were
    stored. The documents are the submission's, then each name that arrived, each
    name once and in order of first appearance (S048): the submission's bound
    counts names and does not require them to differ, and the documents route
    checks the bound over the distinct names, so the list must be that set."""
    facts = {
        **facts,
        "documents": list(dict.fromkeys([*submission.documents, *arrived])),
    }
    # The graph validates these as ``ClaimFacts`` at every node. Facts that
    # would not validate fail the run as they always did; the log says why.
    try:
        ClaimFacts.model_validate(facts)
    except ValidationError as exc:
        logger.warning(
            "the facts of claim %s are not valid for the run: %s %s",
            submission.claim_id,
            type(exc).__name__,
            invalid_fields(exc),
        )
    return facts


def facts_for_run(
    submission: ClaimSubmission, arrived: Sequence[str] = ()
) -> dict[str, Any]:
    """The facts a triage run is sent: for every triage, whatever starts it. The
    documents are the submission's, then each name that arrived, each name once
    and in order of first appearance (S048)."""
    return _facts_with_documents(submission, _facts_of_submission(submission), arrived)


def triage_run_input(
    submission: ClaimSubmission, arrived: Sequence[str] = ()
) -> dict[str, Any]:
    """The whole input of a triage run, built here and nowhere else: the claim's
    facts (``facts_for_run``) and, beside them, the screen of the description as
    posted (``posted_text.input_for_run``, S067). ``start_run`` sends it as it
    is, so the claim is under ``claim`` once. The moves that hold a claim's row
    build the same input in two parts (``prepare_run_input`` and
    ``run_input_with_documents``); a test holds the two to the same bytes."""
    return input_for_run(submission, facts_for_run(submission, arrived))


class PreparedRunInput(NamedTuple):
    """What a run is sent that the stored submission alone decides (S070): the
    submission, its facts without the arrived documents (the description already
    replaced) and the screen of the description as posted. A stored submission
    never changes, so this can be built before a claim's row is locked."""

    submission: ClaimSubmission
    facts: dict[str, Any]
    posted_text_flag: bool


def prepare_run_input(submission: ClaimSubmission) -> PreparedRunInput:
    """The first part of ``triage_run_input``: everything that needs only the
    submission, including the pattern built from the claimant's name. Called
    before the claim's row is locked."""
    return PreparedRunInput(
        submission,
        _facts_of_submission(submission),
        posted_text_addresses_the_model(submission.description),
    )


def run_input_with_documents(
    prepared: PreparedRunInput, arrived: Sequence[str] = ()
) -> dict[str, Any]:
    """The second part of ``triage_run_input``: the documents that arrived, read
    where the caller holds the claim's row, and the input a run is sent. The same
    bytes as ``triage_run_input(prepared.submission, arrived)``; ``prepared`` is
    not changed, so one may be finished for different documents."""
    return {
        "claim": _facts_with_documents(prepared.submission, prepared.facts, arrived),
        POSTED_TEXT_FLAG: prepared.posted_text_flag,
    }


def arrived_documents(conn: psycopg.Connection, claim_id: str) -> tuple[str, ...]:
    """The names of the documents that arrived for the claim, on the caller's
    connection (so inside its transaction)."""
    rows = conn.execute(ARRIVED_DOCUMENTS_SQL, (claim_id,)).fetchall()
    return tuple(name for (name,) in rows)


# When the claim moved to its state and whether that is longer ago than the
# triage lease (the same test ``take_triage`` makes in its one ``SELECT``); read
# by the triage-again route, which has locked the claim without its age.
TRIAGE_AGE_SQL = (
    "SELECT state_changed_at, "
    "state_changed_at < clock_timestamp() - make_interval(secs => %s) "
    "FROM claims.claims WHERE claim_id = %s AND tenant = %s"
)


def triage_outcome(run: RunResponse) -> tuple[TriageProposal, Transition]:
    """The run's proposal and the claim's move it leads to; anything else the
    runtime answered is outside the contract."""
    try:
        proposal = TriageProposal.model_validate(run.output)
    except ValidationError as exc:
        # The fields and their error types, never a value: the output carries
        # the model's rationale and a clause's text.
        logger.warning(
            "the runtime's output of run %s is not a triage proposal: %s %s",
            run.run_id,
            type(exc).__name__,
            invalid_fields(exc),
        )
        raise RuntimeCallError(
            "the runtime's output is not a triage proposal",
            run_id=run.run_id,
            failure="bad-output",
        ) from None
    transition = RUN_OUTCOMES.get((run.status, proposal.route))
    if transition is None:
        raise RuntimeCallError(
            "the runtime's answer does not fit its proposal",
            run_id=run.run_id,
            failure="bad-output",
        )
    return proposal, transition


def store_claim(
    dsn: str,
    tenant: str,
    claim: dict[str, Any],
    *,
    stamped: Collection[str] = (),
) -> dict[str, Any]:
    """Store the claim and return the submission as it is stored. An existing
    one under this ID is left as it is; it is a 409 unless it is the same
    submission in every key not in ``stamped``, and then the stored one is
    returned: the keys the API stamped (the report date of the claimant's
    pages) keep their first value, so a form sent again later is the same
    submission."""
    with connect(dsn, SERVICE_NAME) as conn:
        cursor = conn.execute(
            "INSERT INTO claims.claims (claim_id, tenant, submission) "
            "VALUES (%s, %s, %s) ON CONFLICT (claim_id) DO NOTHING",
            (claim["claim_id"], tenant, Jsonb(claim)),
        )
        if cursor.rowcount == 1:
            return claim
        row = conn.execute(
            "SELECT submission, tenant FROM claims.claims WHERE claim_id = %s",
            (claim["claim_id"],),
        ).fetchone()
    # A claim of another tenant answers as a different submission does, so no
    # answer tells a caller that another tenant has the ID.
    if row is None or row[1] != tenant or not _same_but(row[0], claim, stamped):
        raise HTTPException(409, DIFFERENT_SUBMISSION_DETAIL)
    stored: dict[str, Any] = row[0]
    return stored


def _same_but(
    stored: Mapping[str, Any], claim: Mapping[str, Any], stamped: Collection[str]
) -> bool:
    """Whether the two submissions are equal in every key not in ``stamped``."""
    return {k: v for k, v in stored.items() if k not in stamped} == {
        k: v for k, v in claim.items() if k not in stamped
    }


class LapsedTriage(NamedTuple):
    """A triage taken over: the moment the claim moved, which the request that
    took it keeps (its closing update matches on it), and the run it must end
    after the commit, if the claim held one."""

    taken_at: datetime
    old_run: UUID | None


def take_over_lapsed_triage(
    conn: psycopg.Connection,
    *,
    claim_id: str,
    tenant: str,
    changed_at: datetime,
    triages: int,
    run_id: UUID | None,
) -> LapsedTriage | None:
    """Take over a triage whose lease lapsed, in the caller's transaction (the
    claim locked, found ``triaging`` since ``changed_at`` and holding ``run_id``).

    A ``triaging`` claim holds a run only when it was sent back (every other
    move into ``triaging`` passes none), and the send-back recorded that run's
    ``send_back`` word with the move. Its own request died before it ended the
    run or could not, so the takeover returns it, and the caller ends it, best
    effort, after the commit (``end_run``). The move drops the claim's run.

    A triage that died at the cap cannot be taken over (``move_claim`` refuses a
    move into triaging there), so the claim is moved to ``triage_failed`` instead
    and ``None`` answered: the caller commits that and then refuses with 409, so
    that an adjuster decides a claim whose triage failed. ``None`` is also the
    answer for a claim that did not move (not expected under the lock).
    """
    if triages >= MAX_TRIAGES_PER_CLAIM:
        move_claim(
            conn,
            TRIAGE_FAILED,
            claim_id=claim_id,
            tenant=tenant,
            changed_at=changed_at,
        )
        return None
    taken_at = move_claim(
        conn,
        TRIAGE_RECLAIMED,
        claim_id=claim_id,
        tenant=tenant,
        changed_at=changed_at,
    )
    return None if taken_at is None else LapsedTriage(taken_at, run_id)


def take_triage(
    dsn: str, tenant: str, claim_id: str
) -> tuple[datetime | None, LifecycleState, tuple[str, ...], UUID | None]:
    """Move the claim to ``triaging`` if it may be triaged now.

    Returns the moment it moved (the request keeps it: its closing update
    matches on it) or ``None``, the state the claim was found in, the names
    of the documents that arrived for it, read in the same transaction after the
    move (none for a claim that was never waiting for any), and the run to end
    after the commit: the one a send-back left on a triage that lapsed (see
    ``take_over_lapsed_triage``), otherwise ``None``. A claim that has
    been triaged ``MAX_TRIAGES_PER_CLAIM`` times is refused with 409.
    """
    with connect(dsn, SERVICE_NAME) as conn:
        row = conn.execute(
            "SELECT state, state_changed_at, "
            "state_changed_at < clock_timestamp() - make_interval(secs => %s), "
            "triages, run_id "
            "FROM claims.claims WHERE claim_id = %s AND tenant = %s "
            "FOR NO KEY UPDATE",
            (TRIAGE_LEASE_SECONDS, claim_id, tenant),
        ).fetchone()
        if row is None:
            # Not this tenant's (``store_claim`` refuses that first) or gone.
            raise HTTPException(409, DIFFERENT_SUBMISSION_DETAIL)
        state, changed_at, lapsed, triages, run_id = row
        at_cap = triages >= MAX_TRIAGES_PER_CLAIM
        old_run: UUID | None = None
        if state == "triaging" and lapsed:
            # Committed with the 409 when the claim is at the cap (see the helper).
            taken = take_over_lapsed_triage(
                conn,
                claim_id=claim_id,
                tenant=tenant,
                changed_at=changed_at,
                triages=triages,
                run_id=run_id,
            )
            moved_at, old_run = taken or (None, None)
        elif state in ("submitted", "triage_failed"):
            moved_at = (
                None
                if at_cap
                else move_claim(
                    conn,
                    TRIAGE_STARTED if state == "submitted" else TRIAGE_RETRIED,
                    claim_id=claim_id,
                    tenant=tenant,
                    changed_at=changed_at,
                )
            )
        else:
            return None, state, (), None
        arrived = () if moved_at is None else arrived_documents(conn, claim_id)
    if at_cap:
        raise HTTPException(409, TRIAGE_CAP_DETAIL)
    return moved_at, state, arrived, old_run


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


def close_triage(
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


def fail_triage(
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


def answer(
    status: int, detail: str, claim_id: str, run_id: UUID | None = None
) -> JSONResponse:
    extra = {} if run_id is None else {"run_id": str(run_id)}
    return error_answer(status, detail, claim_id=claim_id, **extra)


def triage_claim(
    dsn: str,
    tenant: str,
    http: httpx.Client,
    span: Span,
    claim_id: str,
    submission: ClaimSubmission,
    meters: ClaimsMeters | None = None,
) -> ClaimResponse | JSONResponse:
    """Take the claim's triage, run it and close it. ``span`` is the caller's
    open span; the run's ID is set on it. A refusal (409) is raised as
    ``HTTPException``; a failure is answered with the claim's ID. The run is
    sent the submission's facts and the documents that arrived for the claim, and
    the screen of its description as posted (``triage_run_input``).
    ``meters`` counts the proposal when it is stored (see ``run_taken_triage``)."""
    try:
        taken_at, found_in, arrived, old_run = take_triage(dsn, tenant, claim_id)
    except psycopg.Error as exc:
        mark_error(span, exc)
        return answer(*claim_database_failure(exc, claim_id), claim_id)
    if taken_at is None:
        raise HTTPException(
            409,
            BEING_TRIAGED_DETAIL if found_in == "triaging" else HAS_PROPOSAL_DETAIL,
        )
    if old_run is not None:
        end_run(http, tenant, claim_id, old_run)
    result = run_taken_triage(
        dsn,
        tenant,
        http,
        span,
        claim_id,
        triage_run_input(submission, arrived),
        taken_at,
        meters=meters,
    )
    if isinstance(result, DecisionFailure):
        return answer(result.status, result.detail, claim_id, result.run_id)
    return result


def run_taken_triage(
    dsn: str,
    tenant: str,
    http: httpx.Client,
    span: Span,
    claim_id: str,
    run_input: dict[str, Any],
    taken_at: datetime,
    *,
    meters: ClaimsMeters | None = None,
) -> ClaimResponse | DecisionFailure:
    """Run the triage this request took and close it: start the run, read its
    outcome, store the proposal and move the claim on, or move it to
    ``triage_failed``. A failure is a ``DecisionFailure`` (a status, a fixed
    text and the run's ID when there is one); a triage taken over by another
    request is a 409 raised as ``HTTPException``. ``span`` is the caller's open
    span; the run's ID is set on it. Every triage passes here, whichever route
    took it, so this is where ``meters`` counts it, once, by how it ended (the
    words are in ``meters.py``); a failure no branch expected is counted
    ``unexpected`` and raised, the 409 excepted: it is counted where it is raised.
    A proposal is counted ``stored`` when it is stored (``_run_triage``); the
    answer is built after that and outside the count, so an answer that cannot
    be built is raised (a 500) and is not a second count for a triage that
    stored its proposal."""
    try:
        outcome = _run_triage(
            dsn, tenant, http, span, claim_id, run_input, taken_at, meters
        )
    except Exception as exc:
        if meters is not None and not isinstance(exc, HTTPException):
            meters.triage_failed("unexpected")
        raise
    if isinstance(outcome, DecisionFailure):
        return outcome
    run, proposal, transition = outcome
    return ClaimResponse(
        claim_id=claim_id,
        state=transition.target,
        run_id=run.run_id,
        run_status=run.status,
        proposal=ProposalSummary(route=proposal.route, drafted_by=proposal.drafted_by),
    )


class _Stored(NamedTuple):
    """A triage that stored its proposal, already counted: what the answer is
    built from."""

    run: RunResponse
    proposal: TriageProposal
    transition: Transition


def _run_triage(
    dsn: str,
    tenant: str,
    http: httpx.Client,
    span: Span,
    claim_id: str,
    run_input: dict[str, Any],
    taken_at: datetime,
    meters: ClaimsMeters | None,
) -> _Stored | DecisionFailure:
    try:
        run = start_run(http, tenant, claim_id, run_input)
        proposal, transition = triage_outcome(run)
    except RuntimeCallError as exc:
        logger.error(
            "triage of %s failed: %s (runtime status %s, run %s)",
            claim_id,
            type(exc).__name__,
            exc.status_code,
            exc.run_id,
        )
        mark_error(span, exc)
        fail_triage(dsn, tenant, claim_id, exc.run_id, taken_at)
        if meters is not None:
            meters.triage_failed(exc.failure)
        return DecisionFailure(
            HTTP_GATEWAY_TIMEOUT if exc.timed_out else 502,
            RUN_TIMEOUT_DETAIL if exc.timed_out else RUN_FAILED_DETAIL,
            exc.run_id,
        )
    set_span_attributes(span, {"meridian.run_id": str(run.run_id)})
    try:
        closed = close_triage(
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
        fail_triage(dsn, tenant, claim_id, run.run_id, taken_at)
        if meters is not None:
            meters.triage_failed("proposal-lost")
        return DecisionFailure(503, PROPOSAL_LOST_DETAIL, run.run_id)
    if not closed:
        logger.warning(
            "triage of %s (run %s) was taken over; its proposal is dropped",
            claim_id,
            run.run_id,
        )
        if meters is not None:
            meters.triage_taken_over()
        raise HTTPException(409, TAKEN_OVER_DETAIL)
    if meters is not None:
        meters.proposal_stored(proposal)
    return _Stored(run, proposal, transition)
