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
    SERVICES,
    VALUES_FILE,
    helm_arguments,
    render,
    rendered_chart,
    run_helm,
)

from meridian.platform.common.telemetry import OTLP_CERTIFICATE_ENV, OTLP_ENDPOINT_ENV

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


def test_no_job_and_no_cronjob_gets_the_authority_or_the_variable() -> None:
    pods = pods_of(rendered_chart())
    others = {n: p for n, p in pods.items() if n not in SERVICES}

    assert len(others) == len(JOBS) + 1  # the three Jobs and the sweep
    for name, pod in others.items():
        container = only_container(pod)
        assert mount_of_ca(container) == [], name
        assert OTLP_CERTIFICATE_ENV not in env_of(container), name
        assert not [v for v in pod["volumes"] if v.get("configMap")], name


# ── nothing when the name is empty ───────────────────────────────────────────


def test_without_an_endpoint_and_a_name_nothing_of_it_is_rendered() -> None:
    pods = pods_of(render([*helm_arguments(), *NO_TELEMETRY]))

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
