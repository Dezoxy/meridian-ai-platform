"""The callback of the pages' sign-in (S021, Y3): the happy path from the start of
a sign-in to the session cookie, what is refused before the issuer is asked, and
what the code exchange refuses. The ID token's own checks are in
``test_signin_flow_idtoken.py``."""

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from signinflowsupport import (
    CALLBACK_PATH,
    CLIENT_ID,
    FRONT_ISSUER,
    NOW,
    REDIRECT_URI,
    TOKEN_URL,
    FlowKit,
    Started,
    Stream,
    answer,
    cookie_value,
    set_cookies,
)
from starlette.datastructures import QueryParams
from starlette.responses import RedirectResponse

from meridian.platform.common import signinflow
from meridian.platform.common.signinflow import (
    MAX_CALLBACK_VALUE_CHARS,
    MAX_TOKEN_RESPONSE_BYTES,
    SigninOutcome,
)
from meridian.platform.common.signinkeys import CalledFromEventLoop
from meridian.platform.common.signinsession import open_session
from meridian.platform.common.signinstate import TRANSACTION_COOKIE_PREFIX, FlowReason

LATER = NOW + 5  # the person was at the issuer's login page for five seconds


def finish(
    kit: FlowKit,
    started: Started,
    *,
    query: QueryParams | None = None,
    cookie: str | None = "default",
    now: float = LATER,
) -> SigninOutcome:
    return kit.flow.finish(
        query if query is not None else kit.callback(started),
        started.cookie if cookie == "default" else cookie,
        now,
    )


def respond(outcome: SigninOutcome) -> RedirectResponse:
    response = RedirectResponse(outcome.return_to, status_code=303)
    outcome.apply_to(response)
    return response


def assert_cleared(response: RedirectResponse) -> None:
    line = set_cookies(response)[f"{TRANSACTION_COOKIE_PREFIX}staff"]
    lowered = line.lower()
    assert 'staff=""' in lowered or "staff=;" in lowered
    assert "max-age=0" in lowered
    assert f"path={CALLBACK_PATH}".lower() in lowered


def assert_refused(
    kit: FlowKit,
    outcome: SigninOutcome,
    reason: FlowReason,
    *,
    asked: int | None = None,
) -> None:
    assert not outcome.signed_in
    assert outcome.reason is reason
    assert outcome.return_to == "/"
    response = respond(outcome)
    assert "meridian_session_staff" not in set_cookies(response)
    assert_cleared(response)
    if asked is not None:
        assert kit.endpoint.calls == asked


