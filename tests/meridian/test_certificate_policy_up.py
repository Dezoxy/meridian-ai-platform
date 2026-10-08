"""What ``infra/kind/up.sh`` does about the certificate policies (S056, T-88).

``up.sh`` installs approver-policy, applies the three policies once its webhook
answers and waits for them before it creates the CA and the issuer. No cluster is
needed: the tests read the order of the script's lines, run its apply function
in bash against a stub ``kctl`` and check the messages of its two waits. The
policies themselves are checked in ``test_certificate_policy.py``.
"""

import os
import re
import subprocess
from itertools import pairwise

from certpolicysupport import KIND_DIR, POLICY_NAMES

UP_SH = (KIND_DIR / "up.sh").read_text(encoding="utf-8")
# The functions of Prometheus's gateway are in the file up.sh sources for them.
GATEWAYS_SH = (KIND_DIR / "gateways.sh").read_text(encoding="utf-8")


def script_lines() -> list[str]:
    """up.sh with each backslash continuation, and each ``||`` that ends a line,
    folded into one line."""
    joined = re.sub(r"\\\n\s*", "", UP_SH)
    return re.sub(r"\|\|\n\s*", "|| ", joined).splitlines()


def line_index(prefix: str) -> int:
    (found,) = [i for i, line in enumerate(script_lines()) if line.startswith(prefix)]
    return found


def line_containing(text: str) -> int:
    (found,) = [i for i, line in enumerate(script_lines()) if text in line]
    return found


def test_up_installs_approver_policy_from_its_pin_into_the_cert_manager_namespace() -> (
    None
):
    installed = script_lines()[line_index("install_release approver-policy ")]

    assert installed == (
        "install_release approver-policy cert-manager"
        ' "${APPROVER_POLICY_CHART}" "${APPROVER_POLICY_VERSION}"'
        ' "${CERT_MANAGER_REPO}" approver-policy.yaml'
        ' --set "image.tag=${APPROVER_POLICY_IMAGE_TAG}"'
        ' --set "image.digest=${APPROVER_POLICY_IMAGE_DIGEST}"'
    )


def test_up_decides_who_may_ask_before_it_creates_the_ca_and_waits_for_the_issuer() -> (
    None
):
    lines = script_lines()
    cert_manager = line_index("install_release cert-manager ")
    approver = line_index("install_release approver-policy ")
    applied = lines.index("apply_certificate_policy")
    ready = line_containing("certificaterequestpolicy")
    ca_applied = line_containing("manifests/service-ca.yaml")
    issuer = line_containing("clusterissuer/meridian-services")

    assert cert_manager < approver < applied < ready < ca_applied < issuer
    assert lines[ready].startswith("kctl wait --for=condition=Ready ")
    assert "--timeout=" in lines[ready]
    for name in sorted(POLICY_NAMES):
        assert f"certificaterequestpolicy/{name}" in lines[ready]


def wait_and_die_message(prefix: str) -> tuple[str, str]:
    """The --timeout of the ``kctl wait`` line that starts with ``prefix`` and
    the text of the ``die`` that ends it (it must end in one)."""
    line = script_lines()[line_index(prefix)]
    found = re.search(r'--timeout=(\d+m) >/dev/null \|\| die "([^"]+)"$', line)
    assert found, f"no `|| die` ends: {line}"
    return found.group(1), found.group(2)


def test_up_s_wait_for_the_policies_dies_naming_what_to_look_at() -> None:
    timeout, message = wait_and_die_message(
        "kctl wait --for=condition=Ready certificaterequestpolicy/"
    )

    assert timeout == "2m"
    # The time it waited, the condition that was not met and where the cause is.
    assert timeout in message
    assert "Ready" in message
    assert "approver-policy" in message
    assert "kubectl -n cert-manager get pods" in message


def test_up_s_wait_for_the_issuer_dies_naming_what_to_look_at() -> None:
    timeout, message = wait_and_die_message(
        "kctl wait --for=condition=Ready clusterissuer/meridian-services "
    )

    assert timeout == "5m"
    assert timeout in message
    # The CA's request is what the issuer waits for, and its Approved or Denied
    # condition says whether the policies decided it.
    assert "meridian-services-ca" in message
    assert "CertificateRequest" in message
    assert "Approved" in message
    assert "Denied" in message


GATEWAY_WAIT = "kctl -n envoy-gateway-system wait --for=condition=Programmed "
PROXY_WAIT = "kctl -n envoy-gateway-system wait --for=condition=Available "


