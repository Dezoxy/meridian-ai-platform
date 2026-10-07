"""What ``make up`` does with Prometheus's gateway (S072, contracts M4 and M4b).

``up.sh`` calls ``apply_prometheus_gateway`` right after the stack's release; the
function and its helpers are in ``gateways.sh``, which ``up.sh`` sources. No
cluster is needed: the tests read the order of ``up.sh``'s lines and run the
functions of ``gateways.sh`` (and the ``object_fingerprint`` of ``up.sh`` that
they call) in bash against a stub ``kctl``. The gateway's manifest, certificate
and configuration are judged in ``test_telemetry_prometheus_gateway.py``.
"""

import re
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from prometheusgatewaysupport import (
    GATEWAY_FILE,
    PLACEHOLDERS,
    SERVICE_ADDRESS,
    nginx_conf,
)
from test_certificate_policy_up import (
    GATEWAYS_SH,
    UP_SH,
    gateways_function,
    script_lines,
    up_function,
)
from test_kind_namespace_policies import PROMETHEUS_POLICY_FILE
from test_kind_platform_images import PINS

# ── up.sh ────────────────────────────────────────────────────────────────────


def first_line(prefix: str) -> int:
    return next(i for i, line in enumerate(script_lines()) if line.startswith(prefix))


def test_the_gateway_is_applied_right_after_the_stack_and_before_the_collector() -> (
    None
):
    lines = script_lines()
    stack = first_line("install_release kube-prometheus-stack ")
    available = first_line(
        "kctl -n observability wait --for=condition=Available prometheus/"
    )
    applied = lines.index("apply_prometheus_gateway")
    tempo = first_line("install_release tempo ")
    collector = first_line("install_release otel-collector ")

    # After the stack's release (which points Grafana at the gateway) and its wait,
    # and before every store and the collector.
    assert stack < available < applied < tempo < collector
    assert lines.count("apply_prometheus_gateway") == 1  # one call, no other


def test_the_function_builds_then_applies_the_policies_then_the_gateway_and_waits() -> (
    None
):
    body = gateways_function("apply_prometheus_gateway")
    built_at = body.index('manifest="$(prometheus_gateway_manifest ')
    policies_at = body.index('-f "${PROMETHEUS_POLICY_FILE}"')
    manifest_at = body.index("kctl apply --server-side --force-conflicts -f - <<<")
    wait_at = body.index("kctl -n observability rollout status deployment/")

    # Nothing that closes a port follows an unchecked step: the manifest is built
    # (and its placeholders checked) before the policies are applied.
    assert built_at < policies_at < manifest_at < wait_at
    assert "object_fingerprint secret prometheus-gateway-tls 'ca\\.crt'" in body
    assert 'address="$(prometheus_service_address)"' in body
    assert "tls.key" not in body and "tls\\.key" not in body
    # The wait is on the ROLLOUT: `wait --for=condition=Available` stays true while
    # the old pod serves, so on a warm cluster it would return at once.
    assert "rollout status deployment/prometheus-gateway" in body
    assert "condition=Available" not in body.split("apply_prometheus_gateway() {")[-1]
    assert "--timeout=5m" in body[wait_at:]
    assert "make up" in body[wait_at:]
    assert "did not finish rolling out" in body[wait_at:]


def test_the_files_up_applies_are_the_files_this_branch_holds() -> None:
    constants = dict(
        re.findall(r'^readonly (PROMETHEUS_\w+)="?([^"\n]+)"?$', GATEWAYS_SH, re.M)
    )

    assert constants["PROMETHEUS_POLICY_FILE"] == (
        "${KIND_DIR}/manifests/observability-prometheus-networkpolicy.yaml"
    )
    assert constants["PROMETHEUS_GATEWAY_FILE"] == (
        "${KIND_DIR}/manifests/observability-prometheus-gateway.yaml"
    )
    assert PROMETHEUS_POLICY_FILE.is_file() and GATEWAY_FILE.is_file()
    assert (
        constants["PROMETHEUS_GATEWAY_IMAGE_PLACEHOLDER"],
        constants["PROMETHEUS_GATEWAY_MANIFEST_PLACEHOLDER"],
        constants["PROMETHEUS_GATEWAY_CA_PLACEHOLDER"],
        constants["PROMETHEUS_GATEWAY_ADDRESS_PLACEHOLDER"],
    ) == PLACEHOLDERS


