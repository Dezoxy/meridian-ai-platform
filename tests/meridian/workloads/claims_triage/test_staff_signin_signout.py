"""The staff sign-in's failure page and sign-out, and the headers of the sign-in's
routes and of the pages that name its issuer (S021, Y4). Split from
``test_staff_signin_pages.py`` along its sections, which passed the size
ceiling."""

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from signinflowsupport import END_SESSION_URL
from staffsigninsupport import (
    SESSION_COOKIE,
    TRANSACTION_COOKIE,
    StaffRig,
    browser,
    build_app,
    send,
)
from workloads.claims_triage.test_staff_signin_pages import Links, cookies_of

SAME_ORIGIN = {"Origin": "http://meridian.localhost:8088"}
OTHER_SITE = {"Origin": "https://other-site.example"}
ISSUER_ORIGIN = "http://id.meridian.localhost:8088"
PAGES_POLICY = (
    "default-src 'none'; style-src 'self'; form-action 'self' {extra}; "
    "frame-ancestors 'none'; base-uri 'none'"
)


@pytest.fixture(scope="module")
def signer() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(65537, 2048)


@pytest.fixture
def rig(signer: rsa.RSAPrivateKey) -> StaffRig:
    return StaffRig(signer)


@pytest.fixture
def client(rig: StaffRig) -> TestClient:
    return browser(build_app(rig))


# ── GET /auth/failed ────────────────────────────────────────────────────────
def test_the_failure_page_is_one_sentence_and_one_link_to_start_again(
    client: TestClient,
) -> None:
    response = send(client, "GET", "/auth/failed")

    page = Links(response.text)
    assert response.status_code == 401
    assert "text/html" in response.headers["content-type"]
    assert page.hrefs.count("/auth/start") == 1
    assert "Sign in again" in page.text
    assert "Sign-in did not complete." in page.text
    assert "?" not in "".join(h for h in page.hrefs if h.startswith("/auth"))
    assert page.actions == []
    assert "return_to" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


# ── POST /auth/sign-out ─────────────────────────────────────────────────────
def test_sign_out_clears_the_cookies_and_goes_to_the_issuers_end_session(
    client: TestClient, rig: StaffRig
) -> None:
    response = send(
        client, "POST", "/auth/sign-out", cookie=rig.cookie(), headers=SAME_ORIGIN
    )

    cookies = cookies_of(response)
    assert response.status_code == 303
    assert response.headers["location"].startswith(END_SESSION_URL + "?")
    assert "max-age=0" in cookies[SESSION_COOKIE].lower()
    assert "max-age=0" in cookies[TRANSACTION_COOKIE].lower()


def test_sign_out_from_another_site_is_refused_and_clears_nothing(
    client: TestClient, rig: StaffRig
) -> None:
    for headers in (OTHER_SITE, {"Sec-Fetch-Site": "cross-site"}):
        response = send(
            client, "POST", "/auth/sign-out", cookie=rig.cookie(), headers=headers
        )

        assert response.status_code == 403
        assert "set-cookie" not in response.headers
        assert "location" not in response.headers


def test_sign_out_cannot_be_started_by_a_get(client: TestClient) -> None:
    assert send(client, "GET", "/auth/sign-out").status_code == 405


def test_the_403_pages_form_posts_to_the_sign_out_route(
    client: TestClient, rig: StaffRig
) -> None:
    page = send(client, "GET", "/adjuster/claims", cookie=rig.cookie(("none",)))
    [(method, action)] = Links(page.text).actions

    response = send(client, method.upper(), action, headers=SAME_ORIGIN)

    assert (response.status_code, method) == (303, "post")


# ── the headers ─────────────────────────────────────────────────────────────
def test_the_auth_routes_send_no_referrer_and_the_pages_do_not(
    client: TestClient, rig: StaffRig
) -> None:
    for method, path in [
        ("GET", "/auth/start"),
        ("GET", "/auth/callback"),
        ("GET", "/auth/failed"),
        ("POST", "/auth/sign-out"),
    ]:
        response = send(client, method, path, headers=SAME_ORIGIN)
        assert response.headers["referrer-policy"] == "no-referrer", path
        assert response.headers["cache-control"] == "no-store", path
        assert response.headers["x-frame-options"] == "DENY", path
    page = send(client, "GET", "/adjuster/claims", cookie=rig.cookie())
    assert page.headers["referrer-policy"] == "same-origin"


def test_the_policy_names_the_issuers_origin_in_form_action_on_the_staff_side(
    client: TestClient, rig: StaffRig
) -> None:
    expected = PAGES_POLICY.format(extra=ISSUER_ORIGIN)

    for path in ("/adjuster/claims", "/auth/failed", "/auth/start"):
        response = send(client, "GET", path, cookie=rig.cookie())
        assert response.headers["content-security-policy"] == expected, path
    stylesheet = send(client, "GET", "/adjuster/static/adjuster.css")
    assert stylesheet.headers["content-security-policy"] == expected


def test_the_claimants_pages_policy_is_not_widened(client: TestClient) -> None:
    response = send(client, "GET", "/claimant/claims")

    assert "form-action 'self';" in response.headers["content-security-policy"]
    assert ISSUER_ORIGIN not in response.headers["content-security-policy"]
