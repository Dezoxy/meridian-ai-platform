"""Starting a sign-in and the transaction cookie (S021, Y3): the redirect to the
issuer, what travels in the cookie, and what the cookie refuses when it comes
back."""

import base64
import hashlib
import hmac
import secrets
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
from signinflowsupport import (
    AUTHORIZATION_URL,
    CALLBACK_PATH,
    CLIENT_ID,
    NOW,
    REDIRECT_URI,
    FlowKit,
    flow_settings,
    https_settings,
    s256,
)

from meridian.platform.common.signin import Principal, Reason, Unauthenticated
from meridian.platform.common.signinflow import SigninFlow
from meridian.platform.common.signinkeys import KeySet
from meridian.platform.common.signinsession import (
    SessionKeys,
    SessionSettings,
    open_session,
    seal_session,
)
from meridian.platform.common.signinstate import (
    MAX_TRANSACTION_COOKIE_BYTES,
    TRANSACTION_COOKIE_PREFIX,
    TRANSACTION_SECONDS,
    TRANSACTION_VERSION,
    FlowReason,
    Transaction,
    open_transaction,
    seal_transaction,
    transaction_cookie_name,
)


def keys_of(*raw: bytes) -> SessionKeys:
    return SessionKeys(current=raw[0], previous=raw[1] if len(raw) > 1 else None)


def a_transaction(**changes: Any) -> Transaction:
    fields: dict[str, Any] = {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "verifier": secrets.token_urlsafe(48),
        "return_to": "/adjuster/claims",
        "population": "staff",
        "expires_at": int(NOW) + TRANSACTION_SECONDS,
    }
    return Transaction(**{**fields, **changes})


@pytest.fixture
def key() -> bytes:
    return secrets.token_bytes(32)


