"""infra/kind/identity.sh: Keycloak on kind as an add-on (S021, Y2b).

The real script runs in bash, in a copy of ``infra/kind`` under ``tmp_path``, with a
stand-in ``kubectl`` first on the path that writes down what it was asked and what
it was given on standard input. The real ``identity-realm.sh`` runs too (it needs
only jq and openssl), so the realm file and the secrets are real: the stand-in keeps
a copy of each file the script hands it, outside the script's output, and the tests
look for those values in everything the script printed. No cluster, no container.

What none of this shows: that the pod starts, that the route is accepted, that the
policies let the edge and the Claims API through. The README's list says so.
"""

import base64
import hashlib
import json
import os
import pty
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from kindsupport import KIND_DIR, requires_jq

pytestmark = requires_jq

SCRIPT_FILES = (
    "common.sh",
    "gateways.sh",
    "identity.sh",
    "identity-realm.sh",
    "pins.env",
)
REALM_SECRET = "keycloak-realm"  # noqa: S105 (a Secret's name)
CREDENTIALS_SECRET = "keycloak-credentials"  # noqa: S105 (a Secret's name)
ENOUGH = 3_000_000  # kB, as /proc/meminfo writes them: 2,929 MB
TOO_LITTLE = 2_000_000  # 1,953 MB

# What the stand-in does. Every call is appended to ``calls``; ``get`` answers from
# the environment the test gives it; ``apply`` keeps its input in ``applied-N``; a
# Secret made with ``create secret generic`` keeps the file it was made from in
# ``secret-NAME.seen`` and the modes of the file and its folder in ``.info``.
STUB = r"""#!/usr/bin/env bash
set -u
all="$*"
printf '%s\n' "${all//$'\n'/ }" >>"${STUB_DIR}/calls"
if [[ -n "${STUB_FAIL-}" && "${all}" == *"${STUB_FAIL}"* ]]; then
  echo "stub: failing on purpose" >&2
  exit 1
fi
file="" previous=""
for argument in "$@"; do
  [[ "${previous}" == -f ]] && file="${argument}"
  previous="${argument}"
done
case "${all}" in
  *"create secret generic"*)
    name="" source=""
    for argument in "$@"; do
      case "${argument}" in
        keycloak-*) name="${argument}" ;;
        --from-file=*) source="${argument#--from-file=*=}" ;;
        --from-env-file=*) source="${argument#--from-env-file=}" ;;
      esac
    done
    printf 'mode=%s dirmode=%s\n' "$(stat -c %a "${source}")" \
      "$(stat -c %a "$(dirname "${source}")")" >"${STUB_DIR}/secret-${name}.info"
    cp "${source}" "${STUB_DIR}/secret-${name}.seen"
    cp "${source}" "${STUB_DIR}/cluster-${name}"
    printf '%s\n' "${source}" >>"${STUB_DIR}/sources"
    printf '{"apiVersion":"v1","kind":"Secret","metadata":{"name":"%s",' "${name}"
    printf '"namespace":"identity","creationTimestamp":null},"data":{}}\n'
    ;;
  *"create configmap"*)
    state=""
    for argument in "$@"; do
      case "${argument}" in --from-literal=state=*) state="${argument#*state=}" ;; esac
    done
    printf '{"apiVersion":"v1","kind":"ConfigMap",'
    printf '"metadata":{"name":"meridian-cluster-holder","creationTimestamp":null},'
    printf '"data":{"state":"%s"}}\n' "${state}"
    ;;
  *" apply "*)
    if [[ "${file}" == - ]]; then
      cat >"${STUB_DIR}/incoming"
    else
      cp "${file}" "${STUB_DIR}/incoming"
    fi
    if grep -q meridian-cluster-holder "${STUB_DIR}/incoming"; then
      echo "HOLDER $(jq -r .data.state "${STUB_DIR}/incoming")" >>"${STUB_DIR}/calls"
      rm -f "${STUB_DIR}/incoming"
    else
      count=$(($(cat "${STUB_DIR}/count" 2>/dev/null || echo 0) + 1))
      echo "${count}" >"${STUB_DIR}/count"
      mv "${STUB_DIR}/incoming" "${STUB_DIR}/applied-${count}"
      echo "APPLIED ${count} ${file##*/}" >>"${STUB_DIR}/calls"
      # A Secret applied from JSON keeps its annotations, as the cluster would.
      doc="${STUB_DIR}/applied-${count}"
      secret="$(jq -r 'select(.kind == "Secret").metadata.name' "${doc}" 2>/dev/null)"
      [[ -z "${secret}" ]] ||
        jq -c '.metadata.annotations // {}' "${doc}" >"${STUB_DIR}/notes-${secret}"
    fi
    ;;
  *"config view"*) printf '%s' "${STUB_SERVER-https://127.0.0.1:6443}" ;;
  *"get secret "*"-o jsonpath={.metadata.annotations."*)
    name="${all#*get secret }"
    name="${name%% *}"
    key="${all##*annotations.}"
    key="${key%\}}"
    key="${key//\\./.}"
    notes="${STUB_DIR}/notes-${name}"
    printf '%s' "$(jq -r --arg key "${key}" '.[$key] // empty' "${notes}" 2>/dev/null)"
    ;;
  *"get secret keycloak-realm -o jsonpath"*)
    if [[ -f "${STUB_DIR}/cluster-keycloak-realm" ]]; then
      base64 -w0 "${STUB_DIR}/cluster-keycloak-realm"
    elif [[ -n "${STUB_REALM_SECRET-}" ]]; then
      printf c3R1Yg==
    fi
    ;;
  *"get secret keycloak-realm -o name"*)
    if [[ -n "${STUB_REALM_SECRET-}" || -f "${STUB_DIR}/cluster-keycloak-realm" ]]; then
      echo secret/keycloak-realm
    fi
    ;;
  *"get secret keycloak-credentials -o name"*)
    kept="${STUB_DIR}/cluster-keycloak-credentials"
    if [[ -n "${STUB_CREDENTIALS_SECRET-}" || -f "${kept}" ]]; then
      echo secret/keycloak-credentials
    fi
    ;;
  *"get deployment keycloak -o name"*)
    [[ -z "${STUB_DEPLOYMENT-}" ]] || echo deployment.apps/keycloak ;;
  *"get namespace identity -o name"*)
    [[ -z "${STUB_NAMESPACE-}" ]] || echo namespace/identity ;;
  *"get gateway edge"*) cat "${STUB_DIR}/gateway-${STUB_GATEWAY:-wide}.json" ;;
  *"get deployment keycloak -o json"*) cat "${STUB_DIR}/deployment.json" ;;
  *"get httproute keycloak -o json"*) cat "${STUB_DIR}/httproute.json" ;;
esac
exit 0
"""

