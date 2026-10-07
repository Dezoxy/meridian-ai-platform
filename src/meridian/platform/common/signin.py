"""Who a person is: the check of a bearer token, and the types the session and
the routes share (S021, T-05). Designed for S021 and wired to no route: nothing
in the services calls it, and the switch that would turn sign-in on is off.

``check_bearer`` turns a raw access token into a ``Principal`` or raises a
refusal. It trusts one issuer (a realm), pinned by its settings. The order is
the point:

1. the token's size, before anything parses it;
2. the header, read without verifying anything, must say RS256 and name a key:
   ``none``, an HMAC algorithm (the old attack signs with the public key's
   bytes), an elliptic-curve or PSS algorithm and a header with no ``alg`` are
   refused here, before any key is looked at;
3. the key with that id, from the issuer's key set (``signinkeys``);
4. the signature with the algorithms of ``ALGORITHMS`` (a constant: never the
   token's own claim), and the issuer, the audience and the presence of the
   required claims (``REQUIRED_CLAIMS`` and the settings' subject claim);
5. the times, checked here against the ``now`` the caller gives (the library
   reads the clock itself and a test cannot move it), with ``LEEWAY_SECONDS``;
6. the kind of token, when the settings name one (``required_typ``,
   ``allowed_azp``): an ID token or a refresh token has the right issuer and
   audience too, and only its ``typ`` or ``azp`` tells it from an access token;
7. the subject, and the roles.

A refusal is an exception of a fixed text: the class says 401, 403 or 503, the
``Reason`` says why (for a log line, never for an answer). Nothing of the token
is in it, and it is raised outside the ``except`` that caught the library's
error, so the library's text, which can quote a claim, is not chained to it.
The roles claim must be a list of strings; anything else means no roles.
Whatever a token holds, ``check_bearer`` returns a ``Principal`` or raises one
of these classes, never another exception (a 500 would say that a token can
break the service).
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Self
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from meridian.platform.common.env import (
    HttpUrl,
    SettingsError,
    require_env,
    service_url_problem,
)
from meridian.platform.common.signinkeys import (
    KeySet,
    KeySetUnavailable,
    UnknownKeyId,
)

type Population = Literal["staff", "claimant"]
type Via = Literal["bearer", "cookie"]
POPULATIONS: tuple[Population, ...] = ("staff", "claimant")

# The variable that says which environment a process runs in, and the one
# environment where a plain-HTTP address is accepted. The gateway reads the same
# variable (``gateway.settings.ENVIRONMENT_ENV``) and ``common`` may not import
# the gateway, so the name is repeated here; a test ties the two copies.
ENVIRONMENT_ENV = "MERIDIAN_ENVIRONMENT"
KIND_ENVIRONMENT = "kind"

# The only algorithm a token may use. A constant of this module and not a
# setting, so that an environment cannot widen it.
ALGORITHMS = ("RS256",)
# What every token must carry besides the subject claim (a setting, ``sub`` by
# default). ``nbf`` is not here: the signature covers the claims, so nobody can
# strip it, and a token with ``exp`` and no ``nbf`` is valid from its issue.
# Requiring it only refuses tokens of an issuer that does not send it. It is
# checked when it is present.
REQUIRED_CLAIMS = ("exp", "iss", "aud")
# Clock skew allowed between the issuer and this process, on ``exp``, ``nbf``
# and ``iat``. Both run on one node on kind and on synchronised hosts on Azure;
# half a minute covers drift, and every second more is a second an expired token
# still opens a door.
LEEWAY_SECONDS = 30
# The longest token read. An Entra access token with app roles is about 1.5 KB;
# 8 KB leaves room for a long audience list and a few dozen roles, and stays
# under the 16 KB header limit proxies commonly apply.
MAX_TOKEN_CHARS = 8192
# Bounds on what a token puts into a principal, and so into a cookie (a browser
# keeps cookies up to 4096 bytes): Entra's object ID is 36 characters, Keycloak's
# subject is a UUID; a role value is a short name.
MAX_SUBJECT_CHARS = 128
MAX_ROLES = 32
MAX_ROLE_CHARS = 64
MAX_ALLOWED_AZP = 16
DEFAULT_ROLES_CLAIM = "roles"
DEFAULT_SUBJECT_CLAIM = "sub"
SIGNIN_ENV_PREFIX = "MERIDIAN_SIGNIN_"


class Reason(StrEnum):
    """Why a credential was refused. For a log line; never part of an answer,
    so a caller cannot tell a wrong audience from a wrong signature."""

    NO_CREDENTIAL = "no-credential"
    MALFORMED = "malformed"
    TOO_LARGE = "too-large"
    ALGORITHM = "algorithm"
    NO_KEY_ID = "no-key-id"
    UNKNOWN_KEY = "unknown-key"
    KEYS_UNAVAILABLE = "keys-unavailable"
    SIGNATURE = "signature"
    ISSUER = "issuer"
    AUDIENCE = "audience"
    CLAIMS = "claims"
    EXPIRED = "expired"
    NOT_YET_VALID = "not-yet-valid"
    WRONG_TYPE = "token-type"
    AUTHORIZED_PARTY = "authorized-party"
    SESSION_VERSION = "session-version"
    SESSION_ALTERED = "session-altered"
    SESSION_POPULATION = "session-population"
    SESSION_TOO_LONG = "session-too-long"
    NO_ROLE = "no-role"


class SigninRefusal(Exception):
    """A refused credential. ``status`` is what the caller is told; the text is
    the reason's code and nothing else."""

    status: ClassVar[int]

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class Unauthenticated(SigninRefusal):
    """No credential, or one that does not hold: 401."""

    status = 401


