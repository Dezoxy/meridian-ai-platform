"""The chart hands the collector's authority to the six services (S063, T-90).

The collector serves TLS with a certificate from an authority of its own, and
``make up`` publishes that authority's public certificate as the ConfigMap
``telemetry-ca`` in the release's namespace. ``telemetry.caConfigMap`` names it:
each of the six services then mounts the ConfigMap's one key, read-only, at a
path of its own (not under its own certificate's directory, whose CA is the
service CA and not this one) and gets the SDK's variable
``OTEL_EXPORTER_OTLP_CERTIFICATE`` pointing at it. The Jobs and the sweep set no
endpoint, so they get neither. The chart refuses an ``https`` endpoint with no
ConfigMap, and a ConfigMap with an ``http`` endpoint. The rendering is
tests/meridian/chartsupport.py's, with kind's values.
"""

from urllib.parse import urlsplit

import pytest
import yaml
from chartsupport import (
    CHART_DIR,
    JOBS,
    RATE_STORE,
    SERVICES,
    VALUES_FILE,
    helm_arguments,
    render,
    rendered_chart,
    run_helm,
    without_rate_store,
)

from meridian.platform.common.telemetry import OTLP_CERTIFICATE_ENV, OTLP_ENDPOINT_ENV
from meridian.workloads.claims_triage.sweep import SERVICE_NAME as SWEEP_SERVICE_NAME

SWEEP = "meridian-sweep"
# The SDK's variable for an export's deadline, in seconds (a float).
OTLP_TIMEOUT_ENV = "OTEL_EXPORTER_OTLP_TIMEOUT"
# The SDK's variable for resource attributes. The sweep sets the instance ID
# alone, to its own service name, so each pass writes the same six series
# (S064, C3); the name itself is not set here (see test_metrics.py).
RESOURCE_ENV = "OTEL_RESOURCE_ATTRIBUTES"
SWEEP_INSTANCE = f"service.instance.id={SWEEP_SERVICE_NAME}"
CONFIGMAP = "telemetry-ca"
CA_DIRECTORY = "/etc/meridian/telemetry-ca"
CA_FILE = f"{CA_DIRECTORY}/ca.crt"
SERVICE_TLS_DIRECTORY = "/etc/meridian/tls"
HTTPS_ENDPOINT = "https://otel-collector.observability.svc.cluster.local:4318"
HTTP_ENDPOINT = "http://otel-collector.observability.svc.cluster.local:4318"
NO_TELEMETRY = [
    "--set-string",
    "telemetry.otlpEndpoint=",
    "--set-string",
    "telemetry.caConfigMap=",
]


def pods_of(documents: list[dict] | tuple[dict, ...]) -> dict[str, dict]:
    """The pod spec of every workload that runs one, by name."""
    return {
        d["metadata"]["name"]: d["spec"]["template"]["spec"]
        for d in documents
        if d["kind"] in ("Deployment", "Job", "CronJob")
        if "template" in d["spec"]
    } | {
        d["metadata"]["name"]: d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        for d in documents
        if d["kind"] == "CronJob"
    }


def only_container(pod: dict) -> dict:
    (container,) = pod["containers"]
    return container


def env_of(container: dict) -> dict[str, str]:
    return {item["name"]: item.get("value", "") for item in container.get("env", [])}


def mount_of_ca(container: dict) -> list[dict]:
    return [
        m for m in container.get("volumeMounts", []) if m["mountPath"] == CA_DIRECTORY
    ]


def kind_values() -> dict:
    return yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))


def failure_of(*extra: str) -> str:
    done = run_helm([*helm_arguments(), *extra])
    assert done.returncode != 0, "the chart rendered"
    return done.stderr


# ── the values ───────────────────────────────────────────────────────────────


def test_the_chart_renders_no_authority_by_default() -> None:
    chart = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))

    assert chart["telemetry"]["caConfigMap"] == ""
    assert chart["telemetry"]["otlpEndpoint"] == ""


