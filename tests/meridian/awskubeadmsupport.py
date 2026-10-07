"""Rendering, stand-in programs and the runner for the boot-script tests (S079).

``test_aws_kubeadm_bootstrap.py`` runs the two cloud-init scripts of
``infra/terraform/aws-kubeadm/templates/`` whole, under bash, against stand-in
programs (``kubeadm``, ``aws``, ``curl``, ``gpg``, ``systemctl``, ``apt-get`` and
the others), each of which logs its arguments to a file under the test's own
``tmp_path``. This module holds what those tests share: the fixed inputs, the
rendering of a template, the stand-ins and the runner. The scripts' own
variables (``BOOT_ROOT``, the number of tries, the seconds between two tries)
are set through the environment, so no test waits.

How the rendering is done: the templates use only ``${name}`` and the escaped
``$${``, so ``render`` below does what Terraform's ``templatefile`` does with
them, and one test (needs ``terraform``) holds the two renderings equal for the
same inputs.
"""

import gzip
import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from servicesupport import REPO_ROOT

MODULE = REPO_ROOT / "infra" / "terraform" / "aws-kubeadm"
TEMPLATES = MODULE / "templates"
SEPARATOR = "\x1f"
SECONDS = 60  # a cap on one script run, never a wait

# ── fixed inputs ─────────────────────────────────────────────────────────────

ADDRESS = "203.0.113.10"  # the documentation's own range
OTHER_ADDRESS = "203.0.113.99"
PORT = "6443"
REGION = "eu-central-1"
PARAMETER = "/meridian-aws-kubeadm/join-command"
MINOR = "1.36"
FINGERPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
OTHER_FINGERPRINT = "7654321076543210765432107654321076543210"
POD_CIDR = "192.168.0.0/16"
CALICO_VERSION = "v3.32.2"
MANIFEST = "kind: List\nitems: []\n"
MANIFEST_SHA256 = hashlib.sha256(MANIFEST.encode()).hexdigest()
BOOTSTRAP_ID = "aaaaaa.1111111111111111"
CA_DIGEST = "2" * 64
VALID = (
    f"kubeadm join {ADDRESS}:{PORT} --token {BOOTSTRAP_ID} "
    f"--discovery-token-ca-cert-hash sha256:{CA_DIGEST}"
)
CONTAINERD_CONFIG = (
    "version = 3\n"
    "[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options]\n"
    "  SystemdCgroup = false\n"
)

COMMON_VALUES = {
    "kubernetes_minor": MINOR,
    "kubernetes_apt_key_fingerprint": FINGERPRINT,
}
CONTROL_PLANE_VALUES = {
    "region": REGION,
    "public_address": ADDRESS,
    "api_port": PORT,
    "pod_network_cidr": POD_CIDR,
    "join_parameter_name": PARAMETER,
    "calico_version": CALICO_VERSION,
    "calico_manifest_sha256": MANIFEST_SHA256,
}
WORKER_VALUES = {
    "region": REGION,
    "control_plane_address": ADDRESS,
    "api_port": PORT,
    "join_parameter_name": PARAMETER,
}

# ── rendering ────────────────────────────────────────────────────────────────

PLACEHOLDER = re.compile(r"\$\$\{|\$\{(\w+)\}")


def template_text(name: str) -> str:
    return (TEMPLATES / f"{name}.sh.tftpl").read_text(encoding="utf-8")


def render(template: str, values: dict[str, str]) -> str:
    """What ``templatefile`` makes of a template that uses only ``${name}`` and
    the escaped ``$${``. A name with no value is a KeyError; a template
    directive (a percent sign and a brace) is not supported and is refused."""
    assert "%{" not in template
    return PLACEHOLDER.sub(
        lambda m: "${" if m.group(0) == "$${" else values[m.group(1)], template
    )


def rendered(role: str) -> str:
    common = render(template_text("node-common"), COMMON_VALUES)
    values = CONTROL_PLANE_VALUES if role == "control-plane" else WORKER_VALUES
    return render(template_text(role), {**values, "common": common})


ROLES = ["control-plane", "worker"]

# EC2 limits user data to 16 KB as sent; the instances send it gzipped
# (user_data_base64 = base64gzip(...)), so the limit applies to the compressed
# bytes. The raw guard is the session's number, against runaway growth.
USER_DATA_LIMIT = 16 * 1024
RAW_GUARD = 64 * 1024


def user_data_sizes(role: str) -> tuple[int, int]:
    """The rendered script's size raw and gzipped (Python's default level)."""
    raw = rendered(role).encode("utf-8")
    return len(raw), len(gzip.compress(raw))


def code_of(script: str) -> str:
    """The script without its comment lines."""
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


# ── stand-in programs ────────────────────────────────────────────────────────

