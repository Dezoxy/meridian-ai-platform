"""The ID token's own check at the callback (S021, Y3): a check of its own, not
the access token's. Every case goes through the whole flow: a start, a code
exchange against the fake token endpoint, and the ID token the fake hands back.
What a good token looks like is ``FlowKit.claims`` (Keycloak's, section 4.3 of
``keycloak-tokens.md``)."""

import hmac as hmac_module
import secrets
from collections.abc import Callable
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from signinflowsupport import FRONT_ISSUER, NOW, FlowKit, flow_settings
from starlette.responses import Response

from meridian.platform.common.signin import (
    LEEWAY_SECONDS,
    MAX_ROLE_CHARS,
    MAX_ROLES,
    MAX_SUBJECT_CHARS,
    MAX_TOKEN_CHARS,
)
from meridian.platform.common.signinflow import SigninFlow, SigninOutcome
from meridian.platform.common.signinsession import open_session
from meridian.platform.common.signinstate import FlowReason

LATER = NOW + 5


def run(kit: FlowKit, **changes: Any) -> SigninOutcome:
    """A whole sign-in whose ID token has these changes."""
    started = kit.begin()
    kit.arm(started, **changes)
    return kit.flow.finish(kit.callback(started), started.cookie, LATER)


def session_of(kit: FlowKit, outcome: SigninOutcome) -> Any:
    response = Response()
    outcome.apply_to(response)
    line = next(
        line
        for line in response.headers.getlist("set-cookie")
        if line.startswith("meridian_session_staff=")
    )
    value = line.split(";", 1)[0].split("=", 1)[1]
    return open_session(value, kit.session_keys, LATER, "staff")


def assert_refused(kit: FlowKit, outcome: SigninOutcome, reason: FlowReason) -> None:
    assert not outcome.signed_in
    assert outcome.reason is reason
    response = Response()
    outcome.apply_to(response)
    assert not any(
        line.startswith("meridian_session_staff=")
        for line in response.headers.getlist("set-cookie")
    )
    assert any(
        line.startswith("meridian_signin_staff=")
        for line in response.headers.getlist("set-cookie")
    )
    assert kit.endpoint.calls >= 1  # the refusal came after the exchange


def test_the_unchanged_token_is_the_good_one(flow_kit: FlowKit) -> None:
    assert run(flow_kit).signed_in


# ── issuer and audience ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "issuer",
    [
        FRONT_ISSUER + "/",
        FRONT_ISSUER.upper(),
        "http://keycloak.identity.svc:8080/realms/meridian-staff",
        "http://id.meridian.localhost:8088/realms/meridian-claimants",
        "",
    ],
)
def test_a_wrong_issuer_is_refused(flow_kit: FlowKit, issuer: str) -> None:
    outcome = run(flow_kit, iss=issuer)

    assert_refused(flow_kit, outcome, FlowReason.ID_ISSUER)


@pytest.mark.parametrize("issuer", [None, [FRONT_ISSUER], 42, {"a": 1}])
def test_an_issuer_that_is_not_text_is_refused(
    flow_kit: FlowKit, signin_forge: Callable[..., str], issuer: object
) -> None:
    # PyJWT will not encode such a claim, so the token is signed by hand.
    started = flow_kit.begin()
    claims = flow_kit.claims(started, iss=issuer)
    forged = signin_forge(
        {"alg": "RS256", "typ": "JWT", "kid": "kid-a"},
        claims,
        lambda data: flow_kit.signer.sign(data, padding.PKCS1v15(), hashes.SHA256()),
    )
    flow_kit.endpoint.body = {"id_token": forged}

    outcome = flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)

    assert_refused(flow_kit, outcome, FlowReason.ID_ISSUER)


