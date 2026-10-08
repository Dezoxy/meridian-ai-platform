"""The session cookie: a signed value that holds what the pages need and no
token (S021, T-05). The Claims API seals and opens it for the staff routes and
pages when ``MERIDIAN_SIGNIN`` is ``staff`` (``staff_signin``), off by default;
no other service uses it.

When a route uses it, a person who has signed in gets one cookie. It holds the
population, the issuer, the subject, the roles and the expiry, and nothing else:
no access token, no ID token, no refresh token. There is no server-side store,
so a session cannot be ended early; it can only run out, which is why the
expiry is the token's and is capped (``MAX_SESSION_SECONDS``) both when the
cookie is sealed and when it is opened (a cookie signed with a key that leaked
is capped too), and why the check of the expiry, the signature and the
population runs on every request.

Format: ``v1.<payload>.<mac>``. The payload is compact JSON, base64url without
padding; the MAC is HMAC-SHA-256 of ``v1.<payload>`` under the key, base64url
without padding. The MAC is compared as text, in constant time, with the one
the module computes: that makes the encoding canonical (a cookie with a changed
bit in the unused end of its last base64 character, or with a character
appended, is a different text and fails), and it makes a truncated or extended
value fail without a decoder to be careful about. The payload is parsed only
after the MAC holds.

Two keys may be given: the current one signs, both verify, so a key is rotated
by making the old one the previous one and ending it after one session length.
Every key is tried on every cookie, whichever matches, so the time taken does
not say which key signed it.

A key is given as base64 of at least ``MIN_SESSION_KEY_BYTES`` random bytes (see
``SessionKeys.from_env``). What the start checks is the form and two floors: the
text is base64 (one alphabet), it decodes to at least ``MIN_SESSION_KEY_BYTES``
bytes, and those bytes hold at least ``MIN_DISTINCT_KEY_BYTES`` different values.
That refuses a short key and a repeated pattern (``aaaa...``, ``password``
repeated). It is a floor, not a proof of randomness: a phrase of 64 different
letters, or ``bytes(range(32))`` in base64, passes. Make the key with a random
source (``openssl rand -base64 48``).

The cookie's attributes are a function of one setting (``SessionSettings``):
``HttpOnly``, ``SameSite=Lax``, ``Path=/`` and ``Secure``, unless the edge is
plain HTTP, which is accepted only on kind.
"""

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from starlette.responses import Response

from meridian.platform.common.env import SettingsError
from meridian.platform.common.signin import (
    ENVIRONMENT_ENV,
    KIND_ENVIRONMENT,
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
# The version in front of the value. A change of format is a new number, and a
# cookie of the old one is refused, so every session ends once and none is
# misread.
VERSION = "v1"
# HMAC-SHA-256's own output size: a key shorter than the hash carries less
# than the hash's strength. ``openssl rand -base64 48`` makes 64 characters.
MIN_SESSION_KEY_BYTES = 32
# A random 32-byte key has about 30 different byte values; a long run of one
# repeated character, which is valid base64, has a handful.
MIN_DISTINCT_KEY_BYTES = 16
# One alphabet or the other, never both: standard base64 is what ``openssl rand
# -base64`` prints and what a Kubernetes Secret created from it holds; the URL-safe
# one is what ``python -c "import secrets; print(secrets.token_urlsafe(48))"``
# prints. Padding may be left off, as that command leaves it.
_STANDARD_BASE64 = r"[A-Za-z0-9+/]+={0,2}"
_URL_SAFE_BASE64 = r"[A-Za-z0-9_-]+={0,2}"
_KEY_TEXT = re.compile(f"{_STANDARD_BASE64}|{_URL_SAFE_BASE64}")
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


def _decoded_key(text: str) -> bytes | None:
    """The bytes of a base64 key text, or None when it is not base64 in one
    alphabet. Never raises, so no library error is on the way to a caller."""
    if not _KEY_TEXT.fullmatch(text):
        return None
    standard = text.replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(standard + "=" * (-len(standard) % 4), validate=True)
    except binascii.Error:
        return None


def _key_of(text: str, variable: str) -> bytes:
    """The key a variable's text holds. The error names the variable, not the
    text: it would be key material."""
    raw = _decoded_key(text.removesuffix("\n"))
    if raw is None or len(raw) < MIN_SESSION_KEY_BYTES:
        problem = f"{variable} must be base64 of at least {MIN_SESSION_KEY_BYTES} bytes"
    elif len(set(raw)) < MIN_DISTINCT_KEY_BYTES:
        problem = f"{variable} must be random bytes, not a repeated pattern"
    else:
        return raw
    raise SettingsError(problem)


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
        ``MERIDIAN_SESSION_KEY_PREVIOUS``. Each is base64 (standard or URL-safe,
        padding optional) of at least ``MIN_SESSION_KEY_BYTES`` bytes with at
        least ``MIN_DISTINCT_KEY_BYTES`` different values, and one trailing
        newline, which a mounted Secret file often has, is dropped first. Raise
        ``SettingsError`` naming the variable; never the value."""
        current = environ.get(SESSION_KEY_ENV)
        if not current:
            raise SettingsError(f"{SESSION_KEY_ENV} is required")
        previous = environ.get(SESSION_KEY_PREVIOUS_ENV) or None
        newest = _key_of(current, SESSION_KEY_ENV)
        if previous is None:
            return cls(current=newest)
        return cls(current=newest, previous=_key_of(previous, SESSION_KEY_PREVIOUS_ENV))


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


def _session_end(principal: Principal, now: float) -> int:
    """When a session for ``principal`` sealed at ``now`` ends: the principal's
    expiry or ``MAX_SESSION_SECONDS`` from ``now``, whichever is sooner."""
    return min(principal.expires_at, math.floor(now) + MAX_SESSION_SECONDS)


def set_session_cookie(
    response: Response,
    principal: Principal,
    settings: SessionSettings,
    now: float,
) -> None:
    """Seal a session for ``principal`` and set it on ``response``; the browser
    drops it when the session ends. The cookie and its ``Max-Age`` come from the
    same principal in one place, so they cannot be paired with another's. Raise
    what ``seal_session`` raises, so a principal that has ended gets no cookie."""
    value = seal_session(principal, settings.keys, now)
    response.set_cookie(
        cookie_name(principal.population),
        value,
        max_age=_session_end(principal, now) - math.floor(now),
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
    ends = _session_end(principal, now)
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
    """The principal in a payload whose MAC held. A payload this module signed has
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
        via="cookie",
    )


def open_session(
    cookie: str, keys: SessionKeys, now: float, population: Population
) -> Principal:
    """The principal in a cookie (``via`` is ``"cookie"``), or
    ``Unauthenticated``: refused when it is of another version, altered or cut
    short, over the size bound, expired, the other population's, or ends later
    than ``MAX_SESSION_SECONDS`` from ``now``, which no sealed cookie does
    (a cookie signed with a key that leaked, and kept as the previous one, is
    refused as well). Nothing of the cookie is in the exception."""
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
    if principal.expires_at > now + MAX_SESSION_SECONDS:
        raise Unauthenticated(Reason.SESSION_TOO_LONG)
    return principal
