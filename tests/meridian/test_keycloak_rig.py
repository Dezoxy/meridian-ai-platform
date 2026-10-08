"""The pinned Keycloak, started in a container, signed in to, and read (S021, Y2a).

OPT-IN: skipped unless ``MERIDIAN_KEYCLOAK_RIG=1``, and CI never sets it. The
rig starts ``KEYCLOAK_IMAGE`` of ``infra/kind/pins.env`` with 1 GB and the realm
that ``infra/kind/identity-realm.sh`` generates, signs in with the scripts'
client and, with no browser, through the pages' authorization-code flow with
PKCE, and writes WHAT THE TOKENS REALLY CARRY to a JSON file
(``MERIDIAN_KEYCLOAK_RIG_OUT``): claim names and shapes (a value that identifies
anything is replaced by its type and length), the verdict of ``check_bearer``
for each token, what ``iss`` follows from the host name asked, what a restart
and a new container do to the keys, memory and time. No token, password,
secret or key is written. The needs: Docker, about 2.5 GB of free memory (the
rig waits up to 15 minutes for it and then stops with a failure that says so).

    MERIDIAN_KEYCLOAK_RIG=1 MERIDIAN_KEYCLOAK_RIG_OUT=/some/dir/observed.json \\
        uv run pytest -n 0 tests/meridian/test_keycloak_rig.py

The tests that run always are the rig's pure helpers.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import keycloakcastsupport as cast
import pytest
from kindsupport import KIND_DIR

from meridian.platform.common.signin import (
    SigninRefusal,
    SigninSettings,
    check_bearer,
)
from meridian.platform.common.signinkeys import KeySet, parse_key_set

OPT_IN = "MERIDIAN_KEYCLOAK_RIG"
OUT_ENV = "MERIDIAN_KEYCLOAK_RIG_OUT"
rig_only = pytest.mark.skipif(
    os.environ.get(OPT_IN) != "1", reason=f"opt-in: set {OPT_IN}=1 (starts a container)"
)

CONTAINER = "meridian-keycloak-rig"
REALM = "meridian-staff"
# The names a browser and a pod use for the issuer (the second host name is the
# kind design's, D2; the Service's name is the next contract's to fix).
FRONT_HOST = "id.meridian.localhost:8088"
BACK_HOST = "keycloak.identity.svc:8080"
API_AUDIENCE = "meridian-claims-api"
PAGES = "meridian-claims-web"
SCRIPTS = "meridian-scripts"
REDIRECT = "http://claims.meridian.localhost:8088/auth/callback"
ORIGIN = "http://claims.meridian.localhost:8088"
ADJUSTER = "ingrid.strand"  # of the cast (Y2e), in place of `test-adjuster`
MEMORY_FLOOR_MB = 2500
MEMORY_PATIENCE_SECONDS = 15 * 60
START_PATIENCE_SECONDS = 240
TOKEN_CLAIMS = [
    *("iss", "aud", "azp", "typ", "sub", "exp", "iat", "nbf"),
    *("roles", "realm_access", "resource_access", "scope", "nonce", "sid"),
]
# Shown as they are: they name no person. Everything else is a type and a length.
LITERAL_CLAIMS = {
    "iss",
    "aud",
    "azp",
    "typ",
    "scope",
    "roles",
    "realm_access",
    "resource_access",
    "acr",
}


class StopTheRig(Exception):
    """A precondition failed; nothing was started."""


# ── pure helpers ─────────────────────────────────────────────────────────────


def pkce_pair() -> tuple[str, str]:
    """A code verifier and its S256 challenge (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def shape(name: str, value: Any) -> Any:
    """A claim's value as the record shows it: literal for the claims that name
    no person, else its type, and its length for a text, a list or an object."""
    if name in LITERAL_CLAIMS:
        return value
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return type(value).__name__
    if isinstance(value, str):
        return f"str({len(value)})"
    if isinstance(value, list):
        return f"list({len(value)})"
    return type(value).__name__


class LoginForm(HTMLParser):
    """The action of the login page's form (``kc-form-login``)."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if tag == "form" and found.get("id") == "kc-form-login":
            self.action = found.get("action")


def login_action(page: str) -> str:
    parser = LoginForm()
    parser.feed(page)
    assert parser.action, "the page has no login form"
    return parser.action


def page_message(page: str) -> str:
    """The text Keycloak's page shows as its message (an error, mostly)."""
    found = re.search(r'class="kc-feedback-text">([^<]*)<', page)
    return found.group(1).strip() if found else "(no message on the page)"


def cookie_flags(response: httpx.Response) -> list[str]:
    """Each cookie the response sets: its name and attributes, never its value."""
    flags = []
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        attributes = [a.strip() for a in rest.split(";")[1:]]
        flags.append(f"{name}: {', '.join(attributes)}")
    return flags


def unverified(token: str) -> dict[str, Any]:
    """The claims of a token, signature not checked: for a comparison or a shape,
    never for a decision."""
    return jwt.decode(token, options={"verify_signature": False})


def cookie_header(client: httpx.Client) -> str:
    """The Cookie header of everything the client was given, Secure or not."""
    return "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)


def describe(token: str) -> dict[str, Any]:
    """Header and claims of a token, unverified, as the record shows them."""
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, options={"verify_signature": False})
    return {
        "header": {
            "alg": header.get("alg"),
            "typ": header.get("typ"),
            "kid": shape("kid", header.get("kid")),
        },
        "claims_present": sorted(claims),
        "claims": {
            name: shape(name, claims[name]) if name in claims else "absent"
            for name in TOKEN_CLAIMS
        },
        "lifetime_seconds": claims["exp"] - claims["iat"] if "exp" in claims else None,
    }


