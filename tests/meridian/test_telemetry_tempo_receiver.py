"""Tempo's receiver asks the collector for its client certificate (S072, M2).

The collector's hop to Tempo (OTLP over gRPC, port 4317) was clear text. Tempo's
receiver now serves TLS with a certificate of the telemetry authority and takes
that authority as its client CA, so only a sender holding a certificate the
authority signed (the collector's, contract M1) is heard. The certificate is the
fourth leaf of ``infra/kind/manifests/telemetry-ca.yaml``, with a policy of its
own in ``certificate-policy.yaml``; Tempo's values set ONE receiver. No cluster
is needed: the tests read the files and judge the certificate against the
policies with the model in ``certpolicysupport.py``. The names, counts and waits
that the other certificates share are in ``test_telemetry_ca.py``.
"""

from urllib.parse import urlparse

import pytest
import yaml
from certpolicysupport import (
    COLLECTOR_CLIENT_POLICY,
    COLLECTOR_POLICY,
    KIND_DIR,
    TEMPO_RECEIVER_POLICY,
    allows,
    hours,
    request_of,
)
from test_telemetry_ca import (
    AUTHORITY,
    COLLECTOR,
    COLLECTOR_CLIENT,
    COLLECTOR_CLIENT_NAMES,
    NAMESPACE,
    NINETY_DAYS,
    SERVER_USAGES,
    TEMPO,
    TEMPO_NAMES,
    TEMPO_TLS_NAME,
    certificate,
    changed,
    collector_values,
    decision,
    policies,
)

TEMPO_VALUES = KIND_DIR / "values" / "tempo.yaml"
MOUNT_PATH = "/etc/tempo/receiver-tls"


def tempo_values() -> dict:
    return yaml.safe_load(TEMPO_VALUES.read_text(encoding="utf-8"))


def keys_of(node: object) -> set[str]:
    """Every mapping key at any depth."""
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in keys_of(v)}
    if isinstance(node, list):
        return {k for v in node for k in keys_of(v)}
    return set()


# ── the certificate ──────────────────────────────────────────────────────────


def test_the_certificate_is_a_server_certificate_and_never_a_client_one() -> None:
    spec = certificate(TEMPO)["spec"]
    collector = certificate(COLLECTOR)["spec"]

    assert sorted(spec["usages"]) == SERVER_USAGES
    assert "client auth" not in spec["usages"]
    assert spec["secretName"] == TEMPO_TLS_NAME
    # The duration and the key of its neighbours, a new key at every renewal.
    assert spec["duration"] == collector["duration"] == NINETY_DAYS
    assert spec["privateKey"] == collector["privateKey"]
    assert spec["privateKey"]["rotationPolicy"] == "Always"
    for absent in ("uris", "ipAddresses", "emailAddresses", "commonName", "isCA"):
        assert absent not in spec, absent


def test_the_names_are_the_host_the_collector_dials_and_its_short_form() -> None:
    endpoint = collector_values()["config"]["exporters"]["otlp_grpc/tempo"]["endpoint"]
    host = urlparse(f"//{endpoint}").hostname

    # Read from the exporter, so the two cannot drift: the collector verifies the
    # certificate against this host, the way the services verify the collector's.
    assert host == "tempo.observability.svc.cluster.local"
    assert certificate(TEMPO)["spec"]["dnsNames"] == [
        host.removesuffix(".cluster.local"),
        host,
    ]
    assert certificate(TEMPO)["spec"]["dnsNames"] == TEMPO_NAMES


# ── its policy ───────────────────────────────────────────────────────────────


def test_the_policy_allows_the_two_names_two_usages_and_nothing_else() -> None:
    spec = policies()[TEMPO_RECEIVER_POLICY]["spec"]

    assert spec["allowed"] == {
        "dnsNames": {"values": TEMPO_NAMES, "required": True},
        "usages": ["digital signature", "server auth"],
    }
    assert spec["constraints"] == {"maxDuration": NINETY_DAYS}
    assert spec["selector"] == {
        "issuerRef": {"name": AUTHORITY, "kind": "Issuer", "group": "cert-manager.io"},
        "namespace": {"matchNames": [NAMESPACE]},
    }


def test_its_own_request_is_approved_by_its_own_policy_alone() -> None:
    request = request_of(certificate(TEMPO))

    assert decision(request) == "approved"
    permitting = {n for n, p in policies().items() if allows(p, request)}
    assert permitting == {TEMPO_RECEIVER_POLICY}


def test_the_collectors_two_policies_do_not_permit_tempos_names() -> None:
    request = request_of(certificate(TEMPO))

    assert not allows(policies()[COLLECTOR_POLICY], request)
    assert not allows(policies()[COLLECTOR_CLIENT_POLICY], request)


@pytest.mark.parametrize(
    ("what", "change"),
    [
        ("client auth only", {"usages": ["client auth"]}),
        ("client auth instead", {"usages": ["digital signature", "client auth"]}),
        (
            "client auth too",
            {"usages": ["digital signature", "server auth", "client auth"]},
        ),
        ("another name", {"dnsNames": ["tempo.other.svc"]}),
        ("a wildcard name", {"dnsNames": ["*.observability.svc"]}),
        ("one more name", {"dnsNames": [*TEMPO_NAMES, "evil.example"]}),
        ("no name", {"dnsNames": None}),
        ("a URI", {"uris": ["spiffe://meridian.kind/ns/meridian/sa/claims-api"]}),
        ("a CA", {"isCA": True}),
        ("a common name", {"commonName": "tempo"}),
        ("a lifetime over 90 days", {"duration": "2161h"}),
    ],
)
def test_a_request_outside_what_tempos_policy_allows_is_denied(
    what: str, change: dict
) -> None:
    request = request_of(changed(certificate(TEMPO), **change))

    assert decision(request) == "denied", what
    assert not any(allows(p, request) for p in policies().values()), what


