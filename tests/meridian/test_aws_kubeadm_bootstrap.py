"""The two boot scripts of the self-managed cluster, run against stand-ins (S079).

``infra/terraform/aws-kubeadm/templates/`` holds the cloud-init scripts of the
control-plane node and of a worker, and the text they share. Nothing here
touches a cloud, a cluster or the machine's own configuration: each script is
rendered with fixed inputs, linted, and run whole under bash against stand-in
programs (``kubeadm``, ``aws``, ``curl``, ``gpg``, ``systemctl``, ``apt-get``
and the others), each of which logs its arguments. The scripts' own variables
(``BOOT_ROOT``, the number of tries, the seconds between two tries) are set
through the environment, so no test waits.

How the rendering is done: the templates use only ``${name}`` and the escaped
``$${``, so ``render`` below does what Terraform's ``templatefile`` does with
them, and one test (needs ``terraform``) holds the two renderings equal for the
same inputs.
"""

import base64
import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
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


def code_of(script: str) -> str:
    """The script without its comment lines."""
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


# ── stand-in programs ────────────────────────────────────────────────────────

LOG = 'printf \'%s\\x1f\' {name} "$@" >>"{scratch}/calls"; echo >>"{scratch}/calls"'

# The answer number N of a series of files <prefix>.0, <prefix>.1 ... is the
# one for the Nth call, and the last one answers every later call. The number of
# files is in <prefix>.total.
PICK = """
pick() {{
  local prefix="{scratch}/$1" n=0 total
  [[ -f $prefix.count ]] && n=$(<"$prefix.count")
  echo $((n + 1)) >"$prefix.count"
  total=$(<"$prefix.total")
  (( n >= total )) && n=$((total - 1))
  [[ -e $prefix.$n.fail ]] && return 255
  cat "$prefix.$n"
}}
"""

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
    "gpg": """
case "$*" in
  *--show-keys*)
    printf 'pub:-:2048:1:AAAA:1:2::-:::scSC::::::23::0:\\n'
    printf 'fpr:::::::::%s:\\n' "$(<"{scratch}/fingerprint")"
    printf 'uid:-::::1::BBBB::stand-in <nobody@example.invalid>::::::::::0:\\n' ;;
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
  join) mkdir -p "$kubernetes"; : >"$kubernetes/kubelet.conf" ;;
  *) echo "stub kubeadm: unexpected $*" >&2; exit 99 ;;
esac
""",
    "containerd": """
if [[ "$*" != "config default" ]]; then
  echo "stub containerd: unexpected $*" >&2; exit 99
fi
cat "{scratch}/containerd.toml"
""",
    "kubectl": "",
    "sysctl": "",
    "apt-get": "",
    "apt-mark": "",
    "systemctl": "",
    "snap": "",
}


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

    def series(self, prefix: str, answers: list[str]) -> None:
        for number, answer in enumerate(answers):
            (self.path / f"{prefix}.{number}").write_text(answer, encoding="utf-8")
        (self.path / f"{prefix}.total").write_text(str(len(answers)), encoding="utf-8")

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
    (tmp_path / "fingerprint").write_text(FINGERPRINT, encoding="utf-8")
    (tmp_path / "join-line").write_text(VALID + "\n", encoding="utf-8")
    (tmp_path / "containerd.toml").write_text(CONTAINERD_CONFIG, encoding="utf-8")
    scratch.series("imds", [ADDRESS])
    scratch.series("param", [VALID + "\n"])
    return scratch


def run_script(
    scratch: Scratch, role: str, **settings: str
) -> subprocess.CompletedProcess[str]:
    """The rendered script of ``role`` under bash, with the stand-ins first on
    PATH, its root prefix in the scratch directory and its waits at zero
    seconds. ``settings`` override the scripts' own variables."""
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


# ── terraform's rendering and the renderer here are one ─────────────────────

needs_terraform = pytest.mark.skipif(
    shutil.which("terraform") is None, reason="terraform is not installed"
)


