"""`make up` makes the rate store's Secret and `make deploy` refuses without it (S066).

The Model Gateway reads its address from the key ``uri`` of the Secret
``rate-store-credentials`` and the store mounts its ACL file from the key
``users.acl``. ``up.sh`` makes the Secret once, from standard input, never prints it
and never overwrites one; ``deploy.sh`` stops before it installs the chart when the
Secret or a key is missing. Nothing touches a cluster: each function runs in bash
against a stub ``kctl``, and what it creates is read back and compared with the
code that reads it (the gateway's own parser of the address, and the commands its
script and its client send), so the two cannot drift.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml
from chartsupport import RATE_STORE, rendered_chart
from servicesupport import REPO_ROOT

from meridian.platform.gateway.rate_store import COLD_CALL_COMMANDS
from meridian.platform.gateway.ratelimit_redis import _SCRIPT, DEFAULT_PREFIX
from meridian.platform.gateway.settings import parse_rate_store_url

KIND_DIR = REPO_ROOT / "infra" / "kind"
COMMON_SH = KIND_DIR / "common.sh"
UP_SH = (KIND_DIR / "up.sh").read_text(encoding="utf-8")
DEPLOY_SH = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
SECRET_NAME = "rate-store-credentials"  # noqa: S105 (a Secret name, not a password)
GATEWAY_USER = "gateway"
STORE_PORT = 6379
# The password: 32 random bytes as 64 lower-case hex digits, an alphabet that
# needs no percent-encoding in an address.
PASSWORD_LENGTH = 64
PASSWORD_ALPHABET = re.compile(r"[0-9a-f]{64}")


def function_text(script: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", script, re.MULTILINE | re.DOTALL)
    assert match, f"no function {name}"
    return match.group(0)


def constants_of(script: str) -> list[str]:
    """The ``readonly RATE_STORE_*`` lines of a script, as written."""
    return re.findall(r"^readonly RATE_STORE_\w+=.*$", script, re.MULTILINE)


def bash(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        timeout=60,
    )


# ── what `make up` creates ───────────────────────────────────────────────────


def run_ensure(
    tmp_path: Path, *, exists: bool = False
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """``ensure_rate_store_secret`` of up.sh in bash against a stub ``kctl`` that
    knows the Secret only when ``exists``. Returns the process, what ``create``
    read from standard input and every ``kctl`` call's arguments."""
    created = tmp_path / "created.yaml"
    calls = tmp_path / "calls"
    calls.touch()
    answer = "return 0" if exists else "return 1"
    script = "\n".join(
        [
            "set -euo pipefail",
            f'. "{COMMON_SH}"',
            *constants_of(UP_SH),
            "kctl() {",
            f'  printf "%s\\n" "$*" >>"{calls}"',
            '  case "$*" in',
            f'    *"get secret {SECRET_NAME}") {answer} ;;',
            f'    *"create -f -"*) cat >>"{created}" ;;',
            '    *) echo "unexpected kctl $*" >&2; return 1 ;;',
            "  esac",
            "}",
            function_text(UP_SH, "ensure_rate_store_secret"),
            "ensure_rate_store_secret",
        ]
    )
    done = bash(script, tmp_path)
    text = created.read_text(encoding="utf-8") if created.exists() else ""
    return done, text, calls.read_text(encoding="utf-8")


def made(tmp_path: Path) -> tuple[dict, str, str]:
    """The Secret ``make up`` makes: the object, the address and the ACL file."""
    done, created, _ = run_ensure(tmp_path)
    assert done.returncode == 0, done.stderr
    secret = yaml.safe_load(created)
    return secret, secret["stringData"]["uri"], secret["stringData"]["users.acl"]


