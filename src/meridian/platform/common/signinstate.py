"""What a sign-in in progress is made of (S021, Y3): the reasons a callback is
refused, the transaction cookie that carries the sign-in from the redirect to
the callback, the path a person is sent back to, and the PKCE challenge. The
flow itself is ``signinflow``; this is its vocabulary, in a module of its own to
keep both small. Wired to no route.

The transaction cookie is ``t1.<payload>.<mac>``: compact JSON in base64url and
an HMAC-SHA-256 of ``t1.<payload>``, compared in constant time under every key
the session keys hold, so a key is rotated as the session's is. Two things keep
it from ever being read as a session cookie, and the session from ever being
read as it: the version word (``t1`` against ``v1``) is inside the MAC'd text,
and the key that signs it is derived from the session key with a label of its
own. It is opened with the checks of a cookie that must be hostile-proof: ASCII,
size, version, MAC, and only then the payload, whose fields, types and shapes
are all checked; then the population, the expiry, and a cap on how far ahead
the expiry may be (``TRANSACTION_SECONDS``, held at open as at seal, so a cookie
signed with a key that leaked is held to it too).

The return path is a path of this app and nothing else (``safe_return_path``).
"""

import base64
import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

from meridian.platform.common.signinsession import SessionKeys

TRANSACTION_COOKIE_PREFIX = "meridian_signin_"
# The version in front of the value, and so inside the MAC'd text.
TRANSACTION_VERSION = "t1"
# The signing key is derived from the session key with this label.
TRANSACTION_KEY_LABEL = b"meridian.signin.transaction.v1"
# Long enough for a person to find their password; short enough that a cookie
# left in a browser is of no use the next day.
TRANSACTION_SECONDS = 600
# A real one is about 750 bytes; a browser keeps 4096.
MAX_TRANSACTION_COOKIE_BYTES = 1536
MAX_RETURN_PATH_CHARS = 256
PKCE_METHOD = "S256"

# What ``secrets.token_urlsafe`` makes, from 32 bytes (43 characters) to the
# longest verifier RFC 7636 allows (128).
_RANDOM_TEXT = re.compile(r"[A-Za-z0-9_-]{43,128}")
# A path of this app: RFC 3986's path characters except ``%`` (an encoded form
# is how a filter is got round), ``?``, ``#``, ``\``, spaces and controls.
_PATH_TEXT = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@/-]*")
_TRANSACTION_FIELDS = frozenset({"s", "n", "v", "r", "p", "e"})


class FlowReason(StrEnum):
    """Why a callback was refused. For a log line and for the caller's own use;
    never part of what a person is shown, so a caller cannot tell a wrong
    audience from a wrong signature."""

    NO_TRANSACTION = "no-transaction"
    TRANSACTION_VERSION = "transaction-version"
    TRANSACTION_ALTERED = "transaction-altered"
    TRANSACTION_POPULATION = "transaction-population"
    TRANSACTION_EXPIRED = "transaction-expired"
    TRANSACTION_TOO_LONG = "transaction-too-long"
    BAD_CALLBACK = "bad-callback"
    STATE_MISMATCH = "state"
    ISSUER_ERROR = "issuer-error"
    ISSUER_PARAMETER = "issuer-parameter"
    NO_CODE = "no-code"
    EXCHANGE_FAILED = "exchange-failed"
    EXCHANGE_DEADLINE = "exchange-deadline"
    EXCHANGE_TOO_LARGE = "exchange-too-large"
    EXCHANGE_MALFORMED = "exchange-malformed"
    ID_MISSING = "id-missing"
    ID_TOO_LARGE = "id-too-large"
    ID_MALFORMED = "id-malformed"
    ID_ALGORITHM = "id-algorithm"
    ID_NO_KEY_ID = "id-no-key-id"
    ID_UNKNOWN_KEY = "id-unknown-key"
    ID_KEYS_UNAVAILABLE = "id-keys-unavailable"
    ID_SIGNATURE = "id-signature"
    ID_ISSUER = "id-issuer"
    ID_AUDIENCE = "id-audience"
    ID_CLAIMS = "id-claims"
    ID_EXPIRED = "id-expired"
    ID_NOT_YET_VALID = "id-not-yet-valid"
    ID_TYPE = "id-type"
    ID_AUTHORIZED_PARTY = "id-authorized-party"
    ID_NONCE = "id-nonce"
    SESSION_TOO_LARGE = "session-too-large"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ── the return path ─────────────────────────────────────────────────────────
def clean_path(value: object) -> bool:
    """A path of this app and nothing else: it starts with one ``/``, holds only
    the characters of ``_PATH_TEXT`` (no host, no scheme, no backslash, no
    ``%``-encoding, no query, no fragment, no space or control character), has
    no empty segment but a trailing one and no ``.`` or ``..`` segment."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_RETURN_PATH_CHARS:
        return False
    if not _PATH_TEXT.fullmatch(value):
        return False
    segments = value.split("/")[1:]
    last = len(segments) - 1
    return all(
        segment not in (".", "..") and (segment != "" or index == last)
        for index, segment in enumerate(segments)
    )


def _under(path: str, prefix: str) -> bool:
    """``path`` is the prefix or below it, on a segment boundary."""
    base = prefix.rstrip("/")
    return path == base or path.startswith(base + "/")


def safe_return_path(candidate: object, prefixes: tuple[str, ...], default: str) -> str:
    """``candidate`` when it is a clean path of this app under one of the
    prefixes, else ``default``: an absolute URL, a ``//host``, a backslash form,
    an encoded form and a path off the allowlist all become the default page, so
    a sign-in can be made to return only to the app's own pages."""
    if not isinstance(candidate, str) or not clean_path(candidate):
        return default
    return candidate if any(_under(candidate, p) for p in prefixes) else default


