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

import base64
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
PROBE_USER = "probe"
STORE_PORT = 6379
# The password: 32 random bytes as 64 lower-case hex digits, an alphabet that
# needs no percent-encoding in an address.
PASSWORD_LENGTH = 64
PASSWORD_ALPHABET = re.compile(r"[0-9a-f]{64}")


# The annotation `make up` puts on the Secret and `make deploy` compares (common.sh):
# the SHA-256 of the ACL file with every password hash (#<64 hex>) masked, so it
# holds the users, their command lists and their key patterns and no secret.
ANNOTATION = "meridian.kind/rate-store-acl-rules"
PASSWORD_HASH = re.compile(r"#[0-9a-f]{64}")


def rules_hash(acl: str) -> str:
    return hashlib.sha256(PASSWORD_HASH.sub("#<hash>", acl).encode()).hexdigest()


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
    tmp_path: Path,
    *,
    exists: bool = False,
    commands: str | None = None,
    trace: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """``ensure_rate_store_secret`` of up.sh in bash against a stub ``kctl`` that
    knows the Secret only when ``exists``. ``commands`` replaces the gateway's
    command list in the constants, as a changed ``up.sh`` would; ``trace`` runs
    the function under ``set -x``, as ``bash -x`` would. Returns the
    process, what ``create`` read from standard input and every ``kctl`` call's
    arguments."""
    created = tmp_path / "created.yaml"
    calls = tmp_path / "calls"
    calls.touch()
    answer = "return 0" if exists else "return 1"
    constants = constants_of(UP_SH)
    if commands is not None:
        constants = [
            f"readonly RATE_STORE_COMMANDS='{commands}'"
            if line.startswith("readonly RATE_STORE_COMMANDS=")
            else line
            for line in constants
        ]
    script = "\n".join(
        [
            "set -euo pipefail",
            f'. "{COMMON_SH}"',
            *constants,
            "kctl() {",
            f'  printf "%s\\n" "$*" >>"{calls}"',
            '  case "$*" in',
            f'    *"get secret {SECRET_NAME}") {answer} ;;',
            f'    *"create -f -"*) cat >>"{created}" ;;',
            '    *) echo "unexpected kctl $*" >&2; return 1 ;;',
            "  esac",
            "}",
            function_text(UP_SH, "ensure_rate_store_secret"),
            *(["set -x"] if trace else []),
            "ensure_rate_store_secret",
            # What the caller finds after the function: is tracing on again?
            'case "$-" in *x*) echo "tracing: on" ;; *) echo "tracing: off" ;; esac',
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
    assert secret["metadata"] == {
        "name": SECRET_NAME,
        "namespace": "meridian",
        "annotations": {ANNOTATION: rules_hash(secret["stringData"]["users.acl"])},
    }
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


@pytest.mark.parametrize("exists", [False, True], ids=["created", "kept"])
def test_tracing_is_off_inside_the_function_and_on_again_when_it_returns(
    tmp_path: Path, exists: bool
) -> None:
    done, created, _ = run_ensure(tmp_path, exists=exists, trace=True)

    assert done.returncode == 0, done.stderr
    assert "tracing: on" in done.stdout
    # Off inside: the trace of the function shows no assignment of the password
    # or of its hash, and the manifest that holds the address never reaches it.
    assert "password=" not in done.stderr
    assert "digest=" not in done.stderr
    assert "rediss://" not in done.stderr
    if created:
        password = parse_rate_store_url(yaml.safe_load(created)["stringData"]["uri"])
        assert password.password not in done.stderr


def test_a_run_without_tracing_is_not_traced_by_the_function(tmp_path: Path) -> None:
    done, _, _ = run_ensure(tmp_path)

    assert done.returncode == 0, done.stderr
    assert "tracing: off" in done.stdout
    assert done.stderr == ""


def test_the_function_says_why_tracing_is_off_inside_it() -> None:
    body = function_text(UP_SH, "ensure_rate_store_secret")

    assert "set +x" in body
    assert "set -x" in body
    # The comment on the switch names the password it keeps out of a trace.
    assert re.search(r"set \+x[^\n]*#[^\n]*password", body)


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


def test_the_acl_file_turns_the_default_user_off_and_names_the_gateways_and_the_probes(
    tmp_path: Path,
) -> None:
    _, _, acl = made(tmp_path)

    users = acl_rules(acl)
    assert list(users) == ["default", GATEWAY_USER, PROBE_USER]
    assert users["default"] == ["off"]
    assert users[GATEWAY_USER][0] == "on"
    assert users[PROBE_USER][0] == "on"
    assert acl.endswith("\n")


def test_the_probe_user_has_no_password_no_key_no_channel_and_exactly_ping(
    tmp_path: Path,
) -> None:
    _, _, acl = made(tmp_path)

    rules = acl_rules(acl)[PROBE_USER]
    # `nopass`: reachable only by a client that holds a certificate of the
    # services' CA and has a network path, and what it can do is ask.
    assert rules == [
        "on",
        "nopass",
        "resetkeys",
        "resetchannels",
        "-@all",
        "+ping",
    ]
    assert not [rule for rule in rules if rule.startswith(("#", ">", "<", "!", "~"))]


def test_the_probe_user_cannot_run_what_the_gateways_user_runs(tmp_path: Path) -> None:
    _, _, acl = made(tmp_path)

    users = acl_rules(acl)
    gateway = {rule for rule in users[GATEWAY_USER] if rule.startswith("+")}
    probe = {rule for rule in users[PROBE_USER] if rule.startswith("+")}

    assert probe == {"+ping"}
    assert not probe & gateway
    # And the gateway's user still cannot ping: it is the probe's command alone.
    assert "+ping" not in gateway


def test_the_acl_file_names_the_user_the_charts_probes_ping_as(tmp_path: Path) -> None:
    _, _, acl = made(tmp_path)
    (deployment,) = [
        d
        for d in rendered_chart()
        if d["kind"] == "Deployment" and d["metadata"]["name"] == RATE_STORE
    ]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]

    for probe in ("readinessProbe", "livenessProbe"):
        script = container[probe]["exec"]["command"][2]
        assert f"--user {PROBE_USER} --pass ''" in script
    assert PROBE_USER in acl_rules(acl)
    assert re.findall(r"^readonly RATE_STORE_PROBE_USER=(\S+)$", UP_SH, re.M) == [
        PROBE_USER
    ]


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
            f'. "{COMMON_SH}"',
            "die() { printf 'error: %s\\n' \"$*\" >&2; exit 1; }",
            "NAMESPACE=meridian",
            *constants_of(DEPLOY_SH),
            "kctl() {",
            '  case "$*" in',
            f'    *"get secret {SECRET_NAME} -o json"*) {reply} ;;',
            '    *) echo "unexpected kctl $*" >&2; return 1 ;;',
            "  esac",
            "}",
            function_text(DEPLOY_SH, "rate_store_expected_acl_hash"),
            function_text(DEPLOY_SH, "require_rate_store_secret"),
            "require_rate_store_secret",
        ]
    )
    return bash(script, tmp_path)


