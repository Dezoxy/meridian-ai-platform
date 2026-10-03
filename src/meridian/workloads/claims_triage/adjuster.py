"""The adjuster's pages of the Claims API (S016): the queue of the claims that
wait for a person, one claim with its proposal and audit trail, and the form
that records a decision. One JSON route, ``GET /adjuster/claims/{claim_id}/proposal``,
answers the stored proposal for ``meridian eval run`` (S050, T-78).

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
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Form, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from opentelemetry.trace import Tracer
from pydantic import ValidationError
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
from meridian.workloads.claims_triage.lifecycle import (
    MAX_TRIAGES_PER_CLAIM,
    SERVICE_NAME,
)
from meridian.workloads.claims_triage.models import (
    ClaimMoveResponse,
    Decision,
    DecisionFailure,
    DecisionResponse,
)
from meridian.workloads.claims_triage.proposal import TriageProposal
from meridian.workloads.claims_triage.triaging import NUMBER_WORDS, arrived_documents

logger = logging.getLogger(__name__)

PACKAGE_DIR = FilePath(__file__).parent
PAGES_PREFIX = "/adjuster/"
CLAIMANT_PREFIX = "/claimant/"
QUEUE_PATH = "/adjuster/claims"
STYLESHEET_PATH = "/adjuster/static/adjuster.css"
NO_SUCH_CLAIM_DETAIL = "no such claim"
CROSS_SITE_DETAIL = "the request came from another site"
NO_PROPOSAL_TEXT = "no proposal is stored"
ARRIVED_LABEL = "Documents that arrived later"
NO_STRUCTURED_PROPOSAL_TEXT = "no structured proposal"
UNREADABLE_PROPOSAL_TEXT = "the stored proposal could not be read"
# The queue's two states are literals in QUEUE_SQL: the partial index of
# migration 0011 serves a query only when its WHERE implies the index's.
QUEUE_LIMIT = 100
TRAIL_LIMIT = 200
HTTP_OK = 200
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
# The only ``Sec-Fetch-Site`` values that pass: ``same-origin`` (the page's own
# post) and ``none`` (the user typed the address). Any other is another site,
# or a value this code does not know (T-70).
OWN_FETCHES = ("same-origin", "none")
CLAIM_ID_PATTERN = r"^CLM-[0-9]{4}$"
# The stylesheet holds no data, so it may be cached; every other response under
# ``/adjuster/`` may hold a claim and is not stored.
STYLESHEET_CACHE_CONTROL = "max-age=3600"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # The Referer holds the claim's ID, so it goes only to this app. A post of
    # the page itself then carries a real Origin in a browser without Fetch
    # Metadata (``no-referrer`` makes Chrome send ``Origin: null``).
    "Referrer-Policy": "same-origin",
    # A page of another site cannot load a response of ``/adjuster/`` or
    # ``/claimant/``, not even with ``no-cors``.
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}

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

QUEUE_SQL = (
    "SELECT c.claim_id, c.state, c.state_changed_at, c.submission ->> 'peril', "
    "c.submission -> 'claimed_amount', p.reason "
    "FROM claims.claims AS c "
    "LEFT JOIN LATERAL (SELECT reason FROM claims.triage_proposals "
    "WHERE claim_id = c.claim_id ORDER BY created_at DESC LIMIT 1) AS p ON true "
    "WHERE c.tenant = %s AND c.state IN ('awaiting_adjuster', 'triage_failed') "
    "ORDER BY c.state_changed_at, c.claim_id LIMIT %s"
)
CLAIM_SQL = (
    "SELECT state, state_changed_at, received_at, submission, run_id, triages "
    "FROM claims.claims WHERE claim_id = %s AND tenant = %s"
)
PROPOSAL_SQL = (
    "SELECT proposal FROM claims.triage_proposals "
    "WHERE claim_id = %s ORDER BY created_at DESC LIMIT 1"
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
# (migration 0011, T-71); the tenant filter is the page's own as well.
TRAIL_SQL = (
    "SELECT recorded_at, db_role, service, event, outcome FROM audit.claim_trail "
    "WHERE claim_id = %s AND tenant = %s ORDER BY recorded_at, event LIMIT %s"
)
# Whether the run completed at or after the decision (the resend button's test).
COMPLETED_SQL = (
    "SELECT EXISTS (SELECT 1 FROM audit.claim_trail "
    "WHERE claim_id = %s AND tenant = %s AND event = 'run.completed' "
    "AND recorded_at >= %s)"
)


class TrailRow(NamedTuple):
    recorded_at: datetime
    db_role: str
    service: str
    event: str
    outcome: str


class QueueRow(NamedTuple):
    claim_id: str
    state: str
    since: datetime
    peril: str | None
    claimed_amount: object
    reason: str | None


@dataclass(frozen=True, slots=True)
class ClaimView:
    """What a claim's page shows. ``facts`` holds the submission's fields the
    decision needs, as text, and never the claimant's name or email.
    ``decision`` is the one that moved the claim into its state, if it is in a
    state a decision leads to. ``resend_due`` is true when that decision has a
    run and no ``run.completed`` event was recorded at or after it. ``run_id``
    is the claim's run (``None`` when it has none) and ``triages`` the number
    of times it has been triaged."""

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
    run_id: UUID | None = None
    triages: int = 0


@dataclass(frozen=True, slots=True)
class Notice:
    """The answer to a post, as the API gives it."""

    status: int
    detail: str


class CrossSiteRefused(Exception):
    """The post came from another site (T-70)."""


def is_cross_site(origin: str | None, host: str | None, fetch_site: str | None) -> bool:
    """Whether a post is one that another site made (T-70).

    ``Sec-Fetch-Site``, when present, decides alone: ``same-origin`` and
    ``none`` pass; ``cross-site``, ``same-site`` and any value not known are
    refused. A page cannot set it (it is a forbidden header name, the browser
    sets it), so it is trusted over ``Origin``: a browser that sends it sends
    ``Origin: null`` for the page's own post under some referrer policies.

    Only when it is absent (an older browser, curl, a test) does ``Origin``
    count: ``null``, one that is not a URL, and one whose host and port are not
    the request's ``Host`` are refused (a browser without Fetch Metadata sends
    a real ``Origin`` under the page's ``same-origin`` referrer policy).
    Netlocs are compared, not schemes (the edge may end TLS), and not case. A
    request with none of the headers passes, as it does on the JSON route
    (T-69).
    """
    if fetch_site is not None:
        return fetch_site.lower() not in OWN_FETCHES
    if origin is None:
        return False
    if host is None:
        return True
    try:
        netloc = urlsplit(origin).netloc
    except ValueError:
        return True
    # ``null`` and a bare word have no netloc and can never equal a Host.
    return netloc.lower() != host.lower()


def require_same_origin(request: Request) -> None:
    headers = request.headers
    if is_cross_site(
        headers.get("origin"), headers.get("host"), headers.get("sec-fetch-site")
    ):
        raise CrossSiteRefused


class SecurityHeadersMiddleware:
    """Add the pages' headers to every response under ``/adjuster/`` and
    ``/claimant/``: a 404, a 405, a 413 and a 422 as well as a page. The JSON
    routes are not touched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(
            (PAGES_PREFIX, CLAIMANT_PREFIX)
        ):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                if scope["path"] == STYLESHEET_PATH and message["status"] == HTTP_OK:
                    headers["Cache-Control"] = STYLESHEET_CACHE_CONTROL
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ── what the pages show, as text ────────────────────────────────────────────
def _when(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _euros(amount: object) -> str:
    if isinstance(amount, int) and not isinstance(amount, bool):
        return f"€{amount:,}"
    return ""


def _text(value: object) -> str:
    return "" if value is None else str(value)


def facts_of(submission: object, arrived: Sequence[str] = ()) -> dict[str, str]:
    """The submission's fields a decision needs, and the names of documents that
    arrived later, in a row of their own when there are any. The claimant is not
    one of the fields."""
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
        "Loss date": _text(claim.get("loss_date")),
        "Reported on": _text(claim.get("reported_on")),
        "Claimed amount": _euros(claim.get("claimed_amount")),
        "Loss location": place,
        "Documents named": named or "none",
        **later,
        "Description": _text(claim.get("description")),
    }