def test_make_up_creates_the_secret_from_standard_input_with_the_two_keys(
    tmp_path: Path,
) -> None:
    done, created, calls = run_ensure(tmp_path)

    assert done.returncode == 0, done.stderr
    secret = yaml.safe_load(created)
    assert secret["apiVersion"] == "v1"
    assert secret["kind"] == "Secret"
    assert secret["metadata"] == {"name": SECRET_NAME, "namespace": "meridian"}
    assert secret["type"] == "Opaque"
    assert set(secret["stringData"]) == {"uri", "users.acl"}
    assert "data" not in secret
    assert calls.splitlines() == [
        f"-n meridian get secret {SECRET_NAME}",
        "create -f -",
    ]
    assert f"creating secret {SECRET_NAME}" in done.stdout


def test_make_up_never_overwrites_a_secret_that_exists(tmp_path: Path) -> None:
    done, created, calls = run_ensure(tmp_path, exists=True)

    assert done.returncode == 0, done.stderr
    assert created == ""
    assert calls.splitlines() == [f"-n meridian get secret {SECRET_NAME}"]
    assert f"secret {SECRET_NAME} exists" in done.stdout


def test_the_function_creates_from_stdin_and_never_applies_or_patches() -> None:
    body = function_text(UP_SH, "ensure_rate_store_secret")

    assert "kctl create -f -" in body
    assert "--from-literal" not in body
    assert not re.search(r"\b(apply|patch|replace)\b", body)
    assert "set +x" in body  # a `bash -x` run must not trace the password
    assert "echo" not in body  # nothing prints what it generates


def test_neither_the_terminal_nor_a_command_line_ever_holds_the_password(
    tmp_path: Path,
) -> None:
    done, created, calls = run_ensure(tmp_path)
    password = parse_rate_store_url(yaml.safe_load(created)["stringData"]["uri"])

    assert password.password not in done.stdout + done.stderr
    assert password.password not in calls