def secret_with(annotation: str | None = None, **data: str) -> dict:
    metadata = {} if annotation is None else {"annotations": {ANNOTATION: annotation}}
    return {"metadata": metadata, "data": data}


def secret_as_made(tmp_path: Path, *, commands: str | None = None) -> dict:
    """The Secret ``make up`` makes, as the API server returns it: the values
    base64-encoded under ``data`` and the annotations as they were written."""
    done, created, _ = run_ensure(tmp_path, commands=commands)
    assert done.returncode == 0, done.stderr
    secret = yaml.safe_load(created)
    return {
        "metadata": {"annotations": secret["metadata"]["annotations"]},
        "data": {
            key: base64.b64encode(value.encode()).decode()
            for key, value in secret["stringData"].items()
        },
    }


def test_deploy_goes_on_when_the_secret_is_what_make_up_makes_now(
    tmp_path: Path,
) -> None:
    done = run_require(tmp_path, answer=secret_as_made(tmp_path))

    assert done.returncode == 0, done.stderr
    assert done.stdout + done.stderr == ""


def test_deploy_goes_on_for_a_secret_made_with_another_password(
    tmp_path: Path,
) -> None:
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first = secret_as_made(tmp_path / "first")
    second = secret_as_made(tmp_path / "second")

    # Two runs, two passwords, two hashes in the file: one annotation.
    assert first["data"]["users.acl"] != second["data"]["users.acl"]
    assert (
        first["metadata"]["annotations"][ANNOTATION]
        == second["metadata"]["annotations"][ANNOTATION]
    )
    assert run_require(tmp_path, answer=second).returncode == 0


def test_the_annotation_masks_every_password_hash_and_holds_no_secret(
    tmp_path: Path,
) -> None:
    secret, uri, acl = made(tmp_path)

    annotation = secret["metadata"]["annotations"][ANNOTATION]
    password = parse_rate_store_url(uri).password
    assert annotation == rules_hash(acl)
    assert re.fullmatch(r"[0-9a-f]{64}", annotation)
    # Not the password, and not the hash that the ACL file holds of it.
    assert annotation != hashlib.sha256(password.encode()).hexdigest()
    assert password not in annotation
    # The hash of the masked text is the one the other password gives too.
    assert PASSWORD_HASH.search(acl)
    assert not PASSWORD_HASH.search(PASSWORD_HASH.sub("#<hash>", acl))


def test_the_annotation_changes_with_a_command_list_a_key_pattern_or_a_user(
    tmp_path: Path,
) -> None:
    names = ("same", "more-commands", "fewer-commands")
    for name in names:
        (tmp_path / name).mkdir()
    same = secret_as_made(tmp_path / "same")["metadata"]["annotations"][ANNOTATION]
    more = secret_as_made(
        tmp_path / "more-commands",
        commands="+evalsha +script|load +time +zremrangebyscore +zrange +zadd "
        "+pexpire +hello +keys",
    )["metadata"]["annotations"][ANNOTATION]
    fewer = secret_as_made(
        tmp_path / "fewer-commands",
        commands="+evalsha +script|load +time +zremrangebyscore +zrange +zadd +pexpire",
    )["metadata"]["annotations"][ANNOTATION]

    assert len({same, more, fewer}) == 3


