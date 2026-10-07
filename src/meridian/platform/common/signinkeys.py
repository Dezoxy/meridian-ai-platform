"""The issuer's signing keys, fetched over HTTP and cached by key id (S021).

``KeySet`` answers one question for the bearer check: the RSA public key that
has this key id. It is the only code that reaches the issuer's key URL.

What it accepts. A key set is JSON with a ``keys`` list. A key counts when it
is an RSA key with a key id, for signing (``use`` absent or ``sig``), for RS256
(``alg`` absent or ``RS256``), at least ``MIN_RSA_BITS`` long and public (a
member ``d`` ends the key). Every other key is dropped without a word: an
elliptic-curve key, a short RSA key and an encryption key in the same set must
not stop the others. At most ``MAX_CACHED_KEYS`` keys are kept, the first ones.
A set with no usable key is a failed fetch, so a cached set is never replaced
by an empty one.

How often it asks. The cached set is fresh for ``KEY_MAX_AGE_SECONDS``. A key
id it does not know, a set that is no longer fresh and an empty cache each ask
for a fetch, and a fetch is made at most once per ``REFETCH_INTERVAL_SECONDS``
whatever the reason and whether the last one worked: a flood of tokens with
unknown key ids, or an issuer that is down, costs one request per interval and
not one per token (T-05). A restart of the issuer that makes new keys is
therefore followed within one interval.

When the fetch fails. A key that is cached still verifies, up to
``KEY_STALE_LIMIT_SECONDS`` after the last good fetch, so that a short outage of
the issuer does not sign everyone out. With nothing cached, or nothing younger
than that, the answer is ``KeySetUnavailable``: fail closed.

The fetch is blocking, under one lock, so a caller runs it in a worker thread
(a plain ``def`` FastAPI dependency does). Waiters hold their thread for at most
the fetch timeout. Errors carry fixed text: the issuer's answer, a URL or a key
is never quoted.
"""

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt.algorithms import RSAAlgorithm

from meridian.platform.common.tls import ClientTls, verify_of

# How long the cached set is trusted without asking again. The issuer rotates
# its keys over weeks and publishes a new one before it signs with it; an hour
# bounds how long a key the issuer has withdrawn keeps verifying while the
# issuer answers.
KEY_MAX_AGE_SECONDS = 3600.0
# How long a cached key may serve while every fetch fails: a day, so a night of
# outage is survived and a lost issuer is not trusted for good.
KEY_STALE_LIMIT_SECONDS = 24 * 3600.0
# The least time between two fetches, successful or not. The same as PyJWT's own
# cooldown; short enough for a restarted issuer's new keys to be taken up
# quickly, long enough that unknown key ids cannot make a fetch per request.
REFETCH_INTERVAL_SECONDS = 30.0
# Entra publishes about half a dozen keys, Keycloak two per realm.
MAX_CACHED_KEYS = 16
# The largest answer read: dozens of keys fit, an endless body does not.
MAX_KEY_SET_BYTES = 64 * 1024
# NIST's floor for RSA signatures through 2030; Entra and Keycloak sign with
# 2048 or more.
MIN_RSA_BITS = 2048
# The longest key id kept; both issuers' are a few dozen characters.
MAX_KEY_ID_LENGTH = 128
# The key URL is inside the cluster or Microsoft's: connecting is quick, and the
# request that waits for it must not wait long.
FETCH_TIMEOUT = httpx.Timeout(3.0, connect=1.0)
RS256 = "RS256"

type Clock = Callable[[], float]


