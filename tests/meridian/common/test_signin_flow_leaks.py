"""What the pages' sign-in leaves behind (S021, Y3): no log line, outcome text or
exception text holds a token, a code, a verifier, a nonce, a state, a cookie or
the client's credential, on any path; one log line per reason per window; and
the sign-out."""

import logging
import re
import secrets
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from signinflowsupport import (
    CLIENT_ID,
    END_SESSION_URL,
    NOW,
    POST_LOGOUT_URI,
    FlowKit,
    Started,
    answer,
    cookie_value,
    set_cookies,
)
from starlette.datastructures import QueryParams
from starlette.responses import RedirectResponse

from meridian.platform.common.signinflow import SigninOutcome
from meridian.platform.common.signinstate import (
    FlowReason,
    Transaction,
    open_transaction,
)

LATER = NOW + 5
LOGGER = "meridian.platform.common.signinflow"
SEGMENT_MIN = 12  # shorter pieces of a credential would match by chance
MARK = "FLOWMARK-" + secrets.token_hex(6)


def pieces(secret: str) -> list[str]:
    """A secret and, when it has dots, each long piece of it."""
    return [secret, *[p for p in secret.split(".") if len(p) >= SEGMENT_MIN]]


class Scene:
    """A start, and what a scan must not find afterwards."""

    def __init__(self, kit: FlowKit) -> None:
        self.kit = kit
        self.started = kit.begin()
        opened = open_transaction(self.started.cookie, kit.session_keys, NOW, "staff")
        assert isinstance(opened, Transaction)
        self.forbidden: list[str] = [
            *pieces(self.started.cookie),
            self.started.state,
            self.started.nonce,
            opened.verifier,
            kit.settings.client_credential.get_secret_value(),
            kit.endpoint.code,
            MARK,
        ]

    def add(self, *secrets_found: str) -> None:
        for secret in secrets_found:
            self.forbidden.extend(pieces(secret))

    def finish(self, query: QueryParams | None = None, **kwargs: Any) -> SigninOutcome:
        cookie = kwargs.get("cookie", self.started.cookie)
        return self.kit.flow.finish(
            query if query is not None else self.kit.callback(self.started),
            cookie,
            kwargs.get("now", LATER),
        )


def arm(scene: Scene, **changes: Any) -> None:
    id_token = scene.kit.arm(scene.started, **changes)
    scene.add(id_token, scene.kit.endpoint.body["access_token"])


def happy(scene: Scene) -> SigninOutcome:
    arm(scene, sub="subject-" + MARK, roles=["role-" + MARK])
    return scene.finish()


def no_cookie(scene: Scene) -> SigninOutcome:
    arm(scene)
    return scene.finish(cookie=None)


def tampered_cookie(scene: Scene) -> SigninOutcome:
    arm(scene)
    return scene.finish(cookie=scene.started.cookie[:-4] + "AAAA")


def foreign_cookie(scene: Scene) -> SigninOutcome:
    arm(scene)
    return scene.finish(cookie=MARK + "." + scene.started.cookie)


def wrong_state(scene: Scene) -> SigninOutcome:
    arm(scene)
    return scene.finish(scene.kit.callback(scene.started, state="state-" + MARK))


def issuer_error(scene: Scene) -> SigninOutcome:
    query = scene.kit.callback(
        scene.started, error="error-" + MARK, error_description="why-" + MARK
    )
    return scene.finish(query)


def wrong_code(scene: Scene) -> SigninOutcome:
    arm(scene)
    return scene.finish(scene.kit.callback(scene.started, code="code-" + MARK))


def refused_by_the_endpoint(scene: Scene) -> SigninOutcome:
    scene.kit.endpoint.override = lambda _: answer(
        400, {"error": "invalid_grant", "error_description": "bad " + MARK}
    )
    return scene.finish()


def endpoint_down(scene: Scene) -> SigninOutcome:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cannot reach " + MARK, request=request)

    scene.kit.endpoint.override = down
    return scene.finish()


def not_json(scene: Scene) -> SigninOutcome:
    scene.kit.endpoint.override = lambda _: answer(200, MARK.encode())
    return scene.finish()


def no_id_token(scene: Scene) -> SigninOutcome:
    scene.kit.endpoint.override = lambda _: answer(
        200, {"access_token": "access-" + MARK}
    )
    return scene.finish()


def id_token_with(claim: str) -> Callable[[Scene], SigninOutcome]:
    def scenario(scene: Scene) -> SigninOutcome:
        arm(scene, **{claim: "bad-" + MARK})
        return scene.finish()

    return scenario


def unknown_key(scene: Scene) -> SigninOutcome:
    arm(scene, kid="kid-" + MARK)
    return scene.finish()


def no_algorithm(scene: Scene) -> SigninOutcome:
    scene.kit.endpoint.body = {"id_token": f"e30.e30.{MARK}"}
    return scene.finish()


