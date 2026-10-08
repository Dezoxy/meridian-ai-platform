"""The log agent: a node agent ships the services' output to Loki (S064, C1).

``infra/kind/values/log-agent.yaml`` is a second release of the OpenTelemetry
Collector chart that ``pins.env`` already pins, the contrib build, as a DaemonSet
in the namespace ``logging``. It reads the files of `meridian`'s pods under
``/var/log/pods`` and sends each line over OTLP with TLS to the collector (since
G1 only the services' and the sweep's, as user 10001 with the group root; what
the pipeline keeps of a file and a line is in ``test_log_agent_selection.py``).

The chart is not vendored, so no test renders it (the neighbours,
``test_kind_observability_security_context.py`` and
``test_kind_platform_images.py``, do not either, and CI has no reason to reach a
Helm repository): the values are read as committed, and the render was read with
``helm template`` of chart 0.174.0 with these values and the ``--set`` arguments
``up.sh`` passes (the report of the change says what it showed: the DaemonSet
``log-agent-agent``, one hostPath, no hostPort, no host namespace, the token off).
What the receiver makes of a line was read by running the pinned contrib image
over a fixture; that is a manual check and has no test here.

Not shown without a cluster: that user 10001 with the group root reads the node's
log files with no capability (shown on the pinned image over a fixture tree with
the node's modes, not on the node), that the exporter's TLS verifies against the
authority, and that a line arrives in Loki (``make smoke`` has a line for the
last, and one for the pod's shape). The first form of the agent, as root, ran on
the kind cluster twice on 2026-10-06.
"""

import re
from fnmatch import fnmatchcase

import yaml
from chartsupport import rendered_chart, without_rate_store
from kindsupport import KIND_DIR, UP_SH
from test_certificate_policy_up import (
    line_containing,
    line_index,
    script_lines,
    up_function,
)
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


POD_UID = "8f5b1c2e-0000-4000-8000-000000000001"
LISTED_FILES = "listed-files-only"


def chart_pod_families() -> list[str]:
    """The names the chart's long-lived pods start with: its Deployments (the six
    services) and its CronJobs (the sweep). The Jobs are not here: they run the
    CLI, which prints, and does not log. Nor is the rate store (S066), a Deployment
    too: it is Redis, whose own log lines went through neither the JSON format nor
    the redaction, so the agent does not open them (the test of the pods not
    opened below names it)."""
    return sorted(
        found["metadata"]["name"]
        for found in without_rate_store(rendered_chart())
        if found["kind"] in ("Deployment", "CronJob")
    )


def chart_job_names() -> list[str]:
    return sorted(
        found["metadata"]["name"]
        for found in rendered_chart()
        if found["kind"] == "Job"
    )


def pod_directory(family: str) -> str:
    """A pod directory under /var/log/pods, as kubelet names it."""
    return f"meridian_{family}-7d9f8-abcde_{POD_UID}"


def included(directory: str) -> bool:
    """Whether one of the receiver's include patterns opens this pod directory
    (the fourth component of the path: a `*` of a glob does not cross a `/`)."""
    return any(
        fnmatchcase(directory, pattern.split("/")[4])
        for pattern in receiver()["include"]
    )


def operators() -> list[dict]:
    return receiver()["operators"]


def operator(identifier: str) -> dict:
    (found,) = [each for each in operators() if each["id"] == identifier]
    return found


def listed_files_expression() -> str:
    """The filter's expression as one line, with the YAML's folding undone."""
    return " ".join(operator(LISTED_FILES)["expr"].split())


def listed_files_regex() -> re.Pattern[str]:
    """The regular expression inside the filter's expression, as Python reads it:
    the expression language reads `\\\\.` in its string as `\\.`."""
    (literal,) = re.findall(r'"(\^[^"]*)"', listed_files_expression())
    return re.compile(literal.replace("\\\\", "\\"))


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
    # `recursiveReadOnly` makes a mount made under /var/log/pods read-only too (a
    # plain read-only bind leaves a submount writable); Enabled, not IfPossible,
    # so a node that cannot do it refuses the pod loudly.
    assert mount("varlogpods") == {
        "name": "varlogpods",
        "mountPath": POD_LOGS,
        "readOnly": True,
        "recursiveReadOnly": "Enabled",
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


def test_the_container_runs_as_the_images_user_and_reads_the_nodes_files_by_group() -> (
    None
):
    found = values()

    # The files are root:root 0640 in a root:root 0750 directory (measured on the
    # node, 2026-10-06): the group reads them, so no uid 0 and no capability.
    assert found["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
        "readOnlyRootFilesystem": True,
    }
    # gid 0 is held as a supplementary group, to read and nothing else (fsGroup
    # does not apply to a hostPath); the primary group is the image's own, set
    # explicitly because runAsUser alone leaves gid 0 where the image has no
    # passwd entry, which would make the checkpoint group-root.
    assert found["podSecurityContext"] == {"supplementalGroups": [0]}


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
        "transform",
        "resource",
        "otlp_http",
        "memory_limiter",
        "batch",
    }


