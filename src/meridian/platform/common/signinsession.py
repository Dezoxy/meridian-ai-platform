"""The session cookie: a signed value that holds what the pages need and no
token (S021, T-05).

After a person signs in, the app sets one cookie. It holds the population, the
issuer, the subject, the roles and the expiry, and nothing else: no access
token, no ID token, no refresh token. There is no server-side store, so the app
cannot end a session early; it can only let it run out, which is why the expiry
is the token's and is capped (``MAX_SESSION_SECONDS``), and why the check of
the expiry, the signature and the population runs on every request.

Format: ``v1.<payload>.<mac>``. The payload is compact JSON, base64url without
padding; the MAC is HMAC-SHA-256 of ``v1.<payload>`` under the key, base64url
without padding. The MAC is compared as text, in constant time, with the one
the app computes: that makes the encoding canonical (a cookie with a changed
bit in the unused end of its last base64 character, or with a character
appended, is a different text and fails), and it makes a truncated or extended
value fail without a decoder to be careful about. The payload is parsed only
after the MAC holds.

Two keys may be given: the current one signs, both verify, so a key is rotated
by making the old one the previous one and ending it after one session length.
Every key is tried on every cookie, whichever matches, so the time taken does
not say which key signed it.

The cookie's attributes are a function of one setting (``SessionSettings``):
``HttpOnly``, ``SameSite=Lax``, ``Path=/`` and ``Secure``, unless the edge is
plain HTTP, which is accepted only on kind.
"""

import base64
import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from starlette.responses import Response

from meridian.platform.common.env import SettingsError
from meridian.platform.common.signin import (
    MAX_ROLE_CHARS,
    MAX_ROLES,
    MAX_SUBJECT_CHARS,
    Population,
    Principal,
    Reason,
    Unauthenticated,
)

SESSION_KEY_ENV = "MERIDIAN_SESSION_KEY"
SESSION_KEY_PREVIOUS_ENV = "MERIDIAN_SESSION_KEY_PREVIOUS"
EDGE_PLAIN_HTTP_ENV = "MERIDIAN_SESSION_EDGE_PLAIN_HTTP"
# The variable the gateway reads for the same fact (``MERIDIAN_ENVIRONMENT``);
# it is named here so that ``common`` does not import the gateway.
ENVIRONMENT_ENV = "MERIDIAN_ENVIRONMENT"
KIND_ENVIRONMENT = "kind"
# The version in front of the value. A change of format is a new number, and a
# cookie of the old one is refused, so every session ends once and none is
# misread.
VERSION = "v1"
# HMAC-SHA-256's own output size: a key shorter than the hash carries less
# than the hash's strength. ``openssl rand -base64 48`` makes 64 characters.
MIN_SESSION_KEY_BYTES = 32
# The most a session lasts whatever the token says: a working day. The cookie
# ends with the token's life when that is shorter, which is the usual case
# (an Entra access token lives about an hour, no refresh is made).
MAX_SESSION_SECONDS = 8 * 3600
# What a browser guarantees to keep for one cookie (RFC 6265, section 6.1). The
# bounds on the subject, the issuer URL and the roles keep a real cookie well
# inside it.
MAX_COOKIE_BYTES = 4096
PAYLOAD_FIELDS = frozenset({"p", "i", "s", "r", "e"})
COOKIE_NAME_PREFIX = "meridian_session_"
PLAIN_HTTP_ONLY_ON_KIND = (
    f"{EDGE_PLAIN_HTTP_ENV} is accepted only with {ENVIRONMENT_ENV}={KIND_ENVIRONMENT}"
)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True, slots=True)
class SessionKeys:
    """The key that signs and, if any, the one that still verifies. Neither is
    in ``repr``."""

    current: bytes = field(repr=False)
    previous: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for key in self.all():
            if len(key) < MIN_SESSION_KEY_BYTES:
                raise SettingsError(
                    f"a session key must be at least {MIN_SESSION_KEY_BYTES} bytes"
                )
        if self.previous is not None and self.previous == self.current:
            raise SettingsError("the previous session key must differ from the current")

    def all(self) -> tuple[bytes, ...]:
        return (
            (self.current,) if self.previous is None else (self.current, self.previous)
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Self:
        """The keys from ``MERIDIAN_SESSION_KEY`` and, optionally,
        ``MERIDIAN_SESSION_KEY_PREVIOUS``, as the bytes of the text. Raise
        ``SettingsError`` naming the variable; never the value."""
        current = environ.get(SESSION_KEY_ENV)
        if not current:
            raise SettingsError(f"{SESSION_KEY_ENV} is required")
        previous = environ.get(SESSION_KEY_PREVIOUS_ENV) or None
        return cls(
            current=current.encode("utf-8"),
            previous=None if previous is None else previous.encode("utf-8"),
        )


@dataclass(frozen=True, slots=True)
class SessionSettings:
    """The keys, and whether the cookie is ``Secure``."""

    keys: SessionKeys
    secure: bool = True

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Self:
        """Read the keys and ``MERIDIAN_SESSION_EDGE_PLAIN_HTTP`` (``true`` or
        ``false``, default false). A plain-HTTP edge is accepted only when
        ``MERIDIAN_ENVIRONMENT`` is ``kind``: any other combination stops the
        start with one fixed sentence (T-06)."""
        keys = SessionKeys.from_env(environ)
        flag = (environ.get(EDGE_PLAIN_HTTP_ENV) or "false").lower()
        if flag not in ("true", "false"):
            raise SettingsError(f"{EDGE_PLAIN_HTTP_ENV} must be true or false")
        plain_http = flag == "true"
        if plain_http and environ.get(ENVIRONMENT_ENV) != KIND_ENVIRONMENT:
            raise SettingsError(PLAIN_HTTP_ONLY_ON_KIND)
        return cls(keys=keys, secure=not plain_http)


def cookie_name(population: str) -> str:
    """One name per population, so a staff session and a claimant's, on one
    host, do not replace each other."""
    return COOKIE_NAME_PREFIX + population


def cookie_attributes(settings: SessionSettings) -> dict[str, Any]:
    """The attributes of the session cookie, as ``Response.set_cookie`` takes
    them."""
    return {
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "secure": settings.secure,
    }


def set_session_cookie(
    response: Response,
    value: str,
    principal: Principal,
    settings: SessionSettings,
    now: float,
) -> None:
    """Set the cookie ``seal_session`` made for ``principal`` on ``response``;
    the browser drops it when the session ends."""
    remaining = min(principal.expires_at - math.floor(now), MAX_SESSION_SECONDS)
    response.set_cookie(
        cookie_name(principal.population),
        value,
        max_age=max(0, remaining),
        **cookie_attributes(settings),
    )


def clear_session_cookie(
    response: Response, population: Population, settings: SessionSettings
) -> None:
    """Ask the browser to forget the cookie, with the same attributes."""
    response.delete_cookie(cookie_name(population), **cookie_attributes(settings))


def _mac(key: bytes, body: str) -> str:
    return _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())


