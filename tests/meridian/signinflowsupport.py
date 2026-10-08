"""What the tests of the pages' sign-in flow (S021, Y3) are made from: the realm's
addresses as the kind cluster shows them, a token endpoint behind a mock
transport that checks what the real one checks (the code, the PKCE verifier,
the redirect address, the client's credential), and a way to mint an ID token
the way the pinned Keycloak shapes it (``keycloak-tokens.md``, section 4.3).

No network, no server, no database. It lives next to the other ``*support``
modules because the tests of ``common`` are not a package and cannot import one
another.
"""

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.datastructures import QueryParams
from starlette.responses import RedirectResponse, Response

from meridian.platform.common.signinflow import FlowSettings, SigninFlow
from meridian.platform.common.signinkeys import KeySet
from meridian.platform.common.signinsession import SessionKeys, SessionSettings
from meridian.platform.common.signinstate import (
    Transaction,
    open_transaction,
    seal_transaction,
)

# The realm as it runs on the kind cluster: the browser's (front) addresses.
FRONT_ISSUER = "http://id.meridian.localhost:8088/realms/meridian-staff"
AUTHORIZATION_URL = f"{FRONT_ISSUER}/protocol/openid-connect/auth"
END_SESSION_URL = f"{FRONT_ISSUER}/protocol/openid-connect/logout"
# The pods' (back-channel) addresses: the Service's name.
BACK = "http://keycloak.identity.svc:8080/realms/meridian-staff/protocol/openid-connect"
TOKEN_URL = f"{BACK}/token"
KEYS_URL = f"{BACK}/certs"
CLIENT_ID = "meridian-claims-web"
APP_ORIGIN = "http://meridian.localhost:8088"
CALLBACK_PATH = "/auth/staff/callback"
REDIRECT_URI = APP_ORIGIN + CALLBACK_PATH
POST_LOGOUT_URI = APP_ORIGIN + "/"
RETURN_PREFIXES = ("/adjuster", "/brief")
DEFAULT_RETURN = "/"
NOW = 1_800_000_000.0
KID = "kid-a"


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def flow_settings(**changes: Any) -> FlowSettings:
    """The kind settings; a change replaces a field."""
    fields: dict[str, Any] = {
        "population": "staff",
        "environment": "kind",
        "issuer": FRONT_ISSUER,
        "client_id": CLIENT_ID,
        "client_credential": secrets.token_urlsafe(24),
        "authorization_url": AUTHORIZATION_URL,
        "token_url": TOKEN_URL,
        "end_session_url": END_SESSION_URL,
        "app_origin": APP_ORIGIN,
        "redirect_uri": REDIRECT_URI,
        "post_logout_redirect_uri": POST_LOGOUT_URI,
        "return_prefixes": RETURN_PREFIXES,
        "default_return_path": DEFAULT_RETURN,
    }
    return FlowSettings(**{**fields, **changes})


# A deployment's addresses (https everywhere), for the settings outside kind.
HTTPS_ADDRESSES = {
    "issuer": "https://id.example.test/realms/staff",
    "authorization_url": "https://id.example.test/realms/staff/auth",
    "token_url": "https://id.example.test/realms/staff/token",
    "end_session_url": "https://id.example.test/realms/staff/logout",
    "app_origin": "https://app.example.test",
    "redirect_uri": "https://app.example.test/cb",
    "post_logout_redirect_uri": "https://app.example.test/",
}


def https_settings(**changes: Any) -> FlowSettings:
    """Settings for a deployment outside kind."""
    return flow_settings(environment="azure", **{**HTTPS_ADDRESSES, **changes})


class Stream(httpx.AsyncByteStream):
    """A body of ``size`` bytes, served in chunks as the client asks, counting
    what it handed over: a response made from bytes is read when it is made, so
    a test of how much the client reads would measure the fake."""

    def __init__(self, size: int, chunk: int = 8192) -> None:
        self.size = size
        self.chunk = chunk
        self.handed_over = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while self.handed_over < self.size:
            piece = min(self.chunk, self.size - self.handed_over)
            self.handed_over += piece
            yield b"x" * piece