def run_up_functions(
    tmp_path: Path,
    calls: str,
    *,
    manifest: Path = GATEWAY_FILE,
    wait_fails: bool = False,
    address: str = SERVICE_ADDRESS,
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    """The functions of up.sh that build and apply the gateway, in bash against a
    stub ``kctl``: it logs each call, answers a read of a Secret with a base64
    certificate text and a read of the Service with ``address``, and keeps what an
    ``apply -f -`` receives. It models the difference that matters on a warm
    cluster: ``wait --for=condition=Available`` ALWAYS succeeds (the old pod keeps
    the Deployment Available) and only ``rollout status`` fails when the new pod
    never becomes Ready (``wait_fails``). Returns the process, the logged calls and
    the manifest applied."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "calls"
    applied = tmp_path / "applied.yaml"
    script = "\n".join(
        [
            "set -euo pipefail",
            'log() { echo "LOG $*"; }',
            'die() { printf "error: %s\\n" "$*" >&2; exit 1; }',
            f'readonly PROMETHEUS_POLICY_FILE="{PROMETHEUS_POLICY_FILE}"',
            f'readonly PROMETHEUS_GATEWAY_FILE="{manifest}"',
            f"readonly PROMETHEUS_GATEWAY_IMAGE_PLACEHOLDER={PLACEHOLDERS[0]}",
            f"readonly PROMETHEUS_GATEWAY_MANIFEST_PLACEHOLDER={PLACEHOLDERS[1]}",
            f"readonly PROMETHEUS_GATEWAY_CA_PLACEHOLDER={PLACEHOLDERS[2]}",
            f"readonly PROMETHEUS_GATEWAY_ADDRESS_PLACEHOLDER={PLACEHOLDERS[3]}",
            "kctl() {",
            f'  echo "$*" >>"{log}"',
            '  case "$*" in',
            '    *"get secret"*) printf "%s" "Q0VSVElGSUNBVEUtVEVYVA==" ;;',
            '    *"get service"*) printf "%s" "${SERVICE_ADDRESS}" ;;',
            f'    *"apply"*"-f -"*) cat >"{applied}" ;;',
            '    *" rollout status "*) [[ "${WAIT_FAILS}" != yes ]] || return 1 ;;',
            "  esac",
            "}",
            up_function("object_fingerprint"),
            gateways_function("fill_placeholder"),
            gateways_function("prometheus_service_address"),
            gateways_function("prometheus_gateway_manifest"),
            gateways_function("apply_prometheus_gateway"),
            calls,
        ]
    )
    env = {
        "PATH": __import__("os").environ["PATH"],
        "WAIT_FAILS": "yes" if wait_fails else "no",
        "SERVICE_ADDRESS": address,
        "NGINX_GATEWAY_IMAGE_REPOSITORY": PINS["NGINX_GATEWAY_IMAGE_REPOSITORY"],
        "NGINX_GATEWAY_IMAGE_TAG": PINS["NGINX_GATEWAY_IMAGE_TAG"],
        "NGINX_GATEWAY_IMAGE_DIGEST": PINS["NGINX_GATEWAY_IMAGE_DIGEST"],
    }
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    asked = log.read_text().splitlines() if log.exists() else []
    return done, asked, applied.read_text() if applied.exists() else ""


def test_up_writes_the_pin_the_two_digests_and_the_address_into_the_deployment(
    tmp_path: Path,
) -> None:
    done, _, applied = run_up_functions(tmp_path, "apply_prometheus_gateway")

    assert done.returncode == 0, done.stderr
    documents = [d for d in yaml.safe_load_all(applied) if d]
    pod = next(d for d in documents if d["kind"] == "Deployment")["spec"]["template"]
    pinned = (
        f"{PINS['NGINX_GATEWAY_IMAGE_REPOSITORY']}:{PINS['NGINX_GATEWAY_IMAGE_TAG']}"
        f"@{PINS['NGINX_GATEWAY_IMAGE_DIGEST']}"
    )
    assert pod["spec"]["containers"][0]["image"] == pinned
    assert re.fullmatch(r"[^@\s]+:[\w.-]+@sha256:[0-9a-f]{64}", pinned)
    assert pod["metadata"]["annotations"] == {
        "meridian-manifest-sha256": sha256(GATEWAY_FILE.read_bytes()).hexdigest(),
        "meridian-ca-sha256": sha256(b"CERTIFICATE-TEXT").hexdigest(),
        "meridian-prometheus-address": SERVICE_ADDRESS,
    }
    for placeholder in PLACEHOLDERS:
        assert placeholder not in applied
    # The ConfigMap reached the cluster whole: the configuration is the file's.
    config = next(d for d in documents if d["kind"] == "ConfigMap")
    assert config["data"]["nginx.conf"] == nginx_conf()


def test_up_builds_the_manifest_then_applies_the_policies_the_gateway_and_waits(
    tmp_path: Path,
) -> None:
    done, asked, _ = run_up_functions(tmp_path, "apply_prometheus_gateway")

    assert done.returncode == 0, done.stderr
    kubectl = [c for c in asked if not c.startswith("LOG")]
    # The Secret is read and the manifest built (and checked) first, so that nothing
    # is closed when the manifest is broken; then the policies, the gateway, the wait.
    assert kubectl[0] == (
        "-n observability get secret prometheus-gateway-tls "
        "-o jsonpath={.data.ca\\.crt}"
    )
    assert kubectl[1] == (
        "-n observability get service kube-prometheus-stack-prometheus "
        "-o jsonpath={.spec.clusterIP}"
    )
    assert kubectl[2].startswith("apply --server-side --force-conflicts -f ")
    assert kubectl[2].endswith("observability-prometheus-networkpolicy.yaml")
    assert kubectl[3] == "apply --server-side --force-conflicts -f -"
    # The ROLLOUT of the new pod, not the Deployment's Available condition.
    assert kubectl[4] == (
        "-n observability rollout status deployment/prometheus-gateway --timeout=5m"
    )
    assert len(kubectl) == 5
    # Only the public field of the Secret is read.
    assert not any("tls.key" in c or "tls\\.key" in c for c in asked)


def test_a_gateway_whose_new_pod_never_becomes_ready_stops_make_up_with_a_sentence(
    tmp_path: Path,
) -> None:
    # The stub's `wait --for=condition=Available` succeeds whatever happens (the old
    # pod keeps the Deployment Available on a warm cluster); only the rollout fails.
    # A wait on that condition would pass this test's setup and fail this assertion.
    done, asked, _ = run_up_functions(
        tmp_path, "apply_prometheus_gateway", wait_fails=True
    )

    assert done.returncode == 1
    assert any(" rollout status " in f" {c} " for c in asked)
    assert not any(" wait " in f" {c} " for c in asked)
    assert "prometheus-gateway" in done.stderr and "5m" in done.stderr
    assert "did not finish rolling out" in done.stderr
    assert "make up" in done.stderr and "refused and dropped" in done.stderr
    assert "old pod may still serve" in done.stderr


@pytest.mark.parametrize("answer", ["", "None", "no address", "10.0.0.1; x"])
def test_a_prometheus_service_with_no_usable_address_stops_before_anything_is_applied(
    tmp_path: Path, answer: str
) -> None:
    done, asked, applied = run_up_functions(
        tmp_path, "apply_prometheus_gateway", address=answer
    )

    assert done.returncode == 1, answer
    assert "kube-prometheus-stack-prometheus" in done.stderr
    assert "no cluster address" in done.stderr
    assert applied == "" and not any(" apply " in f" {c} " for c in asked)


def test_a_service_made_again_changes_the_pod_template_and_so_rolls_the_gateway(
    tmp_path: Path,
) -> None:
    def template(address: str) -> dict:
        done, _, applied = run_up_functions(
            tmp_path / address.replace(".", "-"),
            "apply_prometheus_gateway",
            address=address,
        )
        assert done.returncode == 0, done.stderr
        deployment = next(
            d for d in yaml.safe_load_all(applied) if d and d["kind"] == "Deployment"
        )
        return deployment["spec"]["template"]

    before, after = template("192.0.2.17"), template("192.0.2.99")

    assert before != after
    assert before["metadata"]["annotations"]["meridian-prometheus-address"] == (
        "192.0.2.17"
    )
    assert after["metadata"]["annotations"]["meridian-prometheus-address"] == (
        "192.0.2.99"
    )
    # Nothing else differs: the address alone is what changed the template.
    before["metadata"]["annotations"].pop("meridian-prometheus-address")
    after["metadata"]["annotations"].pop("meridian-prometheus-address")
    assert before == after


def test_a_placeholder_word_left_after_the_fills_is_refused(tmp_path: Path) -> None:
    # A fifth placeholder added to the manifest and not filled by the function would
    # be applied as it stands.
    extra = tmp_path / "extra.yaml"
    extra.write_text(
        GATEWAY_FILE.read_text("utf-8") + "\n# NEW-THING-PLACEHOLDER\n",
        encoding="utf-8",
    )

    done, asked, applied = run_up_functions(
        tmp_path, "apply_prometheus_gateway", manifest=extra
    )

    assert done.returncode == 1
    assert "still holds a placeholder word" in done.stderr
    assert applied == "" and not any(" apply " in f" {c} " for c in asked)


@pytest.mark.parametrize(
    ("what", "mutate"),
    [
        ("a missing placeholder", lambda t: t.replace("CA-SHA256-PLACEHOLDER", "x")),
        ("a doubled placeholder", lambda t: t + "\n# IMAGE-PLACEHOLDER\n"),
        ("a missing image", lambda t: t.replace("IMAGE-PLACEHOLDER", "x")),
    ],
)
def test_a_manifest_that_lost_or_doubled_a_placeholder_is_never_applied(
    tmp_path: Path, what: str, mutate
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text(mutate(GATEWAY_FILE.read_text("utf-8")), encoding="utf-8")

    done, asked, applied = run_up_functions(
        tmp_path, "apply_prometheus_gateway", manifest=broken
    )

    assert done.returncode == 1, what
    assert "placeholder" in done.stderr, what
    assert applied == "", what
    # Nothing was applied at all: the manifest is built and checked before the
    # policies that close Prometheus's port, so a broken file leaves the cluster as
    # it was (the Secret was read, and that is every call).
    assert not any(" apply " in f" {c} " for c in asked), asked
    assert all("get secret" in c or "get service" in c for c in asked), asked


def test_the_value_of_a_placeholder_is_never_read_as_an_expression(
    tmp_path: Path,
) -> None:
    hostile = "a&b|c\\1/$(touch x)`touch y`"
    done, _, _ = run_up_functions(
        tmp_path,
        f"fill_placeholder 'one X two' X '{hostile}'",
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == f"one {hostile} two"
    assert not (tmp_path / "x").exists() and not (tmp_path / "y").exists()


def test_the_pin_is_the_one_name_both_gateways_read() -> None:
    loki_release = next(
        line for line in script_lines() if line.startswith("install_release loki ")
    )

    assert (
        PINS["NGINX_GATEWAY_IMAGE_REPOSITORY"]
        == "docker.io/nginxinc/nginx-unprivileged"
    )
    assert "${NGINX_GATEWAY_IMAGE_TAG}" in loki_release
    assert "${NGINX_GATEWAY_IMAGE_DIGEST}" in loki_release
    # The repository too (contract M4b, review L-6): Loki's chart is given the
    # registry and the repository of the pin, so a repository changed in pins.env
    # moves BOTH gateways, and not Prometheus's alone. The chart's keys are
    # `gateway.image.registry` and `.repository` (values.yaml of Loki 18.13.7).
    assert '--set "gateway.image.registry=${NGINX_GATEWAY_IMAGE_REPOSITORY%%/*}"' in (
        loki_release
    )
    assert '--set "gateway.image.repository=${NGINX_GATEWAY_IMAGE_REPOSITORY#*/}"' in (
        loki_release
    )
    registry, _, repository = PINS["NGINX_GATEWAY_IMAGE_REPOSITORY"].partition("/")
    assert (registry, repository) == ("docker.io", "nginxinc/nginx-unprivileged")
    assert "LOKI_GATEWAY_IMAGE" not in UP_SH and "LOKI_GATEWAY_IMAGE" not in "\n".join(
        PINS
    )
    body = gateways_function("apply_prometheus_gateway")
    assert (
        "${NGINX_GATEWAY_IMAGE_REPOSITORY}:${NGINX_GATEWAY_IMAGE_TAG}@${NGINX_GATEWAY_IMAGE_DIGEST}"
        in body
    )
    # No second name for the same digest: one pin serves both gateways.
    digests = [v for k, v in PINS.items() if v == PINS["NGINX_GATEWAY_IMAGE_DIGEST"]]
    assert len(digests) == 1