class Forbidden(SigninRefusal):
    """A valid principal without the role the route needs: 403."""

    status = 403


class Unavailable(SigninRefusal):
    """The credential could not be judged, because the issuer's keys cannot be
    had: 503. A 401 would make a client drop its session and sign in again in a
    loop, and would hide an outage from a dashboard. It grants nothing."""

    status = 503


@dataclass(frozen=True, slots=True)
class Principal:
    """Who the credential says the caller is. The subject and the roles stay out
    of ``repr``, so a log line that formats the object leaks neither.

    ``issuer`` is the verified ``iss``: a subject is unique only within its
    issuer, so a person is the pair. ``via`` says how the caller was
    authenticated. A cookie travels by itself in a cross-site request and a
    bearer token does not, so a route that changes state and accepts the cookie
    checks the request's origin for ``via == "cookie"``.

    Serialising a ``Principal`` (returning it from a route) puts the subject and
    the roles in the answer: return a response model of the route's own."""

    population: str
    issuer: str
    subject: str = field(repr=False)
    roles: frozenset[str] = field(repr=False)
    expires_at: int
    via: Via

    def has_role(self, role: str) -> bool:
        return role in self.roles


def _printable_word(value: str) -> str:
    if not value or any(ch.isspace() or not ch.isprintable() for ch in value):
        raise ValueError("must be a non-empty text with no whitespace")
    return value


def _optional_word(value: str) -> str:
    return value if value == "" else _printable_word(value)


def _issuer(value: str) -> str:
    # The value is left out of the message: it is the settings' own, but an error
    # that quotes a setting is a habit this repository does not have.
    problem = service_url_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value


Word = Annotated[str, AfterValidator(_printable_word), Field(max_length=256)]
OptionalWord = Annotated[str, AfterValidator(_optional_word), Field(max_length=256)]


