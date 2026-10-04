"""Who is calling: the identity a service reads from the caller's certificate,
the check every request but the health probe passes, and what a caller may
name (S055).

``PeerCertProtocol`` (``peercert.py``) puts the URI SANs of the verified
client certificate in the scope; ``CallerIdentityMiddleware`` turns them into
one of the registry's services or refuses the call. Trust is the CA that
signed the certificate, not the prefix: the prefix is a strict parse.
Refusals carry a fixed body and never echo the certificate.

Each service installs the check on its app with ``install_caller_check`` and
audits a refusal through its own audit path, one row per window
(``audited_refusals``).
"""

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

import anyio.to_thread
from pydantic import AfterValidator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from meridian.platform.common.env import SettingsError, require_env
from meridian.platform.common.http import (
    HEALTH_PATH,
    MAX_ID_LENGTH,
    REFUSED,
    error_answer,
)
from meridian.platform.common.peercert import CLIENT_CERT_URIS, TLS_EXTENSION
from meridian.platform.common.throttle import RefusalAuditThrottle
from meridian.platform.registry import Registry
from meridian.platform.registry.models import ENTITY_ID_PATTERN, Service

logger = logging.getLogger(__name__)

SERVICE_ID = re.compile(ENTITY_ID_PATTERN)
CALLER_STATE_KEY = "caller_service"
ANONYMOUS = "anonymous"
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
# A websocket close for a policy violation (RFC 6455).
WEBSOCKET_POLICY_VIOLATION = 1008
# The variable every service reads for the prefix of its callers' URIs.
IDENTITY_PREFIX_ENV = "MERIDIAN_IDENTITY_PREFIX"
SPIFFE_SCHEME = "spiffe://"
# What the audit says of a refused call, next to the reasons of the service's
# other refusals (``tool-not-allowed``, ``agent-not-allowed``).
AUDIT_REASON_PREFIX = "caller-"
# The reason of a caller that names a tenant or an agent it may not.
NAME_REFUSAL_REASON = AUDIT_REASON_PREFIX + "name-not-allowed"


class Refusal(StrEnum):
    """Why a call was refused; the audit's reason."""

    NO_IDENTITY = "no-identity"
    UNKNOWN_SERVICE = "unknown-service"
    NOT_ALLOWED = "not-allowed"


# What the service's audit is told: the reason and the caller's service ID, or
# None when the caller had no identity.
type OnRefusal = Callable[[Refusal, str | None], None]
# What a service writes for a refusal: the audit reason, the caller's service ID
# (cut to an ID's length) or None, and the refusals the row stands in for.
type WriteRefusal = Callable[[str, str | None, int], None]


def identity_prefix_problem(value: str) -> str | None:
    """The rule ``value`` breaks as a text that never holds the value, or
    ``None`` when it is a prefix a service can use: a SPIFFE URI that ends in
    ``/``, so that what follows it is the service's ID."""
    if not value.startswith(SPIFFE_SCHEME) or not value.endswith("/"):
        return f"must start with {SPIFFE_SCHEME} and end with /"
    return None


def _require_identity_prefix(value: str) -> str:
    # The value is left out of the message.
    problem = identity_prefix_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value


# For a settings field that holds the prefix of the callers' URIs.
IdentityPrefix = Annotated[str, AfterValidator(_require_identity_prefix)]


def identity_prefix_from(environ: Mapping[str, str]) -> str:
    """The prefix a service's ``from_env`` reads; raise ``SettingsError``
    naming the variable when it is missing or breaks the rule, never showing
    its value. It is required: no environment turns the check off."""
    value = require_env(environ, IDENTITY_PREFIX_ENV)
    problem = identity_prefix_problem(value)
    if problem is not None:
        raise SettingsError(f"{IDENTITY_PREFIX_ENV} {problem}")
    return value


@dataclass(frozen=True, slots=True)
class CallerPolicy:
    """What a service checks a caller against: its own ID, the prefix of its
    callers' URIs (up to and including ``/sa/``) and the registry's services."""

    service_id: str
    prefix: str
    services: tuple[Service, ...]

    def caller(self, service_id: str) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)


def service_id_from(uris: Iterable[str], prefix: str) -> str | None:
    """The caller's service ID: what follows ``prefix`` in the one URI that
    starts with it. No such URI, more than one (even the same twice), or a rest
    that is not a registry ID: no identity."""
    ours = [uri for uri in uris if uri.startswith(prefix)]
    if len(ours) != 1:
        return None
    candidate = ours[0].removeprefix(prefix)
    return candidate if SERVICE_ID.fullmatch(candidate) else None


def may_name(service: Service, tenant: str, agent: str) -> bool:
    """The service lists the tenant and the agent it names in a call."""
    return tenant in service.tenants and agent in service.agents


def caller_service(request: Request) -> Service | None:
    """The caller the middleware let through; ``None`` where it did not run (a
    health probe, or an app built without a policy)."""
    state = request.scope.get("state") or {}
    caller = state.get(CALLER_STATE_KEY)
    return caller if isinstance(caller, Service) else None


