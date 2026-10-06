"""The gateway's own client runs under the access list `make up` writes (S066, T-45).

``infra/kind/up.sh`` makes the rate store's Secret with an ACL file: the gateway's
user may run exactly the commands its client and script send, on
``meridian:rate:*``. ``test_kind_rate_store_secret.py`` compares that list with
what the code is believed to send; nothing there runs the client. A client
upgrade that added one command to its handshake would be refused by the store
(``NOPERM``), the gateway would answer every model call with a 503, and every test
would stay green (the infra review's M2). These tests close that: they make a user
on the test Redis with EXACTLY the rules ``up.sh`` writes for the gateway's (read
out of the function's own output, so the two cannot drift), run the gateway's
client as that user, and read the server's ``ACL LOG``.

What is left out of the gateway's connection: TLS. The test Redis is a plain
server, so ``_PinnedContextConnection`` is replaced by redis-py's plain
connection and everything else is ``rate_store_client`` as the gateway runs it
(the timeouts, no retry, no client info, no maintenance notifications, the
user and the password from the address). What TLS adds is not a command.
"""

import re
import secrets
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import redis
import yaml
from redistlssupport import make_pki
from servicesupport import REPO_ROOT

from meridian.platform.gateway import rate_store
from meridian.platform.gateway.ratelimit import RateStoreUnavailable
from meridian.platform.gateway.ratelimit_redis import DEFAULT_PREFIX, RedisRateLimiter
from meridian.platform.gateway.settings import parse_rate_store_url
from meridian.platform.registry.models import TenantLimits

KIND_DIR = REPO_ROOT / "infra" / "kind"
UP_SH = (KIND_DIR / "up.sh").read_text(encoding="utf-8")
GATEWAY_USER_IN_UP_SH = "gateway"
LIMITS = TenantLimits(
    requests_per_10_seconds=100,
    tokens_per_minute=100_000,
    tokens_per_day=10**9,
    cost_per_month_eur=Decimal(1),
)
# A hash the server does not know: the client then goes the way it goes after a
# SCRIPT FLUSH (NOSCRIPT, SCRIPT LOAD, EVALSHA) without touching the scripts of
# the tests that share this Redis.
UNKNOWN_SCRIPT = "0" * 40
ACL_LOG_MAX_LEN = 128  # Redis's default acllog-max-len: every entry it keeps


