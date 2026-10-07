"""Who a person is: the check of a bearer token, and the types the session and
the routes share (S021, T-05).

``check_bearer`` turns a raw access token into a ``Principal`` or raises a
refusal. It trusts one issuer (a realm), pinned by its settings. The order is
the point:

1. the token's size, before anything parses it;
2. the header, read without verifying anything, must say RS256 and name a key:
   ``none``, an HMAC algorithm (the old attack signs with the public key's
   bytes), an elliptic-curve or PSS algorithm and a header with no ``alg`` are
   refused here, before any key is looked at;
3. the key with that id, from the issuer's key set (``signinkeys``);
4. the signature with the algorithms list of ``ALGORITHMS`` (one entry, a
   constant: never the token's own claim), and the issuer, the audience and the
   presence of the required claims;
5. the times, checked here against the ``now`` the caller gives (the library
   reads the clock itself and a test cannot move it), with ``LEEWAY_SECONDS``;
6. the subject, and the roles.

A refusal is an exception of a fixed text: the class says 401 or 403, the
``Reason`` says why (for a log line, never for an answer). Nothing of the token
is in it, and it is raised outside the ``except`` that caught the library's
error, so the library's text, which can quote a claim, is not chained to it.
The roles claim must be a list of strings; anything else means no roles.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Self

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from meridian.platform.common.env import HttpUrl, require_env, service_url_problem
from meridian.platform.common.signinkeys import (
    KeySet,
    KeySetUnavailable,
    UnknownKeyId,
)

type Population = Literal["staff", "claimant"]
POPULATIONS: tuple[Population, ...] = ("staff", "claimant")

# The only algorithm a token may use. A list of one, a constant of this module
# and not a setting: an environment must not be able to widen it.
ALGORITHMS = ["RS256"]
# What a token must carry. ``nbf`` is required to be present, not only checked
# when it is: the contract reads so. Entra's tokens carry it; whether Keycloak's
# do is for the first run against it (Y2) to show, and this is the one line to
# change if they do not.
REQUIRED_CLAIMS = ["exp", "nbf", "iss", "aud", "sub"]
# Clock skew allowed between the issuer and this process, on ``exp`` and
# ``nbf``. Both run on one node on kind and on synchronised hosts on Azure;
# half a minute covers drift, and Microsoft's own guidance is "a few minutes" at
# the most.
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
DEFAULT_ROLES_CLAIM = "roles"
SIGNIN_ENV_PREFIX = "MERIDIAN_SIGNIN_"
BEARER_SCHEME = "bearer"


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
    SESSION_VERSION = "session-version"
    SESSION_ALTERED = "session-altered"
    SESSION_POPULATION = "session-population"
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


@dataclass(frozen=True, slots=True)
class Principal:
    """Who the credential says the caller is. The subject and the roles stay out
    of ``repr``, so a log line that formats the object leaks neither."""

    population: str
    issuer: str
    subject: str = field(repr=False)
    roles: frozenset[str] = field(repr=False)
    expires_at: int

    def has_role(self, role: str) -> bool:
        return role in self.roles


def _printable_word(value: str) -> str:
    if not value or any(ch.isspace() or not ch.isprintable() for ch in value):
        raise ValueError("must be a non-empty text with no whitespace")
    return value


def _issuer(value: str) -> str:
    # The value is left out of the message: it is the settings' own, but an error
    # that quotes a setting is a habit this repository does not have.
    problem = service_url_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value


Word = Annotated[str, AfterValidator(_printable_word), Field(max_length=256)]


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

    @classmethod
    def from_env(cls, environ: Mapping[str, str], population: Population) -> Self:
        """Read ``MERIDIAN_SIGNIN_<POPULATION>_ISSUER``, ``_AUDIENCE``,
        ``_KEYS_URL`` and, optionally, ``_ROLES_CLAIM``. Raise
        ``SettingsError`` naming a missing variable."""
        prefix = f"{SIGNIN_ENV_PREFIX}{population.upper()}_"
        return cls(
            population=population,
            issuer=require_env(environ, prefix + "ISSUER"),
            audience=require_env(environ, prefix + "AUDIENCE"),
            keys_url=require_env(environ, prefix + "KEYS_URL"),
            roles_claim=environ.get(prefix + "ROLES_CLAIM") or DEFAULT_ROLES_CLAIM,
        )


def bearer_token_of(authorization: str) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` value, or None when it
    is another scheme or has no token or more than one."""
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != BEARER_SCHEME or not rest or " " in rest:
        return None
    return rest


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
    ``_principal_or_reason``)."""
    try:
        return jwt.decode(
            token,
            key,
            algorithms=ALGORITHMS,
            issuer=settings.issuer,
            audience=settings.audience,
            options={
                "require": REQUIRED_CLAIMS,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )
    except jwt.InvalidSignatureError:
        return Reason.SIGNATURE
    except jwt.InvalidIssuerError:
        return Reason.ISSUER
    except jwt.InvalidAudienceError:
        return Reason.AUDIENCE
    except (jwt.MissingRequiredClaimError, jwt.exceptions.InvalidSubjectError):
        return Reason.CLAIMS
    except Exception:
        # Its other errors, and whatever else the library raises on hostile
        # input, are a refusal too, never a 500.
        return Reason.MALFORMED


def _number(value: object) -> float | None:
    """A finite JSON number; a bool, a string and NaN or infinity (which
    Python's JSON reader accepts) are not."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def roles_of(claim: object) -> frozenset[str]:
    """The roles of a token's claim: a list of strings within the bounds, else no
    roles at all (a partly valid list is not trusted in part)."""
    if not isinstance(claim, list) or len(claim) > MAX_ROLES:
        return frozenset()
    if not all(isinstance(r, str) and 0 < len(r) <= MAX_ROLE_CHARS for r in claim):
        return frozenset()
    return frozenset(claim)


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
    expires = _number(claims.get("exp"))
    not_before = _number(claims.get("nbf"))
    if expires is None or not_before is None:
        return Reason.CLAIMS
    # An expiry is reached at the end of the leeway, not after it.
    if now >= expires + LEEWAY_SECONDS:
        return Reason.EXPIRED
    if now < not_before - LEEWAY_SECONDS:
        return Reason.NOT_YET_VALID
    subject = claims.get("sub")
    if not isinstance(subject, str) or not 0 < len(subject) <= MAX_SUBJECT_CHARS:
        return Reason.CLAIMS
    return Principal(
        population=settings.population,
        issuer=settings.issuer,
        subject=subject,
        roles=roles_of(claims.get(settings.roles_claim)),
        expires_at=math.floor(expires),
    )


def check_bearer(
    token: str, settings: SigninSettings, keys: KeySet, now: float
) -> Principal:
    """The principal a token proves, or ``Unauthenticated``. ``now`` is epoch
    seconds. Nothing of the token is in the exception."""
    outcome = _principal_or_reason(token, settings, keys, now)
    if isinstance(outcome, Reason):
        raise Unauthenticated(outcome)
    return outcome
