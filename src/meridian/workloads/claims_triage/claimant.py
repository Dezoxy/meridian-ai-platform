"""The claimant's pages of the Claims API (S049): the start page with the claim
form and a lookup, the status page, the form that reports the documents asked
for and the withdrawal.

The pages are server-rendered with the adjuster's Jinja2 environment (autoescape
on, no ``|safe``), carry no script, and are out of the OpenAPI contract. The
claim form runs the code of ``POST /claims`` (``store_claim`` then
``triage_claim``), the documents form ``add_documents`` and the withdrawal
``withdraw``: the code of the JSON routes, not a copy of it. Every post takes
T-70's origin check.

A claim that is stored is answered by its status page whatever its triage did:
the claim's state is the answer (T-65). Only a refusal before anything is stored
(another submission under the ID, a form that does not validate) shows the form
again. The status page tells the claimant what happens next and lists the
documents asked for and those that arrived; it reads one expression of the
latest proposal, its missing documents, and never the proposal, the claimant's
name or e-mail address or the description. A message about a form that does not
validate names the field and the rule, never the value (T-03). Nothing here logs
or puts on a span anything of the claim but its ID. There is no sign-in yet:
anyone who reaches the pages reads any claim's status by its ID (T-01, S021).
"""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, get_args

import httpx
import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from opentelemetry.trace import Tracer
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from meridian.platform.common.audit import AuditUnavailable
from meridian.platform.common.db import connect
from meridian.platform.common.http import AUDIT_UNAVAILABLE, database_failure
from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.workloads.claims_triage.adjuster import (
    CLAIM_ID_PATTERN,
    HTTP_NOT_FOUND,
    HTTP_SEE_OTHER,
    HTTP_UNAVAILABLE,
    NO_SUCH_CLAIM_DETAIL,
    TEMPLATES,
    ClaimId,
    Notice,
    _when,
    render_error,
    require_same_origin,
)
from meridian.workloads.claims_triage.lifecycle import SERVICE_NAME, LifecycleState
from meridian.workloads.claims_triage.models import (
    ClaimMoveResponse,
    ClaimSubmission,
    DecisionFailure,
    DocumentsArrival,
    Peril,
)
from meridian.workloads.claims_triage.moves import add_documents, withdraw
from meridian.workloads.claims_triage.triaging import (
    arrived_documents,
    store_claim,
    triage_claim,
)

logger = logging.getLogger(__name__)

START_PATH = "/claimant/claims"
HTTP_UNPROCESSABLE = 422
ONCE_TEXT = "send this field exactly once, as text"
LOOKUP_MESSAGE = "A claim ID is CLM- and four digits, for example CLM-0001."
# The label of an error that belongs to no one field (the loss after the report).
CLAIM_LABEL = "Claim"
DOCUMENTS_LABEL = "Documents"
# The claim form's fields, in the order of the page, with their labels.
FIELD_LABELS: Mapping[str, str] = {
    "claim_id": "Claim ID",
    "policy_number": "Policy number",
    "peril": "Peril",
    "loss_date": "Date of loss",
    "reported_on": "Date reported",
    "claimed_amount": "Claimed amount",
    "city": "City",
    "country": "Country",
    "description": "Description",
    "documents": DOCUMENTS_LABEL,
    "claimant_name": "Your name",
    "claimant_email": "Your email",
}
# Where a validation error is, by the model's location, as the field it is for.
ERROR_LABELS: Mapping[str, str] = {
    "claim_id": FIELD_LABELS["claim_id"],
    "policy_number": FIELD_LABELS["policy_number"],
    "peril": FIELD_LABELS["peril"],
    "loss_date": FIELD_LABELS["loss_date"],
    "reported_on": FIELD_LABELS["reported_on"],
    "claimed_amount": FIELD_LABELS["claimed_amount"],
    "loss_location.city": FIELD_LABELS["city"],
    "loss_location.country": FIELD_LABELS["country"],
    "description": FIELD_LABELS["description"],
    "documents": DOCUMENTS_LABEL,
    "claimant.name": FIELD_LABELS["claimant_name"],
    "claimant.email": FIELD_LABELS["claimant_email"],
}
# The amount is turned into a number only when it is up to seven digits: the
# model's strict integer refuses everything else, with its own message.
AMOUNT_PATTERN = r"[0-9]{1,7}"
STATE_SENTENCES: Mapping[LifecycleState, str] = {
    "submitted": "We have received your claim and are assessing it.",
    "triaging": "We have received your claim and are assessing it.",
    "awaiting_adjuster": "An adjuster is reviewing your claim.",
    "triage_failed": "An adjuster is reviewing your claim.",
    "documents_requested": "We need more documents before we can go on.",
    "approved": "Your claim is approved.",
    "rejected": "Your claim is not approved.",
    "withdrawn": "You withdrew this claim.",
}
# A claimant may take back a claim that waits for an adjuster or for documents.
WITHDRAWABLE: tuple[LifecycleState, ...] = ("awaiting_adjuster", "documents_requested")