@pytest.mark.parametrize(
    "audience",
    [
        "meridian-claims-api",
        "meridian-claims-web2",
        "",
        ["meridian-claims-api"],
        ["other", "meridian-claims-web"],
        ["meridian-claims-web", "other"],
        ["meridian-claims-web", "meridian-claims-web"],
        [],
        42,
        {"a": "meridian-claims-web"},
    ],
)
def test_an_audience_other_than_exactly_the_client_is_refused(
    flow_kit: FlowKit, audience: object
) -> None:
    outcome = run(flow_kit, aud=audience)

    assert_refused(flow_kit, outcome, FlowReason.ID_AUDIENCE)


def test_an_audience_that_is_the_client_alone_in_a_list_is_accepted(
    flow_kit: FlowKit,
) -> None:
    assert run(flow_kit, aud=["meridian-claims-web"]).signed_in


def test_a_missing_audience_is_refused(flow_kit: FlowKit) -> None:
    outcome = run(flow_kit, drop=("aud",))

    assert_refused(flow_kit, outcome, FlowReason.ID_AUDIENCE)


def test_a_missing_issuer_is_refused(flow_kit: FlowKit) -> None:
    outcome = run(flow_kit, drop=("iss",))

    assert_refused(flow_kit, outcome, FlowReason.ID_ISSUER)


@pytest.mark.parametrize(
    "azp", ["meridian-scripts", "", None, ["meridian-claims-web"], 42]
)
def test_an_authorized_party_other_than_the_client_is_refused(
    flow_kit: FlowKit, azp: object
) -> None:
    outcome = run(flow_kit, azp=azp)

    assert_refused(flow_kit, outcome, FlowReason.ID_AUTHORIZED_PARTY)


def test_an_absent_authorized_party_is_accepted(flow_kit: FlowKit) -> None:
    assert run(flow_kit, drop=("azp",)).signed_in


# ── the kind of token ───────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", ["Bearer", "Refresh", "Offline", "id", "", None, 1])
def test_a_token_that_is_not_an_id_token_by_its_type_is_refused(
    flow_kit: FlowKit, kind: object
) -> None:
    outcome = run(flow_kit, typ=kind)

    assert_refused(flow_kit, outcome, FlowReason.ID_TYPE)


def test_a_token_with_no_type_is_refused(flow_kit: FlowKit) -> None:
    assert_refused(flow_kit, run(flow_kit, drop=("typ",)), FlowReason.ID_TYPE)


def test_an_issuer_that_sends_no_type_is_served_when_the_settings_say_so(
    flow_kit: FlowKit,
) -> None:
    settings = flow_settings(
        required_typ="", client_credential=flow_kit.endpoint.credential
    )
    flow = SigninFlow(
        settings,
        flow_kit.sessions,
        flow_kit.keys,
        transport=flow_kit.endpoint.transport,
    )
    started = flow_kit.begin()
    flow_kit.arm(started, drop=("typ",))

    outcome = flow.finish(flow_kit.callback(started), started.cookie, LATER)

    assert outcome.signed_in


# ── the nonce ───────────────────────────────────────────────────────────────
def test_a_token_without_the_nonce_is_refused(flow_kit: FlowKit) -> None:
    assert_refused(flow_kit, run(flow_kit, drop=("nonce",)), FlowReason.ID_NONCE)


@pytest.mark.parametrize(
    "nonce",
    # The last is a lone surrogate, which JSON can carry and UTF-8 cannot
    # encode: a refusal, not an exception (the security review of Y3).
    ["wrong", "", None, 42, ["x"], "a" * 500, "É" * 43, "\ud800abc"],
)
def test_a_wrong_nonce_is_refused(flow_kit: FlowKit, nonce: object) -> None:
    assert_refused(flow_kit, run(flow_kit, nonce=nonce), FlowReason.ID_NONCE)


def test_the_nonce_of_another_sign_in_is_refused(flow_kit: FlowKit) -> None:
    other = flow_kit.begin()
    started = flow_kit.begin()
    flow_kit.arm(started, nonce=other.nonce)

    outcome = flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)

    assert_refused(flow_kit, outcome, FlowReason.ID_NONCE)


