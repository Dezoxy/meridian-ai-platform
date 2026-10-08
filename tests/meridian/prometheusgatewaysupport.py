"""What the tests of Prometheus's gateway share (S072, contract M4): the manifest and
the lists of paths it is judged against, the helpers that read the manifest, and
the ``map`` rules of the gateway evaluated with nginx's own rules (the ones Loki's
gateway is judged with: ``lokigatewaysupport.py``).
"""

import yaml
from certpolicysupport import KIND_DIR
from lokigatewaysupport import MAP, evaluate, normalised

GATEWAY_FILE = KIND_DIR / "manifests" / "observability-prometheus-gateway.yaml"
TLS_DIRECTORY = "/etc/prometheus-gateway/tls"
GATEWAY_PORT = 8443
GATEWAY_HOST = "prometheus-gateway.observability.svc.cluster.local"
PROMETHEUS_PORT = 9090
UPSTREAM = f"http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:{PROMETHEUS_PORT}"
WRITER = "CN=otel-collector-client"  # the subject nginx shows for the collector's
PLACEHOLDERS = (
    "IMAGE-PLACEHOLDER",
    "MANIFEST-SHA256-PLACEHOLDER",
    "CA-SHA256-PLACEHOLDER",
    "SERVICE-ADDRESS-PLACEHOLDER",
)
# A cluster address the stub answers for the Prometheus Service (a documentation
# address, RFC 5737).
SERVICE_ADDRESS = "192.0.2.17"

# What the gateway serves without a certificate, and where each path comes from.
# "grafana": a call of Grafana's Prometheus datasource (RECALLED from the datasource,
# not seen: the cluster's first run is what shows a call missing here, as a 403 in
# Grafana); "smoke": read in the repository's smoke scripts; "review": the
# infrastructure review's section 7.3; "nginx": nginx answers it itself. Each one
# was SEEN to exist on the pinned image (Prometheus 3.15.0), in the probe of
# ROUTES_SEEN.
READ_PATHS = {
    "/": "nginx",
    "/api/v1/query": "smoke grafana",
    "/api/v1/query_range": "grafana",
    "/api/v1/query_exemplars": "grafana review",
    "/api/v1/format_query": "grafana review",
    "/api/v1/parse_query": "grafana",
    "/api/v1/series": "grafana",
    "/api/v1/labels": "grafana",
    "/api/v1/label/job/values": "grafana",
    "/api/v1/label/__name__/values": "grafana",
    "/api/v1/metadata": "grafana review",
    # The run's probe through Grafana's proxy reads the Targets (that Prometheus's
    # two self-scrape endpoints are up), which no dashboard does.
    "/api/v1/targets": "probe",
    "/api/v1/rules": "smoke grafana review",
    "/api/v1/alerts": "grafana review",
    "/api/v1/status/buildinfo": "grafana review",
}
# The status pages that were on the list until contract M4b and have no caller: the
# configuration (it prints the scrape configuration), the flags (reconnaissance),
# the runtime and the TSDB. Closed, whatever the certificate.
REMOVED_STATUS_PATHS = [
    "/api/v1/status/config",
    "/api/v1/status/flags",
    "/api/v1/status/runtimeinfo",
    "/api/v1/status/tsdb",
]
# The one path that writes, and the only one a certificate opens.
WRITE_PATH = "/api/v1/otlp/v1/metrics"
# Remote write, the admin API and the lifecycle endpoints: 403 whatever the
# certificate, WHETHER OR NOT Prometheus enables them (the pinned image answers 404,
# 500 or 403 for them without their flags, and serves them with the flags).
CLOSED_WRITE_SURFACES = [
    "/api/v1/write",
    "/api/v1/read",
    "/api/v1/otlp",
    "/api/v1/otlp/",
    "/api/v1/otlp/v1/traces",
    "/api/v1/otlp/v1/logs",
    "/api/v1/admin",
    "/api/v1/admin/",
    "/api/v1/admin/tsdb/delete_series",
    "/api/v1/admin/tsdb/clean_tombstones",
    "/api/v1/admin/tsdb/snapshot",
    "/-/reload",
    "/-/quit",
]
# Everything else the pinned image serves, and a few that it does not.
OTHER_CLOSED_PATHS = [
    *REMOVED_STATUS_PATHS,
    "/-/healthy",
    "/-/ready",
    "/federate",
    "/metrics",
    "/graph",
    "/query",
    "/alerts",
    "/rules",
    "/targets",
    "/config",
    "/flags",
    "/service-discovery",
    "/tsdb-status",
    "/status",
    "/version",
    "/debug/pprof/",
    "/debug/pprof/heap",
    "/debug/pprof/profile",
    "/api/v1/notifications",
    "/api/v1/notifications/live",
    "/api/v1/targets/metadata",
    "/api/v1/targets/relabel_steps",
    "/api/v1/scrape_pools",
    "/api/v1/alertmanagers",
    "/api/v1/status/walreplay",
    "/api/v1/status/tsdb/blocks",
    "/api/v1/features",
    "/api/v1/unknown",
    "/api/v2/status",
    "/api/v1",
    "/api/v1/",
    "/foo",
]
# The same paths written the way a client may write them to get past a pattern.
ODD_FORMS = [
    "//api/v1/otlp/v1/metrics",
    "/api//v1/otlp/v1/metrics",
    "/api/v1/otlp//v1/metrics",
    "/api/v1/otlp/v1/metrics//",
    "/api/v1/otlp/v1/metric%73",
    "/api/v1/otlp/v1/%6detrics",
    "/api/v1/otlp/v1/metrics%2F",
    "/api%2Fv1/otlp/v1/metrics",
    "/api/v1/otlp/v1/metrics/",
    "/api/v1/otlp/v1/metrics/x",
    "/api/v1/./otlp/v1/metrics",
    "/api/v1/x/../otlp/v1/metrics",
    "/api/v1/../v1/otlp/v1/metrics",
    "/API/V1/OTLP/V1/METRICS",
    "/api/v1/write/",
    "/api/v1/%77rite",
    "/api/v1/status/../write",
    "/-/%72eload",
    "/api/v1/../-/reload",
    "/api/v1/../../-/quit",
    "/api/v1/query%2F..%2F..%2F-%2Freload",
    "/api/v1/query/",
    "/api/v1/query//",
    "//api/v1/query",
    "/api/v1/query%0A",
    "/api/v1/query%00",
    "/api/v1/query\t",
    "/api/v1/query_range/",
    "/api/v1/query%5Frange",
    "/api/v1/label//values",
    "/api/v1/label/a-b/values",
    "/api/v1/label/../query",
    "/api/v1/label/job/values/",
    "/api/v1/status/config/",
    "//",
    "/ /",
]
# The routes the pinned image (Prometheus 3.15.0) answered other than 404 with every
# optional surface on, probed in a container (the contract's report has the
# table): the read list above is a subset of the GET routes, and the write list is
# the POST ones that change state.
ROUTES_SEEN = {
    *READ_PATHS,
    *REMOVED_STATUS_PATHS,
    WRITE_PATH,
    "/api/v1/write",
    "/api/v1/read",
    "/api/v1/admin/tsdb/delete_series",
    "/api/v1/admin/tsdb/clean_tombstones",
    "/api/v1/admin/tsdb/snapshot",
    "/-/reload",
    "/-/quit",
    "/-/healthy",
    "/-/ready",
    "/federate",
    "/metrics",
    "/graph",
    "/query",
    "/alerts",
    "/rules",
    "/targets",
    "/config",
    "/flags",
    "/service-discovery",
    "/tsdb-status",
    "/status",
    "/version",
    "/debug/pprof/",
    "/debug/pprof/heap",
    "/debug/pprof/profile",
    "/api/v1/notifications",
    "/api/v1/notifications/live",
    "/api/v1/targets/metadata",
    "/api/v1/targets/relabel_steps",
    "/api/v1/scrape_pools",
    "/api/v1/alertmanagers",
    "/api/v1/status/walreplay",
    "/api/v1/status/tsdb/blocks",
    "/api/v1/features",
}


