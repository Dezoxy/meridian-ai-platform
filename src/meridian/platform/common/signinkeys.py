"""The issuer's signing keys, fetched over HTTP and cached by key id (S021).

``KeySet`` answers one question for the bearer check: the RSA public key that
has this key id. It is the only code that reaches the issuer's key URL.

What it accepts. A key set is JSON with a ``keys`` list. A key counts when it
is an RSA key with a key id, for signing (``use`` absent or ``sig``, and
``key_ops`` absent or a list that holds ``verify``), for RS256 (``alg`` absent
or ``RS256``), from ``MIN_RSA_BITS`` to ``MAX_RSA_BITS`` long and public (a
member ``d`` ends the key). Every other key is dropped without a word: an
elliptic-curve key, a short RSA key and an encryption key in the same set must
not stop the others. At most ``MAX_CACHED_KEYS`` keys are kept, the first ones;
a set with more usable keys than that says so once, with the count. A set with
no usable key is a failed fetch, so a cached set is never replaced by an empty
one.

What it accepts from the wire. The request asks for ``Accept-Encoding:
identity``, and nothing is decoded: the answer is read as it came. An answer that
carries a ``Content-Encoding`` other than ``identity`` is a failed fetch, decided
from the header before a body byte is read: a small compressed body can expand
to hundreds of megabytes. ``MAX_KEY_SET_BYTES`` bounds the bytes received and is
checked before a chunk is kept. A fetch has a total deadline too
(``FETCH_DEADLINE_SECONDS``), checked after every chunk against the clock, so a
key URL that drips a byte at a time under the per-read timeout is cut. A read
that has begun is not interrupted, so the worst case for one fetch is the
deadline plus one read timeout.

How often it asks. The cached set is fresh for ``KEY_MAX_AGE_SECONDS``. A key
id it does not know, a set that is no longer fresh and an empty cache each ask
for a fetch, and a fetch is made at most once per ``REFETCH_INTERVAL_SECONDS``
whatever the reason and whether the last one worked: a flood of tokens with
unknown key ids, or an issuer that is down, costs one request per interval and
not one per token (T-05). A restart of the issuer that makes new keys is
therefore followed within one interval.

Who waits for a fetch. The fetch is blocking, so a caller runs it in a worker
thread (a plain ``def`` FastAPI dependency does), and one slow key URL must not
hold every request that needs only a cached key. The cache is an immutable
snapshot swapped by reference: reading it takes no lock. A fetch takes the
fetch lock. ``key_for`` serves, in this order:

1. A FRESH cached key: at once, whatever a fetch is doing. No lock is taken.
2. Otherwise, when a fetch is due, the caller that gets the fetch lock without
   waiting makes it, then answers from the new snapshot.
3. A STALE cached key (older than the fresh age, younger than the stale limit),
   when the fetch is not due or another thread holds the lock: served at once,
   and a WARNING says so (at most one per interval).
4. A usable cache that lacks the key id: ``UnknownKeyId`` at once; an issuer
   that publishes a key has done so long before it signs with it.
5. NO usable cache (a cold start, or nothing younger than the stale limit):
   a wait of at most ``FETCH_WAIT_SECONDS`` for the fetch lock, so that at a
   cold start the callers behind the first are not refused but share its fetch;
   then one more look at the snapshot; then ``KeySetUnavailable``.
The wait is a lock timeout in real time; the deadline and the cache's age read
the injected clock (``time.monotonic`` in service).

When the fetch fails. A key that is cached still verifies, up to
``KEY_STALE_LIMIT_SECONDS`` after the last good fetch, so that a short outage of
the issuer does not sign everyone out. With nothing cached, or nothing younger
than that, the answer is ``KeySetUnavailable``: fail closed. Only
``KeySetUnavailable`` and ``UnknownKeyId`` leave ``key_for``, whatever the key
URL answers.

What it says. The module logger writes one WARNING per failed fetch and one per
interval while a stale key is served, so a flood of requests cannot flood the
log. The text is fixed; the only variables are the failure's class name and the
cache's age in whole seconds. Never the URL (on Entra it carries a tenant
identifier), a header, a body byte, a key id from a token or an exception's
text. Errors carry fixed text for the same reason.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt.algorithms import RSAAlgorithm

from meridian.platform.common.tls import ClientTls, verify_of

logger = logging.getLogger(__name__)

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
# The largest key kept. A verify costs more the longer the modulus (about the
# square of its length); 4096 is the longest in common use for tokens, and 8192
# leaves room for it at a cost that stays a few milliseconds.
MAX_RSA_BITS = 8192
# The longest key id kept; both issuers' are a few dozen characters.
MAX_KEY_ID_LENGTH = 128
# The key URL is inside the cluster or Microsoft's: connecting is quick, and the
# request that waits for it must not wait long.
FETCH_TIMEOUT = httpx.Timeout(3.0, connect=1.0)
# The most one fetch may take to read its answer. The per-read timeout above is
# met again by every chunk, so a key URL that sends a byte every two seconds
# holds a thread for as long as it likes (64 KiB of them: hours). The real
# answers come in well under a second; the Claims API serves requests from a
# pool of about forty threads, and five seconds is the most that a pool can lend
# to one slow URL, once per interval.
FETCH_DEADLINE_SECONDS = 5.0
# How long a caller with no usable cache waits for the fetch in flight: the
# deadline and no longer, since a fetch that is still running after that has
# been cut.
FETCH_WAIT_SECONDS = FETCH_DEADLINE_SECONDS
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


class FetchFailure(Exception):
    """A fetch that did not give a usable set. Its subclasses' class names are
    the words the log uses for the cause; they carry no text of their own."""


class ContentEncodingRefused(FetchFailure):
    """The answer carries a ``Content-Encoding`` other than ``identity``."""


class BadStatus(FetchFailure):
    """The status is not 200 (a redirect is not followed)."""


class BodyTooLarge(FetchFailure):
    """More than ``MAX_KEY_SET_BYTES`` came."""


class DeadlineExceeded(FetchFailure):
    """The answer was still coming after ``FETCH_DEADLINE_SECONDS``."""


class NoUsableKey(FetchFailure):
    """The body is not a key set, or holds no usable key."""


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


def _may_verify(item: dict[object, object]) -> bool:
    """``key_ops`` absent, or a list that holds ``verify``."""
    if "key_ops" not in item:
        return True
    operations = item["key_ops"]
    return isinstance(operations, list) and "verify" in operations


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
    if not _may_verify(item):
        return None
    try:
        key = RSAAlgorithm.from_jwk(item)
    except Exception:  # any malformed member drops the key
        return None
    if not isinstance(key, RSAPublicKey):
        return None
    if not MIN_RSA_BITS <= key.key_size <= MAX_RSA_BITS:
        return None
    return kid, key


def parse_key_set(body: bytes) -> dict[str, RSAPublicKey]:
    """The usable keys of a key set document by key id; empty when it is not
    JSON (or is nested beyond what the parser allows), has no ``keys`` list or
    holds no usable key."""
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return {}
    items = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(items, list):
        return {}
    keys: dict[str, RSAPublicKey] = {}
    over_the_cap = 0
    for item in items:
        found = _usable_key(item)
        if found is None or found[0] in keys:
            continue
        if len(keys) < MAX_CACHED_KEYS:
            keys[found[0]] = found[1]
        else:
            over_the_cap += 1
    if over_the_cap:
        logger.warning(
            "the key set holds more usable keys than are kept; %d dropped",
            over_the_cap,
        )
    return keys


@dataclass(frozen=True, slots=True)
class _Cache:
    """One snapshot. Nothing changes it after it is made (``keys`` is never
    written to); a fetch makes a new one and swaps the reference."""

    keys: dict[str, RSAPublicKey] = field(default_factory=dict)
    fetched_at: float | None = None


class KeySet:
    """The signing keys of one issuer, behind ``key_for``."""

    def __init__(self, url: str, client: httpx.Client, clock: Clock = time.monotonic):
        self._url = url
        self._client = client
        self._clock = clock
        self._fetch_lock = threading.Lock()  # held for the length of a fetch
        self._log_lock = threading.Lock()  # held for the length of a comparison
        self._cache = _Cache()
        self._attempted_at: float | None = None  # written under the fetch lock
        self._stale_logged_at: float | None = None  # written under the log lock

    def key_for(self, kid: str) -> RSAPublicKey:
        """The public key with this id. Raise ``UnknownKeyId`` when the set is
        known and does not hold it, ``KeySetUnavailable`` when there is no set
        to ask. The precedence is in the module's docstring."""
        key = self._fresh_key(self._cache, kid, self._clock())
        if key is not None:
            return key
        self._fetch_if_needed()
        cache, now = self._cache, self._clock()
        key = self._fresh_key(cache, kid, now)
        if key is not None:
            return key
        if not self._usable(cache, now):
            raise KeySetUnavailable
        key = cache.keys.get(kid)
        if key is None:
            raise UnknownKeyId
        self._warn_stale(cache, now)
        return key

    def _age(self, cache: _Cache, now: float) -> float | None:
        return None if cache.fetched_at is None else now - cache.fetched_at

    def _fresh_key(self, cache: _Cache, kid: str, now: float) -> RSAPublicKey | None:
        age = self._age(cache, now)
        if age is None or age >= KEY_MAX_AGE_SECONDS:
            return None
        return cache.keys.get(kid)

    def _usable(self, cache: _Cache, now: float) -> bool:
        age = self._age(cache, now)
        return age is not None and age < KEY_STALE_LIMIT_SECONDS

    def _fetch_due(self, now: float) -> bool:
        attempted = self._attempted_at
        return attempted is None or now - attempted >= REFETCH_INTERVAL_SECONDS

    def _fetch_if_needed(self) -> None:
        """Make the fetch when it is due and no other thread is making it. With
        a usable cache, never wait: the caller is answered from what is cached.
        With none, wait for the fetch in flight, but only so long."""
        now = self._clock()
        usable = self._usable(self._cache, now)
        if usable and not self._fetch_due(now):
            return
        if not self._fetch_lock.acquire(blocking=False):
            if usable:
                return
            if not self._fetch_lock.acquire(timeout=FETCH_WAIT_SECONDS):
                return
        try:
            now = self._clock()
            if self._fetch_due(now):  # not, when the thread ahead of this one did
                self._refresh(now)
        finally:
            self._fetch_lock.release()

    def _refresh(self, now: float) -> None:
        """One fetch. The attempt is noted before it is made, so a fetch that
        raises is not repeated within the interval either. Any exception is a
        failed fetch: nothing but this module's two classes leaves ``key_for``,
        whatever a transport raises."""
        self._attempted_at = now
        try:
            keys = parse_key_set(self._fetch())
            if not keys:
                raise NoUsableKey
        except Exception as error:
            self._warn_failed(type(error).__name__, now)
            return
        self._cache = _Cache(keys=keys, fetched_at=now)

    def _fetch(self) -> bytes:
        """The key set's body as received. Raise ``FetchFailure`` for an answer
        that is not to be used, or the transport's own error."""
        began = self._clock()
        body = bytearray()
        with self._client.stream(
            "GET",
            self._url,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            timeout=FETCH_TIMEOUT,
            follow_redirects=False,
        ) as response:
            if response.status_code != httpx.codes.OK:
                raise BadStatus
            encoding = response.headers.get("content-encoding", "identity")
            if encoding.strip().lower() != "identity":
                raise ContentEncodingRefused
            # With no content encoding (checked above) ``iter_bytes`` decodes
            # nothing, so a chunk is the bytes as they came. ``iter_raw`` would
            # say the same and fail on a response a mock transport has already
            # read.
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > MAX_KEY_SET_BYTES:
                    raise BodyTooLarge
                body.extend(chunk)
                if self._clock() - began > FETCH_DEADLINE_SECONDS:
                    raise DeadlineExceeded
        return bytes(body)

    def _warn_failed(self, failure: str, now: float) -> None:
        age = self._age(self._cache, now)
        if age is None:
            logger.warning("key set fetch failed (%s); nothing is cached", failure)
        else:
            logger.warning(
                "key set fetch failed (%s); the cached keys are %d s old",
                failure,
                int(age),
            )

    def _warn_stale(self, cache: _Cache, now: float) -> None:
        """Say that a stale key is served, once per interval."""
        with self._log_lock:
            last = self._stale_logged_at
            if last is not None and now - last < REFETCH_INTERVAL_SECONDS:
                return
            self._stale_logged_at = now
        logger.warning(
            "serving a cached key from a key set that is %d s old",
            int(self._age(cache, now) or 0),
        )
