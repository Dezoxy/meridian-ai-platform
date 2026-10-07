"""Loki takes writes only through a gateway that asks for the client certificate
(S072, contracts M3 and M3b).

Loki's push path and its read path were one clear-text port that the collector and
Grafana could both call. The chart's own nginx gateway now stands in front of it
(``infra/kind/values/loki.yaml``): ONE TLS 1.3 listener on which a client
certificate is optional at the handshake. A request is a READ (no certificate
needed) only when its path is on an anchored list; a WRITE (the three push paths)
needs a verified certificate whose subject is exactly the collector's; everything
else, the administrator's paths and every path nobody listed, answers 403 whatever
the certificate. Loki's own port takes connections from the gateway alone. No
cluster and no nginx run here: the tests read the files, and the ``map`` blocks
that decide a request are evaluated with nginx's rules (an exact string beats a
regular expression, the regular expressions go in order, then the default), over
the raw request URI and over the form nginx normalises it to (percent-decoding,
merged slashes, dot segments), which are the two views the gateway compares. What
nginx did with the rendered configuration was seen in a container of the pinned
image; those commands and outputs are in the contract's report, not here. The
certificate and its policy are judged with the model in ``certpolicysupport.py``.
"""

import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
import yaml
from certpolicysupport import (
    COLLECTOR_CLIENT_POLICY,
    COLLECTOR_POLICY,
    KIND_DIR,
    LOKI_GATEWAY_POLICY,
    TEMPO_RECEIVER_POLICY,
    allows,
    hours,
    request_of,
)
from test_kind_namespace_policies import (
    GRAFANA,
    LOKI,
    LOKI_GATEWAY,
    collector_labels,
    observability_policies,
    pods,
    rules,
    stack_values,
)
from test_telemetry_ca import (
    AUTHORITY,
    COLLECTOR_CLIENT,
    COLLECTOR_CLIENT_NAMES,
    LOKI_GATEWAY_NAMES,
    LOKI_GATEWAY_TLS_NAME,
    NAMESPACE,
    NINETY_DAYS,
    SERVER_USAGES,
    TEMPO_NAMES,
    certificate,
    changed,
    collector_values,
    decision,
    policies,
)
from test_telemetry_ca import LOKI_GATEWAY as GATEWAY_CERTIFICATE

LOKI_VALUES = KIND_DIR / "values" / "loki.yaml"
TLS_DIRECTORY = "/etc/loki-gateway/tls"
GATEWAY_PORT = 8443
GATEWAY_HOST = "loki-gateway.observability.svc.cluster.local"
LOKI_PORT = 3100
WRITER = "CN=otel-collector-client"  # the subject nginx shows for the collector's

# The locations of the chart's gateway at the pinned version (loki 18.13.7,
# templates/_helpers.tpl `loki.nginxFile`, rendered with this repository's values
# on 2026-10-07; the `/ui` location is gone with `loki.ui.gateway.enabled: false`).
# `=` is an exact location and `^~` a prefix one. A chart bump that adds one is a
# reason to read the template again; the optional render test below compares this
# list with the chart's own rendering.
CHART_LOCATIONS = [
    ("=", "/"),
    ("=", "/stub_status"),
    ("=", "/api/prom/push"),
    ("=", "/loki/api/v1/push"),
    ("=", "/distributor/ring"),
    ("=", "/otlp/v1/logs"),
    ("=", "/flush"),
    ("^~", "/ingester/"),
    ("=", "/ingester"),
    ("=", "/ring"),
    ("=", "/memberlist"),
    ("=", "/ruler/ring"),
    ("=", "/api/prom/rules"),
    ("^~", "/api/prom/rules/"),
    ("=", "/loki/api/v1/rules"),
    ("^~", "/loki/api/v1/rules/"),
    ("=", "/prometheus/api/v1/alerts"),
    ("=", "/prometheus/api/v1/rules"),
    ("=", "/compactor/ring"),
    ("=", "/loki/api/v1/delete"),
    ("=", "/loki/api/v1/cache/generation_numbers"),
    ("=", "/indexgateway/ring"),
    ("=", "/scheduler/ring"),
    ("=", "/config"),
    ("=", "/api/prom/tail"),
    ("=", "/loki/api/v1/tail"),
    ("^~", "/api/prom/"),
    ("=", "/api/prom"),
    ("^~", "/loki/api/v1/"),
    ("=", "/loki/api/v1"),
]