# Each call is logged with its arguments, and the HOME it was started with goes
# to a file of its own: a script must start every program with HOME=/root.
LOG = (
    'printf \'%s\\x1f\' {name} "$@" >>"{scratch}/calls"; echo >>"{scratch}/calls"; '
    'echo "$HOME" >>"{scratch}/homes"'
)

# The answer number N of a series of files <prefix>.0, <prefix>.1 ... is the
# one for the Nth call, and the last one answers every later call. The number of
# files is in <prefix>.total. A file <prefix>.N.err is what that call prints on
# its error stream, <prefix>.N.fail makes the call exit with status 255, and
# <prefix>.N.status makes it exit with the status the file holds (124 is what
# `timeout` exits with when it cut the command off).
PICK = """
pick() {{
  local prefix="{scratch}/$1" n=0 total
  [[ -f $prefix.count ]] && n=$(<"$prefix.count")
  echo $((n + 1)) >"$prefix.count"
  total=$(<"$prefix.total")
  (( n >= total )) && n=$((total - 1))
  [[ -e $prefix.$n.err ]] && cat "$prefix.$n.err" >&2
  [[ -e $prefix.$n.fail ]] && return 255
  [[ -e $prefix.$n.status ]] && return "$(<"$prefix.$n.status")"
  cat "$prefix.$n"
}}
"""

# The option every apt-get call of the scripts holds: a wait for the package
# lock that a first boot's own updater may be holding.
APT_LOCK = ("-o", "DPkg::Lock::Timeout=300")
KERNEL_MODULES = ("overlay", "br_netfilter")
# What `ctr version` prints: a Client section and a Server section, each with a
# Version line. The values are invented.
SERVER_VERSION = "v9.9.9-stand-in"
CTR_VERSION_ANSWER = (
    "Client:\n  Version: v1.1.1-client\n  Revision: 0000\n  Go version: go0\n\n"
    f"Server:\n  Version: {SERVER_VERSION}\n  Revision: 0000\n  UUID: 0\n"
)
SYSCTL_FILE = (
    "net.bridge.bridge-nf-call-iptables = 1\n"
    "net.bridge.bridge-nf-call-ip6tables = 1\n"
    "net.ipv4.ip_forward = 1\n"
)