# ── the happy path, end to end ──────────────────────────────────────────────
def test_a_person_who_signs_in_gets_a_session_and_no_transaction(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin("/adjuster/claims/c-1")
    flow_kit.arm(started)

    outcome = finish(flow_kit, started)

    assert outcome.signed_in
    assert outcome.reason is None
    assert outcome.return_to == "/adjuster/claims/c-1"
    response = respond(outcome)
    cookies = set_cookies(response)
    session = cookie_value(cookies["meridian_session_staff"])
    principal = open_session(session, flow_kit.session_keys, LATER, "staff")
    assert principal.population == "staff"
    assert principal.issuer == FRONT_ISSUER
    assert principal.subject == "f2c1f7de-0b6f-4c55-8a0e-6f4a8d3e9a11"
    assert principal.roles == frozenset({"adjuster"})
    assert principal.expires_at == int(NOW) + 300
    assert principal.via == "cookie"
    assert_cleared(response)


def test_the_session_cookie_is_the_sessions_and_ends_with_the_token(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)

    response = respond(finish(flow_kit, started))

    line = set_cookies(response)["meridian_session_staff"].lower()
    assert "httponly" in line
    assert "samesite=lax" in line
    assert "path=/;" in line + ";"
    assert "max-age=295" in line  # 300 s token, 5 s at the login page
    assert "secure" not in line
    assert response.headers["cache-control"] == "no-store"


def test_no_token_and_no_code_is_in_any_cookie_the_flow_sets(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin()
    id_token = flow_kit.arm(started)
    access = flow_kit.endpoint.body["access_token"]

    response = respond(finish(flow_kit, started))

    everything = " ".join(response.headers.getlist("set-cookie"))
    for forbidden in (id_token, access, flow_kit.endpoint.code):
        assert forbidden not in everything
        assert all(part not in everything for part in id_token.split("."))
    # the session cookie holds the six fields of a session and no more
    session = cookie_value(set_cookies(response)["meridian_session_staff"])
    payload = json.loads(base64.urlsafe_b64decode(session.split(".")[1] + "=="))
    assert set(payload) == {"p", "i", "s", "r", "e"}


def test_the_code_is_exchanged_once_at_the_back_channel_and_nothing_else_is_sent(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)

    finish(flow_kit, started)

    assert flow_kit.endpoint.calls == 1
    request = flow_kit.endpoint.requests[0]
    form = dict(parse_qsl(request.content.decode("ascii")))
    assert request.method == "POST"
    assert str(request.url) == TOKEN_URL
    assert set(form) == {"grant_type", "code", "redirect_uri", "code_verifier"}
    assert form["grant_type"] == "authorization_code"
    assert form["redirect_uri"] == REDIRECT_URI
    credential = flow_kit.settings.client_credential.get_secret_value()
    assert credential not in request.content.decode()
    assert credential not in str(request.url)
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    basic = base64.b64decode(request.headers["authorization"].split()[1]).decode()
    assert basic == f"{CLIENT_ID}:{credential}"
    assert {"cookie", "referer"}.isdisjoint(request.headers)


def test_the_issuer_parameter_is_optional_but_not_wrong(flow_kit: FlowKit) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)

    without = finish(flow_kit, started, query=flow_kit.callback(started, iss=None))

    assert without.signed_in


def test_a_sign_in_is_not_possible_twice_from_one_code(flow_kit: FlowKit) -> None:
    # The issuer redeems a code once: the fake answers a second exchange with
    # invalid_grant when it has already issued the tokens.
    started = flow_kit.begin()
    flow_kit.arm(started)
    first = finish(flow_kit, started)

    second = finish(flow_kit, started)

    assert first.signed_in
    assert_refused(flow_kit, second, FlowReason.EXCHANGE_FAILED)


# ── refused before the issuer is asked ──────────────────────────────────────
def test_no_cookie_is_refused_and_the_issuer_is_not_asked(flow_kit: FlowKit) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)

    outcome = finish(flow_kit, started, cookie=None)

    assert_refused(flow_kit, outcome, FlowReason.NO_TRANSACTION, asked=0)


@pytest.mark.parametrize("cookie", ["", " "])
def test_an_empty_cookie_is_refused_as_altered(flow_kit: FlowKit, cookie: str) -> None:
    started = flow_kit.begin()

    outcome = finish(flow_kit, started, cookie=cookie)

    assert_refused(flow_kit, outcome, FlowReason.TRANSACTION_ALTERED, asked=0)


def test_a_tampered_cookie_is_refused(flow_kit: FlowKit) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)
    mac = started.cookie[-1]
    tampered = started.cookie[:-1] + ("A" if mac != "A" else "B")

    outcome = finish(flow_kit, started, cookie=tampered)

    assert_refused(flow_kit, outcome, FlowReason.TRANSACTION_ALTERED, asked=0)


def test_an_expired_cookie_is_refused(flow_kit: FlowKit) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)

    outcome = finish(flow_kit, started, now=NOW + 600)

    assert_refused(flow_kit, outcome, FlowReason.TRANSACTION_EXPIRED, asked=0)


def test_a_cookie_one_second_short_of_expiry_is_still_good(flow_kit: FlowKit) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started, exp=int(NOW) + 900)

    outcome = finish(flow_kit, started, now=NOW + 599)

    assert outcome.signed_in


def test_a_session_cookie_offered_as_the_transaction_is_refused(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)
    signed_in = respond(finish(flow_kit, started))
    session = cookie_value(set_cookies(signed_in)["meridian_session_staff"])

    outcome = finish(flow_kit, started, cookie=session)

    assert_refused(flow_kit, outcome, FlowReason.TRANSACTION_VERSION, asked=1)