# What the gateway serves without a certificate: the probe's `/` and the paths
# Grafana's Loki datasource calls (and smoke's reads through it), nothing else.
READ_PATHS = [
    "/",
    "/loki/api/v1/query",
    "/loki/api/v1/query_range",
    "/loki/api/v1/labels",
    "/loki/api/v1/label/service_name/values",
    "/loki/api/v1/detected_field/level/values",
    "/loki/api/v1/series",
    "/loki/api/v1/index/stats",
    "/loki/api/v1/index/volume",
    "/loki/api/v1/index/volume_range",
    "/loki/api/v1/patterns",
    "/loki/api/v1/detected_labels",
    "/loki/api/v1/detected_fields",
    "/loki/api/v1/format_query",
    "/loki/api/v1/status/buildinfo",
    "/loki/api/v1/tail",
]
# The three paths that write, and the only ones a certificate opens.
PUSH_PATHS = ["/otlp/v1/logs", "/loki/api/v1/push", "/api/prom/push"]
# The administrator's class: 403 whatever the certificate (each is a location of
# the chart's gateway that Loki would answer, or a prefix under one).
ADMIN_PATHS = [
    "/loki/api/v1/delete",
    "/loki/api/v1/rules",
    "/loki/api/v1/rules/fake/group",
    "/api/prom/rules",
    "/api/prom/rules/fake/group",
    "/prometheus/api/v1/rules",
    "/prometheus/api/v1/alerts",
    "/loki/api/v1/cache/generation_numbers",
    "/flush",
    "/ingester/flush_shutdown",
    "/ingester/shutdown",
    "/ingester",
    "/ring",
    "/distributor/ring",
    "/ruler/ring",
    "/compactor/ring",
    "/indexgateway/ring",
    "/scheduler/ring",
    "/memberlist",
    "/config",
    "/ui",
    "/ui/",
    "/otlp/v1/metrics",
    "/otlp/v1/traces",
    "/otlp",
]
# Paths nobody listed, under the chart's prefix locations and outside them.
UNLISTED_PATHS = [
    "/loki/api/v1/unknown",
    "/loki/api/v1/query/extra",
    "/loki/api/v1/label//values",
    "/loki/api/v1/label/a-b/values",
    "/loki/api/v1/label/../values",
    "/api/prom/query",
    "/api/prom/label",
    "/api/prom/tail",
    "/foo",
    "/ready",
    "/metrics",
    "/stub_status",
]
# The same paths written the way a client may write them to get past a pattern.
ODD_FORMS = [
    "//loki/api/v1/push",
    "/loki//api/v1/push",
    "/loki/api/v1/push//",
    "/loki/api/v1/pu%73h",
    "/loki/api/v1/%70ush",
    "/loki/api/v1/push%2F",
    "/loki%2Fapi/v1/push",
    "/loki/api/v1/push/",
    "/loki/api/v1/push/x",
    "/loki/api/v1/./push",
    "/loki/api/v1/x/../push",
    "/loki/api/v1/../v1/push",
    "/LOKI/API/V1/PUSH",
    "/Loki/api/v1/push",
    "/otlp/v1/logs/",
    "/otlp//v1/logs",
    "/otlp/v1/%6cogs",
    "/api/prom/pu%73h",
    "//api/prom/push",
    "/loki/api/v1/query_range/",
    "//loki/api/v1/query_range",
    "/loki/api/v1/query%5Frange",
    "/loki/api/v1/query_range%00",
    "/loki/api/v1/push\t",
    "//",
    "/ /",
]


def loki_values() -> dict:
    return yaml.safe_load(LOKI_VALUES.read_text(encoding="utf-8"))


def gateway_values() -> dict:
    return loki_values()["gateway"]


# ── the certificate ──────────────────────────────────────────────────────────


def test_the_certificate_is_a_server_certificate_and_never_a_client_one() -> None:
    spec = certificate(GATEWAY_CERTIFICATE)["spec"]
    collector = certificate("otel-collector")["spec"]

    assert sorted(spec["usages"]) == SERVER_USAGES
    assert "client auth" not in spec["usages"]
    assert spec["secretName"] == LOKI_GATEWAY_TLS_NAME
    assert spec["duration"] == collector["duration"] == NINETY_DAYS
    assert spec["privateKey"] == collector["privateKey"]
    assert spec["privateKey"]["rotationPolicy"] == "Always"
    for absent in ("uris", "ipAddresses", "emailAddresses", "commonName", "isCA"):
        assert absent not in spec, absent


def test_the_names_are_the_gateway_service_and_the_host_the_exporter_dials() -> None:
    endpoint = collector_values()["config"]["exporters"]["otlp_http/loki"]["endpoint"]
    host = urlsplit(endpoint).hostname

    # Read from the exporter, so the two cannot drift: the collector verifies the
    # certificate against this host. The Service is the chart's `loki-gateway`.
    assert host == GATEWAY_HOST
    assert certificate(GATEWAY_CERTIFICATE)["spec"]["dnsNames"] == [
        host.removesuffix(".cluster.local"),
        host,
    ]
    assert certificate(GATEWAY_CERTIFICATE)["spec"]["dnsNames"] == LOKI_GATEWAY_NAMES


# ── its policy ───────────────────────────────────────────────────────────────


