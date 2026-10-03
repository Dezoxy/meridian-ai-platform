"""The routes that move a claim a person's way (S048): triage again, withdrawal
and the arrival of documents.

Each is one transaction with the claim locked (``FOR NO KEY UPDATE``, filtered
by tenant): a compare-and-set from the states the lifecycle allows, audited in
the same transaction (T-74). What happens after the commit is best effort or
the triage itself: a paused run is ended through ``end_run`` (the claim's move
stands if that fails), and a triage runs through ``run_taken_triage``, the code
``POST /claims`` runs. Each function returns ``ClaimMoveResponse`` or a
``DecisionFailure`` (a status and a fixed text, which the JSON route answers and
the adjuster's page renders) and raises ``HTTPException`` for 404 and 409.
No document name, description or claimant field reaches a log line or a span
attribute (T-03).
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

import httpx
import psycopg
from fastapi import HTTPException
from opentelemetry.trace import Tracer
from pydantic import ValidationError

from meridian.platform.common.db import connect
from meridian.platform.common.http import INTERNAL_ERROR, database_failure
from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.workloads.claims_triage.adjuster import NO_SUCH_CLAIM_DETAIL
from meridian.workloads.claims_triage.lifecycle import (
    ADJUSTER_SENT_BACK,
    CLAIMANT_WITHDREW_DOCUMENTS,
    CLAIMANT_WITHDREW_WAITING,
    DOCUMENTS_ARRIVED,
    DOCUMENTS_AT_CAP,
    MAX_TRIAGES_PER_CLAIM,
    SERVICE_NAME,
    TRIAGE_RETRIED,
    LifecycleState,
    Transition,
    move_claim,
)
from meridian.workloads.claims_triage.models import (
    MAX_DOCUMENTS,
    ClaimMoveResponse,
    ClaimResponse,
    ClaimSubmission,
    DecisionFailure,
    Outcome,
)
from meridian.workloads.claims_triage.triaging import (
    BEING_TRIAGED_DETAIL,
    TRIAGE_AGE_SQL,
    TRIAGE_CAP_DETAIL,
    TRIAGE_LEASE_SECONDS,
    arrived_documents,
    end_run,
    facts_for_run,
    run_taken_triage,
    take_over_lapsed_triage,
)

logger = logging.getLogger(__name__)

STALE_PAGE_DETAIL = "the claim changed after the page was read; read it again"
NOT_TRIAGEABLE_DETAIL = "the claim cannot be triaged again in its state"
NOT_WITHDRAWABLE_DETAIL = "the claim cannot be withdrawn in its state"
NOT_AWAITING_DOCUMENTS_DETAIL = "the claim does not wait for documents"
TOO_MANY_DOCUMENTS_DETAIL = f"the claim would hold more than {MAX_DOCUMENTS} documents"

LOCK_CLAIM_SQL = (
    "SELECT state, run_id, triages FROM claims.claims "
    "WHERE claim_id = %s AND tenant = %s FOR NO KEY UPDATE"
)
SUBMISSION_SQL = "SELECT submission FROM claims.claims WHERE claim_id = %s"
# One row per run, so an outcome is recorded once for it; ``run_id`` is NULL only
# for the adjuster's decision on a claim that has no paused run.
RECORD_OUTCOME_SQL = (
    "INSERT INTO claims.decisions (claim_id, run_id, decision) VALUES (%s, %s, %s)"
)
RECORD_DOCUMENT_SQL = (
    "INSERT INTO claims.claim_documents (claim_id, name) VALUES (%s, %s) "
    "ON CONFLICT DO NOTHING"
)
TRIAGEABLE_AGAIN: tuple[LifecycleState, ...] = ("awaiting_adjuster", "triage_failed")


class _Taken(NamedTuple):
    """A triage this request took: the run it ends first (a paused one), the
    moment the claim moved to ``triaging`` and the facts to run it with."""

    old_run: UUID | None
    taken_at: datetime
    facts: dict[str, Any]


def _lock_claim(
    conn: psycopg.Connection, tenant: str, claim_id: str
) -> tuple[LifecycleState, UUID | None, int]:
    """The tenant's claim, locked: its state, run and the triages it has had."""
    row = conn.execute(LOCK_CLAIM_SQL, (claim_id, tenant)).fetchone()
    if row is None:
        raise HTTPException(404, NO_SUCH_CLAIM_DETAIL)
    state, run_id, triages = row
    return state, run_id, triages