SCENARIOS: dict[str, Callable[[Scene], SigninOutcome]] = {
    "happy": happy,
    "no-cookie": no_cookie,
    "tampered-cookie": tampered_cookie,
    "foreign-cookie": foreign_cookie,
    "wrong-state": wrong_state,
    "issuer-error": issuer_error,
    "wrong-code": wrong_code,
    "endpoint-refuses": refused_by_the_endpoint,
    "endpoint-down": endpoint_down,
    "not-json": not_json,
    "no-id-token": no_id_token,
    "unknown-key": unknown_key,
    "no-algorithm": no_algorithm,
    **{f"bad-{c}": id_token_with(c) for c in ("iss", "aud", "azp", "typ", "nonce")},
    **{f"bad-{c}": id_token_with(c) for c in ("sub", "exp", "iat", "nbf")},
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_nothing_secret_is_in_a_log_line_or_the_outcome_on_any_path(
    flow_kit: FlowKit, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    scene = Scene(flow_kit)

    with caplog.at_level(logging.DEBUG):
        outcome = SCENARIOS[name](scene)
        response = RedirectResponse(outcome.return_to, status_code=303)
        outcome.apply_to(response)

    text = "\n".join(
        [
            caplog.text,
            *(r.getMessage() for r in caplog.records),
            *(repr(r.args) for r in caplog.records),
            *(str(r.exc_info) for r in caplog.records if r.exc_info),
            repr(outcome),
            str(outcome.reason),
            outcome.return_to,
        ]
    )
    for secret in scene.forbidden:
        assert secret not in text, f"{name}: a secret is in the log or the outcome"
    # the response may hold the session cookie (the point of signing in) but
    # never a token or a code
    cookies = " ".join(response.headers.getlist("set-cookie"))
    for secret in scene.forbidden:
        assert secret not in cookies, f"{name}: a secret is in a cookie"


def test_the_scan_is_not_blind_a_refusal_does_log_its_reason(
    flow_kit: FlowKit, caplog: pytest.LogCaptureFixture
) -> None:
    scene = Scene(flow_kit)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        outcome = no_cookie(scene)

    assert outcome.reason is FlowReason.NO_TRANSACTION
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
        "sign-in callback refused: no-transaction"
    ]


def test_a_secret_planted_in_a_log_line_would_be_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # the scan's other half: it does fail when something leaks
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("elsewhere").warning("leak %s", MARK)

    assert MARK in caplog.text


def test_a_refusal_raises_nothing(flow_kit: FlowKit) -> None:
    # Every way to refuse is an outcome the page answers; an exception here
    # would be a 500, which says that a callback can break the service.
    for name, scenario in sorted(SCENARIOS.items()):
        outcome = scenario(Scene(flow_kit))
        assert isinstance(outcome, SigninOutcome), name


# ── one line per reason per window ──────────────────────────────────────────
def lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def test_a_flood_of_one_refusal_is_one_line_per_window(
    flow_kit: FlowKit, signin_clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    started = flow_kit.begin()

    with caplog.at_level(logging.INFO, logger=LOGGER):
        for _ in range(5):
            flow_kit.flow.finish(flow_kit.callback(started), None, LATER)
        assert lines(caplog) == ["sign-in callback refused: no-transaction"]
        signin_clock.advance(61)
        flow_kit.flow.finish(flow_kit.callback(started), None, LATER)

    assert lines(caplog)[1:] == [
        "sign-in callback refused: no-transaction (and 4 more since the last line)"
    ]


def test_another_reason_has_its_own_window(
    flow_kit: FlowKit, caplog: pytest.LogCaptureFixture
) -> None:
    started = flow_kit.begin()

    with caplog.at_level(logging.INFO, logger=LOGGER):
        flow_kit.flow.finish(flow_kit.callback(started), None, LATER)
        flow_kit.flow.finish(
            flow_kit.callback(started, code=None), started.cookie, LATER
        )

    assert lines(caplog) == [
        "sign-in callback refused: no-transaction",
        "sign-in callback refused: no-code",
    ]


def test_the_reasons_are_a_closed_list_of_plain_words() -> None:
    values = [reason.value for reason in FlowReason]

    assert len(values) == len(set(values))
    assert all(re.fullmatch(r"[a-z]+(-[a-z]+)*", v) for v in values)


# ── signing out ─────────────────────────────────────────────────────────────
def test_sign_out_clears_the_session_cookie_and_goes_to_the_issuers_end_session(
    flow_kit: FlowKit,
) -> None:
    response = flow_kit.flow.sign_out()

    location = response.headers["location"]
    assert location.startswith(END_SESSION_URL + "?")
    assert dict(parse_qsl(urlsplit(location).query)) == {
        "client_id": CLIENT_ID,
        "post_logout_redirect_uri": POST_LOGOUT_URI,
    }
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    cleared = set_cookies(response)["meridian_session_staff"].lower()
    assert "max-age=0" in cleared
    assert "path=/;" in cleared + ";"
    assert "httponly" in cleared
    assert "samesite=lax" in cleared


def test_sign_out_sends_no_token_hint_because_no_token_is_kept(
    flow_kit: FlowKit,
) -> None:
    location = flow_kit.flow.sign_out().headers["location"]

    assert "id_token_hint" not in location
    assert "token" not in urlsplit(location).query


def test_sign_out_also_forgets_a_half_made_sign_in(flow_kit: FlowKit) -> None:
    response = flow_kit.flow.sign_out()

    assert "meridian_signin_staff" in set_cookies(response)
    assert "max-age=0" in set_cookies(response)["meridian_signin_staff"].lower()


def test_a_session_cookie_set_at_sign_in_is_the_one_sign_out_clears(
    flow_kit: FlowKit,
) -> None:
    started: Started = flow_kit.begin()
    flow_kit.arm(started)
    outcome = flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)
    signed_in = RedirectResponse(outcome.return_to, status_code=303)
    outcome.apply_to(signed_in)

    out = flow_kit.flow.sign_out()

    name = cookie_value(set_cookies(signed_in)["meridian_session_staff"])
    assert name
    assert "meridian_session_staff" in set_cookies(out)
