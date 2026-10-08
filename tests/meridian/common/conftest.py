"""Fixtures for the sign-in tests (S021): keys made here, a fake issuer behind an
httpx transport, a clock that moves when told to. No network, no database.

The tests of ``common`` are not a package (the suite imports by path), so a
helper shared by several test files is a fixture that returns it. Every name
starts with ``signin_`` and none of them is used by the older tests here.
"""

import base64
import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import RSAAlgorithm

from meridian.platform.common.signin import SigninSettings
from meridian.platform.common.signinkeys import KeySet

ISSUER = "https://id.example.test/realms/meridian-staff"
AUDIENCE = "meridian-api"
KEYS_URL = "https://id.example.test/realms/meridian-staff/protocol/openid-connect/certs"
# Epoch seconds; every test reads the time from here, never from the machine.
NOW = 1_800_000_000.0
FIRST_KID = "kid-a"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def public_jwk(kid: str, private: rsa.RSAPrivateKey, **members: Any) -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256", **members}


class FakeClock:
    """A monotonic clock that moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ChunkStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A response body served in chunks, read when the client asks: a response
    built from ``content=bytes`` is read (and, when it names a content encoding,
    decoded) the moment it is made, so a test of what the client holds would
    measure the fake. Sync and async, for whichever transport reads it."""

    def __init__(self, body: bytes, chunk_size: int) -> None:
        self._body = body
        self._size = chunk_size

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self._body), self._size):
            yield self._body[start : start + self._size]

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self:
            yield chunk


class FakeIssuer:
    """An issuer: signing keys by key id, a key URL behind a mock transport that
    counts its requests, and a way to mint tokens. The body is served as a
    stream, ``chunk_size`` bytes at a time. The constants are attributes, because
    a test file cannot import this one."""

    name = ISSUER
    audience = AUDIENCE
    url = KEYS_URL
    now = NOW
    first_kid = FIRST_KID

    def __init__(self, keys: dict[str, rsa.RSAPrivateKey]) -> None:
        self.keys = dict(keys)
        self.extra_jwks: list[Any] = []
        self.calls = 0
        self.down = False
        self.status = 200
        self.body: bytes | None = None
        self.chunk_size = 16 * 1024
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def jwks(self) -> dict[str, Any]:
        keys = [public_jwk(kid, key) for kid, key in self.keys.items()]
        return {"keys": [*keys, *self.extra_jwks]}

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("the key URL is down", request=request)
        body = json.dumps(self.jwks()).encode() if self.body is None else self.body
        return httpx.Response(self.status, stream=ChunkStream(body, self.chunk_size))

    def restart(self, keys: dict[str, rsa.RSAPrivateKey]) -> None:
        """New keys under new ids, as a restarted development issuer has."""
        self.keys = dict(keys)

    def claims(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "subject-1",
            "exp": NOW + 600,
            "nbf": NOW - 10,
            "iat": NOW - 10,
            "roles": ["adjuster"],
        }
        return {**base, **overrides}

    def mint(
        self,
        *,
        drop: tuple[str, ...] = (),
        kid: str | None = FIRST_KID,
        key: Any = None,
        alg: str = "RS256",
        **overrides: Any,
    ) -> str:
        """A token signed with ``key`` (the issuer's first key by default)."""
        claims = {k: v for k, v in self.claims(**overrides).items() if k not in drop}
        signing = key if key is not None else next(iter(self.keys.values()))
        headers = {} if kid is None else {"kid": kid}
        return jwt.encode(claims, signing, algorithm=alg, headers=headers)


@pytest.fixture(scope="session")
def signin_rsa_pool() -> list[rsa.RSAPrivateKey]:
    return [rsa.generate_private_key(65537, 2048) for _ in range(4)]


@pytest.fixture(scope="session")
def signin_short_rsa() -> rsa.RSAPrivateKey:
    # The key the key-set client must refuse: short on purpose.
    return rsa.generate_private_key(65537, 1024)  # noqa: S505


@pytest.fixture(scope="session")
def signin_ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def signin_issuer(signin_rsa_pool: list[rsa.RSAPrivateKey]) -> FakeIssuer:
    return FakeIssuer({FIRST_KID: signin_rsa_pool[0]})


@pytest.fixture
def signin_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def signin_settings() -> SigninSettings:
    return SigninSettings(
        population="staff", issuer=ISSUER, audience=AUDIENCE, keys_url=KEYS_URL
    )


@pytest.fixture
def signin_keys(signin_issuer: FakeIssuer, signin_clock: FakeClock) -> KeySet:
    return KeySet(KEYS_URL, clock=signin_clock, transport=signin_issuer.transport)


@pytest.fixture
def signin_forge() -> Callable[..., str]:
    """A token assembled by hand: header and payload as given, and the signature
    that ``sign`` makes from the signing input (none by default). For the
    tokens PyJWT will not build."""

    def forge(
        header: dict[str, Any],
        payload: dict[str, Any],
        sign: Callable[[bytes], bytes] | None = None,
    ) -> str:
        head = b64url(json.dumps(header).encode())
        body = b64url(json.dumps(payload).encode())
        signing_input = f"{head}.{body}".encode()
        signature = b"" if sign is None else sign(signing_input)
        return f"{head}.{body}.{b64url(signature)}"

    return forge


@pytest.fixture
def signin_hmac_with_public_key(
    signin_issuer: FakeIssuer,
) -> Callable[[bytes], bytes]:
    """The signer of the old attack: HMAC-SHA-256 keyed with the issuer's public
    key, in the PEM form a verifier would hand to a library."""
    public = next(iter(signin_issuer.keys.values())).public_key()
    pem = public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    def sign(signing_input: bytes) -> bytes:
        return hmac.new(pem, signing_input, hashlib.sha256).digest()

    return sign