def test_another_populations_transaction_is_refused(flow_kit: FlowKit) -> None:
    started = flow_kit.begin()
    forged = flow_kit.forged_cookie(started, population="claimant")

    outcome = finish(flow_kit, started, cookie=forged)

    assert_refused(flow_kit, outcome, FlowReason.TRANSACTION_POPULATION, asked=0)


@pytest.mark.parametrize(
    "state",
    [None, "", "wrong", "x" * 43, "é" * 43],
    ids=["missing", "empty", "short", "same-length", "non-ascii"],
)
def test_a_state_that_does_not_match_is_refused(
    flow_kit: FlowKit, state: str | None
) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)

    outcome = finish(flow_kit, started, query=flow_kit.callback(started, state=state))

    assert_refused(flow_kit, outcome, FlowReason.STATE_MISMATCH, asked=0)


def test_the_state_of_another_sign_in_is_refused(flow_kit: FlowKit) -> None:
    # Two tabs: the cookie of the second holds the second's state.
    first, second = flow_kit.begin(), flow_kit.begin()
    flow_kit.arm(second)

    outcome = finish(flow_kit, second, query=flow_kit.callback(first))

    assert_refused(flow_kit, outcome, FlowReason.STATE_MISMATCH, asked=0)


def test_the_state_is_compared_in_constant_time(
    flow_kit: FlowKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Any, Any]] = []
    real = signinflow.hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(signinflow.hmac, "compare_digest", spy)
    started = flow_kit.begin()
    flow_kit.arm(started)

    finish(flow_kit, started)

    assert (started.state.encode(), started.state.encode()) in seen


def test_the_issuers_error_is_refused_and_its_description_is_not_echoed(
    flow_kit: FlowKit, caplog: pytest.LogCaptureFixture
) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)
    query = flow_kit.callback(
        started,
        error="access_denied",
        error_description="FLOWMARK-description-1c",
        code=None,
    )

    with caplog.at_level("DEBUG"):
        outcome = finish(flow_kit, started, query=query)

    assert_refused(flow_kit, outcome, FlowReason.ISSUER_ERROR, asked=0)
    assert "FLOWMARK-description-1c" not in caplog.text + repr(outcome)
    assert "access_denied" not in caplog.text + repr(outcome)


def test_an_error_beside_a_code_is_still_refused(flow_kit: FlowKit) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)

    outcome = finish(
        flow_kit, started, query=flow_kit.callback(started, error="server_error")
    )

    assert_refused(flow_kit, outcome, FlowReason.ISSUER_ERROR, asked=0)


@pytest.mark.parametrize("code", [None, ""])
def test_a_missing_or_empty_code_is_refused(
    flow_kit: FlowKit, code: str | None
) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)

    outcome = finish(flow_kit, started, query=flow_kit.callback(started, code=code))

    assert_refused(flow_kit, outcome, FlowReason.NO_CODE, asked=0)


def test_an_issuer_parameter_that_names_another_issuer_is_refused(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)

    outcome = finish(
        flow_kit,
        started,
        query=flow_kit.callback(started, iss="http://evil.example/realms/x"),
    )

    assert_refused(flow_kit, outcome, FlowReason.ISSUER_PARAMETER, asked=0)


@pytest.mark.parametrize("name", ["state", "code", "error", "iss"])
def test_a_parameter_sent_twice_is_refused(flow_kit: FlowKit, name: str) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)
    pairs = list(flow_kit.callback(started, error="server_error").multi_items())
    pairs.append((name, dict(pairs)[name]))

    outcome = finish(flow_kit, started, query=QueryParams(pairs))

    assert_refused(flow_kit, outcome, FlowReason.BAD_CALLBACK, asked=0)


@pytest.mark.parametrize("name", ["state", "code", "iss"])
def test_a_parameter_longer_than_the_bound_is_refused(
    flow_kit: FlowKit, name: str
) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)
    query = flow_kit.callback(started, **{name: "a" * (MAX_CALLBACK_VALUE_CHARS + 1)})

    outcome = finish(flow_kit, started, query=query)

    assert_refused(flow_kit, outcome, FlowReason.BAD_CALLBACK, asked=0)


