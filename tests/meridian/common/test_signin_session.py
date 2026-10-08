"""The session cookie (S021, T-05): what is sealed, what is opened, and what is
refused. The keys are made in the test."""

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any, get_args

import pytest
from starlette.responses import Response

from meridian.platform.common import signin, signinsession
from meridian.platform.common.env import SettingsError
from meridian.platform.common.signin import (
    MAX_ROLES,
    Principal,
    Reason,
    Unauthenticated,
)
from meridian.platform.common.signinsession import (
    COOKIE_NAME_PREFIX,
    ENVIRONMENT_ENV,
    KIND_ENVIRONMENT,
    MAX_COOKIE_BYTES,
    MAX_SESSION_SECONDS,
    MIN_SESSION_KEY_BYTES,
    PLAIN_HTTP_ONLY_ON_KIND,
    VERSION,
    SessionKeys,
    SessionSettings,
    clear_session_cookie,
    cookie_name,
    open_session,
    seal_session,
    set_session_cookie,
)
from meridian.platform.gateway import settings as gateway

NOW = 1_800_000_000.0
ENDS = int(NOW) + 600
ISSUER = "https://id.example.test/realms/meridian-staff"


def principal(**changes: Any) -> Principal:
    """A principal as a cookie opens it: ``via`` is ``cookie``."""
    fields: dict[str, Any] = {
        "population": "staff",
        "issuer": ISSUER,
        "subject": "subject-1",
        "roles": frozenset({"adjuster", "auditor"}),
        "expires_at": ENDS,
        "via": "cookie",
    }
    return Principal(**{**fields, **changes})


@pytest.fixture
def seal_a() -> bytes:
    return secrets.token_bytes(MIN_SESSION_KEY_BYTES)


@pytest.fixture
def seal_b() -> bytes:
    return secrets.token_bytes(MIN_SESSION_KEY_BYTES)


@pytest.fixture
def keys(seal_a: bytes) -> SessionKeys:
    return SessionKeys(current=seal_a)


def reason_of(cookie: str, keys: SessionKeys, now: float = NOW) -> Reason:
    with pytest.raises(Unauthenticated) as refused:
        open_session(cookie, keys, now, "staff")
    return refused.value.reason


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mac_of(key: bytes, body: str) -> str:
    return b64(hmac.new(key, body.encode(), hashlib.sha256).digest())


def payload_of(cookie: str) -> dict[str, Any]:
    return json.loads(unb64(cookie.split(".")[1]))


def resign(payload: dict[str, Any], key: bytes) -> str:
    """A cookie with this payload, signed under ``key``."""
    body = f"{VERSION}.{b64(json.dumps(payload, separators=(',', ':')).encode())}"
    return f"{body}.{mac_of(key, body)}"


# ── the round trip ──────────────────────────────────────────────────────────
def test_a_sealed_cookie_opens_to_the_same_principal(keys: SessionKeys) -> None:
    cookie = seal_session(principal(), keys, NOW)

    assert open_session(cookie, keys, NOW, "staff") == principal()


def test_a_cookie_sealed_from_a_bearer_principal_opens_as_a_cookie_principal(
    keys: SessionKeys,
) -> None:
    bearer = principal(via="bearer")

    cookie = seal_session(bearer, keys, NOW)
    opened = open_session(cookie, keys, NOW, "staff")

    assert opened.via == "cookie"
    assert opened.issuer == bearer.issuer == ISSUER
    assert opened == principal()
    assert "via" not in payload_of(cookie)


def test_the_cookie_is_versioned_and_compact(keys: SessionKeys) -> None:
    cookie = seal_session(principal(), keys, NOW)

    version, payload, mac = cookie.split(".")
    assert version == "v1"
    assert payload and len(mac) == 43  # 32 bytes, base64 without padding
    assert cookie.isascii()
    assert len(cookie) < MAX_COOKIE_BYTES // 4


