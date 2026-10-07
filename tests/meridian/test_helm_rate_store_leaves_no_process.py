"""The rate store's probes leave no process behind (S073, K7).

Both probes ran ``answer=$(timeout 2 redis-cli ...)``. The pinned image's
``timeout`` is busybox's: it left one process behind per probe, which became an
orphan of PID 1, the Redis server, and the server never reaps a child that is not
its own. On kind the container held 2,024 defunct ``timeout`` processes after
about two hours, every probe failed with "can't fork", and the store went not
Ready.

A stand-in ``redis-cli`` cannot show that: the processes that were left behind
belong to the shell, the image's ``timeout`` and the server above them. These
tests start the pinned image the way the pod does (``redis-server`` as PID 1, no
init and no shell above it, a read-only root file system), with the chart's own
configuration, over TLS, run the rendered probe scripts in it many times, and
list every process in the container but PID 1 afterwards. They need Docker; a
missing Docker skips them, unless ``MERIDIAN_REQUIRE_DB=1`` (CI), where it is a
failure: a test that quietly skipped would prove nothing.
"""

import os
import re
import select
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_helm_rate_store_restart import TLS_DIRECTORY, enabled_chart, store_container
from tlssupport import loopback_sans, make_ca

MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"
# Test containers are named with this prefix and unique, so that parallel
# workers never share one and a leftover is plain to see.
CONTAINER_PREFIX = "s073k7-probe-"
READY_LINE = "Ready to accept connections"
START_SECONDS = 60
# The ACL `make up` writes for the probe user, and one that lacks `+ping`, so
# the server answers NOPERM (an answer that is not PONG, with exit status 0).
PROBE_USER = "user probe on nopass resetkeys resetchannels -@all"
PROBE_ACL = f"user default off\n{PROBE_USER} +ping\n"
REFUSING_ACL = f"user default off\n{PROBE_USER}\n"
# Every process in the container but PID 1 and this shell, as "<state> <name>".
# The glob is expanded before the loop's own children exist.
LIST_PROCESSES = (
    'for p in /proc/[0-9]*; do n=${p#/proc/}; if [ "$n" != 1 ] && [ "$n" != "$$" ];'
    " then sed 's/^[0-9]* (\\(.*\\)) \\(.\\) .*/\\2 \\1/' \"$p/stat\"; fi;"
    " done 2>/dev/null"
)


@dataclass(frozen=True)
class Case:
    name: str
    acl: str
    frozen: bool
    runs: int
    status: int
    message: str


# A frozen server makes every run last the probe's own bound, so one run is the
# case; the others are cheap, and a leak of one process per run shows in twenty.
CASES = [
    Case("pong", PROBE_ACL, False, 20, 0, ""),
    Case("another-answer", REFUSING_ACL, False, 20, 1, "answered 'NOPERM"),
    Case("frozen-server", PROBE_ACL, True, 1, 1, "answered '', not PONG"),
]


def docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(["docker", "info"], capture_output=True, check=False).returncode
        == 0
    )


def pinned_image() -> str:
    found = re.search(r"^PYTEST_REDIS_IMAGE\s*:=\s*(\S+)$", MAKEFILE.read_text(), re.M)
    assert found, "the Makefile pins no PYTEST_REDIS_IMAGE"
    return found[1]


def docker(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, check=False
    )


def wait_until_ready(server: subprocess.Popen[bytes]) -> None:
    """Read the server's output until it accepts connections: a blocking read
    with a deadline, not a sleep. Reads the descriptor itself: a buffered reader
    would hold the line back from ``select``."""
    assert server.stdout is not None
    descriptor = server.stdout.fileno()
    seen = b""
    while READY_LINE.encode() not in seen:
        waiting, _, _ = select.select([descriptor], [], [], START_SECONDS)
        chunk = os.read(descriptor, 4096) if waiting else b""
        if not chunk:
            pytest.fail(
                "the store did not start: " + seen[-400:].decode(errors="replace")
            )
        seen += chunk


def make_files(directory: Path, acl: str) -> tuple[Path, Path, Path]:
    """The store's TLS files (its certificate is the probe's client certificate
    too, as in the pod), the chart's own redis.conf and an ACL file."""
    tls = directory / "tls"
    tls.mkdir()
    authority = make_ca(tls, "ca")
    authority.issue("tls", "rate-store", loopback_sans())
    (config,) = [
        d
        for d in enabled_chart()
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "rate-store"
    ]
    conf = directory / "conf"
    conf.mkdir()
    (conf / "redis.conf").write_text(config["data"]["redis.conf"] + "\n")
    users = directory / "acl"
    users.mkdir()
    (users / "users.acl").write_text(acl)
    return tls, conf, users


@contextmanager
def store(directory: Path, acl: str) -> Iterator[str]:
    """The pinned image with ``redis-server`` as PID 1, as the pod runs it, with
    a read-only root file system. Root, not the pod's user 999: a test's
    temporary directory is private to its owner, and who reads the files does
    not change which processes the probe leaves."""
    tls, conf, users = make_files(directory, acl)
    name = f"{CONTAINER_PREFIX}{uuid.uuid4().hex[:12]}"
    server = subprocess.Popen(
        [
            "docker",
            "run",
            "--name",
            name,
            "--read-only",
            "-v",
            f"{tls}:{TLS_DIRECTORY}:ro",
            "-v",
            f"{conf}:/etc/redis:ro",
            "-v",
            f"{users}:/etc/redis-acl:ro",
            pinned_image(),
            "redis-server",
            "/etc/redis/redis.conf",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_until_ready(server)
        yield name
    finally:
        docker("rm", "-f", name)
        server.wait()
        if server.stdout is not None:
            server.stdout.close()


def processes(name: str) -> list[str]:
    done = docker("exec", name, "sh", "-c", LIST_PROCESSES)
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


def probe_command(probe: str) -> list[str]:
    command = store_container()[probe]["exec"]["command"]
    assert command[:2] == ["sh", "-c"]
    return command


def run_probe(name: str, probe: str) -> subprocess.CompletedProcess[str]:
    return docker("exec", name, *probe_command(probe))


@pytest.fixture(scope="module")
def pinned_store_possible() -> None:
    if not docker_ready():
        if os.environ.get("MERIDIAN_REQUIRE_DB") == "1":
            pytest.fail("Docker is required to run the pinned Redis image")
        pytest.skip("Docker is not available")


@pytest.mark.parametrize("probe", ["readinessProbe", "livenessProbe"])
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_a_probe_run_many_times_leaves_no_process_in_the_store_container(
    tmp_path: Path, pinned_store_possible: None, case: Case, probe: str
) -> None:
    with store(tmp_path, case.acl) as name:
        if case.frozen:
            # SIGSTOP on PID 1: the server stays, and answers nothing below the
            # protocol. (`docker pause` freezes `docker exec` too.)
            assert docker("kill", "--signal=SIGSTOP", name).returncode == 0
        before = processes(name)
        results = [run_probe(name, probe) for _ in range(case.runs)]
        after = processes(name)

    assert before == []
    assert {r.returncode for r in results} == {case.status}
    assert all(case.message in r.stderr for r in results)
    # Defunct and alive alike: nothing the probe started is left.
    assert after == []
