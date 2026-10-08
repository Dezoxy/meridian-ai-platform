"""What the tests of the Claims API's staff sign-in (S021, Y4) are made from: the
production factory with the switch on and a ``StaffSignin`` whose key set and
token endpoint sit behind one mock transport, and a way to mint what a request
carries (a bearer token, a session cookie, an ID token for the callback).

Every token is minted with the machine's clock, because the routes call
``time.time()``: the ``NOW`` of ``signinflowsupport`` is a year in the future
and would be refused as issued ahead of time. No network, no server. The
database address refuses a connection at once, so a request that is let through
the guard ends in the database's 503 and not in a hang.

It lives next to the other ``*support`` modules because the tests are not
packages and cannot import one another.
"""

import json
import secrets
import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from signinflowsupport import (
    APP_ORIGIN,
    CLIENT_ID,
    FRONT_ISSUER,
    KEYS_URL,
    KID,
    TokenEndpoint,
    answer,
    flow_settings,
)

from meridian.platform.common.signin import Principal, SigninSettings
from meridian.platform.common.signinsession import (
    SessionKeys,
    SessionSettings,
    cookie_name,
    seal_session,
)
from meridian.platform.common.signinstate import transaction_cookie_name
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.workloads.claims_triage.app import create_app
from meridian.workloads.claims_triage.settings import ClaimsSettings
from meridian.workloads.claims_triage.staff_signin import (
    CALLBACK_PATH,
    StaffSignin,
)

AUDIENCE = "meridian-claims-api"
REFUSING_DSN = "postgresql://claims_api@127.0.0.1:1/meridian"
SESSION_COOKIE = cookie_name("staff")
TRANSACTION_COOKIE = transaction_cookie_name("staff")
CLAIM = "CLM-4711"
SUBJECT = "f2c1f7de-0b6f-4c55-8a0e-6f4a8d3e9a11"


class Started:
    """A sign-in begun through ``GET /auth/start``: what the issuer was told
    and the transaction cookie the browser holds."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.query = dict(parse_qsl(urlsplit(response.headers["location"]).query))
        self.state = self.query["state"]
        self.nonce = self.query["nonce"]
        self.challenge = self.query["code_challenge"]
        lines = response.headers.get_list("set-cookie")
        [line] = [x for x in lines if x.startswith(TRANSACTION_COOKIE + "=")]
        self.cookie = line.split(";", 1)[0].split("=", 1)[1]


class StaffRig:
    """A staff sign-in wired to a fake issuer, and the means to build apps."""

    def __init__(self, signer: rsa.RSAPrivateKey) -> None:
        self.signer = signer
        credential = secrets.token_urlsafe(24)
        self.sessions = SessionSettings(
            keys=SessionKeys(current=secrets.token_bytes(32)), secure=False
        )
        self.endpoint = TokenEndpoint(credential)
        # The support's default is the Y3 tests' path, not this route's.
        self.endpoint.redirect_uri = APP_ORIGIN + CALLBACK_PATH
        self.key_requests = 0
        self.keys_down = False
        jwk = json.loads(RSAAlgorithm.to_jwk(signer.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]}
        self.transport = httpx.MockTransport(self._handle)
        self.signin_settings = SigninSettings(
            population="staff",
            issuer=FRONT_ISSUER,
            audience=AUDIENCE,
            environment="kind",
            keys_url=KEYS_URL,
        )
        self.flow_settings = flow_settings(
            client_credential=credential,
            redirect_uri=APP_ORIGIN + CALLBACK_PATH,
            return_prefixes=("/adjuster",),
            default_return_path="/adjuster/claims",
        )

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == KEYS_URL:
            self.key_requests += 1
            if self.keys_down:
                raise httpx.ConnectError("the key URL is down", request=request)
            return answer(200, self.jwks)
        return await self.endpoint.transport.handle_async_request(request)

    def staff(self) -> StaffSignin:
        return StaffSignin(
            self.signin_settings,
            self.flow_settings,
            self.sessions,
            transport=self.transport,
        )

    # ── what a request carries ──────────────────────────────────────────────
    def bearer(self, roles: tuple[str, ...] = ("adjuster",), **claims: Any) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": FRONT_ISSUER,
            "aud": AUDIENCE,
            "sub": SUBJECT,
            "iat": now,
            "exp": now + 300,
            "roles": list(roles),
        }
        return jwt.encode(
            {**payload, **claims}, self.signer, "RS256", headers={"kid": KID}
        )

    def cookie(self, roles: tuple[str, ...] = ("adjuster",)) -> str:
        """A session cookie as the callback would set it."""
        now = time.time()
        principal = Principal(
            population="staff",
            issuer=FRONT_ISSUER,
            subject=SUBJECT,
            roles=frozenset(roles),
            expires_at=int(now) + 300,
            via="cookie",
        )
        value = seal_session(principal, self.sessions.keys, now)
        return f"{SESSION_COOKIE}={value}"

    def id_token(self, started: Started, **changes: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": FRONT_ISSUER,
            "aud": CLIENT_ID,
            "azp": CLIENT_ID,
            "typ": "ID",
            "sub": SUBJECT,
            "nonce": started.nonce,
            "iat": now,
            "exp": now + 300,
            "roles": ["adjuster"],
        }
        return jwt.encode(
            {**claims, **changes}, self.signer, "RS256", headers={"kid": KID}
        )

    def arm(self, started: Started, **changes: Any) -> None:
        """The issuer will redeem one new code for this sign-in."""
        self.endpoint.challenge = started.challenge
        self.endpoint.code = "code-" + secrets.token_urlsafe(12)
        self.endpoint.redeemed = False
        self.endpoint.body = {
            "access_token": "access-" + secrets.token_urlsafe(20),
            "id_token": self.id_token(started, **changes),
            "token_type": "Bearer",
        }

    def callback_path(self, started: Started, **changes: str | None) -> str:
        query = {
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
        pairs = "&".join(f"{k}={v}" for k, v in query.items())
        return f"{CALLBACK_PATH}?{pairs}"


def settings(**changes: Any) -> ClaimsSettings:
    """The Claims API's settings, the switch on, the file routes on."""
    fields: dict[str, Any] = {
        "runtime_url": "http://runtime.invalid",
        "database_url": REFUSING_DSN,
        "uploads_enabled": True,
        "downloads_enabled": True,
        "signin": "staff",
    }
    return ClaimsSettings(**{**fields, **changes})