class StoredSubmissionInvalid(Exception):
    """The submission a claim holds no longer validates (a row written by hand or
    by an older version). The message is fixed text: pydantic's quotes the
    stored values, which are the claimant's."""


def _submission(conn: psycopg.Connection, claim_id: str) -> ClaimSubmission:
    ((stored,),) = conn.execute(SUBMISSION_SQL, (claim_id,)).fetchall()
    try:
        return ClaimSubmission.model_validate(stored)
    except ValidationError as exc:
        logger.error(
            "the stored submission of claim %s is not valid: %s",
            claim_id,
            type(exc).__name__,
        )
        raise StoredSubmissionInvalid("the stored submission is not valid") from None


def _move_answer(
    result: ClaimResponse | DecisionFailure,
) -> ClaimMoveResponse | DecisionFailure:
    """A triage's answer as the moves' answer."""
    if isinstance(result, DecisionFailure):
        return result
    return ClaimMoveResponse(
        claim_id=result.claim_id,
        state=result.state,
        run_id=result.run_id,
        run_status=result.run_status,
        proposal=result.proposal,
    )


def refuse_stale_page(page_run: str | None, run_id: UUID | None) -> None:
    """A page read before the claim changed cannot decide or move a run nobody
    read (T-33). ``page_run`` is the run the page showed, empty for a claim with
    no run; ``None`` is a caller with no page (the JSON routes), which is not
    checked. Called with the claim locked; the refusal is a 409 and nothing has
    been written."""
    if page_run is not None and page_run != ("" if run_id is None else str(run_id)):
        raise HTTPException(409, STALE_PAGE_DETAIL)


def _refuse_move(moved_at: datetime | None, detail: str) -> datetime:
    """The moment a claim moved. A claim that did not move under this request's
    lock (not expected) is a 409 and the transaction rolls back."""
    if moved_at is None:
        raise HTTPException(409, detail)
    return moved_at


# ── triage again ────────────────────────────────────────────────────────────
def _take_from_state(
    conn: psycopg.Connection,
    tenant: str,
    claim_id: str,
    state: LifecycleState,
    run_id: UUID | None,
    triages: int,
) -> _Taken:
    """Take the triage of a claim that is not ``triaging``: sent back from
    ``awaiting_adjuster`` or tried again from ``triage_failed``."""
    if state not in TRIAGEABLE_AGAIN:
        raise HTTPException(409, NOT_TRIAGEABLE_DETAIL)
    if triages >= MAX_TRIAGES_PER_CLAIM:
        raise HTTPException(409, TRIAGE_CAP_DETAIL)
    old_run: UUID | None = None
    transition: Transition = TRIAGE_RETRIED
    if state == "awaiting_adjuster":
        transition = ADJUSTER_SENT_BACK
        if run_id is not None:
            # The word that ends the paused run, recorded with the move.
            word: Outcome = "send_back"
            conn.execute(RECORD_OUTCOME_SQL, (claim_id, run_id, word))
            old_run = run_id
    # A send-back's event names the run it ends, and the claim holds it while it
    # is triaging; ``close_triage`` or ``fail_triage`` replaces it. A retry has
    # no run to name (a failed run is not a paused one).
    taken_at = _refuse_move(
        move_claim(conn, transition, claim_id=claim_id, tenant=tenant, run_id=old_run),
        NOT_TRIAGEABLE_DETAIL,
    )
    submission = _submission(conn, claim_id)
    facts = facts_for_run(submission, arrived_documents(conn, claim_id))
    return _Taken(old_run, taken_at, facts)


def _take_over(
    conn: psycopg.Connection,
    tenant: str,
    claim_id: str,
    run_id: UUID | None,
    triages: int,
) -> _Taken | str:
    """Take a ``triaging`` claim's triage over if its lease lapsed, exactly as
    ``POST /claims`` does; otherwise, or at the cap (where the claim was moved to
    ``triage_failed``, to be committed), the detail of the 409 that refuses it.
    The run the claim holds is the send-back's, whose request died before it
    ended it or could not: it is returned to be ended after the commit."""
    row = conn.execute(
        TRIAGE_AGE_SQL, (TRIAGE_LEASE_SECONDS, claim_id, tenant)
    ).fetchone()
    if row is None or not row[1]:
        return BEING_TRIAGED_DETAIL
    lapsed = take_over_lapsed_triage(
        conn,
        claim_id=claim_id,
        tenant=tenant,
        changed_at=row[0],
        triages=triages,
        run_id=run_id,
    )
    if lapsed is None:
        return (
            TRIAGE_CAP_DETAIL
            if triages >= MAX_TRIAGES_PER_CLAIM
            else BEING_TRIAGED_DETAIL
        )
    submission = _submission(conn, claim_id)
    facts = facts_for_run(submission, arrived_documents(conn, claim_id))
    return _Taken(lapsed.old_run, lapsed.taken_at, facts)