def manifest_documents() -> list[dict]:
    return [d for d in yaml.safe_load_all(GATEWAY_FILE.read_text("utf-8")) if d]


def of_kind(kind: str) -> dict:
    (found,) = [d for d in manifest_documents() if d["kind"] == kind]
    return found


def nginx_conf() -> str:
    return of_kind("ConfigMap")["data"]["nginx.conf"]


def deployment() -> dict:
    return of_kind("Deployment")


def pod_spec() -> dict:
    return deployment()["spec"]["template"]["spec"]


def container() -> dict:
    (found,) = pod_spec()["containers"]
    return found


def selects_pod(policy: dict, labels: dict[str, str]) -> bool:
    """Whether a policy's pod selector selects a pod with ``labels`` (matchLabels
    and the ``In`` expressions the namespace's policies use)."""
    selector = policy["spec"]["podSelector"]
    wanted = selector.get("matchLabels", {})
    if not all(labels.get(key) == value for key, value in wanted.items()):
        return False
    for expression in selector.get("matchExpressions", []):
        assert expression["operator"] == "In", expression
        if labels.get(expression["key"]) not in expression["values"]:
            return False
    return True


def parse_maps() -> dict[str, tuple[str, list[tuple[str, str]]]]:
    found = {}
    for source, name, body in MAP.findall(nginx_conf()):
        entries = []
        for entry in body.split(";"):
            entry = entry.strip()
            if entry:
                key, _, value = entry.rpartition(" ")
                entries.append((key.strip(), value))
        found[name] = (source.strip('"'), entries)
    return found


def class_of(raw: str) -> str:
    """r, w or x for a raw request URI, by the three maps in the order nginx
    evaluates them (a map is evaluated when its variable is first used)."""
    maps = parse_maps()
    source, entries = maps["$prometheus_class_raw"]
    assert source == "$request_uri"
    by_raw = evaluate(entries, raw)
    source, entries = maps["$prometheus_class_uri"]
    assert source == "$uri"
    by_uri = evaluate(entries, normalised(raw))
    source, entries = maps["$prometheus_class"]
    assert source == "$prometheus_class_raw:$prometheus_class_uri"
    return evaluate(entries, f"{by_raw}:{by_uri}")


def denied(raw: str, verify: str = "NONE", subject: str = "") -> bool:
    """Whether `if ($prometheus_denied) { return 403; }` fires for the request."""
    source, entries = parse_maps()["$prometheus_denied"]
    assert source == "$prometheus_class:$ssl_client_verify:$ssl_client_s_dn"
    return evaluate(entries, f"{class_of(raw)}:{verify}:{subject}") == "1"


# What nginx shows for the kinds of client: none, the collector's certificate, one
# of the authority's with another subject, and one of the authority's with no
# subject. (A certificate that does not verify, or that is for the wrong purpose,
# never reaches these maps: nginx ends the request with its own 400, seen on the
# pinned image; the maps still deny `FAILED:...` and an empty value on a write.)
NO_CERTIFICATE = ("NONE", "")
COLLECTORS = ("SUCCESS", WRITER)
OTHER_NAME = ("SUCCESS", "CN=somebody-else")
NO_NAME = ("SUCCESS", "")
NOT_VERIFIED = ("FAILED:unable to verify the first certificate", WRITER)
EVERY_CLIENT = [NO_CERTIFICATE, COLLECTORS, OTHER_NAME, NO_NAME, NOT_VERIFIED]
