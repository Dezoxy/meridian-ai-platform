"""The log agent: a node agent ships the services' output to Loki (S064, C1).

``infra/kind/values/log-agent.yaml`` is a second release of the OpenTelemetry
Collector chart that ``pins.env`` already pins, the contrib build, as a DaemonSet
in the namespace ``logging``. It reads the files of `meridian`'s pods under
``/var/log/pods`` and sends each line over OTLP with TLS to the collector.

The chart is not vendored, so no test renders it (the neighbours,
``test_kind_observability_security_context.py`` and
``test_kind_platform_images.py``, do not either, and CI has no reason to reach a
Helm repository): the values are read as committed, and the render was read with
``helm template`` of chart 0.174.0 with these values and the ``--set`` arguments
``up.sh`` passes (the report of the change says what it showed: the DaemonSet
``log-agent-agent``, one hostPath, no hostPort, no host namespace, the token off).
What the receiver makes of a line was read by running the pinned contrib image
over a fixture; that is a manual check and has no test here.

Not shown without a cluster: that the node's log files are readable by root with
no capability, that the exporter's TLS verifies against the authority, and that
a line arrives in Loki (``make smoke`` has a line for the last).
"""

import re

import yaml
from test_certificate_policy_up import (
    line_containing,
    line_index,
    script_lines,
    up_function,
)
from test_kind_manifests import KIND_DIR, UP_SH
from test_kind_platform_images import PINS, PINS_TEXT

VALUES_FILE = KIND_DIR / "values" / "log-agent.yaml"
COLLECTOR_FILE = KIND_DIR / "values" / "otel-collector.yaml"
POLICY_FILE = KIND_DIR / "manifests" / "logging-networkpolicy.yaml"

RELEASE = "log-agent"
NAMESPACE = "logging"
COLLECTOR_ADDRESS = "https://otel-collector.observability.svc.cluster.local:4318"
POD_LOGS = "/var/log/pods"
CHECKPOINTS = "/var/lib/otelcol"
DIGEST = r"sha256:[0-9a-f]{64}"


def values() -> dict:
    return yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))


def header() -> str:
    """The comment block that opens the values file, as one line of words."""
    text = VALUES_FILE.read_text(encoding="utf-8").split("\nmode:")[0]
    return " ".join(line.removeprefix("#").strip() for line in text.splitlines())


def keys_of(node: object) -> set[str]:
    """Every key of every mapping in a parsed document."""
    if isinstance(node, dict):
        return set(node).union(*(keys_of(child) for child in node.values()))
    if isinstance(node, list):
        return set().union(*(keys_of(child) for child in node))
    return set()


def receiver() -> dict:
    return values()["config"]["receivers"]["file_log"]


def volume(name: str) -> dict:
    (found,) = [v for v in values()["extraVolumes"] if v["name"] == name]
    return found


def mount(name: str) -> dict:
    (found,) = [m for m in values()["extraVolumeMounts"] if m["name"] == name]
    return found


# ── what up.sh installs ──────────────────────────────────────────────────────


def test_up_installs_the_agent_from_the_collectors_chart_with_its_own_image_pin() -> (
    None
):
    installed = script_lines()[line_index(f"install_release {RELEASE} ")]

    assert installed == (
        f"install_release {RELEASE} {NAMESPACE}"
        ' "${OTEL_COLLECTOR_CHART}" "${OTEL_COLLECTOR_VERSION}"'
        ' "${OTEL_REPO}" log-agent.yaml'
        ' --set "image.repository=${LOG_AGENT_IMAGE_REPOSITORY}"'
        ' --set "image.tag=${LOG_AGENT_IMAGE_TAG}"'
        ' --set "image.digest=${LOG_AGENT_IMAGE_DIGEST}"'
    )
    assert VALUES_FILE.is_file()


def test_the_agent_comes_after_the_collector_and_the_published_authority() -> None:
    lines = script_lines()
    published = lines.index("publish_telemetry_ca")
    collector = line_index("install_release otel-collector ")
    agent = line_index(f"install_release {RELEASE} ")
    gateway_wait = line_containing("waiting for the Gateway to be programmed")

    # It needs the ConfigMap with the authority in `logging` (a pod that cannot
    # mount it never starts) and the collector's Service to send to.
    assert published < collector < agent < gateway_wait
    assert lines[agent - 1].startswith("log ")