def test_the_nonce_is_compared_in_constant_time(
    flow_kit: FlowKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meridian.platform.common import signinidtoken

    seen: list[Any] = []
    real = hmac_module.compare_digest

    def spy(a: Any, b: Any) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(signinidtoken.hmac, "compare_digest", spy)
    started = flow_kit.begin()
    flow_kit.arm(started)

    flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)

    assert (started.nonce.encode(), started.nonce.encode()) in seen


# ── time ────────────────────────────────────────────────────────────────────
def test_a_token_is_good_until_its_expiry_and_not_at_it(flow_kit: FlowKit) -> None:
    now = int(LATER)

    assert run(flow_kit, exp=now + 1).signed_in
    assert_refused(flow_kit, run(flow_kit, exp=now), FlowReason.ID_EXPIRED)
    assert_refused(flow_kit, run(flow_kit, exp=now - 1), FlowReason.ID_EXPIRED)
    assert_refused(flow_kit, run(flow_kit, exp=now - 3600), FlowReason.ID_EXPIRED)


def test_an_expiry_inside_the_second_that_cannot_make_a_session_is_refused(
    flow_kit: FlowKit,
) -> None:
    # 5.5 s after the start with exp at 5.2: not yet expired by the float, but
    # the session would end at the whole second, which has passed.
    started = flow_kit.begin()
    flow_kit.arm(started, exp=NOW + 5.2)

    outcome = flow_kit.flow.finish(
        flow_kit.callback(started), started.cookie, NOW + 5.1
    )

    assert_refused(flow_kit, outcome, FlowReason.ID_EXPIRED)


@pytest.mark.parametrize("name", ["exp", "iat", "nbf"])
@pytest.mark.parametrize(
    "value", ["soon", True, None, [1], float("nan"), float("inf"), 10**400]
)
def test_a_time_claim_that_is_not_a_number_is_refused(
    flow_kit: FlowKit, name: str, value: object
) -> None:
    outcome = run(flow_kit, **{name: value})

    assert not outcome.signed_in
    assert outcome.reason is FlowReason.ID_CLAIMS
    assert flow_kit.endpoint.calls == 1


@pytest.mark.parametrize("name", ["exp", "iat"])
def test_a_missing_expiry_or_issue_time_is_refused(
    flow_kit: FlowKit, name: str
) -> None:
    assert_refused(flow_kit, run(flow_kit, drop=(name,)), FlowReason.ID_CLAIMS)


def test_an_issue_time_ahead_by_more_than_the_skew_is_refused(
    flow_kit: FlowKit,
) -> None:
    now = int(LATER)

    assert run(flow_kit, iat=now + LEEWAY_SECONDS).signed_in
    assert_refused(
        flow_kit,
        run(flow_kit, iat=now + LEEWAY_SECONDS + 1),
        FlowReason.ID_NOT_YET_VALID,
    )


def test_a_not_before_in_the_future_is_refused_and_an_absent_one_is_not(
    flow_kit: FlowKit,
) -> None:
    now = int(LATER)

    assert run(flow_kit, nbf=now + LEEWAY_SECONDS).signed_in
    assert_refused(
        flow_kit,
        run(flow_kit, nbf=now + LEEWAY_SECONDS + 1),
        FlowReason.ID_NOT_YET_VALID,
    )
    assert run(flow_kit, drop=("nbf",)).signed_in  # Keycloak sends none


# ── the signature, the algorithm, the key ───────────────────────────────────
def test_a_token_with_no_signature_algorithm_is_refused(
    flow_kit: FlowKit, signin_forge: Callable[..., str]
) -> None:
    started = flow_kit.begin()
    payload = flow_kit.claims(started)
    forged = signin_forge({"alg": "none", "typ": "JWT", "kid": "kid-a"}, payload)
    flow_kit.endpoint.body = {"id_token": forged}

    outcome = flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)

    assert_refused(flow_kit, outcome, FlowReason.ID_ALGORITHM)