def terraform_rendering(tmp_path: Path, role: str) -> str:
    """What ``templatefile`` makes of the role's template, with the inputs above,
    through ``terraform console`` on a scratch directory that holds the templates
    and no configuration. The text goes out base64 encoded, because the console
    prints a string in its own quoting."""
    shutil.copytree(TEMPLATES, tmp_path / "templates")
    values = CONTROL_PLANE_VALUES if role == "control-plane" else WORKER_VALUES

    def hcl(mapping: dict[str, str]) -> str:
        return "{" + ", ".join(f'{k} = "{v}"' for k, v in mapping.items()) + "}"

    common = f'templatefile("templates/node-common.sh.tftpl", {hcl(COMMON_VALUES)})'
    expression = (
        f'base64encode(templatefile("templates/{role}.sh.tftpl", '
        f"merge({hcl(values)}, {{ common = {common} }})))"
    )
    done = subprocess.run(
        ["terraform", f"-chdir={tmp_path}", "console", "-no-color"],
        input=expression + "\n",
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        check=False,
        timeout=SECONDS,
    )
    assert done.returncode == 0, done.stderr
    return base64.b64decode(done.stdout.strip().strip('"')).decode("utf-8")


@needs_terraform
@pytest.mark.parametrize("role", ROLES)
def test_the_renderer_here_makes_the_text_terraform_makes(
    tmp_path: Path, role: str
) -> None:
    assert terraform_rendering(tmp_path, role) == rendered(role)


@pytest.mark.parametrize("name", ["node-common", *ROLES])
def test_a_template_uses_only_names_the_module_passes_and_no_directive(
    name: str,
) -> None:
    text = template_text(name)
    passed = {
        "node-common": set(COMMON_VALUES),
        "control-plane": {*CONTROL_PLANE_VALUES, "common"},
        "worker": {*WORKER_VALUES, "common"},
    }[name]

    used = {m.group(1) for m in PLACEHOLDER.finditer(text) if m.group(1)}

    assert "%{" not in text
    assert used == passed  # none missing, none left over


# ── what the rendered text must look like ───────────────────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_a_rendered_script_fits_in_the_16_kb_ec2_allows_for_user_data(
    role: str,
) -> None:
    # The EC2 page "Run commands when you launch an EC2 instance with user data
    # input" (read 2026-10-07): "User data is limited to 16 KB, in raw form,
    # before it is base64-encoded."
    assert len(rendered(role).encode("utf-8")) <= 16 * 1024


@pytest.mark.parametrize("role", ROLES)
def test_a_rendered_script_passes_bash_syntax_check(tmp_path: Path, role: str) -> None:
    script = tmp_path / "script.sh"
    script.write_text(rendered(role), encoding="utf-8")

    done = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )

    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("role", ROLES)