def test_up_applies_the_agents_policy_before_the_first_release_and_the_agent() -> None:
    lines = script_lines()
    (namespaces,) = [i for i, line in enumerate(lines) if "namespaces.yaml" in line]
    first_release = line_index("install_release envoy-gateway")
    applied = line_containing("manifests/logging-networkpolicy.yaml")

    assert namespaces < applied < first_release
    assert applied < line_index(f"install_release {RELEASE} ")
    assert lines[applied].startswith("kctl apply --server-side --force-conflicts -f ")
    assert lines[applied - 1].startswith("log ")
    assert POLICY_FILE.is_file()


def test_the_authority_is_published_to_meridian_and_to_logging_by_one_function() -> (
    None
):
    body = up_function("publish_telemetry_ca")
    lines = script_lines()

    assert re.search(r"for namespace in meridian logging; do", body)
    assert lines.count("publish_telemetry_ca") == 1
    assert "-n meridian" not in body and "-n logging" not in body
    assert "configmap telemetry-ca" in body


def test_the_header_of_up_names_the_log_agent_and_its_namespace() -> None:
    top = UP_SH.split("set -euo pipefail")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in top.splitlines())

    assert "log agent" in flat
    assert "logging" in flat


# ── the pin ──────────────────────────────────────────────────────────────────


def test_the_agents_image_is_the_contrib_build_of_the_collectors_own_release() -> None:
    core = PINS["OTEL_COLLECTOR_IMAGE_REPOSITORY"]
    contrib = PINS["LOG_AGENT_IMAGE_REPOSITORY"]

    # The core build has no file-reading receiver; the contrib one has it. Both
    # come from one release, so the chart's one config schema fits both.
    assert contrib == f"{core}-contrib"
    assert PINS["LOG_AGENT_IMAGE_TAG"] == PINS["OTEL_COLLECTOR_IMAGE_TAG"]
    assert re.fullmatch(DIGEST, PINS["LOG_AGENT_IMAGE_DIGEST"])
    assert PINS["LOG_AGENT_IMAGE_DIGEST"] != PINS["OTEL_COLLECTOR_IMAGE_DIGEST"]


def test_the_agents_pin_has_the_one_shape_with_its_renovate_comment_above_the_tag() -> (
    None
):
    lines = PINS_TEXT.splitlines()
    (tag,) = [
        i for i, line in enumerate(lines) if line.startswith("LOG_AGENT_IMAGE_TAG=")
    ]

    assert lines[tag - 2].startswith("LOG_AGENT_IMAGE_REPOSITORY=")
    assert lines[tag - 1] == (
        f"# renovate: datasource=docker depName={PINS['LOG_AGENT_IMAGE_REPOSITORY']}"
    )
    assert lines[tag + 1].startswith("LOG_AGENT_IMAGE_DIGEST=sha256:")


# ── what the pod can reach ───────────────────────────────────────────────────


def test_the_values_make_a_daemonset_named_for_its_release() -> None:
    found = values()

    assert found["mode"] == "daemonset"
    # The chart names a DaemonSet `<fullname>-agent`; smoke looks for this name.
    assert found["fullnameOverride"] == RELEASE
    assert "image" not in found  # the digest comes from pins.env by --set


def test_the_one_host_path_is_the_pod_log_directory_read_only() -> None:
    found = values()

    host_paths = [v for v in found["extraVolumes"] if "hostPath" in v]
    assert host_paths == [
        {"name": "varlogpods", "hostPath": {"path": POD_LOGS, "type": "Directory"}}
    ]
    assert mount("varlogpods") == {
        "name": "varlogpods",
        "mountPath": POD_LOGS,
        "readOnly": True,
    }
    # Not Docker's directory (containerd keeps its files in /var/log/pods) and not
    # a writable host directory for the checkpoint.
    text = VALUES_FILE.read_text(encoding="utf-8")
    assert not re.search(r"^\s*path: /var/(lib|run)/", text, re.M)
    assert "logsCollection" not in found.get("presets", {})


def test_every_other_volume_is_an_empty_dir_or_the_authoritys_config_map() -> None:
    kinds = {
        v["name"]: next(key for key in v if key != "name")
        for v in values()["extraVolumes"]
    }

    assert kinds == {
        "varlogpods": "hostPath",
        "checkpoints": "emptyDir",
        "telemetry-ca": "configMap",
    }
    assert {m["name"] for m in values()["extraVolumeMounts"]} == set(kinds)
    for each in values()["extraVolumeMounts"]:
        assert "subPath" not in each
    assert mount("telemetry-ca")["readOnly"] is True
    assert "readOnly" not in mount("checkpoints")