def test_the_cookie_holds_only_the_five_fields_and_no_token(
    keys: SessionKeys,
) -> None:
    cookie = seal_session(principal(), keys, NOW)

    assert payload_of(cookie) == {
        "p": "staff",
        "i": "https://id.example.test/realms/meridian-staff",
        "s": "subject-1",
        "r": ["adjuster", "auditor"],
        "e": ENDS,
    }


def test_the_roles_are_sealed_in_a_fixed_order(keys: SessionKeys) -> None:
    one = seal_session(principal(roles=frozenset({"b", "a", "c"})), keys, NOW)
    two = seal_session(principal(roles=frozenset({"c", "a", "b"})), keys, NOW)

    assert one == two


def test_a_session_with_no_roles_round_trips(keys: SessionKeys) -> None:
    cookie = seal_session(principal(roles=frozenset()), keys, NOW)

    assert open_session(cookie, keys, NOW, "staff").roles == frozenset()


# ── expiry ──────────────────────────────────────────────────────────────────
def test_a_cookie_opens_until_its_expiry_and_not_at_it(keys: SessionKeys) -> None:
    cookie = seal_session(principal(), keys, NOW)

    assert open_session(cookie, keys, ENDS - 1, "staff") == principal()
    assert reason_of(cookie, keys, ENDS) is Reason.EXPIRED
    assert reason_of(cookie, keys, ENDS + 3600) is Reason.EXPIRED


def test_a_session_never_lasts_longer_than_the_cap(keys: SessionKeys) -> None:
    far = principal(expires_at=int(NOW) + 10 * MAX_SESSION_SECONDS)
    cookie = seal_session(far, keys, NOW)

    assert payload_of(cookie)["e"] == int(NOW) + MAX_SESSION_SECONDS
    assert reason_of(cookie, keys, NOW + MAX_SESSION_SECONDS) is Reason.EXPIRED


def test_the_cap_at_open_lets_the_cap_itself_through_and_not_a_second_more(
    keys: SessionKeys, seal_a: bytes
) -> None:
    payload = payload_of(seal_session(principal(), keys, NOW))
    cap = int(NOW) + MAX_SESSION_SECONDS
    at_cap = resign({**payload, "e": cap}, seal_a)
    over = resign({**payload, "e": cap + 1}, seal_a)

    assert open_session(at_cap, keys, NOW, "staff").expires_at == cap
    assert reason_of(over, keys) is Reason.SESSION_TOO_LONG
    # What the cap lets through at one moment is still inside it a moment later.
    assert open_session(at_cap, keys, NOW + 100, "staff").expires_at == cap
    assert open_session(over, keys, NOW + 1, "staff").expires_at == cap + 1


def test_the_cap_is_enforced_at_open_whichever_key_signed_the_cookie(
    seal_a: bytes, seal_b: bytes
) -> None:
    # A cookie made with a key that leaked, kept as the previous one, and given
    # an expiry 1000 days ahead.
    far = {
        **payload_of(seal_session(principal(), SessionKeys(current=seal_a), NOW)),
        "e": int(NOW) + 1000 * 24 * 3600,
    }
    rotated = SessionKeys(current=seal_b, previous=seal_a)

    assert reason_of(resign(far, seal_b), rotated) is Reason.SESSION_TOO_LONG
    assert reason_of(resign(far, seal_a), rotated) is Reason.SESSION_TOO_LONG


def test_a_principal_that_has_expired_is_not_sealed(keys: SessionKeys) -> None:
    with pytest.raises(Unauthenticated):
        seal_session(principal(expires_at=int(NOW)), keys, NOW)


def test_a_principal_too_large_for_a_cookie_is_not_sealed(keys: SessionKeys) -> None:
    roles = frozenset(f"role-{n:03d}-" + "x" * 56 for n in range(200))

    with pytest.raises(ValueError, match="too large"):
        seal_session(principal(roles=roles), keys, NOW)