def _take_again(dsn: str, tenant: str, claim_id: str, page_run: str | None) -> _Taken:
    with connect(dsn, SERVICE_NAME) as conn:
        state, run_id, triages = _lock_claim(conn, tenant, claim_id)
        refuse_stale_page(page_run, run_id)
        taken = (
            _take_over(conn, tenant, claim_id, run_id, triages)
            if state == "triaging"
            else _take_from_state(conn, tenant, claim_id, state, run_id, triages)
        )
    # Raised after the block, which commits: a triage that died at the cap is
    # failed for good, whatever the answer.
    if isinstance(taken, str):
        raise HTTPException(409, taken)
    return taken


def triage_again(
    dsn: str,
    tenant: str,
    http: httpx.Client,
    tracer: Tracer,
    claim_id: str,
    *,
    page_run: str | None = None,
) -> ClaimMoveResponse | DecisionFailure:
    """Triage the claim again: a claim waiting for an adjuster is sent back (its
    paused run is ended), a claim whose triage failed is tried again. The
    adjuster's page passes the run it showed as ``page_run``: a claim that has
    changed run since is a 409 and nothing moves (see ``refuse_stale_page``)."""
    with start_span(tracer, "claims.triage_again") as span:
        set_span_attributes(
            span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
        )
        try:
            taken = _take_again(dsn, tenant, claim_id, page_run)
        except psycopg.Error as exc:
            mark_error(span, exc)
            return DecisionFailure(*database_failure(exc))
        except StoredSubmissionInvalid as exc:
            mark_error(span, exc)
            return DecisionFailure(500, INTERNAL_ERROR)
        if taken.old_run is not None:
            end_run(http, tenant, claim_id, taken.old_run)
        return _move_answer(
            run_taken_triage(
                dsn, tenant, http, span, claim_id, taken.facts, taken.taken_at
            )
        )


# ── withdrawal ──────────────────────────────────────────────────────────────
def _record_withdrawal(dsn: str, tenant: str, claim_id: str) -> UUID | None:
    """Withdraw the claim; the run to end after the commit, if any. A claim
    already withdrawn moves nowhere, and its run is ended again: withdrawing
    ends the claim's run from either state, and the claim keeps it."""
    with connect(dsn, SERVICE_NAME) as conn:
        state, run_id, _ = _lock_claim(conn, tenant, claim_id)
        if state == "awaiting_adjuster":
            if run_id is not None:
                word: Outcome = "withdrawn"
                conn.execute(RECORD_OUTCOME_SQL, (claim_id, run_id, word))
            _refuse_move(
                move_claim(
                    conn,
                    CLAIMANT_WITHDREW_WAITING,
                    claim_id=claim_id,
                    tenant=tenant,
                    run_id=run_id,
                ),
                NOT_WITHDRAWABLE_DETAIL,
            )
            return run_id
        if state == "documents_requested":
            # Its run may still be paused: an adjuster's request for documents
            # whose resume failed records the decision and moves the claim, and
            # leaves the run. No word is recorded here (the run has one, and its
            # row is unique): the run reads that one and completes with its note,
            # the adjuster's decision being applied. A run that ended already
            # (the rules asked for the documents) answers its status and runs
            # nothing when resumed. The claim keeps the run.
            _refuse_move(
                move_claim(
                    conn,
                    CLAIMANT_WITHDREW_DOCUMENTS,
                    claim_id=claim_id,
                    tenant=tenant,
                    run_id=run_id,
                ),
                NOT_WITHDRAWABLE_DETAIL,
            )
            return run_id
        if state == "withdrawn":
            return run_id
        raise HTTPException(409, NOT_WITHDRAWABLE_DETAIL)


