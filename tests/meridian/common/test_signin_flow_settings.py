"""The pages' sign-in flow, its settings and the small rules under it (S021, Y3):
what the settings refuse, the discovery document of the realm as it runs on
kind, the return path a person is sent back to, and PKCE.

The discovery document in ``fixtures/`` is the realm's own, fetched through the
edge on 2026-10-08. The module never fetches it: the endpoints are settings, and
the document is what a test (and an operator) holds them against.
"""

import json
import secrets
from pathlib import Path
from typing import Any

import pytest
from signinflowsupport import (
    APP_ORIGIN,
    AUTHORIZATION_URL,
    CLIENT_ID,
    END_SESSION_URL,
    FRONT_ISSUER,
    HTTPS_ADDRESSES,
    POST_LOGOUT_URI,
    REDIRECT_URI,
    RETURN_PREFIXES,
    TOKEN_URL,
    flow_settings,
    https_settings,
    s256,
)

from meridian.platform.common.env import SettingsError
from meridian.platform.common.signinflow import (
    FlowSettings,
    SigninFlow,
    discovery_problem,
)
from meridian.platform.common.signinkeys import KeySet
from meridian.platform.common.signinsession import SessionKeys, SessionSettings
from meridian.platform.common.signinstate import (
    MAX_RETURN_PATH_CHARS,
    PKCE_METHOD,
    pkce_challenge,
    safe_return_path,
)

DISCOVERY = Path(__file__).parent / "fixtures" / "discovery-kind-2026-10-08.json"
MARKER = "FLOWMARK-0d41"
CREDENTIAL_MARKER = "FLOWCRED-7b2e-" + secrets.token_hex(4)


def discovery() -> dict[str, Any]:
    return json.loads(DISCOVERY.read_text(encoding="utf-8"))


# ── the realm as it runs on kind ────────────────────────────────────────────
def test_the_realms_discovery_has_the_issuer_and_the_endpoints_on_the_front() -> None:
    document = discovery()

    assert document["issuer"] == FRONT_ISSUER
    for name in (
        "authorization_endpoint",
        "token_endpoint",
        "end_session_endpoint",
        "jwks_uri",
    ):
        assert document[name].startswith("http://id.meridian.localhost:8088/"), name
    assert "S256" in document["code_challenge_methods_supported"]
    assert document["authorization_response_iss_parameter_supported"] is True


def test_the_settings_for_kind_match_the_discovery_document() -> None:
    assert discovery_problem(discovery(), flow_settings()) is None


def test_the_discovery_token_endpoint_is_not_the_back_channel_setting() -> None:
    # A pod cannot call the front address, so the token address is a setting of
    # its own and is not taken from the document.
    assert discovery()["token_endpoint"] != TOKEN_URL
    assert flow_settings().token_url == TOKEN_URL


@pytest.mark.parametrize(
    ("change", "setting"),
    [
        pytest.param({"code_challenge_methods_supported": ["plain"]}, {}, id="no-s256"),
        pytest.param({"code_challenge_methods_supported": []}, {}, id="no-methods"),
        pytest.param({"code_challenge_methods_supported": "S256"}, {}, id="not-a-list"),
        pytest.param({"issuer": FRONT_ISSUER + "/"}, {}, id="issuer"),
        pytest.param({"authorization_endpoint": "http://x.test/a"}, {}, id="auth"),
        pytest.param({"end_session_endpoint": "http://x.test/o"}, {}, id="end"),
    ],
)
def test_a_discovery_document_that_the_settings_do_not_match_is_a_problem(
    change: dict[str, Any], setting: dict[str, Any]
) -> None:
    document = {**discovery(), **change}

    problem = discovery_problem(document, flow_settings(**setting))

    assert problem is not None
    assert FRONT_ISSUER not in problem


def test_a_discovery_document_without_the_method_key_is_a_problem() -> None:
    document = discovery()
    del document["code_challenge_methods_supported"]

    assert discovery_problem(document, flow_settings()) is not None


# ── PKCE ────────────────────────────────────────────────────────────────────
def test_the_s256_challenge_is_the_one_of_rfc_7636_appendix_b() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert pkce_challenge(verifier) == s256(verifier)
    assert PKCE_METHOD == "S256"


