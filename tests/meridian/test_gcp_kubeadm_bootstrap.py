"""The twin's two boot scripts, run against stand-ins (S079).

``infra/terraform/gcp-kubeadm/templates/`` holds the cloud-init scripts of the
control-plane node and of a worker of the Google Cloud twin, and the text they
share. Nothing here touches a cloud, a cluster or the machine's own
configuration: each script is rendered with fixed inputs, linted, and run whole
under bash against stand-in programs, each of which logs its arguments. The
fixed inputs, the rendering, the stand-ins and the runner are in
``gcpkubeadmsupport``. That the cloud-neutral functions are the AWS module's
text is held by ``test_gcp_kubeadm_same_text.py``; the tests here run them in the
twin all the same, because a stand-in that differs would show it. The hostile
values a worker may read, the Unicode and NUL values and the join-shaped error
lines are the AWS tests' own lists, imported so that a case added there is run
here.
"""

import base64
import hashlib
import os
import re
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest
from awskubeadmsupport import (
    APT_LOCK,
    BOOTSTRAP_ID,
    CA_DIGEST,
    CALICO_VERSION,
    CTR_VERSION_ANSWER,
    FINGERPRINT,
    KERNEL_MODULES,
    MANIFEST,
    MANIFEST_SHA256,
    OTHER_ADDRESS,
    OTHER_FINGERPRINT,
    POD_CIDR,
    PORT,
    SERVER_VERSION,
    SYSCTL_FILE,
    VALID,
    Scratch,
    code_of,
    one_error_line,
)
from gcpkubeadmsupport import (
    ALL_METADATA_LIMIT,
    API_PATH,
    BEARER_VALUE,
    COMMON_VALUES,
    CONTROL_PLANE_VALUES,
    INTERNAL_ADDRESS,
    LOCATION,
    METADATA,
    MINOR,
    PROJECT_ID,
    PUBLIC_ADDRESS,
    RAW_GUARD,
    ROLES,
    SECONDS,
    STORE_ID,
    TEMPLATES,
    USER_DATA_LIMIT,
    WORKER_VALUES,
    make_scratch,
    posted_bodies,
    render,
    rendered,
    run_script,
    template_text,
    user_data_size,
)
from servicesupport import REPO_ROOT
from terraformsupport import needs_terraform
from test_aws_kubeadm_bootstrap import (
    HOSTILE,
    JOIN_SHAPED,
    NUL_VALUES,
    UNICODE_VALUES,
    a_utf8_locale,
    default_of,
)

PLACEHOLDER_REGEX = re.compile(r"\$\$\{|\$\{(\w+)\}")
OTHER_PUBLIC_ADDRESS = "198.51.100.99"
ONE_RUN_MARKER = {"control-plane": "admin.conf", "worker": "kubelet.conf"}
NOT_FOUND = "curl: (22) The requested URL returned error: 404"
FORBIDDEN = "curl: (22) The requested URL returned error: 403"


def calls_to_the_api(scratch: Scratch) -> list[list[str]]:
    return [c for c in scratch.called("curl") if c[-1].startswith(API_PATH)]


# ── terraform's rendering and the renderer here are one ─────────────────────


def terraform_rendering(tmp_path: Path, role: str) -> str:
    """What ``templatefile`` makes of the role's template, with the inputs of the
    support module, through ``terraform console`` on a scratch directory that
    holds the templates and no configuration. The text goes out base64 encoded,
    because the console prints a string in its own quoting."""
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

    used = {m.group(1) for m in PLACEHOLDER_REGEX.finditer(text) if m.group(1)}

    assert "%{" not in text
    assert used == passed  # none missing, none left over


# ── what the rendered text must look like ───────────────────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_a_rendered_script_fits_in_the_256_kb_a_metadata_value_may_hold(
    role: str,
) -> None:
    # Compute Engine's page "Set custom metadata" (read 2026-10-07, updated
    # 2026-10-05): "Each metadata value has a maximum limit of 256 KB", and
    # "a combined total limit of 512 KB for all metadata entries". The script is
    # the one value `user-data`, sent plain.
    size = user_data_size(role)

    assert size <= USER_DATA_LIMIT, f"{role}: {size} bytes of {USER_DATA_LIMIT}"
    assert size + 4096 <= ALL_METADATA_LIMIT  # the other keys are far below 4 KB


@pytest.mark.parametrize("role", ROLES)
def test_a_rendered_script_stays_under_64_kib_against_runaway_growth(
    role: str,
) -> None:
    assert user_data_size(role) < RAW_GUARD


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