def test_the_policy_allows_the_two_names_two_usages_and_nothing_else() -> None:
    spec = policies()[LOKI_GATEWAY_POLICY]["spec"]

    assert spec["allowed"] == {
        "dnsNames": {"values": LOKI_GATEWAY_NAMES, "required": True},
        "usages": ["digital signature", "server auth"],
    }
    assert spec["constraints"] == {"maxDuration": NINETY_DAYS}
    assert spec["selector"] == {
        "issuerRef": {"name": AUTHORITY, "kind": "Issuer", "group": "cert-manager.io"},
        "namespace": {"matchNames": [NAMESPACE]},
    }


def test_its_own_request_is_approved_by_its_own_policy_alone() -> None:
    request = request_of(certificate(GATEWAY_CERTIFICATE))

    assert decision(request) == "approved"
    permitting = {n for n, p in policies().items() if allows(p, request)}
    assert permitting == {LOKI_GATEWAY_POLICY}


def test_no_other_policy_of_the_authority_permits_the_gateways_names() -> None:
    request = request_of(certificate(GATEWAY_CERTIFICATE))

    for other in (COLLECTOR_POLICY, COLLECTOR_CLIENT_POLICY, TEMPO_RECEIVER_POLICY):
        assert not allows(policies()[other], request), other


@pytest.mark.parametrize(
    ("what", "change"),
    [
        ("client auth only", {"usages": ["client auth"]}),
        ("client auth instead", {"usages": ["digital signature", "client auth"]}),
        (
            "client auth too",
            {"usages": ["digital signature", "server auth", "client auth"]},
        ),
        ("another name", {"dnsNames": ["loki.observability.svc"]}),
        ("a wildcard name", {"dnsNames": ["*.observability.svc"]}),
        ("one more name", {"dnsNames": [*LOKI_GATEWAY_NAMES, "evil.example"]}),
        ("no name", {"dnsNames": None}),
        ("a URI", {"uris": ["spiffe://meridian.kind/ns/meridian/sa/claims-api"]}),
        ("a CA", {"isCA": True}),
        ("a common name", {"commonName": "loki-gateway"}),
        # The writer's own subject on the gateway's names: the policy allows no
        # common name here, so a server certificate cannot carry the one the
        # gateway admits a write from.
        ("the writer's common name", {"commonName": "otel-collector-client"}),
        ("a lifetime over 90 days", {"duration": "2161h"}),
    ],
)
def test_a_request_outside_what_the_gateways_policy_allows_is_denied(
    what: str, change: dict
) -> None:
    request = request_of(changed(certificate(GATEWAY_CERTIFICATE), **change))

    assert decision(request) == "denied", what
    assert not any(allows(p, request) for p in policies().values()), what


def test_tempo_s_names_are_not_the_gateways_and_its_policy_does_not_permit_them() -> (
    None
):
    request = request_of(
        changed(certificate(GATEWAY_CERTIFICATE), dnsNames=TEMPO_NAMES)
    )

    # Each leaf keeps to its own names: these are Tempo's, and Tempo's policy
    # (which holds the same usages) is the one that permits them.
    assert not allows(policies()[LOKI_GATEWAY_POLICY], request)
    assert allows(policies()[TEMPO_RECEIVER_POLICY], request)


@pytest.mark.parametrize("names", [LOKI_GATEWAY_NAMES[:1], LOKI_GATEWAY_NAMES[1:]])
def test_either_of_the_gateways_names_alone_is_within_its_policy(
    names: list[str],
) -> None:
    request = request_of(changed(certificate(GATEWAY_CERTIFICATE), dnsNames=names))

    assert decision(request) == "approved"


def test_a_client_auth_request_under_the_gateways_name_is_denied() -> None:
    request = request_of(
        changed(
            certificate(GATEWAY_CERTIFICATE),
            usages=["digital signature", "client auth"],
        )
    )

    assert decision(request) == "denied"
    assert not any(allows(p, request) for p in policies().values())


def test_a_request_without_client_auth_is_approved_a_usage_is_no_requirement() -> None:
    # approver-policy's `allowed.usages` is a ceiling (its CRD: "must be a subset"),
    # not a requirement: a request for the gateway's names with `digital signature`
    # alone is approved, and its certificate has no extended key usage, which nginx
    # accepts as a client's (seen). The gateway's subject check is what stops that
    # certificate from writing, and the policy allows it no common name.
    request = request_of(
        changed(certificate(GATEWAY_CERTIFICATE), usages=["digital signature"])
    )

    assert decision(request) == "approved"


def test_the_collectors_client_name_with_a_server_usage_is_still_denied() -> None:
    request = request_of(
        changed(
            certificate(COLLECTOR_CLIENT), usages=["digital signature", "server auth"]
        )
    )

    assert request["dnsNames"] == COLLECTOR_CLIENT_NAMES
    assert decision(request) == "denied"


def test_the_policys_boundaries_are_inclusive() -> None:
    gateway = certificate(GATEWAY_CERTIFICATE)
    cap = policies()[LOKI_GATEWAY_POLICY]["spec"]["constraints"]["maxDuration"]

    assert hours(cap) == hours(gateway["spec"]["duration"])
    assert decision(request_of(changed(gateway, duration="2160h"))) == "approved"
    assert decision(request_of(changed(gateway, duration="2160h1m"))) == "denied"
    assert decision(request_of(changed(gateway, duration=None))) == "never decided"