# ── the redirect to the issuer ──────────────────────────────────────────────
def test_the_redirect_goes_to_the_issuers_front_address_with_the_code_flow(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin("/adjuster/claims")

    location = started.response.headers["location"]
    assert location.startswith(AUTHORIZATION_URL + "?")
    assert started.response.status_code == 303
    assert started.query == {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid",
        "state": started.state,
        "nonce": started.nonce,
        "code_challenge": started.challenge,
        "code_challenge_method": "S256",
    }


def test_the_challenge_is_the_s256_of_the_verifier_that_stays_in_the_cookie(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin()

    opened = open_transaction(started.cookie, flow_kit.session_keys, NOW, "staff")

    assert isinstance(opened, Transaction)
    assert s256(opened.verifier) == started.challenge
    assert opened.verifier not in started.response.headers["location"]
    assert opened.state == started.state
    assert opened.nonce == started.nonce


def test_state_nonce_and_verifier_are_random_and_long_enough(
    flow_kit: FlowKit,
) -> None:
    first, second = flow_kit.begin(), flow_kit.begin()
    opened = open_transaction(first.cookie, flow_kit.session_keys, NOW, "staff")
    again = open_transaction(second.cookie, flow_kit.session_keys, NOW, "staff")

    assert isinstance(opened, Transaction)
    assert isinstance(again, Transaction)
    assert first.state != second.state
    assert first.nonce != second.nonce
    assert opened.verifier != again.verifier
    # RFC 7636: a verifier is 43 to 128 characters; the other two carry 256 bits.
    assert 43 <= len(opened.verifier) <= 128
    assert len(first.state) >= 43
    assert len(first.nonce) >= 43
    assert len({first.state, first.nonce, opened.verifier}) == 3


def test_the_url_carries_neither_the_verifier_nor_the_credential(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin()
    opened = open_transaction(started.cookie, flow_kit.session_keys, NOW, "staff")

    assert isinstance(opened, Transaction)
    url = started.response.headers["location"]
    assert opened.verifier not in url
    assert flow_kit.settings.client_credential.get_secret_value() not in url
    assert "plain" not in url
    assert started.response.headers["cache-control"] == "no-store"


def test_the_scopes_come_from_the_settings(flow_kit: FlowKit) -> None:
    settings = flow_settings(scopes=("openid", "roles"))
    flow = SigninFlow(
        settings,
        flow_kit.sessions,
        flow_kit.keys,
        transport=flow_kit.endpoint.transport,
    )

    response = flow.start(None, NOW)

    query = dict(parse_qsl(urlsplit(response.headers["location"]).query))
    assert query["scope"] == "openid roles"


# ── the cookie the app sets on the redirect ─────────────────────────────────
def test_the_cookie_is_short_lived_http_only_lax_and_only_for_the_callback(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin()

    line = started.response.headers["set-cookie"]
    attributes = {part.strip().lower() for part in line.split(";")[1:]}
    assert line.startswith(f"{TRANSACTION_COOKIE_PREFIX}staff=")
    assert "httponly" in attributes
    assert "samesite=lax" in attributes
    assert f"path={CALLBACK_PATH}".lower() in attributes
    assert f"max-age={TRANSACTION_SECONDS}" in attributes
    assert "secure" not in attributes  # kind's edge is plain http
    assert not any(a.startswith("domain=") for a in attributes)


def test_the_cookie_is_secure_when_the_session_cookie_is(flow_kit: FlowKit) -> None:
    settings = https_settings()
    sessions = SessionSettings(keys=flow_kit.session_keys, secure=True)
    flow = SigninFlow(settings, sessions, KeySet("https://id.example.test/keys"))

    response = flow.start(None, NOW)

    attributes = {p.strip().lower() for p in response.headers["set-cookie"].split(";")}
    assert "secure" in attributes
    assert "path=/cb" in attributes


def test_the_cookie_name_is_per_population() -> None:
    assert transaction_cookie_name("staff") == TRANSACTION_COOKIE_PREFIX + "staff"
    assert transaction_cookie_name("claimant") != transaction_cookie_name("staff")
    assert transaction_cookie_name("staff") != "meridian_session_staff"


def test_the_cookie_fits_a_browsers_limit_with_the_longest_return_path(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin("/adjuster/" + "a" * 240)

    assert len(started.cookie) <= MAX_TRANSACTION_COOKIE_BYTES
    assert MAX_TRANSACTION_COOKIE_BYTES <= 4096


# ── where to return to ──────────────────────────────────────────────────────
def test_a_return_path_on_the_app_is_kept(flow_kit: FlowKit) -> None:
    started = flow_kit.begin("/adjuster/claims/c-1")

    opened = open_transaction(started.cookie, flow_kit.session_keys, NOW, "staff")

    assert isinstance(opened, Transaction)
    assert opened.return_to == "/adjuster/claims/c-1"


@pytest.mark.parametrize(
    "candidate",
    [
        "https://evil.example/adjuster",
        "//evil.example/adjuster",
        "/\\evil.example",
        "/%2F%2Fevil.example",
        "%2F%2Fevil.example",
        "/admin",
        "",
        None,
    ],
)
def test_a_return_path_off_the_allowlist_becomes_the_default_page(
    flow_kit: FlowKit, candidate: str | None
) -> None:
    started = flow_kit.begin(candidate)

    opened = open_transaction(started.cookie, flow_kit.session_keys, NOW, "staff")

    assert isinstance(opened, Transaction)
    assert opened.return_to == "/"


# ── the cookie's form and what it refuses ───────────────────────────────────
def test_a_sealed_transaction_opens_to_what_was_sealed(key: bytes) -> None:
    keys = keys_of(key)
    transaction = a_transaction()

    cookie = seal_transaction(transaction, keys)
    opened = open_transaction(cookie, keys, NOW, "staff")

    assert opened == transaction
    assert cookie.startswith(TRANSACTION_VERSION + ".")
    assert cookie.isascii()


def test_the_payload_holds_no_field_beyond_the_six(key: bytes) -> None:
    import base64
    import json

    cookie = seal_transaction(a_transaction(), keys_of(key))

    payload = json.loads(base64.urlsafe_b64decode(cookie.split(".")[1] + "=="))

    assert set(payload) == {"s", "n", "v", "r", "p", "e"}


def test_the_transaction_repr_shows_none_of_its_secrets() -> None:
    transaction = a_transaction()

    text = repr(transaction) + str(transaction)

    for secret in (transaction.state, transaction.nonce, transaction.verifier):
        assert secret not in text


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        pytest.param(
            lambda c: c[:-1] + ("A" if c[-1] != "A" else "B"), "altered", id="mac-last"
        ),
        pytest.param(lambda c: c[:-2], "altered", id="truncated"),
        pytest.param(lambda c: c + "A", "altered", id="extended"),
        pytest.param(lambda c: c.replace("t1.", "t1.A", 1), "altered", id="payload"),
        pytest.param(lambda c: c + ".extra", "altered", id="extra-part"),
        pytest.param(lambda c: c.rsplit(".", 1)[0], "altered", id="no-mac"),
        pytest.param(lambda c: "t1..", "altered", id="empty-parts"),
        pytest.param(lambda c: "", "altered", id="empty"),
        pytest.param(lambda c: "é" + c, "altered", id="non-ascii"),
        pytest.param(lambda c: c + "A" * 5000, "altered", id="oversized"),
        pytest.param(lambda c: "t2." + c.split(".", 1)[1], "version", id="version"),
        pytest.param(
            lambda c: "v1." + c.split(".", 1)[1], "version", id="session-prefix"
        ),
    ],
)
def test_a_changed_cookie_is_refused(key: bytes, mutate: Any, reason: str) -> None:
    keys = keys_of(key)
    cookie = seal_transaction(a_transaction(), keys)

    opened = open_transaction(mutate(cookie), keys, NOW, "staff")

    expected = {
        "altered": FlowReason.TRANSACTION_ALTERED,
        "version": FlowReason.TRANSACTION_VERSION,
    }[reason]
    assert opened is expected


def test_a_cookie_signed_with_another_key_is_refused(key: bytes) -> None:
    cookie = seal_transaction(a_transaction(), keys_of(secrets.token_bytes(32)))

    opened = open_transaction(cookie, keys_of(key), NOW, "staff")

    assert opened is FlowReason.TRANSACTION_ALTERED


def test_the_previous_key_still_opens_what_it_sealed(key: bytes) -> None:
    old = secrets.token_bytes(32)
    cookie = seal_transaction(a_transaction(), keys_of(old))

    opened = open_transaction(cookie, keys_of(key, old), NOW, "staff")

    assert isinstance(opened, Transaction)


def test_a_session_cookie_offered_as_the_transaction_is_refused(key: bytes) -> None:
    keys = keys_of(key)
    principal = Principal(
        population="staff",
        issuer="http://id.test/r",
        subject="s",
        roles=frozenset({"adjuster"}),
        expires_at=int(NOW) + 300,
        via="cookie",
    )
    session = seal_session(principal, keys, NOW)

    opened = open_transaction(session, keys, NOW, "staff")

    assert opened is FlowReason.TRANSACTION_VERSION


def test_a_transaction_cookie_offered_as_the_session_is_refused(key: bytes) -> None:
    keys = keys_of(key)
    cookie = seal_transaction(a_transaction(), keys)

    with pytest.raises(Unauthenticated) as refused:
        open_session(cookie, keys, NOW, "staff")

    assert refused.value.reason is Reason.SESSION_VERSION


def test_the_transaction_is_signed_with_a_key_derived_for_it(key: bytes) -> None:
    # Not the session key itself: were the version words ever made equal, a
    # transaction's MAC would still not be a valid session MAC for its text.
    cookie = seal_transaction(a_transaction(), keys_of(key))
    body, _, mac = cookie.rpartition(".")

    by_the_session_key = hmac.new(key, body.encode(), hashlib.sha256).digest()

    assert mac != base64.urlsafe_b64encode(by_the_session_key).rstrip(b"=").decode()


def test_a_transaction_relabelled_as_a_session_is_refused(key: bytes) -> None:
    keys = keys_of(key)
    cookie = seal_transaction(a_transaction(), keys)
    relabelled = "v1." + cookie.split(".", 1)[1]

    with pytest.raises(Unauthenticated) as refused:
        open_session(relabelled, keys, NOW, "staff")

    assert refused.value.reason is Reason.SESSION_ALTERED


def test_a_transaction_is_valid_until_its_expiry_and_not_after(key: bytes) -> None:
    keys = keys_of(key)
    cookie = seal_transaction(a_transaction(expires_at=int(NOW) + 100), keys)

    assert isinstance(open_transaction(cookie, keys, NOW + 99, "staff"), Transaction)
    assert (
        open_transaction(cookie, keys, NOW + 100, "staff")
        is FlowReason.TRANSACTION_EXPIRED
    )
    assert (
        open_transaction(cookie, keys, NOW + 5000, "staff")
        is FlowReason.TRANSACTION_EXPIRED
    )


def test_a_transaction_that_lives_longer_than_the_bound_is_refused_when_opened(
    key: bytes,
) -> None:
    # A cookie signed with a key that leaked, or a bug, must not outlive the bound.
    keys = keys_of(key)
    longest = int(NOW) + TRANSACTION_SECONDS
    ok = seal_transaction(a_transaction(expires_at=longest), keys)
    long = seal_transaction(a_transaction(expires_at=longest + 1), keys)

    assert isinstance(open_transaction(ok, keys, NOW, "staff"), Transaction)
    assert open_transaction(long, keys, NOW, "staff") is FlowReason.TRANSACTION_TOO_LONG


def test_another_populations_transaction_is_refused(key: bytes) -> None:
    keys = keys_of(key)
    cookie = seal_transaction(a_transaction(population="claimant"), keys)

    assert (
        open_transaction(cookie, keys, NOW, "staff")
        is FlowReason.TRANSACTION_POPULATION
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "short"},
        {"state": "a" * 129},
        {"state": "has space " + "a" * 40},
        {"nonce": ""},
        {"verifier": "x" * 42},
        {"verifier": "é" * 50},
        {"return_to": "//evil.example"},
        {"return_to": "https://evil.example"},
        {"return_to": 5},
        {"expires_at": "soon"},
        {"expires_at": True},
    ],
)
def test_a_signed_payload_of_the_wrong_shape_is_refused(
    key: bytes, changes: dict[str, Any]
) -> None:
    # Sealed by the real key, so the MAC holds; the shape is what is wrong.
    keys = keys_of(key)
    cookie = seal_transaction(a_transaction(**changes), keys)

    assert (
        open_transaction(cookie, keys, NOW, "staff") is FlowReason.TRANSACTION_ALTERED
    )