def test_the_password_is_32_random_bytes_in_an_alphabet_that_needs_no_encoding(
    tmp_path: Path,
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _, first, _ = made(tmp_path / "a")
    _, second, _ = made(tmp_path / "b")

    one = parse_rate_store_url(first).password
    other = parse_rate_store_url(second).password
    assert len(one) == PASSWORD_LENGTH
    assert PASSWORD_ALPHABET.fullmatch(one)
    # Two runs, two passwords: a constant would pass every check above.
    assert one != other
    # Nothing to percent-encode, so the address is exactly what it looks like.
    assert quote(one, safe="") == one


def test_the_address_is_the_form_the_gateways_own_parser_accepts(
    tmp_path: Path,
) -> None:
    _, uri, _ = made(tmp_path)

    address = parse_rate_store_url(uri)  # raises on anything the gateway refuses
    assert address.username == GATEWAY_USER
    assert address.port == STORE_PORT
    assert uri == (
        f"rediss://{GATEWAY_USER}:{address.password}@{address.host}:{STORE_PORT}/0"
    )


def test_the_address_names_the_host_of_the_stores_certificate_and_the_stores_port(
    tmp_path: Path,
) -> None:
    _, uri, _ = made(tmp_path)

    address = parse_rate_store_url(uri)
    (certificate,) = [
        d
        for d in rendered_chart()
        if d["kind"] == "Certificate" and d["metadata"]["name"] == RATE_STORE
    ]
    (service,) = [
        d
        for d in rendered_chart()
        if d["kind"] == "Service" and d["metadata"]["name"] == RATE_STORE
    ]
    # The gateway verifies the server's certificate against this very name.
    assert certificate["spec"]["dnsNames"] == [address.host]
    assert [p["port"] for p in service["spec"]["ports"]] == [address.port]


def acl_rules(acl: str) -> dict[str, list[str]]:
    """The users of an ACL file, each with its rules, one line per user."""
    users = {}
    for line in acl.splitlines():
        words = line.split()
        assert words[0] == "user", line
        assert words[1] not in users, f"{words[1]} is set twice"
        users[words[1]] = words[2:]
    return users


def commands_the_gateway_needs() -> set[str]:
    """What the gateway's connection and script send, from the code: the cold
    call's commands (a ``SCRIPT`` there is ``SCRIPT LOAD``, the one subcommand
    of it) and the commands its script runs."""
    assert COLD_CALL_COMMANDS == ("HELLO", "EVALSHA", "SCRIPT", "EVALSHA")
    cold = {name.lower() for name in COLD_CALL_COMMANDS} - {"script"}
    script = {name.lower() for name in re.findall(r"redis\.call\('(\w+)'", _SCRIPT)}
    assert script == {"time", "zremrangebyscore", "zrange", "zadd", "pexpire"}
    return cold | {"script|load"} | script


def test_the_acl_file_turns_the_default_user_off_and_names_one_user(
    tmp_path: Path,
) -> None:
    _, _, acl = made(tmp_path)

    users = acl_rules(acl)
    assert list(users) == ["default", GATEWAY_USER]
    assert users["default"] == ["off"]
    assert users[GATEWAY_USER][0] == "on"
    assert acl.endswith("\n")


def test_the_acl_file_holds_the_hash_of_the_password_and_not_the_password(
    tmp_path: Path,
) -> None:
    _, uri, acl = made(tmp_path)

    rules = acl_rules(acl)[GATEWAY_USER]
    password = parse_rate_store_url(uri).password
    (hashed,) = [rule for rule in rules if rule.startswith("#")]
    assert hashed == "#" + hashlib.sha256(password.encode()).hexdigest()
    assert password not in acl
    # No clear-text password rule, and no way to log in without one.
    assert not [rule for rule in rules if rule.startswith((">", "<", "!"))]
    assert "nopass" not in rules
    assert "allcommands" not in rules and "allkeys" not in rules


def test_the_gateways_user_has_its_keys_no_channels_and_exactly_the_commands_it_sends(
    tmp_path: Path,
) -> None:
    _, _, acl = made(tmp_path)

    rules = acl_rules(acl)[GATEWAY_USER]
    granted = [rule for rule in rules if rule.startswith("+")]
    # The key pattern of the limiter's own prefix, once, and no other key rule.
    assert [rule for rule in rules if rule.startswith(("~", "%"))] == [
        f"~{DEFAULT_PREFIX}:*"
    ]
    # Nothing is granted before everything is taken away, and nothing by category.
    assert rules.index("-@all") < rules.index(granted[0])
    categories = [rule for rule in rules if rule.startswith(("+@", "-@"))]
    assert categories == ["-@all"]
    assert "resetchannels" in rules
    assert not [rule for rule in rules if rule.startswith("&")]
    # The command set is EQUAL to what the gateway sends: none missing (the
    # gateway would be refused) and none more (the user could do more).
    assert {rule.removeprefix("+") for rule in granted} == commands_the_gateway_needs()
    assert len(granted) == len(set(granted))
    # In words, the dangerous ones that must not be there.
    for refused in (
        "client",
        "script",
        "script|flush",
        "script|kill",
        "eval",
        "evalsha_ro",
        "function",
        "keys",
        "del",
        "get",
        "set",
        "flushall",
        "flushdb",
        "config",
        "acl",
        "auth",
        "ping",
        "info",
        "publish",
        "subscribe",
        "monitor",
        "debug",
    ):
        assert f"+{refused}" not in granted, refused


# ── where it is called ───────────────────────────────────────────────────────


def test_up_makes_the_secret_after_the_role_secrets_and_before_the_database() -> None:
    lines = UP_SH.splitlines()
    roles = lines.index("ensure_database_secrets")
    (store,) = [i for i, line in enumerate(lines) if line == "ensure_rate_store_secret"]
    (database,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release platform-db")
    ]

    assert roles < store < database


def test_up_and_deploy_name_the_one_secret_the_chart_takes() -> None:
    (up_name,) = re.findall(r"^readonly RATE_STORE_SECRET=(\S+)$", UP_SH, re.MULTILINE)
    (deploy_name,) = re.findall(
        r"^readonly RATE_STORE_SECRET=(\S+)$", DEPLOY_SH, re.MULTILINE
    )
    values = yaml.safe_load((KIND_DIR / "values" / "meridian.yaml").read_text())

    assert up_name == deploy_name == SECRET_NAME
    assert values["rateStore"]["secret"] == SECRET_NAME


# ── what `make deploy` refuses ───────────────────────────────────────────────

# Values the stub's answers hold, which no output may repeat.
URI_VALUE = "rediss://gateway:stand-in-value@rate-store.meridian.svc:6379/0"
ACL_VALUE = "user default off"


def run_require(
    tmp_path: Path, *, answer: dict | None, fails: bool = False
) -> subprocess.CompletedProcess[str]:
    """``require_rate_store_secret`` of deploy.sh in bash against a stub ``kctl``
    that prints ``answer`` as the Secret's JSON, or fails when ``fails``."""
    reply = "return 1" if fails else f"printf '%s' '{json.dumps(answer)}'"
    script = "\n".join(
        [
            "set -euo pipefail",
            "die() { printf 'error: %s\\n' \"$*\" >&2; exit 1; }",
            "NAMESPACE=meridian",
            *constants_of(DEPLOY_SH),
            "kctl() {",
            '  case "$*" in',
            f'    *"get secret {SECRET_NAME} -o json"*) {reply} ;;',
            '    *) echo "unexpected kctl $*" >&2; return 1 ;;',
            "  esac",
            "}",
            function_text(DEPLOY_SH, "require_rate_store_secret"),
            "require_rate_store_secret",
        ]
    )
    return bash(script, tmp_path)


def secret_with(**data: str) -> dict:
    return {"data": data}


def test_deploy_goes_on_when_the_secret_has_both_keys(tmp_path: Path) -> None:
    done = run_require(
        tmp_path, answer=secret_with(uri=URI_VALUE, **{"users.acl": ACL_VALUE})
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout + done.stderr == ""


def test_deploy_refuses_a_secret_that_is_missing_and_says_to_run_make_up(
    tmp_path: Path,
) -> None:
    done = run_require(tmp_path, answer=None, fails=True)

    assert done.returncode == 1
    assert f"Secret {SECRET_NAME} does not exist" in done.stderr
    assert "run 'make up' first" in done.stderr


@pytest.mark.parametrize(
    ("data", "missing"),
    [
        ({"uri": URI_VALUE}, "users.acl"),
        ({"users.acl": ACL_VALUE}, "uri"),
        ({}, "uri"),
        ({"uri": "", "users.acl": ACL_VALUE}, "uri"),
        ({"uri": URI_VALUE, "users.acl": ""}, "users.acl"),
    ],
    ids=["no-acl", "no-uri", "no-data", "empty-uri", "empty-acl"],
)
def test_deploy_refuses_a_secret_that_lacks_a_key_and_says_how_to_make_it_again(
    tmp_path: Path, data: dict, missing: str
) -> None:
    done = run_require(tmp_path, answer={"data": data})

    assert done.returncode == 1
    assert f"no key '{missing}'" in done.stderr
    assert "run 'make up' first" in done.stderr
    # A Secret that exists is kept by `make up`: the remedy says to delete it.
    assert f"kubectl -n meridian delete secret {SECRET_NAME}" in done.stderr


def test_the_refusal_never_prints_what_the_secret_holds(tmp_path: Path) -> None:
    done = run_require(tmp_path, answer={"data": {"uri": URI_VALUE}})

    assert done.returncode == 1
    assert "stand-in-value" not in done.stdout + done.stderr
    assert "rediss://" not in done.stdout + done.stderr


def test_deploy_checks_the_secret_with_the_other_preconditions_before_it_builds() -> (
    None
):
    lines = DEPLOY_SH.splitlines()
    called = lines.index("require_rate_store_secret")

    assert lines.index("require_database") < called < lines.index("build_image")


def test_deploy_waits_for_the_store_before_the_gateway_and_the_ingestion() -> None:
    lines = DEPLOY_SH.splitlines()
    store = lines.index('wait_for_deployment "${RATE_STORE_DEPLOYMENT}"')

    assert re.search(r"^readonly RATE_STORE_DEPLOYMENT=rate-store$", DEPLOY_SH, re.M)
    assert lines.index("wait_for_certificates") < store
    assert store < lines.index('wait_for_deployment "${GATEWAY_SERVICE}"')
    assert store < lines.index("ingest_corpus")