# ── PKCE ────────────────────────────────────────────────────────────────────
def pkce_challenge(verifier: str, method: str = PKCE_METHOD) -> str:
    """The ``S256`` challenge of a verifier (RFC 7636). Any other method,
    ``plain`` included, is refused: a ``plain`` challenge is the verifier itself,
    and the issuer's discovery document lists it."""
    if method != PKCE_METHOD:
        raise ValueError(f"only the {PKCE_METHOD} challenge method is supported")
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())


# ── the transaction cookie ──────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Transaction:
    """What a sign-in in progress holds: the three randoms, where to return to,
    the population and when it ends. The randoms stay out of ``repr``."""

    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    return_to: str
    population: str
    expires_at: int


def transaction_cookie_name(population: str) -> str:
    """One name per population, so two realms' sign-ins on one host do not
    replace each other."""
    return TRANSACTION_COOKIE_PREFIX + population


def _mac(key: bytes, body: str) -> str:
    derived = hmac.new(key, TRANSACTION_KEY_LABEL, hashlib.sha256).digest()
    return _b64(hmac.new(derived, body.encode("ascii"), hashlib.sha256).digest())


def seal_transaction(transaction: Transaction, keys: SessionKeys) -> str:
    """The cookie value, signed with the current key. Nothing is checked here:
    what opens it checks."""
    payload = {
        "s": transaction.state,
        "n": transaction.nonce,
        "v": transaction.verifier,
        "r": transaction.return_to,
        "p": transaction.population,
        "e": transaction.expires_at,
    }
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    body = f"{TRANSACTION_VERSION}.{_b64(text.encode('utf-8'))}"
    cookie = f"{body}.{_mac(keys.current, body)}"
    if len(cookie) > MAX_TRANSACTION_COOKIE_BYTES:
        raise ValueError("the transaction is too large for a cookie")
    return cookie


def _signed_payload(cookie: str, keys: SessionKeys) -> str | FlowReason:
    """The base64 payload of a cookie whose MAC holds under one of the keys, or
    why not. Every key is compared, with no early exit."""
    if not cookie.isascii() or len(cookie) > MAX_TRANSACTION_COOKIE_BYTES:
        return FlowReason.TRANSACTION_ALTERED
    version, dot, rest = cookie.partition(".")
    if not dot:
        return FlowReason.TRANSACTION_ALTERED
    if version != TRANSACTION_VERSION:
        return FlowReason.TRANSACTION_VERSION
    payload, dot, mac = rest.partition(".")
    if not dot or not payload or "." in mac:
        return FlowReason.TRANSACTION_ALTERED
    body = f"{version}.{payload}"
    given = mac.encode("ascii")
    verdicts = [
        hmac.compare_digest(_mac(key, body).encode("ascii"), given)
        for key in keys.all()
    ]
    return payload if any(verdicts) else FlowReason.TRANSACTION_ALTERED


def _transaction_of(payload: str) -> Transaction | None:
    """The transaction in a payload whose MAC held. One that this module signed
    has the right shape; any other (a bug, an older release's) is refused."""
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.keys() != _TRANSACTION_FIELDS:
        return None
    state, nonce, verifier = data["s"], data["n"], data["v"]
    path, population, ends = data["r"], data["p"], data["e"]
    randoms = (state, nonce, verifier)
    if not all(isinstance(v, str) and _RANDOM_TEXT.fullmatch(v) for v in randoms):
        return None
    if not isinstance(population, str) or not clean_path(path):
        return None
    if isinstance(ends, bool) or not isinstance(ends, int):
        return None
    return Transaction(state, nonce, verifier, path, population, ends)


def open_transaction(
    cookie: str, keys: SessionKeys, now: float, population: str
) -> Transaction | FlowReason:
    """The transaction in a cookie, or why it is refused: of another version (a
    session cookie is), altered or cut short, another population's, expired, or
    ending later than ``TRANSACTION_SECONDS`` from ``now``, which no cookie of
    this module does."""
    signed = _signed_payload(cookie, keys)
    if isinstance(signed, FlowReason):
        return signed
    transaction = _transaction_of(signed)
    if transaction is None:
        return FlowReason.TRANSACTION_ALTERED
    if transaction.population != population:
        return FlowReason.TRANSACTION_POPULATION
    if now >= transaction.expires_at:
        return FlowReason.TRANSACTION_EXPIRED
    if transaction.expires_at > math.floor(now) + TRANSACTION_SECONDS:
        return FlowReason.TRANSACTION_TOO_LONG
    return transaction