# The record of who holds the cluster reads the commit of the checkout with git; the
# copy under tmp_path is no repository, and a test makes no commit.
GIT_STUB = """#!/usr/bin/env bash
case "$*" in *"rev-parse --short HEAD"*) echo abc1234 ;; *) exit 1 ;; esac
"""

WIDE = {
    "spec": {
        "listeners": [
            {
                "allowedRoutes": {
                    "namespaces": {
                        "from": "Selector",
                        "selector": {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/metadata.name",
                                    "operator": "In",
                                    "values": ["meridian", "identity"],
                                }
                            ]
                        },
                    }
                }
            }
        ]
    }
}
NARROW = {
    "spec": {
        "listeners": [
            {
                "allowedRoutes": {
                    "namespaces": {
                        "from": "Selector",
                        "selector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "meridian"}
                        },
                    }
                }
            }
        ]
    }
}
DEPLOYMENT = {"status": {"replicas": 1, "readyReplicas": 1}}
ROUTE = {
    "status": {"parents": [{"conditions": [{"type": "Accepted", "status": "True"}]}]}
}


@dataclass
class Run:
    process: subprocess.CompletedProcess[str]
    stub: Path
    cache: Path

    @property
    def calls(self) -> list[str]:
        path = self.stub / "calls"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def output(self) -> str:
        return self.process.stdout + self.process.stderr

    def changes(self) -> list[str]:
        """Every call that is not a read."""
        return [
            call
            for call in self.calls
            if not call.startswith("APPLIED")
            and re.search(r" (apply|create|delete|patch|replace|rollout) ", call)
        ]

    def holders(self) -> list[str]:
        """The states the run recorded as the holder of the cluster, in order."""
        return [c.split()[1] for c in self.calls if c.startswith("HOLDER ")]

    def applied(self) -> list[tuple[str, str]]:
        """(file name or ``-``, text) of each apply, in order."""
        names = [c.split()[2] for c in self.calls if c.startswith("APPLIED")]
        return [
            (name, (self.stub / f"applied-{n}").read_text())
            for n, name in enumerate(names, 1)
        ]

    def seen(self, secret: str) -> str:
        return (self.stub / f"secret-{secret}.seen").read_text()

    def info(self, secret: str) -> str:
        return (self.stub / f"secret-{secret}.info").read_text().strip()

    def leftovers(self) -> list[str]:
        folder = self.cache / "meridian-identity"
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def run_script(
    tmp_path: Path,
    *args: str,
    identity: str = "keycloak",
    kilobytes: int = ENOUGH,
    meminfo: str = "",
    gateway: str = "wide",
    docker_host: str = "unix:///var/run/docker.sock",
    realm_secret: bool = False,
    credentials_secret: bool = False,
    deployment: bool = False,
    namespace: bool = False,
    rotate: str = "",
    fail: str = "",
    edits: tuple[tuple[str, str, str], ...] = (),
    env: dict[str, str] | None = None,
    terminal: bool = False,
) -> Run:
    """``edits``: (file under infra/kind, old text, new text), applied to the copy
    before the run, for the tests of a file that is malformed. ``env`` adds to the
    script's environment. ``terminal``: standard output is a terminal (a pty), as
    when a person runs the command; otherwise it is a pipe, as in every test."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    kind = tmp_path / "infra" / "kind"
    stub = tmp_path / "stub"
    cache = tmp_path / "cache"
    if not kind.exists():
        kind.mkdir(parents=True)
        for name in SCRIPT_FILES:
            shutil.copy2(KIND_DIR / name, kind / name)
        shutil.copytree(KIND_DIR / "manifests", kind / "manifests")
        (kind / "kubeconfig").write_text("stand-in")
        stub.mkdir()
        for tool, text in (("kubectl", STUB), ("git", GIT_STUB)):
            binary = stub / tool
            binary.write_text(text)
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        (stub / "gateway-wide.json").write_text(json.dumps(WIDE))
        (stub / "gateway-narrow.json").write_text(json.dumps(NARROW))
        (stub / "deployment.json").write_text(json.dumps(DEPLOYMENT))
        (stub / "httproute.json").write_text(json.dumps(ROUTE))
    (tmp_path / "meminfo").write_text(
        meminfo or f"MemTotal: 9000000 kB\nMemAvailable: {kilobytes} kB\n"
    )
    for relative, old, new in edits:
        target = kind / relative
        target.write_text(target.read_text().replace(old, new, 1))
    for stale in (
        *stub.glob("applied-*"),
        stub / "calls",
        stub / "count",
        stub / "sources",
    ):
        stale.unlink(missing_ok=True)
    environment = {
        "PATH": f"{stub}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(cache),
        "DOCKER_HOST": docker_host,
        "CLUSTER_HOLDER": "tests",
        "MERIDIAN_MEMINFO_FILE": str(tmp_path / "meminfo"),
        "STUB_DIR": str(stub),
        "STUB_GATEWAY": gateway,
        "STUB_FAIL": fail,
        "MERIDIAN_IDENTITY": identity,
        "MERIDIAN_IDENTITY_ROTATE": rotate,
        **(env or {}),
    }
    for key, on in (
        ("STUB_REALM_SECRET", realm_secret),
        ("STUB_CREDENTIALS_SECRET", credentials_secret),
        ("STUB_DEPLOYMENT", deployment),
        ("STUB_NAMESPACE", namespace),
    ):
        if on:
            environment[key] = "1"
    command = ["bash", str(kind / "identity.sh"), *args]
    if terminal:
        return Run(run_on_terminal(command, environment), stub, cache)
    done = subprocess.run(
        command, capture_output=True, text=True, env=environment, check=False
    )
    return Run(done, stub, cache)


def run_on_terminal(
    command: list[str], environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run with standard output on a pty (so ``-t 1`` is true) and read what was
    written there; standard error stays a pipe. A terminal turns \\n into \\r\\n:
    the carriage returns are removed."""
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
        )
        _, stderr = process.communicate(timeout=60)
        os.close(slave)
        slave = -1
        written = b""
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # the other end is closed: all of it was read
                break
            if not chunk:
                break
            written += chunk
    finally:
        os.close(master)
        if slave != -1:
            os.close(slave)
    text = written.decode("utf-8").replace("\r\n", "\n")
    return subprocess.CompletedProcess(command, process.returncode, text, stderr)


