"""The staff sign-in's four routes and what a refusal looks like on a page
(S021, Y4): the redirect to sign in, the pages that answer 401, 403 and 503, the
callback with and without a permit, the failure page and the sign-out; and the
headers and settings that go with them."""

import base64
import logging
import secrets
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from dbsupport import DatabaseHandle
from fastapi import FastAPI
from fastapi.testclient import TestClient
from signinflowsupport import (
    AUTHORIZATION_URL,
    FRONT_ISSUER,
    flow_settings,
)
from staffsigninsupport import (
    CLAIM,
    REFUSING_DSN,
    SESSION_COOKIE,
    SUBJECT,
    TRANSACTION_COOKIE,
    StaffRig,
    assert_reached_the_handler,
    begin,
    browser,
    build_app,
    send,
)
from staffsigninsupport import settings as off_settings

from meridian.platform.common.env import SettingsError
from meridian.platform.common.signin import Reason, Unauthenticated
from meridian.workloads.claims_triage import adjuster
from meridian.workloads.claims_triage.app import create_app
from meridian.workloads.claims_triage.staff_signin import (
    CALLBACK_PERMITS,
    StaffSignin,
)

LOGGER = "meridian.workloads.claims_triage.staff_signin"
SAME_ORIGIN = {"Origin": "http://meridian.localhost:8088"}
OTHER_SITE = {"Origin": "https://other-site.example"}
ISSUER_ORIGIN = "http://id.meridian.localhost:8088"
PAGES_POLICY = (
    "default-src 'none'; style-src 'self'; form-action 'self' {extra}; "
    "frame-ancestors 'none'; base-uri 'none'"
)
FORM = {"decision": "approve", "run": ""}
FILE = "0b1f1c2e-6f0a-4f6e-8f1a-2f0c3b8a9d10"
NOT_SIGNED_IN = (
    "You are not signed in, or your session has ended, so nothing was recorded. "
    "Open the queue to sign in again."
)
OLD_BANNER = "No sign-in yet: anyone who reaches this page can decide (T-69)."
NEW_BANNER = "Synthetic data only."


@pytest.fixture(scope="module")
def signer() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(65537, 2048)


@pytest.fixture
def rig(signer: rsa.RSAPrivateKey) -> StaffRig:
    return StaffRig(signer)


@pytest.fixture
def staff(rig: StaffRig) -> StaffSignin:
    return rig.staff()


@pytest.fixture
def app(rig: StaffRig, staff: StaffSignin) -> FastAPI:
    return build_app(rig, staff=staff)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return browser(app)


def cookies_of(response: httpx.Response) -> dict[str, str]:
    """The ``Set-Cookie`` lines of a response by cookie name."""
    lines = response.headers.get_list("set-cookie")
    return {line.split("=", 1)[0]: line for line in lines}


