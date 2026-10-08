"""The rig of the tests of ``infra/kind/demo-seed.sh`` (S098).

`make demo-seed` posts the first claims of ``data/synthetic/claims.json`` to the
Claims API through the edge, one at a time, and prints what became of each. No
test reaches a cluster: ``run_seed`` runs the script in a scratch copy of
``infra/kind`` with stand-ins for ``curl``, ``kubectl``, ``docker`` and ``sleep``
first on the PATH. The stand-in ``curl`` records every call in ``calls`` (a
``POST id trace url`` or ``GET id`` line each, and ``SLEEP n`` for the stand-in
``sleep``), so the order of the calls is what the serial-order tests read.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from kindsupport import KIND_DIR

from meridian.workloads.claims_triage.triaging import HAS_PROPOSAL_DETAIL

SEED_SH = (KIND_DIR / "demo-seed.sh").read_text(encoding="utf-8")

# Never a field of a claim but its id and state: this string is the claimant's
# name in every claim posted here, in the description, and in what the stand-in
# services answer, and it must reach neither stdout nor stderr.
MARKER = "Zzyzx-Marker-Claimant"
CLAIMS = [
    {
        "claim_id": f"CLM-{number:04d}",
        "claimant": {"name": MARKER},
        "description": f"{MARKER} description {number}",
    }
    for number in range(1, 48)
]
IDS = [claim["claim_id"] for claim in CLAIMS]

# The script's bounds in these runs (its own are 120 and 3): the wait between
# two readings does not sleep, it moves the script's clock, so a claim that never
# settles ends the run after a fixed number of readings however busy the machine.
SETTLE_TIMEOUT_SECONDS = 100
POLL_INTERVAL_SECONDS = 10
MAX_READINGS = SETTLE_TIMEOUT_SECONDS // POLL_INTERVAL_SECONDS + 1

requires_tools = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("jq", "openssl")),
    reason="jq or openssl is not installed",
)

STUB_CURL = r"""#!/usr/bin/env bash
out="" url="" reads=no
while (($#)); do
  case "$1" in
    -o) out=$2; shift ;;
    -H)
      case "$2" in
        traceparent:*) trace="${2#*: 00-}"; trace="${trace%%-*}" ;;
      esac
      shift ;;
    --data-binary) reads=body; shift ;;
    -w | -m | --noproxy) shift ;;
    -*) ;;
    *) url=$1 ;;
  esac
  shift
done
body=""
[[ "${reads}" == body ]] && body="$(cat)"
case "${url}" in
  */healthz)
    [[ "${STUB_HEALTH}" == 200 ]] || exit 7
    printf 200 ;;
  */adjuster/claims/*/proposal)
    id="${url%/proposal}"
    id="${id##*/}"
    echo "GET ${id}" >>"${STUB_DIR}/calls"
    n=$(($(cat "${STUB_DIR}/n-${id}" 2>/dev/null || echo 0) + 1))
    echo "${n}" >"${STUB_DIR}/n-${id}"
    file="${STUB_DIR}/reads-${id}"
    if [[ ! -f "${file}" ]]; then
      printf '{"detail":"no such claim"}' >"${out}"
      printf 404
      exit 0
    fi
    state="$(sed -n "${n}p" "${file}")"
    [[ -n "${state}" ]] || state="$(tail -n 1 "${file}")"
    jq -cn --arg id "${id}" --arg state "${state}" --arg marker "${STUB_MARKER}" \
      '{claim_id: $id, state: $state, proposal: {note: $marker}}' >"${out}"
    printf 200 ;;
  */claims)
    id="$(jq -r .claim_id <<<"${body}")"
    echo "POST ${id} ${trace} ${url}" >>"${STUB_DIR}/calls"
    printf '%s' "${body}" >"${STUB_DIR}/body-${id}"
    override="${STUB_DIR}/post-${id}"
    if [[ -f "${override}" ]]; then
      status="$(head -n 1 "${override}")"
      [[ "${status}" != curlfail ]] || exit 28
      tail -n +2 "${override}" >"${out}"
      printf '%s' "${status}"
      exit 0
    fi
    if [[ -f "${STUB_DIR}/posted-${id}" ]]; then
      jq -cn --arg detail "${STUB_HAS_PROPOSAL}" '{detail: $detail}' >"${out}"
      printf 409
      exit 0
    fi
    state="$(cat "${STUB_DIR}/state-${id}" 2>/dev/null || echo awaiting_adjuster)"
    touch "${STUB_DIR}/posted-${id}"
    [[ -f "${STUB_DIR}/reads-${id}" ]] || echo "${state}" >"${STUB_DIR}/reads-${id}"
    jq -cn --arg id "${id}" --arg state "${state}" --arg marker "${STUB_MARKER}" \
      '{claim_id: $id, state: $state, run_id: "3f1c2d4e-0000-4000-8000-000000000001",
        run_status: "Completed", proposal: {route: "adjuster", drafted_by: null},
        claimant: {name: $marker}, description: $marker}' >"${out}"
    printf 201 ;;
  *) echo "unexpected curl: ${url}" >&2; exit 1 ;;
esac
"""
STUB_KUBECTL = r"""#!/usr/bin/env bash
echo "$*" >>"${STUB_DIR}/kubectl-calls"
case "$*" in
  *"get nodes"*) [[ "${STUB_NODES}" == up ]] ;;
  *"get configmap meridian-cluster-holder"*) printf '%s' "${STUB_RECORD}" ;;
esac
"""
STUB_DOCKER = r"""#!/usr/bin/env bash
case "$*" in
  "context inspect"*) printf '%s' "${STUB_DOCKER_HOST}" ;;
  "ps"*) printf '%s' "${STUB_DOCKER_NAMES}" ;;