@pytest.mark.parametrize("method", ["plain", "PLAIN", "s256", "", "S512"])
def test_a_challenge_method_other_than_s256_is_refused(method: str) -> None:
    with pytest.raises(ValueError, match="S256") as refused:
        pkce_challenge("v" * 43, method)

    assert "v" * 43 not in str(refused.value)


# ── the return path ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "path",
    [
        "/adjuster",
        "/adjuster/",
        "/adjuster/claims/c-123",
        "/brief/claims/9f2e/brief",
        "/adjuster/a-b_c.d~e",
    ],
)
def test_a_path_under_an_allowed_prefix_is_kept(path: str) -> None:
    assert safe_return_path(path, RETURN_PREFIXES, "/") == path


@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param("https://evil.example/adjuster", id="absolute-url"),
        pytest.param("http://evil.example", id="absolute-http"),
        pytest.param("//evil.example/adjuster", id="protocol-relative"),
        pytest.param("/\\evil.example", id="backslash-after-slash"),
        pytest.param("\\\\evil.example", id="backslashes"),
        pytest.param("/adjuster\\..\\evil", id="backslash-inside"),
        pytest.param("/%2F%2Fevil.example", id="encoded-slashes"),
        pytest.param("%2F%2Fevil.example", id="encoded-protocol-relative"),
        pytest.param("/%5Cevil.example", id="encoded-backslash"),
        pytest.param("/adjuster/%2e%2e/admin", id="encoded-dots"),
        pytest.param("/adjuster%2F..%2Fadmin", id="encoded-slash-inside"),
        pytest.param("/adjuster/../admin", id="dot-dot"),
        pytest.param("/adjuster/./x", id="dot"),
        pytest.param("/adjuster//x", id="empty-segment"),
        pytest.param("/adjuster/x?next=//evil.example", id="query"),
        pytest.param("/adjuster#frag", id="fragment"),
        pytest.param("/adjuster\r\nLocation: //evil", id="line-break"),
        pytest.param("/adjuster/ x", id="space"),
        pytest.param("/adjuster\t", id="tab"),
        pytest.param("/adjuster/é", id="non-ascii"),
        pytest.param("javascript:alert(1)", id="script"),
        pytest.param("adjuster/x", id="no-leading-slash"),
        pytest.param("/adjusterx", id="prefix-is-not-a-segment"),
        pytest.param("/admin", id="not-allowed"),
        pytest.param("/", id="root-is-not-a-prefix"),
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
        pytest.param(42, id="number"),
        pytest.param(["/adjuster"], id="list"),
        pytest.param(b"/adjuster", id="bytes"),
        pytest.param("/adjuster/" + "a" * MAX_RETURN_PATH_CHARS, id="too-long"),
    ],
)
def test_anything_else_becomes_the_default_page(candidate: object) -> None:
    assert safe_return_path(candidate, RETURN_PREFIXES, "/home") == "/home"


def test_the_longest_path_is_kept_and_one_character_more_is_not() -> None:
    fits = "/adjuster/" + "a" * (MAX_RETURN_PATH_CHARS - len("/adjuster/"))

    assert safe_return_path(fits, RETURN_PREFIXES, "/") == fits
    assert safe_return_path(fits + "a", RETURN_PREFIXES, "/") == "/"


def test_the_root_prefix_allows_any_clean_path_but_still_no_host() -> None:
    assert safe_return_path("/anything/here", ("/",), "/d") == "/anything/here"
    assert safe_return_path("//evil.example", ("/",), "/d") == "/d"


# ── the settings ────────────────────────────────────────────────────────────
def env(**changes: str | None) -> dict[str, str]:
    base = {
        "MERIDIAN_ENVIRONMENT": "kind",
        "MERIDIAN_SIGNIN_STAFF_ISSUER": FRONT_ISSUER,
        "MERIDIAN_SIGNIN_STAFF_CLIENT_ID": CLIENT_ID,
        "MERIDIAN_SIGNIN_STAFF_CLIENT_CREDENTIAL": CREDENTIAL_MARKER,
        "MERIDIAN_SIGNIN_STAFF_AUTHORIZATION_URL": AUTHORIZATION_URL,
        "MERIDIAN_SIGNIN_STAFF_TOKEN_URL": TOKEN_URL,
        "MERIDIAN_SIGNIN_STAFF_END_SESSION_URL": END_SESSION_URL,
        "MERIDIAN_SIGNIN_STAFF_APP_ORIGIN": APP_ORIGIN,
        "MERIDIAN_SIGNIN_STAFF_REDIRECT_URI": REDIRECT_URI,
        "MERIDIAN_SIGNIN_STAFF_POST_LOGOUT_REDIRECT_URI": POST_LOGOUT_URI,
    }
    merged: dict[str, str] = dict(base)
    for name, value in changes.items():
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value
    return merged


