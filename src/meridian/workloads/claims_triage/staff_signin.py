"""The Claims API's staff sign-in, wired (S021, Y4): the guard of the staff routes
and pages, and the four routes of the pages' flow. Imported by ``app`` alone (an
import contract keeps the services and the graph code from the sign-in modules);
the routers take what they need as arguments. Built only when ``MERIDIAN_SIGNIN``
is ``staff``; off, nothing here is built and every route is as open as before.

What is guarded. The pages (every route of ``adjuster`` but the stylesheet and
the proposal, and the download) need a staff session cookie or a bearer token
with the ``adjuster`` role and answer a refusal as a page. The JSON routes (the
proposal, the decision, the triage and the three brief routes) need the same and
answer a refusal as JSON, 401 with ``WWW-Authenticate: Bearer`` and 403; on them
a request that carries the cookie and changes state is refused when another site
made it (a cookie travels by itself in a cross-site request, a bearer token does
not). Not guarded, on purpose: ``POST /claims``, the withdrawal, the documents,
the upload routes and everything under ``/claimant/`` (S093 gives those their
own sign-in), ``/healthz`` and the stylesheet.

The four routes, all plain ``def`` (the code exchange and the key fetch block),
all out of the OpenAPI document, each called with ``time.time()``:

- ``GET /auth/start`` begins a sign-in and sends the person to the issuer. An
  optional ``return_to`` is a path under ``/adjuster``, else the queue.
- ``GET /auth/callback`` finishes it. It answers 303 and nothing else: to the
  page the person was going to when signed in, to ``/auth/failed`` when not.
  The transaction cookie is cleared whenever ``finish`` ran, signed in or not;
  a callback shed for want of a permit never ran it and leaves the cookie as
  it was. Nothing of the refusal's reason is shown, and nothing retries. At
  most ``CALLBACK_PERMITS`` run at once, because each makes one outbound
  request that may take five seconds; one without a permit is sent to
  ``/auth/failed`` at once.
- ``GET /auth/failed`` is a fixed page with one link to start again.
- ``POST /auth/sign-out`` clears the cookie and goes to the issuer's end-session
  address; it refuses a post another site made, since a GET logout could be
  started by any page.

The order of things. The framework parses a request's body before it resolves
the dependencies, so a malformed JSON body is a 422 and a malformed form a 400
without a credential; nothing but "the body is not valid" is learned that way,
and the body limit applies first. A well-formed body with a bad field meets the
guard first and is a 401.

What a refusal looks like on a page. A 401 on a GET or HEAD is a 303 to
``/auth/start`` with the page's path as ``return_to`` (a path, never the query),
so signing in brings the person back; for the download that is its claim's page,
not the file. A 401 on another method is the adjuster's
error page with its link back to the queue; a 403 is the same with a sentence
that says the account lacks the role and a sign-out form, so a person can change
account; a 503 is the error page. The texts are fixed. A subject, a role, a
cookie, a token, a code or a state is never in an exception's text, a log
argument, a span or a body here. The handler wraps the one the claimant's pages
registered and hands on everything that is not the staff pages'.
"""

import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Self
from urllib.parse import quote, urlsplit

import httpx
from fastapi import Depends, FastAPI, Request, params
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from meridian.platform.common.env import SettingsError
from meridian.platform.common.http import REFUSED, ErrorBody
from meridian.platform.common.signin import Principal, SigninRefusal, SigninSettings
from meridian.platform.common.signinflow import FlowSettings, SigninFlow
from meridian.platform.common.signinguard import Signin
from meridian.platform.common.signinkeys import KeySet
from meridian.platform.common.signinsession import SessionSettings
from meridian.platform.common.signinstate import transaction_cookie_name
from meridian.platform.common.throttle import RefusalAuditThrottle
from meridian.workloads.claims_triage.adjuster import (
    CLAIM_ID_PATTERN,
    QUEUE_PATH,
    TEMPLATES,
)
from meridian.workloads.claims_triage.page_security import (
    PAGES_PREFIX,
    is_cross_site,
    require_same_origin,
)