def test_a_principal_with_the_most_roles_the_bounds_allow_fits_a_cookie(
    keys: SessionKeys,
) -> None:
    roles = frozenset(f"{n:02d}" + "x" * 62 for n in range(MAX_ROLES))
    subject = "s" * 128
    full = principal(
        roles=roles, subject=subject, issuer="https://i.example/" + "p" * 200
    )

    cookie = seal_session(full, keys, NOW)

    assert len(cookie) <= MAX_COOKIE_BYTES
    assert open_session(cookie, keys, NOW, "staff") == full


# ── population ──────────────────────────────────────────────────────────────
def test_the_other_populations_cookie_is_refused(keys: SessionKeys) -> None:
    cookie = seal_session(principal(population="claimant"), keys, NOW)

    assert reason_of(cookie, keys) is Reason.SESSION_POPULATION
    assert open_session(cookie, keys, NOW, "claimant").population == "claimant"


def test_the_two_populations_have_cookies_of_their_own_names() -> None:
    assert cookie_name("staff") != cookie_name("claimant")
    assert cookie_name("staff").startswith(COOKIE_NAME_PREFIX)


# ── altered, cut, extended ──────────────────────────────────────────────────
def test_any_one_flipped_bit_anywhere_is_refused(keys: SessionKeys) -> None:
    cookie = seal_session(principal(), keys, NOW)

    for index, char in enumerate(cookie):
        for bit in range(8):
            altered = cookie[:index] + chr(ord(char) ^ (1 << bit)) + cookie[index + 1 :]
            assert altered != cookie
            with pytest.raises(Unauthenticated):
                open_session(altered, keys, NOW, "staff")


def test_every_truncation_is_refused(keys: SessionKeys) -> None:
    cookie = seal_session(principal(), keys, NOW)

    for length in range(len(cookie)):
        with pytest.raises(Unauthenticated):
            open_session(cookie[:length], keys, NOW, "staff")


@pytest.mark.parametrize("tail", ["A", "=", ".", ".x", "\n", " ", "AAAA"])
def test_an_extended_cookie_is_refused(keys: SessionKeys, tail: str) -> None:
    cookie = seal_session(principal(), keys, NOW)

    assert isinstance(reason_of(cookie + tail, keys), Reason)


@pytest.mark.parametrize(
    "value", ["", ".", "..", "v1", "v1.", "v1..", "v1.a.b", "garbage", "é" * 10]
)
def test_values_that_are_not_cookies_are_refused(keys: SessionKeys, value: str) -> None:
    assert isinstance(reason_of(value, keys), Reason)


def test_a_cookie_of_another_version_is_refused(
    keys: SessionKeys, seal_a: bytes
) -> None:
    cookie = seal_session(principal(), keys, NOW)
    body = cookie.rsplit(".", 1)[0].replace("v1.", "v2.", 1)
    other = f"{body}.{mac_of(seal_a, body)}"  # a correct MAC over a v2 body

    assert reason_of(other, keys) is Reason.SESSION_VERSION


def test_a_cookie_over_the_size_bound_is_refused(
    keys: SessionKeys, seal_a: bytes
) -> None:
    payload = {**payload_of(seal_session(principal(), keys, NOW)), "x": "p" * 5000}
    cookie = resign(payload, seal_a)  # correctly signed, and too large

    assert len(cookie) > MAX_COOKIE_BYTES
    assert reason_of(cookie, keys) is Reason.SESSION_ALTERED


def test_a_role_added_by_hand_is_refused(keys: SessionKeys) -> None:
    cookie = seal_session(principal(), keys, NOW)
    version, payload, mac = cookie.split(".")
    data = json.loads(unb64(payload))
    data["r"].append("platform-admin")
    forged = b64(json.dumps(data, separators=(",", ":")).encode())

    assert reason_of(f"{version}.{forged}.{mac}", keys) is Reason.SESSION_ALTERED


