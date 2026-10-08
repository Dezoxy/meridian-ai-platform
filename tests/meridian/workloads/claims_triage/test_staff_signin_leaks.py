"""What the staff sign-in leaves behind (S021, Y4): a code, a state, a cookie and
a subject are in no log record, no access line, no span and no body, on the
good path and on every refusal.

The scan is the one of ``test_signin_flow_leaks.py`` in the app: a canary goes in
at each place a person or an issuer can put text, and the whole of what the
process wrote is searched for it. The session cookie the callback sets and the
transaction cookie are where a session belongs and are not scanned; everything
else is."""

import json
import logging
import secrets
from collections.abc import Callable

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from staffsigninsupport import (
    CLAIM,
    SESSION_COOKIE,
    TRANSACTION_COOKIE,
    StaffRig,
    begin,
    browser,
    build_app,
    send,
)

from meridian.platform.common.logformat import configure_logging
from meridian.platform.common.logredaction import install_log_redaction

MARK = "STAFFMARK-" + secrets.token_hex(6)
SEGMENT_MIN = 12  # shorter pieces of a cookie would match by chance
SAME_ORIGIN = {"Origin": "http://meridian.localhost:8088"}


def pieces(secret: str) -> list[str]:
    """A secret and, when it has dots, each long piece of it."""
    return [secret, *[p for p in secret.split(".") if len(p) >= SEGMENT_MIN]]


@pytest.fixture(scope="module")
def signer() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(65537, 2048)


class Scene:
    """An app, a recording of its spans, and what the scan must not find."""

    def __init__(self, signer: rsa.RSAPrivateKey) -> None:
        self.rig = StaffRig(signer)
        self.exporter = InMemorySpanExporter()
        self.client: TestClient = browser(build_app(self.rig, exporter=self.exporter))
        self.started = begin(self.client, f"/adjuster/claims/{CLAIM}")
        self.forbidden: list[str] = [
            MARK,
            self.started.state,
            self.started.nonce,
            *pieces(self.started.cookie),
        ]
        self.seen: list[str] = []

    def arm(self, **changes: object) -> None:
        self.rig.arm(self.started, **changes)
        self.rig.endpoint.code = "code-" + MARK
        body = self.rig.endpoint.body
        self.forbidden += [body["id_token"], body["access_token"], "code-" + MARK]

    def callback(self, **changes: str | None) -> httpx.Response:
        response = send(
            self.client,
            "GET",
            self.rig.callback_path(self.started, **changes),
            cookie=f"{TRANSACTION_COOKIE}={self.started.cookie}",
        )
        self.note(response)
        return response

    def note(self, response: httpx.Response) -> None:
        """Everything of a response that is not a cookie (the cookie is where the
        session belongs)."""
        headers = [
            f"{name}: {value}"
            for name, value in response.headers.multi_items()
            if name.lower() != "set-cookie"
        ]
        self.seen += [response.text, *headers]

    def spans_text(self) -> str:
        parts: list[str] = []
        for span in self.exporter.get_finished_spans():
            parts += [span.name, span.status.description or ""]
            parts += [str(v) for v in (span.attributes or {}).values()]
            for event in span.events:
                parts += [event.name]
                parts += [str(v) for v in (event.attributes or {}).values()]
        return "\n".join(parts)


def happy(scene: Scene) -> None:
    scene.arm(sub="subject-" + MARK, roles=["adjuster", "role-" + MARK])
    scene.forbidden.append("subject-" + MARK)
    response = scene.callback()
    assert response.headers["location"] == f"/adjuster/claims/{CLAIM}"
    # signed in: the pages are served to the cookie that was set, and show nothing
    [line] = [
        x
        for x in response.headers.get_list("set-cookie")
        if x.startswith(SESSION_COOKIE + "=")
    ]
    session = line.split(";", 1)[0]
    scene.note(send(scene.client, "GET", "/adjuster/claims", cookie=session))
    scene.note(send(scene.client, "GET", f"/adjuster/claims/{CLAIM}", cookie=session))


def wrong_state(scene: Scene) -> None:
    scene.arm()
    scene.callback(state="state-" + MARK)


def issuer_error(scene: Scene) -> None:
    scene.callback(error="error-" + MARK, error_description="why-" + MARK)


def wrong_code(scene: Scene) -> None:
    scene.arm()
    scene.callback(code="other-" + MARK)


def endpoint_down(scene: Scene) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cannot reach " + MARK, request=request)

    scene.arm()
    scene.rig.endpoint.override = down
    scene.callback()


def bad_id_token(scene: Scene) -> None:
    scene.arm(sub="subject-" + MARK, aud="aud-" + MARK)
    scene.forbidden.append("subject-" + MARK)
    scene.callback()


