"""What the Keycloak rig and the realm tests share about the cast (S021, Y2e).

The cast is the seven test people of the staff realm
(``infra/kind/identity-realm.sh``), organised in groups: a person's role comes
from the group they are a member of, and a person in no group has no role. It
holds no test.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jwt


def password_key(user: str) -> str:
    """The name of a user's password in the generator's secrets file."""
    return f"MERIDIAN_STAFF_USER_{re.sub('[^A-Z0-9]', '_', user.upper())}_PASSWORD"


def realm_cast(realm_file: Path) -> dict[str, list[str]]:
    """The people of a generated realm file (not the service account), user name
    to the realm roles their groups carry (a person holds none directly)."""
    realm = json.loads(realm_file.read_text(encoding="utf-8"))
    roles_of_group = {f"/{g['name']}": g["realmRoles"] for g in realm["groups"]}
    return {
        user["username"]: [r for g in user["groups"] for r in roles_of_group[g]]
        for user in realm["users"]
        if "serviceAccountClientId" not in user
    }


def observe_cast(
    sign_in: Callable[[str, str, str], dict[str, Any]],
    secrets_by_name: dict[str, str],
    realm_file: Path,
    record: dict[str, Any],
) -> None:
    """Every person of the realm file signs in (``sign_in(user, password,
    pages_secret)`` is the pages' code flow and gives the token response), and
    the claims of the access token that name a role or a group are looked up, and
    the ``roles`` claim of the ID token (``"absent"`` when it has none). The user
    names are fictional; no token is kept."""
    pages_secret = secrets_by_name["MERIDIAN_STAFF_CLIENT_MERIDIAN_CLAIMS_WEB_SECRET"]
    seen: dict[str, Any] = {}
    for user, roles in realm_cast(realm_file).items():
        answer = sign_in(user, secrets_by_name[password_key(user)], pages_secret)
        claims = jwt.decode(answer["access_token"], options={"verify_signature": False})
        ident = jwt.decode(answer["id_token"], options={"verify_signature": False})
        seen[user] = {
            "expected_roles": roles,
            "roles_claim": claims.get("roles", "absent"),
            "id_roles_claim": ident.get("roles", "absent"),
            "realm_access": claims.get("realm_access", "absent"),
            "groups_claim": claims.get("groups", "absent"),
        }
    record["cast"] = seen


def assert_the_cast_holds(record: dict[str, Any]) -> None:
    """Every person signed in and carries the one role their group gives, or none
    (the claim is then absent or empty: either shows no role), and no token says
    anything about groups: the services read `roles`, which keeps Keycloak and
    Entra ID interchangeable. The ID token carries the same `roles` for every
    person (S021, Y4b, the review's rule 8), and the person in no group no such
    claim at all: absent, not an empty list."""
    assert len(record["cast"]) == 7
    for user, seen in record["cast"].items():
        expected = seen["expected_roles"]
        carried, access = seen["roles_claim"], seen["realm_access"]
        assert (carried if isinstance(carried, list) else []) == expected, user
        listed = access.get("roles", []) if isinstance(access, dict) else []
        assert listed == expected, user
        assert seen["groups_claim"] == "absent", user
        assert seen["id_roles_claim"] == (expected or "absent"), user
