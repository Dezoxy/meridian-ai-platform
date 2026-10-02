# Local platform on kind

`make up` builds the local Meridian platform on a one-node kind cluster from
pinned Helm charts. `make smoke` proves it works. `make deploy` puts the S009
walking skeleton (Claims API, Agent Runtime, Model Gateway) on it and
`make demo` runs a claim through it. `make down` removes it.
Status: **implemented** (S006, and S041 for deploy and demo). Nothing here is
deployed anywhere but your laptop; the Azure side is S007 onward. The services
run in replay mode: no model is called, and the model's text is canned and
simulated.

**`make demo` fails since S014 and until S044.** The triage graph now calls
the policy and knowledge tool servers first, and they are not deployed on
kind yet: the run fails, the Claims API answers 502 and the demo stops. The
claim stays stored and can be posted again once the servers run here.

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
extension is enabled declaratively by a `Database` resource. The image ships
PostgreSQL 17.11 with pgvector 0.8.6 (read from the image on 2026-10-02).

For the walking skeleton `make up` also declares a second database,
`meridian`, owned by the role `meridian_owner`, and six more roles:
`claims_api`, `agent_runtime`, `model_gateway`, for the tool servers (S013)
`policy_mcp` and `claims_mcp`, and for the knowledge server (S046)
`knowledge_mcp`. All seven can log in and nothing more (no superuser, createdb
or createrole). The three tool-server roles are declared here because
migrations 0004 and 0006 need them to exist; they have been checked by the
manifest tests only, never on a cluster. Reconciling them and running the
tool servers on kind is S044. The `app` database, role and Secret are
untouched. The `meridian` database declares the `vector` extension too
(S012): migration 0005 needs it, and `meridian_owner` cannot create an
extension PostgreSQL does not trust. This declaration has been checked by
the manifest tests only, never on a cluster; `make smoke` still looks for
the extension in `app` alone, and on a cluster older than this change the
migration can run before the operator has created it (S044). Each role's
password is in a Secret of type `kubernetes.io/basic-auth` in `meridian`, with
the keys `username`, `password` and `uri`: `meridian-owner-db`,
`claims-api-db`, `agent-runtime-db`, `model-gateway-db`, `policy-mcp-db`,
`claims-mcp-db` and `knowledge-mcp-db`. `make up` creates a Secret only if
it is absent, before the
`platform-db` release installs (CloudNativePG cannot reconcile a role whose
Secret is missing), from `openssl rand -hex 24`. The password goes to `kubectl`
on stdin; it is never an argument, never in a file and never printed. The `uri`
names `platform-db-rw.meridian.svc`, which is in the server certificate, and
asks for `sslmode=verify-full`. To read one, which you rarely need:

```sh
kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian \
  -n meridian get secret claims-api-db -o jsonpath='{.data.uri}' | base64 -d
```

Rotating a password is the owner's call and `make up` never overwrites a
Secret. Both `password` and `uri` (it embeds the password) must change
together, CloudNativePG then applies the new password to the role, and the
three Deployments must restart to read it; the migration Job reads its Secret
afresh on every `make deploy`:

```sh
kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian \
  rollout restart deploy/claims-api deploy/agent-runtime deploy/model-gateway
```

PostgreSQL itself enforces the database boundary, with `pg_hba` rules in
[`values/platform-db.yaml`](values/platform-db.yaml) that CloudNativePG places
before its default catch-all, after its own local, replication and pooler
rules: a connection without TLS is rejected; the six roles may log in to
`meridian` over TLS with a SCRAM password and to no other database; no other
role may log in to `meridian`. A client that asks for `sslmode=disable`, or a
service that is pointed at the `app` or `postgres` database, is refused by the
server, whatever its connection string says.

The edge is a Gateway API `Gateway` named `edge` (class `envoy`) with one HTTP
listener. Only routes from the `meridian` namespace may attach. `make deploy`
adds one route, for the Claims API only (below); every other host and path
answers 404.

## Prerequisites

Docker (running), `kind`, `kubectl`, `helm`, `openssl` and `jq`. `make smoke`
and `make demo` also need `curl`. Tested with:

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
| `make deploy` | Needs `make up`. Builds the image, loads it into the node, runs the migration Job, applies the manifests in `manifests/meridian/` and waits for the three Deployments and the route. Safe to rerun. |
| `make demo` | Runs `make deploy`, then posts a synthetic claim and finds its trace in Tempo. Prints PASS only when the trace has spans from all three services. Fails until S044 (see above). |
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

## The walking skeleton: `make deploy` and `make demo`

One image, built from the [`Dockerfile`](../../Dockerfile) at the repository
root, runs all three services and the migration command. It is based on
`python:3.13-slim` and `uv`, both pinned by digest, installs the locked
dependencies without the dev group and the package non-editable into a venv,
runs as user 10001 and sets no command of its own (each manifest names it).
Its build context is an allowlist ([`.dockerignore`](../../.dockerignore)), so
the kubeconfig, `.env` files and `.context/` can never enter it; key and
certificate files are excluded even inside the allowed folders. There is no
registry: `make deploy` tags the image `meridian:<first 12 hex digits of its
ID>` and loads it into the kind node, so an unchanged tree reuses the same
tag.