def test_up_s_wait_for_the_gateway_dies_saying_the_edge_may_serve_and_the_remedy() -> (
    None
):
    timeout, message = wait_and_die_message(GATEWAY_WAIT + "gateway/edge ")

    # The wait itself is as it was: five minutes, for Programmed.
    assert timeout == "5m"
    assert timeout in message
    assert "Programmed" in message
    # Envoy Gateway leaves the condition False while the edge serves.
    assert "serving" in message
    # The remedy, as the three commands of the README's section, in order.
    restart = message.index("rollout restart deploy/envoy-gateway")
    status = message.index("rollout status deploy/envoy-gateway")
    nudge = message.index("annotate gateway edge")
    assert restart < status < nudge
    assert "meridian.local/reconcile-nudge" in message[nudge:]
    # Where the reason is, and the section that has the commands to copy.
    assert "get gateway edge -o yaml" in message
    assert "infra/kind/README.md" in message


def test_up_s_wait_for_the_proxy_dies_saying_what_to_look_at() -> None:
    timeout, message = wait_and_die_message(PROXY_WAIT + "deployment ")

    assert timeout == "5m"
    assert timeout in message
    assert "Available" in message
    assert "kubectl -n envoy-gateway-system get pods" in message
    assert "gateway.envoyproxy.io/owning-gateway-name=edge" in message
    assert "logs deploy/envoy-gateway" in message


def test_the_two_edge_messages_do_not_claim_the_wait_ran_five_minutes() -> None:
    # `kubectl wait` fails at once on "not found" as well as after the timeout,
    # so "in 5m" would be false for the first. The messages say what is known:
    # the wait ended without the condition, after at most five minutes, and
    # kubectl's own message, printed above the error, says which it was.
    for prefix in (GATEWAY_WAIT + "gateway/edge ", PROXY_WAIT + "deployment "):
        timeout, message = wait_and_die_message(prefix)

        assert f"in {timeout}" not in message
        assert f"up to {timeout}" in message
        assert "ended without" in message
        assert "kubectl's own message above" in message


def test_up_s_last_two_waits_keep_their_conditions_and_timeouts() -> None:
    lines = script_lines()
    programmed = lines[line_index(GATEWAY_WAIT)]
    available = lines[line_index(PROXY_WAIT)]

    assert programmed.startswith(GATEWAY_WAIT + "gateway/edge --timeout=5m >/dev/null")
    assert available.startswith(
        PROXY_WAIT
        + "deployment -l gateway.envoyproxy.io/owning-gateway-name=edge --timeout=5m"
    )
    assert line_index(GATEWAY_WAIT) + 1 < line_index(PROXY_WAIT)


def test_the_readme_has_the_section_and_the_commands_the_gateway_message_names() -> (
    None
):
    readme = (KIND_DIR / "README.md").read_text(encoding="utf-8")
    _, message = wait_and_die_message(GATEWAY_WAIT + "gateway/edge ")

    assert "\n## If `make up` was interrupted\n" in readme
    assert "If make up was interrupted" in message
    section = readme.split("\n## If `make up` was interrupted\n")[1].split("\n## ")[0]
    for command in (
        "rollout restart deploy/envoy-gateway",
        "rollout status deploy/envoy-gateway",
        "annotate gateway edge meridian.local/reconcile-nudge=",
    ):
        assert command in section


def test_up_s_comment_does_not_promise_that_a_request_made_in_between_is_quick() -> (
    None
):
    # Helm's wait for approver-policy and the apply's retries can take minutes.
    (comment,) = re.findall(
        r"^# cert-manager's own approver is off.*?(?=^install_release)",
        UP_SH,
        re.MULTILINE | re.DOTALL,
    )

    assert "seconds" not in comment
    assert "minutes" in comment


# ── the apply of the policies is tried again until the webhook answers ───────
# On the kind cluster (2026-10-05) helm's --wait returned for approver-policy
# and the apply of the policy file was refused three times, once per policy:
# "failed calling webhook "policy.cert-manager.io": ... connection refused".
# The pod was Ready (/readyz on 6060) eight seconds after its container started,
# before its webhook on 10250 (failurePolicy: Fail) answered.


def up_function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", UP_SH, re.MULTILINE | re.DOTALL)
    assert match, f"no function {name}"
    return match.group(0)


def gateways_function(name: str) -> str:
    """A function of gateways.sh, as ``up_function`` finds one of up.sh."""
    match = re.search(
        rf"^{name}\(\) \{{\n.*?^\}}\n", GATEWAYS_SH, re.MULTILINE | re.DOTALL
    )
    assert match, f"no function {name}"
    return match.group(0)


def up_constant(name: str) -> int:
    (value,) = re.findall(rf"^readonly {name}=(\d+)$", UP_SH, re.MULTILINE)
    return int(value)