def render_queue(rows: list[QueueRow]) -> str:
    shown = [
        {
            "claim_id": r.claim_id,
            "state": r.state,
            "since": _when(r.since),
            "peril": _text(r.peril),
            "amount": _euros(r.claimed_amount),
            "reason": _text(r.reason),
        }
        for r in rows
    ]
    return TEMPLATES.get_template("queue.html").render(rows=shown, limit=QUEUE_LIMIT)


def render_claim(view: ClaimView, notice: Notice | None = None) -> str:
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
    return TEMPLATES.get_template("claim.html").render(
        view=view,
        since=_when(view.since),
        received_at=_when(view.received_at),
        decision=view.decision and (view.decision[0], _when(view.decision[1])),
        trail=[(_when(t.recorded_at), *t[1:]) for t in view.trail],
        payable=payable,
        waiting=waiting,
        failed=failed,
        # A claim referred at the cap has no paused run: nothing to send back.
        no_run=waiting and not has_run,
        # Triaging again is a send-back of a paused run, or a retry of a failed
        # triage; at the cap it is neither, and the page says why.
        send_back=waiting and has_run and not at_cap,
        triage_again=failed and not at_cap,
        cap_reached=at_cap and (failed or (waiting and has_run)),
        cap_words=NUMBER_WORDS[MAX_TRIAGES_PER_CLAIM],
        notice=notice,
        resend=resend,
        # Jinja would print ``None``: a claim with no run is the empty string.
        run="" if view.run_id is None else str(view.run_id),
        queue_path=QUEUE_PATH,
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
def load_queue(dsn: str, tenant: str) -> list[QueueRow]:
    with connect(dsn, SERVICE_NAME) as conn:
        rows = conn.execute(QUEUE_SQL, (tenant, QUEUE_LIMIT)).fetchall()
    return [QueueRow(*row) for row in rows]


def _proposal_of(
    claim_id: str, row: tuple | None
) -> tuple[TriageProposal | None, str | None]:
    """The latest proposal and, when there is none to show, why."""
    if row is None:
        return None, NO_PROPOSAL_TEXT
    if row[0] is None:
        return None, NO_STRUCTURED_PROPOSAL_TEXT
    try:
        return TriageProposal.model_validate(row[0]), None
    except ValidationError as exc:
        # The claim's ID and the class only: the document is the model's text.
        logger.warning(
            "the stored proposal of claim %s is not valid: %s",
            claim_id,
            type(exc).__name__,
        )
        return None, UNREADABLE_PROPOSAL_TEXT


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
        trail = conn.execute(TRAIL_SQL, (claim_id, tenant, TRAIL_LIMIT)).fetchall()
        # A decision with no run has nothing to resume, so nothing to send again.
        # Whether the run completed is asked of the database, not read off the
        # listed rows: the page lists at most TRAIL_LIMIT of them.
        resend_due = False
        if decision is not None and run_id is not None:
            completed = conn.execute(
                COMPLETED_SQL, (claim_id, tenant, decision[1])
            ).fetchone()
            resend_due = not completed[0]
    proposal, note = _proposal_of(claim_id, proposal_row)
    return ClaimView(
        claim_id=claim_id,
        state=state,
        since=since,
        received_at=received_at,
        facts=facts_of(submission, arrived),
        proposal=proposal,
        proposal_note=note,
        decision=None if decision is None else (decision[0], decision[1]),
        trail=tuple(TrailRow(*row) for row in trail),
        resend_due=resend_due,
        run_id=run_id,
        triages=triages,
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
) -> None:
    """Add the queue, the claim, the decision and triage forms and the
    stylesheet to the Claims API. ``decide`` is the API's one decision path and
    ``triage_again`` the one that sends a claim back or tries it again (S048)."""
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
    def adjuster_queue() -> Response:
        with start_span(tracer, "claims.adjuster.queue") as span:
            set_span_attributes(span, {"meridian.tenant": tenant})
            try:
                rows = load_queue(dsn, tenant)
            except psycopg.Error as exc:
                mark_error(span, exc)
                return render_error(*database_failure(exc))
        return HTMLResponse(render_queue(rows))

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
                return render_error(*database_failure(exc))
        if view is None:
            return render_error(HTTP_NOT_FOUND, NO_SUCH_CLAIM_DETAIL)
        return HTMLResponse(render_claim(view))

    # The proposal the page above shows, as JSON, for ``meridian eval run``
    # (T-78): the claim's ID, its state and the proposal, and nothing of the
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
                return error_answer(*database_failure(exc))
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
            database_failure(exc)  # logs the class and SQLSTATE
            view = None
        if view is None:
            return render_error(failure.status, failure.detail)
        notice = Notice(failure.status, failure.detail)
        return HTMLResponse(render_claim(view, notice), status_code=failure.status)

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