@pytest.mark.parametrize("names", [TEMPO_NAMES[:1], TEMPO_NAMES[1:]])
def test_either_of_tempos_names_alone_is_within_its_policy(names: list[str]) -> None:
    # `required: true` asks for at least one of the listed names and no other.
    request = request_of(changed(certificate(TEMPO), dnsNames=names))

    assert decision(request) == "approved"


def test_a_client_auth_request_under_tempos_server_name_is_denied() -> None:
    request = request_of(
        changed(certificate(TEMPO), usages=["digital signature", "client auth"])
    )

    # Tempo's name with a client usage: the telemetry authority signs no client
    # certificate for it, so the receiver's own name cannot be presented as a
    # sender's.
    assert decision(request) == "denied"
    assert not any(allows(p, request) for p in policies().values())


@pytest.mark.parametrize(
    "usages",
    [
        ["digital signature", "server auth"],
        ["digital signature", "client auth", "server auth"],
    ],
)
def test_a_server_auth_request_under_the_collectors_client_name_is_denied(
    usages: list[str],
) -> None:
    request = request_of(changed(certificate(COLLECTOR_CLIENT), usages=usages))

    # The collector's client name with a server usage: it could stand in for a
    # server certificate, so no policy permits it.
    assert request["dnsNames"] == COLLECTOR_CLIENT_NAMES
    assert decision(request) == "denied"
    assert not any(allows(p, request) for p in policies().values())


def test_the_policys_boundaries_are_inclusive() -> None:
    tempo = certificate(TEMPO)
    cap = policies()[TEMPO_RECEIVER_POLICY]["spec"]["constraints"]["maxDuration"]

    assert hours(cap) == hours(tempo["spec"]["duration"])
    assert decision(request_of(changed(tempo, duration="2160h"))) == "approved"
    assert decision(request_of(changed(tempo, duration="2160h1m"))) == "denied"
    assert decision(request_of(changed(tempo, duration=None))) == "never decided"


# ── Tempo's values ───────────────────────────────────────────────────────────


def test_tempo_has_exactly_one_receiver_otlp_grpc_on_4317_with_a_client_ca() -> None:
    receivers = tempo_values()["tempo"]["receivers"]

    assert set(receivers) == {"jaeger", "otlp"}
    assert set(receivers["otlp"]) == {"protocols"}
    protocols = receivers["otlp"]["protocols"]
    assert set(protocols) == {"grpc", "http"}
    # Helm reads a null as removing the chart's default key: OTLP over HTTP is off.
    assert protocols["http"] is None
    assert protocols["grpc"] == {
        "endpoint": "0.0.0.0:4317",
        "tls": {
            "cert_file": f"{MOUNT_PATH}/tls.crt",
            "key_file": f"{MOUNT_PATH}/tls.key",
            "client_ca_file": f"{MOUNT_PATH}/ca.crt",
            "min_version": "1.3",
        },
    }
    # The floor is the collector's own receiver's.
    receiver_tls = collector_values()["config"]["receivers"]["otlp"]["protocols"][
        "http"
    ]["tls"]
    assert protocols["grpc"]["tls"]["min_version"] == receiver_tls["min_version"]


def test_the_jaeger_protocols_are_all_removed_and_none_is_set_in_any_form() -> None:
    jaeger = tempo_values()["tempo"]["receivers"]["jaeger"]

    # The chart's port templates read these four keys unguarded, so the `jaeger`
    # key stays and each protocol under it is a null (removed by Helm).
    assert jaeger == {
        "protocols": {
            "grpc": None,
            "thrift_binary": None,
            "thrift_compact": None,
            "thrift_http": None,
        }
    }


def test_no_receiver_is_insecure_and_no_other_receiver_is_named() -> None:
    receivers = tempo_values()["tempo"]["receivers"]

    assert "insecure" not in keys_of(receivers)
    assert "insecure_skip_verify" not in keys_of(receivers)
    for other in ("zipkin", "opencensus", "kafka"):
        assert other not in receivers


def test_the_secret_is_mounted_read_only_where_the_receiver_reads_it() -> None:
    values = tempo_values()
    (volume,) = values["extraVolumes"]
    (mount,) = values["tempo"]["extraVolumeMounts"]

    assert volume["secret"]["secretName"] == TEMPO_TLS_NAME
    assert certificate(TEMPO)["spec"]["secretName"] == TEMPO_TLS_NAME
    assert mount["name"] == volume["name"]
    assert mount["mountPath"] == MOUNT_PATH
    assert mount["readOnly"] is True
    assert "subPath" not in mount  # a subPath never sees a renewed Secret
    # 0440, read through the pod's fsGroup as the collector's Secrets are.
    assert volume["secret"]["defaultMode"] == 0o440


def test_the_reads_and_the_probes_on_3200_are_not_touched() -> None:
    tempo = tempo_values()["tempo"]

    # The chart's server block (3200) and both probes are the chart's own: no key
    # for them is set here, so the render keeps them as before.
    assert set(tempo) == {
        "retention",
        "reportingEnabled",
        "securityContext",
        "resources",
        "receivers",
        "extraVolumeMounts",
    }
    assert "livenessProbe" not in tempo
    assert "readinessProbe" not in tempo
    assert "server" not in tempo