# ── the gateway's values ─────────────────────────────────────────────────────


def test_the_gateway_is_on_with_one_replica_and_one_container() -> None:
    gateway = gateway_values()

    assert gateway["enabled"] is True
    assert gateway["replicas"] == 1
    # The chart's metrics sidecar (a second image and a syslog access log) is off.
    assert gateway["metrics"] == {"enabled": False}
    # The chart's hard anti-affinity would hold a rolling update on one node.
    assert gateway["affinity"] is None
    # No Service link variables, and the chart's UI location is dropped.
    assert gateway["enableServiceLinks"] is False
    assert loki_values()["loki"]["ui"] == {"gateway": {"enabled": False}}


def test_the_gateway_has_a_small_memory_request_and_a_limit() -> None:
    resources = gateway_values()["resources"]

    assert resources["requests"]["memory"] == "32Mi"
    assert resources["limits"]["memory"] == "64Mi"
    # The chart's /tmp is an emptyDir with no size limit and no value for one: the
    # container's ephemeral-storage limit bounds what a large push writes there.
    assert resources["limits"]["ephemeral-storage"] == "128Mi"
    assert set(resources["limits"]) == {"memory", "ephemeral-storage"}  # no CPU limit


def test_the_gateway_mounts_no_service_account_token() -> None:
    values = loki_values()

    # Said in the file, not left to the chart's default (which is false too).
    assert values["defaults"]["automountServiceAccountToken"] is False
    assert values["serviceAccount"]["automountServiceAccountToken"] is False
    assert values["gateway"]["automountServiceAccountToken"] is False
    for key in ("podSecurityContext", "containerSecurityContext"):
        # Left to the chart's defaults, which are the `restricted` level.
        assert key not in values["gateway"], key


def test_the_listener_is_one_tls_port_the_service_and_the_container_share() -> None:
    gateway = gateway_values()

    assert gateway["containerPort"] == gateway["service"]["port"] == GATEWAY_PORT
    assert gateway["nginxConfig"]["ssl"] is True
    # `listen [::]` would fail to start on an IPv4-only pod network.
    assert gateway["nginxConfig"]["enableIPv6"] is False
    # Both probes speak TLS, with no client certificate (`optional` admits it).
    assert gateway["readinessProbe"] == {"httpGet": {"scheme": "HTTPS"}}
    liveness = gateway["livenessProbe"]
    assert liveness["httpGet"] == {"path": "/", "port": "http", "scheme": "HTTPS"}
    assert 0 < liveness["periodSeconds"] <= 30
    assert 0 < liveness["timeoutSeconds"] <= 5


def test_the_tls_directives_ask_for_an_optional_certificate_of_the_authority() -> None:
    snippet = gateway_values()["nginxConfig"]["serverSnippet"]
    directives = {
        line.rstrip(";").split(None, 1)[0]: line.rstrip(";").split(None, 1)[1]
        for line in snippet.splitlines()
        if line.strip()
    }

    assert directives == {
        # A variable, read at each handshake (seen on the pinned image): the
        # renewed certificate is served without a reload. The values are below.
        "ssl_certificate": "$loki_gateway_cert",
        "ssl_certificate_key": "$loki_gateway_key",
        # The client CA is read at start only: up.sh annotates the pod with its
        # fingerprint so that the pod is rolled when it changes.
        "ssl_client_certificate": f"{TLS_DIRECTORY}/ca.crt",
        # Optional: required would fail the kubelet's probe and Grafana's reads;
        # `off` would verify nothing; `optional_no_ca` would not check the chain.
        "ssl_verify_client": "optional",
        "ssl_protocols": "TLSv1.3",
    }
    maps = parse_maps()
    assert maps["$loki_gateway_cert"] == (
        "$host",
        [("default", f"{TLS_DIRECTORY}/tls.crt")],
    )
    assert maps["$loki_gateway_key"] == (
        "$host",
        [("default", f"{TLS_DIRECTORY}/tls.key")],
    )
    assert "server_tokens off;" in gateway_values()["nginxConfig"]["httpSnippet"]


def test_the_secret_is_mounted_read_only_outside_the_charts_config_directory() -> None:
    gateway = gateway_values()
    (volume,) = gateway["extraVolumes"]
    (mount,) = gateway["extraVolumeMounts"]

    assert volume["secret"]["secretName"] == LOKI_GATEWAY_TLS_NAME
    assert (
        certificate(GATEWAY_CERTIFICATE)["spec"]["secretName"] == LOKI_GATEWAY_TLS_NAME
    )
    assert mount["name"] == volume["name"]
    assert mount["mountPath"] == TLS_DIRECTORY
    assert mount["readOnly"] is True
    assert "subPath" not in mount
    # The chart's ConfigMap owns /etc/nginx: a mount inside it would not work.
    assert not mount["mountPath"].startswith("/etc/nginx")
    assert volume["secret"]["defaultMode"] == 0o440


