"""The two boot scripts of the self-managed cluster, run against stand-ins (S079).

``infra/terraform/aws-kubeadm/templates/`` holds the cloud-init scripts of the
control-plane node and of a worker, and the text they share. Nothing here
touches a cloud, a cluster or the machine's own configuration: each script is
rendered with fixed inputs, linted, and run whole under bash against stand-in
programs, each of which logs its arguments. The fixed inputs, the rendering,
the stand-ins and the runner are in ``awskubeadmsupport``.
"""

import base64
import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

import pytest
from awskubeadmsupport import (
    ADDRESS,
    APT_LOCK,
    BOOTSTRAP_ID,
    CA_DIGEST,
    CALICO_VERSION,
    COMMON_VALUES,
    CONTROL_PLANE_VALUES,
    FINGERPRINT,
    KERNEL_MODULES,
    MANIFEST,
    MANIFEST_SHA256,
    MINOR,
    OTHER_ADDRESS,
    OTHER_FINGERPRINT,
    PARAMETER,
    PLACEHOLDER,
    POD_CIDR,
    PORT,
    RAW_GUARD,
    REGION,
    ROLES,
    SECONDS,
    SYSCTL_FILE,
    TEMPLATES,
    USER_DATA_LIMIT,
    VALID,
    WORKER_VALUES,
    code_of,
    make_scratch,
    one_error_line,
    rendered,
    run_script,
    template_text,
    user_data_sizes,
)

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
def test_a_rendered_script_gzipped_fits_in_the_16_kb_ec2_allows_for_user_data(
    role: str,
) -> None:
    # The EC2 page "Run commands when you launch an EC2 instance with user data
    # input" (read 2026-10-07): "User data is limited to 16 KB, in raw form,
    # before it is base64-encoded." The instances send gzip (user_data_base64 =
    # base64gzip(...), which cloud-init unpacks), so the bytes that count are the
    # compressed ones.
    raw, compressed = user_data_sizes(role)

    assert compressed <= USER_DATA_LIMIT, (
        f"{role}: {compressed} gzipped bytes (raw {raw}) of {USER_DATA_LIMIT}"
    )


@pytest.mark.parametrize("role", ROLES)
def test_a_rendered_script_stays_under_64_kib_raw_against_runaway_growth(
    role: str,
) -> None:
    raw, compressed = user_data_sizes(role)

    assert raw < RAW_GUARD, f"{role}: {raw} raw bytes (gzipped {compressed})"


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

    assert found == {"BOOT_ROOT": "", "CONTAINERD_ATTEMPTS": "20", **defaults}
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
    # Every file a script writes of its own (the signing key, and the control
    # plane's manifest and join command) is in a mktemp -d directory (mode 700).
    assert "mktemp -d" in code


@pytest.mark.parametrize("role", ROLES)
def test_a_script_pins_the_alphabet_of_its_patterns_beside_its_error_handling(
    role: str,
) -> None:
    code = code_of(rendered(role))

    lines = code.splitlines()
    assert lines.index("export LC_ALL=C") == lines.index("set -euo pipefail") + 1
    # HOME beside it: cloud-init may start a user-data script without one, and gpg
    # and snap are the first programs that look for it.
    assert lines.index("export HOME=/root") == lines.index("export LC_ALL=C") + 1


@pytest.mark.parametrize("role", ROLES)
def test_every_program_a_script_starts_has_root_as_its_home(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, role)  # the runner's HOME is the scratch directory

    assert done.returncode == 0, done.stderr
    homes = (tmp_path / "homes").read_text(encoding="utf-8").splitlines()
    assert len(homes) == len(scratch.calls()) > 10
    assert set(homes) == {"/root"}


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


