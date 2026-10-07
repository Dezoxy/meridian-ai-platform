"""The adjuster's pages of the Claims API (S016): the queue of the claims that
wait for a person, one claim with its proposal and audit trail, and the form
that records a decision. One JSON route, ``GET /adjuster/claims/{claim_id}/proposal``,
answers the stored proposal for ``meridian eval run`` (S050, T-80).

The pages are server-rendered with Jinja2 (autoescape on, no ``|safe``), carry
no script and one stylesheet from the app itself, and are out of the OpenAPI
contract. A decision from the form runs ``decide``, the function behind
``POST /claims/{claim_id}/decision``: record, move, audit, resume. A refusal or
a failed resume that the claim's own routes give (403, 404 for a claim, 409,
502, 503, 504) comes back as a status and a fixed text, which a page shows. The
shared JSON answers stay JSON, with the security headers: 413 (body too large),
422 (a claim ID or a decision that is not one), 404 for a path under
``/adjuster/`` that is no route and 405. The form's route refuses a post
another site made (T-70). The decision and triage forms each carry the claim's
run as the page saw it (a hidden ``run``, empty for a claim with no run), and
a post of a page read before the claim moved to another run is a 409 (T-33).
Nothing here logs or puts on a span anything of the
claim but its ID (T-03). There is no sign-in yet: anyone who reaches the pages
can decide (T-69, S021).
"""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path as FilePath
from typing import Annotated, NamedTuple
from urllib.parse import urlencode
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Form, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from opentelemetry.trace import Tracer
from pydantic import AwareDatetime

from meridian.platform.common.audit import AuditUnavailable
from meridian.platform.common.db import connect
from meridian.platform.common.http import (
    AUDIT_UNAVAILABLE,
    database_failure,
    error_answer,
)
from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.runtime.sweep import ABANDONED_REASON
from meridian.workloads.claims_triage.adjuster_queue import (
    QUEUE_LIMIT,
    UNREADABLE_PROPOSAL_MARK,
    QueueCursor,
    QueueRow,
    _proposal_of,
    _queue_mark,
    load_queue,
)
from meridian.workloads.claims_triage.claim_dates import (
    LOSS_DATE_LABEL,
    REPORTED_ON_LABEL,
    day_gaps,
)
from meridian.workloads.claims_triage.claim_files import (
    DOWNLOAD_EVENT,
    FileSummary,
    list_files,
    size_text,
)
from meridian.workloads.claims_triage.lifecycle import (
    DOCUMENTS_OVERDUE,
    DOCUMENTS_REFUSED_EVENT,
    MAX_TRIAGES_PER_CLAIM,
    SERVICE_NAME,
)
from meridian.workloads.claims_triage.models import (
    ClaimMoveResponse,
    Decision,
    DecisionFailure,
    DecisionResponse,
)
from meridian.workloads.claims_triage.page_security import (
    CLAIMANT_PREFIX,
    CROSS_SITE_DETAIL,
    STYLESHEET_PATH,
    CrossSiteRefused,
    SecurityHeadersMiddleware,
    require_same_origin,
)
from meridian.workloads.claims_triage.proposal import (
    RESTS_ON_NOTES,
    TriageProposal,
    recommendation_rests_on,
)
from meridian.workloads.claims_triage.triaging import (
    NUMBER_WORDS,
    arrived_documents,
    claim_database_failure,
)

logger = logging.getLogger(__name__)

PACKAGE_DIR = FilePath(__file__).parent
QUEUE_PATH = "/adjuster/claims"
NO_SUCH_CLAIM_DETAIL = "no such claim"
ARRIVED_LABEL = "Documents that arrived later"
TRAIL_LIMIT = 200
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_SEE_OTHER = 303
HTTP_UNAVAILABLE = 503
# What a refused post or a failed resume leaves the claim page to show: the
# claim exists, so its page is rendered with the answer's status and text.
PAGE_STATUSES = (409, 502, 504)
# A failed resume leaves the decision recorded; sending it again completes the
# run. A 409 is a refusal or another request at work: nothing to send again.
RESEND_STATUSES = (502, 504)
CLAIM_ID_PATTERN = r"^CLM-[0-9]{4}$"

