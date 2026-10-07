"""The bearer check (S021, T-05): what a token must be to name a person. Keys are
made in the test; the key URL is a mock transport; the time is given."""

from collections.abc import Callable
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from pydantic import ValidationError

from meridian.platform.common.env import SettingsError
from meridian.platform.common.signin import (
    ALGORITHMS,
    LEEWAY_SECONDS,
    MAX_ROLE_CHARS,
    MAX_ROLES,
    MAX_SUBJECT_CHARS,
    MAX_TOKEN_CHARS,
    Forbidden,
    Principal,
    Reason,
    SigninRefusal,
    SigninSettings,
    Unauthenticated,
    bearer_token_of,
    check_bearer,
    roles_of,
)
from meridian.platform.common.signinkeys import (
    KEY_MAX_AGE_SECONDS,
    REFETCH_INTERVAL_SECONDS,
    KeySet,
)


def refusal_of(
    token: str, settings: SigninSettings, keys: KeySet, now: float
) -> Reason:
    with pytest.raises(Unauthenticated) as refused:
        check_bearer(token, settings, keys, now)
    return refused.value.reason


# ── a good token ────────────────────────────────────────────────────────────
def test_a_good_token_names_the_principal(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(roles=["adjuster", "auditor"])

    principal = check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    assert principal == Principal(
        population="staff",
        issuer=signin_issuer.name,
        subject="subject-1",
        roles=frozenset({"adjuster", "auditor"}),
        expires_at=int(signin_issuer.now) + 600,
    )
    assert principal.has_role("auditor")
    assert not principal.has_role("platform-admin")


def test_the_principal_does_not_show_its_subject_or_roles(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(sub="subject-canary-9", roles=["role-canary-9"])

    principal = check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    assert "canary" not in repr(principal) + str(principal)


def test_the_algorithms_list_is_exactly_rs256() -> None:
    assert ALGORITHMS == ["RS256"]


# ── algorithms: refused before any key is looked at ─────────────────────────
def test_alg_none_is_refused_before_any_key_is_used(
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    signin_forge: Callable[..., str],
) -> None:
    token = signin_forge(
        {"alg": "none", "typ": "JWT", "kid": "kid-a"}, signin_issuer.claims()
    )

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.ALGORITHM
    assert signin_issuer.calls == 0


def test_hs256_signed_with_the_public_keys_bytes_is_refused_before_any_key(
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    signin_forge: Callable[..., str],
    signin_hmac_with_public_key: Callable[[bytes], bytes],
) -> None:
    token = signin_forge(
        {"alg": "HS256", "typ": "JWT", "kid": "kid-a"},
        signin_issuer.claims(),
        signin_hmac_with_public_key,
    )

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.ALGORITHM
    assert signin_issuer.calls == 0


def test_es256_is_refused_before_any_key(
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    signin_ec_key: ec.EllipticCurvePrivateKey,
) -> None:
    token = signin_issuer.mint(alg="ES256", key=signin_ec_key)

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.ALGORITHM
    assert signin_issuer.calls == 0


@pytest.mark.parametrize("alg", ["PS256", "RS384", "RS512"])
def test_other_rsa_algorithms_are_refused_before_any_key(
    alg: str, signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(alg=alg)

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.ALGORITHM
    assert signin_issuer.calls == 0


def test_a_header_with_no_alg_is_refused_before_any_key(
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    signin_forge: Callable[..., str],
) -> None:
    token = signin_forge({"typ": "JWT", "kid": "kid-a"}, signin_issuer.claims())

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.ALGORITHM
    assert signin_issuer.calls == 0


@pytest.mark.parametrize("alg", [None, 256, ["RS256"], "rs256", "RS256 "])
def test_an_alg_that_is_not_exactly_rs256_is_refused(
    alg: object,
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    signin_forge: Callable[..., str],
) -> None:
    token = signin_forge(
        {"alg": alg, "typ": "JWT", "kid": "kid-a"}, signin_issuer.claims()
    )

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.ALGORITHM
    assert signin_issuer.calls == 0


@pytest.mark.parametrize("kid", [None, ""])
def test_a_token_without_a_key_id_is_refused_before_any_fetch(
    kid: str | None,
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
) -> None:
    token = signin_issuer.mint(kid=kid)

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.NO_KEY_ID
    assert signin_issuer.calls == 0


# ── signature, issuer, audience ─────────────────────────────────────────────
def test_the_right_key_of_another_issuer_is_refused(
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    signin_rsa_pool: list[rsa.RSAPrivateKey],
) -> None:
    # Another issuer's key, under the key id this issuer uses.
    token = signin_issuer.mint(key=signin_rsa_pool[1])

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.SIGNATURE


def test_a_token_this_issuers_key_signed_for_another_issuer_is_refused(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(iss="https://id.example.test/realms/meridian-claimants")

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.ISSUER


@pytest.mark.parametrize(
    "issuer",
    [
        "https://id.example.test/realms/meridian-staff/",
        "HTTPS://id.example.test/realms/meridian-staff",
        "https://id.example.test/realms/meridian-staff ",
    ],
)
def test_the_issuer_is_compared_exactly(
    issuer: object,
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
) -> None:
    token = signin_issuer.mint(iss=issuer)

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.ISSUER


def test_an_issuer_claim_that_is_a_list_is_refused_and_is_no_server_error(
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    signin_forge: Callable[..., str],
) -> None:
    # PyJWT will not build the token (its encode wants a string): signed by hand
    # with the issuer's key.
    private = next(iter(signin_issuer.keys.values()))
    token = signin_forge(
        {"alg": "RS256", "typ": "JWT", "kid": "kid-a"},
        signin_issuer.claims(iss=[signin_issuer.name]),
        lambda data: private.sign(data, padding.PKCS1v15(), hashes.SHA256()),
    )

    assert isinstance(
        refusal_of(token, signin_settings, signin_keys, signin_issuer.now), Reason
    )


def test_an_altered_payload_fails_the_signature(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    head, _, tail = signin_issuer.mint().partition(".")
    other = signin_issuer.mint(roles=["platform-admin"]).split(".")[1]

    reason = refusal_of(
        f"{head}.{other}.{tail.partition('.')[2]}",
        signin_settings,
        signin_keys,
        signin_issuer.now,
    )

    assert reason is Reason.SIGNATURE


def test_the_wrong_audience_is_refused(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(aud="another-api")

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.AUDIENCE


def test_a_list_audience_that_holds_ours_is_accepted(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(aud=["another-api", signin_issuer.audience])

    principal = check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    assert principal.subject == "subject-1"


def test_a_list_audience_that_does_not_hold_ours_is_refused(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(aud=["another-api", "a-third"])

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.AUDIENCE


# ── times: both sides of the leeway ─────────────────────────────────────────
@pytest.mark.parametrize(
    ("exp_offset", "accepted"),
    [
        (600, True),
        (0, True),
        (-(LEEWAY_SECONDS - 1), True),
        (-LEEWAY_SECONDS, False),
        (-(LEEWAY_SECONDS + 1), False),
        (-3600, False),
    ],
)
def test_expiry_is_judged_with_the_leeway(
    exp_offset: int,
    accepted: bool,
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
) -> None:
    token = signin_issuer.mint(exp=signin_issuer.now + exp_offset)

    if accepted:
        check_bearer(token, signin_settings, signin_keys, signin_issuer.now)
    else:
        reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)
        assert reason is Reason.EXPIRED


@pytest.mark.parametrize(
    ("nbf_offset", "accepted"),
    [
        (-10, True),
        (0, True),
        (LEEWAY_SECONDS, True),
        (LEEWAY_SECONDS + 1, False),
        (3600, False),
    ],
)
def test_not_before_is_judged_with_the_leeway(
    nbf_offset: int,
    accepted: bool,
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
) -> None:
    token = signin_issuer.mint(nbf=signin_issuer.now + nbf_offset)

    if accepted:
        check_bearer(token, signin_settings, signin_keys, signin_issuer.now)
    else:
        reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)
        assert reason is Reason.NOT_YET_VALID


def test_the_time_is_the_one_given_not_the_machines(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint()

    far_future = signin_issuer.now + 10**7

    assert refusal_of(token, signin_settings, signin_keys, far_future) is Reason.EXPIRED


# ── claims that must be there, and be what they should ──────────────────────
@pytest.mark.parametrize("claim", ["sub", "exp", "nbf", "iss", "aud"])
def test_a_missing_required_claim_is_refused(
    claim: str,
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
) -> None:
    token = signin_issuer.mint(drop=(claim,))

    assert refusal_of(token, signin_settings, signin_keys, signin_issuer.now) in (
        Reason.CLAIMS,
        Reason.ISSUER,
        Reason.AUDIENCE,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"sub": 12},
        {"sub": ""},
        {"sub": "x" * (MAX_SUBJECT_CHARS + 1)},
        {"sub": ["a"]},
        {"exp": "tomorrow"},
        {"exp": True},
        {"exp": float("nan")},
        {"exp": float("inf")},
        {"nbf": float("nan")},
        {"nbf": "now"},
    ],
    ids=[
        "sub-number",
        "sub-empty",
        "sub-long",
        "sub-list",
        "exp-string",
        "exp-bool",
        "exp-nan",
        "exp-infinity",
        "nbf-nan",
        "nbf-string",
    ],
)
def test_a_claim_of_the_wrong_kind_is_refused(
    overrides: dict[str, Any],
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
) -> None:
    token = signin_issuer.mint(**overrides)

    assert isinstance(
        refusal_of(token, signin_settings, signin_keys, signin_issuer.now), Reason
    )


def test_a_subject_at_the_bound_is_accepted(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(sub="x" * MAX_SUBJECT_CHARS)

    principal = check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    assert len(principal.subject) == MAX_SUBJECT_CHARS


# ── the token's size, and its shape ─────────────────────────────────────────
def test_a_token_over_the_size_bound_is_refused_before_it_is_parsed(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(padding="p" * MAX_TOKEN_CHARS)

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert len(token) > MAX_TOKEN_CHARS
    assert reason is Reason.TOO_LARGE
    assert signin_issuer.calls == 0


def test_a_token_at_the_size_bound_is_read(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    base = len(signin_issuer.mint(padding=""))
    # Three base64 characters per two bytes of padding; trim until it fits.
    padding = "p" * ((MAX_TOKEN_CHARS - base) * 3 // 4)
    token = signin_issuer.mint(padding=padding)
    while len(token) > MAX_TOKEN_CHARS:
        padding = padding[:-1]
        token = signin_issuer.mint(padding=padding)

    assert MAX_TOKEN_CHARS - len(token) < 8
    check_bearer(token, signin_settings, signin_keys, signin_issuer.now)


@pytest.mark.parametrize(
    "token",
    ["", "abc", "a.b", "a.b.c.d", "a..c", "é.é.é", "a b.c.d", "..", "{}.{}.{}"],
)
def test_a_value_that_is_not_a_compact_token_is_malformed(
    token: str,
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
) -> None:
    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.MALFORMED
    assert signin_issuer.calls == 0


def test_an_unknown_key_id_is_refused_after_the_one_fetch(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(kid="kid-x")

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.UNKNOWN_KEY
    assert signin_issuer.calls == 1


def test_with_the_key_url_down_and_nothing_cached_a_good_token_is_refused(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint()
    signin_issuer.down = True

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.KEYS_UNAVAILABLE


def test_with_the_key_url_down_a_cached_key_still_verifies_a_good_token(
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    signin_clock: Any,
) -> None:
    token = signin_issuer.mint()
    check_bearer(token, signin_settings, signin_keys, signin_issuer.now)
    signin_issuer.down = True
    signin_clock.advance(KEY_MAX_AGE_SECONDS + 1)

    principal = check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    assert principal.subject == "subject-1"


def test_a_restart_of_the_issuer_the_next_token_verifies_after_one_refetch(
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    signin_clock: Any,
    signin_rsa_pool: list[rsa.RSAPrivateKey],
) -> None:
    check_bearer(signin_issuer.mint(), signin_settings, signin_keys, signin_issuer.now)
    signin_issuer.restart({"kid-new": signin_rsa_pool[1]})
    token = signin_issuer.mint(kid="kid-new")
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)

    principal = check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    assert principal.subject == "subject-1"
    assert signin_issuer.calls == 2


# ── the roles claim ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        (["adjuster"], {"adjuster"}),
        ([], set()),
        (["a", "a"], {"a"}),
        ("adjuster", set()),
        (7, set()),
        (None, set()),
        ({"adjuster": True}, set()),
        ([["adjuster"]], set()),
        (["adjuster", 1], set()),
        (["adjuster", None], set()),
        (["adjuster", ""], set()),
        (["x" * (MAX_ROLE_CHARS + 1)], set()),
        (["x" * MAX_ROLE_CHARS], {"x" * MAX_ROLE_CHARS}),
        ([f"r{n}" for n in range(MAX_ROLES + 1)], set()),
        ([f"r{n}" for n in range(MAX_ROLES)], {f"r{n}" for n in range(MAX_ROLES)}),
    ],
)
def test_the_roles_claim_must_be_a_list_of_strings(
    claim: object, expected: set[str]
) -> None:
    assert roles_of(claim) == frozenset(expected)


@pytest.mark.parametrize("roles", ["adjuster", 7, [["adjuster"]], {"a": "b"}])
def test_a_roles_claim_of_the_wrong_shape_gives_no_roles_and_no_error(
    roles: object,
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
) -> None:
    token = signin_issuer.mint(roles=roles)

    principal = check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    assert principal.roles == frozenset()


def test_the_roles_claim_name_is_a_setting(
    signin_issuer: Any, signin_keys: KeySet
) -> None:
    settings = SigninSettings(
        population="staff",
        issuer=signin_issuer.name,
        audience=signin_issuer.audience,
        keys_url=signin_issuer.url,
        roles_claim="groups",
    )
    token = signin_issuer.mint(roles=["not-this"], groups=["this"])

    principal = check_bearer(token, settings, signin_keys, signin_issuer.now)

    assert principal.roles == frozenset({"this"})


# ── what a refusal is ───────────────────────────────────────────────────────
def test_the_two_refusals_tell_401_from_403() -> None:
    assert Unauthenticated.status == 401
    assert Forbidden.status == 403
    assert issubclass(Unauthenticated, SigninRefusal)
    assert issubclass(Forbidden, SigninRefusal)


def test_an_error_the_library_was_not_expected_to_raise_is_a_refusal_with_no_trace(
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("library text with subject-canary-8")

    monkeypatch.setattr("meridian.platform.common.signin.jwt.decode", explode)

    with pytest.raises(Unauthenticated) as refused:
        check_bearer(
            signin_issuer.mint(), signin_settings, signin_keys, signin_issuer.now
        )

    assert refused.value.reason is Reason.MALFORMED
    assert "canary" not in str(refused.value) + repr(refused.value)
    assert refused.value.__context__ is None


def test_a_refusal_is_its_reason_and_carries_nothing_of_the_token(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(sub="subject-canary-3", aud="elsewhere")

    with pytest.raises(Unauthenticated) as refused:
        check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    error = refused.value
    assert str(error) == error.reason.value == "audience"
    assert error.args == ("audience",)
    # The library's error, which names the claim, is not chained to it.
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "canary" not in repr(error)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc.def.ghi", "abc.def.ghi"),
        ("bearer abc.def.ghi", "abc.def.ghi"),
        ("BEARER abc", "abc"),
        ("Bearer", None),
        ("Bearer ", None),
        ("Bearer a b", None),
        ("Bearer  a", None),
        ("Basic abc", None),
        ("abc", None),
        ("", None),
    ],
)
def test_the_bearer_token_is_read_from_the_header(
    header: str, expected: str | None
) -> None:
    assert bearer_token_of(header) == expected


# ── the settings ────────────────────────────────────────────────────────────
GOOD_ENV = {
    "MERIDIAN_SIGNIN_STAFF_ISSUER": "https://id.example.test/realms/staff",
    "MERIDIAN_SIGNIN_STAFF_AUDIENCE": "meridian-api",
    "MERIDIAN_SIGNIN_STAFF_KEYS_URL": "http://keycloak.identity.svc:8080/certs",
}


def test_the_settings_are_read_from_the_population_s_variables() -> None:
    settings = SigninSettings.from_env(GOOD_ENV, "staff")

    assert settings.population == "staff"
    assert settings.issuer == "https://id.example.test/realms/staff"
    assert settings.audience == "meridian-api"
    assert settings.keys_url == "http://keycloak.identity.svc:8080/certs"
    assert settings.roles_claim == "roles"


def test_the_roles_claim_is_read_when_given() -> None:
    env = {**GOOD_ENV, "MERIDIAN_SIGNIN_STAFF_ROLES_CLAIM": "groups"}

    assert SigninSettings.from_env(env, "staff").roles_claim == "groups"


def test_the_other_population_has_variables_of_its_own() -> None:
    with pytest.raises(SettingsError, match="MERIDIAN_SIGNIN_CLAIMANT_ISSUER"):
        SigninSettings.from_env(GOOD_ENV, "claimant")


@pytest.mark.parametrize("name", list(GOOD_ENV))
def test_a_missing_variable_stops_the_start_naming_it(name: str) -> None:
    env = {k: v for k, v in GOOD_ENV.items() if k != name}

    with pytest.raises(SettingsError, match=name):
        SigninSettings.from_env(env, "staff")


@pytest.mark.parametrize(
    "overrides",
    [
        {"MERIDIAN_SIGNIN_STAFF_ISSUER": "id.example.test"},
        {"MERIDIAN_SIGNIN_STAFF_ISSUER": "https://u:p@id.example.test/r"},
        {"MERIDIAN_SIGNIN_STAFF_ISSUER": "https://id.example.test/r?x=1"},
        {"MERIDIAN_SIGNIN_STAFF_KEYS_URL": "ftp://id.example.test/certs"},
        {"MERIDIAN_SIGNIN_STAFF_KEYS_URL": "https://u:canary@id.example.test/c"},
        {"MERIDIAN_SIGNIN_STAFF_AUDIENCE": "two words"},
        {"MERIDIAN_SIGNIN_STAFF_AUDIENCE": "x" * 257},
        {"MERIDIAN_SIGNIN_STAFF_ROLES_CLAIM": "with space"},
    ],
)
def test_a_bad_setting_stops_the_start_and_the_error_holds_no_value(
    overrides: dict[str, str],
) -> None:
    with pytest.raises(ValidationError) as error:
        SigninSettings.from_env({**GOOD_ENV, **overrides}, "staff")

    text = str(error.value)
    assert "canary" not in text
    for value in overrides.values():
        assert value not in text


def test_the_settings_cannot_be_changed(signin_settings: SigninSettings) -> None:
    with pytest.raises(ValidationError):
        signin_settings.audience = "another"  # type: ignore[misc]
