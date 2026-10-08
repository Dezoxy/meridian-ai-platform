"""The sign-in switch of the Claims API (S021, Y4): with it off the app is what it
was (no guard, no ``/auth/`` route, the pages' headers byte for byte); with it
``staff`` the factory builds the sign-in from the environment, and stops with
the variable's name when that environment is not enough."""

import base64
import logging
import secrets

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from staffsigninsupport import (
    CLAIM,
    REFUSING_DSN,
    StaffRig,
    assert_reached_the_handler,
    build_app,
    send,
    settings,
)

from meridian.platform.common.env import SettingsError
from meridian.platform.common.signinguard import route_reports
from meridian.workloads.claims_triage.app import create_app, create_app_from_env

TODAYS_POLICY = (
    "default-src 'none'; style-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
TODAYS_HEADERS = {
    "content-security-policy": TODAYS_POLICY,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "cache-control": "no-store",
}


def off_client() -> TestClient:
    app = create_app(settings(signin="off"))
    return TestClient(app, raise_server_exceptions=False)


APP_LOGGER = "meridian.workloads.claims_triage.app"
ON_LINE = (
    "the staff sign-in is on: the adjuster's pages and routes need the adjuster role"
)


def test_an_app_built_with_the_sign_in_says_so_once_in_a_fixed_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = StaffRig(rsa.generate_private_key(65537, 2048))

    with caplog.at_level(logging.DEBUG):
        build_app(rig)

    ours = [r for r in caplog.records if r.name == APP_LOGGER]
    assert [(r.levelno, r.getMessage()) for r in ours] == [(logging.INFO, ON_LINE)]
    # No address, no key, no setting's value: the line is the same for any rig.
    assert ours[0].msg == ON_LINE
    assert not ours[0].args


def test_an_app_built_with_the_switch_off_says_nothing_of_sign_in(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        create_app(settings(signin="off"))

    assert [r for r in caplog.records if r.name == APP_LOGGER] == []
    assert "sign-in" not in caplog.text.lower()


def test_with_the_switch_off_no_route_has_a_guard_and_none_is_under_auth() -> None:
    app = create_app(settings(signin="off"))

    reports = route_reports(app)

    assert reports
    assert not [r for r in reports if r.guarded or r.roles or r.populations]
    assert not [r for r in reports if r.path.startswith("/auth")]
    assert app.dependency_overrides == {}


def test_with_the_switch_off_the_pages_headers_are_todays() -> None:
    client = off_client()

    for path in (
        "/adjuster/static/adjuster.css",
        "/adjuster/nothing-here",
        "/claimant/claims",
    ):
        response = client.get(path)
        for name, value in TODAYS_HEADERS.items():
            if path.endswith(".css") and name == "cache-control":
                continue  # the stylesheet may be cached
            assert response.headers[name] == value, (path, name)


def test_with_the_switch_off_the_auth_paths_are_no_routes_and_get_no_page_headers() -> (
    None
):
    client = off_client()

    for method, path in [
        ("GET", "/auth/start"),
        ("GET", "/auth/callback"),
        ("GET", "/auth/failed"),
        ("POST", "/auth/sign-out"),
    ]:
        response = client.request(method, path)
        assert response.status_code == 404, path
        assert "content-security-policy" not in response.headers, path
        assert "referrer-policy" not in response.headers, path


def test_with_the_switch_off_the_routes_are_as_open_as_they_were() -> None:
    client = off_client()

    for method, path, body in [
        ("POST", f"/claims/{CLAIM}/decision", {"decision": "approve"}),
        ("POST", f"/claims/{CLAIM}/triage", {}),
        ("GET", f"/claims/{CLAIM}/brief", None),
        ("GET", f"/adjuster/claims/{CLAIM}/proposal", None),
        ("GET", "/adjuster/claims", None),
    ]:
        response = (
            send(client, method, path)
            if body is None
            else send(client, method, path, json=body)
        )
        # The route's own answer, at the database that refuses the connection.
        assert_reached_the_handler(response)


# ── the factory ─────────────────────────────────────────────────────────────
def signin_environment() -> dict[str, str]:
    issuer = "http://id.meridian.localhost:8088/realms/meridian-staff"
    back = "http://keycloak.identity.svc:8080/realms/meridian-staff/protocol"
    origin = "http://meridian.localhost:8088"
    return {
        "MERIDIAN_RUNTIME_URL": "http://runtime.invalid",
        "MERIDIAN_DATABASE_URL": REFUSING_DSN,
        "MERIDIAN_SIGNIN": "staff",
        "MERIDIAN_ENVIRONMENT": "kind",
        "MERIDIAN_SESSION_EDGE_PLAIN_HTTP": "true",
        "MERIDIAN_SESSION_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        "MERIDIAN_SIGNIN_STAFF_ISSUER": issuer,
        "MERIDIAN_SIGNIN_STAFF_AUDIENCE": "meridian-claims-api",
        "MERIDIAN_SIGNIN_STAFF_KEYS_URL": f"{back}/openid-connect/certs",
        "MERIDIAN_SIGNIN_STAFF_CLIENT_ID": "meridian-claims-web",
        "MERIDIAN_SIGNIN_STAFF_CLIENT_CREDENTIAL": secrets.token_urlsafe(24),
        "MERIDIAN_SIGNIN_STAFF_AUTHORIZATION_URL": f"{issuer}/protocol/auth",
        "MERIDIAN_SIGNIN_STAFF_TOKEN_URL": f"{back}/openid-connect/token",
        "MERIDIAN_SIGNIN_STAFF_END_SESSION_URL": f"{issuer}/protocol/logout",
        "MERIDIAN_SIGNIN_STAFF_APP_ORIGIN": origin,
        "MERIDIAN_SIGNIN_STAFF_REDIRECT_URI": f"{origin}/auth/callback",
        "MERIDIAN_SIGNIN_STAFF_POST_LOGOUT_REDIRECT_URI": f"{origin}/",
    }


def test_the_factory_builds_the_sign_in_from_the_environment_when_the_switch_is_staff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in signin_environment().items():
        monkeypatch.setenv(name, value)

    client = TestClient(create_app_from_env(), raise_server_exceptions=False)

    redirect = send(client, "GET", "/adjuster/claims")
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/auth/start?return_to=/adjuster/claims"
    assert send(client, "GET", "/auth/start").status_code == 303
    assert send(client, "GET", "/healthz").status_code == 200


def test_the_factory_builds_no_sign_in_when_the_switch_is_off_or_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = signin_environment()
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MERIDIAN_SIGNIN", "off")

    client = TestClient(create_app_from_env(), raise_server_exceptions=False)
    assert send(client, "GET", "/auth/start").status_code == 404

    monkeypatch.delenv("MERIDIAN_SIGNIN")
    client = TestClient(create_app_from_env(), raise_server_exceptions=False)
    assert send(client, "GET", "/auth/start").status_code == 404


def test_with_the_switch_off_the_sign_in_variables_are_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No sign-in variable at all, and a bad one: neither stops an app that is off.
    monkeypatch.setenv("MERIDIAN_RUNTIME_URL", "http://runtime.invalid")
    monkeypatch.setenv("MERIDIAN_DATABASE_URL", REFUSING_DSN)
    monkeypatch.setenv("MERIDIAN_SIGNIN_STAFF_ISSUER", "not a url")
    monkeypatch.delenv("MERIDIAN_SIGNIN", raising=False)

    assert create_app_from_env().title.startswith("Meridian")


@pytest.mark.parametrize(
    "missing",
    [
        "MERIDIAN_SESSION_KEY",
        "MERIDIAN_SIGNIN_STAFF_ISSUER",
        "MERIDIAN_SIGNIN_STAFF_AUDIENCE",
        "MERIDIAN_SIGNIN_STAFF_KEYS_URL",
        "MERIDIAN_SIGNIN_STAFF_CLIENT_ID",
        "MERIDIAN_SIGNIN_STAFF_CLIENT_CREDENTIAL",
        "MERIDIAN_SIGNIN_STAFF_REDIRECT_URI",
    ],
)
def test_the_factory_with_the_switch_on_names_a_missing_variable_and_no_value(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    environment = signin_environment()
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)

    with pytest.raises(SettingsError) as raised:
        create_app_from_env()

    assert missing in str(raised.value)
    for value in environment.values():
        if len(value) > 20:
            assert value not in str(raised.value)


def test_the_factory_refuses_a_redirect_address_that_is_not_the_callback_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in signin_environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(
        "MERIDIAN_SIGNIN_STAFF_REDIRECT_URI",
        "http://meridian.localhost:8088/auth/staff/callback",
    )

    with pytest.raises(SettingsError, match="/auth/callback"):
        create_app_from_env()


def test_the_factory_refuses_a_plain_http_edge_outside_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in signin_environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MERIDIAN_ENVIRONMENT", "production")

    with pytest.raises(SettingsError):
        create_app_from_env()


def test_a_wrong_switch_word_stops_the_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in signin_environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MERIDIAN_SIGNIN", "yes")

    with pytest.raises(SettingsError, match="MERIDIAN_SIGNIN"):
        create_app_from_env()