def seal_session(principal: Principal, keys: SessionKeys, now: float) -> str:
    """The cookie value for ``principal``: signed with the current key, ending
    with the principal's expiry or ``MAX_SESSION_SECONDS`` from ``now``,
    whichever is sooner. Raise ``Unauthenticated`` for a principal that has
    already expired, ``ValueError`` (a fixed text) for one too large to fit."""
    ends = min(principal.expires_at, math.floor(now) + MAX_SESSION_SECONDS)
    if ends <= now:
        raise Unauthenticated(Reason.EXPIRED)
    payload = {
        "p": principal.population,
        "i": principal.issuer,
        "s": principal.subject,
        "r": sorted(principal.roles),
        "e": ends,
    }
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    body = f"{VERSION}.{_b64(text.encode('utf-8'))}"
    cookie = f"{body}.{_mac(keys.current, body)}"
    if len(cookie) > MAX_COOKIE_BYTES:
        raise ValueError("the session is too large for a cookie")
    return cookie


def _altered() -> Unauthenticated:
    return Unauthenticated(Reason.SESSION_ALTERED)


def _signed(cookie: str, keys: SessionKeys) -> str | Reason:
    """The base64 payload of a cookie whose MAC holds under one of the keys, or
    why not."""
    if not cookie.isascii() or len(cookie) > MAX_COOKIE_BYTES:
        return Reason.SESSION_ALTERED
    version, _, rest = cookie.partition(".")
    if version != VERSION:
        return Reason.SESSION_VERSION
    payload, dot, mac = rest.partition(".")
    if not dot or not payload or "." in mac:
        return Reason.SESSION_ALTERED
    body = f"{version}.{payload}"
    given = mac.encode("ascii")
    # Every key is compared, with no early exit, so the time says nothing about
    # which key signed the value.
    verdicts = [
        hmac.compare_digest(_mac(key, body).encode("ascii"), given)
        for key in keys.all()
    ]
    return payload if any(verdicts) else Reason.SESSION_ALTERED


def _principal_of(payload: str) -> Principal | None:
    """The principal in a payload whose MAC held. A payload this app signed has
    the right shape; one that does not (a bug, or an older release's) is
    refused the same way."""
    try:
        data = json.loads(_unb64(payload))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.keys() != PAYLOAD_FIELDS:
        return None
    population, issuer, subject = data["p"], data["i"], data["s"]
    roles, ends = data["r"], data["e"]
    if not all(isinstance(v, str) for v in (population, issuer, subject)):
        return None
    if not 0 < len(subject) <= MAX_SUBJECT_CHARS:
        return None
    if not isinstance(roles, list) or len(roles) > MAX_ROLES:
        return None
    if not all(isinstance(r, str) and 0 < len(r) <= MAX_ROLE_CHARS for r in roles):
        return None
    if isinstance(ends, bool) or not isinstance(ends, int):
        return None
    return Principal(
        population=population,
        issuer=issuer,
        subject=subject,
        roles=frozenset(roles),
        expires_at=ends,
    )


def open_session(
    cookie: str, keys: SessionKeys, now: float, population: Population
) -> Principal:
    """The principal in a cookie, or ``Unauthenticated``: refused when it is of
    another version, altered or cut short, over the size bound, expired, or the
    other population's. Nothing of the cookie is in the exception."""
    signed = _signed(cookie, keys)
    if isinstance(signed, Reason):
        raise Unauthenticated(signed)
    principal = _principal_of(signed)
    if principal is None:
        raise _altered()
    if principal.population != population:
        raise Unauthenticated(Reason.SESSION_POPULATION)
    if now >= principal.expires_at:
        raise Unauthenticated(Reason.EXPIRED)
    return principal