def test_the_challenge_is_the_s256_of_the_verifier() -> None:
    verifier, challenge = pkce_pair()

    expected = hashlib.sha256(verifier.encode()).digest()
    assert base64.urlsafe_b64decode(challenge + "=") == expected
    assert pkce_pair()[0] != verifier


def test_a_value_that_names_a_person_is_shown_by_type_and_length() -> None:
    assert shape("sub", "f4d8a0c2-1b7e-4d1a-9c3e-0123456789ab") == "str(36)"
    assert shape("sid", "x" * 8) == "str(8)"
    assert shape("exp", 1760000000) == "int"
    assert shape("roles", ["adjuster"]) == ["adjuster"]
    assert shape("iss", "http://id.example.test/realms/r") == (
        "http://id.example.test/realms/r"
    )
    assert shape("other", ["a", "b"]) == "list(2)"


def test_the_login_action_is_read_with_its_ampersands_decoded() -> None:
    page = (
        '<html><form id="other" action="/no"></form>'
        '<form id="kc-form-login" action="http://h/login?a=1&amp;b=2" method="post">'
        "</form></html>"
    )

    assert login_action(page) == "http://h/login?a=1&b=2"


def test_a_token_is_described_without_its_values() -> None:
    token = jwt.encode(
        {"sub": "abc", "iat": 100, "exp": 400, "roles": ["adjuster"], "typ": "Bearer"},
        "k" * 32,
        algorithm="HS256",
        headers={"kid": "kid-value"},
    )

    described = describe(token)

    assert described["claims"]["sub"] == "str(3)"
    assert described["claims"]["nbf"] == "absent"
    assert described["claims"]["roles"] == ["adjuster"]
    assert described["header"]["kid"] == "str(9)"
    assert described["lifetime_seconds"] == 300
    assert "abc" not in json.dumps(described)


# ── the container ────────────────────────────────────────────────────────────


def available_mb() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise StopTheRig("/proc/meminfo has no MemAvailable")


def wait_for_memory() -> int:
    """MemAvailable in MB once it is at least the floor; else stop the rig."""
    deadline = time.monotonic() + MEMORY_PATIENCE_SECONDS
    while True:
        free = available_mb()
        if free >= MEMORY_FLOOR_MB:
            return free
        if time.monotonic() >= deadline:
            raise StopTheRig(
                f"{free} MB available after {MEMORY_PATIENCE_SECONDS // 60} minutes; "
                f"the rig needs {MEMORY_FLOOR_MB}"
            )
        time.sleep(20)