def build_app(
    rig: StaffRig,
    *,
    exporter: InMemorySpanExporter | None = None,
    staff: StaffSignin | None = None,
    **changes: Any,
) -> FastAPI:
    """The production factory with the switch on and no dependency override."""
    return create_app(
        settings(**changes),
        tracer_provider=make_tracer_provider("claims-api", exporter),
        staff_signin=staff if staff is not None else rig.staff(),
    )


def browser(app: FastAPI) -> TestClient:
    """A client that reaches the app as the edge's host name does, and answers
    what the app raises (a 500) instead of raising it."""
    return TestClient(app, base_url=APP_ORIGIN, raise_server_exceptions=False)


def send(
    client: TestClient,
    method: str,
    path: str,
    *,
    cookie: str | None = None,
    bearer: str | None = None,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """One request, redirects not followed, and no cookie but the one given: the
    client's jar is emptied first, so a test never signs in by accident."""
    client.cookies.clear()
    sent = dict(headers or {})
    if cookie is not None:
        sent["Cookie"] = cookie
    if bearer is not None:
        sent["Authorization"] = f"Bearer {bearer}"
    return client.request(method, path, headers=sent, follow_redirects=False, **kwargs)


DATABASE_DOWN = "the database is unavailable"


def assert_reached_the_handler(
    response: httpx.Response, *, like: httpx.Response | None = None
) -> None:
    """The request passed the guard and met the route's own code, which here
    ends at the database that refuses the connection: its 503 says so. The guard
    refuses with 401 or 403, redirects a page to sign in, or (when the issuer's
    keys cannot be had) answers 503 with ``request refused`` or a page that says
    sign-in is unavailable; none of those says the database is down. A HEAD has
    no body: it is the length of the GET's answer (``like``) instead."""
    assert response.status_code == 503
    assert "request refused" not in response.text
    assert "Sign-in is not available" not in response.text
    if like is None:
        assert DATABASE_DOWN in response.text
    else:
        assert DATABASE_DOWN in like.text
        assert response.headers["content-length"] == like.headers["content-length"]


def begin(client: TestClient, return_to: str | None = None) -> Started:
    path = "/auth/start" if return_to is None else f"/auth/start?return_to={return_to}"
    response = send(client, "GET", path)
    assert response.status_code == 303
    return Started(response)
