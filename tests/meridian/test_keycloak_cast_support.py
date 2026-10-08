"""What the rig asserts about the cast, run here without a container (S021, Y4b).

The rig (``test_keycloak_rig.py``, opt-in, needs Docker) signs in every person of
the cast through ``observe_cast`` and judges the record with
``assert_the_cast_holds``, both in ``keycloakcastsupport.py``. The review's rule:
every person's ID token carries ``roles`` equal to the role of their group, and the
person in no group carries no ``roles`` claim at all (the access token's checks,
which were there, stay). Here the two functions run against the real generator's
realm file and tokens made in the test (unsigned in meaning: nothing verifies them,
and no key, password or secret is written down), so a mutation of either side is
seen to fail without Docker. What this does not show is what the pinned Keycloak
puts in the ID token: that is the rig's, and was not run for this change.
"""

from pathlib import Path
from typing import Any

import jwt
import keycloakcastsupport as cast
import pytest
from test_kind_identity_realm import REALM_FILE, generate, read_secrets

# Stand-in key of the tokens made here; nothing reads them with a key.
STAND_IN_KEY = "stand-in-key-for-an-unverified-test-token-0123456789"
GROUPLESS = "linnea.berg"
USERS = [
    "aino.lindqvist",
    "soren.halvorsen",
    "ingrid.strand",
    "mikkel.vang",
    "freja.dahl",
    "henrik.eide",
    GROUPLESS,
]


@pytest.fixture
def realm(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    out = tmp_path / "identity"
    done = generate(out)
    assert done.returncode == 0, done.stderr
    return out / REALM_FILE, read_secrets(out)


def token(claims: dict[str, Any]) -> str:
    return jwt.encode({"sub": "stand-in", **claims}, STAND_IN_KEY, algorithm="HS256")


def sign_in_as(
    roles_of: dict[str, list[str]],
    *,
    access: dict[str, dict[str, Any]] | None = None,
    ident: dict[str, dict[str, Any]] | None = None,
):
    """A ``sign_in`` that answers each person the way the pinned issuer is
    expected to: ``roles`` in the access token and in the ID token for a person
    with a role, and no such claim for one without. ``access`` and ``ident`` change
    one person's claims."""

    def sign_in(user: str, _password: str, _pages_secret: str) -> dict[str, Any]:
        roles = roles_of[user]
        base = {"roles": roles, "realm_access": {"roles": roles}} if roles else {}
        id_base = {"roles": roles} if roles else {}
        return {
            "access_token": token({**base, **(access or {}).get(user, {})}),
            "id_token": token({**id_base, **(ident or {}).get(user, {})}),
        }

    return sign_in


def observed(
    realm: tuple[Path, dict[str, str]], **changes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    realm_file, secrets = realm
    record: dict[str, Any] = {}
    cast.observe_cast(
        sign_in_as(cast.realm_cast(realm_file), **changes), secrets, realm_file, record
    )
    return record


def test_the_issuer_as_expected_passes_for_all_seven(
    realm: tuple[Path, dict[str, str]],
) -> None:
    record = observed(realm)

    cast.assert_the_cast_holds(record)
    assert len(record["cast"]) == 7
    assert record["cast"][GROUPLESS]["id_roles_claim"] == "absent"
    assert record["cast"]["ingrid.strand"]["id_roles_claim"] == ["adjuster"]


def test_the_record_holds_the_id_tokens_roles_of_every_person(
    realm: tuple[Path, dict[str, str]],
) -> None:
    record = observed(realm)
    expected = cast.realm_cast(realm[0])

    for user, seen in record["cast"].items():
        assert seen["id_roles_claim"] == (expected[user] or "absent"), user


@pytest.mark.parametrize("user", USERS)
@pytest.mark.parametrize(
    "claims",
    [
        {"roles": ["auditor", "adjuster", "platform-admin"]},
        {"roles": ["nobody"]},
        {"roles": []},
    ],
    ids=["too-many-roles", "another-role", "empty-list"],
)
def test_a_wrong_roles_claim_in_any_persons_id_token_fails(
    realm: tuple[Path, dict[str, str]], user: str, claims: dict[str, Any]
) -> None:
    expected = cast.realm_cast(realm[0])
    if expected[user] == claims["roles"]:
        pytest.skip("the claim is the right one for this person")

    record = observed(realm, ident={user: claims})

    with pytest.raises(AssertionError):
        cast.assert_the_cast_holds(record)


@pytest.mark.parametrize("user", ["aino.lindqvist", "ingrid.strand", "henrik.eide"])
def test_a_person_with_a_role_whose_id_token_has_no_roles_claim_fails(
    realm: tuple[Path, dict[str, str]], user: str
) -> None:
    record = observed(realm)
    record["cast"][user]["id_roles_claim"] = "absent"

    with pytest.raises(AssertionError):
        cast.assert_the_cast_holds(record)


def test_the_person_in_no_group_must_have_no_claim_not_an_empty_list(
    realm: tuple[Path, dict[str, str]],
) -> None:
    record = observed(realm, ident={GROUPLESS: {"roles": []}})

    with pytest.raises(AssertionError):
        cast.assert_the_cast_holds(record)


def test_the_access_tokens_checks_are_still_made(
    realm: tuple[Path, dict[str, str]],
) -> None:
    wrong_role = observed(realm, access={"ingrid.strand": {"roles": ["auditor"]}})
    with_groups = observed(realm, access={"mikkel.vang": {"groups": ["/x"]}})

    for record in (wrong_role, with_groups):
        with pytest.raises(AssertionError):
            cast.assert_the_cast_holds(record)


def test_the_rigs_file_is_no_longer_than_it_was() -> None:
    # tests/meridian/test_keycloak_rig.py is at its 800-line ceiling: the
    # assertion lives in keycloakcastsupport.py and the rig changed by no line.
    lines = (Path(__file__).parent / "test_keycloak_rig.py").read_text().splitlines()

    assert len(lines) <= 800