def test_deploy_refuses_a_secret_made_with_another_command_list_and_says_what_to_do(
    tmp_path: Path,
) -> None:
    (tmp_path / "old").mkdir()
    old = secret_as_made(
        tmp_path / "old",
        commands="+evalsha +script|load +time +zremrangebyscore +zrange +zadd "
        "+pexpire +hello +client|setinfo",
    )

    done = run_require(tmp_path, answer=old)

    assert done.returncode == 1
    assert f"Secret {SECRET_NAME}" in done.stderr
    assert "ACL file" in done.stderr
    assert "than 'make up' writes now" in done.stderr
    # What to do, in the runbook's order.
    for step in (
        f"kubectl -n meridian delete secret {SECRET_NAME}",
        "run 'make up'",
        "restart the rate store",
        "then the Model Gateway",
        "docs/operations/runbooks/rate-store.md",
    ):
        assert step in done.stderr, step
    flat = done.stderr
    assert flat.index("delete secret") < flat.index("run 'make up'")
    assert flat.index("run 'make up'") < flat.index("restart the rate store")
    assert flat.index("restart the rate store") < flat.index("then the Model Gateway")


def test_deploy_refuses_a_secret_from_before_the_probe_user_it_has_no_annotation(
    tmp_path: Path,
) -> None:
    older = secret_as_made(tmp_path)
    del older["metadata"]["annotations"]

    done = run_require(tmp_path, answer=older)

    assert done.returncode == 1
    assert f"Secret {SECRET_NAME}" in done.stderr
    assert ANNOTATION in done.stderr
    assert "probe" in done.stderr
    assert f"kubectl -n meridian delete secret {SECRET_NAME}" in done.stderr
    assert "docs/operations/runbooks/rate-store.md" in done.stderr


def test_deploy_refuses_an_annotation_that_is_not_a_hash_of_the_current_rules(
    tmp_path: Path,
) -> None:
    secret = secret_as_made(tmp_path)
    secret["metadata"]["annotations"][ANNOTATION] = "0" * 64

    done = run_require(tmp_path, answer=secret)

    assert done.returncode == 1
    assert "than 'make up' writes now" in done.stderr


def test_the_refusal_prints_neither_the_annotation_nor_what_the_secret_holds(
    tmp_path: Path,
) -> None:
    secret = secret_as_made(tmp_path)
    annotation = secret["metadata"]["annotations"][ANNOTATION]
    secret["metadata"]["annotations"][ANNOTATION] = "1" * 64

    done = run_require(tmp_path, answer=secret)

    assert done.returncode == 1
    shown = done.stdout + done.stderr
    assert annotation not in shown
    assert "1" * 64 not in shown
    assert "rediss://" not in shown


def test_up_and_deploy_hold_the_same_rules_the_acl_is_made_from() -> None:
    names = (
        "RATE_STORE_USER",
        "RATE_STORE_KEY_PATTERN",
        "RATE_STORE_COMMANDS",
        "RATE_STORE_PROBE_USER",
        "RATE_STORE_PROBE_COMMANDS",
    )

    def lines(script: str) -> list[str]:
        return [
            line
            for line in constants_of(script)
            if line.split("=", 1)[0].removeprefix("readonly ") in names
        ]

    # deploy.sh cannot read up.sh's constants (they stay where the tests of the
    # gateway's rules read them), so it holds a copy: a test, not a hope.
    assert len(lines(UP_SH)) == len(names)
    assert lines(DEPLOY_SH) == lines(UP_SH)


def test_deploys_expected_hash_is_the_one_up_writes_for_the_same_rules(
    tmp_path: Path,
) -> None:
    # deploy.sh builds the ACL text with a placeholder where the password's hash
    # goes and hashes it masked: one function of up.sh's text, in the other script.
    script = "\n".join(
        [
            "set -euo pipefail",
            f'. "{COMMON_SH}"',
            *constants_of(DEPLOY_SH),
            function_text(DEPLOY_SH, "rate_store_expected_acl_hash"),
            "rate_store_expected_acl_hash",
        ]
    )
    done = bash(script, tmp_path)
    (tmp_path / "up").mkdir()

    assert done.returncode == 0, done.stderr
    assert (
        done.stdout.strip()
        == made(tmp_path / "up")[0]["metadata"]["annotations"][ANNOTATION]
    )


def test_up_annotates_the_secret_it_creates_and_deploy_reads_the_same_name() -> None:
    (name,) = re.findall(
        r"^readonly RATE_STORE_ACL_ANNOTATION=(\S+)$",
        COMMON_SH.read_text(encoding="utf-8"),
        re.MULTILINE,
    )

    assert name == ANNOTATION
    assert "RATE_STORE_ACL_ANNOTATION" in function_text(
        UP_SH, "ensure_rate_store_secret"
    )
    assert "RATE_STORE_ACL_ANNOTATION" in function_text(
        DEPLOY_SH, "require_rate_store_secret"
    )


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