| Service | Reached at | Database role |
|---|---|---|
| Claims API | `http://claims.meridian.localhost:8088` (edge) and `claims-api.meridian.svc:8000` | `claims_api` |
| Agent Runtime | `agent-runtime.meridian.svc:8000`, cluster only | `agent_runtime` |
| Model Gateway | `model-gateway.meridian.svc:8000`, cluster only | `model_gateway` |

In order, `make deploy`:

1. Refuses with "run 'make up' first" if the `meridian` Database is not
   applied, a role is not reconciled or a role Secret is missing.
2. Builds and loads the image.
3. Runs a Job `meridian-migrate-<tag>` with `meridian db migrate`, as
   `meridian_owner`. Only this Job reads that Secret. It must finish before
   anything else is applied; on failure the script prints the Job's log and
   exits non-zero. The Job runs on every deploy (a Job of the same tag is
   deleted first; the runner skips what is applied, so a rerun takes seconds)
   and ends after 300 seconds at most.
4. Applies every other file in `manifests/meridian/` (found by glob: the
   ServiceAccounts, Deployments, Services, the HTTPRoute and the
   BackendTrafficPolicy), and waits for the rollouts and for the route to be
   `Accepted`. `@IMAGE@` (and `@TAG@` in the Job's name) are the only text it
   substitutes.

Each pod gets its own role's connection string from its Secret, and the
cluster CA's public certificate (`ca.crt` only, not the CA's private key that
shares the Secret `platform-db-ca`) at `/etc/meridian/db-ca/ca.crt`. Spans go
to the collector's OTLP/HTTP port, `:4318`; the `/healthz` probes are not
traced. The `*.localhost` name resolves to the loopback address on macOS and
on current Linux resolvers; the edge listens on `127.0.0.1` only.

The route is the whole boundary: the Claims API is the only service with one,
on the host `claims.meridian.localhost`. A request with any other `Host`
header, such as `127.0.0.1:8088` or a page that rebinds its DNS name to
loopback (threat model T-01), matches no route and gets 404 from Envoy.
Envoy buffers each request to the Claims API and answers 413 above 64 KiB, the
app's own limit, before the app sees it. TLS, NetworkPolicy and hardened
charts are S019.

A `ClusterIP` Service means unrouted, not protected: the Agent Runtime and the
Model Gateway have no route at the edge, but any pod in the cluster can call
them until S019's NetworkPolicy lands. The database boundary above does not
depend on that.

`make demo` posts the claims in `data/synthetic/claims.json` in order to the
Claims API through the edge, with a W3C `traceparent` header whose trace ID the
script made up. A claim that already has a triage proposal answers 409 and the
script moves on to the next one, so each run uses the next claim (40 are
available); it stops at the first 201. It prints the claim ID, the status, the
route and the deployment the model call went to, or that no model was called
(never a claimant field, and not the reason: the answer does not carry it),
then reads the trace by that ID from Tempo through Grafana's datasource proxy,
retrying for up to 120 seconds. It prints PASS only if the trace has spans from
`claims-api`, `agent-runtime` and `model-gateway`, with the span count of each,
and exits non-zero otherwise. `make grafana` shows it in Explore with the
TraceQL query `{ trace:id = "<id>" }`.

## If `make up` was interrupted

Rerunning `make up` is the first thing to try. If a release is stuck in a
`pending-*` state, or the node was only half created, run `make down` and then
`make up` again.

If `make up` times out waiting for the Gateway to be programmed and
`kubectl -n envoy-gateway-system get gateway edge` shows `AddressNotAssigned`,
Envoy Gateway did not pick up the node's address (seen on 2026-10-01 on a
cluster that had run for 18 hours; the edge served routes anyway). Restart the
controller, wait for it, then touch the Gateway:

```sh
K="kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n envoy-gateway-system"
$K rollout restart deploy/envoy-gateway
$K rollout status deploy/envoy-gateway
$K annotate gateway edge meridian.local/reconcile-nudge="$(date -u +%FT%TZ)" --overwrite
```

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
plane, the kubelet and containerd. The three Meridian services add limits of
192, 256 and 192 MiB (their measured peak was 57 to 87 MB after two demo
claims); the migration Job adds 192 MiB while it runs.

## Deliberately not here yet

- TLS on the gateway, NetworkPolicy, PodDisruptionBudgets, read-only root
  filesystems and Helm charts for the Meridian services: S019.
- Alertmanager: S024. Its Grafana datasource is off too.
- Persistence beyond the node: PostgreSQL, Loki and Tempo use small volumes on
  the node's disk, Prometheus and Grafana use none. `make down` removes all of
  it.
- Scraping of etcd, controller-manager, scheduler and kube-proxy is off: kind
  does not expose them to the cluster.
- Helm installs the CRDs a chart ships in its `crds/` folder but never
  upgrades them. A chart bump that changes CRDs needs them applied by hand.