def test_loki_s_own_listener_probes_and_service_are_not_touched() -> None:
    values = loki_values()

    assert set(values["loki"]) == {
        "auth_enabled",
        "commonConfig",
        "storage",
        "schemaConfig",
        "limits_config",
        "compactor",
        "pattern_ingester",
        "ui",
    }
    assert set(values["singleBinary"]) == {
        "replicas",
        "resources",
        "persistence",
        "sidecar",
    }


# ── the maps that decide a request ───────────────────────────────────────────

MAP = re.compile(r"map\s+(\"[^\"]+\"|\S+)\s+(\$\w+)\s*\{(.*?)\}", re.S)


def parse_maps() -> dict[str, tuple[str, list[tuple[str, str]]]]:
    snippet = gateway_values()["nginxConfig"]["httpSnippet"]
    found = {}
    for source, name, body in MAP.findall(snippet):
        entries = []
        for entry in body.split(";"):
            entry = entry.strip()
            if entry:
                key, _, value = entry.rpartition(" ")
                entries.append((key.strip(), value))
        found[name] = (source.strip('"'), entries)
    return found


def evaluate(entries: list[tuple[str, str]], key: str) -> str:
    """nginx's map: an exact string wins over any regular expression, the
    regular expressions are tried in order, and `default` is the last resort."""
    exact = {k.strip('"'): v for k, v in entries if not k.startswith("~")}
    if key in exact and key != "default":
        return exact[key]
    for pattern, value in entries:
        if pattern.startswith("~") and re.search(pattern[1:], key):
            return value
    return exact["default"]


def normalised(raw: str) -> str:
    """What nginx's `$uri` is for a raw request URI: the path part, percent-
    decoded, with merged slashes (`merge_slashes` is on by default) and the dot
    segments resolved. The query string is not part of it."""
    path = unquote(raw.split("?", 1)[0])
    path = re.sub(r"/{2,}", "/", path)
    out: list[str] = []
    for segment in path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if len(out) > 1:
                out.pop()
            continue
        out.append(segment)
    return "/".join(out) or "/"


def class_of(raw: str) -> str:
    """r, w or x for a raw request URI, by the three maps in the order nginx
    evaluates them (a map is evaluated when its variable is first used)."""
    maps = parse_maps()
    source, entries = maps["$loki_class_raw"]
    assert source == "$request_uri"
    by_raw = evaluate(entries, raw)
    source, entries = maps["$loki_class_uri"]
    assert source == "$uri"
    by_uri = evaluate(entries, normalised(raw))
    source, entries = maps["$loki_class"]
    assert source == "$loki_class_raw:$loki_class_uri"
    return evaluate(entries, f"{by_raw}:{by_uri}")


def denied(raw: str, verify: str = "NONE", subject: str = "") -> bool:
    """Whether `if ($loki_denied) { return 403; }` fires for the request."""
    source, entries = parse_maps()["$loki_denied"]
    assert source == "$loki_class:$ssl_client_verify:$ssl_client_s_dn"
    return evaluate(entries, f"{class_of(raw)}:{verify}:{subject}") == "1"


# What nginx shows for the four kinds of client: none, the collector's
# certificate, one of the authority's with another subject, and one of the
# authority's with no subject. (A certificate that does not verify, or that is
# for the wrong purpose, never reaches these maps: nginx ends the request with its
# own 400, seen on the pinned image; the maps still deny `FAILED:...` and an
# empty value on a write path.)
NO_CERTIFICATE = ("NONE", "")
COLLECTORS = ("SUCCESS", WRITER)
OTHER_NAME = ("SUCCESS", "CN=somebody-else")
NO_NAME = ("SUCCESS", "")
NOT_VERIFIED = ("FAILED:unable to verify the first certificate", WRITER)
EVERY_CLIENT = [NO_CERTIFICATE, COLLECTORS, OTHER_NAME, NO_NAME, NOT_VERIFIED]


def test_the_maps_and_the_location_snippet_are_the_ones_the_tests_model() -> None:
    maps = parse_maps()

    assert set(maps) == {
        "$loki_gateway_cert",
        "$loki_gateway_key",
        "$loki_class_raw",
        "$loki_class_uri",
        "$loki_class",
        "$loki_denied",
    }
    assert gateway_values()["nginxConfig"]["locationSnippet"].strip() == (
        "if ($loki_denied) { return 403; }"
    )
    # The default of each is the closed class, and nothing but `r` is open to a
    # client with no certificate.
    for name in ("$loki_class_raw", "$loki_class_uri", "$loki_class"):
        assert dict(maps[name][1])["default"] == "x", name
    assert dict(maps["$loki_denied"][1])["default"] == "1"


def test_the_two_views_of_the_path_are_the_same_list() -> None:
    maps = parse_maps()

    # One list on each view of the request: a pattern changed in one and not in the
    # other would open a path on one view only.
    assert maps["$loki_class_raw"][1] == maps["$loki_class_uri"][1]


