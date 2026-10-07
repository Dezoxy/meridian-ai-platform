"""Canary tests for the sign-in (S021, T-05, T-03): a token, a cookie and a
subject that carry a marker string appear in no log record at DEBUG, no
exported span and no response, for every way a credential can be refused, and
for the ones that are accepted.

The app is built through ``create_service_app`` as the services are, so the real
middleware stack, the FastAPI instrumentation and the error handlers are under
test, with the sign-in dependencies on two routes.
"""

import base64
import json
import logging
import secrets
from collections.abc import Callable
from typing import Annotated, Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import Depends
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from starlette.testclient import TestClient

from meridian.platform.common.http import create_service_app
from meridian.platform.common.signin import Principal, SigninSettings
from meridian.platform.common.signinguard import Signin
from meridian.platform.common.signinkeys import KeySet
from meridian.platform.common.signinsession import (
    SessionKeys,
    SessionSettings,
    cookie_name,
    seal_session,
)
from meridian.platform.common.telemetry import make_tracer_provider

MARKER = "CANARY-5c1d9e-subject"
ROLE_MARKER = "role-CANARY-5c1d9e"
NOW = 1_800_000_000.0
ENDS = int(NOW) + 600
SEGMENT_MIN = 12  # shorter pieces of a credential would match by chance


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Kit:
    """What the cases are made from."""

    def __init__(
        self,
        issuer: Any,
        forge: Callable[..., str],
        hmac_sign: Callable[[bytes], bytes],
        ec_key: Any,
        pool: list[Any],
        sessions: SessionSettings,
        previous: bytes,
    ) -> None:
        self.issuer = issuer
        self.forge = forge
        self.hmac_sign = hmac_sign
        self.ec_key = ec_key
        self.pool = pool
        self.sessions = sessions
        self.previous = previous

    def token(self, **overrides: Any) -> str:
        carried = {"sub": MARKER, "note": MARKER, "roles": [ROLE_MARKER]}
        return self.issuer.mint(**{**carried, **overrides})

    def principal(self, **changes: Any) -> Principal:
        fields: dict[str, Any] = {
            "population": "staff",
            "issuer": self.issuer.name,
            "subject": MARKER,
            "roles": frozenset({ROLE_MARKER}),
            "expires_at": ENDS,
        }
        return Principal(**{**fields, **changes})

    def cookie(self, principal: Principal | None = None, now: float = NOW) -> str:
        return seal_session(principal or self.principal(), self.sessions.keys, now)

    def forged_token(self, header: dict[str, Any], signer: Any = None) -> str:
        claims = self.issuer.claims(sub=MARKER, note=MARKER, roles=[ROLE_MARKER])
        return self.forge(header, claims, signer)

    def signed_by_hand(self, claims: dict[str, Any]) -> str:
        private = next(iter(self.issuer.keys.values()))
        return self.forge(
            {"alg": "RS256", "typ": "JWT", "kid": "kid-a"},
            claims,
            lambda data: private.sign(data, padding.PKCS1v15(), hashes.SHA256()),
        )


# Each case: (how to make the credential, the status the route answers).
type Case = tuple[Callable[[Kit], dict[str, Any]], int]


def bearer(token: str) -> dict[str, Any]:
    return {"headers": {"Authorization": f"Bearer {token}"}}


def cookie_of(value: str) -> dict[str, Any]:
    return {"cookies": {cookie_name("staff"): value}}


def altered(value: str) -> str:
    middle = len(value) // 2
    flipped = "A" if value[middle] != "A" else "B"
    return value[:middle] + flipped + value[middle + 1 :]


def role_added(kit: Kit) -> str:
    version, payload, mac = kit.cookie().split(".")
    data = json.loads(unb64(payload))
    data["r"].append("platform-admin")
    return f"{version}.{b64(json.dumps(data, separators=(',', ':')).encode())}.{mac}"


def previous_key_cookie(kit: Kit) -> str:
    rotated = SessionKeys(current=kit.previous)
    return seal_session(
        kit.principal(roles=frozenset({"platform-admin"})), rotated, NOW
    )