def test_a_rendered_script_passes_shellcheck(tmp_path: Path, role: str) -> None:
    program = shutil.which("shellcheck")
    if program is None:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("shellcheck is not installed, and the pipeline needs it here")
        pytest.skip("shellcheck is not installed")
    script = tmp_path / "script.sh"
    script.write_text(rendered(role), encoding="utf-8")

    done = subprocess.run(
        [program, "--shell=bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stdout


SECRET_SHAPES = {
    "a bootstrap token": r"\b[a-z0-9]{6}\.[a-z0-9]{16}\b",
    "a CA hash": r"sha256:[0-9a-f]{64}",
    "an AWS access key id": r"\b(AKIA|ASIA)[0-9A-Z]{16}\b",
    "a private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "a certificate key": r"--certificate-key",
    "a password or a secret in the code": r"(?i)password|passwd|secret|api[_-]?key",
}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("shape", sorted(SECRET_SHAPES))
def test_the_rendered_user_data_holds_no_token_key_or_password_shape(
    role: str, shape: str
) -> None:
    code = code_of(rendered(role))

    assert re.search(SECRET_SHAPES[shape], code) is None


@pytest.mark.parametrize("role", ROLES)
def test_neither_script_evaluates_text_or_pipes_a_fetched_text_into_a_shell(
    role: str,
) -> None:
    code = code_of(rendered(role))

    forbidden = [
        r"\beval\b",
        r"\b(bash|sh|zsh|dash)\s+-c\b",
        r"\|\s*(sudo\s+)?(ba|z|da)?sh\b",
        r"\|\s*xargs\b",
        r"^\s*(source|\.)\s",
        r"<\(\s*curl",
        r"\$\(\s*curl[^)]*\)\s*$",
        r"`",
    ]
    for pattern in forbidden:
        assert re.search(pattern, code, re.MULTILINE) is None, pattern


@pytest.mark.parametrize(
    ("role", "defaults"),
    [
        (
            "control-plane",
            {"ADDRESS_ATTEMPTS": "60", "PUBLISH_ATTEMPTS": "10", "POLL_SECONDS": "5"},
        ),
        ("worker", {"JOIN_ATTEMPTS": "120", "POLL_SECONDS": "10"}),
    ],
)
def test_the_rendered_user_data_sets_no_test_variable_and_its_defaults_are_real(
    role: str, defaults: dict[str, str]
) -> None:
    code = code_of(rendered(role))

    found = dict(re.findall(r'^(\w+)="\$\{\1:-([^}]*)\}"$', code, re.MULTILINE))

    assert found == {"BOOT_ROOT": "", **defaults}
    # None of them is exported or assigned a literal anywhere else.
    for name in found:
        assert len(re.findall(rf"^(export )?{name}=", code, re.MULTILINE)) == 1


@pytest.mark.parametrize("role", ROLES)
def test_a_script_stops_at_the_first_failure_and_keeps_its_files_in_a_private_directory(
    role: str,
) -> None:
    script = rendered(role)
    code = code_of(script)

    assert script.splitlines()[0] == "#!/bin/bash"
    assert re.search(r"^set -euo pipefail$", code, re.MULTILINE)
    # Only the control plane writes files of its own, and only in a mktemp -d
    # directory (mode 700): the value of the parameter and the manifest.
    assert ("mktemp -d" in code) == (role == "control-plane")


# ── what the scripts do, against stand-ins ──────────────────────────────────


def test_the_control_plane_waits_for_its_address_and_gives_up_at_the_bound(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("imds", [OTHER_ADDRESS])

    done = run_script(scratch, "control-plane")

    line = one_error_line(done, "control-plane")
    assert "gave up after 3 tries" in line
    assert ADDRESS in line
    assert scratch.count_file("imds") == 3  # one reading per try, no more
    assert {call[0] for call in scratch.calls()} == {"curl"}  # nothing else ran


def test_a_node_that_reports_no_address_yet_is_waited_for_not_failed(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("imds", ["", OTHER_ADDRESS, ADDRESS])

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    assert scratch.count_file("imds") == 3
    assert len(scratch.called("kubeadm", "init")) == 1


def test_the_control_plane_asks_the_metadata_service_for_a_session_token_first(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    run_script(scratch, "control-plane")

    first, second = scratch.called("curl")[:2]
    assert "-X" in first and "PUT" in first
    assert first[-1] == "http://169.254.169.254/latest/api/token"
    assert "X-aws-ec2-metadata-token: session" in second
    assert second[-1].endswith("/latest/meta-data/public-ipv4")


def test_the_control_plane_runs_kubeadm_init_once_with_the_expected_arguments(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    (init,) = scratch.called("kubeadm", "init")
    assert init == [
        "kubeadm",
        "init",
        "--control-plane-endpoint",
        f"{ADDRESS}:{PORT}",
        "--apiserver-cert-extra-sans",
        ADDRESS,
        "--pod-network-cidr",
        POD_CIDR,
        "--token-ttl",
        "1h",
        "--skip-token-print",
    ]


def test_the_control_plane_does_things_in_order_and_publishes_last(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    run_script(scratch, "control-plane")

    steps = [
        (call[0], call[1]) if call[0] in {"kubeadm", "kubectl", "aws"} else None
        for call in scratch.calls()
    ]
    steps = [step for step in steps if step]
    assert steps[0] == ("kubeadm", "init")
    assert [s[0] for s in steps] == ["kubeadm", "kubectl", "kubeadm", "aws"]
    assert steps[2] == ("kubeadm", "token")
    assert steps[3] == ("aws", "ssm")


def test_the_control_plane_applies_the_manifest_it_checked_by_its_digest(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    (fetch,) = [c for c in scratch.called("curl") if c[-1].endswith("calico.yaml")]
    assert fetch[-1] == (
        "https://raw.githubusercontent.com/projectcalico/calico/"
        f"{CALICO_VERSION}/manifests/calico.yaml"
    )
    assert "--proto-redir" in fetch and "=https" in fetch
    (apply,) = scratch.called("kubectl")
    assert apply[1:4] == [
        "--kubeconfig",
        str(tmp_path / "root" / "etc" / "kubernetes" / "admin.conf"),
        "apply",
    ]
    assert apply[4] == "--server-side"


def test_a_manifest_with_another_digest_is_refused_and_nothing_is_applied(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    changed = MANIFEST + "# changed\n"
    (tmp_path / "calico.yaml").write_text(changed, encoding="utf-8")

    done = run_script(scratch, "control-plane")

    line = one_error_line(done, "control-plane")
    assert hashlib.sha256(changed.encode()).hexdigest() in line
    assert MANIFEST_SHA256 in line
    assert "nothing was applied" in line
    assert scratch.called("kubectl") == []
    assert scratch.called("aws") == []  # and no join command is published
    assert scratch.called("kubeadm", "token") == []


def test_the_control_plane_writes_one_parameter_with_a_join_commands_shape(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    (put,) = scratch.called("aws", "ssm", "put-parameter")
    assert put[3:5] == ["--region", REGION]
    assert put[5:7] == ["--name", PARAMETER]
    assert "--overwrite" in put
    assert put[put.index("--type") + 1] == "SecureString"
    (value,) = scratch.put_values()
    assert value == VALID  # the join command, with no newline after it


def test_the_join_command_reaches_the_cli_through_a_file_and_no_argument_or_log(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    arguments = "\n".join(" ".join(call) for call in scratch.calls())
    for part in (BOOTSTRAP_ID, CA_DIGEST):
        assert part not in arguments
        assert part not in done.stdout + done.stderr
    (put,) = scratch.called("aws", "ssm", "put-parameter")
    assert put[put.index("--value") + 1].startswith("file://")


def test_what_kubeadm_prints_is_checked_before_it_is_published(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    other = VALID.replace(ADDRESS, OTHER_ADDRESS)
    (tmp_path / "join-line").write_text(other + "\n", encoding="utf-8")

    done = run_script(scratch, "control-plane")

    line = one_error_line(done, "control-plane")
    assert "nothing was published" in line
    assert scratch.called("aws") == []


def test_a_parameter_that_cannot_be_written_is_tried_to_the_bound_then_one_line(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "put-failures").write_text("99", encoding="utf-8")

    done = run_script(scratch, "control-plane")

    assert done.returncode == 1
    assert len(done.stderr.splitlines()) == 1
    assert "gave up after 3 tries" in done.stderr
    assert len(scratch.called("aws", "ssm", "put-parameter")) == 3


def test_a_parameter_that_fails_once_is_written_on_the_second_try(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "put-failures").write_text("1", encoding="utf-8")

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    assert len(scratch.called("aws", "ssm", "put-parameter")) == 2


def test_a_second_run_on_the_same_node_does_not_run_kubeadm_init_again(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    first = run_script(scratch, "control-plane")

    second = run_script(scratch, "control-plane")

    assert first.returncode == 0, first.stderr
    line = one_error_line(second, "control-plane")
    assert "runs once per node" in line
    assert len(scratch.called("kubeadm", "init")) == 1


@pytest.mark.parametrize("role", ROLES)
def test_a_signing_key_with_another_fingerprint_is_refused_before_any_package(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "fingerprint").write_text(OTHER_FINGERPRINT, encoding="utf-8")

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert OTHER_FINGERPRINT in line
    assert "refusing to trust it" in line
    for install in scratch.called("apt-get", "install"):
        assert "kubeadm" not in install
    assert scratch.called("kubeadm") == []


@pytest.mark.parametrize("role", ROLES)
def test_the_node_is_set_up_from_the_pinned_repository_and_containerd_uses_systemd(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)

    run_script(scratch, role)

    root = tmp_path / "root"
    sources = (root / "etc/apt/sources.list.d/kubernetes.list").read_text()
    assert sources == (
        "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] "
        f"https://pkgs.k8s.io/core:/stable:/v{MINOR}/deb/ /\n"
    )
    key_fetch = [c for c in scratch.called("curl") if c[-1].endswith("Release.key")]
    assert (
        key_fetch[0][-1]
        == f"https://pkgs.k8s.io/core:/stable:/v{MINOR}/deb/Release.key"
    )
    assert ["apt-mark", "hold", "kubelet", "kubeadm", "kubectl"] in scratch.calls()
    config = (root / "etc/containerd/config.toml").read_text()
    assert "SystemdCgroup = true" in config
    assert "SystemdCgroup = false" not in config
    assert (root / "etc/sysctl.d/k8s.conf").read_text() == "net.ipv4.ip_forward = 1\n"
    assert ["systemctl", "restart", "containerd"] in scratch.calls()
    assert ["snap", "install", "aws-cli", "--classic"] in scratch.calls()


@pytest.mark.parametrize("role", ROLES)
def test_a_containerd_configuration_without_one_cgroup_setting_stops_the_script(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "containerd.toml").write_text("version = 3\n", encoding="utf-8")

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert "SystemdCgroup" in line
    assert scratch.called("kubeadm") == []


@pytest.mark.parametrize(
    ("role", "setting"),
    [
        ("control-plane", "ADDRESS_ATTEMPTS"),
        ("control-plane", "POLL_SECONDS"),
        ("worker", "JOIN_ATTEMPTS"),
        ("worker", "POLL_SECONDS"),
    ],
)
@pytest.mark.parametrize("value", ["ten", "-1", "1e3", "5; touch x", "1234567"])
def test_a_bound_that_is_not_a_whole_number_is_refused_before_anything_runs(
    tmp_path: Path, role: str, setting: str, value: str
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, role, **{setting: value})

    line = one_error_line(done, role)
    assert setting in line
    assert scratch.calls() == []
    assert not (tmp_path / "x").exists()


# ── a worker ─────────────────────────────────────────────────────────────────

PLACEHOLDER_VALUE = "not-yet-written"


def test_a_worker_polls_the_placeholder_to_the_bound_and_gives_up_with_one_line(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("param", [PLACEHOLDER_VALUE + "\n"])

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")
    assert "gave up after 3 tries" in line
    assert PARAMETER in line
    assert len(scratch.called("aws", "ssm", "get-parameter")) == 3
    assert scratch.called("kubeadm") == []


def test_a_worker_reads_the_parameter_with_decryption_by_its_name(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    run_script(scratch, "worker")

    (read, *_) = scratch.called("aws", "ssm", "get-parameter")
    assert read[3:] == [
        "--region",
        REGION,
        "--name",
        PARAMETER,
        "--with-decryption",
        "--query",
        "Parameter.Value",
        "--output",
        "text",
    ]


def test_a_worker_joins_with_exactly_the_fields_of_a_well_shaped_value(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "worker")

    assert done.returncode == 0, done.stderr
    (join,) = scratch.called("kubeadm")
    assert join == [
        "kubeadm",
        "join",
        f"{ADDRESS}:{PORT}",
        "--token",
        BOOTSTRAP_ID,
        "--discovery-token-ca-cert-hash",
        f"sha256:{CA_DIGEST}",
    ]
    assert BOOTSTRAP_ID not in done.stdout + done.stderr  # the value is not logged


def test_a_worker_keeps_polling_until_the_control_plane_has_written_the_parameter(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("param", [PLACEHOLDER_VALUE + "\n", "\n", VALID + "\n"])

    done = run_script(scratch, "worker")

    assert done.returncode == 0, done.stderr
    assert len(scratch.called("aws", "ssm", "get-parameter")) == 3
    assert len(scratch.called("kubeadm", "join")) == 1


def test_a_failed_read_is_a_try_not_a_crash(tmp_path: Path) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("param", ["", VALID])
    (tmp_path / "param.0.fail").touch()

    done = run_script(scratch, "worker")

    assert done.returncode == 0, done.stderr
    assert len(scratch.called("aws", "ssm", "get-parameter")) == 2


@pytest.mark.parametrize("trailing", ["", " ", "\n", " \n"])
def test_one_trailing_space_or_newline_is_allowed_after_a_join_command(
    tmp_path: Path, trailing: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("param", [VALID + trailing])

    done = run_script(scratch, "worker")

    assert done.returncode == 0, done.stderr
    assert len(scratch.called("kubeadm", "join")) == 1


HOSTILE: dict[str, Callable[[str], str]] = {
    "a command after a semicolon": lambda c: f"{VALID}; touch {c}",
    "a command after a semicolon and a space": lambda c: f"{VALID} ; touch {c}",
    "a command after two ampersands": lambda c: f"{VALID} && touch {c}",
    "a command after a pipe": lambda c: f"{VALID} | touch {c}",
    "a backtick in the token": lambda c: VALID.replace(BOOTSTRAP_ID, f"`touch {c}`"),
    "a backtick after the command": lambda c: f"{VALID} `touch {c}`",
    "a command substitution in the token": lambda c: VALID.replace(
        BOOTSTRAP_ID, f"$(touch {c})"
    ),
    "a command substitution after the command": lambda c: f"{VALID} $(touch {c})",
    "a command substitution in the address": lambda c: VALID.replace(
        ADDRESS, f"$(touch {c})"
    ),
    "a newline and a second command": lambda c: f"{VALID}\ntouch {c}",
    "a newline before the command": lambda c: f"touch {c}\n{VALID}",
    "a carriage return and a second command": lambda c: f"{VALID}\rtouch {c}",
    "a second join command on a second line": lambda c: f"{VALID}\n{VALID}",
    "an extra flag": lambda c: f"{VALID} --ignore-preflight-errors=all",
    "an extra flag with a path": lambda c: f"{VALID} --config /tmp/{c}",
    "a flag before the token": lambda c: VALID.replace(
        "--token", "--skip-phases=preflight --token"
    ),
    "two trailing spaces": lambda c: f"{VALID}  ",
    "a leading space": lambda c: f" {VALID}",
    "a leading newline and a trailing letter": lambda c: f"\n{VALID}x",
    "a token one character short": lambda c: VALID.replace(
        BOOTSTRAP_ID, BOOTSTRAP_ID[:-1]
    ),
    "a token one character long": lambda c: VALID.replace(
        BOOTSTRAP_ID, BOOTSTRAP_ID + "1"
    ),
    "a token with a short first part": lambda c: VALID.replace(
        BOOTSTRAP_ID, "aaaaa.11111111111111111"
    ),
    "a token in capitals": lambda c: VALID.replace(BOOTSTRAP_ID, BOOTSTRAP_ID.upper()),
    "a token with a dash": lambda c: VALID.replace(
        BOOTSTRAP_ID, "aaaaaa.-111111111111111"
    ),
    "a hash one digit short": lambda c: VALID.replace(CA_DIGEST, CA_DIGEST[:-1]),
    "a hash one digit long": lambda c: VALID.replace(CA_DIGEST, CA_DIGEST + "2"),
    "a hash in capitals": lambda c: VALID.replace(CA_DIGEST, "A" * 64),
    "a hash that is not hexadecimal": lambda c: VALID.replace(CA_DIGEST, "g" * 64),
    "an address that is not the control plane's": lambda c: VALID.replace(
        ADDRESS, OTHER_ADDRESS
    ),
    "a port that is not the API server's": lambda c: VALID.replace(":6443", ":6444"),
    "a host name for the address": lambda c: VALID.replace(
        ADDRESS, "control-plane.example.invalid"
    ),
    "the address and a second one": lambda c: VALID.replace(
        ADDRESS, f"{ADDRESS},{OTHER_ADDRESS}"
    ),
    "another program": lambda c: VALID.replace("kubeadm join", "touch"),
    "the join command without the word join": lambda c: VALID.replace(
        "kubeadm join", "kubeadm"
    ),
    "the placeholder": lambda c: PLACEHOLDER_VALUE,
    "an empty value": lambda c: "",
    "only white space": lambda c: " \n \t",
    "the word None that the CLI prints for nothing": lambda c: "None",
    "a command alone": lambda c: f"touch {c}",
    "a variable in place of the token": lambda c: VALID.replace(
        BOOTSTRAP_ID, "$BOOTSTRAP"
    ),
}


@pytest.mark.parametrize("name", sorted(HOSTILE), ids=sorted(HOSTILE))
def test_a_value_that_is_not_exactly_a_join_command_joins_nothing_and_runs_nothing(
    tmp_path: Path, name: str
) -> None:
    scratch = make_scratch(tmp_path)
    canary = tmp_path / "canary"
    scratch.series("param", [HOSTILE[name](str(canary))])

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")
    assert "gave up after 3 tries" in line
    assert not canary.exists()  # none of the text was run
    assert scratch.called("kubeadm") == []
    reads = scratch.called("aws", "ssm", "get-parameter")
    assert len(reads) == 3
    # After the packages, the only calls are the reads of the parameter.
    last_install = max(
        number
        for number, call in enumerate(scratch.calls())
        if call[0] in {"apt-get", "apt-mark", "systemctl", "snap"}
    )
    assert {c[0] for c in scratch.calls()[last_install + 1 :]} <= {"aws"}
    for call in scratch.calls():
        assert "touch" not in call and str(canary) not in " ".join(call)


def test_a_worker_that_already_joined_does_not_join_again(tmp_path: Path) -> None:
    scratch = make_scratch(tmp_path)
    first = run_script(scratch, "worker")

    second = run_script(scratch, "worker")

    assert first.returncode == 0, first.stderr
    line = one_error_line(second, "worker")
    assert "runs once per node" in line
    assert len(scratch.called("kubeadm", "join")) == 1