@pytest.mark.parametrize("path", READ_PATHS)
@pytest.mark.parametrize("client", EVERY_CLIENT)
def test_a_read_path_is_served_to_every_client(
    path: str, client: tuple[str, str]
) -> None:
    assert class_of(path) == "r", path
    assert not denied(path, *client)


@pytest.mark.parametrize("path", PUSH_PATHS)
def test_a_push_path_is_served_only_to_the_collectors_certificate(path: str) -> None:
    assert class_of(path) == "w"
    assert not denied(path, *COLLECTORS)
    for client in (NO_CERTIFICATE, OTHER_NAME, NO_NAME, NOT_VERIFIED, ("", "")):
        assert denied(path, *client), (path, client)


@pytest.mark.parametrize("path", PUSH_PATHS)
def test_a_push_path_with_a_query_string_is_still_a_push_path(path: str) -> None:
    assert class_of(path + "?x=1") == "w"
    assert denied(path + "?x=1", *NO_CERTIFICATE)
    assert not denied(path + "?x=1", *COLLECTORS)


@pytest.mark.parametrize("path", [*ADMIN_PATHS, *UNLISTED_PATHS])
@pytest.mark.parametrize("client", EVERY_CLIENT)
def test_an_administrators_or_unlisted_path_is_refused_whatever_the_certificate(
    path: str, client: tuple[str, str]
) -> None:
    assert class_of(path) == "x", path
    assert denied(path, *client), (path, client)


@pytest.mark.parametrize("path", ODD_FORMS)
@pytest.mark.parametrize("client", EVERY_CLIENT)
def test_a_path_written_to_get_past_a_pattern_is_refused_whatever_the_certificate(
    path: str, client: tuple[str, str]
) -> None:
    # Fail closed on what the client SENT: a raw path with `%`, `//`, a dot segment,
    # a trailing slash, another case or a control character is no read and no push,
    # even where nginx would decode, merge or resolve it into one.
    assert class_of(path) == "x", path
    assert denied(path, *client), (path, client)


def test_the_view_of_the_path_nginx_matches_a_location_on_is_modelled() -> None:
    assert normalised("//loki/api/v1/push") == "/loki/api/v1/push"
    assert normalised("/loki/api/v1/pu%73h") == "/loki/api/v1/push"
    assert normalised("/loki/api/v1/x/../push") == "/loki/api/v1/push"
    assert normalised("/loki/api/v1/./push?a=//") == "/loki/api/v1/push"
    assert normalised("/a%2Fb") == "/a/b"
    assert normalised("/") == "/"
    # The raw view alone would let each of these through as the push path it
    # becomes, and the normalised view alone would let a decoded one through; the
    # gateway compares them.
    maps = parse_maps()
    raw_source, raw_entries = maps["$loki_class_raw"]
    assert raw_source == "$request_uri"
    assert evaluate(raw_entries, "/loki/api/v1/pu%73h") == "x"
    assert evaluate(raw_entries, "//loki/api/v1/push") == "x"
    assert evaluate(raw_entries, "/loki/api/v1/push") == "w"


def test_every_location_of_the_chart_is_a_read_a_push_or_closed() -> None:
    open_paths = set(READ_PATHS) | set(PUSH_PATHS)

    for kind, path in CHART_LOCATIONS:
        # A request that lands in each location: the path itself, and for a prefix
        # location a path under it.
        probes = [path] if kind == "=" else [path, path + "anything"]
        for probe in probes:
            if probe in open_paths:
                continue
            assert class_of(probe) == "x", (kind, probe)
            for client in EVERY_CLIENT:
                assert denied(probe, *client), (kind, probe, client)


def test_the_path_the_collectors_exporter_posts_to_is_a_push_path() -> None:
    endpoint = collector_values()["config"]["exporters"]["otlp_http/loki"]["endpoint"]

    # otlp_http appends /v1/logs to the endpoint's path.
    path = urlsplit(endpoint).path + "/v1/logs"
    assert path in PUSH_PATHS
    assert class_of(path) == "w"
    assert not denied(path, *COLLECTORS)


def test_the_paths_grafana_and_smoke_read_loki_by_are_read_paths() -> None:
    sources = {s["name"]: s for s in stack_values()["grafana"]["additionalDataSources"]}
    base = urlsplit(sources["Loki"]["url"]).path

    # Grafana's datasource calls these under the URL's path (empty here); smoke
    # reaches Loki only through Grafana's proxy, with the same paths.
    for call in ("/loki/api/v1/query_range", "/loki/api/v1/labels"):
        assert class_of(base + call) == "r"
        assert not denied(base + call, *NO_CERTIFICATE)