def test_the_pod_has_requests_and_a_memory_limit_and_the_limiter_comes_first() -> None:
    found = values()
    logs = found["config"]["service"]["pipelines"]["logs"]

    assert set(found["resources"]["requests"]) == {"cpu", "memory"}
    # Both limits since S073 (the next tests say why and how much).
    assert set(found["resources"]["limits"]) == {"cpu", "memory"}
    # The chart's memory_limiter takes its share of this limit; a pipeline that
    # does not start with it is not limited.
    assert logs["processors"][0] == "memory_limiter"


def mebibytes(quantity: str) -> int:
    """A Kubernetes memory quantity written in Mi, as a number of MiB."""
    assert quantity.endswith("Mi"), quantity
    return int(quantity.removesuffix("Mi"))


def millicores(quantity: str) -> int:
    assert quantity.endswith("m"), quantity
    return int(quantity.removesuffix("m"))


def resources_comment() -> str:
    """The comment block that sits directly above `resources:`, as one line of
    words: where the values file says why the limits are what they are."""
    lines = VALUES_FILE.read_text(encoding="utf-8").splitlines()
    end = lines.index("resources:")
    start = end
    while start > 0 and lines[start - 1].startswith("#"):
        start -= 1
    return " ".join(line.removeprefix("#").strip() for line in lines[start:end])


# The size at which the pinned image, run under a memory limit, kept the pages of
# its executable in the page cache through a burst of lines with the collector
# down (S073, 2026-10-08): 320 MiB held and 256 MiB did not. 192 MiB, the size
# before, spun on any burst.
MEMORY_THAT_SPUN_WITH_THE_COLLECTOR_DOWN = 256
MEMORY_THAT_HELD_WITH_THE_COLLECTOR_DOWN = 320


def test_the_memory_limit_is_above_the_sizes_the_agent_spun_at() -> None:
    limits = values()["resources"]["limits"]

    # 192Mi was the limit of the form that spun (a core of CPU and 2 GB/s of
    # reads, read from the pod's control group on the kind cluster). A value at or
    # below the size that spun with the collector down brings it back; the value
    # chosen is above the size that held, with the margin the comment names.
    assert mebibytes(limits["memory"]) > MEMORY_THAT_HELD_WITH_THE_COLLECTOR_DOWN
    assert mebibytes(limits["memory"]) > MEMORY_THAT_SPUN_WITH_THE_COLLECTOR_DOWN
    assert limits["memory"] == "384Mi"
    # The request is what the idle agent needs, and the limit is not it: a limit
    # below the request would be refused by the API server.
    assert mebibytes(limits["memory"]) >= mebibytes(
        values()["resources"]["requests"]["memory"]
    )


def test_the_cpu_limit_is_a_ceiling_well_above_what_the_calm_agent_uses() -> None:
    resources = values()["resources"]

    # The idle agent used 0.01 cores and a burst of 140,000 lines averaged 0.11
    # over 20 s in the rig; 500m is a ceiling for a fault, not a budget. A limit
    # at the request (25m) would throttle every burst, one at a core and more
    # would not bound the spin that was seen (1.14 to 1.33 cores).
    assert resources["limits"]["cpu"] == "500m"
    assert millicores(resources["limits"]["cpu"]) >= 10 * millicores(
        resources["requests"]["cpu"]
    )
    assert millicores(resources["limits"]["cpu"]) < 1000


def test_the_values_say_why_the_limits_are_what_they_are() -> None:
    text = resources_comment()

    assert "(S073)" in text and "192Mi" in text and "384Mi" in text
    assert "the page cache" in text and "evicted and read again" in text
    # What did not matter, so nobody removes a setting for it.
    assert "a file removed" in text and "no memory_limiter" in text
    # A CPU limit throttles and does not end the fault.
    assert "does not END one" in text and "still read 1.7 GB/s" in text
    # And what the number does not cure, so it is not read as a cure.
    assert "140,000 lines" in text and "not a cure for everything" in text
    # The Prometheus series did not show it.
    assert "0.001 cores" in text


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


def test_the_receiver_includes_one_pattern_for_each_pod_family_of_the_chart() -> None:
    found = receiver()

    # The services' own output went through the JSON format and the redaction
    # (src/meridian/platform/common/logformat.py, logredaction.py); the Jobs' (the
    # CLI prints) and the database's did not. So the list is positive: a workload
    # the chart gains later is not shipped until someone adds it here, and this
    # test fails first (a seventh service, a second CronJob).
    assert sorted(found["include"]) == sorted(
        f"{POD_LOGS}/meridian_{family}-*/*/*.log" for family in chart_pod_families()
    )
    assert "exclude" not in found
    assert found["include_file_path"] is True


