"""The issuer's key set: what is fetched, kept and refused (S021, T-05). The key
URL is a mock transport that counts its requests; the clock is moved by hand."""

import json
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from meridian.platform.common.signinkeys import (
    KEY_MAX_AGE_SECONDS,
    KEY_STALE_LIMIT_SECONDS,
    MAX_CACHED_KEYS,
    MAX_KEY_SET_BYTES,
    REFETCH_INTERVAL_SECONDS,
    KeySet,
    KeySetUnavailable,
    UnknownKeyId,
    key_client,
    parse_key_set,
)


def jwk_of(kid: str, private: rsa.RSAPrivateKey, **members: Any) -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256", **members}


def numbers(key: Any) -> Any:
    return key.public_numbers()


# ── the first lookup, and the cache ─────────────────────────────────────────
def test_the_first_lookup_fetches_the_set_and_returns_the_key(
    signin_issuer: Any, signin_keys: KeySet, signin_rsa_pool: list[Any]
) -> None:
    key = signin_keys.key_for("kid-a")

    assert numbers(key) == numbers(signin_rsa_pool[0].public_key())
    assert signin_issuer.calls == 1


def test_the_request_is_a_plain_get_of_the_given_url_with_no_credential(
    signin_issuer: Any, signin_keys: KeySet
) -> None:
    signin_keys.key_for("kid-a")

    (request,) = signin_issuer.requests
    assert request.method == "GET"
    assert str(request.url) == signin_issuer.url
    assert {"authorization", "cookie"}.isdisjoint(request.headers.keys())


def test_a_fresh_cached_key_is_not_fetched_again(
    signin_issuer: Any, signin_keys: KeySet, signin_clock: Any
) -> None:
    signin_keys.key_for("kid-a")
    signin_clock.advance(KEY_MAX_AGE_SECONDS - 1)

    signin_keys.key_for("kid-a")

    assert signin_issuer.calls == 1


def test_a_set_that_is_no_longer_fresh_is_fetched_again(
    signin_issuer: Any, signin_keys: KeySet, signin_clock: Any
) -> None:
    signin_keys.key_for("kid-a")
    signin_clock.advance(KEY_MAX_AGE_SECONDS)

    signin_keys.key_for("kid-a")

    assert signin_issuer.calls == 2


# ── an unknown key id: one refetch, then refused ────────────────────────────
def test_an_unknown_key_id_inside_the_interval_costs_no_fetch(
    signin_issuer: Any, signin_keys: KeySet, signin_clock: Any
) -> None:
    signin_keys.key_for("kid-a")
    signin_clock.advance(REFETCH_INTERVAL_SECONDS - 1)

    with pytest.raises(UnknownKeyId):
        signin_keys.key_for("kid-x")

    assert signin_issuer.calls == 1


def test_an_unknown_key_id_after_the_interval_costs_one_fetch_then_is_refused(
    signin_issuer: Any, signin_keys: KeySet, signin_clock: Any
) -> None:
    signin_keys.key_for("kid-a")
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)

    with pytest.raises(UnknownKeyId):
        signin_keys.key_for("kid-x")

    assert signin_issuer.calls == 2


def test_a_hundred_unknown_key_ids_make_one_fetch_per_interval(
    signin_issuer: Any, signin_keys: KeySet, signin_clock: Any
) -> None:
    signin_keys.key_for("kid-a")
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)

    for number in range(100):
        with pytest.raises(UnknownKeyId):
            signin_keys.key_for(f"kid-unknown-{number}")

    assert signin_issuer.calls == 2  # the first lookup, and one for the hundred

    signin_clock.advance(REFETCH_INTERVAL_SECONDS)
    for number in range(100):
        with pytest.raises(UnknownKeyId):
            signin_keys.key_for(f"kid-other-{number}")

    assert signin_issuer.calls == 3


def test_an_unknown_key_id_on_a_cold_cache_fetches_once_and_is_refused(
    signin_issuer: Any, signin_keys: KeySet
) -> None:
    for number in range(5):
        with pytest.raises(UnknownKeyId):
            signin_keys.key_for(f"kid-unknown-{number}")

    assert signin_issuer.calls == 1


# ── the key URL is down ─────────────────────────────────────────────────────
def test_with_the_key_url_down_and_nothing_cached_the_lookup_fails_closed(
    signin_issuer: Any, signin_keys: KeySet
) -> None:
    signin_issuer.down = True

    with pytest.raises(KeySetUnavailable):
        signin_keys.key_for("kid-a")