CASES: dict[str, Case] = {
    # accepted: the marker is still in no log, span or answer
    "token-good": (lambda k: bearer(k.token(roles=["platform-admin"])), 200),
    "cookie-good": (
        lambda k: cookie_of(k.cookie(k.principal(roles=frozenset({"platform-admin"})))),
        200,
    ),
    "cookie-sealed-with-previous-key": (
        lambda k: cookie_of(previous_key_cookie(k)),
        200,
    ),
    # a valid principal without the role
    "token-without-the-role": (lambda k: bearer(k.token()), 403),
    "token-roles-as-a-string": (lambda k: bearer(k.token(roles=ROLE_MARKER)), 403),
    "token-roles-as-a-number": (lambda k: bearer(k.token(roles=7)), 403),
    "token-roles-nested": (lambda k: bearer(k.token(roles=[[ROLE_MARKER]])), 403),
    "cookie-without-the-role": (lambda k: cookie_of(k.cookie()), 403),
    # tokens refused
    "token-alg-none": (
        lambda k: bearer(k.forged_token({"alg": "none", "kid": "kid-a"})),
        401,
    ),
    "token-hs256-with-the-public-key": (
        lambda k: bearer(k.forged_token({"alg": "HS256", "kid": "kid-a"}, k.hmac_sign)),
        401,
    ),
    "token-es256": (
        lambda k: bearer(k.token(alg="ES256", key=k.ec_key, roles=["platform-admin"])),
        401,
    ),
    "token-ps256": (
        lambda k: bearer(k.token(alg="PS256", roles=["platform-admin"])),
        401,
    ),
    "token-alg-missing": (lambda k: bearer(k.forged_token({"kid": "kid-a"})), 401),
    "token-right-key-of-another-issuer": (
        lambda k: bearer(k.token(key=k.pool[1], roles=["platform-admin"])),
        401,
    ),
    "token-for-another-issuer": (
        lambda k: bearer(k.token(iss="https://other.example/realm")),
        401,
    ),
    "token-wrong-audience": (lambda k: bearer(k.token(aud="elsewhere")), 401),
    "token-audience-list-without-ours": (
        lambda k: bearer(k.token(aud=["a", "b"])),
        401,
    ),
    "token-expired": (lambda k: bearer(k.token(exp=NOW - 3600)), 401),
    "token-not-yet-valid": (lambda k: bearer(k.token(nbf=NOW + 3600)), 401),
    "token-no-subject": (lambda k: bearer(k.token(drop=("sub",))), 401),
    "token-no-expiry": (lambda k: bearer(k.token(drop=("exp",))), 401),
    "token-nan-expiry": (lambda k: bearer(k.token(exp=float("nan"))), 401),
    "token-oversize": (lambda k: bearer(k.token(padding="p" * 9000)), 401),
    "token-unknown-key-id": (lambda k: bearer(k.token(kid="kid-unknown")), 401),
    "token-issuer-as-a-list": (
        lambda k: bearer(
            k.signed_by_hand(
                k.issuer.claims(sub=MARKER, iss=[k.issuer.name], roles=[ROLE_MARKER])
            )
        ),
        401,
    ),
    "token-malformed": (lambda k: bearer(k.token()[:-30] + "." + MARKER), 401),
    "token-with-another-scheme": (
        lambda k: {"headers": {"Authorization": f"Basic {k.token()}"}},
        401,
    ),
    # cookies refused
    "cookie-one-flipped-character": (lambda k: cookie_of(altered(k.cookie())), 401),
    "cookie-truncated": (lambda k: cookie_of(k.cookie()[:-9]), 401),
    "cookie-expired": (
        lambda k: cookie_of(k.cookie(k.principal(expires_at=int(NOW) - 10), NOW - 500)),
        401,
    ),
    "cookie-of-the-other-population": (
        lambda k: cookie_of(k.cookie(k.principal(population="claimant"))),
        401,
    ),
    "cookie-sealed-with-an-unknown-key": (
        lambda k: cookie_of(
            seal_session(
                k.principal(roles=frozenset({"platform-admin"})),
                SessionKeys(current=secrets.token_bytes(32)),
                NOW,
            )
        ),
        401,
    ),
    "cookie-with-a-role-added-by-hand": (lambda k: cookie_of(role_added(k)), 401),
    "cookie-of-another-issuer": (
        lambda k: cookie_of(
            k.cookie(
                k.principal(
                    issuer="https://other.example/realm",
                    roles=frozenset({"platform-admin"}),
                )
            )
        ),
        401,
    ),
}