def secret_values(run: Run) -> list[str]:
    """The passwords and client secrets the generator made, from the stand-in's
    copy of the env file, and the realm file's text."""
    pairs = [
        line.split("=", 1)
        for line in run.seen(CREDENTIALS_SECRET).splitlines()
        if "=" in line
    ]
    return [value for _, value in pairs]


# ── a run that installs ──────────────────────────────────────────────────────


def test_a_first_run_makes_both_secrets_then_the_policies_then_the_workload(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up")

    assert run.process.returncode == 0, run.output
    created = [c for c in run.calls if "create secret generic" in c]
    assert [c.split("generic ")[1].split()[0] for c in created] == [
        REALM_SECRET,
        CREDENTIALS_SECRET,
    ]
    assert "--from-file=meridian-staff-realm.json=" in created[0]
    assert "--from-env-file=" in created[1]
    names = [name for name, _ in run.applied()]
    # The namespace and its denials first, then the Secrets (the namespace must
    # exist), then the other two ends, then the workload with the pod.
    assert names == [
        "identity-networkpolicy.yaml",
        "-",  # the Secret of the realm, through the pipe
        "-",  # the Secret of the credentials
        "identity-peers-networkpolicy.yaml",
        "-",  # the workload with the image filled in
    ]
    order = [
        i
        for i, c in enumerate(run.calls)
        if c.startswith("APPLIED") or "create secret generic" in c
    ]
    assert order == sorted(order)


def test_the_workload_is_applied_with_the_pinned_image_and_no_placeholder(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up")
    pins = (KIND_DIR / "pins.env").read_text(encoding="utf-8")
    (image,) = re.findall(r"^KEYCLOAK_IMAGE=(\S+)$", pins, re.M)

    (_, workload) = run.applied()[-1]
    documents = [d for d in yaml.safe_load_all(workload) if d]
    (deployment,) = [d for d in documents if d["kind"] == "Deployment"]
    spec = deployment["spec"]["template"]["spec"]

    assert spec["containers"][0]["image"] == image
    assert spec["initContainers"][0]["image"] == image
    assert "PLACEHOLDER" not in workload
    assert re.search(r"@sha256:[0-9a-f]{64}$", image)


def test_the_run_waits_for_the_rollout_and_prints_what_it_made_and_the_memory_after(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up")

    assert any("rollout status deployment/keycloak" in c for c in run.calls)
    assert run.calls.index(
        next(c for c in run.calls if "rollout status" in c)
    ) > run.calls.index(next(c for c in run.calls if c == "APPLIED 5 -"))
    assert "2929 MB" in run.output  # the figure, before and after
    assert "http://id.meridian.localhost:8088/realms/meridian-staff" in run.output
    assert "identity" in run.output and "keycloak" in run.output


def test_nothing_of_the_realm_or_the_secrets_is_printed_or_put_on_a_command_line(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up")
    values = secret_values(run)

    assert len(values) == 9  # the cast's seven passwords and two clients' secrets
    assert all(len(value) == 48 for value in values)
    for value in values:
        assert value not in run.output
        assert all(value not in call for call in run.calls)
        assert all(value not in text for _, text in run.applied()[2:])
    realm = json.loads(run.seen(REALM_SECRET))
    assert realm["realm"] == "meridian-staff"
    # The realm file holds the secrets (Keycloak reads them there) and nowhere else
    # than in the Secret the stand-in was handed.
    assert all(value in run.seen(REALM_SECRET) for value in values)


def test_the_files_the_secrets_come_from_are_private_and_gone_when_the_run_ends(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up")

    for secret in (REALM_SECRET, CREDENTIALS_SECRET):
        assert run.info(secret) == "mode=600 dirmode=700"
    sources = (run.stub / "sources").read_text().splitlines()
    cache = str(run.cache / "meridian-identity")
    assert all(path.startswith(cache) for path in sources)  # never the repository
    assert "infra/kind" not in "".join(sources)
    assert run.leftovers() == []
    assert all(not Path(path).exists() for path in sources)


def test_the_files_are_gone_when_the_run_fails_halfway(tmp_path: Path) -> None:
    run = run_script(tmp_path, "up", fail="create secret generic keycloak-credentials")

    assert run.process.returncode != 0
    assert run.leftovers() == []
    assert any("keycloak-realm" in c for c in run.calls)


def test_a_second_run_keeps_the_secrets_it_finds(tmp_path: Path) -> None:
    # Y2f: kept only with one generation, so the first run makes them (not a flag).
    assert run_script(tmp_path, "up").process.returncode == 0
    run = run_script(tmp_path, "up")

    assert run.process.returncode == 0, run.output
    assert not [c for c in run.calls if "create secret" in c]
    assert run.leftovers() == []  # no generator ran, no folder was made
    assert "kept" in run.output and "MERIDIAN_IDENTITY_ROTATE=1" in run.output
    assert "ends every session" in run.output
    assert not [c for c in run.calls if "rollout restart" in c]
    assert [n for n, _ in run.applied()] == [
        "identity-networkpolicy.yaml",
        "identity-peers-networkpolicy.yaml",
        "-",
    ]


def test_one_secret_missing_means_both_are_made_anew(tmp_path: Path) -> None:
    run = run_script(tmp_path, "up", realm_secret=True)

    assert run.process.returncode == 0, run.output
    assert len([c for c in run.calls if "create secret generic" in c]) == 2


def annotation(run: Run) -> str:
    """The realm fingerprint the run wrote on the pod template."""
    _, workload = run.applied()[-1]
    (deployment,) = [
        d for d in yaml.safe_load_all(workload) if d and d["kind"] == "Deployment"
    ]
    return deployment["spec"]["template"]["metadata"]["annotations"][
        "meridian.local/realm-sha256"
    ]


def digest_of_cluster_realm(run: Run) -> str:
    """What the fingerprint must be: the SHA-256 of the base64 text the cluster's
    Secret holds (the stand-in keeps the file the last Secret was made from)."""
    content = (run.stub / "cluster-keycloak-realm").read_bytes()
    return hashlib.sha256(base64.b64encode(content)).hexdigest()


def test_the_pod_carries_the_realm_secrets_fingerprint_and_prints_nothing_of_it(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up")

    assert run.process.returncode == 0, run.output
    assert annotation(run) == digest_of_cluster_realm(run)
    assert re.fullmatch(r"[0-9a-f]{64}", annotation(run))
    # The Secret's content is read through one jsonpath, into a variable and a pipe.
    reads = [c for c in run.calls if "get secret keycloak-realm -o jsonpath" in c]
    assert len(reads) == 1
    assert not any(value in run.output for value in secret_values(run))
    content = (run.stub / "cluster-keycloak-realm").read_bytes()
    assert base64.b64encode(content).decode() not in run.output


def test_rotate_makes_new_secrets_and_the_pod_template_changes_with_them(
    tmp_path: Path,
) -> None:
    # Each run's record is read at once: the next run clears the stand-in's files.
    first = annotation(run_script(tmp_path, "up"))
    second = run_script(tmp_path, "up", rotate="1")

    assert second.process.returncode == 0, second.output
    assert len([c for c in second.calls if "create secret generic" in c]) == 2
    assert annotation(second) == digest_of_cluster_realm(second)
    # A changed template rolls the pod by itself: no separate restart is asked.
    assert first != annotation(second)
    assert not [c for c in second.calls if "rollout restart" in c]


def test_a_plain_run_after_a_run_that_changed_nothing_keeps_the_same_template(
    tmp_path: Path,
) -> None:
    first = annotation(run_script(tmp_path, "up"))
    second = run_script(tmp_path, "up")

    assert second.process.returncode == 0, second.output
    assert not [c for c in second.calls if "create secret" in c]
    assert first == annotation(second)


def test_a_rotation_interrupted_before_the_workload_is_rolled_by_the_next_plain_run(
    tmp_path: Path,
) -> None:
    first = annotation(run_script(tmp_path, "up"))
    # The Secrets are replaced, and the run stops at the peers' policies, before
    # the Deployment is applied: the pod on the cluster still has the old realm.
    broken = run_script(
        tmp_path, "up", rotate="1", fail="identity-peers-networkpolicy.yaml"
    )
    broken_creates = len([c for c in broken.calls if "create secret generic" in c])
    broken_applied = [name for name, _ in broken.applied()]
    again = run_script(tmp_path, "up")  # no ROTATE: both Secrets exist, kept

    assert broken.process.returncode != 0
    assert broken_creates == 2
    # No Deployment: the pod on the cluster still has the first run's template.
    assert broken_applied == ["identity-networkpolicy.yaml", "-", "-"]
    assert again.process.returncode == 0, again.output
    assert not [c for c in again.calls if "create secret" in c]
    # The old template is not applied again: the annotation is the new realm's.
    assert annotation(again) == digest_of_cluster_realm(again)
    assert annotation(again) != first


@pytest.mark.parametrize("value", ["yes", "0", "true", "2"])
def test_the_rotate_switch_is_empty_or_1_and_nothing_else(
    tmp_path: Path, value: str
) -> None:
    run = run_script(tmp_path, "up", rotate=value)

    assert run.process.returncode != 0
    assert "MERIDIAN_IDENTITY_ROTATE" in run.output
    assert run.changes() == []


# ── what it refuses, before it changes anything ──────────────────────────────


def test_under_2500_mb_available_it_refuses_and_prints_the_figure(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up", kilobytes=TOO_LITTLE)

    assert run.process.returncode != 0
    assert "1953 MB" in run.output and "2500 MB" in run.output
    assert run.changes() == []
    assert run.applied() == []
    assert run.leftovers() == []


def test_at_2500_mb_available_it_goes_on_and_one_megabyte_below_it_does_not(
    tmp_path: Path,
) -> None:
    on_the_line = run_script(tmp_path / "a", "up", kilobytes=2500 * 1024)
    below = run_script(tmp_path / "b", "up", kilobytes=2500 * 1024 - 1024)

    assert on_the_line.process.returncode == 0, on_the_line.output
    assert below.process.returncode != 0
    assert "2499 MB" in below.output


@pytest.mark.parametrize(
    "meminfo", ["MemTotal: 1 kB\n", "MemAvailable: lots kB\n", "MemAvailable: kB\n"]
)
def test_a_figure_it_cannot_read_stops_it_unchanged(
    tmp_path: Path, meminfo: str
) -> None:
    run = run_script(tmp_path, "up", meminfo=meminfo)

    assert run.process.returncode != 0
    assert "MemAvailable" in run.output
    assert run.changes() == []


def test_with_the_switch_off_it_installs_nothing_and_asks_the_cluster_nothing(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up", identity="")

    assert run.process.returncode != 0
    assert "MERIDIAN_IDENTITY=keycloak" in run.output
    assert run.calls == []


def test_a_wrong_switch_is_refused_by_the_script_too(tmp_path: Path) -> None:
    run = run_script(tmp_path, "up", identity="dex")

    assert run.process.returncode != 0
    assert "MERIDIAN_IDENTITY must be empty" in run.output
    assert run.calls == []


def test_a_docker_engine_that_is_not_local_is_refused(tmp_path: Path) -> None:
    run = run_script(tmp_path, "up", docker_host="tcp://192.0.2.1:2375")

    assert run.process.returncode != 0
    assert "not local" in run.output
    assert run.changes() == []


def test_an_edge_that_does_not_admit_routes_from_identity_stops_it_unchanged(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up", gateway="narrow")

    assert run.process.returncode != 0
    assert "identity" in run.output and "make up" in run.output
    assert run.changes() == []
    assert run.applied() == []


def test_a_cluster_that_does_not_answer_stops_it_unchanged(tmp_path: Path) -> None:
    run = run_script(tmp_path, "up", fail="get nodes")

    assert run.process.returncode != 0
    assert run.changes() == []


def test_a_route_host_name_that_is_not_a_localhost_name_is_refused(
    tmp_path: Path,
) -> None:
    run_script(tmp_path, "status")  # lays the folder out
    script = tmp_path / "infra" / "kind" / "identity.sh"
    text = script.read_text(encoding="utf-8")
    script.write_text(
        text.replace(
            "readonly IDENTITY_ROUTE_HOST=id.meridian.localhost",
            "readonly IDENTITY_ROUTE_HOST=id.meridian.example",
        ),
        encoding="utf-8",
    )

    run = run_script(tmp_path, "up")

    assert run.process.returncode != 0
    assert ".localhost" in run.output
    assert run.changes() == []


def test_the_scripts_localhost_rule_is_the_charts() -> None:
    template = (
        KIND_DIR.parent / "helm" / "meridian" / "templates" / "route.yaml"
    ).read_text(encoding="utf-8")
    (chart,) = re.findall(r'regexMatch "([^"]+\.localhost\$)"', template)
    (script,) = re.findall(
        r"^readonly IDENTITY_ROUTE_PATTERN='([^']+)'$",
        (KIND_DIR / "identity.sh").read_text(encoding="utf-8"),
        re.M,
    )

    assert chart.replace("\\\\", "\\") == script


# ── status reads, note says one line, and nothing removes ────────────────────


def test_status_reads_and_never_changes_or_reads_a_secret(tmp_path: Path) -> None:
    run = run_script(
        tmp_path,
        "status",
        namespace=True,
        deployment=True,
        realm_secret=True,
        credentials_secret=True,
    )

    assert run.process.returncode == 0, run.output
    assert run.changes() == []
    assert run.calls, "it asked the cluster nothing"
    for call in run.calls:
        assert " get " in call, call
        assert "jsonpath" not in call and "-o yaml" not in call, call
        if "secret" in call:
            assert call.endswith("-o name --ignore-not-found"), call
    assert "1/1" in run.output
    assert "Accepted" in run.output
    assert "MB" in run.output


def test_status_says_so_when_the_add_on_is_not_on_the_cluster(tmp_path: Path) -> None:
    run = run_script(tmp_path, "status", identity="")

    assert run.process.returncode == 0, run.output
    assert run.changes() == []
    assert "not" in run.output


def test_when_off_it_says_one_line_if_the_namespace_still_exists(
    tmp_path: Path,
) -> None:
    there = run_script(tmp_path / "a", "note", identity="", namespace=True)
    absent = run_script(tmp_path / "b", "note", identity="")

    lines = [line for line in there.process.stdout.splitlines() if line]
    assert there.process.returncode == 0, there.output
    assert len(lines) == 1
    assert "still exists" in lines[0] and "does not remove it" in lines[0]
    assert absent.process.stdout == "" and absent.process.returncode == 0
    for run in (there, absent):
        assert run.changes() == []
        assert len(run.calls) == 1 and "get namespace identity" in run.calls[0]


def test_a_failed_read_while_off_is_said_and_does_not_stop_make_up(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "note", identity="", fail="get namespace")

    assert run.process.returncode == 0
    assert "could not read" in run.output


def test_there_is_no_removal_command_and_no_other_word_is_accepted(
    tmp_path: Path,
) -> None:
    text = (KIND_DIR / "identity.sh").read_text(encoding="utf-8")

    assert not re.search(r"\b(delete|uninstall|destroy|remove)\)", text)
    for word in ("down", "remove", "delete", "uninstall", ""):
        run = run_script(tmp_path / (word or "empty"), word)
        assert run.process.returncode != 0, word
        assert "usage" in run.output.lower(), word
        assert run.changes() == [], word


def test_the_scripts_mode_and_syntax() -> None:
    script = KIND_DIR / "identity.sh"

    assert script.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0