def test_a_down_key_url_is_asked_once_per_interval_not_once_per_lookup(
    signin_issuer: Any, signin_keys: KeySet, signin_clock: Any
) -> None:
    signin_issuer.down = True

    for _ in range(20):
        with pytest.raises(KeySetUnavailable):
            signin_keys.key_for("kid-a")
    assert signin_issuer.calls == 1

    signin_clock.advance(REFETCH_INTERVAL_SECONDS)
    signin_issuer.down = False

    assert signin_keys.key_for("kid-a") is not None
    assert signin_issuer.calls == 2


def test_with_the_key_url_down_a_fresh_cached_key_still_verifies_without_a_fetch(
    signin_issuer: Any, signin_keys: KeySet, signin_clock: Any
) -> None:
    signin_keys.key_for("kid-a")
    signin_issuer.down = True
    signin_clock.advance(KEY_MAX_AGE_SECONDS - 1)

    assert signin_keys.key_for("kid-a") is not None
    assert signin_issuer.calls == 1


def test_with_the_key_url_down_a_cached_key_past_its_age_still_verifies(
    signin_issuer: Any, signin_keys: KeySet, signin_clock: Any
) -> None:
    signin_keys.key_for("kid-a")
    signin_issuer.down = True
    signin_clock.advance(KEY_MAX_AGE_SECONDS + 1)

    assert signin_keys.key_for("kid-a") is not None
    assert signin_issuer.calls == 2  # one try, which failed


def test_with_the_key_url_down_a_key_past_the_stale_limit_is_refused(
    signin_issuer: Any, signin_keys: KeySet, signin_clock: Any
) -> None:
    signin_keys.key_for("kid-a")
    signin_issuer.down = True
    signin_clock.advance(KEY_STALE_LIMIT_SECONDS)

    with pytest.raises(KeySetUnavailable):
        signin_keys.key_for("kid-a")


def test_with_the_key_url_down_an_unknown_key_id_is_refused_as_unknown(
    signin_issuer: Any, signin_keys: KeySet, signin_clock: Any
) -> None:
    signin_keys.key_for("kid-a")
    signin_issuer.down = True
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)

    with pytest.raises(UnknownKeyId):
        signin_keys.key_for("kid-x")


# ── a restart of the issuer ─────────────────────────────────────────────────
def test_a_restarted_issuer_is_followed_after_one_refetch(
    signin_issuer: Any, signin_keys: KeySet, signin_clock: Any, signin_rsa_pool: list
) -> None:
    signin_keys.key_for("kid-a")
    signin_issuer.restart({"kid-new": signin_rsa_pool[1]})
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)

    key = signin_keys.key_for("kid-new")

    assert numbers(key) == numbers(signin_rsa_pool[1].public_key())
    assert signin_issuer.calls == 2
    with pytest.raises(UnknownKeyId):
        signin_keys.key_for("kid-a")  # the old key is gone with the old set
    assert signin_issuer.calls == 2


def test_a_restarted_issuer_is_not_followed_inside_the_interval(
    signin_issuer: Any, signin_keys: KeySet, signin_clock: Any, signin_rsa_pool: list
) -> None:
    signin_keys.key_for("kid-a")
    signin_issuer.restart({"kid-new": signin_rsa_pool[1]})
    signin_clock.advance(REFETCH_INTERVAL_SECONDS - 1)

    with pytest.raises(UnknownKeyId):
        signin_keys.key_for("kid-new")


# ── which keys count ────────────────────────────────────────────────────────
def test_only_rsa_signing_keys_of_a_safe_size_are_kept(
    signin_issuer: Any,
    signin_keys: KeySet,
    signin_clock: Any,
    signin_rsa_pool: list,
    signin_short_rsa: rsa.RSAPrivateKey,
    signin_ec_key: ec.EllipticCurvePrivateKey,
) -> None:
    elliptic = json.loads(ECAlgorithm.to_jwk(signin_ec_key.public_key()))
    private = json.loads(RSAAlgorithm.to_jwk(signin_rsa_pool[2]))
    signin_issuer.extra_jwks = [
        {**elliptic, "kid": "kid-ec", "use": "sig", "alg": "ES256"},
        jwk_of("kid-short", signin_short_rsa),
        jwk_of("kid-enc", signin_rsa_pool[1], use="enc"),
        jwk_of("kid-384", signin_rsa_pool[1], alg="RS384"),
        {**private, "kid": "kid-private", "use": "sig"},
        {**jwk_of("kid-rsa-as-ec", signin_rsa_pool[1]), "kty": "EC"},
        {**jwk_of("", signin_rsa_pool[1])},
        {**jwk_of("x", signin_rsa_pool[1]), "kid": 7},
        "not a key",
        {"kty": "RSA", "kid": "kid-empty"},
    ]

    assert signin_keys.key_for("kid-a") is not None
    for kid in (
        "kid-ec",
        "kid-short",
        "kid-enc",
        "kid-384",
        "kid-private",
        "kid-rsa-as-ec",
        "kid-empty",
        "",
    ):
        signin_clock.advance(REFETCH_INTERVAL_SECONDS)
        with pytest.raises(UnknownKeyId):
            signin_keys.key_for(kid)


