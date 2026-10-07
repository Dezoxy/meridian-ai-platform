"""The bearer check (S021, T-05): what a token must be to name a person. Keys are
made in the test; the key URL is a mock transport; the time is given."""

from collections.abc import Callable
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

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
    Unavailable,
    check_bearer,
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
        via="bearer",
    )
    assert principal.has_role("auditor")
    assert not principal.has_role("platform-admin")


def test_the_principal_does_not_show_its_subject_or_roles(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(sub="subject-canary-9", roles=["role-canary-9"])

    principal = check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    assert "canary" not in repr(principal) + str(principal)


def test_the_algorithms_are_exactly_rs256_and_cannot_be_changed() -> None:
    assert ALGORITHMS == ("RS256",)
    assert isinstance(ALGORITHMS, tuple)


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
# (a claim that is missing, ``nbf`` and ``iat``: test_signin_claims.py)
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


def test_with_the_key_url_down_and_nothing_cached_a_good_token_is_unavailable_not_401(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint()
    signin_issuer.down = True

    with pytest.raises(Unavailable) as refused:
        check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    assert refused.value.reason is Reason.KEYS_UNAVAILABLE
    assert Unavailable.status == 503
    assert not isinstance(refused.value, Unauthenticated)


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
    claim: object,
    expected: set[str],
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
) -> None:
    token = signin_issuer.mint(roles=claim)

    principal = check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    assert principal.roles == frozenset(expected)


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


# (the bearer header's parsing is in test_signin_guard.py; the settings are in
# test_signin_claims.py)