def test_kinds_endpoint_is_https_on_the_collectors_http_port() -> None:
    telemetry = kind_values()["telemetry"]
    url = urlsplit(telemetry["otlpEndpoint"])

    assert url.scheme == "https"
    assert url.hostname == "otel-collector.observability.svc.cluster.local"
    assert url.port == 4318
    assert telemetry["caConfigMap"] == CONFIGMAP


# ── the six services ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", SERVICES)
def test_a_service_mounts_the_collectors_authority_read_only(name: str) -> None:
    pod = pods_of(rendered_chart())[name]
    container = only_container(pod)
    volumes = [v for v in pod["volumes"] if v.get("configMap")]

    assert mount_of_ca(container) == [
        {"name": "telemetry-ca", "mountPath": CA_DIRECTORY, "readOnly": True}
    ]
    assert volumes == [
        {
            "name": "telemetry-ca",
            "configMap": {
                "name": CONFIGMAP,
                "items": [{"key": "ca.crt", "path": "ca.crt"}],
            },
        }
    ]


@pytest.mark.parametrize("name", SERVICES)
def test_a_service_trusts_that_file_through_the_sdks_own_variable(name: str) -> None:
    env = env_of(only_container(pods_of(rendered_chart())[name]))

    assert env[OTLP_CERTIFICATE_ENV] == CA_FILE
    assert env[OTLP_ENDPOINT_ENV].startswith("https://")
    assert env["MERIDIAN_TLS_CA_FILE"] == f"{SERVICE_TLS_DIRECTORY}/ca.crt"


@pytest.mark.parametrize("name", SERVICES)
def test_the_authority_is_not_under_the_services_own_certificate_directory(
    name: str,
) -> None:
    container = only_container(pods_of(rendered_chart())[name])
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}

    assert not mounts["telemetry-ca"].startswith(SERVICE_TLS_DIRECTORY)
    assert not SERVICE_TLS_DIRECTORY.startswith(mounts["telemetry-ca"])
    assert mounts["tls"] == SERVICE_TLS_DIRECTORY


def test_no_job_gets_the_authority_or_the_variable() -> None:
    pods = pods_of(without_rate_store(rendered_chart()))
    jobs = {n: p for n, p in pods.items() if n not in (*SERVICES, SWEEP)}

    assert len(jobs) == len(JOBS)  # the three Jobs: they send nothing
    for name, pod in jobs.items():
        container = only_container(pod)
        assert mount_of_ca(container) == [], name
        assert OTLP_CERTIFICATE_ENV not in env_of(container), name
        assert OTLP_ENDPOINT_ENV not in env_of(container), name
        assert OTLP_TIMEOUT_ENV not in env_of(container), name
        assert RESOURCE_ENV not in env_of(container), name
        assert not [v for v in pod["volumes"] if v.get("configMap")], name


def test_the_rate_store_sends_no_telemetry_and_mounts_no_authority() -> None:
    # Kind's values turn the store on (S066). It is Redis: no exporter, no
    # address of the collector, no authority to trust; its one ConfigMap is its
    # own configuration, and the NetworkPolicy gives it no egress at all.
    pod = pods_of(rendered_chart())[RATE_STORE]
    container = only_container(pod)

    assert mount_of_ca(container) == []
    assert OTLP_CERTIFICATE_ENV not in env_of(container)
    assert OTLP_ENDPOINT_ENV not in env_of(container)
    assert [v["configMap"]["name"] for v in pod["volumes"] if v.get("configMap")] == [
        RATE_STORE
    ]


# ── the sweep (S064) ─────────────────────────────────────────────────────────


def sweep_pod(documents: list[dict] | tuple[dict, ...] | None = None) -> dict:
    return pods_of(rendered_chart() if documents is None else documents)[SWEEP]


def sweep_cronjob(documents: list[dict] | tuple[dict, ...]) -> dict:
    (cronjob,) = [d for d in documents if d["kind"] == "CronJob"]
    return cronjob