class SigninSettings(BaseModel):
    """One issuer, one realm: the staff's or the claimants'. Nothing here is a
    secret; none of it is logged."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    population: Population
    # Compared with the token's ``iss`` character for character.
    issuer: Annotated[str, AfterValidator(_issuer)]
    audience: Word
    # Its own setting: on kind the browser reaches the issuer by one name and the
    # pods by another, and ``iss`` carries the first.
    keys_url: Annotated[str, HttpUrl]
    # The claim that lists the roles: ``roles`` is Entra's.
    roles_claim: Word = DEFAULT_ROLES_CLAIM
    # The claim that names the person: ``sub`` by default. Entra's ``sub`` is
    # different for the same person in each client application, so for Entra the
    # stable one is ``oid``.
    subject_claim: Word = DEFAULT_SUBJECT_CLAIM
    # What tells an access token from an ID token or a refresh token. Both are
    # empty by default and then not enforced: which claim an issuer sets, and to
    # what, is read from a live token of that issuer before it is written here.
    # ``required_typ``: the payload's ``typ`` must equal it.
    required_typ: OptionalWord = ""
    # ``allowed_azp``: the payload's ``azp`` (the client the token was issued to)
    # must be one of these.
    allowed_azp: Annotated[tuple[Word, ...], Field(max_length=MAX_ALLOWED_AZP)] = ()

    @classmethod
    def from_env(cls, environ: Mapping[str, str], population: Population) -> Self:
        """Read ``MERIDIAN_SIGNIN_<POPULATION>_ISSUER``, ``_AUDIENCE``,
        ``_KEYS_URL`` and, optionally, ``_ROLES_CLAIM``, ``_SUBJECT_CLAIM``,
        ``_REQUIRED_TYP`` and ``_ALLOWED_AZP`` (comma separated). Raise
        ``SettingsError`` naming the variable that is missing or not valid,
        never the value. An ``http`` key URL is accepted only when
        ``MERIDIAN_ENVIRONMENT`` is ``kind``: over plain HTTP, whoever is on the
        path supplies the keys, and with them every token."""
        prefix = f"{SIGNIN_ENV_PREFIX}{population.upper()}_"
        azp = environ.get(prefix + "ALLOWED_AZP") or ""
        outcome = cls._built(
            prefix,
            population=population,
            issuer=require_env(environ, prefix + "ISSUER"),
            audience=require_env(environ, prefix + "AUDIENCE"),
            keys_url=require_env(environ, prefix + "KEYS_URL"),
            roles_claim=environ.get(prefix + "ROLES_CLAIM") or DEFAULT_ROLES_CLAIM,
            subject_claim=(
                environ.get(prefix + "SUBJECT_CLAIM") or DEFAULT_SUBJECT_CLAIM
            ),
            required_typ=environ.get(prefix + "REQUIRED_TYP") or "",
            allowed_azp=tuple(part.strip() for part in azp.split(",")) if azp else (),
        )
        if isinstance(outcome, str):
            raise SettingsError(outcome)
        plain = urlsplit(outcome.keys_url).scheme != "https"
        if plain and environ.get(ENVIRONMENT_ENV) != KIND_ENVIRONMENT:
            raise SettingsError(
                f"{prefix}KEYS_URL must be an https URL unless "
                f"{ENVIRONMENT_ENV} is {KIND_ENVIRONMENT}"
            )
        return outcome

    @classmethod
    def _built(cls, prefix: str, **values: Any) -> Self | str:
        """The settings, or the text of the first problem, naming the variable.
        The problem is returned and not raised from the ``except``, so that
        pydantic's error, whose data holds the rejected value, is neither the
        cause nor the context of the error that reaches the caller."""
        try:
            return cls(**values)
        except ValidationError as error:
            first = error.errors()[0]
            field_name = str(first["loc"][0]) if first["loc"] else "settings"
            return f"{prefix}{field_name.upper()} is not valid: {first['msg']}"


_COMPACT = re.compile(r"[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*")


def _header_of(token: str) -> dict[str, Any] | None:
    """The token's header, read without verifying anything."""
    if not _COMPACT.fullmatch(token):
        return None
    try:
        return jwt.get_unverified_header(token)
    except Exception:  # a refusal, whatever the library raises
        return None


def _decoded(
    token: str, key: RSAPublicKey, settings: SigninSettings
) -> dict[str, Any] | Reason:
    """The verified claims, or why not. Signature, algorithm, issuer, audience
    and the required claims are checked here; the times are not (see
    ``_expiry_or_reason``), nor is the subject (see ``_principal_or_reason``)."""
    try:
        return jwt.decode(
            token,
            key,
            algorithms=list(ALGORITHMS),
            issuer=settings.issuer,
            audience=settings.audience,
            options={
                "require": [*REQUIRED_CLAIMS, settings.subject_claim],
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "verify_sub": False,
            },
        )
    except jwt.InvalidSignatureError:
        return Reason.SIGNATURE
    except jwt.InvalidIssuerError:
        return Reason.ISSUER
    except jwt.InvalidAudienceError:
        return Reason.AUDIENCE
    except jwt.MissingRequiredClaimError:
        return Reason.CLAIMS
    except Exception:
        # Its other errors, and whatever else the library raises on hostile
        # input, are a refusal too, never a 500.
        return Reason.MALFORMED