def test_the_name_the_gateway_admits_is_the_name_the_collectors_policy_allows() -> None:
    allowed = policies()[COLLECTOR_CLIENT_POLICY]["spec"]["allowed"]["commonName"]
    _, entries = parse_maps()["$loki_denied"]
    admitted = [key for key, value in entries if value == "0" and key.startswith('"w:')]

    # One exact string admits a write, and its subject is the common name that the
    # client certificate carries and that the policy allows (and requires).
    assert admitted == [f'"w:SUCCESS:CN={allowed["value"]}"']
    assert allowed == {"value": "otel-collector-client", "required": True}
    assert certificate(COLLECTOR_CLIENT)["spec"]["commonName"] == allowed["value"]


def render_file() -> Path | None:
    found = os.environ.get("MERIDIAN_LOKI_RENDER")
    return Path(found) if found and Path(found).is_file() else None


@pytest.mark.skipif(render_file() is None, reason="MERIDIAN_LOKI_RENDER is not set")
def test_the_charts_own_rendering_lists_the_locations_this_file_pins() -> None:
    # Optional, because the chart is not in the repository and CI has no copy: run
    # `helm template` of the pinned chart with kind's values to a file and name it.
    documents = [d for d in yaml.safe_load_all(render_file().read_text("utf-8")) if d]
    (config,) = [
        d["data"]["nginx.conf"]
        for d in documents
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "loki-gateway"
    ]
    found = re.findall(r"^\s*location (=|\^~) (\S+) \{", config, re.M)

    assert sorted(found) == sorted(CHART_LOCATIONS)
    # Every location that passes a request on carries the check; the exceptions are
    # `/stub_status` (loopback only) and the ones that are `internal`.
    blocks = re.split(r"\n\s*location ", config)[1:]
    for block in blocks:
        name = block.split(" {", 1)[0]
        if name.endswith("/stub_status"):
            continue
        assert "if ($loki_denied) { return 403; }" in block, name


# ── the exporter ─────────────────────────────────────────────────────────────


def test_the_exporter_to_loki_is_mutual_tls_to_the_gateway() -> None:
    exporters = collector_values()["config"]["exporters"]
    exporter = exporters["otlp_http/loki"]
    parts = urlsplit(exporter["endpoint"])

    assert parts.scheme == "https"
    assert parts.hostname == GATEWAY_HOST
    assert parts.port == GATEWAY_PORT == gateway_values()["containerPort"]
    assert parts.path == "/otlp"
    # The same block as Tempo's exporter (the authority verifies the gateway, the
    # pair is the collector's client certificate, the same reload and floor).
    assert exporter["tls"] == exporters["otlp_grpc/tempo"]["tls"]
    assert exporter["tls"] == {
        "ca_file": "/etc/otel-collector/client-tls/ca.crt",
        "cert_file": "/etc/otel-collector/client-tls/tls.crt",
        "key_file": "/etc/otel-collector/client-tls/tls.key",
        "reload_interval": "5m",
        "min_version": "1.3",
    }
    assert set(exporter) == {"endpoint", "tls"}


def test_the_exporter_does_not_go_to_lokis_own_port() -> None:
    text = json.dumps(collector_values()["config"]["exporters"])

    assert f":{LOKI_PORT}" not in text
    assert "http://loki." not in text


# ── Grafana's datasource ─────────────────────────────────────────────────────


def loki_datasource() -> dict:
    sources = stack_values()["grafana"]["additionalDataSources"]
    (found,) = [s for s in sources if s["uid"] == "loki"]
    return found


def test_grafana_reads_loki_through_the_gateway_over_tls_and_trusts_the_authority() -> (
    None
):
    source = loki_datasource()
    parts = urlsplit(source["url"])

    assert (parts.scheme, parts.hostname, parts.port) == (
        "https",
        GATEWAY_HOST,
        GATEWAY_PORT,
    )
    assert parts.hostname in certificate(GATEWAY_CERTIFICATE)["spec"]["dnsNames"]
    assert source["jsonData"]["tlsAuthWithCACert"] is True
    assert source["secureJsonData"] == {"tlsCACert": "$LOKI_GATEWAY_CA"}


def test_grafana_presents_no_certificate_and_skips_no_verification() -> None:
    source = loki_datasource()

    # A reader holds nothing a writer needs: no client pair, no basic login, no
    # skipped verification, in the datasource or anywhere in Grafana's values.
    grafana = json.dumps(stack_values()["grafana"])
    for forbidden in (
        'tlsAuth"',
        "tlsClientCert",
        "tlsClientKey",
        "tlsSkipVerify",
        "basicAuth",
        "otel-collector-client",
        "loki-gateway-tls",
    ):
        assert forbidden not in grafana, forbidden
    assert "tlsAuth" not in source["jsonData"]
    assert set(source["secureJsonData"]) == {"tlsCACert"}
    assert "extraSecretMounts" not in stack_values()["grafana"]


def test_the_authority_reaches_grafana_as_a_public_certificate_from_a_configmap() -> (
    None
):
    env = stack_values()["grafana"]["envValueFrom"]

    assert env == {
        "LOKI_GATEWAY_CA": {
            "configMapKeyRef": {"name": "telemetry-ca", "key": "ca.crt"}
        }
    }


