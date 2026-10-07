"""The claims of a token and the settings that judge them (S021, T-05): which
claims are required and in what reasons their absence ends, ``nbf`` and ``iat``,
the kind of token (``typ`` and ``azp``), the subject claim, the 503, what
``check_bearer`` may raise and the settings' variables. Keys are made in the
test; the key URL is a mock transport; the time is given."""

import base64
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from pydantic import ValidationError

from meridian.platform.common.env import SettingsError
from meridian.platform.common.signin import (
    ALGORITHMS,
    LEEWAY_SECONDS,
    REQUIRED_CLAIMS,
    Principal,
    Reason,
    SigninRefusal,
    SigninSettings,
    Unauthenticated,
    Unavailable,
    check_bearer,
)
from meridian.platform.common.signinkeys import KeySet

ENV_PREFIX = "MERIDIAN_SIGNIN_STAFF_"


def refusal_of(
    token: str, settings: SigninSettings, keys: KeySet, now: float
) -> Reason:
    with pytest.raises(Unauthenticated) as refused:
        check_bearer(token, settings, keys, now)
    return refused.value.reason


def settings_for(issuer: Any, **extra: Any) -> SigninSettings:
    return SigninSettings(
        population="staff",
        issuer=issuer.name,
        audience=issuer.audience,
        keys_url=issuer.url,
        **extra,
    )


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def raw_token(issuer: Any, header: bytes, payload: bytes) -> str:
    """A token signed by the issuer's key over the bytes given, which need not be
    anything ``json.dumps`` can make (a 4,301-digit number, a deep header)."""
    head, body = b64url(header), b64url(payload)
    private = next(iter(issuer.keys.values()))
    signature = private.sign(
        f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    return f"{head}.{body}.{b64url(signature)}"


# ── the required claims, one reason each ────────────────────────────────────
def test_the_required_claims_are_a_tuple_of_exp_iss_aud() -> None:
    assert REQUIRED_CLAIMS == ("exp", "iss", "aud")
    assert isinstance(REQUIRED_CLAIMS, tuple)
    assert isinstance(ALGORITHMS, tuple)


@pytest.mark.parametrize("claim", ["sub", "exp", "iss", "aud"])
def test_a_missing_required_claim_is_refused_as_claims(
    claim: str,
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
) -> None:
    token = signin_issuer.mint(drop=(claim,))

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.CLAIMS


def test_a_token_without_nbf_is_accepted(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(drop=("nbf",))

    principal = check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    assert principal.subject == "subject-1"


@pytest.mark.parametrize("claim", ["nbf", "iat"])
@pytest.mark.parametrize("value", [None, "now", True, float("nan"), [1]])
def test_a_time_claim_that_is_present_and_not_a_number_is_refused(
    claim: str,
    value: object,
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
) -> None:
    token = signin_issuer.mint(**{claim: value})

    reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)

    assert reason is Reason.CLAIMS


# ── iat: not from the future ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("iat_offset", "accepted"),
    [
        (-10, True),
        (0, True),
        (LEEWAY_SECONDS, True),
        (LEEWAY_SECONDS + 1, False),
        (86400, False),
    ],
)
def test_an_issue_time_ahead_of_the_leeway_is_refused(
    iat_offset: int,
    accepted: bool,
    signin_issuer: Any,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
) -> None:
    token = signin_issuer.mint(iat=signin_issuer.now + iat_offset)

    if accepted:
        check_bearer(token, signin_settings, signin_keys, signin_issuer.now)
    else:
        reason = refusal_of(token, signin_settings, signin_keys, signin_issuer.now)
        assert reason is Reason.NOT_YET_VALID