def function_text(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", UP_SH, re.MULTILINE | re.DOTALL)
    assert match, f"no function {name} in up.sh"
    return match.group(0)


def acl_of_up_sh(tmp_path: Path) -> tuple[list[str], str]:
    """The gateway's rules as ``ensure_rate_store_secret`` writes them, and the
    password they hash: the function in bash against a stub ``kctl`` that keeps
    what is piped to ``create``."""
    created = tmp_path / "created.yaml"
    script = "\n".join(
        [
            "set -euo pipefail",
            f'. "{KIND_DIR / "common.sh"}"',
            *re.findall(r"^readonly RATE_STORE_\w+=.*$", UP_SH, re.MULTILINE),
            "kctl() {",
            '  case "$*" in',
            "    *get\\ secret*) return 1 ;;",
            f'    *"create -f -"*) cat >"{created}" ;;',
            '    *) echo "unexpected kctl $*" >&2; return 1 ;;',
            "  esac",
            "}",
            function_text("ensure_rate_store_secret"),
            "ensure_rate_store_secret",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    secret = yaml.safe_load(created.read_text(encoding="utf-8"))
    password = parse_rate_store_url(secret["stringData"]["uri"]).password
    lines = secret["stringData"]["users.acl"].splitlines()
    (line,) = [
        words
        for words in (text.split() for text in lines)
        if words[:2] == ["user", GATEWAY_USER_IN_UP_SH]
    ]
    return line[2:], password


@dataclass
class GatewayUser:
    """A user of the test Redis with the gateway's rules, a client builder for it
    and its tenant (a key under ``meridian:rate:`` no other test shares)."""

    name: str
    rules: list[str]
    password: str
    tenant: str
    admin: redis.Redis
    limiter: Callable[[], RedisRateLimiter]

    def acl_log(self) -> list[dict[str, Any]]:
        """The server's ACL LOG entries of this user alone (the log is the
        server's, and the tests of a run share it)."""
        return [
            {_text(k): _text(v) for k, v in entry.items()}
            # Without a count the server returns its ten latest entries, and the
            # other workers of a run write to the same log: ask for all it keeps.
            for entry in self.admin.acl_log(count=ACL_LOG_MAX_LEN)
            if _text(entry["username"]) == self.name
        ]

    def set_rules(self, rules: list[str]) -> None:
        self.admin.execute_command("ACL", "SETUSER", self.name, "reset", *rules)


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class _PlainConnection(redis.Connection):
    """``_PinnedContextConnection`` without TLS: the same keyword arguments."""

    def __init__(self, *, ssl_context: object, **kwargs: Any) -> None:
        super().__init__(**kwargs)


@pytest.fixture
def gateway_user(
    redis_client: redis.Redis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[GatewayUser]:
    monkeypatch.setattr(rate_store, "_PinnedContextConnection", _PlainConnection)
    rules, password = acl_of_up_sh(tmp_path)
    name = f"acltest-{secrets.token_hex(6)}"
    tenant = f"acltest-{secrets.token_hex(6)}"
    tls = make_pki(tmp_path).tls()
    server = redis_client.connection_pool.connection_kwargs
    url = f"rediss://{name}:{password}@{server['host']}:{server['port']}/0"
    clients: list[redis.Redis] = []

    def limiter() -> RedisRateLimiter:
        client = rate_store.rate_store_client(url, tls)
        clients.append(client)
        return RedisRateLimiter(client)

    user = GatewayUser(name, rules, password, tenant, redis_client, limiter)
    try:
        user.set_rules(rules)
        yield user
    finally:
        for client in clients:
            client.close()
        redis_client.delete(f"{DEFAULT_PREFIX}:{tenant}")
        redis_client.execute_command("ACL", "DELUSER", name)


def admit(limiter: RedisRateLimiter, user: GatewayUser) -> object:
    return limiter.admit(user.tenant, LIMITS, 1)


# ── the rules are the ones up.sh writes ─────────────────────────────────────
def test_the_server_holds_the_user_up_sh_describes_on_with_a_password_and_the_keys(
    gateway_user: GatewayUser,
) -> None:
    reply = gateway_user.admin.execute_command("ACL", "GETUSER", gateway_user.name)
    granted = {_text(field): value for field, value in reply.items()}
    flags = [_text(flag) for flag in granted["flags"]]

    assert gateway_user.rules[0] == "on"
    assert "on" in flags
    assert "nopass" not in flags
    assert _text(granted["keys"]) == f"~{DEFAULT_PREFIX}:*"
    assert "+evalsha" in gateway_user.rules
    assert "+script|load" in gateway_user.rules


# ── the gateway's client, as that user ──────────────────────────────────────
def test_a_cold_call_a_warm_call_and_a_call_after_the_script_was_flushed_are_admitted(
    gateway_user: GatewayUser,
) -> None:
    limiter = gateway_user.limiter()

    cold = admit(limiter, gateway_user)
    warm = admit(limiter, gateway_user)
    # The server forgets its scripts, as a restart or a SCRIPT FLUSH makes it.
    gateway_user.admin.script_flush()
    after_flush = admit(limiter, gateway_user)

    assert (cold, warm, after_flush) == (None, None, None)
    assert gateway_user.admin.zcard(limiter.key_for(gateway_user.tenant)) == 3
    assert gateway_user.acl_log() == []


def test_the_server_logs_no_denial_for_the_gateways_client(
    gateway_user: GatewayUser,
) -> None:
    limiter = gateway_user.limiter()
    admit(limiter, gateway_user)
    admit(limiter, gateway_user)
    limiter._script.sha = UNKNOWN_SCRIPT
    admit(limiter, gateway_user)
    gateway_user.limiter()  # a second client: a second connection's handshake
    admit(gateway_user.limiter(), gateway_user)

    assert gateway_user.acl_log() == []


# ── the negative control: the test can fail ─────────────────────────────────
def test_a_user_without_zadd_is_refused_and_the_log_names_the_command(
    gateway_user: GatewayUser,
) -> None:
    gateway_user.set_rules([r for r in gateway_user.rules if r != "+zadd"])
    limiter = gateway_user.limiter()

    with pytest.raises(RateStoreUnavailable) as raised:
        admit(limiter, gateway_user)

    entries = gateway_user.acl_log()
    assert len(entries) == 1
    assert (entries[0]["object"], entries[0]["reason"]) == ("zadd", "command")
    assert entries[0]["context"] == "lua"  # refused inside the script
    assert "zadd" not in str(raised.value).lower()  # the store's text is not carried


def test_every_command_of_the_list_but_hello_is_one_the_client_needs(
    gateway_user: GatewayUser,
) -> None:
    # Take one command at a time from the user and go the long way (the script
    # unknown to the server, so the client sends SCRIPT LOAD too): the call must
    # fail and the log must name that command. So no grant of the list is idle
    # (a command added to the list without the client sending it fails here) and
    # the positive test above is one that can fail.
    #
    # HELLO is left out: the server checks it as the connection's user BEFORE the
    # AUTH folded into it takes effect, so taking it from the gateway's user
    # changes nothing. Measured on Redis 8.10.2 also with the default user off
    # (the store's setting): the call is admitted and the log is empty, so
    # ``+hello`` in up.sh's list is idle. Left in the list, and out of this loop.
    commands = [r[1:] for r in gateway_user.rules if r.startswith("+")]
    assert {"evalsha", "script|load", "zadd"} <= set(commands)
    needed = [command for command in commands if command != "hello"]
    for command in needed:
        gateway_user.set_rules([r for r in gateway_user.rules if r != f"+{command}"])
        limiter = gateway_user.limiter()
        limiter._script.sha = UNKNOWN_SCRIPT
        logged_before = {e["object"] for e in gateway_user.acl_log()}

        with pytest.raises(RateStoreUnavailable):
            admit(limiter, gateway_user)

        logged_after = {e["object"] for e in gateway_user.acl_log()}
        assert logged_after - logged_before == {command}, command