def test_the_sweep_gets_the_services_own_address_and_authority_file() -> None:
    documents = rendered_chart()
    sweep = env_of(only_container(sweep_pod()))
    service = env_of(only_container(pods_of(documents)["claims-api"]))

    assert sweep[OTLP_ENDPOINT_ENV] == service[OTLP_ENDPOINT_ENV]
    assert sweep[OTLP_ENDPOINT_ENV] == kind_values()["telemetry"]["otlpEndpoint"]
    assert sweep[OTLP_CERTIFICATE_ENV] == service[OTLP_CERTIFICATE_ENV] == CA_FILE


def test_the_sweep_mounts_the_collectors_authority_read_only_as_a_service_does() -> (
    None
):
    pod = sweep_pod()
    service = pods_of(rendered_chart())["claims-api"]
    configmaps = [v for v in pod["volumes"] if v.get("configMap")]

    assert mount_of_ca(only_container(pod)) == [
        {"name": "telemetry-ca", "mountPath": CA_DIRECTORY, "readOnly": True}
    ]
    assert mount_of_ca(only_container(pod)) == mount_of_ca(only_container(service))
    assert configmaps == [
        {
            "name": "telemetry-ca",
            "configMap": {
                "name": CONFIGMAP,
                "items": [{"key": "ca.crt", "path": "ca.crt"}],
            },
        }
    ]


def test_the_sweep_bounds_its_send_and_the_bound_is_under_its_deadline() -> None:
    cronjob = sweep_cronjob(rendered_chart())
    deadline = cronjob["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"]
    timeout = env_of(only_container(sweep_pod()))[OTLP_TIMEOUT_ENV]

    # The SDK reads this variable in seconds (a float), not in milliseconds.
    assert timeout == "5"
    assert 0 < float(timeout) < deadline


def test_no_service_gets_the_sweeps_timeout() -> None:
    pods = pods_of(rendered_chart())

    for name in SERVICES:
        assert OTLP_TIMEOUT_ENV not in env_of(only_container(pods[name])), name


def test_the_sweep_has_one_fixed_instance_id_so_each_pass_writes_the_same_series() -> (
    None
):
    environment = env_of(only_container(sweep_pod()))

    assert SWEEP_SERVICE_NAME == "claims-sweep"
    assert environment[RESOURCE_ENV] == "service.instance.id=claims-sweep"
    assert environment[RESOURCE_ENV] == SWEEP_INSTANCE
    # The instance ID alone: the service's name is the code's (``job``).
    assert "service.name" not in environment[RESOURCE_ENV]


def test_no_service_gets_the_sweeps_instance_id() -> None:
    # Six services share a name each with its own replicas: a fixed instance ID
    # would make two replicas write one series.
    pods = pods_of(rendered_chart())

    for name in SERVICES:
        assert RESOURCE_ENV not in env_of(only_container(pods[name])), name


def test_the_sweep_without_an_endpoint_and_a_name_gets_none_of_the_five() -> None:
    documents = render([*helm_arguments(), *NO_TELEMETRY])
    container = only_container(sweep_pod(documents))

    assert mount_of_ca(container) == []
    assert OTLP_CERTIFICATE_ENV not in env_of(container)
    assert OTLP_ENDPOINT_ENV not in env_of(container)
    assert OTLP_TIMEOUT_ENV not in env_of(container)
    assert RESOURCE_ENV not in env_of(container)
    assert not [v for v in sweep_pod(documents)["volumes"] if v.get("configMap")]


def test_the_sweep_on_http_gets_the_address_the_bound_and_the_id() -> None:
    arguments = [
        "--set-string",
        f"telemetry.otlpEndpoint={HTTP_ENDPOINT}",
        "--set-string",
        "telemetry.caConfigMap=",
    ]

    container = only_container(sweep_pod(render([*helm_arguments(), *arguments])))

    assert env_of(container)[OTLP_ENDPOINT_ENV] == HTTP_ENDPOINT
    assert env_of(container)[OTLP_TIMEOUT_ENV] == "5"
    assert env_of(container)[RESOURCE_ENV] == SWEEP_INSTANCE
    assert OTLP_CERTIFICATE_ENV not in env_of(container)
    assert mount_of_ca(container) == []