def test_a_cookie_signed_with_a_key_the_app_does_not_hold_is_refused(
    keys: SessionKeys, seal_b: bytes
) -> None:
    cookie = resign(payload_of(seal_session(principal(), keys, NOW)), seal_b)

    assert reason_of(cookie, keys) is Reason.SESSION_ALTERED


@pytest.mark.parametrize(
    "change",
    [
        {"r": "adjuster"},
        {"r": [1]},
        {"r": [""]},
        {"s": ""},
        {"s": 7},
        {"e": "tomorrow"},
        {"e": float(ENDS)},
        {"e": True},
        {"p": 1},
    ],
)
def test_a_signed_payload_of_the_wrong_shape_is_refused(
    keys: SessionKeys, seal_a: bytes, change: dict[str, Any]
) -> None:
    payload = {**payload_of(seal_session(principal(), keys, NOW)), **change}

    assert isinstance(reason_of(resign(payload, seal_a), keys), Reason)


@pytest.mark.parametrize("field", ["p", "i", "s", "r", "e"])
def test_a_signed_payload_missing_a_field_or_with_another_is_refused(
    keys: SessionKeys, seal_a: bytes, field: str
) -> None:
    payload = payload_of(seal_session(principal(), keys, NOW))
    without = {k: v for k, v in payload.items() if k != field}

    assert isinstance(reason_of(resign(without, seal_a), keys), Reason)
    assert isinstance(reason_of(resign({**payload, "extra": 1}, seal_a), keys), Reason)


@pytest.mark.parametrize("body", ["null", "[]", '"text"', "1", "not json", "{"])
def test_a_signed_payload_that_is_not_an_object_is_refused(
    keys: SessionKeys, seal_a: bytes, body: str
) -> None:
    text = f"{VERSION}.{b64(body.encode())}"

    assert isinstance(reason_of(f"{text}.{mac_of(seal_a, text)}", keys), Reason)


def test_a_signed_payload_that_is_not_base64_is_refused(
    keys: SessionKeys, seal_a: bytes
) -> None:
    text = f"{VERSION}.!!!!"

    assert isinstance(reason_of(f"{text}.{mac_of(seal_a, text)}", keys), Reason)


# ── two keys: rotation ──────────────────────────────────────────────────────
def test_a_cookie_sealed_with_the_previous_key_still_opens(
    seal_a: bytes, seal_b: bytes
) -> None:
    old = seal_session(principal(), SessionKeys(current=seal_a), NOW)
    rotated = SessionKeys(current=seal_b, previous=seal_a)

    assert open_session(old, rotated, NOW, "staff") == principal()


def test_new_cookies_are_sealed_with_the_current_key_only(
    seal_a: bytes, seal_b: bytes
) -> None:
    rotated = SessionKeys(current=seal_b, previous=seal_a)

    new = seal_session(principal(), rotated, NOW)

    assert open_session(new, SessionKeys(current=seal_b), NOW, "staff") == principal()
    assert reason_of(new, SessionKeys(current=seal_a)) is Reason.SESSION_ALTERED


def test_a_cookie_sealed_with_a_key_that_was_rotated_out_is_refused(
    seal_a: bytes, seal_b: bytes
) -> None:
    old = seal_session(principal(), SessionKeys(current=seal_a), NOW)
    rotated_twice = SessionKeys(current=secrets.token_bytes(32), previous=seal_b)

    assert reason_of(old, rotated_twice) is Reason.SESSION_ALTERED


def test_a_key_under_the_minimum_length_stops_the_start(seal_a: bytes) -> None:
    short = secrets.token_bytes(MIN_SESSION_KEY_BYTES - 1)

    with pytest.raises(SettingsError, match="at least 32 bytes"):
        SessionKeys(current=short)
    with pytest.raises(SettingsError, match="at least 32 bytes"):
        SessionKeys(current=seal_a, previous=short)


def test_the_minimum_length_itself_is_accepted() -> None:
    SessionKeys(current=secrets.token_bytes(MIN_SESSION_KEY_BYTES))