@pytest.mark.parametrize(
    "alg", ["none", "None", "HS256", "HS512", "ES256", "PS256", ""]
)
def test_a_token_whose_header_names_another_algorithm_is_refused(
    flow_kit: FlowKit, signin_forge: Callable[..., str], alg: str
) -> None:
    started = flow_kit.begin()
    forged = signin_forge(
        {"alg": alg, "typ": "JWT", "kid": "kid-a"},
        flow_kit.claims(started),
        lambda _: b"x",
    )
    flow_kit.endpoint.body = {"id_token": forged}

    outcome = flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)

    assert_refused(flow_kit, outcome, FlowReason.ID_ALGORITHM)


def test_a_token_signed_with_hmac_keyed_by_the_public_key_is_refused(
    flow_kit: FlowKit,
    signin_forge: Callable[..., str],
    signin_hmac_with_public_key: Callable[[bytes], bytes],
) -> None:
    started = flow_kit.begin()
    forged = signin_forge(
        {"alg": "HS256", "typ": "JWT", "kid": "kid-a"},
        flow_kit.claims(started),
        signin_hmac_with_public_key,
    )
    flow_kit.endpoint.body = {"id_token": forged}

    outcome = flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)

    assert_refused(flow_kit, outcome, FlowReason.ID_ALGORITHM)


def test_a_token_signed_with_a_key_the_set_does_not_hold_is_refused(
    flow_kit: FlowKit,
) -> None:
    stranger = rsa.generate_private_key(65537, 2048)

    outcome = run(flow_kit, key=stranger)

    assert_refused(flow_kit, outcome, FlowReason.ID_SIGNATURE)


def test_a_key_id_the_set_does_not_hold_is_refused(flow_kit: FlowKit) -> None:
    assert_refused(flow_kit, run(flow_kit, kid="kid-zzz"), FlowReason.ID_UNKNOWN_KEY)


def test_a_token_with_no_key_id_is_refused(flow_kit: FlowKit) -> None:
    assert_refused(flow_kit, run(flow_kit, kid=None), FlowReason.ID_NO_KEY_ID)


def test_a_key_set_that_cannot_be_had_is_an_outage_and_a_refusal(
    flow_kit: FlowKit, signin_issuer: Any
) -> None:
    signin_issuer.down = True

    outcome = run(flow_kit)

    assert_refused(flow_kit, outcome, FlowReason.ID_KEYS_UNAVAILABLE)


def test_a_token_with_an_altered_signature_is_refused(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin()
    flow_kit.arm(started)
    token = flow_kit.endpoint.body["id_token"]
    header, payload, signature = token.split(".")
    flow_kit.endpoint.body = {"id_token": f"{header}.{payload}.{signature[:-3]}AAA"}

    outcome = flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)

    assert_refused(flow_kit, outcome, FlowReason.ID_SIGNATURE)


@pytest.mark.parametrize(
    "token",
    ["abc", "a.b", "a.b.c.d", "a b.c.d", "...", "..", "é.é.é", "a.b.c\n"],
)
def test_text_that_is_not_a_compact_token_is_refused(
    flow_kit: FlowKit, token: str
) -> None:
    started = flow_kit.begin()
    flow_kit.endpoint.body = {"id_token": token}

    outcome = flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)

    assert_refused(flow_kit, outcome, FlowReason.ID_MALFORMED)