def from_env(environ: dict[str, str]) -> FlowSettings:
    return FlowSettings.from_env(
        environ, "staff", return_prefixes=RETURN_PREFIXES, default_return_path="/"
    )


def test_the_settings_are_built_from_the_environment() -> None:
    settings = from_env(env())

    assert settings.population == "staff"
    assert settings.issuer == FRONT_ISSUER
    assert settings.client_id == CLIENT_ID
    assert settings.authorization_url == AUTHORIZATION_URL
    assert settings.token_url == TOKEN_URL
    assert settings.redirect_uri == REDIRECT_URI
    assert settings.scopes == ("openid",)
    assert settings.required_typ == "ID"
    assert settings.roles_claim == "roles"
    assert settings.subject_claim == "sub"
    assert settings.return_prefixes == RETURN_PREFIXES
    assert settings.client_credential.get_secret_value() == CREDENTIAL_MARKER


def test_the_scopes_are_a_comma_separated_list() -> None:
    environ = env(MERIDIAN_SIGNIN_STAFF_SCOPES="openid, roles")

    assert from_env(environ).scopes == ("openid", "roles")


@pytest.mark.parametrize(
    "variable",
    [
        "MERIDIAN_SIGNIN_STAFF_ISSUER",
        "MERIDIAN_SIGNIN_STAFF_CLIENT_ID",
        "MERIDIAN_SIGNIN_STAFF_CLIENT_CREDENTIAL",
        "MERIDIAN_SIGNIN_STAFF_AUTHORIZATION_URL",
        "MERIDIAN_SIGNIN_STAFF_TOKEN_URL",
        "MERIDIAN_SIGNIN_STAFF_END_SESSION_URL",
        "MERIDIAN_SIGNIN_STAFF_APP_ORIGIN",
        "MERIDIAN_SIGNIN_STAFF_REDIRECT_URI",
        "MERIDIAN_SIGNIN_STAFF_POST_LOGOUT_REDIRECT_URI",
    ],
)
@pytest.mark.parametrize("how", ["missing", "empty"])
def test_a_missing_required_variable_stops_the_start_and_is_named(
    variable: str, how: str
) -> None:
    environ = env(**{variable: None if how == "missing" else ""})

    with pytest.raises(SettingsError, match=variable):
        from_env(environ)


@pytest.mark.parametrize(
    "field",
    [
        "issuer",
        "authorization_url",
        "token_url",
        "end_session_url",
        "app_origin",
        "redirect_uri",
        "post_logout_redirect_uri",
    ],
)
@pytest.mark.parametrize("environment", ["", "azure", "Kind", "kind "])
def test_a_plain_http_address_is_refused_outside_kind(
    field: str, environment: str
) -> None:
    plain = {field: HTTPS_ADDRESSES[field].replace("https://", "http://")}

    # the other six are https, so only this field can be the cause
    with pytest.raises(Exception, match=field) as refused:
        flow_settings(environment=environment, **{**HTTPS_ADDRESSES, **plain})

    assert "example.test" not in str(refused.value)


def test_https_addresses_are_accepted_outside_kind() -> None:
    settings = https_settings()

    assert settings.environment == "azure"


@pytest.mark.parametrize(
    "redirect",
    [
        pytest.param("http://meridian.localhost:9999/cb", id="other-port"),
        pytest.param("http://other.localhost:8088/cb", id="other-host"),
        pytest.param("http://meridian.localhost:8088.evil.test/cb", id="suffix"),
        pytest.param("https://meridian.localhost:8088/cb", id="other-scheme"),
        pytest.param("http://meridian.localhost/cb", id="no-port"),
        pytest.param("http://id.meridian.localhost:8088/cb", id="issuer-host"),
    ],
)
@pytest.mark.parametrize("field", ["redirect_uri", "post_logout_redirect_uri"])
def test_a_return_address_that_is_not_under_the_apps_origin_is_refused(
    field: str, redirect: str
) -> None:
    with pytest.raises(Exception, match=field):
        flow_settings(**{field: redirect})