logger = logging.getLogger(__name__)

POPULATION = "staff"
ROLE = "adjuster"
START_PATH = "/auth/start"
CALLBACK_PATH = "/auth/callback"
FAILED_PATH = "/auth/failed"
SIGN_OUT_PATH = "/auth/sign-out"
# How many callbacks may be at the issuer at once: each makes one outbound
# request, up to five seconds, in a thread of the pool.
CALLBACK_PERMITS = 4
HTTP_SEE_OTHER = 303
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_UNAVAILABLE = 503
# The statuses the guard answers with, and so the ones this module re-answers.
REFUSAL_STATUSES = (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN, HTTP_UNAVAILABLE)
READS = ("GET", "HEAD")
# The proposal is the JSON route among the adjuster's paths: it answers a
# refusal as JSON, for a script.
PROPOSAL_PATH = re.compile(r"/adjuster/claims/[^/]+/proposal")
# The download: a person sent back to it after the issuer arrives by a navigation
# that is not a same-origin fetch, which the download refuses, and its file
# identifier is a capability that belongs in no return address.
DOWNLOAD_PATH = re.compile(r"/adjuster/claims/([^/]+)/files/[^/]+")
# What the OpenAPI document says of the two refusals, for a person (the shared
# descriptions are for a calling service).
SIGNIN_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTP_UNAUTHORIZED: {
        "model": ErrorBody,
        "description": "No valid bearer token or staff session was presented.",
    },
    HTTP_FORBIDDEN: {
        "model": ErrorBody,
        "description": (
            "The caller does not hold the adjuster role, or the request carried "
            "the session cookie from another site."
        ),
    },
    # The routes document 503 for the database and the audit log, with the
    # claim's ID in the body; the guard's 503 (the issuer's keys cannot be had)
    # has the looser body. The routes put this after theirs, so this one wins.
    HTTP_UNAVAILABLE: {
        "model": ErrorBody,
        "description": (
            "The database or the audit log is unavailable, or the sign-in "
            "issuer's keys could not be fetched."
        ),
    },
}

NOT_SIGNED_IN_TEXT = (
    "You are not signed in, or your session has ended, so nothing was recorded. "
    "Open the queue to sign in again."
)
NO_ROLE_TEXT = (
    "This account does not have the adjuster role. Sign out to use another account."
)
UNAVAILABLE_TEXT = "Sign-in is not available right now. Try again in a moment."
FAILED_TEXT = "Sign-in did not complete."
SIGN_IN_AGAIN = "Sign in again"
BACK_TO_QUEUE = "Back to the queue"

type Dependency = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class Guards:
    """What ``app`` hands the routers: the dependencies of the pages and of the
    JSON routes, and the answers the JSON routes add to the OpenAPI document.
    Each is empty while the switch is off."""

    pages: tuple[params.Depends, ...] = ()
    json: tuple[params.Depends, ...] = ()
    responses: Mapping[int | str, dict[str, Any]] = field(default_factory=dict)


NO_GUARDS = Guards()


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _refusing_cross_site_cookies(role: Dependency) -> Dependency:
    """A dependency, to run after ``role``, that refuses with a 403 a request
    that carries the cookie, changes state and was made by another site. A
    bearer token is not sent by a browser by itself, so it is not asked."""

    def refuse_cross_site_cookie(
        request: Request, principal: Annotated[Principal, Depends(role)]
    ) -> None:
        if (
            principal.via == "cookie"
            and request.method not in READS
            and is_cross_site(
                request.headers.get("origin"),
                request.headers.get("host"),
                request.headers.get("sec-fetch-site"),
            )
        ):
            raise StarletteHTTPException(HTTP_FORBIDDEN, REFUSED)

    return refuse_cross_site_cookie