def test_a_key_with_no_use_and_no_alg_member_counts(
    signin_issuer: Any, signin_keys: KeySet, signin_rsa_pool: list
) -> None:
    bare = jwk_of("kid-bare", signin_rsa_pool[1])
    del bare["use"], bare["alg"]
    signin_issuer.extra_jwks = [bare]

    assert signin_keys.key_for("kid-bare") is not None


def test_at_most_the_first_sixteen_keys_are_kept() -> None:
    private = rsa.generate_private_key(65537, 2048)
    document = {"keys": [jwk_of(f"kid-{number:02d}", private) for number in range(40)]}

    keys = parse_key_set(json.dumps(document).encode())

    assert len(keys) == MAX_CACHED_KEYS == 16
    assert "kid-15" in keys
    assert "kid-16" not in keys


def test_the_first_of_two_keys_with_one_id_wins(signin_rsa_pool: list) -> None:
    document = {
        "keys": [
            jwk_of("same", signin_rsa_pool[0]),
            jwk_of("same", signin_rsa_pool[1]),
        ]
    }

    keys = parse_key_set(json.dumps(document).encode())

    assert numbers(keys["same"]) == numbers(signin_rsa_pool[0].public_key())


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'"keys"', b'{"keys": "x"}', b'{"keys": {}}', b"{}"],
)
def test_a_document_without_a_list_of_keys_has_no_keys(body: bytes) -> None:
    assert parse_key_set(body) == {}


# ── a set that cannot be used never replaces one that can ───────────────────
@pytest.mark.parametrize(
    "answer",
    [
        {"status": 500},
        {"status": 404},
        {"status": 302},
        {"body": b"not json"},
        {"body": b'{"keys": []}'},
        {"body": b"x" * (MAX_KEY_SET_BYTES + 1)},
    ],
    ids=["500", "404", "redirect", "not-json", "no-keys", "too-large"],
)
def test_an_unusable_answer_replaces_nothing_in_a_warm_cache(
    answer: dict[str, Any],
    signin_issuer: Any,
    signin_keys: KeySet,
    signin_clock: Any,
) -> None:
    signin_keys.key_for("kid-a")
    signin_issuer.status = answer.get("status", 200)
    signin_issuer.body = answer.get("body")
    signin_clock.advance(KEY_MAX_AGE_SECONDS)

    # Warm cache: the old key serves, the bad answer replaced nothing.
    assert signin_keys.key_for("kid-a") is not None
    assert signin_issuer.calls == 2


@pytest.mark.parametrize(
    "answer",
    [
        {"status": 500},
        {"status": 302},
        {"body": b"not json"},
        {"body": b'{"keys": []}'},
        {"body": b"x" * (MAX_KEY_SET_BYTES + 1)},
    ],
    ids=["500", "redirect", "not-json", "no-keys", "too-large"],
)
def test_an_unusable_answer_on_a_cold_cache_is_unavailable(
    answer: dict[str, Any], signin_issuer: Any, signin_keys: KeySet
) -> None:
    signin_issuer.status = answer.get("status", 200)
    signin_issuer.body = answer.get("body")

    with pytest.raises(KeySetUnavailable):
        signin_keys.key_for("kid-a")


def test_a_redirect_is_not_followed(signin_rsa_pool: list) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://elsewhere.test/"})

    keys = KeySet(
        "https://id.example.test/keys",
        key_client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(KeySetUnavailable):
        keys.key_for("kid-a")

    assert seen == ["https://id.example.test/keys"]


# ── errors say nothing ──────────────────────────────────────────────────────
def test_the_errors_carry_fixed_text(signin_issuer: Any, signin_keys: KeySet) -> None:
    with pytest.raises(UnknownKeyId) as unknown:
        signin_keys.key_for("kid-canary-marker")
    signin_issuer.down = True
    cold = KeySet(
        signin_issuer.url, key_client(transport=signin_issuer.transport), lambda: 0.0
    )
    with pytest.raises(KeySetUnavailable) as unavailable:
        cold.key_for("kid-canary-marker")

    for error in (unknown.value, unavailable.value):
        assert "canary" not in str(error) + repr(error)
        assert signin_issuer.url not in str(error) + repr(error)