def test_the_container_runs_as_root_with_nothing_else() -> None:
    found = values()

    assert found["securityContext"] == {
        "runAsUser": 0,
        "runAsGroup": 0,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
        "readOnlyRootFilesystem": True,
    }
    # runAsNonRoot is left out: it would stop a pod that has to read root's files.
    assert "runAsNonRoot" not in found["securityContext"]
    assert found["podSecurityContext"] == {}


def test_the_pod_has_no_host_namespace_no_host_port_and_no_token() -> None:
    found = values()

    for key in ("hostNetwork", "hostPID", "hostIPC", "hostUsers"):
        assert found.get(key, False) is False, key
    assert found["serviceAccount"] == {"automountServiceAccountToken": False}
    assert "presets" not in found
    assert "clusterRole" not in found
    # The chart's ports each get a hostPort in daemonset mode; none is left on.
    assert set(found["ports"]) == {
        "otlp",
        "otlp-http",
        "jaeger-compact",
        "jaeger-thrift",
        "jaeger-grpc",
        "zipkin",
    }
    assert all(port == {"enabled": False} for port in found["ports"].values())
    assert "hostPort" not in keys_of(found)


def test_nothing_in_the_configuration_calls_the_api_server() -> None:
    config = values()["config"]

    named = {
        name.split("/")[0]
        for section in ("receivers", "processors", "exporters", "extensions")
        for name, body in config.get(section, {}).items()
        if body is not None
    }
    pipeline = config["service"]["pipelines"]["logs"]
    named |= {
        name.split("/")[0]
        for kind in ("receivers", "processors", "exporters")
        for name in pipeline[kind]
    }
    # Pod, namespace and container come from the file's path, by the receiver's
    # own `container` operator.
    assert not {n for n in named if n.startswith(("k8s", "resourcedetection"))}
    # (health_check and the batch and memory_limiter settings are the chart's own
    # defaults, merged under these values.)
    assert named == {
        "file_storage",
        "file_log",
        "resource",
        "otlp_http",
        "memory_limiter",
        "batch",
    }


def test_the_pod_has_requests_and_a_memory_limit_and_the_limiter_comes_first() -> None:
    found = values()
    logs = found["config"]["service"]["pipelines"]["logs"]

    assert set(found["resources"]["requests"]) == {"cpu", "memory"}
    assert set(found["resources"]["limits"]) == {"memory"}
    # The chart's memory_limiter takes its share of this limit; a pipeline that
    # does not start with it is not limited.
    assert logs["processors"][0] == "memory_limiter"


def test_the_checkpoint_is_an_empty_dir_with_a_size_limit_the_extension_writes_to() -> (
    None
):
    found = values()
    extension = found["config"]["extensions"]["file_storage"]

    assert volume("checkpoints")["emptyDir"]["sizeLimit"]
    assert mount("checkpoints")["mountPath"] == CHECKPOINTS
    assert extension["directory"] == CHECKPOINTS
    assert receiver()["storage"] == "file_storage"
    assert found["config"]["service"]["extensions"] == ["health_check", "file_storage"]


# ── what it reads and what it sends ──────────────────────────────────────────


def test_the_receiver_includes_the_files_of_meridians_pods_and_not_the_databases() -> (
    None
):
    found = receiver()

    assert found["include"] == [f"{POD_LOGS}/meridian_*/*/*.log"]
    # PostgreSQL's own output is not the services' (no redaction, no JSON) and a
    # statement that failed can be in it.
    assert found["exclude"] == [f"{POD_LOGS}/meridian_platform-db-*/*/*.log"]
    assert found["include_file_path"] is True


def test_files_found_at_start_are_read_from_the_end_and_a_failed_send_retried() -> None:
    found = receiver()

    assert found["start_at"] == "end"
    assert found["retry_on_failure"] == {"enabled": True}