def wrong_role(scene: Scene) -> None:
    subject = "subject-" + MARK
    scene.forbidden += [subject, "role-" + MARK]
    token = scene.rig.bearer(("role-" + MARK,), sub=subject)
    scene.forbidden.append(token)
    scene.note(send(scene.client, "GET", "/adjuster/claims", bearer=token))
    scene.note(
        send(scene.client, "GET", f"/adjuster/claims/{CLAIM}/proposal", bearer=token)
    )
    cookie = scene.rig.cookie(("role-" + MARK,)).replace(SESSION_COOKIE + "=", "")
    scene.forbidden += pieces(cookie)
    scene.note(
        send(
            scene.client,
            "GET",
            "/adjuster/claims",
            cookie=f"{SESSION_COOKIE}={cookie}",
        )
    )


def garbage_credentials(scene: Scene) -> None:
    for headers in (
        {"Authorization": "Bearer " + MARK},
        {"Authorization": "Bearer a.b." + MARK},
        {"Authorization": "Basic " + MARK},
    ):
        scene.note(
            send(scene.client, "GET", "/adjuster/claims", headers=headers),
        )
        scene.note(send(scene.client, "POST", f"/claims/{CLAIM}/triage", json={}))
    scene.note(
        send(
            scene.client,
            "GET",
            "/adjuster/claims",
            cookie=f"{SESSION_COOKIE}=v1.{MARK}.{MARK}",
        )
    )


def keys_down(scene: Scene) -> None:
    scene.rig.keys_down = True
    token = scene.rig.bearer(sub="subject-" + MARK)
    scene.forbidden += [token, "subject-" + MARK]
    scene.note(send(scene.client, "GET", "/adjuster/claims", bearer=token))


def sign_out(scene: Scene) -> None:
    cookie = scene.rig.cookie()
    scene.forbidden += pieces(cookie.split("=", 1)[1])
    scene.note(
        send(
            scene.client,
            "POST",
            "/auth/sign-out",
            cookie=cookie,
            headers={"Origin": "https://other-site.example/" + MARK},
        )
    )
    scene.note(
        send(scene.client, "POST", "/auth/sign-out", cookie=cookie, headers=SAME_ORIGIN)
    )


SCENARIOS: dict[str, Callable[[Scene], None]] = {
    "happy": happy,
    "wrong-state": wrong_state,
    "issuer-error": issuer_error,
    "wrong-code": wrong_code,
    "endpoint-down": endpoint_down,
    "bad-id-token": bad_id_token,
    "wrong-role": wrong_role,
    "garbage-credentials": garbage_credentials,
    "keys-down": keys_down,
    "sign-out": sign_out,
}


def _is_the_test_clients(record: logging.LogRecord) -> bool:
    return record.name.startswith("httpx") and (
        "meridian.localhost" in record.getMessage()
    )


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_nothing_secret_is_in_a_log_a_span_or_a_body_on_any_path(
    signer: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    scene = Scene(signer)

    with caplog.at_level(logging.DEBUG):
        SCENARIOS[name](scene)

    # What the app wrote. The test's own client logs every request it makes at
    # INFO, query string and all: that is the client's line, not the app's.
    records = [r for r in caplog.records if not _is_the_test_clients(r)]
    logs = "\n".join(
        [
            *(f"{r.name} {r.getMessage()}" for r in records),
            *(repr(r.args) for r in records),
            *(str(r.exc_info) for r in records if r.exc_info),
        ]
    )
    everything = "\n".join([logs, scene.spans_text(), *scene.seen])
    for secret in scene.forbidden:
        assert secret not in everything, f"{name}: a secret is somewhere it must not be"


def test_the_scan_is_not_blind_a_planted_text_in_each_place_is_found(
    signer: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
) -> None:
    scene = Scene(signer)

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("elsewhere").warning("leak %s", MARK)
        response = send(scene.client, "GET", f"/adjuster/claims/{CLAIM}/proposal")
    scene.note(response)

    assert MARK in caplog.text
    # the span scan reads real spans, and the body scan real bodies
    assert "GET /adjuster/claims/{claim_id}/proposal" in scene.spans_text()
    assert "request refused" in "\n".join(scene.seen)


def test_the_callbacks_query_is_in_no_span_of_the_request(
    signer: rsa.RSAPrivateKey,
) -> None:
    scene = Scene(signer)
    scene.arm()

    response = scene.callback()

    spans = scene.exporter.get_finished_spans()
    assert response.status_code == 303
    assert [s for s in spans if s.name.startswith("GET /auth/callback")]
    text = scene.spans_text()
    assert "?" not in text
    assert scene.started.state not in text
    assert "code-" + MARK not in text
    assert "/auth/callback" in text


def test_the_callbacks_query_is_not_in_the_access_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_log_redaction()
    configure_logging("claims-api")
    code, state = "code-" + MARK, "state-" + MARK

    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "203.0.113.7:51234",
        "GET",
        f"/auth/callback?code={code}&state={state}&iss=http%3A%2F%2Fissuer",
        "1.1",
        303,
    )

    out = capsys.readouterr().out
    line = json.loads(out)
    assert line["path"] == "/auth/callback"
    assert code not in out
    assert state not in out
    assert "iss=" not in out
