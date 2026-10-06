"""The rate store restarts itself when its certificate was renewed (S066, T-45).

Redis reads its certificate once, at start. cert-manager renews it at two thirds
of its life and the kubelet rewrites the mounted files, but the server keeps
serving the old certificate, and when that expires every gateway call is a 503.
The six services report themselves unhealthy on a renewed file
(``meridian.platform.common.certlife``); Redis cannot, so its LIVENESS probe
does: it fails when the certificate on the volume was written after the server
process started, and the kubelet restarts the container.

These tests render the chart (``helm template``; tests/meridian/chartsupport.py)
and pin the two probes, the fixed arguments of the shell string, the time the
restart takes, and where the helpers live. They also RUN the rendered script
(with a stand-in ``redis-cli``) against a directory built the way the kubelet
builds a Secret volume, so that the rule is exercised and not only read. The
proof on the pinned Redis image, over TLS, is in the step's Part C section.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from chartsupport import CHART_DIR, helm_arguments, render

IMAGE = "redis:8.10.2-alpine@sha256:" + "ab" * 32
TLS_DIRECTORY = "/etc/meridian/tls"
PORT = 6379
TEMPLATES = CHART_DIR / "templates"
# The kubelet's name for the pointer it swaps, and for the files of a Secret.
FILES = ("tls.crt", "tls.key", "ca.crt")
# Clock ticks a second in /proc/<pid>/stat: the kernel's USER_HZ, 100 on every
# architecture Meridian runs on.
TICKS = 100
HELPERS_MAXIMUM = 800
MOVED = (
    "meridian.rateStorePort",
    "meridian.rateStoreRule",
    "meridian.usesRateStore",
    "meridian.rateStore",
)


def enabled_chart(*extra: str) -> list[dict]:
    return render(
        [
            *helm_arguments(),
            "--set",
            "rateStore.enabled=true",
            "--set-string",
            f"rateStore.image={IMAGE}",
            *extra,
        ]
    )


def store_container(*extra: str) -> dict:
    (deployment,) = [
        d
        for d in enabled_chart(*extra)
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "rate-store"
    ]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    return container


def liveness_script(*extra: str) -> str:
    return store_container(*extra)["livenessProbe"]["exec"]["command"][2]


# ── the two probes ───────────────────────────────────────────────────────────


def test_the_readiness_probe_is_the_handshake_and_the_probe_users_ping_alone() -> None:
    probe = store_container()["readinessProbe"]

    # A shell with a fixed script, as the liveness probe: the script is the
    # liveness script's first lines (test_helm_rate_store_hardening.py runs it).
    command = probe["exec"]["command"]
    assert command[:2] == ["sh", "-c"]
    assert command[3:] == ["rate-store-readiness", TLS_DIRECTORY, str(PORT)]
    assert liveness_script().startswith(command[2].rstrip("\n"))
    assert probe["timeoutSeconds"] == 3
    assert probe["periodSeconds"] == 5


def test_the_liveness_probe_is_a_shell_with_a_fixed_script_and_two_arguments() -> None:
    command = store_container()["livenessProbe"]["exec"]["command"]

    assert command[:2] == ["sh", "-c"]
    assert command[3:] == ["rate-store-liveness", TLS_DIRECTORY, str(PORT)]
    assert isinstance(command[2], str)


def test_the_liveness_script_still_does_what_the_readiness_probe_does() -> None:
    script = liveness_script()

    assert (
        "answer=$(redis-cli --tls"
        ' --cacert "$1/ca.crt" --cert "$1/tls.crt" --key "$1/tls.key"'
        " -h 127.0.0.1 -p \"$2\" --user probe --pass '' --no-auth-warning ping)"
    ) in script
    # The answer is read, not the exit status: it is 0 for NOAUTH and for BUSY.
    assert '[ "$answer" = PONG ]' in script
    for refused in ("--insecure", " -a ", "--askpass"):
        assert refused not in script


def test_no_value_of_the_chart_is_in_the_shell_string() -> None:
    changed = liveness_script(
        "--namespace",
        "somewhere-else",
        "--set",
        "certificate.duration=720h",
        "--set",
        "rateStore.maxmemory=16mb",
        "--set-string",
        "image.tag=another",
    )

    assert changed == liveness_script()
    # The directory and the port reach the script as arguments, never as text.
    assert TLS_DIRECTORY not in changed
    assert str(PORT) not in changed
    assert "{{" not in changed


def test_the_script_expands_only_its_two_arguments_and_the_numbers_it_reads() -> None:
    script = liveness_script()

    expansions = set(re.findall(r"\$(\(\(|\(|\w+)", script))

    assert expansions == {
        "1",
        "2",
        "(",
        "((",
        "answer",
        "n",
        "boot",
        "ticks",
        "written",
        "started",
    }
    # An argument is always inside double quotes: a space or a glob in it is
    # one word.
    assert not re.findall(r'(?<!")\$[12]', script)


def test_the_liveness_probe_is_read_only_and_writes_nothing() -> None:
    (deployment,) = [
        d
        for d in enabled_chart()
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "rate-store"
    ]
    pod = deployment["spec"]["template"]["spec"]
    (container,) = pod["containers"]

    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert {v["name"] for v in pod["volumes"]} == {"tls", "acl", "config"}
    assert all("emptyDir" not in v for v in pod["volumes"])
    # The only redirection is the message to standard error.
    redirections = {
        found.rstrip(";") for found in re.findall(r">\S*", liveness_script())
    }
    assert redirections <= {">&2"}


# ── how long after a renewal ─────────────────────────────────────────────────


def test_a_renewal_is_acted_on_within_a_minute_of_the_kubelet_writing_it() -> None:
    probe = store_container()["livenessProbe"]

    window = probe["periodSeconds"] * probe["failureThreshold"]

    assert probe["periodSeconds"] == 10
    assert probe["failureThreshold"] == 6
    assert window == 60
    # The failure is permanent until the restart, so the first failing probe
    # and the threshold's last are the only waits: nothing is added by retries.
    assert probe.get("successThreshold", 1) == 1


def test_a_hung_probe_ends_as_a_failed_one_before_the_next_starts() -> None:
    probe = store_container()["livenessProbe"]

    assert probe["timeoutSeconds"] == 3
    assert probe["timeoutSeconds"] < probe["periodSeconds"]


def test_the_header_states_the_time_and_what_a_restart_costs() -> None:
    text = (TEMPLATES / "rate-store.yaml").read_text(encoding="utf-8")
    header = text.split("*/ -}}", 1)[0]

    assert "60 seconds" in header
    assert "every tenant its windows again" in header
    assert "ledger" in header


# ── the rule, run against a directory the way the kubelet builds one ─────────


def process_start() -> int:
    """When PID 1 started, in whole seconds, by the rule the script states: the
    boot time plus the start in ticks, cut to the second."""
    boot = int(re.search(r"^btime (\d+)$", Path("/proc/stat").read_text(), re.M)[1])
    stat = Path("/proc/1/stat").read_text()
    ticks = int(stat.rsplit(") ", 1)[1].split(" ")[19])
    return boot + ticks // TICKS


def write_secret_volume(root: Path, stamp: str, written: int) -> None:
    """A Secret volume as the kubelet writes it: the files in ``..<stamp>``,
    ``..data`` pointing at that directory, and each file a link through
    ``..data``."""
    directory = root / f"..{stamp}"
    directory.mkdir(parents=True)
    for name in FILES:
        path = directory / name
        path.write_text(f"{stamp} {name}\n")
        os.utime(path, (written, written))
    swap(root, stamp)
    for name in FILES:
        link = root / name
        if not link.is_symlink():
            link.symlink_to(f"..data/{name}")


def swap(root: Path, stamp: str) -> None:
    """``ln -s ..<stamp> ..data_tmp; mv -T ..data_tmp ..data``: one rename."""
    pending = root / "..data_tmp"
    pending.symlink_to(f"..{stamp}")
    os.replace(pending, root / "..data")


def renew(root: Path, stamp: str, written: int) -> None:
    """What the kubelet does when the Secret changed: a new directory, then the
    pointer swapped, then the old directory removed."""
    write_secret_volume(root, stamp, written)


@pytest.fixture
def probe(tmp_path: Path):
    """Run the rendered liveness command in this process's own shell with a
    stand-in ``redis-cli`` first on the path, against ``tmp_path/tls``. Returns
    (exit status, the redis-cli arguments, standard error)."""
    shell = shutil.which("sh")
    assert shell is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "redis-cli-arguments"
    stand_in = bin_dir / "redis-cli"
    stand_in.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$RECORD"\n'
        'printf "%s\\n" "${REDIS_CLI_ANSWER-PONG}"\nexit "${REDIS_CLI_STATUS:-0}"\n'
    )
    stand_in.chmod(0o755)
    script = liveness_script()

    def run(
        redis_cli_status: int = 0, answer: str | None = None
    ) -> tuple[int, list[str], str]:
        done = subprocess.run(
            [shell, "-c", script, "rate-store-liveness", str(tmp_path / "tls"), "6379"],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "RECORD": str(record),
                "REDIS_CLI_STATUS": str(redis_cli_status),
                # Unset, the stand-in answers PONG, as a healthy store does.
                **({} if answer is None else {"REDIS_CLI_ANSWER": answer}),
            },
        )
        arguments = record.read_text().splitlines() if record.exists() else []
        return done.returncode, arguments, done.stderr

    return run


def test_a_certificate_written_before_the_server_started_is_healthy(
    tmp_path: Path, probe
) -> None:
    write_secret_volume(tmp_path / "tls", "2026_a", process_start() - 60)

    status, arguments, _ = probe()

    assert status == 0
    assert arguments[:2] == ["--tls", "--cacert"]
    assert arguments[2] == f"{tmp_path / 'tls'}/ca.crt"
    assert arguments[-8:] == [
        "-p",
        "6379",
        "--user",
        "probe",
        "--pass",
        "",
        "--no-auth-warning",
        "ping",
    ]


def test_a_certificate_written_in_the_second_the_server_started_is_healthy(
    tmp_path: Path, probe
) -> None:
    # The kubelet writes the volume before it starts the container, so a file
    # no newer than the process is how every start looks; a false alarm here
    # would restart a healthy store at its first probe, once, and then be quiet.
    write_secret_volume(tmp_path / "tls", "2026_a", process_start())

    status, _, _ = probe()

    assert status == 0


def test_a_certificate_written_after_the_server_started_is_unhealthy(
    tmp_path: Path, probe
) -> None:
    write_secret_volume(tmp_path / "tls", "2026_a", process_start() + 1)

    status, _, stderr = probe()

    assert status == 1
    assert "newer than the server" in stderr


def test_a_renewal_the_way_the_kubelet_writes_it_turns_a_healthy_probe_unhealthy(
    tmp_path: Path, probe
) -> None:
    volume = tmp_path / "tls"
    write_secret_volume(volume, "2026_a", process_start() - 3600)
    before, _, _ = probe()

    renew(volume, "2026_b", process_start() + 5)
    after, _, _ = probe()

    assert (before, after) == (0, 1)
    # The link the server reads now leads to the new directory's file.
    assert (volume / "tls.crt").read_text() == "2026_b tls.crt\n"


def test_a_server_started_after_the_renewal_is_healthy_again_with_the_same_files(
    tmp_path: Path, probe
) -> None:
    # A restart of the container loads the new files; the process is now newer
    # than they are, and nothing else changes: the probe does not loop.
    volume = tmp_path / "tls"
    write_secret_volume(volume, "2026_a", process_start() - 3600)
    renew(volume, "2026_b", process_start() - 5)

    status, _, _ = probe()

    assert status == 0


def test_the_time_read_is_the_file_the_link_leads_to_not_the_link(
    tmp_path: Path, probe
) -> None:
    volume = tmp_path / "tls"
    write_secret_volume(volume, "2026_a", process_start() - 3600)
    # A link made just now, over an old file, as a swap makes them.
    os.utime(volume / "tls.crt", (process_start() + 50,) * 2, follow_symlinks=False)

    status, _, _ = probe()

    assert status == 0


def test_a_server_that_does_not_answer_is_unhealthy_whatever_the_files_say(
    tmp_path: Path, probe
) -> None:
    write_secret_volume(tmp_path / "tls", "2026_a", process_start() - 60)

    status, _, _ = probe(redis_cli_status=1, answer="")

    assert status == 1


@pytest.mark.parametrize(
    "answer",
    [
        "NOAUTH Authentication required.",
        "BUSY Redis is busy running a script. You can only call SCRIPT KILL.",
        "WRONGPASS invalid username-password pair or user is disabled.",
    ],
    ids=["noauth", "busy", "wrongpass"],
)
def test_a_server_that_answers_anything_but_pong_is_unhealthy_with_fresh_files(
    tmp_path: Path, probe, answer: str
) -> None:
    # redis-cli exits 0 for each of these: a frozen store (BUSY) and a store
    # that lost the probe user (WRONGPASS) must not pass for healthy.
    write_secret_volume(tmp_path / "tls", "2026_a", process_start() - 60)

    status, _, stderr = probe(answer=answer)

    assert status == 1
    assert "not PONG" in stderr


def test_a_missing_certificate_file_is_unhealthy(tmp_path: Path, probe) -> None:
    (tmp_path / "tls").mkdir()

    status, _, _ = probe()

    assert status == 1


# ── the helpers' own file ────────────────────────────────────────────────────


def test_the_rate_stores_helpers_are_in_their_own_template_file() -> None:
    helpers = (TEMPLATES / "_helpers.tpl").read_text(encoding="utf-8")
    own = (TEMPLATES / "_rate-store.tpl").read_text(encoding="utf-8")

    for name in MOVED:
        assert f'{{{{- define "{name}" -}}}}' in own
        assert f'define "{name}"' not in helpers
    assert len(helpers.splitlines()) < HELPERS_MAXIMUM


def test_the_moved_helpers_are_still_found_with_the_store_on_and_off() -> None:
    # kind turns the store on (K3b), so "off" is kind's values less that switch.
    off = render([*helm_arguments(), "--set", "rateStore.enabled=false"])
    on = enabled_chart()

    assert not [d for d in off if d["metadata"]["name"] == "rate-store"]
    assert len([d for d in on if d["metadata"]["name"] == "rate-store"]) == 6


def test_the_values_comment_lists_the_gateways_commands_without_client() -> None:
    text = (CHART_DIR / "values.yaml").read_text(encoding="utf-8")
    comment = " ".join(
        line.removeprefix("#").strip()
        for line in text.split("\nrateStore:", 1)[0].splitlines()
        if line.startswith("#")
    )

    assert (
        "(EVALSHA, SCRIPT LOAD, TIME, ZREMRANGEBYSCORE, ZRANGE, ZADD, PEXPIRE, HELLO)"
        in comment
    )
    assert "SETINFO" not in text
