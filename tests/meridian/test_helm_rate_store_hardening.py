"""What the cluster reviews of the rate store asked of the chart (S066, T-45).

Three things, each pinned here and run where it can be:

* A store that is frozen by a script that never ends answers every client but
  the one that could stop it with BUSY, and a probe that only proved the
  handshake (an unauthenticated ``PING`` answers NOAUTH and ``redis-cli`` exits
  0 for NOAUTH and BUSY alike) kept the pod Ready for ever. Both probes now run
  ``PING`` as the ACL user ``probe`` (``up.sh`` writes it: no password, no key, no
  channel, exactly ``+ping``) and pass only when the OUTPUT is ``PONG``. The
  rendered scripts are run here against a stand-in ``redis-cli``; the proof on the
  pinned image (healthy, frozen, restarted) is in the step's Part C section.
* Redis's side is tighter: TLS 1.3 alone, and 1 MB for a bulk and for a client's
  query buffer, which bounds what an authenticated ``SCRIPT LOAD`` can put in
  memory. The header of the template says what no directive bounds.
* The chart refuses two more things: the store without the NetworkPolicy that is
  its only control before authentication, and a values ``env`` item that gives a
  service the store's address.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from chartsupport import SERVICES, helm_arguments, render, run_helm
from test_helm_rate_store_restart import (
    IMAGE,
    PORT,
    SCRIPT_SHELL,
    TEMPLATES,
    TLS_DIRECTORY,
    enabled_chart,
    store_container,
)

PROBE_USER = "probe"
VARIABLE = "MERIDIAN_GATEWAY_RATE_STORE_URL"
PONG_LINE = (
    'redis-cli --tls --cacert "$1/ca.crt" --cert "$1/tls.crt"'
    ' --key "$1/tls.key" -h 127.0.0.1 -p "$2"'
    f" --user {PROBE_USER} --pass '' --no-auth-warning ping &"
)
ANSWER_RULE = '[ "$answer" = PONG ] ||'


def command_of(probe: str) -> list[str]:
    return store_container()[probe]["exec"]["command"]


def failure_of(*arguments: str) -> str:
    done = run_helm(list(arguments))
    assert done.returncode != 0, "the chart rendered"
    return done.stderr


def flat_header() -> str:
    text = (TEMPLATES / "rate-store.yaml").read_text(encoding="utf-8")
    header = text.split("*/ -}}", 1)[0]
    return " ".join(line.strip() for line in header.splitlines())


# ── the probes ask the probe user for a PONG ─────────────────────────────────


@pytest.mark.parametrize("probe", ["readinessProbe", "livenessProbe"])
def test_both_probes_are_a_fixed_script_with_the_directory_and_the_port_as_arguments(
    probe: str,
) -> None:
    command = command_of(probe)

    assert command[:2] == ["sh", "-c"]
    assert command[3:] == [
        "rate-store-readiness" if probe == "readinessProbe" else "rate-store-liveness",
        TLS_DIRECTORY,
        str(PORT),
    ]
    assert "{{" not in command[2]
    assert TLS_DIRECTORY not in command[2]


@pytest.mark.parametrize("probe", ["readinessProbe", "livenessProbe"])
def test_both_probes_ping_as_the_probe_user_and_need_the_answer_pong(
    probe: str,
) -> None:
    script = command_of(probe)[2]

    assert PONG_LINE in script
    assert ANSWER_RULE in script
    # The exit status of redis-cli is 0 for NOAUTH and for BUSY: it is not read.
    assert "ping || " not in script
    # Only the probe user, no password, and nothing that opens a way round TLS.
    for refused in ("--insecure", " -a ", "--askpass", "gateway"):
        assert refused not in script


@pytest.mark.parametrize("probe", ["readinessProbe", "livenessProbe"])
def test_neither_probe_puts_a_credential_in_the_pods_spec(probe: str) -> None:
    container = store_container()

    assert "env" not in container
    assert "envFrom" not in container
    assert not re.search(r"--pass\s+'[^']", command_of(probe)[2])
    assert not re.search(r"redis(s)?://", json.dumps(container))


def test_the_readiness_probe_keeps_its_timings() -> None:
    probe = store_container()["readinessProbe"]

    assert probe["timeoutSeconds"] == 5
    assert probe["periodSeconds"] == 5


@pytest.mark.parametrize("probe", ["readinessProbe", "livenessProbe"])
def test_redis_cli_has_a_time_limit_of_its_own_inside_the_kubelets(probe: str) -> None:
    script = command_of(probe)[2]
    limit = store_container()[probe]["timeoutSeconds"]

    # A frozen process (a paused or stopped server) never answers, and the
    # kubelet's timeout fails the probe without ending what it started: a
    # client would be left behind for every probe. The script starts redis-cli
    # and `sleep SECONDS` beside each other, waits for the first to end and ends
    # the other. Not `timeout`: the image's leaves a process behind per run, and
    # not redis-cli's `-t`, which does not bound a stopped server (K7).
    seconds = re.search(r"\n  sleep (\d+) &\n", script)
    assert seconds, script
    assert 0 < int(seconds.group(1)) < limit
    assert script.count("redis-cli") == 1
    assert "\n  wait -n\n" in script
    assert "timeout" not in script
    assert " -t " not in script


def test_the_bound_is_a_sleep_started_beside_redis_cli_and_nothing_else(
    tmp_path: Path,
) -> None:
    shell = shutil.which(SCRIPT_SHELL)
    assert shell is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "sleep-arguments"
    # A stand-in sleep that records its arguments and then really sleeps, so the
    # script has to end it.
    (bin_dir / "sleep").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$RECORD"\nexec "$REAL_SLEEP" 30\n'
    )
    (bin_dir / "sleep").chmod(0o755)
    # The client answers once the sleep has recorded its arguments, so that the
    # script cannot end the sleep before it has started.
    (bin_dir / "redis-cli").write_text(
        '#!/bin/sh\nwhile [ ! -s "$RECORD" ]; do :; done\nprintf "PONG\\n"\n'
    )
    (bin_dir / "redis-cli").chmod(0o755)
    command = command_of("readinessProbe")

    done = subprocess.run(
        [shell, "-c", command[2], command[3], "/some/tls", "6379"],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "RECORD": str(record),
            "REAL_SLEEP": shutil.which("sleep") or "sleep",
        },
    )

    assert (done.returncode, done.stderr) == (0, "")
    assert record.read_text().splitlines() == ["2"]


def test_a_redis_cli_that_the_bound_ended_is_not_ready_and_is_not_left_running(
    tmp_path: Path,
) -> None:
    shell = shutil.which(SCRIPT_SHELL)
    assert shell is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "client-pid"
    # A client that never answers (a stopped server), and a bound that is over as
    # soon as the client has written its process number: the script must end the
    # client, say that nothing was answered and leave no client behind.
    (bin_dir / "redis-cli").write_text(
        '#!/bin/sh\necho $$ > "$RECORD"\nexec "$REAL_SLEEP" 30\n'
    )
    (bin_dir / "redis-cli").chmod(0o755)
    (bin_dir / "sleep").write_text(
        '#!/bin/sh\nwhile [ ! -s "$RECORD" ]; do :; done\nexit 0\n'
    )
    (bin_dir / "sleep").chmod(0o755)
    command = command_of("readinessProbe")

    done = subprocess.run(
        [shell, "-c", command[2], command[3], "/some/tls", "6379"],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "RECORD": str(record),
            "REAL_SLEEP": shutil.which("sleep") or "sleep",
        },
    )

    assert done.returncode == 1
    assert "answered '', not PONG" in done.stderr
    # The script ended the client and waited for it: the process is gone. A
    # client left running, or defunct under an exited shell, would still be there.
    with pytest.raises(ProcessLookupError):
        os.kill(int(record.read_text()), 0)


def test_the_liveness_script_asks_for_the_pong_before_the_certificates_time() -> None:
    script = command_of("livenessProbe")[2]

    assert script.index(ANSWER_RULE) < script.index("btime")


@pytest.fixture
def readiness(tmp_path: Path):
    """Run the rendered readiness command in this machine's shell with a stand-in
    ``redis-cli`` first on the path. The stand-in prints ``ANSWER`` and exits with
    ``STATUS`` and records its arguments. Returns (exit status, arguments,
    standard error)."""
    shell = shutil.which(SCRIPT_SHELL)
    assert shell is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "arguments"
    stand_in = bin_dir / "redis-cli"
    stand_in.write_text(
        '#!/bin/sh\nfor a in "$@"; do printf "[%s]\\n" "$a"; done > "$RECORD"\n'
        'printf "%s" "$ANSWER"\nexit "${STATUS:-0}"\n'
    )
    stand_in.chmod(0o755)
    command = command_of("readinessProbe")

    def run(answer: str, status: int = 0) -> tuple[int, list[str], str]:
        done = subprocess.run(
            [shell, "-c", command[2], command[3], "/some/tls", "6379"],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "RECORD": str(record),
                "ANSWER": answer,
                "STATUS": str(status),
            },
        )
        arguments = record.read_text().splitlines() if record.exists() else []
        return done.returncode, arguments, done.stderr

    return run


def test_a_pong_is_ready(readiness) -> None:
    status, arguments, stderr = readiness("PONG\n")

    assert (status, stderr) == (0, "")
    assert arguments == [
        "[--tls]",
        "[--cacert]",
        "[/some/tls/ca.crt]",
        "[--cert]",
        "[/some/tls/tls.crt]",
        "[--key]",
        "[/some/tls/tls.key]",
        "[-h]",
        "[127.0.0.1]",
        "[-p]",
        "[6379]",
        "[--user]",
        f"[{PROBE_USER}]",
        "[--pass]",
        "[]",
        "[--no-auth-warning]",
        "[ping]",
    ]


@pytest.mark.parametrize(
    "answer",
    [
        "NOAUTH Authentication required.\n",
        "BUSY Redis is busy running a script. You can only call SCRIPT KILL.\n",
        "WRONGPASS invalid username-password pair or user is disabled.\n",
        "NOPERM User probe has no permissions to run the 'ping' command\n",
        "pong\n",
        "PONG PONG\n",
        "",
    ],
    ids=["noauth", "busy", "wrongpass", "noperm", "lower-case", "two-words", "nothing"],
)
def test_any_answer_but_pong_is_not_ready_though_redis_cli_exits_zero(
    readiness, answer: str
) -> None:
    status, _, stderr = readiness(answer, status=0)

    assert status == 1
    assert "not PONG" in stderr


def test_a_server_that_does_not_connect_is_not_ready(readiness) -> None:
    status, _, stderr = readiness("", status=1)

    assert status == 1
    assert "not PONG" in stderr


def test_the_refusal_says_what_was_answered_so_the_pods_events_show_a_freeze(
    readiness,
) -> None:
    _, _, stderr = readiness("BUSY Redis is busy running a script.\n")

    assert "BUSY Redis is busy running a script." in stderr


# ── Redis's side ─────────────────────────────────────────────────────────────


def directives() -> dict[str, str]:
    (config,) = [
        d
        for d in enabled_chart()
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "rate-store"
    ]
    found = {}
    for line in config["data"]["redis.conf"].splitlines():
        name, _, value = line.partition(" ")
        found[name] = value
    return found


def test_the_server_negotiates_tls_one_three_alone() -> None:
    assert directives()["tls-protocols"] == '"TLSv1.3"'


def test_a_bulk_and_a_query_buffer_are_one_megabyte() -> None:
    found = directives()

    # The gateway's script is about 2 KB: a megabyte is far above it and far
    # below what a 100 MB SCRIPT LOAD would put in memory.
    assert found["proto-max-bulk-len"] == "1mb"
    assert found["client-query-buffer-limit"] == "1mb"


def megabytes(value: str) -> int:
    assert value.endswith("mb"), value
    return int(value.removesuffix("mb"))


def test_every_clients_buffers_together_are_bounded_well_under_the_pod_limit() -> None:
    found = directives()
    limit = store_container()["resources"]["limits"]["memory"]

    # 256 clients at the 1 MB each may hold is 256 MB, four pod limits: the
    # flood of the third infra review killed the store with 240 of them.
    assert "maxmemory-clients" in found
    assert megabytes(found["maxmemory-clients"]) == 8
    assert megabytes(found["maxmemory-clients"]) < megabytes(found["maxmemory"])
    assert limit.endswith("Mi")
    assert megabytes(found["maxmemory-clients"]) < int(limit.removesuffix("Mi"))


def test_the_header_gives_the_reason_of_the_aggregate_bound() -> None:
    flat = flat_header()

    assert "maxmemory-clients" in flat
    after = flat.split("maxmemory-clients", 1)[1]
    # What it bounds, what it is for, and what the single-client limits cannot do.
    assert "client-query-buffer-limit" in flat
    assert "together" in after
    assert "evict" in after
    assert "1 MB" in after or "1mb" in after


def test_the_header_no_longer_says_the_probe_user_can_only_ask() -> None:
    flat = flat_header()

    assert "all it can do is ask" not in flat
    # It may only run PING, and its session is an authenticated one that any
    # holder of a services certificate with a path to the port can open: what
    # bounds that is the NetworkPolicy and the aggregate bound on the buffers.
    assert "may only run PING" in flat
    assert "authenticated" in flat
    assert "NetworkPolicy and maxmemory-clients" in flat


def test_the_header_gives_the_reasons_of_the_three_directives() -> None:
    flat = flat_header()

    for directive in ("proto-max-bulk-len", "client-query-buffer-limit"):
        assert directive in flat
    assert "tls-protocols" in flat
    assert "TLS 1.3 alone" in flat
    assert "SCRIPT LOAD" in flat


def test_the_header_says_plainly_what_no_directive_bounds() -> None:
    flat = flat_header()

    assert "No directive bounds" in flat
    after = flat.split("No directive bounds", 1)[1]
    assert "script that writes without end" in after
    assert "memory limit" in after
    assert "restarts the store" in after
    assert "every tenant its windows again" in after


def test_the_header_states_the_limits_of_the_probes_it_accepts() -> None:
    flat = flat_header()

    assert "enough idle connections" in flat
    assert "expired unrenewed" in flat
    assert "stepped back" in flat
    assert "a step forward hides a renewal" in flat


def test_the_header_says_a_second_user_exists_and_how_a_freeze_ends() -> None:
    flat = flat_header()

    assert "no other user exists" not in flat
    assert "other user exists" not in flat
    assert "probe" in flat
    # The probe user, what it may do and how a freeze ends.
    assert "+ping" in flat
    assert "BUSY" in flat
    assert "within about a minute" in flat


# ── the chart refuses two more things ────────────────────────────────────────


def test_the_store_without_a_network_policy_is_refused_and_says_why() -> None:
    stderr = failure_of(
        *helm_arguments(),
        "--set",
        "rateStore.enabled=true",
        "--set",
        "networkPolicy.enabled=false",
    )

    assert "rateStore.enabled" in stderr
    assert "networkPolicy.enabled" in stderr
    assert "before authentication" in stderr


def test_the_store_with_its_network_policy_renders() -> None:
    store = [d for d in enabled_chart() if d["metadata"]["name"] == "rate-store"]

    assert "NetworkPolicy" in {d["kind"] for d in store}


def test_without_the_store_the_network_policy_may_be_off() -> None:
    documents = render(
        [
            *helm_arguments(),
            "--set",
            "rateStore.enabled=false",
            "--set",
            "networkPolicy.enabled=false",
        ]
    )

    assert not [d for d in documents if d["kind"] == "NetworkPolicy"]


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("enabled", ["true", "false"])
def test_a_values_env_item_with_the_stores_address_is_refused_for_every_service(
    service: str, enabled: str
) -> None:
    item = json.dumps([{"name": VARIABLE, "value": "rediss://u:p@somewhere:6379/0"}])

    stderr = failure_of(
        *helm_arguments(),
        "--set",
        f"rateStore.enabled={enabled}",
        "--set-string",
        f"rateStore.image={IMAGE}",
        "--set-json",
        f"services.{service}.env={item}",
    )

    assert VARIABLE in stderr
    assert f"services.{service}" in stderr
    assert "Secret" in stderr


def test_an_env_item_of_another_name_still_renders() -> None:
    item = json.dumps([{"name": "SOME_OTHER_VARIABLE", "value": "x"}])

    documents = render(
        [
            *helm_arguments(),
            "--set-json",
            f"services.claims-api.env={item}",
        ]
    )

    assert documents