def test_a_line_is_parsed_when_it_is_json_and_sent_as_it_is_when_it_is_not() -> None:
    operators = receiver()["operators"]
    by_id = {operator["id"]: operator for operator in operators}

    assert [operator["type"] for operator in operators] == [
        "container",
        "json_parser",
        "move",
    ]
    json = by_id["json"]
    # A line that does not parse is sent on untouched and quietly.
    assert json["on_error"] == "send_quiet"
    assert json["parse_from"] == "body"
    assert json["severity"] == {"parse_from": "attributes.level"}
    assert json["if"].startswith('body matches "^')
    move = by_id["message-to-body"]
    assert (move["from"], move["to"]) == ("attributes.message", "body")
    # A JSON line without a `message` keeps the whole line as its body.
    assert move["if"] == "attributes.message != nil"


def test_nothing_in_the_pipeline_drops_or_filters_a_record() -> None:
    config = values()["config"]
    operator_types = {operator["type"] for operator in receiver()["operators"]}

    assert operator_types.isdisjoint({"filter", "router", "remove", "retain"})
    assert set(config["processors"]) == {"resource/service"}
    assert config["service"]["pipelines"]["logs"]["processors"] == [
        "memory_limiter",
        "resource/service",
        "batch",
    ]


def test_the_services_name_is_the_containers_name() -> None:
    (action,) = values()["config"]["processors"]["resource/service"]["attributes"]

    assert action == {
        "key": "service.name",
        "from_attribute": "k8s.container.name",
        "action": "upsert",
    }


def test_the_pipelines_of_traces_and_metrics_are_removed_and_logs_is_the_only_one() -> (
    None
):
    pipelines = values()["config"]["service"]["pipelines"]

    assert pipelines["traces"] is None and pipelines["metrics"] is None
    assert pipelines["logs"]["receivers"] == ["file_log"]
    assert pipelines["logs"]["exporters"] == ["otlp_http/collector"]
    receivers = values()["config"]["receivers"]
    assert {name for name, body in receivers.items() if body is not None} == {
        "file_log"
    }


# ── where it sends ───────────────────────────────────────────────────────────


def test_it_sends_over_https_to_the_collector_verified_against_the_authority() -> None:
    exporter = values()["config"]["exporters"]["otlp_http/collector"]
    ca = volume("telemetry-ca")["configMap"]
    (item,) = ca["items"]

    assert exporter["endpoint"] == COLLECTOR_ADDRESS
    assert exporter["tls"]["ca_file"] == (
        f"{mount('telemetry-ca')['mountPath']}/{item['path']}"
    )
    assert exporter["tls"]["min_version"] == "1.3"
    assert "insecure" not in exporter["tls"]
    # The ConfigMap is the one `publish_telemetry_ca` makes, under the key it uses.
    assert ca["name"] == "telemetry-ca"
    assert item["key"] == "ca.crt"
    assert "--from-literal=ca.crt=" in up_function("publish_telemetry_ca")


def test_the_address_is_the_one_the_services_are_told_to_send_to() -> None:
    meridian = yaml.safe_load(
        (KIND_DIR / "values" / "meridian.yaml").read_text(encoding="utf-8")
    )

    assert meridian["telemetry"]["otlpEndpoint"] == COLLECTOR_ADDRESS


def test_the_collector_is_not_changed_for_the_agents_records() -> None:
    collector = yaml.safe_load(COLLECTOR_FILE.read_text(encoding="utf-8"))
    logs = collector["config"]["service"]["pipelines"]["logs"]

    # The collector already takes OTLP logs and writes them to Loki.
    assert logs == {
        "receivers": ["otlp"],
        "processors": ["memory_limiter", "batch"],
        "exporters": ["otlp_http/loki"],
    }


# ── what the header says ─────────────────────────────────────────────────────


def test_the_header_says_what_the_mount_reaches_and_what_remains() -> None:
    text = header()

    assert "EVERY pod on the node" in text
    assert "a compromised agent reads the output of every pod on its node" in text
    assert "root" in text and "mode 0640" in text
    assert "every capability dropped" in text
    assert "`baseline` forbids a hostPath volume" in text
    assert "runAsNonRoot" in text
    assert "not in `observability`" in text
    assert "has not run on one" in text


def test_the_header_says_what_a_restart_resends() -> None:
    text = header()

    assert "survives a restart of the container" in text
    assert "gone when the pod is replaced" in text
    assert "re-sends nothing" in text
    assert "not sent" in text
    assert "lost when it stops" in text


def test_the_header_says_where_the_files_are_on_kind() -> None:
    text = header()

    assert "containerd" in text
    assert "/var/log/containers" in text and "symlinks" in text