def test_the_families_are_the_six_services_and_the_sweep() -> None:
    # So the test above cannot pass on an empty list because the render found
    # nothing: it names what the render must hold.
    assert len(chart_pod_families()) == 7
    assert "meridian-sweep" in chart_pod_families()
    assert "claims-api" in chart_pod_families()


def test_a_pod_of_the_chart_is_opened_by_exactly_one_pattern() -> None:
    for family in chart_pod_families():
        matching = [
            pattern
            for pattern in receiver()["include"]
            if fnmatchcase(pod_directory(family), pattern.split("/")[4])
        ]

        assert len(matching) == 1, (family, matching)


def test_the_jobs_the_database_and_smokes_pods_are_not_opened() -> None:
    jobs = chart_job_names()

    assert {"meridian-migrate", "meridian-seed", "meridian-ingest"} <= {
        job.rsplit("-", 1)[0] for job in jobs
    }
    for family in (
        *jobs,
        "platform-db-1",
        "rate-store",
        "probe-x7k2",
        "smoke-logs-1700000000",
        "claims-apiary",
        "meridian",
    ):
        assert not included(pod_directory(family)), family


def test_files_found_at_start_are_read_from_the_end_and_a_failed_send_retried() -> None:
    found = receiver()

    assert found["start_at"] == "end"
    assert found["retry_on_failure"] == {"enabled": True}


def test_a_line_is_parsed_when_it_is_json_and_sent_as_it_is_when_it_is_not() -> None:
    by_id = {each["id"]: each for each in operators()}

    assert [each["type"] for each in operators()] == [
        "filter",
        "container",
        "move",
        "json_parser",
        "move",
    ]
    json = by_id["json"]
    # A line that does not parse is sent on untouched and quietly.
    assert json["on_error"] == "send_quiet"
    assert json["parse_from"] == "body"
    assert json["severity"]["parse_from"] == "attributes.level"
    assert json["if"].startswith('body matches "^')
    move = by_id["message-to-body"]
    assert (move["from"], move["to"]) == ("attributes.message", "body")
    # A JSON line without a `message` keeps the whole line as its body.
    assert move["if"] == "attributes.message != nil"


def test_the_one_thing_the_pipeline_drops_is_a_record_from_an_unlisted_file() -> None:
    config = values()["config"]
    types = [each["type"] for each in operators()]

    assert types.count("filter") == 1 and operators()[0]["id"] == LISTED_FILES
    assert {"router", "remove", "retain"}.isdisjoint(types)
    assert set(config["processors"]) == {"transform/line", "resource/service"}
    assert config["service"]["pipelines"]["logs"]["processors"] == [
        "memory_limiter",
        "transform/line",
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
    assert "every capability dropped" in text
    assert "`baseline` forbids a hostPath volume" in text
    assert "not in `observability`" in text
    # The include list is patterns, not a pod's identity (the review's L2).
    assert "path patterns, not the identity of a pod" in text


def test_the_header_says_the_user_is_not_root_and_how_it_reads_the_files() -> None:
    text = header()

    assert "10001" in text and "supplementary group" in text
    assert (
        "drwxr-x--- root:root (0750)" in text and "-rw-r----- root:root (0640)" in text
    )
    assert "The GROUP reads them" in text
    assert "supplementalGroups" in text and "fsGroup does not apply" in text
    # What the first form said, which was false once the node was looked at.
    assert "runs as root" not in text and "cannot meet" not in text
    # A wrong group is silent, so the header says what notices it.
    assert "fails silently" in text and "smoke's line" in text
    # `restricted` is stopped by the hostPath alone now.
    assert "the hostPath is the only thing that stops it" in text


def test_the_header_says_what_ran_on_a_cluster_and_what_did_not() -> None:
    text = header()

    assert "has not run on one" not in text
    assert "ran on the kind cluster twice on 2026-10-06" in text
    assert "11:10 UTC" in text and "11:34 UTC" in text
    assert "no permission or TLS error" in text and "41 of 41" in text
    # The third run, in the present form (the user, the list), was seen.
    assert "ran a third time (12:56 to 13:06 UTC, 44 of 44)" in text
    assert "uid and gid 10001" in text and "14 files watched" in text
    assert "Not seen on a cluster: a restart of the agent, a renewal of the" in text
    assert "authority, a flood of lines" in text
    assert "tested without a cluster" in text


def test_the_header_says_what_is_shipped_and_where_the_rest_stays() -> None:
    text = header()

    assert "six services of the Meridian chart and the sweep" in text
    assert "(migrate, seed, ingest)" in text and "kubectl logs" in text
    assert "a positive one" in text
    # The sentence the review found false: the health check and the metrics bind.
    assert "nothing listens" not in text
    assert "13133" in text and "8888" in text
    assert "Only `service_name` and `k8s_*` are the file's" in text


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
