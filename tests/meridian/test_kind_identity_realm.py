"""infra/kind/identity-realm.sh: the staff realm of the mock issuer (S021, Y2a).

The script writes the realm file Keycloak imports at start and, beside it, the
file of the passwords and client secrets it made. These tests run the real
script (it needs only bash, jq and openssl) and read what it wrote. They need
no container: what Keycloak does with the file is the rig's question
(test_keycloak_rig.py, opt-in), and the cluster's is the next contract's.
"""

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from kindsupport import KIND_DIR, REPO_ROOT

SCRIPT = KIND_DIR / "identity-realm.sh"
PINS = KIND_DIR / "pins.env"
REDIRECT = "http://claims.meridian.localhost:8088/auth/callback"
ORIGIN = "http://claims.meridian.localhost:8088"
REALM_FILE = "meridian-staff-realm.json"
SECRETS_FILE = "secrets.env"
ROLES = ["platform-admin", "agent-developer", "adjuster", "auditor"]
# The directory `make up` is meant to write to; .gitignore must hide it.
IGNORED_DIRECTORY = "infra/kind/.identity"

requires_tools = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "jq", "openssl")),
    reason="bash, jq or openssl is not installed",
)
pytestmark = requires_tools


def generate(
    out: Path,
    *arguments: str,
    shell_flags: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run the script; the output directory is `out`, the other two arguments
    are the kind values unless `arguments` replaces them."""
    wanted = arguments or (REDIRECT, ORIGIN)
    return subprocess.run(
        ["bash", *shell_flags, str(SCRIPT), str(out), *wanted],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_COLOR": "1"},
    )


def read_secrets(out: Path) -> dict[str, str]:
    found = {}
    for line in (out / SECRETS_FILE).read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        assert separator, "a line of the secrets file is not KEY=value"
        found[key] = value
    return found


def realm_of(out: Path) -> dict:
    return json.loads((out / REALM_FILE).read_text(encoding="utf-8"))


def client_of(realm: dict, client_id: str) -> dict:
    (client,) = [c for c in realm["clients"] if c["clientId"] == client_id]
    return client


def mapper_of(client: dict, kind: str) -> dict:
    (mapper,) = [m for m in client["protocolMappers"] if m["protocolMapper"] == kind]
    return mapper


@pytest.fixture
def made(tmp_path: Path) -> Path:
    out = tmp_path / "identity"
    result = generate(out)
    assert result.returncode == 0, result.stderr
    return out


# ── what the realm holds ─────────────────────────────────────────────────────


def test_the_realm_parses_and_is_the_staff_realm(made: Path) -> None:
    realm = realm_of(made)

    assert realm["realm"] == "meridian-staff"
    assert realm["enabled"] is True
    assert realm["registrationAllowed"] is False


def test_it_has_the_four_roles_and_no_other_realm_role(made: Path) -> None:
    realm = realm_of(made)

    assert [role["name"] for role in realm["roles"]["realm"]] == ROLES


def test_each_role_has_one_test_user_with_that_role_and_a_password(made: Path) -> None:
    realm = realm_of(made)
    people = [u for u in realm["users"] if "serviceAccountClientId" not in u]

    assert sorted(u["username"] for u in people) == sorted(f"test-{r}" for r in ROLES)
    for user in people:
        role = user["username"].removeprefix("test-")
        assert user["realmRoles"] == [role]
        assert user["enabled"] is True
        # A user that must still act (verify an address, change the password)
        # stops the code flow at a second form and the direct grant with an error.
        assert user["requiredActions"] == []
        assert user["emailVerified"] is True
        assert user["email"] and user["firstName"] and user["lastName"]
        (credential,) = user["credentials"]
        assert credential["type"] == "password"
        assert credential["temporary"] is False
        assert len(credential["value"]) >= 32


def test_the_pages_client_is_confidential_code_flow_with_pkce_required(
    made: Path,
) -> None:
    client = client_of(realm_of(made), "meridian-claims-web")

    assert client["publicClient"] is False
    assert client["standardFlowEnabled"] is True
    assert client["directAccessGrantsEnabled"] is False
    assert client["serviceAccountsEnabled"] is False
    assert client["implicitFlowEnabled"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["redirectUris"] == [REDIRECT]
    assert client["webOrigins"] == [ORIGIN]
    assert client["secret"]


def test_the_scripts_client_has_client_credentials_only(made: Path) -> None:
    realm = realm_of(made)
    client = client_of(realm, "meridian-scripts")

    assert client["publicClient"] is False
    assert client["serviceAccountsEnabled"] is True
    assert client["standardFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False
    assert client["implicitFlowEnabled"] is False
    assert client["redirectUris"] == []
    assert client["secret"]
    (account,) = [u for u in realm["users"] if "serviceAccountClientId" in u]
    assert account["serviceAccountClientId"] == "meridian-scripts"
    assert account["username"] == "service-account-meridian-scripts"
    assert account["realmRoles"] == ["adjuster", "auditor"]


@pytest.mark.parametrize("client_id", ["meridian-claims-web", "meridian-scripts"])
def test_both_clients_put_a_list_roles_claim_and_an_api_audience_in_the_access_token(
    made: Path, client_id: str
) -> None:
    client = client_of(realm_of(made), client_id)

    roles = mapper_of(client, "oidc-usermodel-realm-role-mapper")["config"]
    assert roles["claim.name"] == "roles"
    assert roles["multivalued"] == "true"
    assert roles["access.token.claim"] == "true"
    assert roles["id.token.claim"] == "true"
    audience = mapper_of(client, "oidc-audience-mapper")["config"]
    assert audience["included.custom.audience"] == "meridian-claims-api"
    assert audience["access.token.claim"] == "true"
    # The ID token's audience stays the client id (Y3 checks it equals it).
    assert audience["id.token.claim"] == "false"
    assert audience["included.custom.audience"] != client_id


def test_the_api_audience_is_no_client_id(made: Path) -> None:
    realm = realm_of(made)

    assert "meridian-claims-api" not in [c["clientId"] for c in realm["clients"]]


@pytest.mark.parametrize("client_id", ["meridian-claims-web", "meridian-scripts"])
def test_a_clients_token_holds_only_the_four_roles_it_is_scoped_to(
    made: Path, client_id: str
) -> None:
    realm = realm_of(made)

    assert client_of(realm, client_id)["fullScopeAllowed"] is False
    (scope,) = [s for s in realm["scopeMappings"] if s["client"] == client_id]
    assert scope["roles"] == ROLES


def test_every_user_has_an_id_of_its_own_that_the_next_run_gives_it_again(
    tmp_path: Path, made: Path
) -> None:
    again = tmp_path / "again"
    assert generate(again).returncode == 0

    first, second = realm_of(made)["users"], realm_of(again)["users"]

    ids = [user["id"] for user in first]
    assert len(first) == 5
    assert len(set(ids)) == 5
    for identifier in ids:
        assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", identifier)
    # The token's `sub` is this id: after a Keycloak that starts on an empty
    # database, the same person must be the same subject.
    assert ids == [user["id"] for user in second]
    assert not set(ids) & set(read_secrets(made).values())


def test_the_lifetimes_are_stated_and_the_session_is_within_the_apps_eight_hours(
    made: Path,
) -> None:
    realm = realm_of(made)

    assert realm["accessTokenLifespan"] == 300
    assert realm["ssoSessionMaxLifespan"] == 8 * 3600
    assert realm["ssoSessionIdleTimeout"] == 1800
    assert realm["ssoSessionIdleTimeout"] <= realm["ssoSessionMaxLifespan"]


def test_the_arguments_are_the_redirect_uri_and_the_web_origin(tmp_path: Path) -> None:
    result = generate(
        tmp_path / "o", "https://pages.example.test/cb/*", "https://pages.example.test"
    )

    assert result.returncode == 0, result.stderr
    client = client_of(realm_of(tmp_path / "o"), "meridian-claims-web")
    assert client["redirectUris"] == ["https://pages.example.test/cb/*"]
    assert client["webOrigins"] == ["https://pages.example.test"]


# ── what it refuses ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        (REDIRECT,),
        ("ftp://claims.test/cb", ORIGIN),
        ("claims.test/cb", ORIGIN),
        ("http://claims.test/*/cb", ORIGIN),
        ("*", ORIGIN),
        ("http://claims.test/cb with space", ORIGIN),
        (REDIRECT, "http://claims.test/"),
        (REDIRECT, "http://claims.test/path"),
        (REDIRECT, "*"),
        (REDIRECT, ORIGIN, "extra"),
    ],
)
def test_a_bad_argument_is_refused_and_nothing_is_written(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    out = tmp_path / "o"
    command = ["bash", str(SCRIPT), str(out), *arguments]
    if arguments == ():
        command = ["bash", str(SCRIPT)]

    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode != 0
    assert not out.exists()


def test_a_directory_in_a_repository_that_does_not_ignore_it_is_refused(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "work"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    out = repository / "identity"

    result = generate(out)

    assert result.returncode != 0
    assert "gitignore" in result.stderr.lower()
    assert not out.exists()


def test_a_directory_the_repository_ignores_is_accepted(tmp_path: Path) -> None:
    repository = tmp_path / "work"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / ".gitignore").write_text("identity/\n", encoding="utf-8")

    result = generate(repository / "identity")

    assert result.returncode == 0, result.stderr


def test_the_directory_make_up_will_use_is_ignored_by_git() -> None:
    probe = f"{IGNORED_DIRECTORY}/{SECRETS_FILE}"
    realm = f"{IGNORED_DIRECTORY}/{REALM_FILE}"

    for path in (probe, realm):
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", path],
            check=False,
        )
        assert result.returncode == 0, f"{path} is not ignored"


# ── secrets ──────────────────────────────────────────────────────────────────


def test_the_secrets_file_holds_a_password_per_user_and_a_secret_per_client(
    made: Path,
) -> None:
    secrets = read_secrets(made)

    passwords = [
        f"MERIDIAN_STAFF_USER_TEST_{role.upper().replace('-', '_')}_PASSWORD"
        for role in ROLES
    ]
    assert sorted(secrets) == sorted(
        [
            *passwords,
            "MERIDIAN_STAFF_CLIENT_MERIDIAN_CLAIMS_WEB_SECRET",
            "MERIDIAN_STAFF_CLIENT_MERIDIAN_SCRIPTS_SECRET",
        ]
    )
    for value in secrets.values():
        assert re.fullmatch(r"[0-9a-f]{48}", value)
    assert len(set(secrets.values())) == len(secrets)


def test_the_realm_holds_each_secret_exactly_once_and_at_the_place_keycloak_reads(
    made: Path,
) -> None:
    realm = realm_of(made)
    text = (made / REALM_FILE).read_text(encoding="utf-8")
    secrets = read_secrets(made)

    for value in secrets.values():
        assert text.count(value) == 1
    passwords = [c["value"] for u in realm["users"] for c in u.get("credentials", [])]
    client_secrets = [c["secret"] for c in realm["clients"] if "secret" in c]
    assert sorted(passwords + client_secrets) == sorted(secrets.values())
    # Nowhere else in a client: not in an attribute, not in a mapper.
    for client in realm["clients"]:
        rest = {k: v for k, v in client.items() if k != "secret"}
        assert not any(v in json.dumps(rest) for v in secrets.values())


def test_the_files_are_readable_by_their_owner_only(made: Path) -> None:
    for name in (SECRETS_FILE, REALM_FILE):
        mode = stat.S_IMODE((made / name).stat().st_mode)
        assert mode == 0o600, f"{name} has mode {oct(mode)}"
    assert stat.S_IMODE(made.stat().st_mode) == 0o700


def test_two_runs_give_other_secrets_and_the_same_structure(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    assert generate(first).returncode == 0
    assert generate(second).returncode == 0

    one, two = read_secrets(first), read_secrets(second)
    assert sorted(one) == sorted(two)
    assert set(one.values()).isdisjoint(two.values())

    def structure(out: Path) -> str:
        text = (out / REALM_FILE).read_text(encoding="utf-8")
        for value in read_secrets(out).values():
            text = text.replace(value, "<secret>")
        return text

    assert structure(first) == structure(second)


def test_a_second_run_over_the_same_directory_replaces_the_files(
    tmp_path: Path,
) -> None:
    out = tmp_path / "o"
    assert generate(out).returncode == 0
    before = read_secrets(out)

    assert generate(out).returncode == 0

    assert set(before.values()).isdisjoint(read_secrets(out).values())
    assert stat.S_IMODE((out / SECRETS_FILE).stat().st_mode) == 0o600


@pytest.mark.parametrize("flags", [(), ("-x",)])
def test_the_script_prints_none_of_what_it_generated(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    out = tmp_path / "o"

    result = generate(out, shell_flags=flags)

    assert result.returncode == 0, result.stderr
    values = read_secrets(out).values()
    assert len(values) == 6
    for value in values:
        assert value not in result.stdout
        assert value not in result.stderr
    assert result.stdout.strip()


def test_the_script_has_a_usage_and_says_where_it_wrote(tmp_path: Path) -> None:
    result = generate(tmp_path / "o")

    assert str(tmp_path / "o") in result.stdout
    assert SECRETS_FILE in result.stdout


# ── the pin ──────────────────────────────────────────────────────────────────


def test_the_issuer_image_is_pinned_by_digest_in_one_line() -> None:
    pins = PINS.read_text(encoding="utf-8")

    assert re.search(
        r"^KEYCLOAK_IMAGE=quay\.io/keycloak/keycloak:26\.8\.0@sha256:[0-9a-f]{64}$",
        pins,
        re.MULTILINE,
    )