def test_the_timeout_follows_its_value_and_may_not_reach_the_deadline() -> None:
    arguments = [*helm_arguments(), "--set", "sweep.telemetryTimeoutSeconds=2.5"]
    container = only_container(sweep_pod(render(arguments)))

    assert env_of(container)[OTLP_TIMEOUT_ENV] == "2.5"
    for bad in ("0", "-1", "120", "500"):
        message = failure_of("--set", f"sweep.telemetryTimeoutSeconds={bad}")
        assert "sweep.telemetryTimeoutSeconds" in message, bad


def test_the_services_keep_the_order_of_their_variables_mounts_and_volumes() -> None:
    # What the services render, in the order they render it: the two variables
    # after the service's own and before the identity's, the mount and the
    # volume last. The byte-for-byte comparison with the chart before the
    # helpers is in the report of S064's contract C2.
    pods = pods_of(rendered_chart())
    pair = [OTLP_ENDPOINT_ENV, OTLP_CERTIFICATE_ENV]

    for name in SERVICES:
        container = only_container(pods[name])
        names = [item["name"] for item in container["env"]]
        first = names.index(OTLP_ENDPOINT_ENV)
        assert names[first : first + 2] == pair, name
        assert container["volumeMounts"][-1]["name"] == "telemetry-ca", name
        assert pods[name]["volumes"][-1]["name"] == "telemetry-ca", name


# ── nothing when the name is empty ───────────────────────────────────────────


def test_without_an_endpoint_and_a_name_nothing_of_it_is_rendered() -> None:
    # The store's ConfigMap is its own configuration, not the authority.
    pods = pods_of(without_rate_store(render([*helm_arguments(), *NO_TELEMETRY])))

    for name, pod in pods.items():
        container = only_container(pod)
        assert mount_of_ca(container) == [], name
        assert OTLP_CERTIFICATE_ENV not in env_of(container), name
        assert OTLP_ENDPOINT_ENV not in env_of(container), name
        assert not [v for v in pod["volumes"] if v.get("configMap")], name


def test_an_http_endpoint_without_a_name_renders_as_it_did() -> None:
    arguments = [
        "--set-string",
        f"telemetry.otlpEndpoint={HTTP_ENDPOINT}",
        "--set-string",
        "telemetry.caConfigMap=",
    ]

    pods = pods_of(render([*helm_arguments(), *arguments]))

    for name in SERVICES:
        container = only_container(pods[name])
        assert env_of(container)[OTLP_ENDPOINT_ENV] == HTTP_ENDPOINT
        assert OTLP_CERTIFICATE_ENV not in env_of(container), name
        assert mount_of_ca(container) == [], name


# ── the two refusals ─────────────────────────────────────────────────────────


def test_the_chart_refuses_an_https_endpoint_without_the_configmap() -> None:
    message = failure_of("--set-string", "telemetry.caConfigMap=")

    assert "telemetry.caConfigMap" in message
    assert "https" in message


def test_the_chart_refuses_the_configmap_with_an_http_endpoint() -> None:
    message = failure_of("--set-string", f"telemetry.otlpEndpoint={HTTP_ENDPOINT}")

    assert "telemetry.caConfigMap" in message
    assert "http" in message


def test_a_scheme_in_capitals_is_still_https_to_the_chart() -> None:
    capitals = HTTPS_ENDPOINT.replace("https", "HTTPS")
    arguments = [
        *helm_arguments(),
        "--set-string",
        f"telemetry.otlpEndpoint={capitals}",
    ]

    done = run_helm(arguments)

    assert done.returncode == 0, done.stderr


def test_the_configmaps_name_is_checked_as_a_name() -> None:
    message = failure_of("--set-string", "telemetry.caConfigMap=Not A Name")

    assert "telemetry.caConfigMap" in message