def withdraw(
    dsn: str, tenant: str, http: httpx.Client, tracer: Tracer, claim_id: str
) -> ClaimMoveResponse | DecisionFailure:
    """Withdraw the claim, from ``awaiting_adjuster`` or ``documents_requested``.
    Idempotent: a withdrawal posted again answers the same and ends the run
    again (a resume of an ended run answers its status and runs nothing)."""
    with start_span(tracer, "claims.withdraw") as span:
        set_span_attributes(
            span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
        )
        try:
            to_end = _record_withdrawal(dsn, tenant, claim_id)
        except psycopg.Error as exc:
            mark_error(span, exc)
            return DecisionFailure(*database_failure(exc))
        run_status = None
        if to_end is not None:
            set_span_attributes(span, {"meridian.run_id": str(to_end)})
            run_status = end_run(http, tenant, claim_id, to_end)
        return ClaimMoveResponse(
            claim_id=claim_id, state="withdrawn", run_id=to_end, run_status=run_status
        )


# ── documents ───────────────────────────────────────────────────────────────
class _Arrival(NamedTuple):
    """Documents that arrived: the run the claim held (to end after the commit)
    and, unless the claim was at the cap, the triage this request took."""

    old_run: UUID | None
    taken: tuple[datetime, dict[str, Any]] | None


def _store_arrival(
    dsn: str, tenant: str, claim_id: str, documents: Sequence[str]
) -> _Arrival:
    """Store the names and move the claim: to ``triaging`` with the moment it
    moved and the facts to triage it with, or, when it has been triaged as often
    as the cap allows, to ``awaiting_adjuster`` (no triage). Either move drops the
    claim's run, so the run it held is returned: it may still be paused (an
    adjuster's request for documents whose resume failed)."""
    with connect(dsn, SERVICE_NAME) as conn:
        state, old_run, triages = _lock_claim(conn, tenant, claim_id)
        if state != "documents_requested":
            raise HTTPException(409, NOT_AWAITING_DOCUMENTS_DETAIL)
        submission = _submission(conn, claim_id)
        held = {*submission.documents, *arrived_documents(conn, claim_id)}
        if len(held | set(documents)) > MAX_DOCUMENTS:
            raise HTTPException(409, TOO_MANY_DOCUMENTS_DETAIL)
        for name in documents:
            conn.execute(RECORD_DOCUMENT_SQL, (claim_id, name))
        if triages >= MAX_TRIAGES_PER_CLAIM:
            _refuse_move(
                move_claim(conn, DOCUMENTS_AT_CAP, claim_id=claim_id, tenant=tenant),
                NOT_AWAITING_DOCUMENTS_DETAIL,
            )
            return _Arrival(old_run, None)
        taken_at = _refuse_move(
            move_claim(conn, DOCUMENTS_ARRIVED, claim_id=claim_id, tenant=tenant),
            NOT_AWAITING_DOCUMENTS_DETAIL,
        )
        facts = facts_for_run(submission, arrived_documents(conn, claim_id))
        return _Arrival(old_run, (taken_at, facts))


def add_documents(
    dsn: str,
    tenant: str,
    http: httpx.Client,
    tracer: Tracer,
    claim_id: str,
    documents: Sequence[str],
) -> ClaimMoveResponse | DecisionFailure:
    """Take the names of documents that arrived for a claim that waits for them
    and triage it with all it holds. At the triage cap the names are stored and
    the claim is referred to an adjuster, with no run. The run the claim held is
    ended first, best effort, as for a send-back: a resume of a run that has
    ended answers its status and runs nothing, and one still paused reads the
    recorded ``request_documents`` and completes with its note."""
    with start_span(tracer, "claims.documents") as span:
        set_span_attributes(
            span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
        )
        try:
            arrival = _store_arrival(dsn, tenant, claim_id, documents)
        except psycopg.Error as exc:
            mark_error(span, exc)
            return DecisionFailure(*database_failure(exc))
        except StoredSubmissionInvalid as exc:
            mark_error(span, exc)
            return DecisionFailure(500, INTERNAL_ERROR)
        if arrival.old_run is not None:
            end_run(http, tenant, claim_id, arrival.old_run)
        if arrival.taken is None:
            return ClaimMoveResponse(claim_id=claim_id, state="awaiting_adjuster")
        taken_at, facts = arrival.taken
        return _move_answer(
            run_taken_triage(dsn, tenant, http, span, claim_id, facts, taken_at)
        )
