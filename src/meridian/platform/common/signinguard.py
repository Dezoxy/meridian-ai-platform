"""The FastAPI side of sign-in: a dependency that returns the principal, one
that also needs a role, and the walk that reports what guards each route (S021,
T-05). Designed for S021 and wired to no route: no service uses it yet, and the
switch that would turn sign-in on is off.

``Signin`` holds one population's settings and key set. Its dependencies are
plain ``def`` functions, which FastAPI runs in a worker thread: the key fetch
blocks (see ``signinkeys``).

What a request may present. An ``Authorization`` header means a bearer token,
and only that: a bad token is not rescued by a cookie next to it, and two
headers are refused as ambiguous. With no header, the population's session
cookie. Neither: refused. A cookie whose issuer is not this realm's is refused,
so a change of issuer ends the sessions of the old one. The principal says
which of the two it came from (``Principal.via``).

How it answers. A refusal is an ``HTTPException`` with the shared fixed text
(``REFUSED``) and the status 401, 403 for a principal without the role, or 503
when the issuer's keys cannot be had. The body is the same for every cause: it
never says whether the signature, the audience or the expiry failed. A 401 for
the JSON form carries ``WWW-Authenticate: Bearer``; a page's does not, because a
browser would answer it with a login box, and the 403 and the 503 carry none.
Turning a 401 into a redirect to the issuer belongs to the page flow, which
does not exist yet.

What a refusal leaves. The reason (never the subject, a role or a header) goes
to the log through a throttle, one line per reason per window with the count of
the refusals the window held back, and to ``on_refusal`` when one is given: it
is called with the reason and the request before the answer is made, and when it
raises the request is refused all the same (a hook that fails, such as an audit
write, must not turn a refusal into an answer) and its failure is logged by
class name only.

What the walk reports. ``route_reports`` lists every route of an app with its
methods, whether a sign-in dependency made by ``Signin.principal`` guards it,
the roles ``Signin.require_role`` demands of it and the populations of the
guards. A route that has no method of its own is named by one label: a mount
as ``MOUNT /path`` and a websocket as ``WEBSOCKET /path``, and a route that only
answers ``HEAD`` or ``OPTIONS`` is reported under those methods. A mount's
routes are not read. ``unguarded_routes`` is for a test that walks an app and
names every route that has no sign-in dependency and is not on a closed list; it
also names each entry of ``app.dependency_overrides``, because an override
replaces a guard without the route table showing it. Only a dependency made by
``Signin`` counts: one that carries the marker attribute without having been
made by it does not.
"""

import logging
import time
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.routing import RouteContext, iter_route_contexts
from starlette.exceptions import HTTPException
from starlette.routing import Mount, WebSocketRoute

from meridian.platform.common.http import REFUSED
from meridian.platform.common.signin import (
    Forbidden,
    Principal,
    Reason,
    SigninRefusal,
    SigninSettings,
    Unauthenticated,
    check_bearer,
)
from meridian.platform.common.signinkeys import Clock, KeySet
from meridian.platform.common.signinsession import (
    SessionSettings,
    cookie_name,
    open_session,
)
from meridian.platform.common.throttle import RefusalAuditThrottle

logger = logging.getLogger(__name__)

AUTHENTICATE_HEADER = {"WWW-Authenticate": "Bearer"}
# Set on every dependency ``Signin.principal`` makes, to a ``_Guard``; and, on
# the dependency ``Signin.require_role`` makes, ``ROLE_ATTRIBUTE`` to a
# ``_RoleDemand``. The walk reads both, and takes a value of another type (a
# ``True`` set by hand) for no marker.
GUARD_ATTRIBUTE = "meridian_signin_guard"
ROLE_ATTRIBUTE = "meridian_signin_role"
# The methods Starlette adds to a route that the walk does not list, unless a
# route has no other.
IMPLIED_METHODS = frozenset({"HEAD", "OPTIONS"})
MOUNT_LABEL = "MOUNT"
WEBSOCKET_LABEL = "WEBSOCKET"
# A route that answers every method and is neither: a class-based ASGI route.
ANY_LABEL = "ANY"
OVERRIDE_LABEL = "DEPENDENCY_OVERRIDE"