def test_a_token_over_the_size_bound_is_refused_before_it_is_read(
    flow_kit: FlowKit,
) -> None:
    started = flow_kit.begin()
    flow_kit.endpoint.body = {"id_token": "a." * (MAX_TOKEN_CHARS // 2 + 1) + "a"}

    outcome = flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)

    assert_refused(flow_kit, outcome, FlowReason.ID_TOO_LARGE)


# ── what the session is made of ─────────────────────────────────────────────
def test_the_principal_takes_the_pinned_issuer_and_the_tokens_subject_and_roles(
    flow_kit: FlowKit,
) -> None:
    outcome = run(flow_kit, roles=["adjuster", "auditor"], sub="user-1")

    principal = session_of(flow_kit, outcome)

    assert principal.issuer == FRONT_ISSUER
    assert principal.subject == "user-1"
    assert principal.roles == frozenset({"adjuster", "auditor"})


@pytest.mark.parametrize(
    "roles", ["adjuster", {"adjuster": True}, 5, None, [1], ["a", 2], ["x" * 65], [""]]
)
def test_roles_that_are_not_a_list_of_short_strings_are_no_roles(
    flow_kit: FlowKit, roles: object
) -> None:
    principal = session_of(flow_kit, run(flow_kit, roles=roles))

    assert principal.roles == frozenset()


def test_a_missing_roles_claim_is_no_roles_and_a_sign_in(flow_kit: FlowKit) -> None:
    outcome = run(flow_kit, drop=("roles",))

    assert outcome.signed_in
    assert session_of(flow_kit, outcome).roles == frozenset()


def test_more_roles_than_the_bound_are_no_roles(flow_kit: FlowKit) -> None:
    roles = [f"role-{i}" for i in range(MAX_ROLES + 1)]

    assert session_of(flow_kit, run(flow_kit, roles=roles)).roles == frozenset()
    fits = roles[:MAX_ROLES]
    assert session_of(flow_kit, run(flow_kit, roles=fits)).roles == frozenset(fits)


def test_the_roles_claim_is_a_setting(flow_kit: FlowKit) -> None:
    settings = flow_settings(
        roles_claim="groups", client_credential=flow_kit.endpoint.credential
    )
    flow = SigninFlow(
        settings,
        flow_kit.sessions,
        flow_kit.keys,
        transport=flow_kit.endpoint.transport,
    )
    started = flow_kit.begin()
    flow_kit.arm(started, groups=["auditor"], roles=["adjuster"])

    outcome = flow.finish(flow_kit.callback(started), started.cookie, LATER)

    assert session_of(flow_kit, outcome).roles == frozenset({"auditor"})


@pytest.mark.parametrize(
    "subject", [None, "", 42, ["s"], "x" * (MAX_SUBJECT_CHARS + 1)]
)
def test_a_subject_that_is_missing_empty_or_too_long_is_refused(
    flow_kit: FlowKit, subject: object
) -> None:
    outcome = run(flow_kit, sub=subject)

    assert not outcome.signed_in
    assert outcome.reason is FlowReason.ID_CLAIMS


def test_the_longest_subject_is_accepted(flow_kit: FlowKit) -> None:
    assert run(flow_kit, sub="x" * MAX_SUBJECT_CHARS).signed_in


def test_a_principal_too_large_for_a_cookie_is_a_refusal_not_an_error(
    flow_kit: FlowKit,
) -> None:
    # Each role is within its bound and the token is within its own (8 KB), but
    # a non-ASCII character is six bytes in the cookie's JSON, so the cookie is
    # over the browser's limit.
    roles = [f"{i:02d}" + "é" * (MAX_ROLE_CHARS - 2) for i in range(12)]

    outcome = run(flow_kit, roles=roles)

    assert_refused(flow_kit, outcome, FlowReason.SESSION_TOO_LARGE)


def test_a_lot_of_ascii_roles_still_fit(flow_kit: FlowKit) -> None:
    roles = [f"{i:02d}" + "r" * (MAX_ROLE_CHARS - 2) for i in range(MAX_ROLES)]

    assert run(flow_kit, roles=roles).signed_in


def test_no_other_claim_reaches_the_session(flow_kit: FlowKit) -> None:
    marker = "FLOWMARK-email-" + secrets.token_hex(4)
    outcome = run(flow_kit, email=marker, name=marker, preferred_username=marker)
    response = Response()
    outcome.apply_to(response)

    assert marker not in " ".join(response.headers.getlist("set-cookie"))
    assert marker not in repr(outcome)