@pytest.fixture
def kit(
    signin_issuer: Any,
    signin_forge: Callable[..., str],
    signin_hmac_with_public_key: Callable[[bytes], bytes],
    signin_ec_key: Any,
    signin_rsa_pool: list[Any],
) -> Kit:
    sessions = SessionSettings(SessionKeys(current=secrets.token_bytes(32)))
    previous = secrets.token_bytes(32)
    return Kit(
        signin_issuer,
        signin_forge,
        signin_hmac_with_public_key,
        signin_ec_key,
        signin_rsa_pool,
        sessions,
        previous,
    )


def build_app(signin: Signin, exporter: InMemorySpanExporter) -> TestClient:
    service = create_service_app(
        title="canary",
        description="the sign-in canary app",
        service_name="canary",
        tracer_name="canary",
        max_body_bytes=64 * 1024,
        tracer_provider=make_tracer_provider("canary", exporter),
        environ={},
    )
    app = service.app

    @app.get("/admin")
    def admin(
        who: Annotated[Principal, Depends(signin.require_role("platform-admin"))],
    ) -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def span_texts(exporter: InMemorySpanExporter) -> list[str]:
    texts: list[str] = []
    for span in exporter.get_finished_spans():
        texts.append(span.name)
        texts.extend(str(value) for value in (span.attributes or {}).values())
        texts.append(str(span.status.description or ""))
        for event in span.events:
            texts.append(event.name)
            texts.extend(str(v) for v in (event.attributes or {}).values())
        texts.extend(str(link.attributes) for link in span.links)
    return texts


def forbidden_in(credential: str) -> set[str]:
    """The credential, its pieces and the marker."""
    pieces = {
        p for p in credential.replace(" ", ".").split(".") if len(p) >= SEGMENT_MIN
    }
    bare = credential.removeprefix("Bearer ")
    return {MARKER, ROLE_MARKER, credential, bare, *pieces}


@pytest.mark.parametrize("name", list(CASES))
def test_no_credential_marker_reaches_a_log_a_span_or_an_answer(
    name: str,
    kit: Kit,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    caplog: pytest.LogCaptureFixture,
) -> None:
    make, expected = CASES[name]
    exporter = InMemorySpanExporter()
    sessions = SessionSettings(
        SessionKeys(current=kit.sessions.keys.current, previous=kit.previous)
        if name == "cookie-sealed-with-previous-key"
        else kit.sessions.keys
    )
    signin = Signin(signin_settings, signin_keys, sessions, lambda: NOW)
    client = build_app(signin, exporter)
    request = make(kit)
    credentials = [
        *request.get("headers", {}).values(),
        *request.get("cookies", {}).values(),
    ]

    for cookie, value in request.get("cookies", {}).items():
        client.cookies.set(cookie, value)

    with caplog.at_level(logging.DEBUG):
        response = client.get("/admin", headers=request.get("headers"))

    assert response.status_code == expected, name
    # Not vacuous: the request was traced and a refusal was logged.
    assert any(s.startswith("GET") for s in span_texts(exporter))
    if expected != 200:
        assert any("sign-in refused" in r.getMessage() for r in caplog.records)
    forbidden: set[str] = set()
    for credential in credentials:
        forbidden |= forbidden_in(credential)
    haystacks = {
        "log": [
            part
            for r in caplog.records
            for part in (
                r.getMessage(),
                str(r.msg),
                str(r.args),
                str(r.exc_text),
                str(r.__dict__),
            )
        ],
        "span": span_texts(exporter),
        "answer": [response.text, str(dict(response.headers))],
    }
    for place, texts in haystacks.items():
        for text in texts:
            for needle in forbidden:
                assert needle not in text, f"{name}: {place} holds a credential"


def test_a_key_url_that_is_down_leaks_nothing_either(
    kit: Kit,
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = InMemorySpanExporter()
    signin = Signin(signin_settings, signin_keys, kit.sessions, lambda: NOW)
    client = build_app(signin, exporter)
    token = kit.token(roles=["platform-admin"])
    signin_issuer.down = True

    with caplog.at_level(logging.DEBUG):
        response = client.get("/admin", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert any("keys-unavailable" in r.getMessage() for r in caplog.records)
    everything = [
        *(r.getMessage() + str(r.__dict__) for r in caplog.records),
        *span_texts(exporter),
        response.text,
        str(dict(response.headers)),
    ]
    for needle in forbidden_in(token):
        assert not [t for t in everything if needle in t]
