"""What ``make up`` and ``make deploy`` do about the telemetry authority (S063,
T-90; S072).

``up.sh`` applies the authority after the policies and before the collector, waits
for the five leaf certificates, publishes the authority's public certificate to
the namespaces that read it, and gives each pod that reads a certificate at start
the fingerprint that rolls it when one changes; ``deploy.sh`` stops before the
image when the published ConfigMap is missing. No cluster is needed: the tests
read the order of the scripts' lines, run the one function of ``up.sh`` that
publishes the certificate in bash against a stub ``kctl``, and run ``deploy.sh``
whole against stubs. The certificates and the policies are judged in
``test_telemetry_ca.py``.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from certpolicysupport import (
    AUTHORITY_POLICY,
    COLLECTOR_CLIENT_POLICY,
    COLLECTOR_POLICY,
    KIND_DIR,
    LOKI_GATEWAY_POLICY,
    POLICY_NAMES,
    PROMETHEUS_GATEWAY_POLICY,
    TEMPO_RECEIVER_POLICY,
)
from certscriptsupport import POLICIES, SECONDS
from test_certificate_deploy import run_deploy, without_the_record
from test_certificate_policy_up import (
    GATEWAYS_SH,
    UP_SH,
    line_containing,
    line_index,
    script_lines,
    up_function,
    wait_and_die_message,
)
from test_telemetry_ca import (
    AUTHORITY,
    CERTIFICATE_TEXT,
    COLLECTOR,
    COLLECTOR_CLIENT,
    LOKI_GATEWAY,
    PROMETHEUS_GATEWAY,
    TEMPO,
    b64,
    policies,
)

# ── up.sh ────────────────────────────────────────────────────────────────────


def test_up_applies_the_authority_after_the_policies_and_before_the_collector() -> None:
    lines = script_lines()
    policies_ready = line_containing("certificaterequestpolicy")
    applied = line_containing("manifests/telemetry-ca.yaml")
    waited = line_index("kctl -n observability wait --for=condition=Ready certificate/")
    published = lines.index("publish_telemetry_ca")
    collector = line_index("install_release otel-collector ")

    assert policies_ready < applied < waited < published < collector
    assert lines[applied].startswith("kctl apply --server-side --force-conflicts -f ")
    assert lines.count("publish_telemetry_ca") == 1
    for name in sorted(POLICY_NAMES):
        assert f"certificaterequestpolicy/{name}" in lines[policies_ready]


def test_up_s_wait_for_the_collectors_certificate_has_a_bound_and_a_remedy() -> None:
    timeout, message = wait_and_die_message(
        "kctl -n observability wait --for=condition=Ready certificate/otel-collector "
    )

    assert timeout == "5m"
    assert timeout in message
    # The request is what the Certificate waits for, and its Approved or Denied
    # condition says whether a policy decided it; the policies are named.
    assert "CertificateRequest" in message
    assert "Approved" in message
    assert "Denied" in message
    assert "kubectl -n observability" in message
    assert COLLECTOR_POLICY in message
    assert AUTHORITY_POLICY in message


def test_the_policy_wait_names_all_nine_policies() -> None:
    line = script_lines()[line_containing("certificaterequestpolicy")]

    assert line.count("certificaterequestpolicy/") == 9


def test_up_waits_for_the_five_leaf_certificates_before_the_stores() -> None:
    lines = script_lines()
    waited = line_index("kctl -n observability wait --for=condition=Ready certificate/")
    tempo = line_index("install_release tempo ")
    collector = line_index("install_release otel-collector ")

    # A Secret that does not exist leaves a pod in ContainerCreating and stops
    # `make up` at the release that mounts it: the client certificate (the
    # collector's release), Tempo's receiver certificate (Tempo's release, S072
    # contract M2), the gateway's (Loki's release, contract M3) and Prometheus's
    # gateway's (its Deployment, applied after the stack's release, contract M4)
    # are waited for as the server certificate is, in the same bounded wait, before
    # the first store is installed.
    loki = line_index("install_release loki ")
    stack = line_index("install_release kube-prometheus-stack ")
    assert waited < stack < tempo < loki < collector
    for name in (COLLECTOR, COLLECTOR_CLIENT, TEMPO, LOKI_GATEWAY, PROMETHEUS_GATEWAY):
        assert f"certificate/{name} " in lines[waited]
    assert lines[waited].count("certificate/") == 5
    assert "--timeout=5m" in lines[waited]
    _, message = wait_and_die_message(
        "kctl -n observability wait --for=condition=Ready certificate/"
    )
    for name in (
        COLLECTOR_CLIENT,
        COLLECTOR_CLIENT_POLICY,
        TEMPO,
        TEMPO_RECEIVER_POLICY,
        LOKI_GATEWAY,
        LOKI_GATEWAY_POLICY,
        PROMETHEUS_GATEWAY,
        PROMETHEUS_GATEWAY_POLICY,
    ):
        assert name in message


def test_the_nine_policy_names_are_the_same_in_the_four_places() -> None:
    lines = script_lines()
    in_manifest = set(policies())
    in_up = re.findall(
        r"certificaterequestpolicy/([a-z-]+)",
        lines[line_containing("certificaterequestpolicy")],
    )
    (in_deploy,) = re.findall(
        r"^readonly CERTIFICATE_POLICIES=\((.*)\)$",
        (KIND_DIR / "deploy.sh").read_text(encoding="utf-8"),
        re.M,
    )
    (in_smoke,) = re.findall(
        r"^readonly POLICY_NAMES=\((.*)\)$",
        (KIND_DIR / "smoke.d" / "10-certificate-policy.sh").read_text(encoding="utf-8"),
        re.M,
    )

    assert len(in_manifest) == 9
    # Each place lists each name once, and the lists are equal to the manifest's.
    for listed in (in_up, in_deploy.split(), in_smoke.split(), list(POLICIES)):
        assert len(listed) == 9
        assert set(listed) == in_manifest == POLICY_NAMES
    assert {
        COLLECTOR_CLIENT_POLICY,
        TEMPO_RECEIVER_POLICY,
        LOKI_GATEWAY_POLICY,
        PROMETHEUS_GATEWAY_POLICY,
    } <= in_manifest


def test_lokis_policies_are_applied_just_before_its_release_not_at_the_start() -> None:
    lines = script_lines()
    start = line_containing('apply_api_server_policy "${OBSERVABILITY_POLICY_FILE}"')
    tempo = line_index("install_release tempo ")
    applied = line_containing('-f "${LOKI_POLICY_FILE}"')
    loki = line_index("install_release loki ")
    collector = line_index("install_release otel-collector ")

    # The namespace's file is applied at the start (with the node's address), and
    # Loki's and the gateway's own file just before Loki's release (S072, contract
    # M3b): between the two only the fingerprint of the CA and the failure note.
    assert start < tempo < applied < loki < collector
    assert loki - applied <= 3
    assert lines[applied].startswith("kctl apply --server-side --force-conflicts -f ")
    assert "observability-loki-networkpolicy.yaml" in "\n".join(lines)
    assert "observability-loki-networkpolicy.yaml" not in lines[start]


def test_a_failed_release_says_the_window_stays_open_until_a_rerun() -> None:
    lines = script_lines()
    note = line_containing('release_failure_note="telemetry stays refused')
    start = line_containing('apply_api_server_policy "${OBSERVABILITY_POLICY_FILE}"')
    first_release = line_index("install_release envoy-gateway ")
    stack = line_index("install_release kube-prometheus-stack ")
    loki = line_index("install_release loki ")
    collector = line_index("install_release otel-collector ")
    empty = [i for i, line in enumerate(lines) if line == 'release_failure_note=""']
    body = up_function("install_release")

    # Empty at the top of the script, set right after the namespace's policies are
    # applied (S072, contract M4b: from that apply on, on a warm cluster, Grafana
    # cannot read Prometheus and the old collector's metrics time out) and emptied
    # again once the collector's release has run: every release between them says so.
    assert len(empty) == 2
    assert empty[0] < start < note < stack < loki < collector < empty[1]
    assert start < first_release  # the note is set before any release can fail
    for words in (
        "refused and dropped",
        "Grafana's Prometheus and Loki reads fail",
        "re-run",
        "old collector's exporters",
    ):
        assert words in lines[note], words
    assert "converges" in lines[note]
    # Three assignments in all: the empty one at the top, the one note, and the one
    # that empties it (a note set later, before Loki's release, would leave the
    # failures before it without one, which is what this replaced).
    assignments = [x for x in lines if x.startswith("release_failure_note=")]
    assert len(assignments) == 3
    assert "${release_failure_note:+: ${release_failure_note}}" in body


def test_the_authority_is_published_to_observability_before_the_stack() -> None:
    lines = script_lines()
    published = lines.index("publish_telemetry_ca")
    grafana_ca = line_containing("grafana_ca_sha=")
    stack = line_index("install_release kube-prometheus-stack ")

    # Grafana reads the ConfigMap telemetry-ca into its environment at start: the
    # pod cannot start before the ConfigMap exists.
    assert published < grafana_ca < stack


def test_each_pod_that_reads_a_certificate_at_start_gets_its_fingerprint() -> None:
    lines = script_lines()
    stack = lines[line_index("install_release kube-prometheus-stack ")]
    tempo = lines[line_index("install_release tempo ")]
    loki = lines[line_index("install_release loki ")]
    collector = lines[line_index("install_release otel-collector ")]

    # Grafana: the CA (its environment variable). Tempo: its certificate and the
    # CA (start only; no reload_interval is established). The gateway: the CA only,
    # because its certificate is a variable that nginx reads at each handshake.
    # The collector (contract M4): its client certificate and the CA; it re-reads
    # them every five minutes by itself, and a change of the certificate's SUBJECT
    # is refused by the gateways until it does (run R16).
    assert '--set-string "grafana.podAnnotations.meridian-ca-sha256=' in stack
    assert '--set-string "podAnnotations.meridian-cert-sha256=' in tempo
    assert '--set-string "podAnnotations.meridian-ca-sha256=' in tempo
    assert '--set-string "gateway.podAnnotations.meridian-ca-sha256=' in loki
    assert "meridian-cert-sha256" not in loki
    assert '--set-string "podAnnotations.meridian-client-cert-sha256=' in collector
    assert '--set-string "podAnnotations.meridian-ca-sha256=' in collector
    call = r"^(\w+)=\"\$\(object_fingerprint (\w+) (\S+) '([^']+)'\)\"$"
    calls = re.findall(call, "\n".join(lines), re.M)
    assert sorted((variable, name, field) for variable, _, name, field in calls) == [
        # Prometheus's gateway reads its own inside apply_prometheus_gateway (a
        # function, not a top-level line): test_telemetry_prometheus_gateway_up.py.
        ("collector_ca_sha", "otel-collector-client-tls", r"ca\.crt"),
        ("collector_client_sha", "otel-collector-client-tls", r"tls\.crt"),
        ("grafana_ca_sha", "telemetry-ca", r"ca\.crt"),
        ("loki_ca_sha", "loki-gateway-tls", r"ca\.crt"),
        ("tempo_ca_sha", "tempo-receiver-tls", r"ca\.crt"),
        ("tempo_cert_sha", "tempo-receiver-tls", r"tls\.crt"),
    ]


def test_the_fingerprint_function_reads_public_fields_only_and_prints_a_digest() -> (
    None
):
    body = up_function("object_fingerprint")
    # The call sites are in up.sh and, since its gateway functions moved, gateways.sh.
    sites = re.findall(
        r"object_fingerprint \w+ \S+ '([^']+)'", UP_SH + "\n" + GATEWAYS_SH
    )

    assert sites
    assert set(sites) <= {r"tls\.crt", r"ca\.crt"}
    assert "tls.key" not in body and r"tls\.key" not in body
    assert "sha256sum" in body
    assert "-o yaml" not in body and "-o json" not in body


def run_publish(
    tmp_path: Path, *, certificate_text: str | None = CERTIFICATE_TEXT, fail: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str], list[dict]]:
    """``publish_telemetry_ca`` from up.sh in bash against a stub ``kctl``. The
    stub logs each call's arguments, answers the read of ``tls.crt`` with the
    base64 of ``certificate_text`` (nothing when ``None``), builds the
    ConfigMap's JSON for ``create configmap`` in the namespace ``-n`` names, and
    records what each ``apply`` gets on its standard input. ``fail`` names a call
    (``get`` or ``apply``) that fails. Returns the process, the logged calls and
    the manifests applied, in order (one per namespace)."""
    calls = tmp_path / "calls"
    applied = tmp_path / "applied"
    applied.mkdir()
    encoded = "" if certificate_text is None else b64(certificate_text)
    fail_when = '[[ -z "${FAIL}" || "$*" != *"${FAIL}"* ]] || return 1'
    literal = "--from-literal=ca.crt="
    script = "\n".join(
        [
            "set -euo pipefail",
            'log() { echo "LOG $*"; }',
            'die() { echo "DIE $*" >&2; exit 1; }',
            "kctl() {",
            f'  echo "$*" >>"{calls}"',
            f"  {fail_when}",
            '  case "$*" in',
            '    *"get secret"*) printf "%s" "${ENCODED}" ;;',
            '    *"create configmap"*)',
            '      for a in "$@"; do',
            f'        [[ "$a" != {literal}* ]] || v="${{a#{literal}}}"',
            "      done",
            '      jq -n --arg v "${v}" --arg ns "$2" \'{apiVersion: "v1",',
            '        kind: "ConfigMap", metadata: {name: "telemetry-ca",',
            "        namespace: $ns, creationTimestamp: null},",
            '        data: {"ca.crt": $v}}\' ;;',
            f'    *"apply"*) cat >"{applied}/$(date +%s%N)" ;;',
            '    *) echo "stub kctl: unexpected $*" >&2; return 99 ;;',
            "  esac",
            "}",
            up_function("publish_telemetry_ca"),
            "publish_telemetry_ca",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "ENCODED": encoded, "FAIL": fail},
        check=False,
        timeout=SECONDS,
    )
    asked = calls.read_text().splitlines() if calls.exists() else []
    objects = [json.loads(path.read_text()) for path in sorted(applied.iterdir())]
    return done, asked, objects


def test_the_authoritys_certificate_is_published_to_three_namespaces(
    tmp_path: Path,
) -> None:
    done, asked, objects = run_publish(tmp_path)

    assert done.returncode == 0, done.stderr
    # The six services and telemetrygen's Jobs mount it in `meridian`; the log
    # agent (S064) mounts it in `logging`; Grafana's environment reads it in
    # `observability` to trust Loki's gateway (S072, contract M3): one
    # certificate, three copies.
    assert [m["metadata"]["namespace"] for m in objects] == [
        "meridian",
        "logging",
        "observability",
    ]
    for manifest in objects:
        assert manifest["kind"] == "ConfigMap"
        assert manifest["metadata"]["name"] == AUTHORITY
        assert manifest["data"] == {"ca.crt": CERTIFICATE_TEXT}
        assert "creationTimestamp" not in manifest["metadata"]
    # Server-side and forced, as every other apply here: a rerun converges.
    applies = [c for c in asked if " apply " in c]
    assert len(applies) == 3
    for apply, namespace in zip(
        applies, ["meridian", "logging", "observability"], strict=True
    ):
        assert "--server-side" in apply
        assert "--force-conflicts" in apply
        assert f"-n {namespace} " in apply
    # The Secret is read once, whatever the number of namespaces.
    assert len([c for c in asked if "get secret" in c]) == 1


def test_a_failed_apply_in_meridian_stops_before_logging_gets_a_copy(
    tmp_path: Path,
) -> None:
    done, asked, objects = run_publish(tmp_path, fail="-n meridian apply")

    assert done.returncode == 1
    assert objects == []
    assert not any("-n logging apply" in c for c in asked)


def test_the_key_of_the_authority_is_never_read_and_no_secret_is_printed(
    tmp_path: Path,
) -> None:
    done, asked, _ = run_publish(tmp_path)

    (read,) = [c for c in asked if "get secret" in c]
    # Only the one field was asked for: `-o jsonpath=` of tls.crt, never the
    # whole object (-o yaml or json) and never tls.key.
    assert read.startswith(f"-n observability get secret {AUTHORITY} ")
    assert read.endswith(r"-o jsonpath={.data.tls\.crt}")
    assert not any("tls.key" in c or "-o yaml" in c for c in asked)
    # `-o json` is on the ConfigMap's dry run, never on a read of a Secret.
    assert not any("secret" in c and re.search(r"-o (json|yaml)\b", c) for c in asked)
    # The certificate is public, and the log still does not carry it.
    assert "VEVTVC1PTkxZ" not in done.stdout + done.stderr
    assert "BEGIN" not in done.stdout + done.stderr


def test_a_secret_that_holds_a_private_key_in_tls_crt_is_refused_before_any_apply(
    tmp_path: Path,
) -> None:
    text = (
        f"{CERTIFICATE_TEXT}\n-----BEGIN EC PRIVATE KEY-----\nQQ==\n"
        "-----END EC PRIVATE KEY-----"
    )

    done, asked, objects = run_publish(tmp_path, certificate_text=text)

    assert done.returncode == 1
    assert objects == []
    assert not any("apply" in c for c in asked)
    assert "DIE" in done.stderr
    assert "tls.crt" in done.stderr
    assert "PRIVATE" not in done.stderr


@pytest.mark.parametrize("text", [None, "", "not a certificate"])
def test_a_secret_with_no_certificate_in_tls_crt_ends_the_run_with_a_remedy(
    tmp_path: Path, text: str | None
) -> None:
    done, asked, objects = run_publish(tmp_path, certificate_text=text)

    assert done.returncode == 1
    assert objects == []
    assert not any("apply" in c for c in asked)
    assert AUTHORITY in done.stderr
    assert "observability" in done.stderr


def test_a_failed_read_of_the_secret_ends_the_run_and_applies_nothing(
    tmp_path: Path,
) -> None:
    done, asked, objects = run_publish(tmp_path, fail="get secret")

    assert done.returncode == 1
    assert objects == []
    assert not any("apply" in c for c in asked)
    assert "DIE" in done.stderr


def test_a_failed_apply_ends_the_run(tmp_path: Path) -> None:
    done, _, _ = run_publish(tmp_path, fail="apply")

    assert done.returncode == 1


def test_up_publishes_the_configmap_on_every_run_not_only_when_it_is_absent() -> None:
    body = up_function("publish_telemetry_ca")

    # Idempotent by server-side apply: a renewed authority reaches the ConfigMap
    # at the next `make up`, so there is no "only if absent" guard.
    assert "get configmap" not in body
    assert "--server-side" in body
    assert re.search(r"\(\)\s*\{", body)


# ── deploy.sh ────────────────────────────────────────────────────────────────


def test_deploy_stops_before_the_image_when_the_authoritys_configmap_is_missing(
    tmp_path: Path,
) -> None:
    done, calls = run_deploy(tmp_path, "ready", telemetry_ca="missing")

    assert done.returncode != 0
    assert "telemetry-ca" in done.stderr
    assert "run 'make up'" in done.stderr
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    assert "get configmap telemetry-ca" in calls
    assert "docker build" not in calls
    assert "kind load" not in calls
    assert "helm" not in calls
    assert " apply " not in without_the_record(calls)


def test_deploy_goes_on_to_the_image_when_the_configmap_is_there(
    tmp_path: Path,
) -> None:
    done, calls = run_deploy(tmp_path, "ready")

    assert "docker build failed" in done.stderr
    assert "get configmap telemetry-ca" in calls
    assert "telemetry-ca" not in done.stderr