def test_the_previous_key_must_differ_from_the_current(seal_a: bytes) -> None:
    with pytest.raises(SettingsError, match="must differ"):
        SessionKeys(current=seal_a, previous=seal_a)


def test_the_keys_are_not_in_the_repr(seal_a: bytes, seal_b: bytes) -> None:
    text = repr(SessionKeys(current=seal_a, previous=seal_b))

    assert repr(seal_a) not in text
    assert repr(seal_b) not in text


# ── settings from the environment ───────────────────────────────────────────
KEY_BYTES = secrets.token_bytes(48)
OLDER_BYTES = secrets.token_bytes(48)
KEY = base64.b64encode(KEY_BYTES).decode()
GOOD = {"MERIDIAN_SESSION_KEY": KEY}
KEY_VARIABLE = "MERIDIAN_SESSION_KEY"
PREVIOUS_VARIABLE = "MERIDIAN_SESSION_KEY_PREVIOUS"


def base64_of(raw: bytes, *, url_safe: bool = False, padded: bool = True) -> str:
    text = (base64.urlsafe_b64encode if url_safe else base64.b64encode)(raw).decode()
    return text if padded else text.rstrip("=")


def test_the_keys_are_read_from_the_environment_as_base64() -> None:
    env = {**GOOD, PREVIOUS_VARIABLE: base64_of(OLDER_BYTES)}

    settings = SessionSettings.from_env(env)

    assert settings.keys.current == KEY_BYTES
    assert settings.keys.previous == OLDER_BYTES
    assert settings.secure is True


def test_no_previous_key_is_none() -> None:
    assert SessionSettings.from_env(GOOD).keys.previous is None
    empty = {**GOOD, PREVIOUS_VARIABLE: ""}
    assert SessionSettings.from_env(empty).keys.previous is None


def test_a_missing_key_stops_the_start_naming_the_variable() -> None:
    with pytest.raises(SettingsError, match="MERIDIAN_SESSION_KEY is required"):
        SessionSettings.from_env({})


@pytest.mark.parametrize("url_safe", [False, True])
@pytest.mark.parametrize("padded", [False, True])
def test_a_key_is_accepted_in_either_base64_alphabet_with_or_without_padding(
    url_safe: bool, padded: bool
) -> None:
    # These six bytes are `+vv8/f7/` in the standard alphabet and `-vv8_f7_` in
    # the URL-safe one, so each form really uses its own characters. 47 bytes
    # need padding in the standard form.
    raw = bytes(range(250, 256)) + secrets.token_bytes(41)
    text = base64_of(raw, url_safe=url_safe, padded=padded)

    keys = SessionSettings.from_env({KEY_VARIABLE: text}).keys

    assert keys.current == raw


def test_one_trailing_newline_is_dropped_and_no_more() -> None:
    kept = SessionSettings.from_env({KEY_VARIABLE: KEY + "\n"})
    assert kept.keys.current == KEY_BYTES
    inside = KEY[:9] + "\n" + KEY[9:]
    for text in (KEY + "\n\n", KEY + "\r\n", KEY + " ", " " + KEY, inside):
        with pytest.raises(SettingsError, match=KEY_VARIABLE):
            SessionSettings.from_env({KEY_VARIABLE: text})


def test_a_key_that_mixes_the_two_alphabets_is_refused() -> None:
    raw = bytes(range(250, 256)) + secrets.token_bytes(42)
    mixed = base64_of(raw).replace("+", "-", 1)  # "-" with a "/" still in it
    assert "-" in mixed and "/" in mixed

    with pytest.raises(SettingsError, match=KEY_VARIABLE):
        SessionSettings.from_env({KEY_VARIABLE: mixed})