def run_apply_certificate_policy(
    tmp_path, *, failures: int | None
) -> tuple[subprocess.CompletedProcess[str], list[tuple[int, str]]]:
    """``apply_certificate_policy`` from up.sh in bash with up.sh's two
    constants, a ``kctl`` that fails ``failures`` times (always when ``None``)
    and a ``sleep`` that only moves ``SECONDS`` on, so the run is instant.
    Returns the process and each ``kctl`` call as (``SECONDS``, arguments)."""
    calls = tmp_path / "calls"
    script = "\n".join(
        [
            "set -euo pipefail",
            "SECONDS=0",
            f"POLICY_TIMEOUT={up_constant('POLICY_TIMEOUT')}",
            f"POLICY_INTERVAL={up_constant('POLICY_INTERVAL')}",
            "KIND_DIR=/kind",
            'log() { echo "LOG $*"; }',
            'die() { echo "DIE $*" >&2; exit 1; }',
            "sleep() { SECONDS=$((SECONDS + $1)); }",
            "kctl() {",
            f'  echo "${{SECONDS}} $*" >>"{calls}"',
            f'  n=$(($(wc -l <"{calls}")))',
            '  if [[ -z "${FAILURES}" ]] || ((n <= FAILURES)); then',
            '    echo "Error from server (InternalError): refused #${n}" >&2',
            "    return 1",
            "  fi",
            '  echo "certificaterequestpolicy.policy.cert-manager.io/x applied"',
            "}",
            up_function("apply_certificate_policy"),
            "apply_certificate_policy",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "FAILURES": "" if failures is None else str(failures),
        },
        check=False,
    )
    made = []
    for line in calls.read_text().splitlines() if calls.exists() else []:
        seconds, _, arguments = line.partition(" ")
        made.append((int(seconds), arguments))
    return done, made


def test_up_names_how_long_it_tries_the_policy_apply_and_how_often() -> None:
    assert up_constant("POLICY_TIMEOUT") == 120
    assert up_constant("POLICY_INTERVAL") == 3


def test_up_applies_the_policy_file_only_inside_a_loop_that_ends_at_a_deadline() -> (
    None
):
    body = re.sub(r"\\\n\s*", "", up_function("apply_certificate_policy"))
    lines = script_lines()

    assert "SECONDS" in body
    assert "${POLICY_TIMEOUT}" in body
    assert "${POLICY_INTERVAL}" in body
    assert re.search(r"^\s*(until|while) ", body, re.MULTILINE)
    assert (
        'kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/'
        'certificate-policy.yaml"'
    ) in body
    # The one place the file is named, and the function is called once.
    assert sum("manifests/certificate-policy.yaml" in line for line in lines) == 1
    assert lines.count("apply_certificate_policy") == 1


def test_a_policy_apply_refused_twice_and_then_accepted_succeeds_in_silence(
    tmp_path,
) -> None:
    done, calls = run_apply_certificate_policy(tmp_path, failures=2)

    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    assert done.stderr == ""
    assert len(calls) == 3
    # The stub's sleep adds the interval to bash's SECONDS, which also counts
    # real time: a loaded machine can add a second, never take one away. So the
    # gaps have a floor and no exact value.
    times = [seconds for seconds, _ in calls]
    interval = up_constant("POLICY_INTERVAL")
    assert all(later - earlier >= interval for earlier, later in pairwise(times))
    assert {arguments for _, arguments in calls} == {
        "apply --server-side --force-conflicts "
        "-f /kind/manifests/certificate-policy.yaml"
    }


def test_a_policy_apply_accepted_at_once_is_not_repeated(tmp_path) -> None:
    done, calls = run_apply_certificate_policy(tmp_path, failures=0)

    assert done.returncode == 0, done.stderr
    assert len(calls) == 1


def test_a_policy_apply_never_accepted_ends_at_the_deadline_and_says_what_failed(
    tmp_path,
) -> None:
    timeout = up_constant("POLICY_TIMEOUT")
    interval = up_constant("POLICY_INTERVAL")

    done, calls = run_apply_certificate_policy(tmp_path, failures=None)

    assert done.returncode == 1
    assert done.stdout == ""
    # It tried for the whole time and no longer: it gave up after a try made at
    # or past the deadline, and made no try after one that the check at the
    # deadline had let through. (SECONDS also counts real time, so a loaded
    # machine can add a second to a try: only these two bounds are exact.)
    times = [seconds for seconds, _ in calls]
    assert times == sorted(times)
    assert times[-1] >= timeout
    assert times[-2] < timeout
    assert len(calls) <= timeout // interval + 1
    # kctl's last error, and what did not answer and where to look.
    assert f"refused #{len(calls)}" in done.stderr
    assert "approver-policy" in done.stderr
    assert "webhook" in done.stderr
    assert "kubectl -n cert-manager get pods" in done.stderr
    assert "logs" in done.stderr
