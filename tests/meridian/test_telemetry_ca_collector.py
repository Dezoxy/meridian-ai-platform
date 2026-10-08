"""The collector serves TLS, and presents its client certificate (S063, S072).

The collector's values: its receiver is HTTP with TLS and no gRPC, it mounts the
server certificate and the client certificate read-only in directories of their
own, reads them again so that a renewal needs no restart, and its three exporters
use the client certificate. The manifest's header names who can read and overwrite
the authority's key. The certificates, the policies and the manifest are judged
in ``test_telemetry_ca.py``, which also holds ``collector_values``.
"""

import json

from certpolicysupport import hours
from test_telemetry_ca import (
    COLLECTOR,
    COLLECTOR_CLIENT,
    COLLECTOR_CLIENT_TLS_NAME,
    COLLECTOR_TLS_NAME,
    MANIFEST,
    certificate,
    collector_values,
)

# ── the collector serves TLS ─────────────────────────────────────────────────


def test_the_collector_serves_otlp_over_http_with_tls_and_no_grpc_receiver() -> None:
    protocols = collector_values()["config"]["receivers"]["otlp"]["protocols"]

    # Helm merges a null over the chart's default and removes the key.
    assert protocols["grpc"] is None
    assert protocols["http"]["endpoint"] == "${env:MY_POD_IP}:4318"
    assert set(protocols["http"]["tls"]) == {
        "cert_file",
        "key_file",
        "reload_interval",
        "min_version",  # the floor: TLS 1.3 (test_kind_observability_security_context)
    }


def manifest_header() -> str:
    head = MANIFEST.read_text(encoding="utf-8").split("apiVersion:")[0]
    return " ".join(line.removeprefix("#").strip() for line in head.splitlines())


def test_the_header_names_who_can_read_and_overwrite_the_authoritys_key() -> None:
    header = manifest_header()

    # The readers of the key today are named: cert-manager's controller and
    # cainjector and Prometheus's operator (kube-state-metrics is named as no longer
    # one of them); the two can write the Secret too, so the key is replaceable as
    # well. The CloudNativePG operator is named as NO LONGER one: since S072
    # (contract C) it works through a Role in `meridian`, and the header said it
    # still read the Secret until contract M3b.
    for reader in (
        "cert-manager's controller",
        "cainjector",
        "Prometheus's operator",
        "kube-state-metrics",
    ):
        assert reader in header, reader
    assert "overwrite" in header
    assert "The CloudNativePG operator no longer does either" in header
    assert "cert-manager and Prometheus's operator can also write Secrets" in header
    assert "the CloudNativePG operator, and in the rendered" not in header


def test_the_collector_closes_4317_in_the_service_and_the_container_too() -> None:
    ports = collector_values()["ports"]

    # The chart would otherwise keep a Service port and a container port for a
    # receiver that no longer listens.
    assert ports["otlp"] == {"enabled": False}
    assert "otlp-http" not in ports or ports["otlp-http"].get("enabled", True)


def test_the_collector_mounts_the_certificate_read_only_where_tls_reads_it() -> None:
    values = collector_values()
    tls = values["config"]["receivers"]["otlp"]["protocols"]["http"]["tls"]
    (volume,) = [v for v in values["extraVolumes"] if v["name"] == "tls"]
    (mount,) = [m for m in values["extraVolumeMounts"] if m["name"] == "tls"]

    assert volume["secret"]["secretName"] == COLLECTOR_TLS_NAME
    assert certificate(COLLECTOR)["spec"]["secretName"] == COLLECTOR_TLS_NAME
    assert mount["name"] == volume["name"]
    assert mount["readOnly"] is True
    # A directory, not a subPath: a subPath mount never sees the renewed Secret.
    assert "subPath" not in mount
    assert tls["cert_file"] == f"{mount['mountPath']}/tls.crt"
    assert tls["key_file"] == f"{mount['mountPath']}/tls.key"


def test_the_collector_mounts_both_secrets_read_only_in_directories_of_their_own() -> (
    None
):
    values = collector_values()

    # The client certificate (S072, contract M1): a second volume and mount beside
    # the server certificate's, and nothing else.
    assert [v["name"] for v in values["extraVolumes"]] == ["tls", "client-tls"]
    assert [m["name"] for m in values["extraVolumeMounts"]] == ["tls", "client-tls"]
    volume = {v["name"]: v for v in values["extraVolumes"]}["client-tls"]
    mount = {m["name"]: m for m in values["extraVolumeMounts"]}["client-tls"]
    assert volume["secret"]["secretName"] == COLLECTOR_CLIENT_TLS_NAME
    assert (
        certificate(COLLECTOR_CLIENT)["spec"]["secretName"] == COLLECTOR_CLIENT_TLS_NAME
    )
    assert mount["readOnly"] is True
    assert "subPath" not in mount  # a subPath never sees a renewed Secret
    paths = [m["mountPath"] for m in values["extraVolumeMounts"]]
    assert len(set(paths)) == 2
    assert mount["mountPath"] == "/etc/otel-collector/client-tls"
    assert not any(a.startswith(b + "/") for a in paths for b in paths)