type Dependency = Callable[..., Principal]
# Called with the reason and the request of every refusal, before the answer.
type OnRefusal = Callable[[Reason, Request], None]


@dataclass(frozen=True, slots=True)
class _Guard:
    """What ``Signin.principal``'s dependency carries: whose sign-in it is."""

    population: str


@dataclass(frozen=True, slots=True)
class _RoleDemand:
    """What ``Signin.require_role``'s dependency carries: the role it demands.
    The dependency under it carries the guard; this one does not, so a route
    that is only asked for a role is not thereby guarded."""

    role: str


class Signin:
    """The sign-in of one population on one app."""

    def __init__(
        self,
        settings: SigninSettings,
        keys: KeySet,
        sessions: SessionSettings | None = None,
        clock: Clock = time.time,
        *,
        on_refusal: OnRefusal | None = None,
    ) -> None:
        self.settings = settings
        self._keys = keys
        self._sessions = sessions
        self._clock = clock
        self._on_refusal = on_refusal
        # One log line per reason per window; the key is bounded by the reasons.
        self._throttle = RefusalAuditThrottle(clock)
        self._dependencies = {
            True: self._make_dependency(json_form=True),
            False: self._make_dependency(json_form=False),
        }

    def principal(self, *, json_form: bool = True) -> Dependency:
        """A dependency that returns the principal of the request or answers 401
        (503 when the issuer's keys cannot be had). The same function every time
        for a form, so FastAPI resolves it once per request however many routes'
        dependencies name it."""
        return self._dependencies[json_form]

    def require_role(self, role: str, *, json_form: bool = True) -> Dependency:
        """A dependency that returns the principal when it holds ``role``, and
        answers 403 when it does not (401 as ``principal`` does). The walk reads
        the role off it."""
        authenticated = self._dependencies[json_form]

        def with_role(
            request: Request,
            principal: Annotated[Principal, Depends(authenticated)],
        ) -> Principal:
            if not principal.has_role(role):
                raise self._refuse(Forbidden(Reason.NO_ROLE), request, json_form)
            return principal

        setattr(with_role, ROLE_ATTRIBUTE, _RoleDemand(role))
        return with_role

    def _make_dependency(self, *, json_form: bool) -> Dependency:
        def authenticated(request: Request) -> Principal:
            outcome = self._outcome(request)
            if isinstance(outcome, SigninRefusal):
                raise self._refuse(outcome, request, json_form)
            return outcome

        setattr(authenticated, GUARD_ATTRIBUTE, _Guard(self.settings.population))
        return authenticated

    def _outcome(self, request: Request) -> Principal | SigninRefusal:
        """The principal, or the refusal (returned, so that no exception from the
        libraries is on the way to the handler)."""
        now = self._clock()
        headers = request.headers.getlist("authorization")
        try:
            if len(headers) > 1:
                return Unauthenticated(Reason.MALFORMED)
            if headers:
                token = _bearer_token_of(headers[0])
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

    def _refuse(
        self, refusal: SigninRefusal, request: Request, json_form: bool
    ) -> HTTPException:
        """Log the refusal, tell the hook, and make the answer. The hook cannot
        stop the answer: whatever it raises, the request is refused."""
        self._log(logging.WARNING, refusal.reason.value, "sign-in refused: %s")
        if self._on_refusal is not None:
            try:
                self._on_refusal(refusal.reason, request)
            except Exception as error:
                # The class and nothing else: the text can quote what the hook
                # was working with.
                self._log(
                    logging.ERROR,
                    type(error).__name__,
                    "the hook of a sign-in refusal failed: %s",
                    key="hook-failed",
                )
        return _answer(refusal, json_form)

    def _log(self, level: int, word: str, message: str, *, key: str = "") -> None:
        """One line per key per window, with the count of the lines held back.
        The key is the reason by default; the word is the reason's code or an
        exception's class, never a subject, a role or a header."""
        carried = self._throttle.due(None, key or word)
        if carried is None:
            return
        extra = f" (and {carried} more since the last line)" if carried else ""
        logger.log(level, message + extra, word)