esac
"""
STUB_SLEEP = r"""#!/usr/bin/env bash
echo "SLEEP $1" >>"${STUB_DIR}/calls"
"""


def patched_script() -> str:
    """The script with its bounds replaced by this rig's and its poll's wait
    turned into a move of its own clock (as demo's tests do)."""
    patched = SEED_SH
    for name, value in (
        ("SETTLE_TIMEOUT", SETTLE_TIMEOUT_SECONDS),
        ("POLL_INTERVAL", POLL_INTERVAL_SECONDS),
        # The stand-in sleep returns at once, so an edge that is down is asked
        # again in a loop of this many real seconds.
        ("EDGE_TIMEOUT", 2),
    ):
        patched, found = re.subn(
            rf"^readonly {name}=\d+$",
            f"readonly {name}={value}",
            patched,
            flags=re.MULTILINE,
        )
        assert found == 1, name
    patched, found = re.subn(
        r'^(\s*)sleep "\$\{POLL_INTERVAL\}"$',
        r"\1SECONDS=$((SECONDS + POLL_INTERVAL))",
        patched,
        flags=re.MULTILINE,
    )
    assert found == 1
    return patched


def run_seed(
    tmp_path: Path,
    *,
    count: str | None = None,
    pace: str | None = "0",
    states: dict[str, str] | None = None,
    readings: dict[str, list[str]] | None = None,
    posts: dict[str, tuple[int | str, str]] | None = None,
    environment: dict[str, str] | None = None,
    health: str = "200",
    nodes: str = "up",
    record: str = "",
    docker_host: str = "unix:///var/run/docker.sock",
    docker_names: str = "meridian-control-plane\n",
    available_mb: int = 8000,
    kubeconfig: bool = True,
    claims: list[dict] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """demo-seed.sh in a scratch copy of ``infra/kind`` against the stand-ins.

    ``states`` is the state a claim has when its post is answered (default
    ``awaiting_adjuster``); ``readings`` the states the JSON route answers to
    its successive reads (the last one repeats; default the post's state);
    ``posts`` a fixed answer, a status and a body, for the claim's every post
    (the status ``curlfail`` makes curl itself fail). Run twice on one
    ``tmp_path`` the stand-ins remember which claims were posted, as the Claims
    API would. Returns the process and every recorded call, each as its words.
    """
    kind, bin_dir = tmp_path / "infra" / "kind", tmp_path / "bin"
    data = tmp_path / "data" / "synthetic"
    for folder in (kind, bin_dir, data):
        folder.mkdir(parents=True, exist_ok=True)
    (kind / "demo-seed.sh").write_text(patched_script(), encoding="utf-8")
    for name in ("common.sh", "pins.env"):
        (kind / name).write_text((KIND_DIR / name).read_text(encoding="utf-8"))
    if kubeconfig:
        (kind / "kubeconfig").touch()
    (data / "claims.json").write_text(json.dumps(claims or CLAIMS), encoding="utf-8")
    for name, text in (
        ("curl", STUB_CURL),
        ("kubectl", STUB_KUBECTL),
        ("docker", STUB_DOCKER),
        ("sleep", STUB_SLEEP),
    ):
        (bin_dir / name).write_text(text, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    for claim_id, state in (states or {}).items():
        (tmp_path / f"state-{claim_id}").write_text(state)
    for claim_id, reads in (readings or {}).items():
        (tmp_path / f"reads-{claim_id}").write_text("\n".join(reads) + "\n")
    for claim_id, (status, body) in (posts or {}).items():
        (tmp_path / f"post-{claim_id}").write_text(f"{status}\n{body}")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        f"MemTotal: 12000000 kB\nMemAvailable: {available_mb * 1024} kB\n"
    )
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_DIR": str(tmp_path),
        "STUB_MARKER": MARKER,
        "STUB_HEALTH": health,
        "STUB_NODES": nodes,
        "STUB_RECORD": record,
        "STUB_DOCKER_HOST": docker_host,
        "STUB_DOCKER_NAMES": docker_names,
        "STUB_HAS_PROPOSAL": HAS_PROPOSAL_DETAIL,
        "MEMINFO_FILE": str(meminfo),
        "CLUSTER_HOLDER": "s098-demo-seed",
        **(environment or {}),
    }
    if count is not None:
        env["COUNT"] = count
    if pace is not None:
        env["PACE_SECONDS"] = pace
    done = subprocess.run(
        ["bash", str(kind / "demo-seed.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )
    calls = tmp_path / "calls"
    lines = calls.read_text().splitlines() if calls.exists() else []
    return done, [line.split(" ") for line in lines]


def posts_of(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[0] == "POST"]


def post_ids(calls: list[list[str]]) -> list[str]:
    return [call[1] for call in posts_of(calls)]


def reads_of(calls: list[list[str]], claim_id: str) -> int:
    return sum(1 for call in calls if call[:2] == ["GET", claim_id])


def claim_lines(done: subprocess.CompletedProcess[str]) -> list[list[str]]:
    """The per-claim lines of the output, each as its words."""
    return [
        line.split()
        for line in done.stdout.splitlines()
        if re.match(r"^CLM-\d{4}\s", line)
    ]


def summary(done: subprocess.CompletedProcess[str]) -> dict[str, int]:
    """The summary's rows, as text to count: ``  referred to an adjuster   3``."""
    return {
        match[1].strip(): int(match[2])
        for match in re.finditer(r"^  (\S.*?)\s+(\d+)$", done.stdout, re.MULTILINE)
    }