STUBS = {
    "curl": """
url=""; out=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    --output) out="${args[i + 1]}" ;;
    http*) url="${args[i]}" ;;
  esac
done
case "$url" in
  */latest/api/token) printf 'session' ;;
  */meta-data/public-ipv4)
    value=$(pick imds) || exit 22
    [[ -n $value ]] || exit 22
    printf '%s' "$value" ;;
  */Release.key) cp "{scratch}/release.key" "$out" ;;
  */manifests/calico.yaml) cp "{scratch}/calico.yaml" "$out" ;;
  *) echo "stub curl: unexpected $url" >&2; exit 99 ;;
esac
""",
    # The listing is what the test put in <scratch>/listing, whatever file gpg
    # is asked to read: see Scratch.listing.
    "gpg": """
case "$*" in
  *--show-keys*) cat "{scratch}/listing" ;;
  *--dearmor*)
    args=("$@")
    for ((i = 0; i < ${#args[@]}; i++)); do
      [[ ${args[i]} == --output ]] && : >"${args[i + 1]}"
    done
    exit 0 ;;
  *) echo "stub gpg: unexpected $*" >&2; exit 99 ;;
esac
""",
    "aws": """
case "$1 $2" in
  "ssm get-parameter") pick param ;;
  "ssm put-parameter")
    args=("$@")
    for ((i = 0; i < ${#args[@]}; i++)); do
      [[ ${args[i]} == --value ]] && source="${args[i + 1]#file://}"
    done
    n=0; [[ -f "{scratch}/put.count" ]] && n=$(<"{scratch}/put.count")
    echo $((n + 1)) >"{scratch}/put.count"
    cp "$source" "{scratch}/put.$n.value"
    failures=0
    [[ -f "{scratch}/put-failures" ]] && failures=$(<"{scratch}/put-failures")
    (( n < failures )) && exit 255
    exit 0 ;;
  *) echo "stub aws: unexpected $*" >&2; exit 99 ;;
esac
""",
    "kubeadm": """
kubernetes="$BOOT_ROOT/etc/kubernetes"
case "$1" in
  init)
    mkdir -p "$kubernetes"; : >"$kubernetes/admin.conf"
    # The real init prints its bootstrap token unless it is told not to.
    [[ " $* " == *" --skip-token-print "* ]] || cat "{scratch}/join-line" ;;
  token) cat "{scratch}/join-line" ;;
  join)
    [[ -e "{scratch}/join-fails" ]] && exit 1
    mkdir -p "$kubernetes"; : >"$kubernetes/kubelet.conf" ;;
  *) echo "stub kubeadm: unexpected $*" >&2; exit 99 ;;
esac
""",
    "containerd": """
if [[ "$*" != "config default" ]]; then
  echo "stub containerd: unexpected $*" >&2; exit 99
fi
cat "{scratch}/containerd.toml"
""",
    # What kubectl is asked to apply is copied, when it is a file, so that a test
    # can read the bytes that would have been applied.
    "kubectl": """
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ ${args[i]} == -f && -f ${args[i + 1]} ]]; then
    cp "${args[i + 1]}" "{scratch}/applied"
  fi
done
""",
    # Only `sysctl --system`. A settings file that names the bridge keys is
    # refused unless the module that makes them was loaded first (a stand-in's
    # model of the kernel: without br_netfilter there is no such key to set).
    "sysctl": """
if [[ "$*" != "--system" ]]; then
  echo "stub sysctl: unexpected $*" >&2; exit 99
fi
if grep -q '^net.bridge' "$BOOT_ROOT/etc/sysctl.d/k8s.conf" 2>/dev/null &&
  [[ ! -e "{scratch}/loaded.br_netfilter" ]]; then
  echo "sysctl: cannot stat /proc/sys/net/bridge/bridge-nf-call-iptables" >&2; exit 255
fi
""",
    # One module per call: after the first name, modprobe reads its arguments as
    # parameters of that module, so a call with two names is refused here. A file
    # <scratch>/modprobe-fails holding a module's name makes that module fail.
    "modprobe": """
case "$*" in
  overlay|br_netfilter) ;;
  *) echo "stub modprobe: unexpected $*" >&2; exit 99 ;;
esac
if [[ -e "{scratch}/modprobe-fails" && $(<"{scratch}/modprobe-fails") == "$1" ]]; then
  exit 1
fi
: >"{scratch}/loaded.$1"
""",
    # Only the two lock-timeout forms the scripts use, each with the option first
    # and the exact package list: a typo in a flag or a package is exit 99. A
    # file <scratch>/apt-get-failures holding N makes the first N calls (of any
    # verb) fail with apt's status for a lock it could not take.
    "apt-get": """
if [[ $1 != -o || $2 != DPkg::Lock::Timeout=300 ]]; then
  echo "stub apt-get: no lock timeout in: $*" >&2; exit 99
fi
shift 2
case "$*" in
  update) ;;
  "install -y apt-transport-https ca-certificates curl gpg containerd") ;;
  "install -y kubelet kubeadm kubectl") ;;
  *) echo "stub apt-get: unexpected $*" >&2; exit 99 ;;
esac
failures=0
file="{scratch}/apt-get-failures"
[[ -f $file ]] && failures=$(<"$file")
n=0; [[ -f "{scratch}/apt-get.count" ]] && n=$(<"{scratch}/apt-get.count")
echo $((n + 1)) >"{scratch}/apt-get.count"
if (( n < failures )); then
  echo "E: Could not get lock /var/lib/apt/lists/lock" >&2; exit 100
fi
""",
    # Only the one call the scripts make; a file <scratch>/apt-mark-failures
    # holding N makes the first N calls fail.
    "apt-mark": """
if [[ "$*" != "hold kubelet kubeadm kubectl" ]]; then
  echo "stub apt-mark: unexpected $*" >&2; exit 99
fi
failures=0
file="{scratch}/apt-mark-failures"
[[ -f $file ]] && failures=$(<"$file")
n=0; [[ -f "{scratch}/apt-mark.count" ]] && n=$(<"{scratch}/apt-mark.count")
echo $((n + 1)) >"{scratch}/apt-mark.count"
if (( n < failures )); then
  echo "E: Could not get lock /var/lib/dpkg/lock-frontend" >&2; exit 100
fi
""",
    # Three calls. A restart makes the socket appear under the boot root, unless
    # the test left <scratch>/no-socket there; the socket is a copy (cp -a) of
    # the one make_scratch made.
    "systemctl": """
case "$*" in
  "enable containerd"|"enable kubelet") ;;
  "restart containerd")
    if [[ ! -e "{scratch}/no-socket" ]]; then
      mkdir -p "$BOOT_ROOT/run/containerd"
      cp -a "{scratch}/containerd.sock" "$BOOT_ROOT/run/containerd/containerd.sock"
    fi ;;
  *) echo "stub systemctl: unexpected $*" >&2; exit 99 ;;
esac
""",
    # `ctr version`, answered from the series <scratch>/ctr.N (see PICK).
    "ctr": """
if [[ "$*" != "version" ]]; then
  echo "stub ctr: unexpected $*" >&2; exit 99
fi
pick ctr
""",
    "snap": "",
}


