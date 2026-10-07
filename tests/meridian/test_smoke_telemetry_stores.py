"""Smoke's check 12: the telemetry stores' refusals and the certificates they serve
(S072, contract M3b).

``check_telemetry_stores`` in ``infra/kind/smoke.d/12-telemetry-stores.sh`` starts
one probe Pod in ``observability`` (the Claims API's image, the collector's name
label and smoke's own) and prints five lines: a push to Loki's gateway with no
client certificate is 403 and a read is 200; Tempo's receiver ends a connection
without a certificate in the alert "certificate required"; Loki's own port times
out for the Pod and is reached by the same Pod with the gateway's labels; and the
gateway and Tempo's receiver each serve the certificate that is in their Secret.
The harness runs the file in bash against a stub ``kctl`` whose probe answers are
the test's, and a real certificate made by ``openssl`` for the fingerprints.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_smoke_network_policy as network
from kindsupport import KIND_DIR, one_line_function, requires_jq

pytestmark = requires_jq

PART = KIND_DIR / "smoke.d" / "12-telemetry-stores.sh"
GATEWAY = "loki-gateway.observability.svc.cluster.local:8443"
TEMPO = "tempo.observability.svc.cluster.local:4317"
LOKI = "loki.observability.svc.cluster.local:3100"
STUB = r"""
kctl() {
  printf '%s\n' "${*//$'\n'/ }" >>"${ASKED}"
  case "$*" in
    *" get pod -l "*) printf "%s" "${LEFTOVER_PODS:-}" ;;
    *"get deployment claims-api -o json"*) cat "${STATE}/deployment.json" ;;
    *"get deployment claims-api"*) [[ "${CLAIMS}" == yes ]] || return 1 ;;
    *"get deployment loki-gateway"*) [[ "${GATEWAY_DEPLOYED}" == yes ]] || return 1 ;;
    *" get secret loki-gateway-tls "*) printf "%s" "${GATEWAY_SECRET}" ;;
    *" get secret tempo-receiver-tls "*) printf "%s" "${TEMPO_SECRET}" ;;
    *" label pod "*)
      if [[ "$*" == *"app.kubernetes.io/component=gateway"* ]]; then
        touch "${STATE}/relabelled"
      else
        rm -f "${STATE}/relabelled"
      fi ;;
    *" exec "*)
      if [[ "${EXEC_STATUS}" != 0 ]]; then
        echo "${EXEC_ERROR}" >&2
        return "${EXEC_STATUS}"
      fi
      case "$*" in
        *http.client*)
          if [[ "$*" == *" POST /loki"* ]]; then
            printf '%s\n' "${PUSH}"
          else
            printf '%s\n' "${READ}"
          fi ;;
        *tls.recv*) printf '%s\n' "${ALERT}" ;;
        *hashlib*)
          if [[ "$*" == *" loki-gateway"* ]]; then
            printf '%s\n' "${SERVED_GATEWAY}"
          else
            printf '%s\n' "${SERVED_TEMPO}"
          fi ;;
        *socket.create_connection*)
          if [[ -e "${STATE}/relabelled" ]]; then
            printf '%s\n' "${TCP_CONTROL}"
          else
            printf '%s\n' "${TCP_BLOCKED}"
          fi ;;
      esac ;;
    *" create "*)
      cat >"${STATE}/created.json"
      [[ "${CREATE_STATUS}" == 0 ]] || { echo "Error: create" >&2; return 1; } ;;
    *" wait "*)
      [[ "${WAIT_STATUS}" == 0 ]] || { echo "error: timed out" >&2; return 1; } ;;
    *" delete "*) ;;
  esac
}
"""


def certificate_fingerprints(tmp_path: Path) -> tuple[str, str]:
    """A throwaway self-signed certificate: its PEM in base64 (what a Secret's
    ``data`` holds) and the SHA-256 of its DER form (what a pod serves)."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    key, pem = tmp_path / "k.pem", tmp_path / "c.pem"
    command = ["openssl", "req", "-x509", "-newkey", "ec", "-nodes", "-days", "2"]
    command += ["-pkeyopt", "ec_paramgen_curve:prime256v1", "-subj", "/CN=smoke-test"]
    command += ["-keyout", str(key), "-out", str(pem)]
    subprocess.run(command, check=True, capture_output=True)
    text = pem.read_text()
    der = ssl.PEM_cert_to_DER_cert(text)
    return base64.b64encode(text.encode()).decode(), hashlib.sha256(der).hexdigest()