def test_the_tempo_datasource_and_the_traces_to_logs_link_are_as_they_were() -> None:
    sources = stack_values()["grafana"]["additionalDataSources"]
    (tempo,) = [s for s in sources if s["uid"] == "tempo"]

    assert tempo["url"] == "http://tempo.observability.svc.cluster.local:3200"
    # The link names the Loki datasource by uid, so it follows the datasource to
    # the gateway and names no address of its own.
    link = tempo["jsonData"]["tracesToLogsV2"]
    assert link["datasourceUid"] == "loki"
    assert not {"url", "endpoint", "address"} & set(link)


# ── who may reach which port ─────────────────────────────────────────────────


def admits(rule: dict, port: int) -> bool:
    """Whether a rule lets traffic through on ``port``: a rule with no ``ports``
    key lets every port through, which is what Kubernetes does with it."""
    ports = rule.get("ports")
    return ports is None or port in {p["port"] for p in ports}


def ingress_peers_for(port: int) -> dict[str, list[dict]]:
    """Each policy of the namespace that admits ``port``, and the peers it lets in."""
    found: dict[str, list[dict]] = {}
    for name, policy in observability_policies().items():
        for rule in rules(policy, "ingress"):
            if admits(rule, port):
                found.setdefault(name, []).extend(rule.get("from", [{}]))
    return found


def egress_targets_for(port: int) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for name, policy in observability_policies().items():
        for rule in rules(policy, "egress"):
            if admits(rule, port):
                found.setdefault(name, []).extend(rule.get("to", [{}]))
    return found


def test_a_rule_with_no_port_is_taken_to_admit_every_port() -> None:
    portless = {"from": [pods(LOKI_GATEWAY)]}

    # The helpers must see an all-ports rule towards Loki or the gateway, not skip
    # it: the tests below would otherwise pass over one.
    assert admits(portless, LOKI_PORT)
    assert admits(portless, GATEWAY_PORT)
    assert not admits({"ports": [{"port": 53}]}, LOKI_PORT)


def test_loki_s_own_port_takes_connections_from_the_gateway_alone() -> None:
    admitted = ingress_peers_for(LOKI_PORT)

    # One policy admits 3100, and its one peer is the gateway's pods: neither the
    # collector nor Grafana, and no other pod of the namespace.
    assert admitted == {"loki": [pods(LOKI_GATEWAY)]}
    assert pods(LOKI_GATEWAY) != pods(LOKI)


def test_no_pod_but_the_gateway_and_smokes_probe_may_send_to_lokis_own_port() -> None:
    senders = egress_targets_for(LOKI_PORT)

    # The gateway's own egress, and a policy that exists for smoke's probe Pod
    # alone (it proves Loki's ingress rule, see its comment); both name Loki's
    # pods and no other target, and nothing else in the namespace reaches 3100.
    assert senders == {
        "egress-loki-gateway": [pods(LOKI)],
        "smoke-telemetry-probe": [pods(LOKI)],
    }


def test_the_gateway_admits_the_collector_and_grafana_on_its_one_port() -> None:
    admitted = ingress_peers_for(GATEWAY_PORT)

    assert admitted == {"loki-gateway": [pods(collector_labels()), pods(GRAFANA)]}


def test_the_collector_and_grafana_send_to_the_gateway_and_to_no_other_loki_pod() -> (
    None
):
    senders = egress_targets_for(GATEWAY_PORT)

    assert senders == {
        "egress-grafana": [pods(LOKI_GATEWAY)],
        "egress-otel-collector": [pods(LOKI_GATEWAY)],
        # Smoke's probe Pod has the collector's label, so the collector's rule
        # applies to it as it does to the collector.
    }


def test_the_gateway_may_reach_loki_and_dns_and_nothing_else() -> None:
    egress = [
        (name, rule)
        for name, policy in observability_policies().items()
        if policy["spec"]["podSelector"].get("matchLabels") == LOKI_GATEWAY
        and "Egress" in policy["spec"]["policyTypes"]
        for rule in rules(policy, "egress")
    ]

    assert egress == [
        (
            "egress-loki-gateway",
            {"to": [pods(LOKI)], "ports": [{"port": 3100, "protocol": "TCP"}]},
        )
    ]
    # DNS is the namespace-wide rule's, which selects every pod.
    dns = observability_policies()["egress-dns"]
    assert dns["spec"]["podSelector"] == {}


def test_the_policies_of_lokis_pods_are_in_a_file_of_their_own() -> None:
    from test_kind_namespace_policies import (
        LOKI_POLICY_FILE,
        OBSERVABILITY_FILE,
        policies_of,
    )

    own = set(policies_of(LOKI_POLICY_FILE))
    main = set(policies_of(OBSERVABILITY_FILE))

    assert own == {
        "loki",
        "loki-gateway",
        "egress-loki-gateway",
        "egress-loki",
        "smoke-telemetry-probe",
    }
    assert own.isdisjoint(main)