# Only the latest proposal's missing documents: never the document itself.
STATUS_SQL = (
    "SELECT state, received_at FROM claims.claims WHERE claim_id = %s AND tenant = %s"
)
MISSING_SQL = (
    "SELECT proposal -> 'missing_documents' FROM claims.triage_proposals "
    "WHERE claim_id = %s ORDER BY created_at DESC LIMIT 1"
)

type Problems = Sequence[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class StatusView:
    """What a claim's status page holds: nothing of the proposal but the names
    of the documents it asked for, nothing of the submission."""

    claim_id: str
    state: LifecycleState
    received_at: datetime
    missing: tuple[str, ...]
    arrived: tuple[str, ...]


def _missing_names(claim_id: str, stored: object) -> tuple[str, ...]:
    """The proposal's missing documents; none when it holds no list of names."""
    if stored is None:
        return ()
    if isinstance(stored, list) and all(isinstance(name, str) for name in stored):
        return tuple(stored)
    # The claim's ID and that it was unreadable: the value is the model's text.
    logger.warning("the missing documents of claim %s could not be read", claim_id)
    return ()


def load_status(dsn: str, tenant: str, claim_id: str) -> StatusView | None:
    """The tenant's claim as its status page shows it; ``None`` when there is no
    such claim (another tenant's is none)."""
    with connect(dsn, SERVICE_NAME) as conn:
        claim = conn.execute(STATUS_SQL, (claim_id, tenant)).fetchone()
        if claim is None:
            return None
        proposal = conn.execute(MISSING_SQL, (claim_id,)).fetchone()
        arrived = arrived_documents(conn, claim_id)
    state, received_at = claim
    return StatusView(
        claim_id=claim_id,
        state=state,
        received_at=received_at,
        missing=_missing_names(claim_id, proposal[0] if proposal else None),
        arrived=arrived,
    )


# ── what the form says ──────────────────────────────────────────────────────
def names_of(text: str) -> tuple[str, ...]:
    """The document names of a textarea: one per line, stripped, blank lines
    dropped, each name once and in order of first appearance (T-38: the bound of
    20 counts what is left)."""
    lines = (line.strip() for line in text.splitlines())
    return tuple(dict.fromkeys(line for line in lines if line))


def read_form(form: FormData) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """The claim form's values and the problems with the fields themselves: a
    field must be sent exactly once, as text (it may be empty). A value that was
    sent, even twice, is kept to be shown again."""
    values: dict[str, str] = {}
    problems: list[tuple[str, str]] = []
    for name, label in FIELD_LABELS.items():
        sent = form.getlist(name)
        first = sent[0] if sent and isinstance(sent[0], str) else ""
        values[name] = first
        if len(sent) != 1 or not isinstance(sent[0], str):
            problems.append((label, ONCE_TEXT))
    return values, problems


def submission_data(values: Mapping[str, str]) -> dict[str, Any]:
    """The dict ``ClaimSubmission`` takes, from the form's values."""
    typed = values["claimed_amount"]
    amount = int(typed) if re.fullmatch(AMOUNT_PATTERN, typed) else typed
    return {
        "claim_id": values["claim_id"],
        "policy_number": values["policy_number"],
        "peril": values["peril"],
        "loss_date": values["loss_date"],
        "reported_on": values["reported_on"],
        "claimed_amount": amount,
        "loss_location": {"city": values["city"], "country": values["country"]},
        "description": values["description"],
        "documents": names_of(values["documents"]),
        "claimant": {
            "name": values["claimant_name"],
            "email": values["claimant_email"],
        },
    }


def messages_of(exc: ValidationError) -> list[tuple[str, str]]:
    """One message per error, as the field's label and the model's text: no
    input, no URL, no context, so no value of the claimant's is in one."""
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    shown = (
        (
            ERROR_LABELS.get(
                ".".join(part for part in error["loc"] if isinstance(part, str)),
                CLAIM_LABEL,
            ),
            error["msg"],
        )
        for error in errors
    )
    return list(dict.fromkeys(shown))


# ── what the pages show, as text ────────────────────────────────────────────
def render_start(
    values: Mapping[str, str] | None = None,
    *,
    errors: Problems = (),
    notice: Notice | None = None,
    lookup_message: str = "",
    status: int = 200,
) -> HTMLResponse:
    """The start page: the claim form, with ``values`` in its inputs, and the
    lookup."""
    page = TEMPLATES.get_template("claimant_start.html").render(
        values=values or dict.fromkeys(FIELD_LABELS, ""),
        labels=FIELD_LABELS,
        perils=get_args(Peril),
        errors=errors,
        notice=notice,
        lookup_message=lookup_message,
    )
    return HTMLResponse(page, status_code=status)


def render_status(
    view: StatusView,
    *,
    notice: Notice | None = None,
    errors: Problems = (),
    typed: str = "",
) -> str:
    return TEMPLATES.get_template("claimant_claim.html").render(
        claim_id=view.claim_id,
        received_at=_when(view.received_at),
        sentence=STATE_SENTENCES[view.state],
        asking=view.state == "documents_requested",
        missing=view.missing,
        arrived=view.arrived,
        can_withdraw=view.state in WITHDRAWABLE,
        notice=notice,
        errors=errors,
        typed=typed,
    )


def claimant_error(status: int, detail: str) -> HTMLResponse:
    return render_error(status, detail, for_claimant=True)


def audit_unavailable(exc: AuditUnavailable) -> HTMLResponse:
    """What the shared handler of the JSON routes answers, as a claimant's page."""
    logger.error("audit write failed: %s", exc)
    return claimant_error(HTTP_UNAVAILABLE, AUDIT_UNAVAILABLE)


def to_status(claim_id: str) -> RedirectResponse:
    return RedirectResponse(f"{START_PATH}/{claim_id}", status_code=HTTP_SEE_OTHER)


# ── the routes ──────────────────────────────────────────────────────────────
def add_claimant_pages(
    app: FastAPI,
    *,
    dsn: str,
    tenant: str,
    http: httpx.Client,
    tracer: Tracer,
) -> None:
    """Add the start page, the claim form, the status page and the documents and
    withdrawal forms to the Claims API. Called after ``add_adjuster_pages``,
    whose middleware adds the headers and whose handler refuses a post another
    site made (it picks the claimant's page by the path)."""

    def status_response(
        claim_id: str,
        *,
        notice: Notice | None = None,
        errors: Problems = (),
        typed: str = "",
        status: int = 200,
    ) -> Response:
        """The claim's status page, with the answer to a post as a notice or the
        form's messages. A claim that cannot be read is the error page: the
        answer's own status and text when there is one, else the read's."""
        with start_span(tracer, "claims.claimant.status") as span:
            set_span_attributes(
                span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
            )
            try:
                view = load_status(dsn, tenant, claim_id)
            except psycopg.Error as exc:
                mark_error(span, exc)
                failure = database_failure(exc)  # logs the class and SQLSTATE
                return claimant_error(
                    *(failure if notice is None else (notice.status, notice.detail))
                )
        if view is None:
            return claimant_error(
                *(
                    (HTTP_NOT_FOUND, NO_SUCH_CLAIM_DETAIL)
                    if notice is None
                    else (notice.status, notice.detail)
                )
            )
        page = render_status(view, notice=notice, errors=errors, typed=typed)
        return HTMLResponse(page, status_code=status)

    def triage_stored(claim_id: str, submission: ClaimSubmission) -> None:
        """Triage a stored claim in a span, in one thread. Whatever the triage
        answers or refuses (409: being triaged, or it already has a proposal), the
        claim's state is the answer (T-65); an audit failure is not swallowed."""
        with start_span(tracer, "claims.claimant.submit") as span:
            set_span_attributes(
                span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
            )
            try:
                triage_claim(dsn, tenant, http, span, claim_id, submission)
            except HTTPException as exc:
                logger.info(
                    "triage of claim %s refused: %s (the claim's state answers)",
                    claim_id,
                    exc.status_code,
                )

    def answered(
        claim_id: str,
        call: Callable[[], ClaimMoveResponse | DecisionFailure],
    ) -> Response:
        """Run a post's one action: a redirect to the status page when it
        succeeds, the status page with the answer's status and text when it
        does not."""
        try:
            result = call()
        except HTTPException as exc:
            result = DecisionFailure(exc.status_code, str(exc.detail))
        except AuditUnavailable as exc:
            return audit_unavailable(exc)
        if isinstance(result, DecisionFailure):
            return status_response(
                claim_id,
                notice=Notice(result.status, result.detail),
                status=result.status,
            )
        return to_status(claim_id)

    @app.get(START_PATH, include_in_schema=False, response_class=HTMLResponse)
    def claimant_start(request: Request) -> Response:
        asked = request.query_params.getlist("claim_id")
        if not asked:
            return render_start()
        if len(asked) == 1 and re.fullmatch(CLAIM_ID_PATTERN, asked[0]):
            return to_status(asked[0])
        # Not echoed: the value is not a claim ID, and the message says what is.
        return render_start(lookup_message=LOOKUP_MESSAGE, status=HTTP_UNPROCESSABLE)

    # The form is read in the route, not declared as ``Form()`` parameters: the
    # 422 FastAPI builds for those is JSON and carries no page. The blocking work
    # (the database, the runtime) runs in the thread pool, off the event loop.
    @app.post(
        START_PATH,
        include_in_schema=False,
        dependencies=[Depends(require_same_origin)],
    )
    async def claimant_submit(request: Request) -> Response:
        form = await request.form()
        try:
            values, problems = read_form(form)
        finally:
            await form.close()
        if problems:
            return render_start(values, errors=problems, status=HTTP_UNPROCESSABLE)
        try:
            submission = ClaimSubmission.model_validate(submission_data(values))
        except ValidationError as exc:
            # Never ``str(exc)``, and nothing logged: it quotes the values.
            return render_start(
                values, errors=messages_of(exc), status=HTTP_UNPROCESSABLE
            )
        claim_id = submission.claim_id
        try:
            await run_in_threadpool(
                store_claim, dsn, tenant, submission.model_dump(mode="json")
            )
        except HTTPException as exc:
            # Another submission under this ID: nothing is stored, nothing runs.
            return render_start(
                values,
                notice=Notice(exc.status_code, str(exc.detail)),
                status=exc.status_code,
            )
        except psycopg.Error as exc:
            return claimant_error(*database_failure(exc))
        except AuditUnavailable as exc:
            return audit_unavailable(exc)
        try:
            await run_in_threadpool(triage_stored, claim_id, submission)
        except AuditUnavailable as exc:
            return audit_unavailable(exc)
        return to_status(claim_id)

    @app.get(
        START_PATH + "/{claim_id}", include_in_schema=False, response_class=HTMLResponse
    )
    def claimant_status(claim_id: ClaimId) -> Response:
        return status_response(claim_id)

    @app.post(
        START_PATH + "/{claim_id}/documents",
        include_in_schema=False,
        dependencies=[Depends(require_same_origin)],
    )
    async def claimant_documents(claim_id: ClaimId, request: Request) -> Response:
        form = await request.form()
        try:
            sent = form.getlist("documents")
        finally:
            await form.close()
        if len(sent) != 1 or not isinstance(sent[0], str):
            return await run_in_threadpool(
                status_response,
                claim_id,
                errors=[(DOCUMENTS_LABEL, ONCE_TEXT)],
                status=HTTP_UNPROCESSABLE,
            )
        try:
            arrival = DocumentsArrival.model_validate({"documents": names_of(sent[0])})
        except ValidationError as exc:
            return await run_in_threadpool(
                status_response,
                claim_id,
                errors=messages_of(exc),
                typed=sent[0],
                status=HTTP_UNPROCESSABLE,
            )
        return await run_in_threadpool(
            answered,
            claim_id,
            lambda: add_documents(
                dsn, tenant, http, tracer, claim_id, arrival.documents
            ),
        )

    # No field: the button is the whole request.
    @app.post(
        START_PATH + "/{claim_id}/withdrawal",
        include_in_schema=False,
        dependencies=[Depends(require_same_origin)],
    )
    def claimant_withdrawal(claim_id: ClaimId) -> Response:
        return answered(claim_id, lambda: withdraw(dsn, tenant, http, tracer, claim_id))