def test_the_length_floor_is_in_bytes_after_decoding() -> None:
    at_floor = secrets.token_bytes(MIN_SESSION_KEY_BYTES)
    below = secrets.token_bytes(MIN_SESSION_KEY_BYTES - 1)

    accepted = SessionSettings.from_env({KEY_VARIABLE: base64_of(at_floor)})
    assert accepted.keys.current == at_floor
    with pytest.raises(SettingsError, match=KEY_VARIABLE):
        SessionSettings.from_env({KEY_VARIABLE: base64_of(below)})


@pytest.mark.parametrize(
    "text",
    [
        "k" * 40,  # base64 of 30 bytes: too short
        "password" * 6,  # base64 of 36 bytes, but not random
        "a" * 64,  # base64 of 48 equal-looking bytes
        "A" * 64,  # 48 zero bytes
        "not base64 at all, not even close to it!!",
        "!" * 64,
    ],
)
def test_a_key_of_characters_a_person_picked_stops_the_start(text: str) -> None:
    with pytest.raises(SettingsError, match=KEY_VARIABLE):
        SessionSettings.from_env({KEY_VARIABLE: text})


def test_the_diversity_floor_is_sixteen_different_bytes() -> None:
    def key_of_distinct(count: int) -> str:
        return base64_of(bytes(range(count)) * (48 // count) + bytes(48 % count))

    assert SessionSettings.from_env({KEY_VARIABLE: key_of_distinct(16)}).keys.current
    with pytest.raises(SettingsError, match=KEY_VARIABLE):
        SessionSettings.from_env({KEY_VARIABLE: key_of_distinct(15)})


@pytest.mark.parametrize("variable", [KEY_VARIABLE, PREVIOUS_VARIABLE])
def test_a_bad_key_stops_the_start_naming_its_variable_and_holding_no_key(
    variable: str,
) -> None:
    marker = "key!CANARY!3f9a"  # "!" is in neither alphabet
    env = {**GOOD, variable: marker * 4}

    with pytest.raises(SettingsError) as error:
        SessionSettings.from_env(env)

    raised = error.value
    assert variable in str(raised)
    for place in (str(raised), repr(raised), str(raised.args), repr(vars(raised))):
        assert marker not in place
    assert raised.__cause__ is None
    assert raised.__context__ is None


def test_the_environment_variable_and_the_kind_value_are_the_gateways() -> None:
    # The modules repeat the gateway's names because ``common`` may not import
    # the gateway (an import-linter contract); this test may, and ties them.
    assert signinsession.ENVIRONMENT_ENV == signin.ENVIRONMENT_ENV
    assert ENVIRONMENT_ENV == gateway.ENVIRONMENT_ENV
    assert KIND_ENVIRONMENT in get_args(gateway.Environment)


def test_a_plain_http_edge_is_accepted_only_on_kind() -> None:
    plain = {**GOOD, "MERIDIAN_SESSION_EDGE_PLAIN_HTTP": "true"}

    on_kind = SessionSettings.from_env({**plain, ENVIRONMENT_ENV: "kind"})

    assert on_kind.secure is False
    for environment in ("azure", "local", "test", "ci", "Kind", "kind ", ""):
        with pytest.raises(SettingsError) as error:
            SessionSettings.from_env({**plain, ENVIRONMENT_ENV: environment})
        assert str(error.value) == PLAIN_HTTP_ONLY_ON_KIND
    with pytest.raises(SettingsError) as error:
        SessionSettings.from_env(plain)
    assert str(error.value) == PLAIN_HTTP_ONLY_ON_KIND


def test_kind_alone_does_not_turn_secure_off() -> None:
    assert SessionSettings.from_env({**GOOD, ENVIRONMENT_ENV: "kind"}).secure is True
    explicit = {
        **GOOD,
        ENVIRONMENT_ENV: "kind",
        "MERIDIAN_SESSION_EDGE_PLAIN_HTTP": "false",
    }
    assert SessionSettings.from_env(explicit).secure is True


@pytest.mark.parametrize("flag", ["yes", "1", "on", "tru"])
def test_a_flag_that_is_not_true_or_false_stops_the_start(flag: str) -> None:
    env = {**GOOD, "MERIDIAN_SESSION_EDGE_PLAIN_HTTP": flag, ENVIRONMENT_ENV: "kind"}

    with pytest.raises(SettingsError, match="must be true or false"):
        SessionSettings.from_env(env)


# ── the cookie's attributes ─────────────────────────────────────────────────
def set_cookie_line(response: Response) -> str:
    return response.headers["set-cookie"]


def value_of(response: Response) -> str:
    """The cookie's value, as the browser would send it back."""
    return set_cookie_line(response).split(";")[0].partition("=")[2]


def test_the_cookie_is_http_only_lax_path_root_and_secure(keys: SessionKeys) -> None:
    response = Response()

    set_session_cookie(response, principal(), SessionSettings(keys), NOW)

    line = set_cookie_line(response)
    assert line.startswith(f"{cookie_name('staff')}={value_of(response)}")
    attributes = {part.strip().lower() for part in line.split(";")[1:]}
    assert {"httponly", "secure", "samesite=lax", "path=/", "max-age=600"} <= attributes


def test_the_cookie_set_is_the_principals_own_and_ends_when_its_max_age_does(
    keys: SessionKeys,
) -> None:
    response = Response()
    mine = principal(subject="mine", expires_at=int(NOW) + 100)

    set_session_cookie(response, mine, SessionSettings(keys), NOW)

    value = value_of(response)
    assert "Max-Age=100" in set_cookie_line(response)
    assert open_session(value, keys, NOW, "staff") == mine
    assert reason_of(value, keys, NOW + 100) is Reason.EXPIRED


def test_on_a_plain_http_edge_only_secure_is_dropped(keys: SessionKeys) -> None:
    response = Response()
    settings = SessionSettings(keys, secure=False)

    set_session_cookie(response, principal(), settings, NOW)

    attributes = {part.strip().lower() for part in set_cookie_line(response).split(";")}
    assert "secure" not in attributes
    assert {"httponly", "samesite=lax", "path=/"} <= attributes


def test_the_cookie_does_not_outlive_the_cap(keys: SessionKeys) -> None:
    response = Response()
    far = principal(expires_at=int(NOW) + 10 * MAX_SESSION_SECONDS)

    set_session_cookie(response, far, SessionSettings(keys), NOW)

    assert f"Max-Age={MAX_SESSION_SECONDS}" in set_cookie_line(response)
    assert open_session(value_of(response), keys, NOW, "staff").expires_at == (
        int(NOW) + MAX_SESSION_SECONDS
    )


def test_a_principal_that_has_ended_gets_no_cookie_at_all(keys: SessionKeys) -> None:
    response = Response()

    with pytest.raises(Unauthenticated) as refused:
        set_session_cookie(
            response, principal(expires_at=int(NOW) - 5), SessionSettings(keys), NOW
        )

    assert refused.value.reason is Reason.EXPIRED
    assert "set-cookie" not in response.headers


def test_clearing_the_cookie_keeps_the_attributes(keys: SessionKeys) -> None:
    response = Response()

    clear_session_cookie(response, "staff", SessionSettings(keys))

    attributes = {part.strip().lower() for part in set_cookie_line(response).split(";")}
    assert {"httponly", "secure", "samesite=lax", "path=/", "max-age=0"} <= attributes
    assert set_cookie_line(response).startswith(f"{cookie_name('staff')}=")


# ── a refusal says nothing ──────────────────────────────────────────────────
def test_a_refusal_carries_only_its_reason(keys: SessionKeys) -> None:
    cookie = seal_session(principal(subject="subject-canary-4"), keys, NOW)
    altered = cookie[:-3] + ("AAA" if not cookie.endswith("AAA") else "BBB")

    with pytest.raises(Unauthenticated) as refused:
        open_session(altered, keys, NOW, "staff")

    error = refused.value
    assert str(error) == error.reason.value
    assert "canary" not in repr(error) + str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