def caller_policy(
    prefix: str | None, service_id: str, registry: Registry
) -> CallerPolicy | None:
    """A service's policy from its own registry ID, the prefix and the registry
    it loaded; none without a prefix (an app built in code). An ID the registry
    does not hold stops the start."""
    if prefix is None:
        return None
    if registry.service(service_id) is None:
        raise SettingsError(f"the registry has no service {service_id!r}")
    return CallerPolicy(service_id, prefix, registry.services)


def audit_reason(reason: Refusal) -> str:
    return AUDIT_REASON_PREFIX + reason.value


def audited_refusals(throttle: RefusalAuditThrottle, write: WriteRefusal) -> OnRefusal:
    """The ``on_refusal`` of a service: ``write`` the row of a refusal when the
    throttle says it is due, so a flood leaves one row per window and not one
    per request (T-49). The window's key is the reason and, for a caller the
    registry maps, its ID; the ID of an unknown caller is a certificate's text
    and never a key, so the map is bounded by the registry. A write that fails
    releases the window and the error goes to the caller of this function."""

    def on_refusal(reason: Refusal, service_id: str | None) -> None:
        word = audit_reason(reason)
        known = service_id if reason is Refusal.NOT_ALLOWED else None
        key = f"{known or '-'}/{word}"
        carried = throttle.due(None, key)
        if carried is None:
            return
        try:
            write(
                word,
                None if service_id is None else service_id[:MAX_ID_LENGTH],
                carried,
            )
        except BaseException:
            throttle.release(None, key, carried)
            raise

    return on_refusal


def _uris(scope: Scope) -> tuple[str, ...]:
    tls = (scope.get("extensions") or {}).get(TLS_EXTENSION) or {}
    return tuple(tls.get(CLIENT_CERT_URIS) or ())


def _is_health_probe(scope: Scope) -> bool:
    return scope["method"] == "GET" and scope["path"] == HEALTH_PATH


class CallerIdentityMiddleware:
    """Refuse every HTTP request but ``GET /healthz`` that does not come from a
    service the registry maps and that may call this one.

    401 for no identity, 403 for a service that is unknown or does not list
    this one in its ``calls``, both with the same fixed body. The caller's
    ``Service`` goes into ``scope["state"]["caller_service"]`` of a new scope;
    the one given is not changed. A refusal is logged with its reason and the
    caller's service ID, and handed to ``on_refusal``. A websocket, which no
    service has, is refused too.
    """

    def __init__(
        self,
        app: ASGIApp,
        policy: CallerPolicy,
        on_refusal: OnRefusal | None = None,
    ) -> None:
        self.app = app
        self.policy = policy
        self.on_refusal = on_refusal

    async def _note(self, reason: Refusal, service_id: str | None) -> None:
        """Log the refusal and hand it to the audit, which writes to a database:
        in a worker thread, and waited for. A callback that fails changes
        nothing the caller sees; only its class is logged."""
        logger.warning(
            "call refused: %s (caller %s)", reason.value, service_id or ANONYMOUS
        )
        if self.on_refusal is None:
            return
        try:
            await anyio.to_thread.run_sync(self.on_refusal, reason, service_id)
        except Exception as exc:
            logger.error("the audit of a refused call failed: %s", type(exc).__name__)

    async def _refuse(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        reason: Refusal,
        service_id: str | None,
    ) -> None:
        await self._note(reason, service_id)
        status = HTTP_UNAUTHORIZED if reason is Refusal.NO_IDENTITY else HTTP_FORBIDDEN
        await error_answer(status, REFUSED)(scope, receive, send)

    async def _refuse_stream(self, scope: Scope, send: Send) -> None:
        """A scope that is not HTTP has no identity (the protocol class gives
        only HTTP scopes one), and no service has a websocket."""
        await self._note(Refusal.NO_IDENTITY, None)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": WEBSOCKET_POLICY_VIOLATION})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self._refuse_stream(scope, send)
            return
        if _is_health_probe(scope):
            await self.app(scope, receive, send)
            return
        service_id = service_id_from(_uris(scope), self.policy.prefix)
        if service_id is None:
            await self._refuse(scope, receive, send, Refusal.NO_IDENTITY, None)
            return
        caller = self.policy.caller(service_id)
        if caller is None:
            await self._refuse(
                scope, receive, send, Refusal.UNKNOWN_SERVICE, service_id
            )
            return
        if self.policy.service_id not in caller.calls:
            await self._refuse(scope, receive, send, Refusal.NOT_ALLOWED, service_id)
            return
        state = {**(scope.get("state") or {}), CALLER_STATE_KEY: caller}
        await self.app({**scope, "state": state}, receive, send)


def install_caller_check(
    app: Starlette, policy: CallerPolicy | None, on_refusal: OnRefusal | None
) -> None:
    """Put the check on ``app``, outside every middleware it has so far: an
    anonymous call is refused before a body is read or limited, and inside the
    FastAPI instrumentation, which wraps the stack when it is built, so a
    refusal has its server span. No policy (an app built in code without a
    prefix) installs nothing."""
    if policy is not None:
        app.add_middleware(
            CallerIdentityMiddleware, policy=policy, on_refusal=on_refusal
        )