def test_the_join_command_comes_from_a_token_that_lives_one_hour(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    (token,) = scratch.called("kubeadm", "token")
    assert token == [
        "kubeadm",
        "token",
        "create",
        "--ttl",
        "1h",
        "--print-join-command",
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
    (apply,) = scratch.called("kubectl")
    assert apply[1:4] == [
        "--kubeconfig",
        str(tmp_path / "root" / "etc" / "kubernetes" / "admin.conf"),
        "apply",
    ]
    assert apply[4] == "--server-side"


def test_the_control_plane_applies_the_local_file_it_checked_and_not_a_url(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    (apply,) = scratch.called("kubectl")
    assert apply[5] == "-f"
    target = apply[6]
    assert len(apply) == 7
    assert "://" not in target
    # The one file that was fetched (and digested) is the one that is applied:
    # a directory under the script's TMPDIR, and its bytes are the pinned ones.
    assert Path(target).name == "calico.yaml"
    assert Path(target).parent.parent == tmp_path / "tmp"
    assert (tmp_path / "applied").read_text(encoding="utf-8") == MANIFEST
    assert len([c for c in scratch.called("curl") if "calico" in c[-1]]) == 1


@pytest.mark.parametrize("role", ROLES)
def test_every_download_is_https_only_tls12_failing_and_bounded_in_time(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, role)

    assert done.returncode == 0, done.stderr
    fetches = [c for c in scratch.called("curl") if c[-1].startswith("https://")]
    expected = ["Release.key"] + (["calico.yaml"] if role == "control-plane" else [])
    assert sorted(c[-1].rsplit("/", 1)[1] for c in fetches) == sorted(expected)
    for call in fetches:
        # The exact pairs, in order: a redirect flag does not stand in for the
        # protocol flag.
        pairs = list(pairwise(call))
        assert ("--proto", "=https") in pairs, call
        assert ("--proto-redir", "=https") in pairs, call
        assert "--tlsv1.2" in call
    for call in scratch.called("curl"):
        assert "--fail" in call, call
        assert "--max-time" in call, call
        assert int(call[call.index("--max-time") + 1]) > 0


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


ONE_RUN_MARKER = {"control-plane": "admin.conf", "worker": "kubelet.conf"}


@pytest.mark.parametrize("role", ROLES)
def test_a_second_run_on_the_same_node_calls_nothing_and_says_why_it_stopped(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    first = run_script(scratch, role)
    calls_of_the_first_run = scratch.calls()

    second = run_script(scratch, role)

    assert first.returncode == 0, first.stderr
    line = one_error_line(second, role)  # one line, and the exit status 1
    assert "runs once per node" in line
    assert ONE_RUN_MARKER[role] in line
    # Not an apt-get, a snap, a systemctl restart or a kubeadm: not a call.
    assert scratch.calls() == calls_of_the_first_run


@pytest.mark.parametrize("role", ROLES)
def test_a_node_with_the_mark_of_its_one_run_is_refused_before_it_is_touched(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    kubernetes = tmp_path / "root" / "etc" / "kubernetes"
    kubernetes.mkdir(parents=True)
    (kubernetes / ONE_RUN_MARKER[role]).touch()

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert "runs once per node" in line
    assert scratch.calls() == []  # not even a read of the metadata service
    assert list((tmp_path / "root").rglob("*")) == [
        tmp_path / "root" / "etc",
        kubernetes,
        kubernetes / ONE_RUN_MARKER[role],
    ]  # no sysctl file, no containerd configuration, no apt source


@pytest.mark.parametrize("role", ROLES)
def test_a_signing_key_with_another_fingerprint_is_refused_before_any_package(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.listing((OTHER_FINGERPRINT, ""))

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert OTHER_FINGERPRINT in line
    assert "refusing to trust it" in line
    installs = scratch.called("apt-get", *APT_LOCK, "install")
    assert installs  # the distribution's packages were installed, before the key
    for install in installs:
        assert "kubeadm" not in install
    assert scratch.called("kubeadm") == []


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    "keys",
    [
        [(FINGERPRINT, ""), (OTHER_FINGERPRINT, "")],
        [(OTHER_FINGERPRINT, ""), (FINGERPRINT, "")],
        [(FINGERPRINT, ""), (FINGERPRINT, "")],
        [],
    ],
    ids=["the pinned key first", "the pinned key second", "the pinned twice", "none"],
)
def test_a_key_file_that_does_not_hold_exactly_one_primary_key_is_refused_unwritten(
    tmp_path: Path, role: str, keys: list[tuple[str, str]]
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.listing(*keys)

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert "exactly one" in line
    # Nothing is written under /etc/apt, no keyring is made and the repository is
    # not added: the downloaded key stays in the script's private directory.
    assert not (tmp_path / "root" / "etc" / "apt").exists()
    assert scratch.called("apt-get", *APT_LOCK, "install", "-y", "kubelet") == []
    assert scratch.called("kubeadm") == []


@pytest.mark.parametrize("role", ROLES)
def test_the_key_that_is_trusted_is_the_one_that_was_listed_and_checked(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, role)

    assert done.returncode == 0, done.stderr
    keyring = tmp_path / "root" / "etc" / "apt" / "keyrings"
    assert [p.name for p in keyring.iterdir()] == ["kubernetes-apt-keyring.gpg"]
    assert (keyring / "kubernetes-apt-keyring.gpg").stat().st_mode & 0o777 == 0o644
    listed = [c for c in scratch.called("gpg") if "--show-keys" in c]
    (dearmor,) = [c for c in scratch.called("gpg") if "--dearmor" in c]
    # The listing is of the file that dearmor made, not of the downloaded one.
    assert listed[-1][-1] == dearmor[dearmor.index("--output") + 1]
    assert listed[-1][-1] != dearmor[-1]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("expiry", ["1", "1000000000"])
def test_a_key_that_has_expired_is_refused_with_one_line_before_the_repository(
    tmp_path: Path, role: str, expiry: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.listing((FINGERPRINT, expiry))

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert "expired" in line
    assert FINGERPRINT in line
    assert not (tmp_path / "root" / "etc" / "apt").exists()
    assert scratch.called("kubeadm") == []


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("expiry", ["", "4102444800"])  # no expiry; the year 2100
def test_a_key_with_no_expiry_or_one_in_the_future_is_accepted(
    tmp_path: Path, role: str, expiry: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.listing((FINGERPRINT, expiry))

    done = run_script(scratch, role)

    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("expiry", ["2026-12-29", "$(touch x)", "1e9", "-5"])
def test_an_expiry_that_is_not_a_number_is_refused_and_never_evaluated(
    tmp_path: Path, role: str, expiry: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.listing((FINGERPRINT, expiry))

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert "expiry" in line
    assert not (tmp_path / "x").exists()
    assert not (tmp_path / "root" / "etc" / "apt").exists()


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
    assert (root / "etc/sysctl.d/k8s.conf").read_text() == SYSCTL_FILE
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


@pytest.mark.parametrize("role", ROLES)
def test_every_apt_get_call_waits_up_to_300_seconds_for_the_package_lock(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, role)

    assert done.returncode == 0, done.stderr
    calls = scratch.called("apt-get")
    assert [call[3] for call in calls] == ["update", "install", "update", "install"]
    for call in calls:
        assert ("-o", "DPkg::Lock::Timeout=300") in pairwise(call)
        assert call[1:3] == list(APT_LOCK)  # before the verb, as apt-get reads it
    # And in the text: no apt-get line of the script is without the option.
    lines = re.findall(r"^\s*apt-get .*$", code_of(rendered(role)), re.MULTILINE)
    assert len(lines) == 4
    assert all(" -o DPkg::Lock::Timeout=300 " in line for line in lines)


@pytest.mark.parametrize("role", ROLES)
def test_the_node_loads_overlay_and_br_netfilter_now_and_at_every_boot(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, role)

    assert done.returncode == 0, done.stderr
    # One module a call: modprobe reads the names after the first as parameters.
    assert scratch.called("modprobe") == [["modprobe", m] for m in KERNEL_MODULES]
    boot_file = tmp_path / "root" / "etc" / "modules-load.d" / "k8s.conf"
    assert boot_file.read_text(encoding="utf-8") == "overlay\nbr_netfilter\n"


@pytest.mark.parametrize("role", ROLES)
def test_the_modules_are_loaded_before_the_sysctl_settings_are_applied(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, role)  # the sysctl stand-in refuses bridge keys
    #                                   when br_netfilter was not loaded first

    assert done.returncode == 0, done.stderr
    order = [call[0] for call in scratch.calls() if call[0] in {"modprobe", "sysctl"}]
    assert order == ["modprobe", "modprobe", "sysctl"]
    assert scratch.called("sysctl") == [["sysctl", "--system"]]
    settings = (tmp_path / "root" / "etc" / "sysctl.d" / "k8s.conf").read_text()
    assert settings == SYSCTL_FILE


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("module", KERNEL_MODULES)
def test_a_module_that_does_not_load_stops_the_script_before_settings_or_packages(
    tmp_path: Path, role: str, module: str
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "modprobe-fails").write_text(module, encoding="utf-8")

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert module in line
    assert scratch.called("sysctl") == []
    assert scratch.called("apt-get") == []


@pytest.mark.parametrize("role", ROLES)
def test_the_script_waits_for_containerd_to_answer_and_goes_on_at_the_third_try(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("ctr", ["", "", "Client:\n"])
    (tmp_path / "ctr.0.fail").touch()
    (tmp_path / "ctr.1.fail").touch()

    done = run_script(scratch, role)  # three tries is the bound of this run

    assert done.returncode == 0, done.stderr
    assert scratch.called("ctr") == [["ctr", "version"]] * 3
    calls = scratch.calls()
    restart = calls.index(["systemctl", "restart", "containerd"])
    asked = [n for n, call in enumerate(calls) if call[0] == "ctr"]
    assert restart < asked[0]
    assert asked[-1] < min(n for n, call in enumerate(calls) if call[0] == "kubeadm")


@pytest.mark.parametrize("role", ROLES)
def test_a_containerd_that_never_answers_ends_the_script_after_the_bound_with_one_line(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "ctr.0.fail").touch()  # the one answer of the series is a failure

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert "gave up after 3 tries" in line
    assert "containerd" in line
    assert len(scratch.called("ctr")) == 3
    assert scratch.called("kubeadm") == []
    assert ["systemctl", "enable", "kubelet"] not in scratch.calls()


@pytest.mark.parametrize("role", ROLES)
def test_an_answer_one_try_after_the_bound_is_too_late(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("ctr", ["", "", "", "Client:\n"])
    for number in range(3):
        (tmp_path / f"ctr.{number}.fail").touch()

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert "gave up after 3 tries" in line
    assert len(scratch.called("ctr")) == 3


@pytest.mark.parametrize("role", ROLES)
def test_a_socket_that_never_appears_is_never_asked_and_ends_the_script(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "no-socket").touch()  # the restart makes no socket

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert "gave up after 3 tries" in line
    assert "containerd.sock" in line
    assert scratch.called("ctr") == []
    assert scratch.called("kubeadm") == []


@pytest.mark.parametrize(
    ("role", "setting"),
    [
        ("control-plane", "ADDRESS_ATTEMPTS"),
        ("control-plane", "POLL_SECONDS"),
        ("control-plane", "CONTAINERD_ATTEMPTS"),
        ("worker", "JOIN_ATTEMPTS"),
        ("worker", "POLL_SECONDS"),
        ("worker", "CONTAINERD_ATTEMPTS"),
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
    "the flag that skips the CA check in place of the hash flag": lambda c: (
        VALID.replace(
            f"--discovery-token-ca-cert-hash sha256:{CA_DIGEST}",
            "--discovery-token-unsafe-skip-ca-verification",
        )
    ),
    "the flag that skips the CA check after the hash flag": lambda c: (
        f"{VALID} --discovery-token-unsafe-skip-ca-verification"
    ),
    "a token that starts with a dash": lambda c: VALID.replace(
        BOOTSTRAP_ID, "-aaaaa.1111111111111111"
    ),
    "a tab in place of the first space": lambda c: VALID.replace(" ", "\t", 1),
    "a tab in place of the space before the token flag": lambda c: VALID.replace(
        " --token", "\t--token"
    ),
    "a tab in place of the space before the hash flag": lambda c: VALID.replace(
        " --discovery", "\t--discovery"
    ),
    "a carriage return and a newline at the end": lambda c: f"{VALID}\r\n",
    "a carriage return at the end": lambda c: f"{VALID}\r",
    "a port with a leading zero": lambda c: VALID.replace(":6443", ":06443"),
    "an address with a leading zero in an octet": lambda c: VALID.replace(
        ADDRESS, "203.0.113.010"
    ),
    "a SHA-512 prefix and a 128-digit hash": lambda c: VALID.replace(
        f"sha256:{CA_DIGEST}", f"sha512:{'2' * 128}"
    ),
    "a SHA-512 prefix on a 64-digit hash": lambda c: VALID.replace(
        "sha256:", "sha512:"
    ),
    "a hash with no prefix": lambda c: VALID.replace("sha256:", ""),
    "the token flag with an equals sign": lambda c: VALID.replace(
        "--token ", "--token="
    ),
    "the hash flag with an equals sign": lambda c: VALID.replace(
        "--discovery-token-ca-cert-hash sha256", "--discovery-token-ca-cert-hash=sha256"
    ),
    "a duplicated token flag": lambda c: f"{VALID} --token bbbbbb.2222222222222222",
    "a duplicated hash flag": lambda c: (
        f"{VALID} --discovery-token-ca-cert-hash sha256:{'3' * 64}"
    ),
    "a value of 4 KB with no command in it": lambda c: "a" * 4096,
    "a value of 4 KB after the command": lambda c: f"{VALID} {'a' * 4096}",
    "a value of 4 KB in the token": lambda c: VALID.replace(BOOTSTRAP_ID, "a" * 4096),
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


ACCESS_DENIED = (
    "An error occurred (AccessDeniedException) when calling the GetParameter "
    "operation: the role is not authorized to perform ssm:GetParameter with an "
    "explicit deny in an identity-based policy"
)
THROTTLED = "An error occurred (ThrottlingException) when calling the GetParameter"


def failing_reads(tmp_path: Path, *stderr: bytes | None) -> None:
    """The parameter reads of a worker, one per entry, each exiting 255 and
    printing the entry on its error stream (nothing for None)."""
    for number, text in enumerate(stderr):
        (tmp_path / f"param.{number}.fail").touch()
        if text is not None:
            (tmp_path / f"param.{number}.err").write_bytes(text)


def gave_up(tmp_path: Path, *stderr: bytes | None) -> tuple[str, str]:
    """Runs a worker whose reads all fail as given: its one error line, and what
    the whole run printed."""
    scratch = make_scratch(tmp_path)
    scratch.series("param", [""] * len(stderr))
    failing_reads(tmp_path, *stderr)

    done = run_script(scratch, "worker")

    return one_error_line(done, "worker"), done.stdout + done.stderr


def test_a_worker_refused_by_the_service_says_so_when_it_gives_up(
    tmp_path: Path,
) -> None:
    error = ACCESS_DENIED.encode()
    assert len(ACCESS_DENIED) <= 200  # the whole line is carried, not a cut of it

    line, output = gave_up(tmp_path, error + b"\n", error + b"\n", error + b"\n")

    assert "gave up after 3 tries" in line
    assert PARAMETER in line
    assert ACCESS_DENIED in line
    assert len(output.splitlines()) > 1  # the tries are logged, one line each
    # The directory of the script's files is gone, the error file with it.
    assert list((tmp_path / "tmp").iterdir()) == []


def test_only_the_first_line_of_the_last_error_reaches_the_line(
    tmp_path: Path,
) -> None:
    error = f"{ACCESS_DENIED}\nthe second line is not shown\nnor the third\n"

    line, _ = gave_up(tmp_path, error.encode())

    assert ACCESS_DENIED in line
    assert "second line" not in line
    assert "third" not in line


def test_the_last_error_is_the_one_the_line_carries_not_the_first(
    tmp_path: Path,
) -> None:
    line, _ = gave_up(
        tmp_path, THROTTLED.encode() + b"\n", b"", ACCESS_DENIED.encode() + b"\n"
    )

    assert ACCESS_DENIED in line
    assert "ThrottlingException" not in line


def test_an_error_that_a_later_read_without_one_replaced_is_not_carried(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("param", ["", PLACEHOLDER_VALUE + "\n"])  # then it only polls
    failing_reads(tmp_path, ACCESS_DENIED.encode() + b"\n")

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")
    assert "gave up after 3 tries" in line
    assert "AccessDeniedException" not in line


def test_a_failed_read_that_printed_nothing_is_said_to_have_printed_nothing(
    tmp_path: Path,
) -> None:
    line, _ = gave_up(tmp_path, None)

    assert "gave up after 3 tries" in line
    assert "printed no message" in line


def test_the_error_is_cut_to_200_characters(tmp_path: Path) -> None:
    error = b"E" + b"x" * 5000 + b"\n"

    line, _ = gave_up(tmp_path, error)

    assert "E" + "x" * 199 in line
    assert "x" * 200 not in line


def test_the_error_is_cut_even_when_it_has_no_newline(tmp_path: Path) -> None:
    error = b"E" + b"x" * 5000  # a first line that is also the last, and long

    line, _ = gave_up(tmp_path, error)

    assert "E" + "x" * 199 in line
    assert "x" * 200 not in line


def test_every_character_outside_printable_ascii_is_replaced_in_the_line(
    tmp_path: Path,
) -> None:
    raw = "An error: café‮ \x01\ttab\x1b[31m\rend".encode()

    line, output = gave_up(tmp_path, raw + b"\n")

    expected = re.sub(rb"[^ -~]", b"?", raw).decode("ascii")
    assert expected in line
    assert "?" in line
    assert line.isascii() and line.isprintable()
    assert "\x1b" not in output


def test_a_nul_byte_in_the_error_is_dropped_with_no_bash_warning(
    tmp_path: Path,
) -> None:
    line, _ = gave_up(tmp_path, b"An error\x00 occurred\n")  # one stderr line

    assert "An error occurred" in line
    assert "null byte" not in line


JOIN_SHAPED = {
    "a whole join command": f"An error occurred: bad value {VALID}",
    "a bootstrap token": f"An error occurred: bad value {BOOTSTRAP_ID}",
    "a CA hash": f"An error occurred: bad value {CA_DIGEST}",
    "a CA hash with its prefix": f"An error occurred: sha256:{CA_DIGEST}",
}


@pytest.mark.parametrize("name", sorted(JOIN_SHAPED), ids=sorted(JOIN_SHAPED))
def test_an_error_line_that_holds_the_shape_of_a_join_command_is_withheld(
    tmp_path: Path, name: str
) -> None:
    error = (JOIN_SHAPED[name] + "\n").encode()

    line, output = gave_up(tmp_path, error)

    assert "gave up after 3 tries" in line
    assert "withheld" in line
    for part in (BOOTSTRAP_ID, CA_DIGEST, "kubeadm join"):
        assert part not in output


def test_the_masking_does_not_hide_an_ordinary_service_error(tmp_path: Path) -> None:
    line, _ = gave_up(tmp_path, (ACCESS_DENIED + "\n").encode())

    assert "withheld" not in line


def test_a_join_that_fails_says_what_to_do_and_is_not_tried_again(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "join-fails").touch()

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")
    assert line == (
        "worker: ERROR: kubeadm join failed. The join command in the parameter may "
        "be older than the control plane (the control plane was replaced and the "
        "parameter kept): remove this environment and apply it again."
    )
    assert len(scratch.called("kubeadm", "join")) == 1  # one shot, no retry
    assert len(scratch.called("aws", "ssm", "get-parameter")) == 1


# The three strings of the security review, each of which a pattern with the
# alphabet of a UTF-8 locale accepts: fullwidth digits, accented letters and
# Arabic-Indic digits.
FULLWIDTH_ONE = chr(0xFF11)
E_ACUTE = chr(0xE9)
ARABIC_INDIC_TWO = chr(0x662)
UNICODE_VALUES = {
    "fullwidth digits in the token": VALID.replace(
        BOOTSTRAP_ID, "aaaaaa." + FULLWIDTH_ONE * 16
    ),
    "accented letters in the token": VALID.replace(
        BOOTSTRAP_ID, E_ACUTE * 5 + "1." + "1" * 16
    ),
    "Arabic-Indic digits in the hash": VALID.replace(CA_DIGEST, ARABIC_INDIC_TWO * 64),
}


def a_utf8_locale() -> str:
    """The locale in which the old pattern, with no pinned alphabet, accepts the
    strings above: en_US.UTF-8 when the machine has it (the review's finding was
    made there), else C.UTF-8."""
    done = subprocess.run(["locale", "-a"], capture_output=True, text=True, check=False)
    names = {n.lower().replace("utf-8", "utf8") for n in done.stdout.split()}
    return "en_US.UTF-8" if "en_us.utf8" in names else "C.UTF-8"


@pytest.mark.parametrize("name", sorted(UNICODE_VALUES), ids=sorted(UNICODE_VALUES))
def test_the_pattern_refuses_non_ascii_digits_and_letters_in_a_utf8_locale(
    tmp_path: Path, name: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("param", [UNICODE_VALUES[name]])

    done = run_script(scratch, "worker", LC_ALL=a_utf8_locale())

    line = one_error_line(done, "worker")
    assert "gave up after 3 tries" in line  # the pattern refused it, not kubeadm
    assert scratch.called("kubeadm") == []


NUL_VALUES = {
    "a NUL after the command": VALID + "\x00",
    "a NUL and a newline after the command": VALID + "\x00\n",
    "a NUL inside the token": VALID.replace(
        BOOTSTRAP_ID, "aaa\x00aaa.1111111111111111"
    ),
}


@pytest.mark.parametrize("name", sorted(NUL_VALUES), ids=sorted(NUL_VALUES))
def test_a_nul_byte_in_the_value_is_a_refusal_with_one_line_and_no_bash_warning(
    tmp_path: Path, name: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("param", [NUL_VALUES[name].encode("utf-8")])

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")  # nothing else on stderr: no warning
    assert "gave up after 3 tries" in line
    assert "null byte" not in done.stderr
    assert scratch.called("kubeadm") == []