def test_a_token_without_iat_is_accepted(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(drop=("iat",))

    check_bearer(token, signin_settings, signin_keys, signin_issuer.now)


# ── nothing escapes as a 500 ────────────────────────────────────────────────
def payload_with(issuer: Any, exp: str) -> bytes:
    return (
        f'{{"iss":"{issuer.name}","aud":"{issuer.audience}","sub":"s","exp":{exp}}}'
    ).encode()


def hostile_tokens(issuer: Any) -> dict[str, str]:
    header = b'{"alg":"RS256","kid":"kid-a"}'
    depth = 2500  # as deep as the token's size bound allows
    nested = b'{"alg":"RS256","kid":"kid-a","x":' + b"[" * depth + b"]" * depth + b"}"
    return {
        "exp-of-400-digits": raw_token(
            issuer, header, payload_with(issuer, "1" + "0" * 399)
        ),
        "exp-of-4301-digits": raw_token(
            issuer, header, payload_with(issuer, "1" + "0" * 4300)
        ),
        "nbf-absent": issuer.mint(drop=("nbf",)),
        "iat-of-400-digits": raw_token(
            issuer,
            header,
            payload_with(issuer, "9999999999").replace(
                b'"sub"', b'"iat":1' + b"0" * 399 + b',"sub"'
            ),
        ),
        "header-deeply-nested": raw_token(
            issuer, nested, payload_with(issuer, "9999999999")
        ),
    }


def test_check_bearer_returns_a_principal_or_raises_its_own_classes_only(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    tokens = hostile_tokens(signin_issuer)
    outcomes: dict[str, str] = {}

    for name, token in tokens.items():
        try:
            result = check_bearer(
                token, signin_settings, signin_keys, signin_issuer.now
            )
        except (Unauthenticated, Unavailable) as refusal:
            outcomes[name] = f"refused: {refusal.reason.value}"
        else:
            outcomes[name] = f"accepted: {type(result).__name__}"

    assert outcomes["nbf-absent"] == "accepted: Principal"
    # An integer too large for a float is a malformed claim, not an OverflowError.
    assert outcomes["exp-of-400-digits"] == "refused: claims"
    assert outcomes["iat-of-400-digits"] == "refused: claims"
    # A number over the interpreter's digit limit is a parse error of the library.
    assert outcomes["exp-of-4301-digits"] == "refused: malformed"
    # A header nested as deep as the size bound allows is read (Python's JSON
    # reader copes with it), and the token is judged like any other.
    assert outcomes["header-deeply-nested"] == "accepted: Principal"


# ── the kind of token ───────────────────────────────────────────────────────
KIND = {"required_typ": "Access", "allowed_azp": ("web-client", "script-client")}


def test_the_defaults_enforce_no_kind(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    assert signin_settings.required_typ == ""
    assert signin_settings.allowed_azp == ()
    token = signin_issuer.mint(typ="ID", azp="anything", nonce="n", at_hash="h")

    check_bearer(token, signin_settings, signin_keys, signin_issuer.now)


def test_a_token_of_the_named_kind_is_accepted(
    signin_issuer: Any, signin_keys: KeySet
) -> None:
    settings = settings_for(signin_issuer, **KIND)
    token = signin_issuer.mint(typ="Access", azp="script-client")

    principal = check_bearer(token, settings, signin_keys, signin_issuer.now)

    assert principal.subject == "subject-1"


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        pytest.param(
            {"typ": "ID", "azp": "web-client", "nonce": "n", "at_hash": "h"},
            Reason.WRONG_TYPE,
            id="id-token",
        ),
        pytest.param(
            {"typ": "Refresh", "azp": "web-client"},
            Reason.WRONG_TYPE,
            id="refresh-token",
        ),
        pytest.param({"azp": "web-client"}, Reason.WRONG_TYPE, id="no-typ"),
        pytest.param(
            {"typ": ["Access"], "azp": "web-client"}, Reason.WRONG_TYPE, id="typ-list"
        ),
        pytest.param(
            {"typ": "Access", "azp": "another-client"},
            Reason.AUTHORIZED_PARTY,
            id="wrong-azp",
        ),
        pytest.param({"typ": "Access"}, Reason.AUTHORIZED_PARTY, id="no-azp"),
        pytest.param(
            {"typ": "Access", "azp": ["web-client"]},
            Reason.AUTHORIZED_PARTY,
            id="azp-list",
        ),
    ],
)
def test_a_token_of_another_kind_is_refused_with_a_reason_of_its_own(
    claims: dict[str, Any], reason: Reason, signin_issuer: Any, signin_keys: KeySet
) -> None:
    settings = settings_for(signin_issuer, **KIND)
    token = signin_issuer.mint(**claims)

    assert refusal_of(token, settings, signin_keys, signin_issuer.now) is reason


def test_each_kind_setting_is_enforced_without_the_other(
    signin_issuer: Any, signin_keys: KeySet
) -> None:
    typ_only = settings_for(signin_issuer, required_typ="Access")
    azp_only = settings_for(signin_issuer, allowed_azp=("web-client",))

    now = signin_issuer.now
    wrong_typ = refusal_of(signin_issuer.mint(typ="ID"), typ_only, signin_keys, now)
    wrong_azp = refusal_of(signin_issuer.mint(azp="x"), azp_only, signin_keys, now)

    assert wrong_typ is Reason.WRONG_TYPE
    assert wrong_azp is Reason.AUTHORIZED_PARTY
    check_bearer(signin_issuer.mint(typ="Access"), typ_only, signin_keys, now)
    check_bearer(signin_issuer.mint(azp="web-client"), azp_only, signin_keys, now)


# ── the subject claim ───────────────────────────────────────────────────────
def test_the_subject_claim_is_a_setting_and_sub_by_default(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint(sub="the-sub", oid="the-oid")

    oid = settings_for(signin_issuer, subject_claim="oid")
    by_default = check_bearer(token, signin_settings, signin_keys, signin_issuer.now)
    by_oid = check_bearer(token, oid, signin_keys, signin_issuer.now)

    assert (by_default.subject, by_oid.subject) == ("the-sub", "the-oid")


def test_the_named_subject_claim_is_required_and_sub_is_not_then(
    signin_issuer: Any, signin_keys: KeySet
) -> None:
    settings = settings_for(signin_issuer, subject_claim="oid")

    missing = signin_issuer.mint(sub="the-sub")  # no oid
    only_oid = signin_issuer.mint(drop=("sub",), oid="the-oid")

    now = signin_issuer.now
    assert refusal_of(missing, settings, signin_keys, now) is Reason.CLAIMS
    assert check_bearer(only_oid, settings, signin_keys, now).subject == "the-oid"


# ── what the principal records ──────────────────────────────────────────────
def test_the_principal_carries_the_verified_issuer_and_says_bearer(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    token = signin_issuer.mint()

    principal = check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    assert principal.issuer == signin_issuer.name
    assert principal.via == "bearer"
    assert isinstance(principal, Principal)


# ── the 503 ─────────────────────────────────────────────────────────────────
def test_an_unknown_key_id_stays_a_401_class_and_only_unavailable_keys_are_a_503(
    signin_issuer: Any, signin_settings: SigninSettings, signin_keys: KeySet
) -> None:
    unknown = refusal_of(
        signin_issuer.mint(kid="kid-x"), signin_settings, signin_keys, signin_issuer.now
    )

    assert unknown is Reason.UNKNOWN_KEY
    assert Unauthenticated.status == 401
    assert Unavailable.status == 503
    assert issubclass(Unavailable, SigninRefusal)
    assert not issubclass(Unavailable, Unauthenticated)


# ── the settings ────────────────────────────────────────────────────────────
GOOD_ENV = {
    ENV_PREFIX + "ISSUER": "https://id.example.test/realms/staff",
    ENV_PREFIX + "AUDIENCE": "meridian-api",
    ENV_PREFIX + "KEYS_URL": "https://id.example.test/realms/staff/certs",
}
KIND_ENV = {
    **GOOD_ENV,
    ENV_PREFIX + "KEYS_URL": "http://keycloak.identity.svc:8080/certs",
    "MERIDIAN_ENVIRONMENT": "kind",
}


def test_the_settings_are_read_from_the_population_s_variables() -> None:
    settings = SigninSettings.from_env(GOOD_ENV, "staff")

    assert settings.population == "staff"
    assert settings.issuer == "https://id.example.test/realms/staff"
    assert settings.audience == "meridian-api"
    assert settings.keys_url == "https://id.example.test/realms/staff/certs"
    assert settings.roles_claim == "roles"
    assert settings.subject_claim == "sub"
    assert settings.required_typ == ""
    assert settings.allowed_azp == ()


def test_the_optional_settings_are_read_when_given() -> None:
    env = {
        **GOOD_ENV,
        ENV_PREFIX + "ROLES_CLAIM": "groups",
        ENV_PREFIX + "SUBJECT_CLAIM": "oid",
        ENV_PREFIX + "REQUIRED_TYP": "Access",
        ENV_PREFIX + "ALLOWED_AZP": "web-client, script-client",
    }

    settings = SigninSettings.from_env(env, "staff")

    assert settings.roles_claim == "groups"
    assert settings.subject_claim == "oid"
    assert settings.required_typ == "Access"
    assert settings.allowed_azp == ("web-client", "script-client")


def test_an_empty_optional_variable_is_the_default() -> None:
    env = {**GOOD_ENV, ENV_PREFIX + "REQUIRED_TYP": "", ENV_PREFIX + "ALLOWED_AZP": ""}

    settings = SigninSettings.from_env(env, "staff")

    assert (settings.required_typ, settings.allowed_azp) == ("", ())


def test_the_other_population_has_variables_of_its_own() -> None:
    with pytest.raises(SettingsError, match="MERIDIAN_SIGNIN_CLAIMANT_ISSUER"):
        SigninSettings.from_env(GOOD_ENV, "claimant")


@pytest.mark.parametrize("name", list(GOOD_ENV))
def test_a_missing_variable_stops_the_start_naming_it(name: str) -> None:
    env = {k: v for k, v in GOOD_ENV.items() if k != name}

    with pytest.raises(SettingsError, match=name):
        SigninSettings.from_env(env, "staff")


# A key URL over plain HTTP: only on kind.
def test_a_plain_http_key_url_is_accepted_only_on_kind() -> None:
    assert SigninSettings.from_env(KIND_ENV, "staff").keys_url.startswith("http://")
    plain = {k: v for k, v in KIND_ENV.items() if k != "MERIDIAN_ENVIRONMENT"}
    for environment in ("azure", "local", "test", "ci", "Kind", "kind ", ""):
        env = {**plain, "MERIDIAN_ENVIRONMENT": environment}
        with pytest.raises(SettingsError, match=ENV_PREFIX + "KEYS_URL"):
            SigninSettings.from_env(env, "staff")
    with pytest.raises(SettingsError, match=ENV_PREFIX + "KEYS_URL"):
        SigninSettings.from_env(plain, "staff")


def test_an_https_key_url_is_accepted_in_every_environment() -> None:
    for environment in ("azure", "kind", "local"):
        env = {**GOOD_ENV, "MERIDIAN_ENVIRONMENT": environment}
        assert SigninSettings.from_env(env, "staff").keys_url.startswith("https://")


CANARY = "pw-CANARY-8d41"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("ISSUER", "id.example.test"),
        ("ISSUER", f"https://u:{CANARY}@id.example.test/r"),
        ("ISSUER", f"https://id.example.test/r?x={CANARY}"),
        ("KEYS_URL", "ftp://id.example.test/certs"),
        ("KEYS_URL", f"https://u:{CANARY}@id.example.test/c"),
        ("KEYS_URL", f"https://id.example.test/c#{CANARY}"),
        ("AUDIENCE", f"two {CANARY}"),
        ("AUDIENCE", CANARY + "x" * 257),
        ("ROLES_CLAIM", f"with {CANARY}"),
        ("SUBJECT_CLAIM", f"with {CANARY}"),
        ("REQUIRED_TYP", f"with {CANARY}"),
        ("ALLOWED_AZP", f"a,{CANARY} b"),
        ("ALLOWED_AZP", f"{CANARY},,b"),
        ("ALLOWED_AZP", ",".join([CANARY] * 17)),
    ],
)
def test_a_bad_setting_stops_the_start_naming_the_variable_and_holding_no_value(
    variable: str, value: str
) -> None:
    with pytest.raises(SettingsError) as raised:
        SigninSettings.from_env({**GOOD_ENV, ENV_PREFIX + variable: value}, "staff")

    error = raised.value
    assert ENV_PREFIX + variable in str(error)
    # Not in the text, the representation, the arguments, the attributes or the
    # errors it would chain: pydantic's data holds the rejected input.
    for place in (str(error), repr(error), str(error.args), repr(vars(error))):
        assert CANARY not in place
        assert value not in place
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not isinstance(error, ValidationError)


def test_the_settings_cannot_be_changed(signin_settings: SigninSettings) -> None:
    with pytest.raises(ValidationError):
        signin_settings.audience = "another"  # type: ignore[misc]