class KeySetError(Exception):
    """No key could be found for a key id. The text is fixed."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class UnknownKeyId(KeySetError):
    """The key set is known and has no usable key with this id."""

    def __init__(self) -> None:
        super().__init__("no signing key has this key id")


class KeySetUnavailable(KeySetError):
    """There is no usable key set: the first fetch failed, or every fetch has
    failed for longer than the stale limit."""

    def __init__(self) -> None:
        super().__init__("the issuer's key set is unavailable")


def key_client(
    tls: ClientTls | None = None, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """The HTTP client of a ``KeySet``: no proxy or certificate setting from the
    environment, no redirect followed, the timeout above. ``tls`` is the CA the
    issuer is trusted by when its key URL is ``https`` (the library's default
    verification when none); ``transport`` is for a test."""
    return httpx.Client(
        verify=verify_of(tls),
        trust_env=False,
        follow_redirects=False,
        timeout=FETCH_TIMEOUT,
        transport=transport,
    )


def _usable_key(item: object) -> tuple[str, RSAPublicKey] | None:
    """The key id and public key of one entry of the set, or None."""
    if not isinstance(item, dict):
        return None
    kid = item.get("kid")
    if not isinstance(kid, str) or not 0 < len(kid) <= MAX_KEY_ID_LENGTH:
        return None
    if item.get("kty") != "RSA" or "d" in item:
        return None
    if item.get("use", "sig") != "sig" or item.get("alg", RS256) != RS256:
        return None
    try:
        key = RSAAlgorithm.from_jwk(item)
    except Exception:  # any malformed member drops the key
        return None
    if not isinstance(key, RSAPublicKey) or key.key_size < MIN_RSA_BITS:
        return None
    return kid, key


def parse_key_set(body: bytes) -> dict[str, RSAPublicKey]:
    """The usable keys of a key set document by key id; empty when it is not
    JSON, has no ``keys`` list or holds no usable key."""
    try:
        document = json.loads(body)
    except ValueError:
        return {}
    items = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(items, list):
        return {}
    keys: dict[str, RSAPublicKey] = {}
    for item in items:
        found = _usable_key(item)
        if found is not None and found[0] not in keys:
            keys[found[0]] = found[1]
        if len(keys) >= MAX_CACHED_KEYS:
            break
    return keys


@dataclass(frozen=True, slots=True)
class _Cache:
    keys: dict[str, RSAPublicKey] = field(default_factory=dict)
    fetched_at: float | None = None


class KeySet:
    """The signing keys of one issuer, behind ``key_for``."""

    def __init__(self, url: str, client: httpx.Client, clock: Clock = time.monotonic):
        self._url = url
        self._client = client
        self._clock = clock
        self._lock = threading.Lock()
        self._cache = _Cache()
        self._attempted_at: float | None = None

    def key_for(self, kid: str) -> RSAPublicKey:
        """The public key with this id. Raise ``UnknownKeyId`` when the set is
        known and does not hold it, ``KeySetUnavailable`` when there is no set
        to ask."""
        with self._lock:
            now = self._clock()
            key = self._fresh_key(kid, now)
            if key is None and self._fetch_due(now):
                self._refresh(now)
                key = self._fresh_key(kid, now)
            if key is None:
                key = self._stale_key(kid, now)
            if key is not None:
                return key
            if self._cache_usable(now):
                raise UnknownKeyId
            raise KeySetUnavailable

    def _age(self, now: float) -> float | None:
        fetched = self._cache.fetched_at
        return None if fetched is None else now - fetched

    def _fresh_key(self, kid: str, now: float) -> RSAPublicKey | None:
        age = self._age(now)
        if age is None or age >= KEY_MAX_AGE_SECONDS:
            return None
        return self._cache.keys.get(kid)

    def _stale_key(self, kid: str, now: float) -> RSAPublicKey | None:
        return self._cache.keys.get(kid) if self._cache_usable(now) else None

    def _cache_usable(self, now: float) -> bool:
        age = self._age(now)
        return age is not None and age < KEY_STALE_LIMIT_SECONDS

    def _fetch_due(self, now: float) -> bool:
        attempted = self._attempted_at
        return attempted is None or now - attempted >= REFETCH_INTERVAL_SECONDS

    def _refresh(self, now: float) -> None:
        """One fetch. The attempt is noted before it is made, so a fetch that
        raises is not repeated within the interval either."""
        self._attempted_at = now
        keys = parse_key_set(self._fetch())
        if keys:
            self._cache = _Cache(keys=keys, fetched_at=now)

    def _fetch(self) -> bytes:
        """The key set's body, or ``b""`` for every failure: a transport error,
        a status other than 200, a body over the bound."""
        body = bytearray()
        try:
            with self._client.stream(
                "GET",
                self._url,
                headers={"Accept": "application/json"},
                timeout=FETCH_TIMEOUT,
                follow_redirects=False,
            ) as response:
                if response.status_code != httpx.codes.OK:
                    return b""
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_KEY_SET_BYTES:
                        return b""
        except httpx.HTTPError:
            return b""
        return bytes(body)