def run_check(
    tmp_path: Path,
    *,
    push: str = "403",
    read: str = "200",
    alert: str = "TLSV13_ALERT_CERTIFICATE_REQUIRED",
    blocked: str = "blocked",
    control: str = "reached",
    served_gateway: str | None = None,
    served_tempo: str | None = None,
    gateway_secret: str | None = None,
    tempo_secret: str | None = None,
    claims: str = "yes",
    deployed: str = "claims-api",
    gateway_deployed: str = "yes",
    create_status: int = 0,
    wait_status: int = 0,
    exec_status: int = 0,
    exec_error: str = "",
    leftover_pods: list[dict[str, object]] | None = None,
) -> tuple[list[str], str, str]:
    """``check_telemetry_stores`` in bash against a stub ``kctl``. Returns the
    output lines, everything ``kctl`` was asked (one call per line) and standard
    error. The Secrets hold one certificate; the pods serve it unless a served
    fingerprint is given."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    secret, fingerprint = certificate_fingerprints(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / "deployment.json").write_text(json.dumps(network.DEPLOYMENT))
    asked = tmp_path / "kctl-calls"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            "sleep() { :; }",
            'telemetry_probe_pod=""',  # shared.sh declares it
            'deployed_services() { printf "%s" "${DEPLOYED}"; }',
            one_line_function(
                (KIND_DIR / "smoke.d" / "shared.sh").read_text(), "clean_lines"
            ),
            STUB,
            f'. "{PART}"',
            "check_telemetry_stores",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "ASKED": str(asked),
            "STATE": str(state),
            "PUSH": push,
            "READ": read,
            "ALERT": alert,
            "TCP_BLOCKED": blocked,
            "TCP_CONTROL": control,
            "SERVED_GATEWAY": served_gateway or fingerprint,
            "SERVED_TEMPO": served_tempo or fingerprint,
            "GATEWAY_SECRET": gateway_secret if gateway_secret is not None else secret,
            "TEMPO_SECRET": tempo_secret if tempo_secret is not None else secret,
            "CLAIMS": claims,
            "DEPLOYED": deployed,
            "GATEWAY_DEPLOYED": gateway_deployed,
            "CREATE_STATUS": str(create_status),
            "WAIT_STATUS": str(wait_status),
            "EXEC_STATUS": str(exec_status),
            "EXEC_ERROR": exec_error,
            "LEFTOVER_PODS": json.dumps({"items": leftover_pods or []}),
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text(), done.stderr


def verdicts(lines: list[str]) -> list[str]:
    return [line.split()[0] for line in lines]


def test_all_five_lines_pass_when_the_stores_refuse_and_serve_what_they_should(
    tmp_path: Path,
) -> None:
    lines, _, errors = run_check(tmp_path)

    assert verdicts(lines) == ["PASS"] * 5
    assert errors == ""
    assert "403 for a push" in lines[0] and "200 for a read" in lines[0]
    assert "certificate required" in lines[1]
    assert "times out" in lines[2] and "gateway's labels reaches it" in lines[2]
    assert "Loki's gateway serves the certificate" in lines[3]
    assert "Tempo's receiver serves the certificate" in lines[4]


def test_a_push_the_gateway_answers_with_anything_but_403_fails(
    tmp_path: Path,
) -> None:
    for answer in ("200", "204", "400", "404"):
        lines, _, _ = run_check(tmp_path / answer, push=answer)
        assert lines[0].startswith("FAIL  telemetry stores: a push to Loki's gateway")
        assert f"answered {answer}, not 403" in lines[0]
        assert verdicts(lines)[1:] == ["PASS"] * 4


def test_a_refused_push_with_a_read_that_is_not_200_fails_and_says_which(
    tmp_path: Path,
) -> None:
    lines, _, _ = run_check(tmp_path, read="502")

    assert lines[0].startswith("FAIL  telemetry stores: the gateway refused the push")
    assert "answered 502, not 200" in lines[0]


def test_a_receiver_that_does_not_end_the_connection_in_the_alert_fails(
    tmp_path: Path,
) -> None:
    for answer, words in (
        ("open", "was open"),
        ("closed", "was closed"),
        ("TLSV13_ALERT_BAD_CERTIFICATE", "TLSV13_ALERT_BAD_CERTIFICATE"),
    ):
        lines, _, _ = run_check(tmp_path / answer, alert=answer)
        assert lines[1].startswith("FAIL  telemetry stores: ")
        assert words in lines[1]


def test_loki_s_own_port_reached_by_the_probe_is_a_fail(tmp_path: Path) -> None:
    lines, _, _ = run_check(tmp_path, blocked="reached")

    assert lines[2].startswith("FAIL  telemetry stores: a pod that is not the gateway")
    assert "Loki's ingress rule is missing or too wide" in lines[2]


def test_a_probe_that_neither_reaches_nor_times_out_is_not_called_blocked(
    tmp_path: Path,
) -> None:
    lines, _, _ = run_check(tmp_path, blocked="error: Connection refused")

    assert lines[2].startswith("FAIL  telemetry stores: the probe to Loki's own port")
    assert "no answer of reached or blocked" in lines[2]


def test_a_control_that_does_not_reach_loki_with_the_gateways_labels_is_a_fail(
    tmp_path: Path,
) -> None:
    lines, asked, _ = run_check(tmp_path, control="blocked")

    assert lines[2].startswith("FAIL  telemetry stores: the control did not reach")
    assert "shows nothing" in lines[2]
    # Four tries, as the contract of the line says.
    probes = [c for c in asked.splitlines() if " exec " in c and "3100" in c]
    assert len(probes) == 1 + 4


def test_the_gateways_labels_go_on_for_the_control_and_come_off_again(
    tmp_path: Path,
) -> None:
    _, asked, _ = run_check(tmp_path)
    labels = [c for c in asked.splitlines() if " label pod " in c]

    assert len(labels) == 2
    assert all(
        c.startswith("-n observability label pod smoke-telemetry-") for c in labels
    )
    assert "app.kubernetes.io/name=loki" in labels[0]
    assert "app.kubernetes.io/instance=loki" in labels[0]
    assert "app.kubernetes.io/component=gateway" in labels[0]
    # Taken off again whatever the probe said: back to the collector's name label,
    # and the two others removed.
    assert "app.kubernetes.io/name=opentelemetry-collector" in labels[1]
    assert "app.kubernetes.io/instance-" in labels[1]
    assert "app.kubernetes.io/component-" in labels[1]


def test_a_control_that_fails_still_takes_the_labels_off_again(tmp_path: Path) -> None:
    _, asked, _ = run_check(tmp_path, control="blocked")
    labels = [c for c in asked.splitlines() if " label pod " in c]

    assert len(labels) == 2
    assert "app.kubernetes.io/instance-" in labels[1]


def test_a_gateway_that_serves_another_certificate_than_its_secret_fails(
    tmp_path: Path,
) -> None:
    lines, _, _ = run_check(tmp_path, served_gateway="a" * 64)

    assert verdicts(lines) == ["PASS", "PASS", "PASS", "FAIL", "PASS"]
    assert (
        "Loki's gateway serves a certificate that is NOT the one in the Secret"
        in (lines[3])
    )
    assert "loki-gateway-tls" in lines[3]
    assert "make up" in lines[3] and "deployment/loki-gateway" in lines[3]


def test_a_receiver_that_serves_another_certificate_than_its_secret_fails(
    tmp_path: Path,
) -> None:
    lines, _, _ = run_check(tmp_path, served_tempo="b" * 64)

    assert verdicts(lines) == ["PASS", "PASS", "PASS", "PASS", "FAIL"]
    assert "Tempo's receiver serves a certificate that is NOT the one in" in lines[4]
    assert "tempo-receiver-tls" in lines[4]
    assert "statefulset/tempo" in lines[4]


def test_a_secret_with_no_certificate_in_it_is_a_fail_not_a_pass(
    tmp_path: Path,
) -> None:
    for secret in ("", "bm90IGEgY2VydGlmaWNhdGU="):  # nothing; "not a certificate"
        lines, _, _ = run_check(tmp_path / (secret or "empty"), gateway_secret=secret)
        assert lines[3].startswith(
            "FAIL  telemetry stores: could not read the certificate"
        )


def test_a_pod_that_serves_nothing_a_fingerprint_can_be_made_of_is_a_fail(
    tmp_path: Path,
) -> None:
    lines, _, _ = run_check(tmp_path, served_gateway="error: timed out")

    assert lines[3].startswith(
        "FAIL  telemetry stores: the connection to Loki's gateway"
    )
    assert "gave no certificate" in lines[3]


def test_only_the_public_certificate_of_a_secret_is_read(tmp_path: Path) -> None:
    _, asked, _ = run_check(tmp_path)
    reads = [c for c in asked.splitlines() if " get secret " in c]

    assert len(reads) == 2
    for call in reads:
        assert call.endswith(r"-o jsonpath={.data.tls\.crt}")
    assert not any("tls.key" in c or re.search(r"-o (yaml|json)\b", c) for c in reads)


def test_a_failed_exec_is_reported_as_an_error_and_never_as_a_refusal(
    tmp_path: Path,
) -> None:
    lines, _, _ = run_check(
        tmp_path, exec_status=1, exec_error="Traceback: name does not resolve"
    )

    assert verdicts(lines) == ["FAIL"] * 5
    assert "name does not resolve" in " ".join(lines)
    assert not any(line.startswith("PASS") for line in lines)


def test_the_probe_pod_is_the_collectors_in_observability_with_no_token(
    tmp_path: Path,
) -> None:
    _, asked, _ = run_check(tmp_path)
    pod = json.loads((tmp_path / "state" / "created.json").read_text())

    assert pod["metadata"]["namespace"] == "observability"
    assert pod["metadata"]["labels"] == {
        "meridian-smoke": "telemetry-probe",
        "app.kubernetes.io/name": "opentelemetry-collector",
    }
    assert pod["spec"]["automountServiceAccountToken"] is False
    (container,) = pod["spec"]["containers"]
    claims = network.DEPLOYMENT["spec"]["template"]["spec"]
    assert container["image"] == claims["containers"][0]["image"]
    assert container["securityContext"] == claims["containers"][0]["securityContext"]
    assert pod["spec"]["securityContext"] == claims["securityContext"]
    assert pod["spec"]["activeDeadlineSeconds"] == 300
    # No volume, so no client certificate or key can be in it.
    assert "volumes" not in pod["spec"]
    assert "volumeMounts" not in container
    name = pod["metadata"]["name"]
    calls = asked.splitlines()
    assert calls[-1] == (
        f"-n observability delete pod {name} --ignore-not-found --wait=false"
    )


def test_the_probe_presents_no_certificate_and_names_the_stores_addresses(
    tmp_path: Path,
) -> None:
    _, asked, _ = run_check(tmp_path)
    execs = [c for c in asked.splitlines() if " exec " in c]
    text = " ".join(execs)

    assert len(execs) == 2 + 1 + 1 + 1 + 2  # two requests, the alert, Tempo's and
    # the gateway's fingerprints, and Loki's port twice (the control is the second)
    assert GATEWAY.split(":")[0] in text and TEMPO.split(":")[0] in text
    assert LOKI.split(":")[0] in text
    assert "load_cert_chain" not in text and "certfile" not in text
    assert "tls.key" not in text and "client.pem" not in text


def test_leftover_probe_pods_older_than_five_minutes_are_deleted_by_name(
    tmp_path: Path,
) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=900)).strftime("%Y-%m-%dT%H:%M:%SZ")
    young = (datetime.now(UTC) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pods = [
        {"metadata": {"name": "smoke-telemetry-1", "creationTimestamp": old}},
        {"metadata": {"name": "smoke-telemetry-2", "creationTimestamp": young}},
    ]

    _, asked, _ = run_check(tmp_path, leftover_pods=pods)

    assert "-n observability delete pod smoke-telemetry-1 --ignore-not-found" in asked
    assert "smoke-telemetry-2" not in asked


def test_no_meridian_deployment_is_one_skip_and_no_pod(tmp_path: Path) -> None:
    lines, asked, _ = run_check(tmp_path, deployed="")

    assert [line.split()[0] for line in lines] == ["SKIP"]
    assert "make deploy" in lines[0]
    assert " create " not in asked


def test_a_missing_gateway_is_one_fail_that_says_make_up(tmp_path: Path) -> None:
    lines, asked, _ = run_check(tmp_path, gateway_deployed="no")

    assert verdicts(lines) == ["FAIL"]
    assert "make up" in lines[0]
    assert " create " not in asked


def test_a_pod_that_cannot_be_made_or_is_never_ready_is_one_fail(
    tmp_path: Path,
) -> None:
    made, _, _ = run_check(tmp_path / "a", create_status=1)
    ready, _, _ = run_check(tmp_path / "b", wait_status=1)

    assert verdicts(made) == ["FAIL"]
    assert "could not create the probe Pod" in made[0]
    assert verdicts(ready) == ["FAIL"]
    assert "was not Ready" in ready[0]