def test_the_flow_refuses_to_run_on_an_event_loop(flow_kit: FlowKit) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)

    async def on_the_loop() -> SigninOutcome:
        return finish(flow_kit, started)

    with pytest.raises(CalledFromEventLoop):
        asyncio.run(on_the_loop())

    assert flow_kit.endpoint.calls == 0


# ── the exchange ────────────────────────────────────────────────────────────
def armed(flow_kit: FlowKit) -> Started:
    started = flow_kit.begin()
    flow_kit.arm(started)
    return started


def reply(flow_kit: FlowKit, make: Callable[[httpx.Request], httpx.Response]) -> None:
    flow_kit.endpoint.override = make


@pytest.mark.parametrize("status", [400, 401, 403, 500, 503])
def test_an_exchange_the_issuer_refuses_is_refused(
    flow_kit: FlowKit, status: int
) -> None:
    started = armed(flow_kit)
    reply(flow_kit, lambda _: answer(status, {"error": "invalid_grant"}))

    outcome = finish(flow_kit, started)

    assert_refused(flow_kit, outcome, FlowReason.EXCHANGE_FAILED, asked=1)


def test_a_wrong_credential_gets_no_tokens(flow_kit: FlowKit) -> None:
    started = armed(flow_kit)
    flow_kit.endpoint.credential = "not-the-clients"

    outcome = finish(flow_kit, started)

    assert_refused(flow_kit, outcome, FlowReason.EXCHANGE_FAILED, asked=1)


def test_a_redirect_from_the_token_endpoint_is_not_followed(flow_kit: FlowKit) -> None:
    started = armed(flow_kit)
    reply(
        flow_kit,
        lambda _: answer(307, b"", {"location": "http://evil.example/t"}),
    )

    outcome = finish(flow_kit, started)

    assert_refused(flow_kit, outcome, FlowReason.EXCHANGE_FAILED, asked=1)


def test_a_token_endpoint_that_is_down_is_refused(flow_kit: FlowKit) -> None:
    started = armed(flow_kit)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("FLOWMARK-host-unreachable", request=request)

    reply(flow_kit, down)

    outcome = finish(flow_kit, started)

    assert_refused(flow_kit, outcome, FlowReason.EXCHANGE_FAILED, asked=1)


def test_a_slow_token_endpoint_is_cut_by_one_deadline(
    flow_kit: FlowKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signinflow, "EXCHANGE_DEADLINE_SECONDS", 0.2)
    started = armed(flow_kit)
    flow_kit.endpoint.delay = 30.0

    began = time.monotonic()
    outcome = finish(flow_kit, started)
    waited = time.monotonic() - began

    assert_refused(flow_kit, outcome, FlowReason.EXCHANGE_DEADLINE, asked=1)
    assert waited < 3.0


def test_a_token_endpoint_that_drips_its_body_is_cut_by_the_same_deadline(
    flow_kit: FlowKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signinflow, "EXCHANGE_DEADLINE_SECONDS", 0.3)
    started = armed(flow_kit)

    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                await asyncio.sleep(0.1)
                yield b" "

    reply(flow_kit, lambda _: httpx.Response(200, stream=Drip()))

    began = time.monotonic()
    outcome = finish(flow_kit, started)

    assert_refused(flow_kit, outcome, FlowReason.EXCHANGE_DEADLINE, asked=1)
    assert time.monotonic() - began < 3.0


def test_the_connect_timeout_is_below_the_deadline() -> None:
    # A cancelled TLS handshake leaves its socket open until a collection; the
    # connect timeout must end the handshake before the deadline can.
    assert signinflow.EXCHANGE_TIMEOUT.connect is not None
    assert signinflow.EXCHANGE_TIMEOUT.connect < signinflow.EXCHANGE_DEADLINE_SECONDS


