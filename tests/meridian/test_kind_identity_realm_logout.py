"""The pages client's post-logout address in the realm (S021, Y4b, round two).

The realm's pages client said ``post.logout.redirect.uris: "+"``, which Keycloak
reads as "the client's redirect addresses", and the only one is the callback. The
app returns from sign-out to ``/adjuster/claims``, which the issuer would refuse.
``identity-realm.sh`` now takes an OPTIONAL fourth argument, the exact post-logout
address, and writes it into that attribute; without it the attribute stays ``+``
and the files are what they were, so the callers that pass three arguments (the
rig, older tests) write what they wrote. ``identity.sh`` passes the address of
``IDENTITY_POST_LOGOUT_URI`` and ``values/signin.yaml`` names the same one.

What none of this shows: that the pinned Keycloak accepts the address at the
logout endpoint (the rig does not sign out; it was not run).
"""

import json
import re
from pathlib import Path

import pytest
import yaml
from kindsupport import KIND_DIR
from test_kind_identity_realm import (
    ORIGIN,
    REALM_FILE,
    REDIRECT,
    client_of,
    generate,
    realm_of,
)

ATTRIBUTE = "post.logout.redirect.uris"
POST_LOGOUT = f"{ORIGIN}/adjuster/claims"
PAGES = "meridian-claims-web"
SCRIPTS = "meridian-scripts"


def made(tmp_path: Path, *extra: str) -> dict:
    out = tmp_path / "identity"
    done = generate(out, REDIRECT, ORIGIN, *extra)
    assert done.returncode == 0, done.stderr
    return realm_of(out)


def without_the_random(client: dict) -> dict:
    """A client less its secret, which differs at every run."""
    return {k: v for k, v in client.items() if k != "secret"}


def test_without_the_argument_the_attribute_is_plus_as_it_was(tmp_path: Path) -> None:
    realm = made(tmp_path)

    attributes = client_of(realm, PAGES)["attributes"]
    assert attributes == {"pkce.code.challenge.method": "S256", ATTRIBUTE: "+"}


def test_with_the_argument_the_attribute_is_that_address_and_nothing_else_moves(
    tmp_path: Path,
) -> None:
    plain = made(tmp_path / "a")
    given = made(tmp_path / "b", POST_LOGOUT)

    pages = client_of(given, PAGES)
    assert pages["attributes"] == {
        "pkce.code.challenge.method": "S256",
        ATTRIBUTE: POST_LOGOUT,
    }
    assert pages["redirectUris"] == [REDIRECT]
    # The rest of the client, the other client, the roles, the groups and the
    # users are the same (the users' ids are derived, the secrets are not compared).
    old = without_the_random(client_of(plain, PAGES))
    new = without_the_random(pages)
    old["attributes"] = new["attributes"] = None
    assert old == new
    assert without_the_random(client_of(given, SCRIPTS)) == without_the_random(
        client_of(plain, SCRIPTS)
    )
    assert given["groups"] == plain["groups"]
    assert [u["id"] for u in given["users"]] == [u["id"] for u in plain["users"]]


def test_the_scripts_client_has_no_post_logout_attribute_either_way(
    tmp_path: Path,
) -> None:
    for extra in ((), (POST_LOGOUT,)):
        client = client_of(made(tmp_path / str(len(extra)), *extra), SCRIPTS)
        assert ATTRIBUTE not in client.get("attributes", {})


def test_the_address_is_never_a_secret_so_it_may_be_in_the_realm_file(
    tmp_path: Path,
) -> None:
    out = tmp_path / "identity"
    generate(out, REDIRECT, ORIGIN, POST_LOGOUT)

    assert json.loads((out / REALM_FILE).read_text())["clients"]


@pytest.mark.parametrize(
    "address",
    [
        "/adjuster/claims",
        "adjuster/claims",
        "http://evil.example/adjuster/claims",
        f"{ORIGIN}.evil.example/adjuster/claims",
        f"{ORIGIN}evil/adjuster/claims",
        "https://claims.meridian.localhost:8088/adjuster/claims",
        f"{ORIGIN}/adjuster/claims?next=x",
        f"{ORIGIN}/adjuster/claims#top",
        f"{ORIGIN}/adjuster/*",
        f"{ORIGIN}/adjuster claims",
        f"{ORIGIN}/a\tb",
        ORIGIN,
        "+",
        "",
    ],
    ids=lambda a: a or "empty",
)
def test_an_address_that_is_not_under_the_origin_or_has_a_query_is_refused(
    tmp_path: Path, address: str
) -> None:
    out = tmp_path / "identity"

    done = generate(out, REDIRECT, ORIGIN, address)

    assert done.returncode != 0
    assert "POST_LOGOUT_URI" in done.stderr
    assert not out.exists()  # refused before anything was written


def test_five_arguments_are_a_usage_line_and_four_names_the_optional_one(
    tmp_path: Path,
) -> None:
    done = generate(tmp_path / "x", REDIRECT, ORIGIN, POST_LOGOUT, "more")

    assert done.returncode != 0
    assert "usage: identity-realm.sh OUTPUT_DIR REDIRECT_URI WEB_ORIGIN" in done.stderr
    assert "[POST_LOGOUT_URI]" in done.stderr


# ── identity.sh and the values ───────────────────────────────────────────────


def shell_text(name: str) -> str:
    return (KIND_DIR / name).read_text(encoding="utf-8")


def test_identity_sh_names_the_address_beside_the_redirect_address() -> None:
    text = shell_text("identity.sh")
    redirect = re.search(r"^readonly IDENTITY_REDIRECT_URI=.*$", text, re.M)
    post = re.search(r"^readonly IDENTITY_POST_LOGOUT_URI=(.*)$", text, re.M)

    assert redirect and post
    assert post.group(1) == '"${IDENTITY_CLAIMS_ORIGIN}/adjuster/claims"'
    assert text.index(post.group(0)) > text.index(redirect.group(0))
    assert re.search(
        r'identity-realm\.sh" "\$\{work\}" "\$\{IDENTITY_REDIRECT_URI\}" '
        r'"\$\{IDENTITY_CLAIMS_ORIGIN\}" "\$\{IDENTITY_POST_LOGOUT_URI\}" \|\|',
        text,
    )


def test_the_values_return_after_sign_out_is_the_one_the_realm_is_given() -> None:
    values = yaml.safe_load(
        (KIND_DIR / "values" / "signin.yaml").read_text(encoding="utf-8")
    )["signin"]["staff"]

    assert values["postLogoutRedirectUri"] == POST_LOGOUT
    assert values["postLogoutRedirectUri"] == f"{values['appOrigin']}/adjuster/claims"