def docker(*arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {arguments[0]} failed: {result.stderr[-400:]}")
    return result.stdout.strip()


def pinned_image() -> str:
    for line in (KIND_DIR / "pins.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("KEYCLOAK_IMAGE="):
            return line.partition("=")[2]
    raise StopTheRig("pins.env has no KEYCLOAK_IMAGE")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Container:
    """The one container of the rig: 1 GB, no swap, loopback ports only."""

    def __init__(self, imports: Path) -> None:
        self.port = free_port()
        self.management_port = free_port()
        self.imports = imports

    def start(self) -> float:
        """Run it; the seconds from the command to its return."""
        started = time.monotonic()
        docker(
            "run", "-d", "--name", CONTAINER,
            "--memory", "1g", "--memory-swap", "1g",
            "-p", f"127.0.0.1:{self.port}:8080",
            "-p", f"127.0.0.1:{self.management_port}:9000",
            "-v", f"{self.imports}:/opt/keycloak/data/import:ro",
            pinned_image(),
            "start-dev", "--import-realm", "--health-enabled=true",
            f"--hostname=http://{FRONT_HOST}",
            "--hostname-backchannel-dynamic=true",
        )  # fmt: skip
        return time.monotonic() - started

    def remove(self) -> None:
        docker("rm", "-f", CONTAINER, check=False)

    def stats(self) -> dict[str, str]:
        columns = "{{.MemUsage}}|{{.MemPerc}}|{{.CPUPerc}}"
        line = docker("stats", "--no-stream", "--format", columns, CONTAINER)
        usage, percent, cpu = line.split("|")
        return {"memory": usage, "memory_percent": percent, "cpu": cpu}

    def oom_killed(self) -> bool:
        return docker("inspect", "-f", "{{.State.OOMKilled}}", CONTAINER) == "true"

    def log_tail(self, lines: int = 30) -> str:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(lines), CONTAINER],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        return result.stdout + result.stderr

    def http(self) -> httpx.Client:
        return httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}",
            trust_env=False,
            follow_redirects=False,
            timeout=30,
        )

    def wait_ready(self, since: float) -> dict[str, float]:
        """Seconds from `since` to the first discovery answer and to a ready
        health check (port 9000), polled every half second."""
        deadline = time.monotonic() + START_PATIENCE_SECONDS
        seen: dict[str, float] = {}
        discovery = f"/realms/{REALM}/.well-known/openid-configuration"
        ready = f"http://127.0.0.1:{self.management_port}/health/ready"
        while time.monotonic() < deadline and len(seen) < 2:
            for name, url in (("discovery", discovery), ("health_ready", ready)):
                if name not in seen and self._answers(url):
                    seen[name] = round(time.monotonic() - since, 1)
            time.sleep(0.5)
        if len(seen) < 2:
            raise AssertionError(f"not ready: {seen}\n{self.log_tail()}")
        return seen

    def _answers(self, url: str) -> bool:
        try:
            if url.startswith("http://"):
                return httpx.get(url, trust_env=False, timeout=3).status_code == 200
            return self.get(url).status_code == 200
        except httpx.HTTPError:
            return False

    def get(self, path: str, host: str = FRONT_HOST) -> httpx.Response:
        with self.http() as client:
            return client.get(path, headers={"Host": host})


# ── what the rig does against it ─────────────────────────────────────────────


def connect_url(container: Container, path: str) -> str:
    return f"http://127.0.0.1:{container.port}{path}"


def endpoint(path: str) -> str:
    return f"/realms/{REALM}/protocol/openid-connect/{path}"


def scripts_token(container: Container, secret: str, host: str) -> dict[str, Any]:
    with container.http() as client:
        answer = client.post(
            endpoint("token"),
            data={"grant_type": "client_credentials"},
            auth=(SCRIPTS, secret),
            headers={"Host": host},
        )
    assert answer.status_code == 200, answer.text[:200]
    return answer.json()