def test_the_three_exporters_of_the_collector_use_the_client_certificate() -> None:
    config = collector_values()["config"]
    exporters = config["exporters"]

    # S072, contracts M2, M3 and M4: the client mount is used by all three
    # exporters, Tempo's, Loki's and Prometheus's, and by nothing else. The
    # receiver's tls block is the four keys it had.
    for used in ("otlp_grpc/tempo", "otlp_http/loki", "otlp_http/prometheus"):
        assert "client-tls" in json.dumps(exporters[used]), used
    assert "client-tls" not in json.dumps(config["receivers"])
    assert "client-tls" not in json.dumps(config["service"])
    assert "client_ca_file" not in json.dumps(config)
    assert "client_ca_file_reload" not in json.dumps(config)
    assert set(config["receivers"]["otlp"]["protocols"]["http"]["tls"]) == {
        "cert_file",
        "key_file",
        "reload_interval",
        "min_version",
    }
    assert [n for n, e in config["exporters"].items() if e and "tls" in e] == [
        "otlp_grpc/tempo",
        "otlp_http/prometheus",
        "otlp_http/loki",
    ]
    assert list(config["exporters"]) == [
        "debug",
        "otlp_grpc/tempo",
        "otlp_http/prometheus",
        "otlp_http/loki",
    ]


def test_the_collectors_key_is_readable_through_the_pods_fs_group_only() -> None:
    values = collector_values()

    # 0440, as the services' Secrets: root's and the pod's group's, so the
    # container's one user (10001) reads it through fsGroup and nobody else does.
    for volume in values["extraVolumes"]:
        assert volume["secret"]["defaultMode"] == 0o440
    assert values["podSecurityContext"] == {"fsGroup": 10001}


def test_the_collector_reads_the_certificate_again_so_a_renewal_needs_no_restart() -> (
    None
):
    tls = collector_values()["config"]["receivers"]["otlp"]["protocols"]["http"]["tls"]

    # configtls re-reads the pair at a handshake once this long has passed since
    # the last read (go.opentelemetry.io/collector/config/configtls v1.68.0, the
    # release of collector 0.162.0): hours(...) fails on a text that is no time.
    assert 0 < hours(tls["reload_interval"]) <= 1


def test_the_collectors_hop_to_prometheus_is_mutual_tls_to_its_gateway() -> None:
    # Split from the test that held all three hops (S072, contract M2: Tempo's is
    # TLS below; contract M3: Loki's is mutual TLS to its gateway, in
    # test_telemetry_loki_gateway.py). This one was clear text, with its string
    # unchanged, until contract M4 (test_telemetry_prometheus_gateway.py judges the
    # exporter: this only says it is no longer the clear-text string).
    exporters = collector_values()["config"]["exporters"]

    assert exporters["otlp_http/prometheus"]["endpoint"].startswith("https://")
    assert ":9090" not in json.dumps(exporters)


def test_the_collectors_hop_to_tempo_is_tls_with_the_client_certificate() -> None:
    exporter = collector_values()["config"]["exporters"]["otlp_grpc/tempo"]
    receiver_tls = collector_values()["config"]["receivers"]["otlp"]["protocols"][
        "http"
    ]["tls"]

    assert exporter["endpoint"] == "tempo.observability.svc.cluster.local:4317"
    # Not insecure, in no form: the key is gone, not set false.
    assert "insecure" not in exporter["tls"]
    assert "insecure_skip_verify" not in exporter["tls"]
    # The authority verifies Tempo (ca.crt of the client Secret: the same
    # authority, written there by cert-manager's CA Issuer), and the pair is the
    # collector's client certificate from the mount of contract M1.
    assert exporter["tls"] == {
        "ca_file": "/etc/otel-collector/client-tls/ca.crt",
        "cert_file": "/etc/otel-collector/client-tls/tls.crt",
        "key_file": "/etc/otel-collector/client-tls/tls.key",
        "reload_interval": receiver_tls["reload_interval"],
        "min_version": "1.3",  # the floor of the collector's own receiver
    }
    assert exporter["tls"]["min_version"] == receiver_tls["min_version"]
    assert 0 < hours(exporter["tls"]["reload_interval"]) <= 1