class Links(HTMLParser):
    """The hrefs, the form actions and the text of a page."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.actions: list[tuple[str, str]] = []
        self.text = ""
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if tag == "a" and found.get("href"):
            self.hrefs.append(found["href"] or "")
        if tag == "form":
            self.actions.append(
                ((found.get("method") or ""), found.get("action") or "")
            )

    def handle_data(self, data: str) -> None:
        self.text += data


# ── a refusal of a page ─────────────────────────────────────────────────────
def test_a_get_without_a_session_goes_to_sign_in_and_back_to_the_same_path(
    client: TestClient,
) -> None:
    response = send(client, "GET", f"/adjuster/claims/{CLAIM}")

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == f"/auth/start?return_to=/adjuster/claims/{CLAIM}"
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.text == ""


def test_the_query_of_the_refused_page_is_not_carried_to_sign_in(
    client: TestClient,
) -> None:
    response = send(
        client,
        "GET",
        "/adjuster/claims?after_time=2026-01-01T00:00:00Z&after_claim=CLM-0001",
    )

    assert response.headers["location"] == "/auth/start?return_to=/adjuster/claims"


def test_a_head_without_a_session_goes_to_sign_in_too(client: TestClient) -> None:
    # The download is the one page route that answers HEAD.
    path = f"/adjuster/claims/{CLAIM}/files/{FILE}"

    response = send(client, "HEAD", path, headers=SAME_ORIGIN)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/start?return_to=")


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_a_download_without_a_session_returns_to_its_claims_page_never_the_file(
    client: TestClient, method: str
) -> None:
    path = f"/adjuster/claims/{CLAIM}/files/{FILE}"

    response = send(client, method, path, headers=SAME_ORIGIN)

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/auth/start?return_to=/adjuster/claims/{CLAIM}"
    )
    assert FILE not in response.headers["location"]


@pytest.mark.parametrize("claim", ["NOT-A-CLAIM", "CLM-12", "CLM-123456", "clm-1234"])
def test_a_download_of_something_that_is_no_claim_returns_to_the_queue(
    client: TestClient, claim: str
) -> None:
    path = f"/adjuster/claims/{claim}/files/{FILE}"

    response = send(client, "GET", path, headers=SAME_ORIGIN)

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/start?return_to=/adjuster/claims"


def test_the_sign_in_that_follows_a_refused_download_lands_on_the_claims_page(
    client: TestClient, rig: StaffRig
) -> None:
    refused = send(
        client, "GET", f"/adjuster/claims/{CLAIM}/files/{FILE}", headers=SAME_ORIGIN
    )
    started = begin(client, refused.headers["location"].split("return_to=")[1])
    rig.arm(started)

    callback = send(
        client,
        "GET",
        rig.callback_path(started),
        cookie=f"{TRANSACTION_COOKIE}={started.cookie}",
    )

    assert callback.headers["location"] == f"/adjuster/claims/{CLAIM}"


def test_a_post_without_a_session_is_the_401_page_with_a_way_to_the_queue(
    client: TestClient,
) -> None:
    response = send(
        client,
        "POST",
        f"/adjuster/claims/{CLAIM}/decision",
        data=FORM,
        headers=SAME_ORIGIN,
    )

    page = Links(response.text)
    assert response.status_code == 401
    assert "text/html" in response.headers["content-type"]
    assert "www-authenticate" not in response.headers
    assert "/adjuster/claims" in page.hrefs
    assert "return_to" not in response.text
    assert " ".join(page.text.split()).count(NOT_SIGNED_IN) == 1
    assert page.actions == []  # nothing to sign out of


def test_a_person_without_the_role_gets_the_403_page_with_a_sign_out_form(
    client: TestClient, rig: StaffRig
) -> None:
    response = send(
        client, "GET", "/adjuster/claims", cookie=rig.cookie(("auditor", "reviewer"))
    )

    page = Links(response.text)
    assert response.status_code == 403
    assert page.actions == [("post", "/auth/sign-out")]
    assert "does not have the adjuster role" in page.text
    # Nothing of the person: not a subject, not the roles they do have, not why.
    for word in (SUBJECT, "auditor", "reviewer", "no-role", "Reason"):
        assert word not in response.text


def test_a_failure_to_reach_the_issuers_keys_is_the_503_page(
    client: TestClient, rig: StaffRig
) -> None:
    rig.keys_down = True

    response = send(client, "GET", "/adjuster/claims", bearer=rig.bearer())

    assert response.status_code == 503
    assert "text/html" in response.headers["content-type"]
    assert "not available right now" in response.text
    assert "return_to" not in response.text


def test_the_refusal_pages_carry_the_pages_security_headers(
    client: TestClient, rig: StaffRig
) -> None:
    forbidden = send(client, "GET", "/adjuster/claims", cookie=rig.cookie(("none",)))
    redirect = send(client, "GET", "/adjuster/claims")

    for response in (forbidden, redirect):
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "same-origin"


def test_the_proposal_keeps_its_json_refusal_and_is_never_redirected(
    client: TestClient, rig: StaffRig
) -> None:
    path = f"/adjuster/claims/{CLAIM}/proposal"

    anonymous = send(client, "GET", path)
    wrong_role = send(client, "GET", path, bearer=rig.bearer(("auditor",)))

    assert anonymous.status_code == 401
    assert anonymous.json() == {"detail": "request refused"}
    assert anonymous.headers["www-authenticate"] == "Bearer"
    assert wrong_role.status_code == 403
    assert wrong_role.json() == {"detail": "request refused"}


def test_a_404_under_the_adjusters_pages_is_still_the_frameworks(
    client: TestClient, rig: StaffRig
) -> None:
    response = send(client, "GET", "/adjuster/nothing-here", cookie=rig.cookie())

    assert response.status_code == 404


def test_a_refusal_of_a_path_that_is_not_the_adjusters_is_handed_on(
    client: TestClient,
) -> None:
    response = send(client, "POST", f"/claims/{CLAIM}/decision", json=FORM)

    assert response.status_code == 401
    assert response.json() == {"detail": "request refused"}


# ── GET /auth/start ─────────────────────────────────────────────────────────
def test_start_sends_the_person_to_the_issuer_with_a_transaction_cookie(
    client: TestClient,
) -> None:
    response = send(client, "GET", "/auth/start")

    query = dict(parse_qsl(urlsplit(response.headers["location"]).query))
    cookie = cookies_of(response)[TRANSACTION_COOKIE].lower()
    assert response.status_code == 303
    assert response.headers["location"].startswith(AUTHORIZATION_URL + "?")
    assert query["redirect_uri"] == "http://meridian.localhost:8088/auth/callback"
    assert query["code_challenge_method"] == "S256"
    assert "path=/auth/callback" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert response.headers["cache-control"] == "no-store"


def test_start_keeps_a_return_path_of_the_adjusters_pages_and_no_other(
    client: TestClient, rig: StaffRig
) -> None:
    for wanted, expected in [
        (f"/adjuster/claims/{CLAIM}", f"/adjuster/claims/{CLAIM}"),
        ("/claimant/claims", "/adjuster/claims"),
        ("https://other-site.example/adjuster/claims", "/adjuster/claims"),
        ("//other-site.example/adjuster/claims", "/adjuster/claims"),
        ("/adjuster/../claims", "/adjuster/claims"),
    ]:
        started = begin(client, wanted)
        rig.arm(started)
        callback = send(
            client,
            "GET",
            rig.callback_path(started),
            cookie=f"{TRANSACTION_COOKIE}={started.cookie}",
        )
        assert callback.headers["location"] == expected, wanted


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        (CLAIM, f"/adjuster/claims/{CLAIM}"),
        ("NOT-A-CLAIM", "/adjuster/claims"),
        ("CLM-12", "/adjuster/claims"),
    ],
)
def test_start_turns_a_download_path_into_its_claims_page_and_keeps_the_file_out(
    client: TestClient, rig: StaffRig, claim: str, expected: str
) -> None:
    # Asked for directly, as the refusal handler's redirect asks for it.
    started = begin(client, f"/adjuster/claims/{claim}/files/{FILE}")
    payload = started.cookie.split(".")[1]
    held = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    rig.arm(started)

    callback = send(
        client,
        "GET",
        rig.callback_path(started),
        cookie=f"{TRANSACTION_COOKIE}={started.cookie}",
    )

    assert callback.headers["location"] == expected
    # The transaction cookie is signed, not encrypted: the file's id is not in it.
    assert FILE not in held
    assert expected in held


# ── GET /auth/callback ──────────────────────────────────────────────────────
def test_a_good_callback_is_a_303_to_the_page_asked_for_with_a_session(
    client: TestClient, rig: StaffRig
) -> None:
    started = begin(client, f"/adjuster/claims/{CLAIM}")
    rig.arm(started)

    response = send(
        client,
        "GET",
        rig.callback_path(started),
        cookie=f"{TRANSACTION_COOKIE}={started.cookie}",
    )

    cookies = cookies_of(response)
    assert response.status_code == 303
    assert response.headers["location"] == f"/adjuster/claims/{CLAIM}"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert response.text == ""
    assert "httponly" in cookies[SESSION_COOKIE].lower()
    assert "max-age=0" in cookies[TRANSACTION_COOKIE].lower()
    assert "path=/auth/callback" in cookies[TRANSACTION_COOKIE].lower()
    # ... and the cookie it set is a session the pages accept.
    session = cookies[SESSION_COOKIE].split(";", 1)[0]
    page = send(client, "GET", "/adjuster/claims", cookie=session)
    assert_reached_the_handler(page)


def test_a_refused_callback_is_a_303_to_the_failure_page_and_clears_the_transaction(
    client: TestClient, rig: StaffRig
) -> None:
    started = begin(client)
    rig.arm(started)

    response = send(
        client,
        "GET",
        rig.callback_path(started, state="not-the-state"),
        cookie=f"{TRANSACTION_COOKIE}={started.cookie}",
    )

    cookies = cookies_of(response)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/failed"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert SESSION_COOKIE not in cookies
    assert "max-age=0" in cookies[TRANSACTION_COOKIE].lower()
    assert rig.endpoint.calls == 0


def test_a_callback_the_issuer_refuses_the_code_for_is_the_same_303(
    client: TestClient, rig: StaffRig
) -> None:
    started = begin(client)
    rig.arm(started)
    rig.endpoint.redeemed = True  # the code was used

    response = send(
        client,
        "GET",
        rig.callback_path(started),
        cookie=f"{TRANSACTION_COOKIE}={started.cookie}",
    )

    assert (response.status_code, response.headers["location"]) == (303, "/auth/failed")
    assert rig.endpoint.calls == 1
    assert SESSION_COOKIE not in cookies_of(response)


def test_a_callback_with_no_transaction_cookie_is_the_same_303(
    client: TestClient, rig: StaffRig
) -> None:
    started = begin(client)
    rig.arm(started)

    response = send(client, "GET", rig.callback_path(started))

    assert (response.status_code, response.headers["location"]) == (303, "/auth/failed")
    assert rig.endpoint.calls == 0


def test_a_callback_answers_303_and_nothing_else_whatever_it_is_sent(
    client: TestClient,
) -> None:
    for target in [
        "/auth/callback",
        "/auth/callback?error=access_denied&error_description=why",
        "/auth/callback?state=a&state=b&code=c",
        "/auth/callback?code=" + "x" * 5000,
    ]:
        response = send(client, "GET", target, cookie=f"{TRANSACTION_COOKIE}=junk")

        assert response.status_code == 303, target
        assert response.headers["location"] == "/auth/failed"
        assert response.text == ""
        assert "why" not in str(response.headers)


def test_with_no_permit_free_a_callback_is_a_303_to_the_failure_page_at_once(
    client: TestClient,
    rig: StaffRig,
    staff: StaffSignin,
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = begin(client)
    rig.arm(started)
    for _ in range(CALLBACK_PERMITS):
        assert staff.permits.acquire(blocking=False)
    assert not staff.permits.acquire(blocking=False)  # the pool is empty

    with caplog.at_level(logging.INFO, logger=LOGGER):
        responses = [
            send(
                client,
                "GET",
                rig.callback_path(started),
                cookie=f"{TRANSACTION_COOKIE}={started.cookie}",
            )
            for _ in range(3)
        ]

    for response in responses:
        assert (response.status_code, response.headers["location"]) == (
            303,
            "/auth/failed",
        )
        assert SESSION_COOKIE not in cookies_of(response)
    # finish never ran: no request to the issuer, and the transaction is left.
    assert rig.endpoint.calls == 0
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.WARNING]
    assert "no permit free" in caplog.text
    # the permits come back: after they are released a callback runs again
    for _ in range(CALLBACK_PERMITS):
        staff.permits.release()
    again = send(
        client,
        "GET",
        rig.callback_path(started),
        cookie=f"{TRANSACTION_COOKIE}={started.cookie}",
    )
    assert again.headers["location"] == "/adjuster/claims"
    assert rig.endpoint.calls == 1


def test_a_permit_is_given_back_after_every_callback_good_or_bad(
    client: TestClient, rig: StaffRig, staff: StaffSignin
) -> None:
    for _ in range(CALLBACK_PERMITS + 2):
        send(client, "GET", "/auth/callback?code=a&state=b")

    assert_all_permits_free(staff)


def assert_all_permits_free(staff: StaffSignin) -> None:
    """Every permit can be taken at once, and the pool is then empty: none was
    kept and none given back twice. They are put back."""
    taken = [staff.permits.acquire(blocking=False) for _ in range(CALLBACK_PERMITS)]
    try:
        assert all(taken)
        assert not staff.permits.acquire(blocking=False)
    finally:
        for got in taken:
            if got:
                staff.permits.release()


def test_a_permit_is_given_back_after_the_exchange_was_reached(
    client: TestClient, rig: StaffRig, staff: StaffSignin
) -> None:
    good = begin(client)
    rig.arm(good)
    signed_in = send(
        client,
        "GET",
        rig.callback_path(good),
        cookie=f"{TRANSACTION_COOKIE}={good.cookie}",
    )
    refused = begin(client)
    rig.arm(refused)
    rig.endpoint.redeemed = True  # the issuer refuses this code
    turned_away = send(
        client,
        "GET",
        rig.callback_path(refused),
        cookie=f"{TRANSACTION_COOKIE}={refused.cookie}",
    )

    assert rig.endpoint.calls == 2  # both reached the issuer
    assert signed_in.headers["location"] == "/adjuster/claims"
    assert turned_away.headers["location"] == "/auth/failed"
    assert_all_permits_free(staff)


def test_when_finish_raises_the_permit_is_back_and_the_answer_is_the_500(
    client: TestClient,
    rig: StaffRig,
    staff: StaffSignin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mark = "finish-text-mark-5c2e"

    def raising(*args: object, **kwargs: object) -> None:
        raise RuntimeError(mark)

    monkeypatch.setattr(staff.flow, "finish", raising)
    started = begin(client)
    rig.arm(started)

    response = send(
        client,
        "GET",
        rig.callback_path(started),
        cookie=f"{TRANSACTION_COOKIE}={started.cookie}",
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert mark not in response.text
    assert "set-cookie" not in response.headers
    assert_all_permits_free(staff)


@pytest.mark.parametrize("error", [Unauthenticated(Reason.EXPIRED), ValueError("x")])
def test_a_session_that_cannot_be_set_is_the_failure_page_with_the_transaction_cleared(
    client: TestClient,
    rig: StaffRig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(
        "meridian.platform.common.signinflow.set_session_cookie", refuse
    )
    started = begin(client)
    rig.arm(started)

    with caplog.at_level(logging.DEBUG):
        response = send(
            client,
            "GET",
            rig.callback_path(started),
            cookie=f"{TRANSACTION_COOKIE}={started.cookie}",
        )

    cookies = cookies_of(response)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/failed"
    assert SESSION_COOKIE not in cookies
    assert "max-age=0" in cookies[TRANSACTION_COOKIE].lower()
    assert "path=/auth/callback" in cookies[TRANSACTION_COOKIE].lower()
    assert type(error).__name__ in caplog.text
    assert response.headers["cache-control"] == "no-store"


# ── the pages show a sign-out form only when the sign-in is on ──────────────
def claim_view() -> adjuster.ClaimView:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    return adjuster.ClaimView(
        claim_id=CLAIM,
        state="awaiting_adjuster",
        since=moment,
        received_at=moment,
        facts={},
        proposal=None,
        proposal_note=None,
        decision=None,
        trail=(),
    )


def test_the_queue_and_claim_pages_have_a_sign_out_form_only_when_it_is_on() -> None:
    on = [
        adjuster.render_queue([], signin=True),
        adjuster.render_claim(claim_view(), signin=True),
    ]
    off = [adjuster.render_queue([]), adjuster.render_claim(claim_view())]

    for page in on:
        assert Links(page).actions[0] == ("post", "/auth/sign-out")
        assert "No sign-in yet" not in page
    for page in off:
        assert "/auth/" not in page
        assert "sign-out" not in page.lower()
        assert (
            '<p class="banner">Synthetic data only. No sign-in yet: anyone who '
            "reaches this page can decide (T-69).</p>"
        ) in page


def banner_pages(signin: bool, rig: StaffRig, dsn: str) -> list[httpx.Response]:
    """A 404 claim page, a database-failure page and a cross-site 403 page, of
    the app with the switch on or off."""
    if signin:
        client = browser(build_app(rig, database_url=dsn))
        cookie: str | None = rig.cookie()
    else:
        client = browser(create_app(off_settings(signin="off", database_url=dsn)))
        cookie = None
    broken = browser(
        build_app(rig, database_url=REFUSING_DSN)
        if signin
        else create_app(off_settings(signin="off"))
    )
    return [
        send(client, "GET", f"/adjuster/claims/{CLAIM}", cookie=cookie),
        send(broken, "GET", "/adjuster/claims", cookie=cookie),
        send(
            client,
            "POST",
            f"/adjuster/claims/{CLAIM}/decision",
            cookie=cookie,
            data=FORM,
            headers=OTHER_SITE,
        ),
    ]


@pytest.mark.parametrize("signin", [True, False])
def test_the_error_pages_say_there_is_a_sign_in_only_when_there_is_one(
    signin: bool, rig: StaffRig, fresh_database: DatabaseHandle
) -> None:
    missing, database, cross_site = banner_pages(
        signin, rig, fresh_database.dsn("claims_api")
    )

    assert (missing.status_code, database.status_code, cross_site.status_code) == (
        404,
        503,
        403,
    )
    for page in (missing, database, cross_site):
        assert (NEW_BANNER in page.text and OLD_BANNER not in page.text) == signin
        assert (OLD_BANNER in page.text) == (not signin)
        assert (Links(page.text).actions == [("post", "/auth/sign-out")]) == signin


def test_a_claimants_error_page_never_gets_the_adjusters_banner_or_form(
    rig: StaffRig,
) -> None:
    client = browser(build_app(rig))

    page = send(client, "GET", "/claimant/claims/CLM-12")  # not a claim ID

    assert page.status_code == 422
    assert OLD_BANNER not in page.text
    assert "/auth/" not in page.text
    assert "sign-out" not in page.text.lower()


def test_the_queue_page_of_the_running_app_has_the_form_for_a_signed_in_person(
    rig: StaffRig, fresh_database: DatabaseHandle
) -> None:
    app = build_app(rig, database_url=fresh_database.dsn("claims_api"))

    page = send(browser(app), "GET", "/adjuster/claims", cookie=rig.cookie())

    assert page.status_code == 200
    assert Links(page.text).actions == [("post", "/auth/sign-out")]


# ── the build refuses what would not work ───────────────────────────────────
def test_a_redirect_address_whose_path_is_not_the_callbacks_stops_the_build(
    rig: StaffRig,
) -> None:
    credential = secrets.token_urlsafe(24)
    wrong = flow_settings(
        client_credential=credential,
        return_prefixes=("/adjuster",),
        default_return_path="/adjuster/claims",
    )  # redirect_uri: /auth/staff/callback

    with pytest.raises(SettingsError) as raised:
        StaffSignin(rig.signin_settings, wrong, rig.sessions)

    assert "/auth/callback" in str(raised.value)
    assert "staff/callback" not in str(raised.value)
    assert credential not in str(raised.value)


def test_settings_for_another_issuer_or_population_stop_the_build(
    rig: StaffRig,
) -> None:
    other_issuer = rig.signin_settings.model_copy(
        update={"issuer": FRONT_ISSUER + "-other"}
    )
    claimants = rig.signin_settings.model_copy(update={"population": "claimant"})

    for settings in (other_issuer, claimants):
        with pytest.raises(SettingsError):
            StaffSignin(settings, rig.flow_settings, rig.sessions)
