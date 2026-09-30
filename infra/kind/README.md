# Local platform on kind

`make up` builds the local Meridian platform on a one-node kind cluster from
pinned Helm charts. `make smoke` proves it works. `make down` removes it.
Status: **implemented** (S006). Nothing here is deployed anywhere but your
laptop; the Azure side is S007 onward.

## What `make up` creates

| Component | Chart | Chart version | Namespace |
|---|---|---|---|
| Envoy Gateway (Gateway API edge) | `oci://docker.io/envoyproxy/gateway-helm` | v1.9.2 | `envoy-gateway-system` |
| CloudNativePG operator | `cloudnative-pg` | 0.29.1 (operator 1.30.1) | `cnpg-system` |
| PostgreSQL 17 with pgvector (`platform-db`) | `cluster` | 0.8.1 | `meridian` |
| Prometheus, Grafana, kube-state-metrics, node-exporter | `kube-prometheus-stack` | 91.8.2 (Grafana chart 13.2.7) | `observability` |
| Tempo (traces) | `tempo` | 3.1.0 | `observability` |
| Loki (logs) | `loki` | 18.13.7 | `observability` |
| OpenTelemetry Collector | `opentelemetry-collector` | 0.174.0 (collector 0.161.0) | `observability` |

Every version and image digest is in [`pins.env`](pins.env), the only place
to change one. The values that override chart defaults are in
[`values/`](values/), the Gateway and namespaces in [`manifests/`](manifests/).

How telemetry flows: an application sends OTLP to
`otel-collector.observability:4317` (gRPC) or `:4318` (HTTP). The collector
forwards traces to Tempo, metrics to Prometheus's OTLP receiver and logs to
Loki's OTLP endpoint. Grafana has three datasources with fixed uids:
`prometheus`, `tempo` and `loki`. Retention is 24 hours everywhere.

The database is a CloudNativePG `Cluster` named `platform-db` with one
instance and 2 Gi of storage. CloudNativePG generates the `app` credentials
(Secret `platform-db-app` in `meridian`); nothing is set by hand. The `vector`
extension is enabled declaratively by a `Database` resource.

The edge is a Gateway API `Gateway` named `edge` (class `envoy`) with one HTTP
listener. Only routes from the `meridian` namespace may attach. There is no
route yet, so it answers 404 to everything until S009.

## Prerequisites

Docker (running), `kind`, `kubectl`, `helm`, `openssl`. `make smoke` also
needs `curl` and `jq`. Tested with:

| Tool | Version |
|---|---|
| kind | v0.33.0 (node image Kubernetes v1.36.4) |
| helm | v4.3.0 |
| kubectl | v1.37.0 |
| Docker Desktop | 4.93.0 (engine 29.8.1), 7.65 GiB memory |

Give Docker at least 6 GiB. The first `make up` downloads about 30 images (the
node image, Kubernetes components and the platform).

## Commands

| Command | What it does |
|---|---|
| `make up` | Create the cluster if absent and install every release. Safe to rerun; it converges. Took 4 minutes from no cluster (245 s, node image already local), under a minute after. |
| `make smoke` | One PASS or FAIL line per check; exits non-zero on any FAIL. |
| `make grafana` | Port-forward Grafana to <http://127.0.0.1:3000>. User `admin`. |
| `make grafana-password` | Print the Grafana admin password. |
| `make down` | Delete the `meridian` cluster and its credentials file. Destructive; refuses any other cluster name. |

`make smoke` checks three things:

1. **Edge.** `curl http://127.0.0.1:8088/` returns 404, and Envoy's own
   request counter went up. That covers laptop, kind port mapping, NodePort
   and Envoy.
2. **Database.** `pg_extension` in `platform-db` lists `vector`.
3. **Telemetry.** Three short Jobs run `telemetrygen` and send one trace, one
   log and one metric for a fresh service name (`meridian-smoke-<epoch>`)
   through the collector. The script then reads each back through Grafana's
   datasource proxy from Tempo, Loki and Prometheus, waiting up to 120 seconds
   each. It prints the trace ID and how to find the data in Grafana Explore.

`make smoke` creates three Jobs in `observability`. Kubernetes removes each one
15 minutes after it finishes.

## If `make up` was interrupted

Rerunning `make up` is the first thing to try. If a release is stuck in a
`pending-*` state, or the node was only half created, run `make down` and then
`make up` again.

## Ports

| Address | What |
|---|---|
| `127.0.0.1:8088` | The edge (Envoy), from kind's `extraPortMappings` to node port 30080. Loopback only. |
| `127.0.0.1:3000` | Grafana, only while `make grafana` runs. |

Grafana is deliberately not behind the gateway (threat model T-03). It has
sign-in, sign-up is off, anonymous access is off. Its admin password is
generated once by `make up` into the Secret `grafana-admin` and is never
written to a file or printed by any script except `make grafana-password`.

## Using kubectl and helm

The cluster's credentials are in `infra/kind/kubeconfig` (gitignored). The
scripts never touch `~/.kube/config`, never rely on the current context, and
never run `helm repo add`. To work by hand:

```sh
export KUBECONFIG=$PWD/infra/kind/kubeconfig
kubectl get pods -A
```

## Memory

The laptop this was built on gives Docker Desktop 7.65 GiB. The memory limits
of the components add up to about 4.4 GiB. Measured with `docker stats` on the
node container on 2026-09-30, after a `make up` from no cluster, a second
`make up` and one `make smoke`: 3.6 GiB, which includes the Kubernetes control
plane, the kubelet and containerd.

## Deliberately not here yet

- TLS on the gateway, NetworkPolicy and hardened security contexts: S019.
- Application routes: the first HTTPRoute arrives with S009.
- Alertmanager: S024. Its Grafana datasource is off too.
- Persistence beyond the node: PostgreSQL, Loki and Tempo use small volumes on
  the node's disk, Prometheus and Grafana use none. `make down` removes all of
  it.
- Scraping of etcd, controller-manager, scheduler and kube-proxy is off: kind
  does not expose them to the cluster.
- Helm installs the CRDs a chart ships in its `crds/` folder but never
  upgrades them. A chart bump that changes CRDs needs them applied by hand.