def _bearer_token_of(authorization: str) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` value, or None when it
    is another scheme or has no token or more than one."""
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer" or not rest or " " in rest:
        return None
    return rest


def _answer(refusal: SigninRefusal, json_form: bool) -> HTTPException:
    headers = AUTHENTICATE_HEADER if json_form and refusal.status == 401 else None
    return HTTPException(refusal.status, REFUSED, headers=headers)


@dataclass(frozen=True, slots=True)
class RouteReport:
    """What the walk found at one route. ``labels`` are the names a closed list
    of open routes uses: ``"GET /path"`` for each method, ``"MOUNT /path"`` or
    ``"WEBSOCKET /path"``. ``methods`` is empty for those two. ``roles`` are all
    the roles demanded (a request must hold every one) and ``populations`` the
    populations of the guards in the route's dependencies."""

    path: str
    labels: tuple[str, ...]
    methods: tuple[str, ...]
    guarded: bool
    roles: frozenset[str]
    populations: frozenset[str]


def _marks(dependant: object) -> Iterator[object]:
    """Every marker on the calls in a dependency tree."""
    call = getattr(dependant, "call", None)
    for attribute in (GUARD_ATTRIBUTE, ROLE_ATTRIBUTE):
        mark = getattr(call, attribute, None)
        if mark is not None:
            yield mark
    for sub in getattr(dependant, "dependencies", ()):
        yield from _marks(sub)


def _methods_of(context: RouteContext) -> tuple[str, ...] | None:
    """The methods a route is listed under: without the ones Starlette adds, but
    a route with no other is listed under what it has. None: no method of its
    own (a mount, a websocket, a route for every method)."""
    methods = context.methods
    if methods is None:
        return None
    return tuple(sorted((methods - IMPLIED_METHODS) or methods))


def _labels(context: RouteContext, methods: tuple[str, ...] | None) -> tuple[str, ...]:
    path = context.path or ""
    if methods is not None:
        return tuple(f"{method} {path}" for method in methods)
    original = context.original_route
    if isinstance(original, WebSocketRoute):
        return (f"{WEBSOCKET_LABEL} {path}",)
    if isinstance(original, Mount):
        return (f"{MOUNT_LABEL} {path}",)
    return (f"{ANY_LABEL} {path}",)


def route_reports(app: FastAPI) -> list[RouteReport]:
    """What guards each route of ``app``, in the order of the route table.

    Routes are read the way FastAPI's own OpenAPI generator reads them
    (``iter_route_contexts``), so a route of an included router is seen with the
    dependencies of the router and of the ``include_router`` call."""
    reports: list[RouteReport] = []
    for context in iter_route_contexts(app.routes):
        marks = list(_marks(getattr(context, "dependant", None)))
        guards = [mark for mark in marks if isinstance(mark, _Guard)]
        demands = [mark for mark in marks if isinstance(mark, _RoleDemand)]
        methods = _methods_of(context)
        reports.append(
            RouteReport(
                path=context.path or "",
                labels=_labels(context, methods),
                methods=methods or (),
                guarded=bool(guards),
                roles=frozenset(demand.role for demand in demands),
                populations=frozenset(guard.population for guard in guards),
            )
        )
    return reports


def unguarded_routes(app: FastAPI, open_routes: Collection[str]) -> list[str]:
    """The routes of ``app`` that no sign-in dependency guards and ``open_routes``
    does not allow, as ``"GET /path"`` (a mount as ``"MOUNT /path"``, a websocket
    as ``"WEBSOCKET /path"``), and one ``"DEPENDENCY_OVERRIDE <name>"`` for each
    entry of ``app.dependency_overrides`` (a closed list does not allow those),
    sorted. FastAPI's own ``/openapi.json`` and ``/docs`` are routes too. A test
    asserts that the list is empty, with the closed list of open routes written
    in it. That a route is guarded says nothing about its role: a test of the
    routes that change state reads ``route_reports`` for the roles."""
    named = frozenset(open_routes)
    found = [
        label
        for report in route_reports(app)
        if not report.guarded
        for label in report.labels
        if label not in named
    ]
    found.extend(
        f"{OVERRIDE_LABEL} {_name_of(dependency)}"
        for dependency in app.dependency_overrides
    )
    return sorted(found)


def _name_of(dependency: object) -> str:
    return str(getattr(dependency, "__qualname__", type(dependency).__name__))
