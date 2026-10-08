"""The NetworkPolicy of smoke's telemetry Jobs, in `meridian` (S063, contract N1).

``infra/kind/manifests/smoke-networkpolicy.yaml`` lets the pods of smoke's
telemetrygen Jobs reach DNS and the collector and nothing else. The Job itself is
read from the heredoc of ``start_job`` in ``smoke.sh``, with the variables it names
filled in. The policies of the two platform namespaces and the helpers these tests
share with them are in ``test_kind_namespace_policies.py``.
"""

import re

import yaml
from chartsupport import peers, reaches
from kindsupport import SMOKE_SH, function_body
from test_kind_namespace_policies import (
    SMOKE_FILE,
    dns_rule,
    header_of,
    pods,
    policies_of,
    selected,
)

# ── smoke's telemetry Jobs, in meridian ──────────────────────────────────────


def smoke_policy() -> dict:
    (policy,) = policies_of(SMOKE_FILE).values()
    return policy


def test_smokes_job_pods_may_reach_dns_and_the_collector_only() -> None:
    policy = smoke_policy()
    collector = peers()["collector"]

    assert policy["metadata"]["namespace"] == "meridian"
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    # No ingress rule: a Job's pod takes no call.
    assert "ingress" not in policy["spec"]
    assert policy["spec"]["egress"] == [
        dns_rule(),
        {
            "to": [pods(collector["podLabels"], collector["namespace"])],
            "ports": collector["ports"],
        },
    ]
    assert reaches(policy, "egress", collector)
    assert [p["port"] for p in collector["ports"]] == [4318]


def test_the_policy_selects_smokes_label_not_the_one_the_database_admits() -> None:
    policy = smoke_policy()
    manifest = start_job_manifest()
    template_labels = manifest["spec"]["template"]["metadata"]["labels"]

    assert selected(policy) == {"app.kubernetes.io/name": "meridian-smoke"}
    assert selected(policy).items() <= template_labels.items()
    # The database admits every pod of meridian that carries part-of=meridian: the
    # Job is not one, and the chart's default-deny selects it for the rest.
    assert "app.kubernetes.io/part-of" not in template_labels
    assert manifest["metadata"]["namespace"] == "meridian"
    assert "smoke" in header_of(SMOKE_FILE) and "default-deny" in header_of(SMOKE_FILE)


def start_job_manifest() -> dict:
    """The Job ``start_job`` of smoke.sh makes, read from the script's own text:
    its heredoc with the variables it names filled in."""
    body = re.search(r"^start_job\(\) \{\n.*?<<EOF\n(.*?)^EOF$", SMOKE_SH, re.M | re.S)
    assert body, "no heredoc in start_job"
    text = body.group(1)
    constants = {
        name: value
        for name, value in re.findall(r"^readonly (\w+)=(\S+)$", SMOKE_SH, re.M)
    }
    values = {
        "signal": "traces",
        "epoch": "1",
        "service": "meridian-smoke-1",
        "count_flag": "--traces",
        "TELEMETRYGEN_IMAGE": "telemetrygen:test",
        "TELEMETRYGEN_NAMESPACE": constants.get("TELEMETRYGEN_NAMESPACE", ""),
        "COLLECTOR_ENDPOINT": constants["COLLECTOR_ENDPOINT"],
        # S063: the authority's ConfigMap and the file telemetrygen trusts.
        "TELEMETRY_CA_CONFIGMAP": constants["TELEMETRY_CA_CONFIGMAP"],
        "TELEMETRYGEN_CA_DIRECTORY": constants["TELEMETRYGEN_CA_DIRECTORY"],
        "TELEMETRYGEN_CA_FILE": constants["TELEMETRYGEN_CA_DIRECTORY"] + "/ca.crt",
    }
    filled = re.sub(r"\$\{(\w+)\}", lambda m: values[m.group(1)], text)
    return yaml.safe_load(filled)


def test_smokes_telemetry_job_runs_in_meridian_and_pushes_otlp_over_http_to_4318() -> (
    None
):
    manifest = start_job_manifest()
    peer = peers()["collector"]
    (container,) = manifest["spec"]["template"]["spec"]["containers"]
    args = container["args"]

    assert manifest["metadata"]["namespace"] == "meridian"
    assert "--otlp-http" in args
    # S063: the push is TLS, verified against the authority's file.
    assert "--otlp-insecure" not in args
    assert args[args.index("--ca-cert") + 1] == "/etc/telemetry-ca/ca.crt"
    endpoint = args[args.index("--otlp-endpoint") + 1]
    # The same address and port the six services push to, not the gRPC port.
    port = peer["ports"][0]["port"]
    assert endpoint == f"otel-collector.{peer['namespace']}.svc.cluster.local:{port}"
    assert not endpoint.endswith(":4317")
    # The signal flags keep the default URL paths of the HTTP exporter.
    assert "--otlp-http-url-path" not in args


def test_the_three_jobs_one_manifest_ends_at_a_deadline_so_the_ttl_can_remove_it() -> (
    None
):
    manifest = start_job_manifest()
    starts = re.findall(
        r"^\s+start_job (\w+) --\1$", function_body(SMOKE_SH, "check_telemetry"), re.M
    )

    # One heredoc makes all three Jobs. A Job whose pod never starts never
    # finishes, and ttlSecondsAfterFinished counts from a finished Job only.
    assert starts == ["traces", "logs", "metrics"]
    assert manifest["spec"]["activeDeadlineSeconds"] > 0
    assert manifest["spec"]["ttlSecondsAfterFinished"] > 0


def test_the_job_meets_restricted_so_meridians_warn_and_audit_say_nothing() -> None:
    manifest = start_job_manifest()
    pod = manifest["spec"]["template"]["spec"]
    (container,) = pod["containers"]

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