def code_flow(
    container: Container, user: str, password: str, secret: str, exchange_host: str
) -> dict[str, Any]:
    """The pages' flow without a browser: the login form is fetched and posted
    by an HTTP client that keeps the cookies, the code is read from the redirect
    (which is not followed: its host is the Claims API's), and exchanged with
    the PKCE verifier at `exchange_host`, as the app's pod would."""
    verifier, challenge = pkce_pair()
    state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    query = {
        "client_id": PAGES, "response_type": "code", "scope": "openid",
        "redirect_uri": REDIRECT, "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }  # fmt: skip
    with container.http() as client:
        form = client.get(endpoint("auth"), params=query, headers={"Host": FRONT_HOST})
        assert form.status_code == 200, form.text[:200]
        action = urlsplit(login_action(form.text))
        # Keycloak marks its cookies Secure even on an http front name, and an HTTP
        # client does not send a Secure cookie over http (a browser does, on a
        # localhost name): the rig sends them itself.
        posted = client.post(
            f"{action.path}?{action.query}",
            data={"username": user, "password": password, "credentialId": ""},
            headers={"Host": FRONT_HOST, "Cookie": cookie_header(client)},
        )
        assert posted.status_code == 302, (
            f"{posted.status_code}: {page_message(posted.text)}; "
            f"cookies of the form: {cookie_flags(form)}"
        )
        location = urlsplit(posted.headers["location"])
        returned = parse_qs(location.query)
        assert returned["state"] == [state]
        exchanged = client.post(
            endpoint("token"),
            data={
                "grant_type": "authorization_code",
                "code": returned["code"][0],
                "redirect_uri": REDIRECT,
                "code_verifier": verifier,
            },
            auth=(PAGES, secret),
            headers={"Host": exchange_host},
        )
    assert exchanged.status_code == 200, exchanged.text[:200]
    answer = exchanged.json()
    answer["_nonce_sent"] = nonce
    answer["_redirect_params"] = sorted(returned)
    answer["_redirect_iss"] = returned.get("iss", [None])[0]
    answer["_cookie_flags"] = cookie_flags(form)
    return answer


def refusals(container: Container, secret: str, password: str) -> dict[str, Any]:
    """What the realm refuses: no PKCE, a direct grant, a wrong secret."""
    seen: dict[str, Any] = {}
    with container.http() as client:
        headers = {"Host": FRONT_HOST}
        no_pkce = client.get(
            endpoint("auth"),
            params={
                "client_id": PAGES,
                "response_type": "code",
                "scope": "openid",
                "redirect_uri": REDIRECT,
                "state": "s",
            },
            headers=headers,
        )
        seen["authorization_without_pkce"] = {
            "status": no_pkce.status_code,
            "error": parse_qs(urlsplit(no_pkce.headers.get("location", "")).query).get(
                "error"
            ),
        }
        direct = client.post(
            endpoint("token"),
            data={"grant_type": "password", "username": ADJUSTER,
                  "password": password},
            auth=(PAGES, secret), headers=headers,
        )  # fmt: skip
        seen["direct_grant_on_the_pages_client"] = {
            "status": direct.status_code,
            "error": direct.json().get("error"),
        }
        wrong = client.post(
            endpoint("token"),
            data={"grant_type": "client_credentials"},
            auth=(SCRIPTS, "wrong-" + secrets.token_hex(8)), headers=headers,
        )  # fmt: skip
        seen["wrong_client_secret"] = {"status": wrong.status_code}
        pages_credentials = client.post(
            endpoint("token"),
            data={"grant_type": "client_credentials"},
            auth=(PAGES, secret), headers=headers,
        )  # fmt: skip
        seen["client_credentials_on_the_pages_client"] = {
            "status": pages_credentials.status_code,
            "error": pages_credentials.json().get("error"),
        }
    return seen


def settings_for(
    container: Container, issuer: str, typ: str = "", azp: tuple[str, ...] = ()
) -> SigninSettings:
    return SigninSettings(
        population="staff",
        issuer=issuer,
        audience=API_AUDIENCE,
        environment="kind",  # the key URL is plain HTTP on the loopback
        keys_url=connect_url(container, endpoint("certs")),
        required_typ=typ,
        allowed_azp=azp,
    )


def verdict(token: str, settings: SigninSettings, keys: KeySet) -> dict[str, Any]:
    """What `check_bearer` says: accepted with the roles, or the refusal."""
    try:
        principal = check_bearer(token, settings, keys, time.time())
    except SigninRefusal as refusal:
        return {
            "outcome": "refused",
            "status": refusal.status,
            "reason": refusal.reason,
        }
    return {
        "outcome": "accepted",
        "roles": sorted(principal.roles),
        "subject": f"str({len(principal.subject)})",
        "issuer_is_the_setting": principal.issuer == settings.issuer,
    }


def key_facts(container: Container) -> dict[str, Any]:
    body = container.get(endpoint("certs")).content
    keys = json.loads(body)["keys"]
    return {
        "keys": [
            {"use": k.get("use"), "alg": k.get("alg"), "kty": k.get("kty")}
            for k in keys
        ],
        "kids": sorted(k["kid"] for k in keys),
        "usable_for_the_module": len(parse_key_set(body)),
    }


def discovery_under(container: Container, host: str) -> dict[str, Any]:
    document = container.get(
        f"/realms/{REALM}/.well-known/openid-configuration", host
    ).json()
    wanted = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")
    wanted = (*wanted, "end_session_endpoint")
    return {name: document.get(name) for name in wanted}


class Hidden(str):
    """A generated password or secret whose repr is not its value: pytest prints
    the arguments of a failing frame with repr, and a traceback in a log is
    where a value that was never to be printed would end up."""

    def __repr__(self) -> str:
        return "<hidden>"


def read_secrets(path: Path) -> dict[str, str]:
    pairs = (line.partition("=") for line in path.read_text().splitlines())
    return {key: Hidden(value) for key, _, value in pairs}


def test_a_generated_value_does_not_show_in_a_repr() -> None:
    value = Hidden("0123456789abcdef")

    assert "0123456789abcdef" not in repr({"a": value})
    assert "0123456789abcdef" not in f"{[value]}"
    assert value == "0123456789abcdef"


@contextmanager
def generated_realm(work: Path) -> Iterator[tuple[Path, dict[str, str]]]:
    """The realm generated into `work`/identity, a readable copy of the realm
    file alone in `work`/import (the image runs as user 1000 and the generated
    file is mode 600; the secrets file is never mounted), and the secrets."""
    out, imports = work / "identity", work / "import"
    subprocess.run(
        ["bash", str(KIND_DIR / "identity-realm.sh"), str(out), REDIRECT, ORIGIN],
        check=True, capture_output=True,
    )  # fmt: skip
    imports.mkdir(mode=0o755)
    shutil.copy(out / f"{REALM}-realm.json", imports / f"{REALM}-realm.json")
    (imports / f"{REALM}-realm.json").chmod(0o644)
    yield imports, read_secrets(out / "secrets.env")


# ── the rig ──────────────────────────────────────────────────────────────────


def image_facts(image: str) -> dict[str, Any]:
    inspected = json.loads(docker("image", "inspect", image))[0]
    config = inspected["Config"]
    return {
        "reference": image,
        "size_bytes": inspected["Size"],
        "user": config.get("User"),
        "entrypoint": config.get("Entrypoint"),
        "cmd": config.get("Cmd"),
        "exposed_ports": sorted(config.get("ExposedPorts", {})),
        "architecture": inspected["Architecture"],
    }


def observe_tokens(
    container: Container, secrets_by_name: dict[str, str], record: dict[str, Any]
) -> dict[str, Any]:
    """The flows and what the tokens carry; returns the raw tokens for the
    checks that follow (they are never written)."""
    password = secrets_by_name[cast.password_key(ADJUSTER)]
    pages_secret = secrets_by_name["MERIDIAN_STAFF_CLIENT_MERIDIAN_CLAIMS_WEB_SECRET"]
    scripts_secret = secrets_by_name["MERIDIAN_STAFF_CLIENT_MERIDIAN_SCRIPTS_SECRET"]
    flows = {
        "pages_exchanged_at_front": code_flow(
            container, ADJUSTER, password, pages_secret, FRONT_HOST
        ),
        "pages_exchanged_at_back": code_flow(
            container, ADJUSTER, password, pages_secret, BACK_HOST
        ),
        "scripts_at_front": scripts_token(container, scripts_secret, FRONT_HOST),
        "scripts_at_back": scripts_token(container, scripts_secret, BACK_HOST),
    }
    described: dict[str, Any] = {}
    for name, answer in flows.items():
        entry = {"response_fields": sorted(k for k in answer if not k.startswith("_"))}
        for kind in ("access_token", "id_token", "refresh_token"):
            if kind in answer:
                entry[kind] = describe(answer[kind])
        for field in ("expires_in", "refresh_expires_in", "scope", "token_type"):
            entry[field] = answer.get(field)
        if "_nonce_sent" in answer and "id_token" in answer:
            claims = jwt.decode(answer["id_token"], options={"verify_signature": False})
            entry["id_token_nonce_equals_the_one_sent"] = (
                claims.get("nonce") == answer["_nonce_sent"]
            )
            entry["redirect_query_names"] = answer["_redirect_params"]
            entry["redirect_iss"] = answer["_redirect_iss"]
            entry["login_cookies_set_by_the_form"] = answer["_cookie_flags"]
        described[name] = entry
    record["tokens"] = described
    record["refusals"] = refusals(container, pages_secret, password)
    return flows


def observe_verdicts(
    container: Container, flows: dict[str, Any], record: dict[str, Any]
) -> KeySet:
    """`check_bearer` for each kind of token against the live key URL."""
    pages = flows["pages_exchanged_at_front"]
    issuer = jwt.decode(pages["access_token"], options={"verify_signature": False})[
        "iss"
    ]
    keys = KeySet(connect_url(container, endpoint("certs")))
    azp = (PAGES, SCRIPTS)
    cases = {
        "access_token_pages": (pages["access_token"], settings_for(container, issuer)),
        "access_token_pages_typ_bearer_and_azp": (
            pages["access_token"],
            settings_for(container, issuer, "Bearer", azp),
        ),
        "id_token_typ_empty": (pages["id_token"], settings_for(container, issuer)),
        "id_token_typ_bearer": (
            pages["id_token"],
            settings_for(container, issuer, "Bearer"),
        ),
        "refresh_token_typ_empty": (
            pages["refresh_token"],
            settings_for(container, issuer),
        ),
        "access_token_scripts": (
            flows["scripts_at_front"]["access_token"],
            settings_for(container, issuer, "Bearer", azp),
        ),
        "access_token_pages_exchanged_at_back": (
            flows["pages_exchanged_at_back"]["access_token"],
            settings_for(container, issuer, "Bearer", azp),
        ),
        "access_token_pages_wrong_issuer_setting": (
            pages["access_token"],
            settings_for(
                container, "http://keycloak.identity.svc:8080/realms/" + REALM
            ),
        ),
    }
    record["check_bearer"] = {
        name: verdict(token, settings, keys)
        for name, (token, settings) in cases.items()
    }
    record["issuer_pinned_for_these_checks"] = issuer
    return keys


def observe_restart(
    container: Container,
    secrets_by_name: dict[str, str],
    flows: dict[str, Any],
    keys: KeySet,
    record: dict[str, Any],
    how: str,
) -> None:
    """After `how` ('docker restart' or 'a new container from the same image'):
    are the key ids new, is the realm there, does a token from before verify,
    and does a token from after, once the module has fetched again."""
    before = key_facts(container)["kids"] if how == "restart" else record["kids_before"]
    issuer = record["issuer_pinned_for_these_checks"]
    old = flows["pages_exchanged_at_front"]["access_token"]
    started = time.monotonic()
    if how == "restart":
        docker("restart", CONTAINER)
    else:
        container.remove()
        container.start()
    seconds = container.wait_ready(started)
    realm_back = container.get(f"/realms/{REALM}/.well-known/openid-configuration")
    after = key_facts(container)["kids"]
    time.sleep(31)  # the module's refetch interval (REFETCH_INTERVAL_SECONDS)
    settings = settings_for(container, issuer, "Bearer", (PAGES, SCRIPTS))
    pages_secret = secrets_by_name["MERIDIAN_STAFF_CLIENT_MERIDIAN_CLAIMS_WEB_SECRET"]
    scripts_secret = secrets_by_name["MERIDIAN_STAFF_CLIENT_MERIDIAN_SCRIPTS_SECRET"]
    new_scripts = scripts_token(container, scripts_secret, FRONT_HOST)["access_token"]
    password = secrets_by_name[cast.password_key(ADJUSTER)]
    code_flow(container, ADJUSTER, password, pages_secret, FRONT_HOST)
    fresh = KeySet(connect_url(container, endpoint("certs")))
    record[f"after_{how}"] = {
        "token_from_before_with_a_new_key_set": verdict(old, settings, fresh),
        "scripts_subject_is_the_one_from_before": unverified(new_scripts)["sub"]
        == unverified(flows["scripts_at_front"]["access_token"])["sub"],
        "ready_seconds": seconds,
        "realm_answers": realm_back.status_code,
        "kid_sets_equal": before == after,
        "kids_before_count": len(before),
        "kids_after_count": len(after),
        "token_from_before_with_the_old_key_cache": verdict(old, settings, keys),
        "token_from_after_with_the_old_key_cache": verdict(new_scripts, settings, keys),
    }


@rig_only
def test_the_pinned_image_signs_in_and_its_tokens_are_recorded() -> None:
    out = Path(os.environ.get(OUT_ENV, "keycloak-rig-observed.json"))
    record: dict[str, Any] = {"image": image_facts(pinned_image())}
    work = Path(tempfile.mkdtemp(prefix="keycloak-rig-", dir=out.parent))
    container: Container | None = None
    try:
        assert docker("ps", "-aq", "--filter", f"name=^{CONTAINER}$") == ""
        record["available_mb_before_start"] = wait_for_memory()
        with generated_realm(work) as (imports, secrets_by_name):
            container = Container(imports)
            record["docker_run_seconds"] = round(container.start(), 1)
            record["ready"] = container.wait_ready(time.monotonic() - 1)
            record["ready_note"] = "seconds counted from the end of docker run"
            record["kids_before"] = key_facts(container)["kids"]
            record["keys"] = key_facts(container)
            time.sleep(20)
            record["stats_at_rest"] = container.stats()
            record["discovery"] = {
                host: discovery_under(container, host)
                for host in (FRONT_HOST, BACK_HOST, f"127.0.0.1:{container.port}")
            }
            flows = observe_tokens(container, secrets_by_name, record)
            cast.observe_cast(
                lambda u, p, s: code_flow(container, u, p, s, FRONT_HOST),
                secrets_by_name, imports / f"{REALM}-realm.json", record,
            )  # fmt: skip
            keys = observe_verdicts(container, flows, record)
            record["stats_after_flows"] = container.stats()
            observe_restart(container, secrets_by_name, flows, keys, record, "restart")
            observe_restart(container, secrets_by_name, flows, keys, record, "new")
            record["stats_after_restarts"] = container.stats()
            record["oom_killed"] = container.oom_killed()
        record["available_mb_after"] = available_mb()
    finally:
        if container is not None:
            container.remove()
        shutil.rmtree(work, ignore_errors=True)
        out.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}", out.read_text(encoding="utf-8"))
    assert_the_design_holds(record)