def test_the_origin_compare_ignores_case_and_the_default_port() -> None:
    settings = https_settings(
        app_origin="https://App.Example.test",
        redirect_uri="https://app.example.test:443/cb",
        post_logout_redirect_uri="https://APP.example.test/",
    )

    assert settings.redirect_uri.endswith("/cb")


@pytest.mark.parametrize(
    "scopes",
    [(), ("profile",), ("email", "roles"), ("OPENID",), ("openid profile",)],
)
def test_the_scopes_must_include_openid_and_each_be_one_word(
    scopes: tuple[str, ...],
) -> None:
    with pytest.raises(Exception, match="scopes"):
        flow_settings(scopes=scopes)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authorization_url", AUTHORIZATION_URL + "?prompt=login"),
        ("token_url", "http://user:pw@keycloak.identity.svc:8080/token"),
        ("end_session_url", END_SESSION_URL + "#x"),
        ("redirect_uri", REDIRECT_URI + "?a=b"),
        ("app_origin", APP_ORIGIN + "/path"),
        ("issuer", "ftp://id.meridian.localhost"),
        ("client_id", "two words"),
        ("client_id", ""),
        ("client_credential", ""),
        ("default_return_path", "//evil.example"),
        ("return_prefixes", ("adjuster",)),
        ("return_prefixes", ("/adjuster", "https://evil.example")),
    ],
)
def test_an_address_with_a_query_a_password_or_a_bad_shape_is_refused(
    field: str, value: object
) -> None:
    with pytest.raises(Exception, match=field):
        flow_settings(**{field: value})


def test_a_refused_setting_never_quotes_a_value() -> None:
    environ = env(
        MERIDIAN_SIGNIN_STAFF_TOKEN_URL=f"http://user:{MARKER}@keycloak.test:8080/t",
        MERIDIAN_SIGNIN_STAFF_REDIRECT_URI=f"http://{MARKER}.test/cb",
    )

    with pytest.raises(SettingsError) as refused:
        from_env(environ)

    text = str(refused.value) + repr(refused.value.args)
    assert MARKER not in text
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    assert "MERIDIAN_SIGNIN_STAFF_TOKEN_URL" in text


def test_a_direct_construction_error_hides_the_input_too() -> None:
    with pytest.raises(Exception) as refused:
        flow_settings(token_url=f"http://user:{MARKER}@keycloak.test:8080/t")

    assert MARKER not in str(refused.value)


def test_the_credential_is_in_no_text_the_settings_make() -> None:
    settings = flow_settings(client_credential=CREDENTIAL_MARKER)

    texts = [str(settings), repr(settings), settings.model_dump_json()]
    for text in texts:
        assert CREDENTIAL_MARKER not in text
    assert settings.client_credential.get_secret_value() == CREDENTIAL_MARKER


def test_the_settings_are_frozen_and_take_no_unknown_field() -> None:
    settings = flow_settings()

    with pytest.raises(Exception, match="frozen"):
        settings.client_id = "other"  # type: ignore[misc]
    with pytest.raises(Exception, match="extra"):
        flow_settings(surprise="x")


# ── the flow wants the settings and the session cookie to agree ─────────────
def keys_for_flow() -> KeySet:
    return KeySet("https://id.example.test/keys")


@pytest.mark.parametrize("secure", [True, False])
def test_the_cookie_must_be_secure_exactly_when_the_app_is_served_over_https(
    secure: bool,
) -> None:
    # kind's app origin is plain http: a Secure cookie would never come back,
    # and an https origin with a cookie that is not Secure is a downgrade.
    kind = flow_settings()
    sessions = SessionSettings(keys=SessionKeys(current=b"k" * 32), secure=secure)

    if secure:
        with pytest.raises(SettingsError, match="Secure"):
            SigninFlow(kind, sessions, keys_for_flow())
    else:
        SigninFlow(kind, sessions, keys_for_flow())


def test_an_https_app_origin_needs_a_secure_cookie() -> None:
    settings = https_settings()
    loose = SessionSettings(keys=SessionKeys(current=b"k" * 32), secure=False)
    tight = SessionSettings(keys=SessionKeys(current=b"k" * 32), secure=True)

    with pytest.raises(SettingsError, match="Secure"):
        SigninFlow(settings, loose, keys_for_flow())
    SigninFlow(settings, tight, keys_for_flow())