ClaimId = Annotated[str, Path(pattern=CLAIM_ID_PATTERN)]
# Each takes the claim, the move and the run the page showed (empty for a claim
# with no run): a claim that has another run now is refused (T-33).
DecideFn = Callable[[str, Decision, str], DecisionResponse | DecisionFailure]
TriageAgainFn = Callable[[str, str], ClaimMoveResponse | DecisionFailure]

TEMPLATES = Environment(
    loader=FileSystemLoader(PACKAGE_DIR / "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    auto_reload=False,
)

CLAIM_SQL = (
    "SELECT state, state_changed_at, received_at, submission, run_id, triages "
    "FROM claims.claims WHERE claim_id = %s AND tenant = %s"
)
PROPOSAL_SQL = (
    "SELECT proposal FROM claims.triage_proposals "
    "WHERE claim_id = %s ORDER BY created_at DESC, proposal_id LIMIT 1"
)
STATE_SQL = "SELECT state FROM claims.claims WHERE claim_id = %s AND tenant = %s"
# The decision that moved the claim into its state: a word of the three, for
# the claim's own run (``IS NOT DISTINCT FROM``: a decision with no run belongs
# to a claim with no run). ``send_back`` and ``withdrawn`` end a run; they are
# moves, which the trail shows, and never a decision.
DECISION_SQL = (
    "SELECT decision, decided_at FROM claims.decisions "
    "WHERE claim_id = %s AND run_id IS NOT DISTINCT FROM %s::uuid "
    "AND decision IN ('approve', 'reject', 'request_documents') "
    "ORDER BY decided_at DESC LIMIT 1"
)
# The states a decision leads to; in any other the page shows no decision.
DECIDED_STATES = ("approved", "rejected", "documents_requested")
# audit.claim_trail is the one thing of the audit log the Claims API may read
# (migration 0011, T-71); the tenant filter is the page's own as well. Rows of
# one transaction share a recorded_at; seq (migration 0017) is the order the
# database inserted them in, so it breaks the tie. It is the order of insertion
# and not of commit: across concurrent transactions recorded_at comes first.
# The page lists the NEWEST TRAIL_LIMIT rows (selected newest first, one more
# than the limit to know whether older rows exist, then put in time order), and
# leaves out the downloads of files: an anonymous GET writes one row each, and
# they would push the claim's decisions out of the listing. They are counted in
# one line by DOWNLOADS_SQL. The filter is in this query because the view cannot
# be filtered without a migration.
TRAIL_SQL = (
    "SELECT recorded_at, db_role, service, event, outcome, reason "
    "FROM audit.claim_trail "
    "WHERE claim_id = %s AND tenant = %s AND event <> %s "
    "ORDER BY recorded_at DESC, seq DESC LIMIT %s"
)
DOWNLOADS_SQL = (
    "SELECT count(*), max(recorded_at) FROM audit.claim_trail "
    "WHERE claim_id = %s AND tenant = %s AND event = %s"
)
# How the run ended, completed or failed, at or after the decision: its event
# and reason, or no row while it has not ended (the resend button's test): the
# sweep ends an abandoned run Failed, and sending the decision again could not
# complete it. The first end is the one that counts.
ENDED_SQL = (
    "SELECT event, reason FROM audit.claim_trail "
    "WHERE claim_id = %s AND tenant = %s "
    "AND event IN ('run.completed', 'run.failed') AND recorded_at >= %s "
    "ORDER BY recorded_at, seq LIMIT 1"
)
# The end the page explains: the sweep's, for a run that could not be resumed.
SWEPT_END = ("run.failed", ABANDONED_REASON)
# Why the claim is in its present state: the reason of the latest ``claim.<state>``
# event, the move that brought it there. Asked of the database, not read off the
# listed rows, which are at most TRAIL_LIMIT.
REASON_SQL = (
    "SELECT reason FROM audit.claim_trail "
    "WHERE claim_id = %s AND tenant = %s AND event = %s "
    "ORDER BY recorded_at DESC, seq DESC LIMIT 1"
)
# The reason of the sweep's move of a claim whose documents did not come.
DOCUMENTS_OVERDUE_REASON = DOCUMENTS_OVERDUE.trigger
# The refusal's event after the claim's latest referral (a claim referred again
# starts over): one row or none, asked of the database like ``REASON_SQL``.
LATE_DOCUMENTS_SQL = (
    "SELECT 1 FROM audit.claim_trail AS late "
    "WHERE late.claim_id = %(claim)s AND late.tenant = %(tenant)s "
    "AND late.event = %(late)s AND (late.recorded_at, late.seq) > ("
    "SELECT recorded_at, seq FROM audit.claim_trail "
    "WHERE claim_id = %(claim)s AND tenant = %(tenant)s "
    "AND event = 'claim.awaiting_adjuster' "
    "ORDER BY recorded_at DESC, seq DESC LIMIT 1) LIMIT 1"
)


def documents_refused_since_referral(
    conn: psycopg.Connection, claim_id: str, tenant: str
) -> bool:
    """Whether documents were refused after the deadline since the latest
    referral; the refusal asks it too, with the claim locked."""
    params = {"claim": claim_id, "tenant": tenant, "late": DOCUMENTS_REFUSED_EVENT}
    return conn.execute(LATE_DOCUMENTS_SQL, params).fetchone() is not None


class TrailRow(NamedTuple):
    recorded_at: datetime
    db_role: str
    service: str
    event: str
    outcome: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class ClaimView:
    """What a claim's page shows. ``facts`` holds the submission's fields the
    decision needs, as text, and never the claimant's name or email.
    ``decision`` is the one that moved the claim into its state, if it is in a
    state a decision leads to. ``resend_due`` is true when that decision has a
    run and no ``run.completed`` or ``run.failed`` event was recorded at or
    after it. ``swept`` is true when that end was the sweep's (``run.failed``
    with the reason ``abandoned``): the decision stands, its run was ended
    before it could note it. ``run_id`` is the claim's run (``None`` when it has
    none) and ``triages`` the number of times it has been triaged.
    ``referral_reason`` is the reason of the move that brought a waiting claim
    to the adjuster (``None`` for a claim in another state, or a move with no
    reason). ``documents_refused``: documents were posted after the deadline
    since a referral for overdue documents. ``files`` are the files the claimant
    sent, in arrival order (S070); the page lists them, and links to each only
    when the download is on (``render_claim``'s ``downloads``)."""

    claim_id: str
    state: str
    since: datetime
    received_at: datetime
    facts: Mapping[str, str]
    proposal: TriageProposal | None
    proposal_note: str | None
    decision: tuple[str, datetime] | None
    trail: tuple[TrailRow, ...]
    resend_due: bool = False
    swept: bool = False
    run_id: UUID | None = None
    triages: int = 0
    referral_reason: str | None = None
    documents_refused: bool = False
    files: tuple[FileSummary, ...] = ()
    trail_older: bool = False
    downloads: tuple[int, datetime] | None = None


@dataclass(frozen=True, slots=True)
class Notice:
    """The answer to a post, as the API gives it."""

    status: int
    detail: str


# ── what the pages show, as text ────────────────────────────────────────────
def _when(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _euros(amount: object) -> str:
    if isinstance(amount, int) and not isinstance(amount, bool):
        return f"€{amount:,}"
    return ""


def _text(value: object) -> str:
    return "" if value is None else str(value)


def facts_of(
    submission: object,
    arrived: Sequence[str] = (),
    received_at: datetime | None = None,
) -> dict[str, str]:
    """The submission's fields a decision needs, and the names of documents that
    arrived later, in a row of their own when there are any. The claimant is not
    one of the fields. The two dates are labelled for what they are and followed
    by the gaps in days (``claim_dates``, S070)."""
    claim = submission if isinstance(submission, dict) else {}
    location = claim.get("loss_location")
    place = (
        f"{_text(location.get('city'))}, {_text(location.get('country'))}"
        if isinstance(location, dict)
        else ""
    )
    documents = claim.get("documents")
    named = ", ".join(map(str, documents)) if isinstance(documents, list) else ""
    later = {ARRIVED_LABEL: ", ".join(arrived)} if arrived else {}
    return {
        "Policy number": _text(claim.get("policy_number")),
        "Peril": _text(claim.get("peril")),
        LOSS_DATE_LABEL: _text(claim.get("loss_date")),
        REPORTED_ON_LABEL: _text(claim.get("reported_on")),
        **day_gaps(claim, received_at),
        "Claimed amount": _euros(claim.get("claimed_amount")),
        "Loss location": place,
        "Documents named": named or "none",
        **later,
        "Description": _text(claim.get("description")),
    }


def _next_page_path(cursor: QueueCursor) -> str:
    """The link to the page after ``cursor``: the row's full moment (microseconds
    and offset, which ``_when`` cuts) and its claim, URL-encoded."""
    query = urlencode(
        {
            "after_time": cursor.since.astimezone(UTC).isoformat(),
            "after_claim": cursor.claim_id,
        }
    )
    return f"{QUEUE_PATH}?{query}"


def render_queue(
    rows: list[QueueRow],
    next_page: QueueCursor | None = None,
    *,
    after_cursor: bool = False,
) -> str:
    """The queue page. ``after_cursor`` is a page asked for with a cursor: it has
    a way back to the first page, and when empty says no claim follows the
    cursor (the claims before it may still wait)."""
    shown = [
        {
            "claim_id": r.claim_id,
            "state": r.state,
            "since": _when(r.since),
            "peril": _text(r.peril),
            "amount": _euros(r.claimed_amount),
            "reason": _text(r.reason),
            "rests_on": _queue_mark(r),
            # The same test as the claim's page: the sweep referred it.
            "overdue": r.state == "awaiting_adjuster"
            and r.referral_reason == DOCUMENTS_OVERDUE_REASON,
        }
        for r in rows
    ]
    # One line for the page, the count only: the judgment is silent per row, so
    # that reloading the queue cannot multiply log lines (the claim's own page
    # logs the claim).
    unreadable = sum(s["rests_on"] == UNREADABLE_PROPOSAL_MARK for s in shown)
    if unreadable:
        logger.warning(
            "rows of the queue page whose stored proposal could not be read: %d",
            unreadable,
        )
    return TEMPLATES.get_template("queue.html").render(
        rows=shown,
        limit=QUEUE_LIMIT,
        next_page=None if next_page is None else _next_page_path(next_page),
        first_page=QUEUE_PATH if after_cursor else None,
    )


def render_claim(
    view: ClaimView, notice: Notice | None = None, *, downloads: bool = False
) -> str:
    """The claim's page. ``downloads``: the download is on, so each listed file
    has a link to it; off, the page is what it was without the download."""
    proposal = view.proposal
    payable = _euros(proposal.payable_amount) if proposal else ""
    # The recorded word again, for a run that did not complete; not beside a
    # refusal, which says there is nothing to send again.
    resend = (
        view.decision[0]
        if view.decision and view.resend_due and _may_resend(notice)
        else None
    )
    waiting = view.state == "awaiting_adjuster"
    failed = view.state == "triage_failed"
    has_run = view.run_id is not None
    at_cap = view.triages >= MAX_TRIAGES_PER_CLAIM
    overdue = waiting and view.referral_reason == DOCUMENTS_OVERDUE_REASON
    return TEMPLATES.get_template("claim.html").render(
        view=view,
        since=_when(view.since),
        received_at=_when(view.received_at),
        decision=view.decision and (view.decision[0], _when(view.decision[1])),
        trail=[(_when(t.recorded_at), *t[1:5], _text(t.reason)) for t in view.trail],
        payable=payable,
        # The sentence beside the recommendation; empty when nothing to mark.
        rests_on=RESTS_ON_NOTES.get(
            recommendation_rests_on(
                proposal and proposal.recommendation,
                proposal and proposal.assessment,
            ),
            "",
        ),
        waiting=waiting,
        failed=failed,
        # A claim referred at the cap has no paused run: nothing to send back.
        no_run=waiting and not has_run,
        # The sweep referred it: the documents asked for did not arrive.
        overdue=overdue,
        documents_refused=view.documents_refused,
        # Triaging again is a send-back of a paused run, or a retry of a failed
        # triage; at the cap it is neither, and the page says why.
        send_back=waiting and has_run and not at_cap,
        triage_again=failed and not at_cap,
        cap_reached=at_cap and (failed or (waiting and has_run)),
        cap_words=NUMBER_WORDS[MAX_TRIAGES_PER_CLAIM],
        notice=notice,
        resend=resend,
        swept=view.swept,
        # Jinja would print ``None``: a claim with no run is the empty string.
        run="" if view.run_id is None else str(view.run_id),
        queue_path=QUEUE_PATH,
        downloads=downloads,
        # The downloads of the claim's files, counted and not listed, and whether
        # older events than the listed ones exist.
        file_downloads=view.downloads and (view.downloads[0], _when(view.downloads[1])),
        trail_older=view.trail_older,
        trail_limit=TRAIL_LIMIT,
        # The link, and the file's identifier in it, only with the download on;
        # empty otherwise, and then the page holds no identifier.
        files=[
            (
                f.kind,
                f.media_type,
                size_text(f.size_bytes),
                _when(f.received_at),
                f.sha256[:12],
                f.sha256,
                f"{QUEUE_PATH}/{view.claim_id}/files/{f.file_id}" if downloads else "",
            )
            for f in view.files
        ],
    )


def _may_resend(notice: Notice | None) -> bool:
    return notice is None or notice.status in RESEND_STATUSES


def render_error(
    status: int, detail: str, *, for_claimant: bool = False
) -> HTMLResponse:
    """An error page in the layout of the pages it was refused on: the
    adjuster's, with a way back to the queue, or a claimant's, with a way back
    to the claimant's claims and nothing of the adjuster's."""
    page = TEMPLATES.get_template("error.html").render(
        status=status,
        detail=detail,
        layout="claimant_base.html" if for_claimant else "base.html",
        back_path=CLAIMANT_PREFIX + "claims" if for_claimant else QUEUE_PATH,
        back_label="Back to your claims" if for_claimant else "Back to the queue",
    )
    return HTMLResponse(page, status_code=status)


# ── what the pages read ─────────────────────────────────────────────────────
def _referral_of(
    conn: psycopg.Connection, claim_id: str, tenant: str
) -> tuple[str | None, bool]:
    """For a claim in ``awaiting_adjuster``: the reason of the move that referred
    it (``None`` when there is none) and whether documents were refused after the
    deadline since that referral."""
    referral = conn.execute(
        REASON_SQL, (claim_id, tenant, "claim.awaiting_adjuster")
    ).fetchone()
    referral_reason = None if referral is None else referral[0]
    # Asked of the database: the listed trail cuts off the newest rows.
    documents_refused = (
        referral_reason == DOCUMENTS_OVERDUE_REASON
        and documents_refused_since_referral(conn, claim_id, tenant)
    )
    return referral_reason, documents_refused


def load_claim(dsn: str, tenant: str, claim_id: str) -> ClaimView | None:
    """The claim of ``tenant`` with what its page shows; ``None`` when there is
    no such claim (another tenant's is none)."""
    with connect(dsn, SERVICE_NAME) as conn:
        claim = conn.execute(CLAIM_SQL, (claim_id, tenant)).fetchone()
        if claim is None:
            return None
        state, since, received_at, submission, run_id, triages = claim
        proposal_row = conn.execute(PROPOSAL_SQL, (claim_id,)).fetchone()
        arrived = arrived_documents(conn, claim_id)
        decision = None
        if state in DECIDED_STATES:
            decision = conn.execute(DECISION_SQL, (claim_id, run_id)).fetchone()
        rows = conn.execute(
            TRAIL_SQL, (claim_id, tenant, DOWNLOAD_EVENT, TRAIL_LIMIT + 1)
        ).fetchall()
        ((downloaded, last_download),) = conn.execute(
            DOWNLOADS_SQL, (claim_id, tenant, DOWNLOAD_EVENT)
        ).fetchall()
        # Read with the tenant's filter, as the claim is: never the content.
        files = list_files(conn, tenant, claim_id)
        # A decision with no run has nothing to resume, so nothing to send again.
        # How the run ended is asked of the database, not read off the listed
        # rows: the page lists at most TRAIL_LIMIT of them.
        resend_due = swept = False
        if decision is not None and run_id is not None:
            ended = conn.execute(ENDED_SQL, (claim_id, tenant, decision[1])).fetchone()
            resend_due = ended is None
            swept = ended == SWEPT_END
        referral_reason, documents_refused = (
            _referral_of(conn, claim_id, tenant)
            if state == "awaiting_adjuster"
            else (None, False)
        )
    proposal, note = _proposal_of(claim_id, proposal_row)
    return ClaimView(
        claim_id=claim_id,
        state=state,
        since=since,
        received_at=received_at,
        facts=facts_of(submission, arrived, received_at),
        proposal=proposal,
        proposal_note=note,
        decision=None if decision is None else (decision[0], decision[1]),
        trail=tuple(TrailRow(*row) for row in reversed(rows[:TRAIL_LIMIT])),
        trail_older=len(rows) > TRAIL_LIMIT,
        downloads=(downloaded, last_download) if downloaded else None,
        resend_due=resend_due,
        swept=swept,
        run_id=run_id,
        triages=triages,
        referral_reason=referral_reason,
        documents_refused=documents_refused,
        files=files,
    )


def load_proposal(
    dsn: str, tenant: str, claim_id: str
) -> tuple[str, TriageProposal | None] | None:
    """The claim's state and its latest proposal, which is ``None`` when it has
    none or it cannot be read; ``None`` when ``tenant`` has no such claim. It
    reads nothing of the claimant: no submission, no trail, no run."""
    with connect(dsn, SERVICE_NAME) as conn:
        claim = conn.execute(STATE_SQL, (claim_id, tenant)).fetchone()
        if claim is None:
            return None
        proposal_row = conn.execute(PROPOSAL_SQL, (claim_id,)).fetchone()
    return claim[0], _proposal_of(claim_id, proposal_row)[0]


# ── the routes ──────────────────────────────────────────────────────────────
def add_adjuster_pages(
    app: FastAPI,
    *,
    dsn: str,
    tenant: str,
    tracer: Tracer,
    decide: DecideFn,
    triage_again: TriageAgainFn,
    downloads_enabled: bool = False,
) -> None:
    """Add the queue, the claim, the decision and triage forms and the
    stylesheet to the Claims API. ``decide`` is the API's one decision path and
    ``triage_again`` the one that sends a claim back or tries it again (S048).
    ``downloads_enabled``: the claim page links to each file (the route itself is
    ``file_download``'s)."""
    stylesheet = (PACKAGE_DIR / "static" / "adjuster.css").read_bytes()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(CrossSiteRefused)
    async def refuse_cross_site(request: Request, __: Exception) -> Response:
        # The claim's ID, when the path holds one, and no header value.
        claim_id = str(request.path_params.get("claim_id"))
        if not re.fullmatch(CLAIM_ID_PATTERN, claim_id):
            claim_id = "(not a claim ID)"
        logger.warning("cross-site post refused for claim %s", claim_id)
        # The page of the site the post was aimed at: a claimant's refused post
        # never shows the adjuster's banner or link.
        return render_error(
            HTTP_FORBIDDEN,
            CROSS_SITE_DETAIL,
            for_claimant=request.url.path.startswith(CLAIMANT_PREFIX),
        )

    @app.get(STYLESHEET_PATH, include_in_schema=False)
    def adjuster_stylesheet() -> Response:
        return Response(stylesheet, media_type="text/css")

    @app.get(QUEUE_PATH, include_in_schema=False, response_class=HTMLResponse)
    def adjuster_queue(
        after_time: Annotated[AwareDatetime | None, Query()] = None,
        after_claim: Annotated[str | None, Query(pattern=CLAIM_ID_PATTERN)] = None,
    ) -> Response:
        # The two name the last row of the page before: both or neither. The
        # shared 422 answers, as it does for a value that does not parse.
        if (after_time is None) != (after_claim is None):
            missing = "after_claim" if after_claim is None else "after_time"
            raise RequestValidationError(
                [
                    {
                        "type": "missing",
                        "loc": ("query", missing),
                        "msg": "Field required together with the other cursor field",
                        "input": None,
                    }
                ]
            )
        after = (
            None
            if after_time is None or after_claim is None
            else QueueCursor(after_time, after_claim)
        )
        with start_span(tracer, "claims.adjuster.queue") as span:
            set_span_attributes(span, {"meridian.tenant": tenant})
            try:
                rows, next_page = load_queue(dsn, tenant, after)
            except psycopg.Error as exc:
                mark_error(span, exc)
                return render_error(*database_failure(exc))
        return HTMLResponse(
            render_queue(rows, next_page, after_cursor=after is not None)
        )

    @app.get(
        QUEUE_PATH + "/{claim_id}", include_in_schema=False, response_class=HTMLResponse
    )
    def adjuster_claim(claim_id: ClaimId) -> Response:
        with start_span(tracer, "claims.adjuster.claim") as span:
            set_span_attributes(
                span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
            )
            try:
                view = load_claim(dsn, tenant, claim_id)
            except psycopg.Error as exc:
                mark_error(span, exc)
                return render_error(*claim_database_failure(exc, claim_id))
        if view is None:
            return render_error(HTTP_NOT_FOUND, NO_SUCH_CLAIM_DETAIL)
        return HTMLResponse(render_claim(view, downloads=downloads_enabled))

    # The proposal the page above shows, as JSON, for ``meridian eval run``
    # (T-80): the claim's ID, its state and the proposal, and nothing of the
    # claimant. A read: no audit row, as the page writes none (S021).
    @app.get(QUEUE_PATH + "/{claim_id}/proposal", include_in_schema=False)
    def adjuster_proposal(claim_id: ClaimId) -> JSONResponse:
        with start_span(tracer, "claims.adjuster.proposal") as span:
            set_span_attributes(
                span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
            )
            try:
                found = load_proposal(dsn, tenant, claim_id)
            except psycopg.Error as exc:
                mark_error(span, exc)
                return error_answer(*claim_database_failure(exc, claim_id))
        if found is None:
            return error_answer(HTTP_NOT_FOUND, NO_SUCH_CLAIM_DETAIL)
        state, proposal = found
        return JSONResponse(
            {
                "claim_id": claim_id,
                "state": state,
                "proposal": (
                    None if proposal is None else proposal.model_dump(mode="json")
                ),
            }
        )

    def failure_page(failure: DecisionFailure, claim_id: str) -> Response:
        if failure.status not in PAGE_STATUSES:
            return render_error(failure.status, failure.detail)
        try:
            view = load_claim(dsn, tenant, claim_id)
        except psycopg.Error as exc:
            claim_database_failure(exc, claim_id)  # logs the claim, class, SQLSTATE
            view = None
        if view is None:
            return render_error(failure.status, failure.detail)
        notice = Notice(failure.status, failure.detail)
        page = render_claim(view, notice, downloads=downloads_enabled)
        return HTMLResponse(page, status_code=failure.status)

    def answered(
        call: Callable[[], DecisionResponse | ClaimMoveResponse | DecisionFailure],
        claim_id: str,
    ) -> Response:
        """Run a post's one action: a redirect to the claim's page when it
        succeeds, the claim's page (or an error page) with the answer's status
        and text when it does not."""
        try:
            result = call()
        except HTTPException as exc:
            result = DecisionFailure(exc.status_code, str(exc.detail))
        except AuditUnavailable as exc:
            # What the shared handler of the JSON routes answers, as a page.
            logger.error("audit write failed: %s", exc)
            result = DecisionFailure(HTTP_UNAVAILABLE, AUDIT_UNAVAILABLE)
        if isinstance(result, DecisionFailure):
            return failure_page(result, claim_id)
        return RedirectResponse(f"{QUEUE_PATH}/{claim_id}", status_code=HTTP_SEE_OTHER)

    # ``Form(min_length=1, max_length=1)`` on a list: a post that names a field
    # twice, or not at all, is a 422 (FastAPI's own, the shared JSON answer), not
    # "the last one wins". ``run`` is the claim's run as the page saw it (T-33),
    # empty for a claim with no run; a claim that has another run now is a 409.
    @app.post(
        QUEUE_PATH + "/{claim_id}/decision",
        include_in_schema=False,
        dependencies=[Depends(require_same_origin)],
    )
    def adjuster_decision(
        claim_id: ClaimId,
        decision: Annotated[list[Decision], Form(min_length=1, max_length=1)],
        run: Annotated[list[str], Form(min_length=1, max_length=1)],
    ) -> Response:
        return answered(lambda: decide(claim_id, decision[0], run[0]), claim_id)

    # The one field is the page's run: the button is the rest of the request. It
    # sends a paused claim back to triage and tries a failed triage again (the
    # code behind ``POST /claims/{claim_id}/triage``).
    @app.post(
        QUEUE_PATH + "/{claim_id}/triage",
        include_in_schema=False,
        dependencies=[Depends(require_same_origin)],
    )
    def adjuster_triage(
        claim_id: ClaimId,
        run: Annotated[list[str], Form(min_length=1, max_length=1)],
    ) -> Response:
        return answered(lambda: triage_again(claim_id, run[0]), claim_id)