# The shapes of what must not be in a script's code. The word "secret" is not
# here: Secret Manager is what the scripts talk to, and its name is in the code.
SECRET_SHAPES = {
    "a bootstrap token": r"\b[a-z0-9]{6}\.[a-z0-9]{16}\b",
    "a CA hash": r"sha256:[0-9a-f]{64}",
    "a Google access token": r"\bya29\.[A-Za-z0-9_-]{20,}",
    "a Google API key": r"\bAIza[0-9A-Za-z_-]{30,}",
    "a service account key": r'"private_key(_id)?"',
    "a private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "a certificate key": r"--certificate-key",
    "a password in the code": r"(?i)password|passwd|api[_-]?key",
    "an authorisation header with a value": r"Bearer [A-Za-z0-9._~+/-]{10,}",
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


@pytest.mark.parametrize("role", ROLES)
def test_neither_script_uses_gcloud_snap_or_a_json_tool(role: str) -> None:
    code = code_of(rendered(role))

    assert re.search(r"\b(gcloud|gsutil|snap|jq|python3?)\b", code) is None


@pytest.mark.parametrize(
    ("role", "defaults"),
    [
        (
            "control-plane",
            {"ADDRESS_ATTEMPTS": "12", "PUBLISH_ATTEMPTS": "10", "POLL_SECONDS": "5"},
        ),
        ("worker", {"JOIN_ATTEMPTS": "240", "POLL_SECONDS": "10"}),
    ],
)
def test_the_rendered_user_data_sets_no_test_variable_and_its_defaults_are_real(
    role: str, defaults: dict[str, str]
) -> None:
    code = code_of(rendered(role))

    found = dict(re.findall(r'^(\w+)="\$\{\1:-([^}]*)\}"$', code, re.MULTILINE))

    assert found == {
        "BOOT_ROOT": "",
        "CONTAINERD_ATTEMPTS": "20",
        "APT_ATTEMPTS": "10",
        **defaults,
    }
    for name in found:
        assert len(re.findall(rf"^(export )?{name}=", code, re.MULTILINE)) == 1


def test_a_workers_poll_outlasts_what_only_the_control_plane_spends_first() -> None:
    """As the AWS module's test of the same name: the control plane is created
    after the workers, and what only it spends before the join command exists is
    summed from the defaults and the flags of the rendered scripts, and the
    worker's window (tries times seconds between tries) must cover it. Summed: the
    wait for the addresses (tries times pause), the Calico manifest's download
    (curl's tries, each of at most --max-time seconds, and the pauses between
    them), the publish (tries times pause, plus one call of at most --max-time
    seconds each) and one wait for the package lock. NOT summed, for lack of a
    bound in the script: `kubeadm init` and `kubeadm token create`, and the time
    Terraform takes to create the control plane after the workers."""
    control_plane = code_of(rendered("control-plane"))
    worker = code_of(rendered("worker"))
    pause = default_of(control_plane, "POLL_SECONDS")
    download = re.search(
        r"--retry (\d+) --retry-delay (\d+) --connect-timeout \d+ --max-time (\d+)",
        control_plane,
    )
    assert download is not None
    retries, retry_delay, max_time = (int(n) for n in download.groups())
    (call_time,) = {
        int(n) for n in re.findall(r"--connect-timeout 10 --max-time (\d+)", worker)
    } - {max_time}
    (lock_wait,) = {
        int(n) for n in re.findall(r"DPkg::Lock::Timeout=(\d+)", control_plane)
    }
    parts = {
        "the wait for the addresses": default_of(control_plane, "ADDRESS_ATTEMPTS")
        * pause,
        "the Calico manifest's download": (retries + 1) * max_time
        + retries * retry_delay,
        "the publish of the join command": default_of(control_plane, "PUBLISH_ATTEMPTS")
        * (pause + call_time + 5),
        "one wait for the package lock": lock_wait,
    }
    window = default_of(worker, "JOIN_ATTEMPTS") * default_of(worker, "POLL_SECONDS")

    assert all(seconds > 0 for seconds in parts.values()), parts
    assert window >= sum(parts.values()), (window, parts)


@pytest.mark.parametrize("role", ROLES)
def test_a_script_stops_at_the_first_failure_and_keeps_its_files_in_a_private_directory(
    role: str,
) -> None:
    script = rendered(role)
    code = code_of(script)

    assert script.splitlines()[0] == "#!/bin/bash"
    assert re.search(r"^set -euo pipefail$", code, re.MULTILINE)
    assert "mktemp -d" in code


@pytest.mark.parametrize("role", ROLES)
def test_a_script_pins_the_alphabet_of_its_patterns_beside_its_error_handling(
    role: str,
) -> None:
    lines = code_of(rendered(role)).splitlines()

    assert lines.index("export LC_ALL=C") == lines.index("set -euo pipefail") + 1
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


@pytest.mark.parametrize("role", ROLES)
def test_a_run_that_goes_well_leaves_nothing_in_its_temporary_directory(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, role)

    assert done.returncode == 0, done.stderr
    assert list((tmp_path / "tmp").iterdir()) == []


# ── the metadata server and the addresses ───────────────────────────────────


def test_the_control_plane_runs_the_packages_before_it_asks_for_its_addresses(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    calls = scratch.calls()
    first_ask = next(n for n, c in enumerate(calls) if c[-1].startswith(METADATA))
    assert ["apt-mark", "hold", "kubelet", "kubeadm", "kubectl"] in calls[:first_ask]
    assert first_ask < next(
        n for n, c in enumerate(calls) if c[:2] == ["kubeadm", "init"]
    )


def test_every_call_to_the_metadata_server_carries_its_header_and_is_a_get(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "control-plane")  # the stand-in refuses the rest

    assert done.returncode == 0, done.stderr
    asked = [c for c in scratch.called("curl") if c[-1].startswith(METADATA)]
    keys = [c[-1].removeprefix(f"{METADATA}/") for c in asked]
    assert keys == [
        "instance/network-interfaces/0/ip",
        "instance/network-interfaces/0/access-configs/0/external-ip",
        "project/project-id",
        "instance/service-accounts/default/token",  # the publish asks for it
    ]
    for call in asked:
        assert ("-H", "Metadata-Flavor: Google") in pairwise(call), call
        assert "-X" not in call and "--request" not in call


def test_the_control_plane_gives_up_at_the_bound_when_the_node_reports_other_addresses(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("internal", [OTHER_ADDRESS])

    done = run_script(scratch, "control-plane")

    line = one_error_line(done, "control-plane")
    assert "gave up after 3 tries" in line
    assert INTERNAL_ADDRESS in line
    assert PUBLIC_ADDRESS in line
    assert scratch.count_file("internal") == 3  # one reading per try, no more
    assert scratch.called("kubeadm") == []  # not even a kubeadm init


@pytest.mark.parametrize("which", ["internal", "external"])
def test_a_node_that_reports_an_address_late_is_waited_for_not_failed(
    tmp_path: Path, which: str
) -> None:
    scratch = make_scratch(tmp_path)
    right = {"internal": INTERNAL_ADDRESS, "external": PUBLIC_ADDRESS}[which]
    wrong = {"internal": OTHER_ADDRESS, "external": OTHER_PUBLIC_ADDRESS}[which]
    scratch.series(which, ["", wrong, right])  # the empty answer is a 404

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    assert scratch.count_file(which) == 3
    assert len(scratch.called("kubeadm", "init")) == 1


def test_a_public_address_that_is_not_the_reserved_one_is_never_waited_out(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("external", [OTHER_PUBLIC_ADDRESS])

    done = run_script(scratch, "control-plane")

    line = one_error_line(done, "control-plane")
    assert "gave up after 3 tries" in line
    assert scratch.called("kubeadm") == []


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    "project",
    [
        "a/../b",
        "example-project; touch x",
        "Example-Project",
        "abcd",
        "-example",
        "example-",
        "a" * 31,
        "example project",
        "$(touch x)",
        "dömain-project",
    ],
)
def test_a_project_id_the_script_does_not_accept_stops_it_before_any_call_to_google(
    tmp_path: Path, role: str, project: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("project", [project])

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert "project ID" in line
    assert project not in line  # the value is not echoed
    assert calls_to_the_api(scratch) == []
    assert scratch.called("kubeadm", "init") == []
    assert scratch.called("kubeadm", "join") == []
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("role", ROLES)
def test_a_project_id_the_metadata_server_does_not_give_stops_the_script_with_one_line(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("project", [""])

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert "could not read the project ID" in line
    assert calls_to_the_api(scratch) == []


# ── what the control plane does ─────────────────────────────────────────────


def test_the_control_plane_runs_kubeadm_init_once_with_the_internal_endpoint(
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
        f"{INTERNAL_ADDRESS}:{PORT}",
        "--apiserver-cert-extra-sans",
        PUBLIC_ADDRESS,
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
        (call[0], call[1])
        for call in scratch.calls()
        if call[0] in {"kubeadm", "kubectl"} or call[-1].startswith(API_PATH)
    ]
    assert [s[0] for s in steps] == ["kubeadm", "kubectl", "kubeadm", "curl"]
    assert steps[0] == ("kubeadm", "init")
    assert steps[2] == ("kubeadm", "token")


def test_the_control_plane_applies_the_local_file_it_checked_by_its_digest(
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
    assert apply[1:5] == [
        "--kubeconfig",
        str(tmp_path / "root" / "etc" / "kubernetes" / "admin.conf"),
        "apply",
        "--server-side",
    ]
    assert apply[5] == "-f"
    assert len(apply) == 7
    assert "://" not in apply[6]
    assert Path(apply[6]).name == "calico.yaml"
    assert (tmp_path / "applied").read_text(encoding="utf-8") == MANIFEST


@pytest.mark.parametrize("role", ROLES)
def test_every_download_is_https_only_tls12_failing_and_bounded_in_time(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, role)

    assert done.returncode == 0, done.stderr
    fetches = [c for c in scratch.called("curl") if c[-1].startswith("https://")]
    downloads = [c for c in fetches if not c[-1].startswith(API_PATH)]
    expected = ["Release.key"] + (["calico.yaml"] if role == "control-plane" else [])
    assert sorted(c[-1].rsplit("/", 1)[1] for c in downloads) == sorted(expected)
    for call in fetches:
        pairs = list(pairwise(call))
        assert ("--proto", "=https") in pairs, call
        assert ("--proto-redir", "=https") in pairs, call
        assert "--tlsv1.2" in call
        assert "--location" not in call or call in downloads  # an API call: no redirect
    for call in scratch.called("curl"):
        assert "--fail" in call, call
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
    assert calls_to_the_api(scratch) == []  # and no join command is published
    assert scratch.called("kubeadm", "token") == []


# ── publishing the join command ─────────────────────────────────────────────


def test_the_control_plane_adds_one_version_with_a_join_commands_shape(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    (post,) = calls_to_the_api(scratch)
    assert post[-1] == f"{API_PATH}:addVersion"
    assert post[post.index("--request") + 1] == "POST"
    assert ("--header", "Content-Type: application/json") in pairwise(post)
    (body,) = posted_bodies(scratch)
    assert re.fullmatch(r'\{"payload":\{"data":"[A-Za-z0-9+/=]+"\}\}', body)
    encoded = re.search(r'"data":"([^"]+)"', body)[1]
    assert base64.b64decode(encoded).decode() == VALID  # no newline after it


def test_the_url_is_the_regional_endpoint_the_project_from_the_server_and_the_secret(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    run_script(scratch, "control-plane")

    (post,) = calls_to_the_api(scratch)
    assert post[-1] == (
        f"https://secretmanager.{LOCATION}.rep.googleapis.com/v1/projects/"
        f"{PROJECT_ID}/locations/{LOCATION}/secrets/{STORE_ID}:addVersion"
    )


def test_the_access_token_reaches_curl_through_a_file_and_no_argument_or_log(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    arguments = "\n".join(" ".join(call) for call in scratch.calls())
    assert BEARER_VALUE not in arguments + done.stdout + done.stderr
    (post,) = calls_to_the_api(scratch)
    (header,) = [b for a, b in pairwise(post) if a == "-H" and b.startswith("@")]
    assert Path(header[1:]).name == "api-headers"
    assert (tmp_path / "auth.0").read_text(encoding="utf-8") == (
        f"Authorization: Bearer {BEARER_VALUE}\n"
    )
    assert (tmp_path / "auth.0.mode").read_text(encoding="utf-8").strip() == "600"
    assert list((tmp_path / "tmp").iterdir()) == []  # and it is removed


def test_the_join_command_reaches_curl_through_a_file_and_no_argument_or_log(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    arguments = "\n".join(" ".join(call) for call in scratch.calls())
    encoded = base64.b64encode(VALID.encode()).decode()
    for part in (BOOTSTRAP_ID, CA_DIGEST, encoded):
        assert part not in arguments
        assert part not in done.stdout + done.stderr
    (post,) = calls_to_the_api(scratch)
    assert post[post.index("--data-binary") + 1].startswith("@")


def test_what_kubeadm_prints_is_checked_before_it_is_published(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    other = VALID.replace(INTERNAL_ADDRESS, OTHER_ADDRESS)
    (tmp_path / "join-line").write_text(other + "\n", encoding="utf-8")

    done = run_script(scratch, "control-plane")

    line = one_error_line(done, "control-plane")
    assert "nothing was published" in line
    assert calls_to_the_api(scratch) == []


def test_a_secret_that_cannot_be_written_is_tried_to_the_bound_then_one_line(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "post-failures").write_text("99", encoding="utf-8")

    done = run_script(scratch, "control-plane")

    line = one_error_line(done, "control-plane")
    assert "gave up after 3 tries" in line
    assert STORE_ID in line
    assert "returned error: 403" in line  # why, in curl's own words
    assert len(posted_bodies(scratch)) == 3
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_write_that_fails_once_is_logged_with_its_reason_and_made_on_the_second_try(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "post-failures").write_text("1", encoding="utf-8")

    done = run_script(scratch, "control-plane")

    assert done.returncode == 0, done.stderr
    assert len(posted_bodies(scratch)) == 2
    assert "could not write the secret (try 1 of 3): " + FORBIDDEN in done.stdout
    assert done.stderr == ""


def test_a_refused_write_whose_error_names_a_token_is_withheld_not_shown(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "post-failures").write_text("99", encoding="utf-8")
    leaked = f"curl: (22) denied for Bearer {BEARER_VALUE}\n"
    (tmp_path / "post-error").write_text(leaked, encoding="utf-8")

    done = run_script(scratch, "control-plane")

    line = one_error_line(done, "control-plane")
    assert "withheld" in line
    assert BEARER_VALUE not in done.stdout + done.stderr


@pytest.mark.parametrize(
    "answer",
    [
        "{}",
        '{"access_token":"short","expires_in":3599}',
        '{"access_token":"has a space in it 01234567890123456789"}',
        '{"access_token":"quote\\"; touch x; echo \\"0123456789012345"}',
        '{"access_token":""}',
        "not json",
        "",
    ],
)
def test_an_answer_with_no_usable_access_token_is_a_failed_try_and_calls_nothing_else(
    tmp_path: Path, answer: str
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "token-response").write_text(answer, encoding="utf-8")

    done = run_script(scratch, "control-plane")

    line = one_error_line(done, "control-plane")
    assert "gave up after 3 tries" in line
    assert "no access token" in line
    assert calls_to_the_api(scratch) == []
    assert not (tmp_path / "x").exists()


# ── a second run, the key, the packages and containerd ──────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_a_second_run_on_the_same_node_calls_nothing_and_says_why_it_stopped(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    first = run_script(scratch, role)
    calls_of_the_first_run = scratch.calls()

    second = run_script(scratch, role)

    assert first.returncode == 0, first.stderr
    line = one_error_line(second, role)
    assert "runs once per node" in line
    assert ONE_RUN_MARKER[role] in line
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
    assert scratch.calls() == []  # not even a read of the metadata server
    assert list((tmp_path / "root").rglob("*")) == [
        tmp_path / "root" / "etc",
        kubernetes,
        kubernetes / ONE_RUN_MARKER[role],
    ]


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
    for install in scratch.called("apt-get", *APT_LOCK, "install"):
        assert "kubeadm" not in install
    assert scratch.called("kubeadm") == []


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    "keys",
    [
        [(FINGERPRINT, ""), (OTHER_FINGERPRINT, "")],
        [(FINGERPRINT, ""), (FINGERPRINT, "")],
        [],
    ],
    ids=["the pinned key first", "the pinned twice", "none"],
)
def test_a_key_file_that_does_not_hold_exactly_one_primary_key_is_refused_unwritten(
    tmp_path: Path, role: str, keys: list[tuple[str, str]]
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.listing(*keys)

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert "exactly one" in line
    assert not (tmp_path / "root" / "etc" / "apt").exists()
    assert scratch.called("kubeadm") == []


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
    assert ["apt-mark", "hold", "kubelet", "kubeadm", "kubectl"] in scratch.calls()
    config = (root / "etc/containerd/config.toml").read_text()
    assert "SystemdCgroup = true" in config
    assert (root / "etc/sysctl.d/k8s.conf").read_text() == SYSCTL_FILE
    assert ["systemctl", "restart", "containerd"] in scratch.calls()
    assert scratch.called("snap") == []  # no snap and no cloud command line here


@pytest.mark.parametrize("role", ROLES)
def test_every_apt_get_call_waits_for_the_lock_and_a_failing_one_is_tried_again(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "apt-get-failures").write_text("2", encoding="utf-8")

    done = run_script(scratch, role)  # three tries is the bound of this run

    assert done.returncode == 0, done.stderr
    calls = scratch.called("apt-get")
    assert [call[3] for call in calls] == ["update"] * 3 + [
        "install",
        "update",
        "install",
    ]
    for call in calls:
        assert call[1:3] == list(APT_LOCK)


@pytest.mark.parametrize("role", ROLES)
def test_an_apt_get_that_never_succeeds_ends_the_script_after_the_bound_with_one_line(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "apt-get-failures").write_text("99", encoding="utf-8")

    done = run_script(scratch, role)

    assert done.returncode == 1
    errors = [line for line in done.stderr.splitlines() if "ERROR" in line]
    assert len(errors) == 1
    assert done.stderr.splitlines()[-1] == errors[0]
    assert "apt-get update" in errors[0]
    assert len(scratch.called("apt-get")) == 3
    assert scratch.called("curl") == []  # nothing after it ran: no address, no key


@pytest.mark.parametrize("role", ROLES)
def test_the_node_loads_overlay_and_br_netfilter_before_the_sysctl_settings(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, role)

    assert done.returncode == 0, done.stderr
    assert scratch.called("modprobe") == [["modprobe", m] for m in KERNEL_MODULES]
    order = [call[0] for call in scratch.calls() if call[0] in {"modprobe", "sysctl"}]
    assert order == ["modprobe", "modprobe", "sysctl"]
    boot_file = tmp_path / "root" / "etc" / "modules-load.d" / "k8s.conf"
    assert boot_file.read_text(encoding="utf-8") == "overlay\nbr_netfilter\n"


@pytest.mark.parametrize("role", ROLES)
def test_a_containerd_that_never_answers_ends_the_script_after_the_bound(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "ctr.0.fail").touch()

    done = run_script(scratch, role)

    line = one_error_line(done, role)
    assert "gave up after 3 tries" in line
    assert "containerd" in line
    assert len(scratch.called("ctr")) == 3
    assert scratch.called("kubeadm") == []


@pytest.mark.parametrize("role", ROLES)
def test_the_servers_version_is_logged_once_when_containerd_answers(
    tmp_path: Path, role: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("ctr", ["", "", CTR_VERSION_ANSWER])
    (tmp_path / "ctr.0.fail").touch()
    (tmp_path / "ctr.1.fail").touch()

    done = run_script(scratch, role)

    assert done.returncode == 0, done.stderr
    assert done.stdout.count(SERVER_VERSION) == 1
    assert "v1.1.1-client" not in done.stdout


@pytest.mark.parametrize(
    ("role", "setting"),
    [
        ("control-plane", "ADDRESS_ATTEMPTS"),
        ("control-plane", "PUBLISH_ATTEMPTS"),
        ("control-plane", "POLL_SECONDS"),
        ("control-plane", "APT_ATTEMPTS"),
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


def failing_reads(tmp_path: Path, *stderr: bytes | None) -> None:
    """The reads of a worker, one per entry, each exiting 22 as curl does for an
    HTTP error and printing the entry on its error stream (nothing for None)."""
    for number, text in enumerate(stderr):
        (tmp_path / f"secret.{number}.status").write_text("22", encoding="utf-8")
        if text is not None:
            (tmp_path / f"secret.{number}.err").write_bytes(text)


def gave_up(tmp_path: Path, *stderr: bytes | None) -> tuple[str, str]:
    """Runs a worker whose reads all fail as given: its one error line, and what
    the whole run printed."""
    scratch = make_scratch(tmp_path)
    scratch.series("secret", [""] * len(stderr))
    failing_reads(tmp_path, *stderr)

    done = run_script(scratch, "worker")

    return one_error_line(done, "worker"), done.stdout + done.stderr


def test_a_worker_polls_a_secret_with_no_version_to_the_bound_and_says_why(
    tmp_path: Path,
) -> None:
    line, output = gave_up(tmp_path, *[(NOT_FOUND + "\n").encode()] * 3)

    assert "gave up after 3 tries" in line
    assert STORE_ID in line
    assert "returned error: 404" in line
    assert output.count("the secret does not hold a join command yet") == 3
    assert output.count("the request to Secret Manager reported") == 1  # logged once


def test_a_worker_reads_the_newest_version_with_a_get_and_the_token_file(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)

    done = run_script(scratch, "worker")

    assert done.returncode == 0, done.stderr
    (read,) = calls_to_the_api(scratch)
    assert read[-1] == f"{API_PATH}/versions/latest:access"
    assert "--request" not in read  # a GET
    assert "--data-binary" not in read
    assert read[-2].startswith("@")
    assert BEARER_VALUE not in "\n".join(" ".join(c) for c in scratch.calls())
    assert BEARER_VALUE not in done.stdout + done.stderr
    assert list((tmp_path / "tmp").iterdir()) == []


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
        f"{INTERNAL_ADDRESS}:{PORT}",
        "--token",
        BOOTSTRAP_ID,
        "--discovery-token-ca-cert-hash",
        f"sha256:{CA_DIGEST}",
    ]
    assert BOOTSTRAP_ID not in done.stdout + done.stderr  # the value is not logged


def test_a_worker_keeps_polling_until_the_control_plane_has_added_a_version(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("secret", ["", "\n", VALID + "\n"])
    (tmp_path / "secret.0.status").write_text("22", encoding="utf-8")
    (tmp_path / "secret.0.err").write_text(NOT_FOUND + "\n", encoding="utf-8")

    done = run_script(scratch, "worker")

    assert done.returncode == 0, done.stderr
    assert len(calls_to_the_api(scratch)) == 3
    assert len(scratch.called("kubeadm", "join")) == 1


@pytest.mark.parametrize("trailing", ["", " ", "\n", " \n"])
def test_one_trailing_space_or_newline_is_allowed_after_a_join_command(
    tmp_path: Path, trailing: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("secret", [VALID + trailing])

    done = run_script(scratch, "worker")

    assert done.returncode == 0, done.stderr
    assert len(scratch.called("kubeadm", "join")) == 1


@pytest.mark.parametrize("name", sorted(HOSTILE), ids=sorted(HOSTILE))
def test_a_value_that_is_not_exactly_a_join_command_joins_nothing_and_runs_nothing(
    tmp_path: Path, name: str
) -> None:
    scratch = make_scratch(tmp_path)
    canary = tmp_path / "canary"
    scratch.series("secret", [HOSTILE[name](str(canary))])

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")
    assert "gave up after 3 tries" in line
    assert not canary.exists()  # none of the text was run
    assert scratch.called("kubeadm") == []
    assert len(calls_to_the_api(scratch)) == 3
    last_install = max(
        number
        for number, call in enumerate(scratch.calls())
        if call[0] in {"apt-get", "apt-mark", "systemctl"}
    )
    assert {c[0] for c in scratch.calls()[last_install + 1 :]} <= {"curl"}
    for call in scratch.calls():
        assert "touch" not in call and str(canary) not in " ".join(call)


@pytest.mark.parametrize("name", sorted(UNICODE_VALUES), ids=sorted(UNICODE_VALUES))
def test_the_pattern_refuses_non_ascii_digits_and_letters_in_a_utf8_locale(
    tmp_path: Path, name: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("secret", [UNICODE_VALUES[name].encode("utf-8")])

    done = run_script(scratch, "worker", LC_ALL=a_utf8_locale())

    line = one_error_line(done, "worker")
    assert "gave up after 3 tries" in line  # the pattern refused it, not kubeadm
    assert scratch.called("kubeadm") == []


@pytest.mark.parametrize("name", sorted(NUL_VALUES), ids=sorted(NUL_VALUES))
def test_a_nul_byte_in_the_value_is_a_refusal_with_one_line_and_no_bash_warning(
    tmp_path: Path, name: str
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("secret", [NUL_VALUES[name].encode("utf-8")])

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")  # nothing else on stderr: no warning
    assert "gave up after 3 tries" in line
    assert "null byte" not in done.stderr
    assert scratch.called("kubeadm") == []


def test_an_answer_with_no_data_field_is_a_failed_read_that_says_so(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "raw-response").write_text('{"name": "x"}', encoding="utf-8")

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")
    assert "gave up after 3 tries" in line
    assert "holds no data field" in line
    assert scratch.called("kubeadm") == []


@pytest.mark.parametrize(
    "data",
    ["!!!not base64!!!", "a", "====", "@@@@"],
)
def test_an_answer_whose_data_is_not_base64_is_a_failed_read(
    tmp_path: Path, data: str
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "raw-response").write_text(
        f'{{"payload": {{"data": "{data}"}}}}', encoding="utf-8"
    )

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")
    assert "gave up after 3 tries" in line
    assert scratch.called("kubeadm") == []


def test_a_data_field_of_json_that_hides_a_command_is_matched_never_run(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    canary = tmp_path / "canary"
    (tmp_path / "raw-response").write_text(
        '{"payload": {"data": "$(touch ' + str(canary) + ')"}}', encoding="utf-8"
    )

    done = run_script(scratch, "worker")

    one_error_line(done, "worker")
    assert not canary.exists()


def test_an_access_token_that_the_metadata_server_refuses_is_a_failed_try_with_a_reason(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "token.0.status").write_text("22", encoding="utf-8")
    (tmp_path / "token.0.err").write_text(FORBIDDEN + "\n", encoding="utf-8")

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")
    assert "gave up after 3 tries" in line
    assert "returned error: 403" in line
    assert calls_to_the_api(scratch) == []


# What the Secret Manager call may print: curl's one line. The longer texts here
# are what the masking has to survive, whatever prints them.
LONG = "E" + "x" * 5000


def test_only_the_first_line_of_the_last_error_reaches_the_line(
    tmp_path: Path,
) -> None:
    error = f"{FORBIDDEN}\nthe second line is not shown\nnor the third\n"

    line, _ = gave_up(tmp_path, error.encode())

    assert FORBIDDEN in line
    assert "second line" not in line
    assert "third" not in line


def test_the_last_error_is_the_one_the_line_carries_not_the_first(
    tmp_path: Path,
) -> None:
    line, _ = gave_up(
        tmp_path, (NOT_FOUND + "\n").encode(), b"", (FORBIDDEN + "\n").encode()
    )

    assert "returned error: 403" in line
    assert "returned error: 404" not in line


def test_an_error_that_a_later_read_without_one_replaced_is_not_carried(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("secret", ["", "\n"])  # then it only polls, with no error
    failing_reads(tmp_path, (FORBIDDEN + "\n").encode())

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")
    assert "gave up after 3 tries" in line
    assert "403" not in line


def test_a_failed_read_that_printed_nothing_is_said_to_have_printed_nothing(
    tmp_path: Path,
) -> None:
    line, _ = gave_up(tmp_path, None)

    assert "gave up after 3 tries" in line
    assert "printed no message" in line


@pytest.mark.parametrize("error", [LONG.encode() + b"\n", LONG.encode()])
def test_the_error_is_cut_to_400_characters_with_or_without_a_newline(
    tmp_path: Path, error: bytes
) -> None:
    line, _ = gave_up(tmp_path, error)

    assert "E" + "x" * 399 in line
    assert "x" * 400 not in line


def test_every_character_outside_printable_ascii_is_replaced_in_the_line(
    tmp_path: Path,
) -> None:
    raw = "An error: café‮ \x01\ttab\x1b[31m\rend".encode()

    line, output = gave_up(tmp_path, raw + b"\n")

    expected = re.sub(rb"[^ -~]", b"?", raw).decode("ascii")
    assert expected in line
    assert line.isascii() and line.isprintable()
    assert "\x1b" not in output


def test_a_nul_byte_in_the_error_is_dropped_with_no_bash_warning(
    tmp_path: Path,
) -> None:
    line, _ = gave_up(tmp_path, b"An error\x00 occurred\n")

    assert "An error occurred" in line
    assert "null byte" not in line


# The Google access token's shape, made at run time so that no file of the
# repository holds one in a form a scanner takes for a credential.
GOOGLE_TOKEN_SHAPE = "ya" + "29." + "0123456789abcdefghijklmnop"
TOKEN_SHAPED = {
    **{name: text for name, text in JOIN_SHAPED.items()},
    "a Google access token": f"An error occurred: bad value {GOOGLE_TOKEN_SHAPE}",
    "an authorisation header": "An error occurred: sent Bearer abc",
}


@pytest.mark.parametrize("name", sorted(TOKEN_SHAPED), ids=sorted(TOKEN_SHAPED))
def test_an_error_line_that_holds_the_shape_of_a_join_command_or_a_token_is_withheld(
    tmp_path: Path, name: str
) -> None:
    error = (TOKEN_SHAPED[name] + "\n").encode()

    line, output = gave_up(tmp_path, error)

    assert "gave up after 3 tries" in line
    assert "withheld" in line
    for part in (BOOTSTRAP_ID, CA_DIGEST, "kubeadm join", GOOGLE_TOKEN_SHAPE):
        assert part not in output


def test_a_token_with_a_non_ascii_character_inside_it_is_withheld_not_shown_by_its_tail(
    tmp_path: Path,
) -> None:
    error = (
        f"An error occurred: bad value {BOOTSTRAP_ID[:7]}{chr(0xE9)}{BOOTSTRAP_ID[7:]}"
    )

    line, output = gave_up(tmp_path, (error + "\n").encode())

    assert "withheld" in line
    assert BOOTSTRAP_ID[7:] not in output


def test_the_masking_does_not_hide_an_ordinary_service_error(tmp_path: Path) -> None:
    line, _ = gave_up(tmp_path, (FORBIDDEN + "\n").encode())

    assert "withheld" not in line


def test_the_error_is_logged_the_first_time_and_not_again_while_it_stays_the_same(
    tmp_path: Path,
) -> None:
    error = (FORBIDDEN + "\n").encode()

    _, output = gave_up(tmp_path, error, error, error)

    assert output.count(FORBIDDEN) == 2  # the log line once, the last line once
    first = output.index(FORBIDDEN)
    assert "reported" in output[first - 40 : first]


def test_a_changed_error_is_logged_again(tmp_path: Path) -> None:
    refused = (FORBIDDEN + "\n").encode()
    missing = (NOT_FOUND + "\n").encode()

    _, output = gave_up(tmp_path, refused, missing, missing)

    assert output.count(FORBIDDEN) == 1
    assert output.count(NOT_FOUND) == 2


def test_the_logged_error_line_is_the_masked_one(tmp_path: Path) -> None:
    error = f"An error occurred: bad value {VALID}\n".encode()

    _, output = gave_up(tmp_path, error, error)

    assert "reported: a line that looked like a join command or a token" in output
    for part in (BOOTSTRAP_ID, CA_DIGEST, "kubeadm join"):
        assert part not in output


def test_a_join_that_fails_says_what_to_do_and_is_not_tried_again(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    (tmp_path / "join-fails").touch()

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")
    assert line == (
        "worker: ERROR: kubeadm join failed. The join command in the secret may "
        "be older than the control plane (the control plane was replaced and the "
        "secret kept): remove this environment and apply it again."
    )
    assert len(scratch.called("kubeadm", "join")) == 1  # one shot, no retry
    assert len(calls_to_the_api(scratch)) == 1


def test_the_placeholder_of_the_aws_module_is_not_a_join_command_here_either(
    tmp_path: Path,
) -> None:
    scratch = make_scratch(tmp_path)
    scratch.series("secret", ["not-yet-written\n"])

    done = run_script(scratch, "worker")

    line = one_error_line(done, "worker")
    assert "gave up after 3 tries" in line
    assert scratch.called("kubeadm") == []


def test_this_file_runs_where_the_repository_root_is_known() -> None:
    assert (REPO_ROOT / "infra" / "terraform" / "gcp-kubeadm").is_dir()
    assert re.fullmatch(r"\d+\.\d+", MINOR)
    assert (
        render("${a}$${b}", {"a": "1"}) == "1${b}"
    )  # the renderer the tests share is the one that was imported
