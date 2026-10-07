"""The collector's and Tempo's containers meet `restricted` (S063, contract FB).

``observability`` warns and audits at `restricted`: the two pods that stopped it
(``otel-collector`` and ``tempo``, found by the server-side dry run of wave 1a)
now carry, by their charts' own values, the fields that level asks for. The
charts are not vendored, so no test renders them; the fields are read from the
values files as they are committed, and the rendered result was read with
``helm template`` at the pins on 2026-10-06 (the report of the change says what
it showed). Both images run as user 10001 and say so themselves: the collector's
Dockerfile (``USER 10001:10001``, ``FROM scratch``), Tempo's (``USER 10001:10001``
on distroless static, ``/var/tempo`` the only directory made writable), and the
charts' values name the same user.
"""

from pathlib import Path

import yaml
from kindsupport import KIND_DIR

VALUES = KIND_DIR / "values"
COLLECTOR_FILE = VALUES / "otel-collector.yaml"
TEMPO_FILE = VALUES / "tempo.yaml"
LOKI_FILE = VALUES / "loki.yaml"
USER = 10001
# What `restricted` asks of a container (runAsNonRoot, allowPrivilegeEscalation
# false, drop ALL, a seccomp profile), the user the image already runs as, and a
# root filesystem that cannot be written (both pods write only to their volumes).
RESTRICTED_CONTAINER = {
    "runAsNonRoot": True,
    "runAsUser": USER,
    "runAsGroup": USER,
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "seccompProfile": {"type": "RuntimeDefault"},
    "readOnlyRootFilesystem": True,
}


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_collectors_container_meets_restricted_as_user_10001() -> None:
    values = load(COLLECTOR_FILE)

    assert values["securityContext"] == RESTRICTED_CONTAINER


def test_the_collectors_user_is_the_group_its_tls_files_are_read_through() -> None:
    values = load(COLLECTOR_FILE)

    # The Secret's files are mode 0440 and group-readable through fsGroup; the
    # user the container runs as is the one that group was chosen for.
    assert values["podSecurityContext"] == {"fsGroup": USER}
    assert values["securityContext"]["runAsUser"] == USER
    assert values["extraVolumes"][0]["secret"]["defaultMode"] == 0o440


def test_the_collector_mounts_no_service_account_token() -> None:
    values = load(COLLECTOR_FILE)

    assert values["serviceAccount"]["automountServiceAccountToken"] is False


def test_nothing_in_the_collectors_configuration_calls_the_api_server() -> None:
    values = load(COLLECTOR_FILE)
    config = values["config"]

    # The token is off because no component needs it. k8sattributes, the
    # k8s_cluster and k8sobjects receivers and the resource detectors all call
    # the API server; none is configured, and no preset that adds one is on.
    assert "presets" not in values
    named = {
        name.split("/")[0]
        for section in ("receivers", "processors", "exporters", "extensions")
        for name, body in config.get(section, {}).items()
        if body is not None
    }
    pipelines = config["service"]["pipelines"]
    named |= {
        name.split("/")[0]
        for pipeline in pipelines.values()
        for kind in ("receivers", "processors", "exporters")
        for name in pipeline.get(kind, [])
    }
    assert not {n for n in named if n.startswith(("k8s", "resourcedetection"))}
    assert named == {
        "otlp",
        "otlp_grpc",
        "otlp_http",
        "memory_limiter",
        "batch",
    }


def test_the_collectors_tls_listener_accepts_tls_1_3_and_nothing_older() -> None:
    values = load(COLLECTOR_FILE)

    tls = values["config"]["receivers"]["otlp"]["protocols"]["http"]["tls"]

    assert tls["min_version"] == "1.3"
    assert tls["cert_file"] == "/etc/otel-collector/tls/tls.crt"
    assert tls["key_file"] == "/etc/otel-collector/tls/tls.key"


def test_tempos_container_meets_restricted_as_user_10001() -> None:
    values = load(TEMPO_FILE)

    # The chart's `tempo.securityContext` is the container's; its own top-level
    # `securityContext` (the pod's: user 10001, runAsNonRoot, fsGroup) is left at
    # the chart's default and is not repeated here.
    assert values["tempo"]["securityContext"] == RESTRICTED_CONTAINER


def test_tempo_mounts_no_service_account_token() -> None:
    values = load(TEMPO_FILE)

    # One key drives both the ServiceAccount and the pod in the chart.
    assert values["serviceAccount"]["automountServiceAccountToken"] is False


def test_loki_mounts_no_service_account_token_in_the_pod_or_the_service_account() -> (
    None
):
    values = load(LOKI_FILE)

    # Loki's chart has two keys: `defaults` flips the pod, `serviceAccount`
    # flips the ServiceAccount; either one alone leaves the other mounted.
    assert values["defaults"]["automountServiceAccountToken"] is False
    assert values["serviceAccount"]["automountServiceAccountToken"] is False


def test_tempo_still_writes_only_under_the_volume_it_is_given() -> None:
    values = load(TEMPO_FILE)

    # A read-only root filesystem holds because the traces and the WAL stay at
    # the chart's own paths under /var/tempo, the volume the chart mounts: the
    # values override neither the storage paths nor the configuration.
    assert values["persistence"]["enabled"] is True
    assert "config" not in values
    assert "storage" not in values["tempo"]


def test_the_values_no_longer_say_the_pod_sets_no_security_context() -> None:
    text = COLLECTOR_FILE.read_text(encoding="utf-8")

    assert "sets no security context" not in text