def test_an_answer_that_is_too_large_is_refused_without_being_read(
    flow_kit: FlowKit,
) -> None:
    started = armed(flow_kit)
    stream = Stream(50 * 1024 * 1024)
    reply(flow_kit, lambda _: httpx.Response(200, stream=stream))

    outcome = finish(flow_kit, started)

    assert_refused(flow_kit, outcome, FlowReason.EXCHANGE_TOO_LARGE, asked=1)
    assert stream.handed_over <= MAX_TOKEN_RESPONSE_BYTES + 2 * stream.chunk


def test_an_answer_of_exactly_the_bound_is_read_and_one_byte_more_is_not(
    flow_kit: FlowKit,
) -> None:
    started = armed(flow_kit)
    body = json.dumps(flow_kit.endpoint.body).encode()
    padding = MAX_TOKEN_RESPONSE_BYTES - len(body)
    assert padding > 0

    reply(flow_kit, lambda _: answer(200, body + b" " * padding))
    fits = finish(flow_kit, started)
    reply(flow_kit, lambda _: answer(200, body + b" " * (padding + 1)))
    over = finish(flow_kit, started)

    assert fits.signed_in
    assert_refused(flow_kit, over, FlowReason.EXCHANGE_TOO_LARGE)


@pytest.mark.parametrize(
    "encoding", ["gzip", "br", "deflate", "zstd", "identity, gzip"]
)
def test_a_content_encoding_is_refused_before_a_byte_of_the_body(
    flow_kit: FlowKit, encoding: str
) -> None:
    started = armed(flow_kit)
    stream = Stream(1000)
    reply(
        flow_kit,
        lambda _: httpx.Response(
            200, headers={"content-encoding": encoding}, stream=stream
        ),
    )

    outcome = finish(flow_kit, started)

    assert_refused(flow_kit, outcome, FlowReason.EXCHANGE_FAILED, asked=1)
    assert stream.handed_over == 0


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"<html>login</html>",
        b"[]",
        b'"id_token"',
        b"null",
        b"42",
        b'{"id_token": ',
        b"[" * 60_000,
        b"\xff\xfe",
        b'{"a": NaN',
    ],
)
def test_an_answer_that_is_not_a_json_object_is_refused(
    flow_kit: FlowKit, body: bytes
) -> None:
    started = armed(flow_kit)
    reply(flow_kit, lambda _: answer(200, body))

    outcome = finish(flow_kit, started)

    assert_refused(flow_kit, outcome, FlowReason.EXCHANGE_MALFORMED, asked=1)


@pytest.mark.parametrize(
    "document",
    [
        {"access_token": "a.b.c", "token_type": "Bearer"},
        {"access_token": "a.b.c", "id_token": None},
        {"access_token": "a.b.c", "id_token": ""},
        {"access_token": "a.b.c", "id_token": 42},
        {"access_token": "a.b.c", "id_token": ["a.b.c"]},
        {"refresh_token": "a.b.c"},
        {},
    ],
)
def test_an_answer_with_an_access_token_and_no_id_token_is_refused(
    flow_kit: FlowKit, document: dict[str, Any]
) -> None:
    started = armed(flow_kit)
    reply(flow_kit, lambda _: answer(200, document))

    outcome = finish(flow_kit, started)

    assert_refused(flow_kit, outcome, FlowReason.ID_MISSING, asked=1)


def test_an_access_token_in_the_place_of_the_id_token_is_never_accepted(
    flow_kit: FlowKit,
) -> None:
    # The access token as the API gets it: the API's audience, `Bearer`, no nonce.
    started = flow_kit.begin()
    access = flow_kit.mint(
        started, aud="meridian-claims-api", typ="Bearer", drop=("nonce",)
    )
    flow_kit.endpoint.body = {"access_token": access, "id_token": access}

    outcome = finish(flow_kit, started)

    assert_refused(flow_kit, outcome, FlowReason.ID_AUDIENCE, asked=1)


def test_an_access_token_that_even_names_the_client_is_refused_by_its_type(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin()
    access = flow_kit.mint(started, typ="Bearer", drop=("nonce",))
    flow_kit.endpoint.body = {"id_token": access}

    outcome = finish(flow_kit, started)

    assert_refused(flow_kit, outcome, FlowReason.ID_TYPE, asked=1)