class BytesStream(httpx.AsyncByteStream):
    """A body read when the client asks (``aiter_raw`` of a response made from
    ``content=`` raises ``StreamConsumed``: it was read when it was made)."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body


def answer(
    status: int = 200,
    body: bytes | dict[str, Any] | list[Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """A response with a streamed body: bytes as they are, anything else as JSON."""
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.Response(status, headers=headers, stream=BytesStream(content))


class TokenEndpoint:
    """The issuer's token endpoint. It answers 200 with ``body`` only to the
    request the real one would accept: the code it issued, the verifier whose
    S256 is the challenge it was shown, the redirect address it was shown and
    the client's credential in a Basic header; anything else is a 400, as an
    ``invalid_grant`` is. ``override`` replaces the answer, whatever is asked."""

    def __init__(self, credential: str) -> None:
        self.credential = credential
        self.code = "code-" + secrets.token_urlsafe(12)
        self.challenge = ""
        self.redirect_uri = REDIRECT_URI
        self.body: dict[str, Any] = {}
        self.delay = 0.0
        self.redeemed = False
        self.override: Any = None
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    @property
    def calls(self) -> int:
        return len(self.requests)

    def _accepts(self, request: httpx.Request) -> bool:
        form = dict(parse_qsl(request.content.decode("ascii")))
        wanted = base64.b64encode(
            f"{quote_plus(CLIENT_ID)}:{quote_plus(self.credential)}".encode()
        ).decode()
        verifier = form.get("code_verifier", "")
        return (
            request.method == "POST"
            and str(request.url) == TOKEN_URL
            and request.headers.get("authorization") == f"Basic {wanted}"
            and form.get("grant_type") == "authorization_code"
            and form.get("code") == self.code
            and form.get("redirect_uri") == self.redirect_uri
            and bool(verifier)
            and s256(verifier) == self.challenge
        )

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.override is not None:
            return self.override(request)
        if self.redeemed or not self._accepts(request):
            return answer(400, {"error": "invalid_grant"})
        self.redeemed = True  # a code is good for one exchange
        return answer(200, self.body)


@dataclass(frozen=True)
class Started:
    """What a sign-in start left: the redirect, and what the issuer was told."""

    response: RedirectResponse
    state: str
    nonce: str
    challenge: str
    cookie: str

    @property
    def query(self) -> dict[str, str]:
        return dict(parse_qsl(urlsplit(self.response.headers["location"]).query))


def set_cookies(response: Response) -> dict[str, str]:
    """The ``Set-Cookie`` lines of a response by cookie name."""
    lines = response.headers.getlist("set-cookie")
    return {line.split("=", 1)[0]: line for line in lines}


def cookie_value(line: str) -> str:
    return line.split(";", 1)[0].split("=", 1)[1]


class FlowKit:
    """A flow wired to a fake issuer: ``flow`` is the unit under test, ``signer``
    signs the ID tokens, ``keys`` serves the key set, ``endpoint`` is the token
    endpoint."""

    def __init__(
        self,
        flow_keys: KeySet,
        signer: rsa.RSAPrivateKey,
        settings: FlowSettings,
        sessions: SessionSettings,
        endpoint: TokenEndpoint,
        flow: SigninFlow,
    ) -> None:
        self.keys = flow_keys
        self.signer = signer
        self.settings = settings
        self.sessions = sessions
        self.endpoint = endpoint
        self.flow = flow

    @property
    def session_keys(self) -> SessionKeys:
        return self.sessions.keys

    def begin(self, return_to: str | None = None, now: float = NOW) -> Started:
        response = self.flow.start(return_to, now)
        query = dict(parse_qsl(urlsplit(response.headers["location"]).query))
        cookie = cookie_value(response.headers["set-cookie"])
        # the issuer shows its login page, and will redeem one new code
        self.endpoint.challenge = query["code_challenge"]
        self.endpoint.code = "code-" + secrets.token_urlsafe(12)
        self.endpoint.redeemed = False
        return Started(
            response, query["state"], query["nonce"], query["code_challenge"], cookie
        )

    def claims(self, started: Started, **changes: Any) -> dict[str, Any]:
        """An ID token's payload as the pinned Keycloak makes it (section 4.3)."""
        claims: dict[str, Any] = {
            "iss": FRONT_ISSUER,
            "aud": CLIENT_ID,
            "azp": CLIENT_ID,
            "typ": "ID",
            "sub": "f2c1f7de-0b6f-4c55-8a0e-6f4a8d3e9a11",
            "nonce": started.nonce,
            "iat": int(NOW),
            "exp": int(NOW) + 300,
            "roles": ["adjuster"],
            "sid": "3c9e1a5b-sid",
        }
        return {**claims, **changes}

    def mint(
        self,
        started: Started,
        *,
        drop: tuple[str, ...] = (),
        kid: str | None = KID,
        key: Any = None,
        alg: str = "RS256",
        **changes: Any,
    ) -> str:
        claims = {
            k: v for k, v in self.claims(started, **changes).items() if k not in drop
        }
        headers = {} if kid is None else {"kid": kid}
        return jwt.encode(
            claims, key if key is not None else self.signer, alg, headers=headers
        )

    def arm(self, started: Started, **claim_changes: Any) -> str:
        """The token endpoint will answer the code with an access token and an
        ID token minted with these changes; the ID token is returned."""
        id_token = self.mint(started, **claim_changes)
        self.endpoint.body = {
            "access_token": "access-" + secrets.token_urlsafe(20),
            "id_token": id_token,
            "token_type": "Bearer",
            "expires_in": 300,
        }
        return id_token

    def callback(self, started: Started, **changes: str | None) -> QueryParams:
        """The query the issuer sends the browser back with; a change replaces a
        parameter, or removes it when it is ``None``."""
        query: dict[str, str] = {
            "state": started.state,
            "code": self.endpoint.code,
            "iss": FRONT_ISSUER,
            "session_state": "ss-1",
        }
        for name, value in changes.items():
            if value is None:
                query.pop(name, None)
            else:
                query[name] = value
        return QueryParams(query)

    def forged_cookie(self, started: Started, now: float = NOW, **fields: Any) -> str:
        """A transaction cookie sealed with the real key and these fields changed."""
        opened = open_transaction(started.cookie, self.session_keys, now, "staff")
        assert isinstance(opened, Transaction)
        return seal_transaction(replace(opened, **fields), self.session_keys)


def json_bytes(value: Any) -> bytes:
    return json.dumps(value).encode()
