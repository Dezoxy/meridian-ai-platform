"""Who is calling: the identity a service reads from the caller's certificate,
the check every request but the health probe passes, and what a caller may
name (S055).

``PeerCertProtocol`` (``peercert.py``) puts the URI SANs of the verified
client certificate in the scope; ``CallerIdentityMiddleware`` turns them into
one of the registry's services or refuses the call. Trust is the CA that
signed the certificate, not the prefix: the prefix is a strict parse.
Refusals carry a fixed body and never echo the certificate.
"""

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from meridian.platform.common.http import HEALTH_PATH, REFUSED, error_answer
from meridian.platform.common.peercert import CLIENT_CERT_URIS, TLS_EXTENSION
from meridian.platform.registry.models import ENTITY_ID_PATTERN, Service

logger = logging.getLogger(__name__)

SERVICE_ID = re.compile(ENTITY_ID_PATTERN)
CALLER_STATE_KEY = "caller_service"
ANONYMOUS = "anonymous"
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403


class Refusal(StrEnum):
    """Why a call was refused; the audit's reason."""

    NO_IDENTITY = "no-identity"
    UNKNOWN_SERVICE = "unknown-service"
    NOT_ALLOWED = "not-allowed"


# What the service's audit is told: the reason and the caller's service ID, or
# None when the caller had no identity.
type OnRefusal = Callable[[Refusal, str | None], None]


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
    caller's service ID, and handed to ``on_refusal``.
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

    async def _refuse(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        reason: Refusal,
        service_id: str | None,
    ) -> None:
        logger.warning(
            "call refused: %s (caller %s)", reason.value, service_id or ANONYMOUS
        )
        if self.on_refusal is not None:
            self.on_refusal(reason, service_id)
        status = HTTP_UNAUTHORIZED if reason is Refusal.NO_IDENTITY else HTTP_FORBIDDEN
        await error_answer(status, REFUSED)(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_health_probe(scope):
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
