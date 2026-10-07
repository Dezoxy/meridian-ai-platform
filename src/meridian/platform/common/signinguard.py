"""The FastAPI side of sign-in: a dependency that returns the principal, one
that also needs a role, and the walk that finds a route with neither (S021,
T-05). No route uses them yet.

``Signin`` holds one population's settings and key set. Its dependencies are
plain ``def`` functions, which FastAPI runs in a worker thread: the key fetch
blocks (see ``signinkeys``).

What a request may present. An ``Authorization`` header means a bearer token,
and only that: a bad token is not rescued by a cookie next to it, and two
headers are refused as ambiguous. With no header, the population's session
cookie. Neither: refused. A cookie whose issuer is not this realm's is refused,
so a change of issuer ends the sessions of the old one.

How it answers. A refusal is an ``HTTPException`` with the shared fixed text
(``REFUSED``) and the status 401, or 403 for a principal without the role. The
body is the same for every cause: it never says whether the signature, the
audience or the expiry failed (the reason is in the log line, which carries
nothing else). A 401 for the JSON form carries ``WWW-Authenticate: Bearer``; a
page's does not, because a browser would answer it with a login box. Turning a
401 into a redirect to the issuer is the page flow's (a later contract).

``unguarded_routes`` is for a test that walks an app and names every route that
has no sign-in dependency and is not on a closed list.
"""

import logging
import time
from collections.abc import Callable, Collection
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.routing import RouteContext, iter_route_contexts
from starlette.exceptions import HTTPException

from meridian.platform.common.http import REFUSED
from meridian.platform.common.signin import (
    Forbidden,
    Principal,
    Reason,
    SigninRefusal,
    SigninSettings,
    Unauthenticated,
    bearer_token_of,
    check_bearer,
)
from meridian.platform.common.signinkeys import KeySet
from meridian.platform.common.signinsession import (
    SessionSettings,
    cookie_name,
    open_session,
)

logger = logging.getLogger(__name__)

AUTHENTICATE_HEADER = {"WWW-Authenticate": "Bearer"}
# Set on every dependency this module makes; the route walk looks for it.
GUARD_ATTRIBUTE = "meridian_signin_guard"
# The methods Starlette adds to a route that the walk does not list.
IMPLIED_METHODS = frozenset({"HEAD", "OPTIONS"})

type Dependency = Callable[..., Principal]
type Clock = Callable[[], float]


def _marked[F: Callable[..., object]](function: F) -> F:
    setattr(function, GUARD_ATTRIBUTE, True)
    return function


class Signin:
    """The sign-in of one population on one app."""

    def __init__(
        self,
        settings: SigninSettings,
        keys: KeySet,
        sessions: SessionSettings | None = None,
        clock: Clock = time.time,
    ) -> None:
        self.settings = settings
        self._keys = keys
        self._sessions = sessions
        self._clock = clock
        self._dependencies = {
            True: self._make_dependency(json_form=True),
            False: self._make_dependency(json_form=False),
        }

    def principal(self, *, json_form: bool = True) -> Dependency:
        """A dependency that returns the principal of the request or answers 401.
        The same function every time for a form, so FastAPI resolves it once per
        request however many routes' dependencies name it."""
        return self._dependencies[json_form]

    def require_role(self, role: str, *, json_form: bool = True) -> Dependency:
        """A dependency that returns the principal when it holds ``role``, and
        answers 403 when it does not (401 as ``principal`` does)."""
        authenticated = self._dependencies[json_form]

        def with_role(
            principal: Annotated[Principal, Depends(authenticated)],
        ) -> Principal:
            if not principal.has_role(role):
                _log(Forbidden(Reason.NO_ROLE))
                raise _answer(Forbidden(Reason.NO_ROLE), json_form)
            return principal

        return _marked(with_role)

    def _make_dependency(self, *, json_form: bool) -> Dependency:
        def authenticated(request: Request) -> Principal:
            outcome = self._outcome(request)
            if isinstance(outcome, SigninRefusal):
                _log(outcome)
                raise _answer(outcome, json_form)
            return outcome

        return _marked(authenticated)

    def _outcome(self, request: Request) -> Principal | SigninRefusal:
        """The principal, or the refusal (returned, so that no exception from the
        libraries is on the way to the handler)."""
        now = self._clock()
        headers = request.headers.getlist("authorization")
        try:
            if len(headers) > 1:
                return Unauthenticated(Reason.MALFORMED)
            if headers:
                token = bearer_token_of(headers[0])
                if token is None:
                    return Unauthenticated(Reason.MALFORMED)
                return check_bearer(token, self.settings, self._keys, now)
            return self._from_cookie(request, now)
        except SigninRefusal as refusal:
            return refusal

    def _from_cookie(self, request: Request, now: float) -> Principal:
        population = self.settings.population
        cookie = request.cookies.get(cookie_name(population))
        if cookie is None or self._sessions is None:
            raise Unauthenticated(Reason.NO_CREDENTIAL)
        principal = open_session(cookie, self._sessions.keys, now, population)
        if principal.issuer != self.settings.issuer:
            raise Unauthenticated(Reason.ISSUER)
        return principal


def _log(refusal: SigninRefusal) -> None:
    # The reason's code and nothing else: no subject, no role, no header.
    logger.warning("sign-in refused: %s", refusal.reason.value)


def _answer(refusal: SigninRefusal, json_form: bool) -> HTTPException:
    headers = AUTHENTICATE_HEADER if json_form and refusal.status == 401 else None
    return HTTPException(refusal.status, REFUSED, headers=headers)


def _guarded(dependant: object) -> bool:
    """Whether a route's dependency tree holds a call made by this module."""
    call = getattr(dependant, "call", None)
    if getattr(call, GUARD_ATTRIBUTE, False):
        return True
    return any(_guarded(sub) for sub in getattr(dependant, "dependencies", ()))


def _labels(context: RouteContext) -> list[str]:
    path = context.path or ""
    if context.methods is None:
        # A mount or a websocket: no method to name.
        return [f"{type(context.original_route).__name__.upper()} {path}"]
    return [f"{m} {path}" for m in sorted(context.methods - IMPLIED_METHODS)]


def unguarded_routes(app: FastAPI, open_routes: Collection[str]) -> list[str]:
    """The routes of ``app`` that no sign-in dependency guards and ``open_routes``
    does not allow, as ``"GET /path"`` (a mount as ``"MOUNT /path"``), sorted.
    FastAPI's own ``/openapi.json`` and ``/docs`` are routes too. A test asserts
    that the list is empty, with the closed list of open routes written in it.

    Routes are read the way FastAPI's own OpenAPI generator reads them
    (``iter_route_contexts``), so a route of an included router is seen with the
    dependencies of the router and of the ``include_router`` call."""
    named = frozenset(open_routes)
    found: list[str] = []
    for context in iter_route_contexts(app.routes):
        if _guarded(getattr(context, "dependant", None)):
            continue
        found.extend(label for label in _labels(context) if label not in named)
    return sorted(found)
