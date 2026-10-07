"""approver-policy's liveness probe, added after the install (S073).

The chart (cert-manager-approver-policy v0.28.0) renders a readiness probe and
has no value for a liveness probe; its schema refuses a key it does not know. So
``infra/kind/manifests/approver-policy-liveness.yaml`` holds the one field, and
``infra/kind/up.sh`` applies it server-side under the field manager
``meridian-kind`` after the release is installed and before the policies are
applied. No cluster is needed: the tests read the manifest and the script, and run
the script's function in bash against a stub ``kctl``. They cannot say that the
kubelet restarts a frozen process, that a second ``make up`` leaves the probe in
place, or that the pod stays quiet under load: only a cluster shows those.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from certpolicysupport import KIND_DIR

MANIFEST = KIND_DIR / "manifests" / "approver-policy-liveness.yaml"
UP_SH = (KIND_DIR / "up.sh").read_text(encoding="utf-8")
PINS = (KIND_DIR / "pins.env").read_text(encoding="utf-8")

FIELD_MANAGER = "meridian-kind"

# What the chart renders, read in templates/deployment.yaml of the chart at
# CHART_READ and in a `helm template` of it with values/approver-policy.yaml: the
# container is named like the Deployment (the chart's name helper), and the
# health port is the one named `healthcheck` (app.readinessProbe.port, 6060).
# Neither can be read here without the chart, so they are pinned with the
# version they were read at; the test below fails when pins.env moves on.
CHART_READ = "v0.28.0"
CHART_CONTAINER_NAME = "cert-manager-approver-policy"
CHART_HEALTH_PORT_NAME = "healthcheck"
CHART_NAMESPACE = "cert-manager"


def probe_document() -> dict:
    documents = [
        document
        for document in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8"))
        if document is not None
    ]
    assert len(documents) == 1
    return documents[0]


def the_container() -> dict:
    (container,) = probe_document()["spec"]["template"]["spec"]["containers"]
    return container


def script_lines() -> list[str]:
    """up.sh with each backslash continuation, and each ``||`` that ends a line,
    folded into one line."""
    joined = re.sub(r"\\\n\s*", "", UP_SH)
    return re.sub(r"\|\|\n\s*", "|| ", joined).splitlines()


def folded(text: str) -> str:
    """Each backslash continuation, and each ``||`` that ends a line, folded."""
    joined = re.sub(r"\\\n\s*", "", text)
    return re.sub(r"\|\|\n\s*", "|| ", joined)


def up_function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", UP_SH, re.MULTILINE | re.DOTALL)
    assert match, f"no function {name}"
    return match.group(0)


# ── the chart the manifest was written against ───────────────────────────────


def test_the_pins_still_hold_the_chart_the_container_and_port_names_were_read_in() -> (
    None
):
    (pinned,) = re.findall(r"^APPROVER_POLICY_VERSION=(\S+)$", PINS, re.MULTILINE)

    assert pinned == CHART_READ, (
        f"the approver-policy chart moved from {CHART_READ} to {pinned}: read the "
        "chart's deployment template again (the container's name, the health "
        "port's name, and whether the chart has taken a liveness probe, which "
        "would make the manifest a conflict), then change CHART_READ here"
    )


def test_the_probe_is_on_the_container_and_the_port_name_the_chart_renders() -> None:
    document = probe_document()
    container = the_container()

    assert document["apiVersion"] == "apps/v1"
    assert document["kind"] == "Deployment"
    # The chart's Deployment is named like its container, in the release's namespace.
    assert document["metadata"] == {
        "name": CHART_CONTAINER_NAME,
        "namespace": CHART_NAMESPACE,
    }
    assert container["name"] == CHART_CONTAINER_NAME
    assert container["livenessProbe"]["httpGet"]["port"] == CHART_HEALTH_PORT_NAME


def test_the_manifest_is_for_the_namespace_up_installs_the_release_into() -> None:
    (namespace,) = re.findall(
        r"^install_release approver-policy (\S+) ", UP_SH, re.MULTILINE
    )

    assert namespace == CHART_NAMESPACE == probe_document()["metadata"]["namespace"]


# ── the probe ────────────────────────────────────────────────────────────────


def test_the_manifest_holds_exactly_one_liveness_probe_with_the_agreed_numbers() -> (
    None
):
    text = MANIFEST.read_text(encoding="utf-8")
    probe = the_container()["livenessProbe"]

    assert len(re.findall(r"^\s+livenessProbe:", text, re.MULTILINE)) == 1
    assert probe == {
        "httpGet": {"path": "/readyz", "port": CHART_HEALTH_PORT_NAME},
        "initialDelaySeconds": 30,
        "periodSeconds": 20,
        "timeoutSeconds": 5,
        "failureThreshold": 6,
    }


@pytest.mark.parametrize("path", ["/healthz", "/livez"])
def test_the_probe_never_asks_for_a_path_the_binary_does_not_serve(path: str) -> None:
    # Both answer 404 on approver-policy (read on the running pod, 2026-10-07):
    # a liveness probe there fails from its first try and the pod restarts for ever.
    served = the_container()["livenessProbe"]["httpGet"]["path"]

    assert served != path
    assert served == "/readyz"
    # And in no field of the file, a comment aside.
    code = "\n".join(
        line
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert path not in code


def test_the_values_file_sets_no_liveness_probe_the_charts_schema_refuses_it() -> None:
    # The chart's values schema has additionalProperties: false at the top, at
    # `app` and at `app.readinessProbe`: a liveness key in the values file is an
    # error from Helm, not a setting. This stays false; the probe is the
    # manifest's (the test above), and the values file says where it comes from.
    path = KIND_DIR / "values" / "approver-policy.yaml"
    text = path.read_text(encoding="utf-8")
    values = yaml.safe_load(text)

    assert "livenessProbe" not in values
    assert "livenessProbe" not in values.get("app", {})
    assert "manifests/approver-policy-liveness.yaml" in text


def test_the_manifest_owns_the_probe_and_no_other_field() -> None:
    document = probe_document()

    # Every level down to the probe holds the keys that name the object and
    # the path to the container, and nothing else: no image, no replicas, no
    # resources, no other field of the container.
    assert set(document) == {"apiVersion", "kind", "metadata", "spec"}
    assert set(document["spec"]) == {"template"}
    assert set(document["spec"]["template"]) == {"spec"}
    assert set(document["spec"]["template"]["spec"]) == {"containers"}
    assert set(the_container()) == {"name", "livenessProbe"}
    assert set(the_container()["livenessProbe"]) == {
        "httpGet",
        "initialDelaySeconds",
        "periodSeconds",
        "timeoutSeconds",
        "failureThreshold",
    }
    assert set(the_container()["livenessProbe"]["httpGet"]) == {"path", "port"}


def test_the_header_says_why_the_path_the_numbers_and_the_two_limits() -> None:
    header = "\n".join(
        line
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    )

    assert "/healthz" in header and "404" in header
    assert "under load" in header
    assert "frozen process" in header
    assert "stuck" in header and "reconciler" in header
    assert "no value" in header
    assert "refuses" in header


# ── what up.sh does with it ──────────────────────────────────────────────────


def test_up_applies_the_probe_after_the_install_and_before_the_policies() -> None:
    lines = script_lines()
    installed = next(
        i for i, line in enumerate(lines) if line.startswith("install_release approver")
    )
    called = lines.index("apply_approver_liveness_probe")
    policies = lines.index("apply_certificate_policy")

    assert installed < called < policies
    assert lines.count("apply_approver_liveness_probe") == 1


def test_up_applies_it_server_side_as_meridian_kind_and_never_forces_conflicts() -> (
    None
):
    body = folded(up_function("apply_approver_liveness_probe"))
    (applied,) = [
        line for line in body.splitlines() if "approver-policy-liveness.yaml" in line
    ]

    assert 'kctl apply --server-side --field-manager=meridian-kind -f "' in applied
    assert applied.count("--field-manager=") == 1
    assert f"--field-manager={FIELD_MANAGER} " in applied
    # A conflict with the chart must fail, so nothing here takes the field by force.
    assert "--force-conflicts" not in body
    assert "--force" not in body.replace("--force-", "")
    # Outside comments the script names the manifest once: in this function.
    assert (
        sum(
            "approver-policy-liveness.yaml" in line
            for line in script_lines()
            if not line.lstrip().startswith("#")
        )
        == 1
    )


def test_the_manager_name_is_not_one_the_chart_or_the_other_applies_use() -> None:
    # Helm applies as `helm`; kubectl's own default is `kubectl`. The probe is
    # owned by a manager of its own, so a conflict names it.
    assert FIELD_MANAGER not in {"helm", "kubectl", "kubectl-client-side-apply"}
    assert UP_SH.count(f"--field-manager={FIELD_MANAGER}") == 1


def test_up_waits_for_the_rollout_with_a_bound_and_dies_saying_where_to_look() -> None:
    body = folded(up_function("apply_approver_liveness_probe"))
    (waited,) = [line for line in body.splitlines() if "rollout status" in line]
    found = re.search(
        r"rollout status deployment/cert-manager-approver-policy "
        r'--timeout=(\d+m) >/dev/null \|\| die "([^"]+)"$',
        waited.strip(),
    )

    assert found, waited
    timeout, message = found.groups()
    assert "-n cert-manager" in waited
    assert timeout in message
    assert "kubectl -n cert-manager get pods" in message
    assert "logs deploy/cert-manager-approver-policy" in message
    assert "Liveness probe failed" in message
    # The apply comes first in the function, then the wait.
    assert body.index("approver-policy-liveness.yaml") < body.index("rollout status")


def test_the_apply_failure_says_to_delete_the_manifest_and_set_the_charts_value() -> (
    None
):
    body = folded(up_function("apply_approver_liveness_probe"))
    (message,) = re.findall(
        r'-f "[^"]+approver-policy-liveness.yaml" 2>&1\)" \|\| die "([^"]+)"', body
    )

    assert "kubectl said: ${out}" in message
    assert "delete infra/kind/manifests/approver-policy-liveness.yaml" in message
    assert "set the chart's value" in message


def test_the_comment_says_a_second_run_is_expected_to_change_nothing_and_not_seen() -> (
    None
):
    (comment,) = re.findall(
        r"^((?:# .*\n)+)apply_approver_liveness_probe\(\) \{", UP_SH, re.MULTILINE
    )
    flat = " ".join(line.removeprefix("# ") for line in comment.splitlines())

    assert "second run" in flat
    assert "no rollout" in flat
    assert "has not been seen" in flat


def test_the_header_of_up_lists_the_probe_in_the_step_it_belongs_to() -> None:
    header = UP_SH.split("set -euo pipefail")[0]
    flat = re.sub(r"\s*\n#\s*", " ", header)

    assert "liveness probe" in flat
    assert flat.index("approver-policy") < flat.index("liveness probe")
    assert flat.index("liveness probe") < flat.index("the CA that signs")


# ── the function run against a stand-in kctl ─────────────────────────────────


def run_function(
    tmp_path: Path, *, fail_on: str | None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """``apply_approver_liveness_probe`` from up.sh in bash with a ``kctl`` that
    records its call and fails the one whose words hold ``fail_on``. Returns the
    process and each call's words."""
    calls = tmp_path / "calls"
    script = "\n".join(
        [
            "set -euo pipefail",
            "KIND_DIR=/kind",
            'die() { echo "DIE $*" >&2; exit 1; }',
            "kctl() {",
            f'  echo "$*" >>"{calls}"',
            '  if [[ -n "${FAIL_ON}" && "$*" == *"${FAIL_ON}"* ]]; then',
            '    echo "Apply failed with 1 conflict: conflict with \\"helm\\"" >&2',
            "    return 1",
            "  fi",
            "}",
            up_function("apply_approver_liveness_probe"),
            "apply_approver_liveness_probe",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "FAIL_ON": fail_on or ""},
        check=False,
    )
    made = calls.read_text().splitlines() if calls.exists() else []
    return done, made


def test_the_function_applies_then_waits_and_is_silent_when_both_succeed(
    tmp_path,
) -> None:
    done, calls = run_function(tmp_path, fail_on=None)

    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    assert calls == [
        "apply --server-side --field-manager=meridian-kind "
        "-f /kind/manifests/approver-policy-liveness.yaml",
        "-n cert-manager rollout status deployment/cert-manager-approver-policy "
        "--timeout=5m",
    ]


def test_a_refused_apply_stops_the_run_with_the_error_and_waits_for_nothing(
    tmp_path,
) -> None:
    done, calls = run_function(tmp_path, fail_on="--field-manager")

    assert done.returncode == 1
    assert len(calls) == 1
    assert 'conflict with "helm"' in done.stderr
    assert "the chart has taken the field" in done.stderr


def test_a_rollout_that_does_not_finish_stops_the_run_saying_where_to_look(
    tmp_path,
) -> None:
    done, calls = run_function(tmp_path, fail_on="rollout status")

    assert done.returncode == 1
    assert len(calls) == 2
    assert "did not finish its rollout" in done.stderr
    assert "kubectl -n cert-manager get pods" in done.stderr