class StaffSignin:
    """The staff population's sign-in on the Claims API: one key set, the guard
    and the page flow sharing it, one session setting. ``transport`` is for a
    test (it serves the key set and the token endpoint). The issuer is not one of
    the mesh's services, so no client certificate is configured.

    Refuses to build, with a fixed sentence, when the two settings are not both
    for the staff population at one issuer, or when the redirect address's path
    is not the callback route's: the issuer would send the person to a path that
    is no route."""

    def __init__(
        self,
        signin_settings: SigninSettings,
        flow_settings: FlowSettings,
        sessions: SessionSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (signin_settings.population, flow_settings.population) != (
            POPULATION,
            POPULATION,
        ):
            raise SettingsError("the staff sign-in needs the staff population")
        if signin_settings.issuer != flow_settings.issuer:
            raise SettingsError("the bearer check and the page flow need one issuer")
        if urlsplit(flow_settings.redirect_uri).path != CALLBACK_PATH:
            raise SettingsError(
                f"the staff redirect address must end in the path {CALLBACK_PATH}"
            )
        keys = KeySet(signin_settings.keys_url, transport=transport)
        self.guard = Signin(signin_settings, keys, sessions)
        self.flow = SigninFlow(flow_settings, sessions, keys, transport=transport)
        self.sessions = sessions
        # Reachable so that a test can take the permits.
        self.permits = threading.BoundedSemaphore(CALLBACK_PERMITS)
        # Where the sign-out form is sent on: the pages' policy names it.
        self.issuer_origin = _origin_of(flow_settings.end_session_url)
        self._page_role = self.guard.require_role(ROLE, json_form=False)
        self._json_role = self.guard.require_role(ROLE)
        self._cross_site = _refusing_cross_site_cookies(self._json_role)
        self._throttle = RefusalAuditThrottle()

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Self:
        """Build it from the staff settings, the session key and the flow's
        addresses, and nothing else. Raise ``SettingsError`` naming the variable,
        never the value."""
        return cls(
            SigninSettings.from_env(environ, POPULATION),
            FlowSettings.from_env(
                environ,
                POPULATION,
                return_prefixes=("/adjuster",),
                default_return_path=QUEUE_PATH,
            ),
            SessionSettings.from_env(environ),
        )

    def guards(self) -> Guards:
        return Guards(
            pages=(Depends(self._page_role),),
            json=(Depends(self._json_role), Depends(self._cross_site)),
            responses=SIGNIN_RESPONSES,
        )

    def say(self, key: str, message: str) -> None:
        """One warning per key per window, with the count held back; ``message``
        is a fixed text, with at most a class name in it."""
        carried = self._throttle.due(None, key)
        if carried is not None:
            extra = f" (and {carried} more since the last line)" if carried else ""
            logger.warning("%s%s", message, extra)


def guards_of(staff: StaffSignin | None) -> Guards:
    """The guards of the app: none while the switch is off."""
    return NO_GUARDS if staff is None else staff.guards()


# ── the pages ───────────────────────────────────────────────────────────────
def _page(
    status: int,
    detail: str,
    *,
    back_path: str = QUEUE_PATH,
    back_label: str = BACK_TO_QUEUE,
    sign_out: bool = False,
) -> HTMLResponse:
    """The adjuster's error page, in the layout the sign-in is on for. Fixed
    texts only; ``return_to`` is in no HTML."""
    html = TEMPLATES.get_template("error.html").render(
        status=status,
        detail=detail,
        layout="base.html",
        back_path=back_path,
        back_label=back_label,
        signin=True,
        sign_out=sign_out,
    )
    return HTMLResponse(html, status_code=status)


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(
        location, status_code=HTTP_SEE_OTHER, headers={"Cache-Control": "no-store"}
    )


def _is_staff_page(path: str) -> bool:
    return path.startswith(PAGES_PREFIX) and not PROPOSAL_PATH.fullmatch(path)


def _return_to(path: str) -> str:
    """Where signing in brings a person back to: the refused page, except the
    download, whose return address is its claim's page (or the queue when the
    claim's ID is not one), never the file's path."""
    found = DOWNLOAD_PATH.fullmatch(path)
    if found is None:
        return path
    claim_id = found.group(1)
    if re.fullmatch(CLAIM_ID_PATTERN, claim_id):
        return f"{QUEUE_PATH}/{claim_id}"
    return QUEUE_PATH


def _wrap_refusals(app: FastAPI) -> None:
    """Answer the guard's refusals of a staff page as a page. The handler is
    the one registered before (the claimant's, which hands on to the framework's):
    everything that is not a refusal of a staff page goes to it."""
    previous = app.exception_handlers[StarletteHTTPException]

    @app.exception_handler(StarletteHTTPException)
    async def staff_refusals(request: Request, exc: StarletteHTTPException) -> Response:
        path = request.url.path
        if (
            exc.status_code not in REFUSAL_STATUSES
            or exc.detail != REFUSED
            or not _is_staff_page(path)
        ):
            return await previous(request, exc)
        if exc.status_code == HTTP_UNAUTHORIZED:
            if request.method in READS:
                # The path only, never the query; the flow keeps it only if it
                # is a clean path under the adjuster's pages.
                return _redirect(
                    f"{START_PATH}?return_to={quote(_return_to(path), safe='/')}"
                )
            return _page(HTTP_UNAUTHORIZED, NOT_SIGNED_IN_TEXT)
        if exc.status_code == HTTP_FORBIDDEN:
            return _page(HTTP_FORBIDDEN, NO_ROLE_TEXT, sign_out=True)
        return _page(HTTP_UNAVAILABLE, UNAVAILABLE_TEXT)


# ── the four routes ─────────────────────────────────────────────────────────
def add_staff_signin(app: FastAPI, staff: StaffSignin) -> None:
    """Add the four routes of the flow and the page answers of the guard. Called
    after ``add_claimant_pages``, whose handler it wraps. All plain ``def``."""
    flow = staff.flow
    transaction = transaction_cookie_name(POPULATION)
    _wrap_refusals(app)

    @app.get(START_PATH, include_in_schema=False)
    def auth_start(request: Request) -> Response:
        # Asked for by hand, a download's path would be kept by the flow (it is a
        # clean path under the adjuster's pages): it goes through the same
        # turning into its claim's page as a refusal's does.
        wanted = request.query_params.get("return_to")
        return flow.start(None if wanted is None else _return_to(wanted), time.time())

    def failed_after_apply_error(error: Exception) -> Response:
        """The session could not be sealed after all (rule 6): the person is
        sent to the failure page, the transaction is cleared all the same, and
        only the exception's class is logged."""
        staff.say(
            "apply-failed",
            f"the staff session could not be set: {type(error).__name__}",
        )
        response = _redirect(FAILED_PATH)
        flow.clear_transaction(response)
        return response

    @app.get(CALLBACK_PATH, include_in_schema=False)
    def auth_callback(request: Request) -> Response:
        if not staff.permits.acquire(blocking=False):
            staff.say("no-permit", "sign-in callbacks are being shed: no permit free")
            return _redirect(FAILED_PATH)
        try:
            outcome = flow.finish(
                request.query_params, request.cookies.get(transaction), time.time()
            )
        finally:
            staff.permits.release()
        response = _redirect(outcome.return_to if outcome.signed_in else FAILED_PATH)
        try:
            outcome.apply_to(response)
        except (SigninRefusal, ValueError) as error:
            return failed_after_apply_error(error)
        return response

    @app.get(FAILED_PATH, include_in_schema=False)
    def auth_failed() -> Response:
        return _page(
            HTTP_UNAUTHORIZED,
            FAILED_TEXT,
            back_path=START_PATH,
            back_label=SIGN_IN_AGAIN,
        )

    @app.post(
        SIGN_OUT_PATH,
        include_in_schema=False,
        dependencies=[Depends(require_same_origin)],
    )
    def auth_sign_out() -> Response:
        return flow.sign_out()