def assert_the_design_holds(record: dict[str, Any]) -> None:
    """What S021's design rests on, as the live tokens showed it (written after
    the first run, so that a Keycloak release that changes one fails here)."""
    front = f"http://{FRONT_HOST}/realms/{REALM}"
    tokens = record["tokens"]
    for name in ("pages_exchanged_at_front", "pages_exchanged_at_back"):
        access, ident = tokens[name]["access_token"], tokens[name]["id_token"]
        assert access["claims"]["iss"] == ident["claims"]["iss"] == front
        assert access["claims"]["typ"] == "Bearer"
        assert access["claims"]["aud"] == API_AUDIENCE
        assert access["claims"]["roles"] == ["adjuster"]
        assert ident["claims"]["typ"] == "ID"
        assert ident["claims"]["aud"] == PAGES
        assert ident["claims"]["roles"] == ["adjuster"]
        assert tokens[name]["id_token_nonce_equals_the_one_sent"] is True
    cast.assert_the_cast_holds(record)
    scripts = tokens["scripts_at_back"]["access_token"]["claims"]
    assert scripts["iss"] == front
    assert scripts["roles"] == ["adjuster", "auditor"]
    assert "refresh_token" not in tokens["scripts_at_front"]
    verdicts = record["check_bearer"]
    for accepted in ("access_token_pages", "access_token_scripts"):
        assert verdicts[accepted]["outcome"] == "accepted"
    for refused in ("id_token_typ_empty", "refresh_token_typ_empty"):
        assert verdicts[refused]["outcome"] == "refused"
    assert record["after_restart"]["kid_sets_equal"] is True
    assert record["after_new"]["kid_sets_equal"] is False
    assert record["after_new"]["scripts_subject_is_the_one_from_before"] is True
    assert record["oom_killed"] is False