def _number(value: object) -> float | None:
    """A finite JSON number; a bool, a string, NaN and infinity (which Python's
    JSON reader accepts) and an integer too large for a float (a 400-digit
    ``exp``, which ``float`` refuses with ``OverflowError``) are not."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _roles_of(claim: object) -> frozenset[str]:
    """The roles of a token's claim: a list of strings within the bounds, else no
    roles at all (a partly valid list is not trusted in part)."""
    if not isinstance(claim, list) or len(claim) > MAX_ROLES:
        return frozenset()
    if not all(isinstance(r, str) and 0 < len(r) <= MAX_ROLE_CHARS for r in claim):
        return frozenset()
    return frozenset(claim)


def _expiry_or_reason(claims: Mapping[str, Any], now: float) -> float | Reason:
    """The expiry, when the token is inside its times. ``exp`` is required.
    ``nbf`` and ``iat`` are checked when they are present (a null or a text is
    refused, an absent one is not) and ``iat`` more than the leeway ahead is
    refused as ``nbf`` is."""
    expires = _number(claims.get("exp"))
    if expires is None:
        return Reason.CLAIMS
    ahead: list[float] = []
    for name in ("nbf", "iat"):
        if name in claims:
            value = _number(claims[name])
            if value is None:
                return Reason.CLAIMS
            ahead.append(value)
    # An expiry is reached at the end of the leeway, not after it.
    if now >= expires + LEEWAY_SECONDS:
        return Reason.EXPIRED
    if any(now < value - LEEWAY_SECONDS for value in ahead):
        return Reason.NOT_YET_VALID
    return expires


def _kind_problem(claims: Mapping[str, Any], settings: SigninSettings) -> Reason | None:
    """Why the token is not of the kind the settings name, or None."""
    if settings.required_typ and claims.get("typ") != settings.required_typ:
        return Reason.WRONG_TYPE
    if settings.allowed_azp and claims.get("azp") not in settings.allowed_azp:
        return Reason.AUTHORIZED_PARTY
    return None


def _principal_or_reason(
    token: str, settings: SigninSettings, keys: KeySet, now: float
) -> Principal | Reason:
    if len(token) > MAX_TOKEN_CHARS:
        return Reason.TOO_LARGE
    header = _header_of(token)
    if header is None:
        return Reason.MALFORMED
    if header.get("alg") not in ALGORITHMS:
        return Reason.ALGORITHM
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        return Reason.NO_KEY_ID
    try:
        key = keys.key_for(kid)
    except UnknownKeyId:
        return Reason.UNKNOWN_KEY
    except KeySetUnavailable:
        return Reason.KEYS_UNAVAILABLE
    claims = _decoded(token, key, settings)
    if isinstance(claims, Reason):
        return claims
    expires = _expiry_or_reason(claims, now)
    if isinstance(expires, Reason):
        return expires
    wrong_kind = _kind_problem(claims, settings)
    if wrong_kind is not None:
        return wrong_kind
    subject = claims.get(settings.subject_claim)
    if not isinstance(subject, str) or not 0 < len(subject) <= MAX_SUBJECT_CHARS:
        return Reason.CLAIMS
    return Principal(
        population=settings.population,
        issuer=settings.issuer,
        subject=subject,
        roles=_roles_of(claims.get(settings.roles_claim)),
        expires_at=math.floor(expires),
        via="bearer",
    )


def check_bearer(
    token: str, settings: SigninSettings, keys: KeySet, now: float
) -> Principal:
    """The principal a token proves, or ``Unauthenticated`` (``Unavailable`` when
    the issuer's keys cannot be had). ``now`` is epoch seconds. Nothing of the
    token is in the exception."""
    outcome = _principal_or_reason(token, settings, keys, now)
    if isinstance(outcome, Reason):
        if outcome is Reason.KEYS_UNAVAILABLE:
            raise Unavailable(outcome)
        raise Unauthenticated(outcome)
    return outcome