def gpg_listing(*keys: tuple[str, str]) -> str:
    """What ``gpg --show-keys --with-colons`` prints for a file that holds one
    primary key per ``(fingerprint, expiry)`` given: the expiry is field 7 of the
    ``pub`` line, in seconds since the epoch, and empty for a key that never
    expires."""
    lines = []
    for fingerprint, expiry in keys:
        lines += [
            f"pub:-:2048:1:AAAA:1:{expiry}::-:::scSC::::::23::0:",
            f"fpr:::::::::{fingerprint}:",
            "uid:-::::1::BBBB::stand-in <nobody@example.invalid>::::::::::0:",
        ]
    return "\n".join(lines) + "\n"


@dataclass
class Scratch:
    path: Path

    def calls(self) -> list[list[str]]:
        text = (self.path / "calls").read_text(encoding="utf-8")
        return [line.split(SEPARATOR)[:-1] for line in text.splitlines() if line]

    def called(self, command: str, *first: str) -> list[list[str]]:
        """The calls to ``command`` whose first arguments are ``first``."""
        return [
            call
            for call in self.calls()
            if call[0] == command and call[1 : 1 + len(first)] == list(first)
        ]

    def series(self, prefix: str, answers: list[str | bytes]) -> None:
        for number, answer in enumerate(answers):
            data = answer if isinstance(answer, bytes) else answer.encode("utf-8")
            (self.path / f"{prefix}.{number}").write_bytes(data)
        (self.path / f"{prefix}.total").write_text(str(len(answers)), encoding="utf-8")

    def listing(self, *keys: tuple[str, str]) -> None:
        """What the gpg stand-in prints when it is asked to list a key file."""
        (self.path / "listing").write_text(gpg_listing(*keys), encoding="utf-8")

    def put_values(self) -> list[str]:
        return [
            f.read_text(encoding="utf-8") for f in sorted(self.path.glob("put.*.value"))
        ]

    def count_file(self, name: str) -> int:
        path = self.path / f"{name}.count"
        return int(path.read_text(encoding="utf-8")) if path.exists() else 0


def make_scratch(tmp_path: Path) -> Scratch:
    """A scratch directory with every stand-in program written, and the answers
    of a node that boots well: the address is the Elastic IP at once, the key
    has the pinned fingerprint, the manifest is the pinned one, kubeadm prints a
    join command, and the parameter holds one."""
    scratch = Scratch(tmp_path)
    for directory in ("bin", "root", "tmp"):
        (tmp_path / directory).mkdir(exist_ok=True)
    (tmp_path / "calls").touch()
    for name, body in STUBS.items():
        text = "#!/usr/bin/env bash\n"
        text += PICK.format(scratch=tmp_path)
        text += LOG.format(name=name, scratch=tmp_path) + "\n"
        text += body.replace("{scratch}", str(tmp_path))
        path = tmp_path / "bin" / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    (tmp_path / "release.key").write_text("stand-in key\n", encoding="utf-8")
    (tmp_path / "calico.yaml").write_text(MANIFEST, encoding="utf-8")
    scratch.listing((FINGERPRINT, ""))
    (tmp_path / "join-line").write_text(VALID + "\n", encoding="utf-8")
    (tmp_path / "containerd.toml").write_text(CONTAINERD_CONFIG, encoding="utf-8")
    scratch.series("imds", [ADDRESS])
    scratch.series("param", [VALID + "\n"])
    scratch.series("ctr", [CTR_VERSION_ANSWER])
    os.mknod(tmp_path / "containerd.sock", stat.S_IFSOCK | 0o600)
    return scratch


def run_script(
    scratch: Scratch, role: str, **settings: str
) -> subprocess.CompletedProcess[str]:
    """The rendered script of ``role`` under bash, with the stand-ins first on
    PATH, its root prefix in the scratch directory and its waits at zero
    seconds. ``settings`` override the scripts' own variables (and add to the
    environment: ``LC_ALL`` is one)."""
    script = scratch.path / f"{role}.sh"
    script.write_text(rendered(role), encoding="utf-8")
    env = {
        "PATH": f"{scratch.path / 'bin'}:/usr/bin:/bin",
        "HOME": str(scratch.path),
        "TMPDIR": str(scratch.path / "tmp"),
        "BOOT_ROOT": str(scratch.path / "root"),
        "POLL_SECONDS": "0",
        "ADDRESS_ATTEMPTS": "3",
        "PUBLISH_ATTEMPTS": "3",
        "JOIN_ATTEMPTS": "3",
        "CONTAINERD_ATTEMPTS": "3",
        "APT_ATTEMPTS": "3",
        **settings,
    }
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=scratch.path,
        check=False,
        timeout=SECONDS,
    )


def one_error_line(done: subprocess.CompletedProcess[str], role: str) -> str:
    """The single line a script gave up with: it is on the error stream, it is
    the only thing there, and it names the role."""
    lines = done.stderr.splitlines()
    assert len(lines) == 1, done.stderr
    assert lines[0].startswith(f"{role}: ERROR: ")
    assert done.returncode == 1
    return lines[0]
