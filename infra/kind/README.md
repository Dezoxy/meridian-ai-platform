# Local platform on kind

`make up` builds the local Meridian platform on a one-node kind cluster from
pinned Helm charts. `make smoke` proves it works. `make deploy` puts the six
Meridian services on it (Claims API, Agent Runtime, Model Gateway and the
policy, claims and knowledge tool servers), seeds the policy store and ingests
the policy wordings; `make demo` runs a claim through them. `make down`
removes it.
Status: **implemented** (S006, S041 for deploy and demo, S044 for the tool
servers, S043 for the cost dashboard, S015 for the adjuster's decision,
S016 for the adjuster's pages, S019 for the Helm chart and its hardening).
Nothing here is deployed anywhere but a local cluster (the laptop it was
built on, and on 2026-10-06 a Linux virtual machine); the Azure side is S007
onward. The services run in replay mode: no model is called, the model's
text is canned and simulated, and so are the embeddings. A triage that asks
the model its one question therefore gets no usable answer and goes to an
adjuster; the rules decide every other claim.

## What `make up` creates

| Component | Chart | Chart version | Namespace |
|---|---|---|---|
| Envoy Gateway (Gateway API edge) | `oci://docker.io/envoyproxy/gateway-helm` | v1.9.2 | `envoy-gateway-system` |
| cert-manager (with the CA for the services) | `cert-manager` (`https://charts.jetstack.io`) | v1.21.2 | `cert-manager` |
| approver-policy (decides which certificate requests are approved) | `cert-manager-approver-policy` (`https://charts.jetstack.io`) | v0.28.0 | `cert-manager` |
| CloudNativePG operator | `cloudnative-pg` | 0.29.1 (operator 1.30.1) | `cnpg-system` |
| PostgreSQL 17 with pgvector (`platform-db`) | `cluster` | 0.8.1 | `meridian` |
| Prometheus, Grafana, kube-state-metrics (node-exporter is off, see below) | `kube-prometheus-stack` | 91.8.2 (Grafana chart 13.2.7) | `observability` |
| Tempo (traces) | `tempo` | 3.1.0 | `observability` |
| Loki (logs) | `loki` | 18.13.7 | `observability` |
| OpenTelemetry Collector | `opentelemetry-collector` | 0.174.0 (collector 0.162.0) | `observability` |

Every version and image digest is in [`pins.env`](pins.env), the only place
to change one. `.github/renovate.json` reads them, so Renovate, once the
owner has installed the app, proposes newer ones in grouped pull requests
each month. CI does not start this platform: such a pull request needs
`make up` and `make smoke` before it merges, and the table above follows
by hand. The values that override chart defaults are in
[`values/`](values/); the Gateway, the namespaces, Grafana's Role and the
NetworkPolicies of the database, `cert-manager`, `observability` and smoke's
Jobs are in [`manifests/`](manifests/). The Meridian
services have a chart of their own, [`../helm/meridian/`](../helm/meridian/),
which `make deploy` installs (below).

### The images the charts run (S063)

Pinned means a digest, or an image that is never pulled (S019). Since S063
every image a chart starts here is pinned by the multi-architecture index
digest of its tag, each in [`pins.env`](pins.env) as `X_IMAGE_TAG` and
`X_IMAGE_DIGEST` under a `# renovate:` comment, and `make up` passes both to
the chart with `--set`. The tag is the one the chart installs by default at
its pinned version, except the collector's: the chart's appVersion is 0.161.0
and the pin is 0.162.0 (it was pinned before this step). A chart upgrade moves
the tags with it, and Renovate
proposes a tag of its own that may not be the chart's, so read the chart's
defaults again (below) before merging. What a chart's key takes is not
uniform: `digest` takes `sha256:<hex>`; `sha` in kube-prometheus-stack and
Grafana takes the hex alone (their templates write `@sha256:` themselves) and
`up.sh` strips the prefix; `sha` in kube-state-metrics takes the whole digest;
Tempo and CloudNativePG have no digest key, so the tag key carries
`tag@digest`; Loki and the collector print the reference without the tag. The
tests in `tests/meridian/test_kind_platform_images.py` hold what the files
say; they cannot hold that this list is complete, which needs the network.
Status: pins and tests implemented and tested without a cluster; the
digests were read from the registries on 2026-10-06 (each is an index with
`linux/amd64` and `linux/arm64`).

| Release | Image (digest in `pins.env`) | Where the chart takes it | By digest |
|---|---|---|---|
| envoy-gateway | `docker.io/envoyproxy/gateway:v1.9.2` | `global.images.envoyGateway.image`: the controller, the certgen hook Job and the proxy's shutdown manager (named in the controller's configuration) | yes |
| envoy-gateway | `docker.io/envoyproxy/envoy:distroless-v1.39.1` | the proxy pods: the controller's built-in default (v1.9.2 source), a digest already; `manifests/gateway.yaml` names none | yes, not set here |
| envoy-gateway | `docker.io/envoyproxy/ratelimit:0482748e` | `global.images.ratelimit.image`, in the controller's configuration; started only for a global rate-limit policy, and there is none | left by tag, never started |
| cert-manager | `quay.io/jetstack/cert-manager-controller:v1.21.2` | `image.tag`, `image.digest` | yes |
| cert-manager | `quay.io/jetstack/cert-manager-webhook:v1.21.2` | `webhook.image.tag`, `.digest` | yes |
| cert-manager | `quay.io/jetstack/cert-manager-cainjector:v1.21.2` | `cainjector.image.tag`, `.digest` | yes |
| cert-manager | `quay.io/jetstack/cert-manager-startupapicheck:v1.21.2` | `startupapicheck.image.tag`, `.digest` (a post-install hook Job) | yes |
| cert-manager | `quay.io/jetstack/cert-manager-acmesolver:v1.21.2` | the controller's flag `--acme-http01-solver-image`; `acmesolver.image.digest` exists | left by tag, never started (no ACME issuer) |
| approver-policy | `quay.io/jetstack/cert-manager-approver-policy:v0.28.0` | `image.tag`, `image.digest` | yes |
| cnpg | `ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1` | `image.tag` as `tag@digest`; the chart passes it as `OPERATOR_IMAGE_NAME` too, so the init container of every database pod is by digest as well | yes, through the tag key |
| platform-db | `ghcr.io/cloudnative-pg/postgresql:17.11-standard-trixie` | `cluster.imageName` (`POSTGRES_IMAGE`) | yes |
| platform-db | `alpine:3.17` | the chart's `helm test` Job, started by `helm test` only | left by tag, never started |
| kube-prometheus-stack | `quay.io/prometheus-operator/prometheus-operator:v0.94.1` | `prometheusOperator.image.tag`, `.sha` | yes |
| kube-prometheus-stack | `quay.io/prometheus-operator/prometheus-config-reloader:v0.94.1` | `prometheusOperator.prometheusConfigReloader.image.tag`, `.sha` (the operator's flag `--prometheus-config-reloader`; the sidecar of Prometheus) | yes |
| kube-prometheus-stack | `ghcr.io/jkroepke/kube-webhook-certgen:1.8.9` | `prometheusOperator.admissionWebhooks.patch.image.tag`, `.sha` (the create and patch hook Jobs) | yes |
| kube-prometheus-stack | `quay.io/prometheus/prometheus:v3.15.0-distroless` | `prometheus.prometheusSpec.image.tag`, `.sha` (a field of the Prometheus resource) | yes |
| kube-prometheus-stack | `registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.20.0` | `kube-state-metrics.image.tag`, `.sha` | yes |
| kube-prometheus-stack | `docker.io/grafana/grafana:13.2.3-distroless` | `grafana.image.tag`, `.sha` | yes |
| kube-prometheus-stack | `quay.io/kiwigrid/k8s-sidecar:2.11.2` | `grafana.sidecar.image.tag`, `.sha` (both sidecar containers) | yes |
| kube-prometheus-stack | `quay.io/thanos/thanos:v0.42.4` | the operator's flag `--thanos-default-base-image`; `prometheusOperator.thanosImage.sha` exists | left by tag, never started (no Thanos sidecar) |
| kube-prometheus-stack | `quay.io/prometheus/node-exporter:v1.12.1-distroless` | `prometheus-node-exporter.image.digest` | not pinned: switched off on kind (S063); on again, it needs a pin |
| tempo | `docker.io/grafana/tempo:3.1.0` | `tempo.tag` as `tag@digest` | yes, through the tag key |
| loki | `docker.io/grafana/loki:3.7.8` | `loki.image.tag`, `.digest` | yes |
| otel-collector | `ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector:0.162.0` | `image.repository`, `.tag`, `.digest` (before S063) | yes |

The Prometheus tag is also the Makefile's `PROMTOOL_IMAGE`. Every image left
by tag is one that nothing starts: if an ACME issuer, a rate-limit policy, a
Thanos sidecar or `helm test` is added, its image needs a pin first.

To read what a chart installs by default (its tags must be the ones in
`pins.env`), render it without the `--set` arguments, here for cert-manager;
the other releases take their chart, repository, version and values file from
`up.sh` the same way. With the `--set` arguments `up.sh` passes, every line
this prints ends in a digest, except the images the table says are left by tag
or not pinned:

```sh
set -a; source infra/kind/pins.env; set +a
helm template cert-manager "${CERT_MANAGER_CHART}" --repo "${CERT_MANAGER_REPO}" \
  --version "${CERT_MANAGER_VERSION}" --namespace cert-manager \
  --values infra/kind/values/cert-manager.yaml |
  grep -oE '(docker\.io|quay\.io|ghcr\.io|registry\.k8s\.io)/[A-Za-z0-9._/-]+(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?' |
  sort -u
```

On the cluster, after `make up` (with `KUBECONFIG` set as `make up` prints),
this prints one line for each container, init containers included, outside
the node's own namespaces, that runs an image without a digest. It should
print only containers of `meridian` pods whose image is the local
`meridian:<id>` (their pull policy is `Never`; the image never leaves the
node):

```sh
kubectl get pods -A -o go-template='{{range .items}}{{$pod := printf "%s/%s" .metadata.namespace .metadata.name}}{{range .spec.initContainers}}{{$pod}} {{.image}}{{"\n"}}{{end}}{{range .spec.containers}}{{$pod}} {{.image}}{{"\n"}}{{end}}{{end}}' |
  grep -vE '^(kube-system|local-path-storage)/' | grep -v '@sha256:'
```

`make up` on a running cluster installs each release again in place
(`helm upgrade --install`): the Deployments and StatefulSets whose image
reference changed roll once.

How telemetry flows: an application sends OTLP over HTTP with TLS to
`otel-collector.observability:4318` (S063: the gRPC port, 4317, is closed, and
the collector's certificate comes from an authority of its own, below). The
collector forwards traces to Tempo, metrics to Prometheus's OTLP receiver and
logs to Loki's OTLP endpoint, and those three hops stay clear text: they are
inside `observability`, and the threat model names the hop to the collector.
Grafana has three datasources with fixed uids:
`prometheus`, `tempo` and `loki`. Retention is 24 hours everywhere.

node-exporter is off on kind (S063). It is the one pod of `observability` that
needs the node's own network and PID namespaces and `/proc` and `/sys` from the
host, which would hold the namespace's Pod Security level at `privileged`, and
no alert rule and no dashboard of this repository reads a `node_*` series (a
test keeps it so). So the chart's own node-exporter dashboards show no data on
kind, and are not installed at all: the switch (`nodeExporter.enabled` in
[`values/kube-prometheus-stack.yaml`](values/kube-prometheus-stack.yaml))
drops them with the DaemonSet, and the four rule groups that read its series
are turned off too (`node.rules` stays: one of its five rules is
kube-state-metrics', and the other four record nothing without node-exporter).
The "Compute Resources" dashboards read kube-state-metrics and cAdvisor and
stay. A node's CPU, memory or disk is therefore not observed on kind; turn
node-exporter on again when a rule needs it, and give it a namespace of its
own or label `observability` `privileged`.

Grafana reads ConfigMaps in its own namespace and nothing else (S043). The
chart's defaults let its dashboard sidecar watch every namespace, which gave
Grafana's service account a ClusterRole to read every ConfigMap and Secret
in the cluster, the database roles' passwords among them, and the chart's
namespaced Role would still add Secrets. So the chart creates no RBAC for
Grafana; [`manifests/grafana-rbac.yaml`](manifests/grafana-rbac.yaml) gives
it a Role in `observability` that reads ConfigMaps only, and both sidecars
watch that namespace only (threat model T-68). `make smoke` checks it.

kube-state-metrics reads no Secret either (S063, T-68); the Prometheus
operator still does. Rendered at chart 91.8.2 with these values, the chart
gave kube-state-metrics a ClusterRole whose only rule for Secrets was `list`
and `watch` in every namespace, there because its `secrets` collector is on
by default. `collectorsExclude: [secrets]` in
[`values/kube-prometheus-stack.yaml`](values/kube-prometheus-stack.yaml)
turns that collector off, the chart derives its rules from the collectors, and
the rendered ClusterRole has 27 rules instead of 28, none for Secrets. Every
other collector stays the chart's. What goes quiet: the `kube_secret_*`
family of series is no longer exported. Nothing reads it: no
alert rule and no dashboard of this repository does (a test keeps it so), and
a search of the chart's own default rules and dashboards, rendered with these
values, found no `kube_secret` at all, so no panel or alert of the chart goes
quiet. The series the repository reads
(`kube_deployment_status_replicas_available`, `kube_pod_status_ready`,
`kube_cronjob_status_last_successful_time`, `kube_cronjob_created` and
`kube_pod_container_status_restarts_total`) come from collectors that stay.
`make smoke` asks the API server about it (check 5). Tested without a
cluster; not yet run on one.

The Prometheus operator keeps its ClusterRole, which reads, creates and
changes Secrets and ConfigMaps in every namespace (the rule is `get`, `list`,
`watch`, `create`, `update`, `delete`): the operator writes the generated
configuration of Prometheus as Secrets in `observability`, and reads the
Secrets that a ServiceMonitor names in the ServiceMonitor's own namespace.
The chart at this version has no value for a namespaced Role: the
ClusterRole and its binding are rendered whenever the operator and
`global.rbac.create` are on, and `global.rbac.create: false` would drop
every Role of the chart (Prometheus's, the admission webhook's and the
operator's), to be written by hand, well beyond Grafana's one rule.
`prometheusOperator.namespaces` narrows only what the operator watches (the
flag `--namespaces=`, rendered and read), not what its ClusterRole may read,
and it would stop Prometheus from picking up a ServiceMonitor or PrometheusRule
outside the namespace it names; a test keeps it unset. So the operator's
read of Secrets stays open (T-68's residual), and `make smoke` does not
check it.

The CA for the services (S055) is three cert-manager objects in
[`manifests/service-ca.yaml`](manifests/service-ca.yaml): a self-signed
issuer, a CA certificate (ECDSA P-256, one year) and the `meridian-services`
issuer that signs one certificate per service. The services use those
certificates to prove to one another which service they are, by mutual TLS
(the chart's part is under "Who a service is" below). The CA's private key is
a Secret in the `cert-manager` namespace, outside `meridian`: no Meridian pod
can read it, and the operators that hold a cluster-wide read of Secrets can
(cert-manager's controller and cainjector, the CloudNativePG operator). The
CA keeps its key at its renewal by an explicit `rotationPolicy: Never`, because
cert-manager's default has been `Always` since v1.18.0 and a new key would
leave a restarted pod distrusting the pods that had not.

Who may ask for a certificate (S056, threat T-88) is decided by cert-manager's
approver-policy, not by cert-manager: its built-in approver, which approves
every request, is switched off (`disableAutoApproval` in
[`values/cert-manager.yaml`](values/cert-manager.yaml)). Five
`CertificateRequestPolicy` objects in
[`manifests/certificate-policy.yaml`](manifests/certificate-policy.yaml) apply
to the four issuers (the two of the services' CA and the two of the
collector's), and approver-policy may act for no other signer
([`values/approver-policy.yaml`](values/approver-policy.yaml)):

- `meridian-services` permits a request for the `meridian-services` issuer
  only from the `meridian` namespace, with a URI under
  `spiffe://meridian.kind/ns/meridian/sa/`, a `*.meridian.svc` DNS name, the
  three usages the services use (digital signature, client auth, server auth)
  and at most 90 days; a CA, a common name or any other field is not allowed.
- `meridian-services-ca` permits the CA certificate's own request (issuer
  `meridian-selfsigned`, namespace `cert-manager`, common name
  `meridian-services-ca`, `isCA`, at most a year); without it the CA's renewal
  would wait for ever.
- `meridian-deny-unlisted` selects a request for either Meridian issuer from
  any namespace and permits none that names anything, so a request that no
  other policy permits is denied, not left waiting. A request for any other
  issuer meets no policy and is never approved.
- `telemetry-ca` and `otel-collector` (S063, below) are the same for the
  collector's own authority, each selecting one namespaced `Issuer` in
  `observability`: the first permits the authority's request (common name
  `telemetry-ca`, `isCA`, at most a year), the second the collector's server
  certificate (its two DNS names, the usages digital signature and server
  auth, at most 90 days, no URI and no CA). Each is also what denies a
  request for its issuer that it does not permit: no policy of the deny kind
  is needed.

The namespace limit is held twice: by each policy's selector and by where its
binding is. cert-manager's account may `use` the four policies that allow
through a Role and RoleBinding in one namespace each, and the one that denies
through a ClusterRoleBinding, so a request from another namespace meets only
the policy that denies. What is left: whoever can create a `Certificate` in
`meridian` has any service's identity issued (the policy checks the namespace
and the URI prefix, not which service), and whoever can change a policy or its
binding undoes the limit; on kind that is the cluster's administrator.

`make up` installs approver-policy and applies the policies before the CA,
waits for the five to be Ready, and then waits for the issuer to be Ready
before it installs the database. On a cluster where cert-manager already ran
with its approver on, `make up` turns the approver off first and brings the
policies seconds later: the certificates already issued are not touched, and
a request made in between waits and is then decided.

`make up` also provisions the dashboards in [`dashboards/`](dashboards/), one
ConfigMap each in `observability`, and applies Meridian's alert rules in
[`alerts/`](alerts/), one `PrometheusRule` (both below).

The database is a CloudNativePG `Cluster` named `platform-db` with one
instance and 2 Gi of storage. CloudNativePG generates the `app` credentials
(Secret `platform-db-app` in `meridian`); nothing is set by hand. The `vector`
extension is enabled declaratively by a `Database` resource. The image ships
PostgreSQL 17.11 with pgvector 0.8.6 (read from the image on 2026-10-02).

For the walking skeleton `make up` also declares a second database,
`meridian`, owned by the role `meridian_owner`, and ten more roles:
`claims_api`, `agent_runtime`, `model_gateway`, for the tool servers (S013)
`policy_mcp` and `claims_mcp`, for the knowledge server (S046)
`knowledge_mcp`, for the scheduled sweep (S052, below) `claims_sweep`, for
the upkeep of the gateway's ledger (S066) `gateway_upkeep` and for the seed
and the ingestion Jobs (S063) `policy_seed` and `knowledge_ingest`. All eleven
can log in and nothing more (no superuser, createdb, createrole, bypassrls or
replication, and a member of no role: migrations 0020 and 0022 refuse the
three newest otherwise). The seed Job runs as `policy_seed`, which holds the
two policy tables and nothing else, and the ingestion Job as
`knowledge_ingest`, which holds the knowledge chunks and an insert on the
audit log: only the migration Job holds the owner's Secret, and each of the
other two Jobs alone holds its role's. Those two roles have no connection
limit, as the owner's Jobs had none. The sweep's role may hold at most 4
connections: its job runs one pod at a time and holds one connection at a
time, a run by hand beside the scheduled one makes two pods, and each may open
a second connection while it replaces a broken one. The upkeep role may hold at
most 2: the command opens one connection for milliseconds. That bounds what a
holder of the credential can hold open; it does not stop one session from
sitting in an open transaction. Only an operator uses `gateway_upkeep`, from a
terminal (the runbook
[budget exhaustion](../../docs/operations/runbooks/budget-exhaustion.md#the-upkeep-command)
says how); no workload of the chart holds its Secret `gateway-upkeep-db`, and a
test keeps it so. The three tool-server roles may each hold at most 20
connections: a tool server runs at most eight calls at once, one connection
each and one more for a failure's audit row, and during a rollout two of its
pods run side by side; a runaway server cannot use up PostgreSQL's 100. The
other roles have no such bound until they get a connection pool (S027). The `app`
database, role and Secret are untouched. The `meridian` database declares the
`vector` extension too (S012): migration 0005 needs it, and `meridian_owner`
cannot create an extension PostgreSQL does not trust. `make up` waits until
the operator reports the database applied, which includes the extension, and
`make deploy` refuses to start before that, so the migration never runs
first; `make smoke` looks for the extension in both databases. Each role's
password is in a Secret of type `kubernetes.io/basic-auth` in `meridian`, with
the keys `username`, `password` and `uri`: `meridian-owner-db`,
`claims-api-db`, `agent-runtime-db`, `model-gateway-db`, `policy-mcp-db`,
`claims-mcp-db`, `knowledge-mcp-db`, `claims-sweep-db`, `gateway-upkeep-db`,
`policy-seed-db` and `knowledge-ingest-db`. `make up` creates
a Secret only if it is absent, before the
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
Deployment that uses the role must restart to read it; the Jobs read their
Secrets (the owner's, the seed's and the ingestion's) afresh on every
`make deploy`, and the sweep's every run reads its own:

```sh
kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian \
  rollout restart deploy/claims-api deploy/agent-runtime deploy/model-gateway \
  deploy/policy-mcp deploy/claims-mcp deploy/knowledge-mcp
```

PostgreSQL itself enforces the database boundary, with `pg_hba` rules in
[`values/platform-db.yaml`](values/platform-db.yaml) that CloudNativePG places
before its default catch-all, after its own local, replication and pooler
rules: a connection without TLS is rejected; the eleven roles may log in to
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

| Tool | A laptop | A Linux virtual machine (2026-10-06) |
|---|---|---|
| kind | v0.33.0 (node image Kubernetes v1.36.4) | v0.33.0 (node image Kubernetes v1.36.4, as pinned) |
| helm | v4.3.0 | v4.3.0 |
| kubectl | v1.37.0 | v1.37.1 |
| Docker | Docker Desktop 4.93.0 (engine 29.8.1), 7.65 GiB memory | Docker Engine 29.8.2, rootless, not Docker Desktop |
| Processor | arm64 | amd64 |

On the virtual machine every image the cluster pins resolved on amd64:
`make up` from nothing ended with every release installed (5 min 04 s the
first time, 4 min 28 s the second, with the images on the machine), then
`make deploy`, `make demo` and `make smoke` passed.

Give Docker at least 6 GiB. The first `make up` downloads about 30 images (the
node image, Kubernetes components and the platform).

## Commands

| Command | What it does |
|---|---|
| `make up` | Create the cluster if absent, install every release, provision the Grafana dashboards and apply the alert rules. Safe to rerun; it converges. Took 4 to 5 minutes from no cluster (245 s and 304 s, images already local, on the laptop; 5 min 04 s and 4 min 28 s on the Linux machine of the table above), under a minute after. |
| `make deploy` | Needs `make up`. Builds the image, loads it into the node, runs the migration and seed Jobs, installs or upgrades the Helm release `meridian` from [`../helm/meridian/`](../helm/meridian/) (the sweep's CronJob and the network policies among its objects), ingests the wordings once per image and waits for the six Deployments and the route. Safe to rerun. The first deploy of an image waits a minute after the ingestion (below). |
| `make images` | Lists the `meridian:*` images in the Docker engine and in the kind node, each marked `in use` (a pod template of the namespace's Deployments, CronJobs and Jobs names it, or a Pod that exists), `rollback` (only an old ReplicaSet names it: a rollback's target, kept, with no command) or `unused`, with the counts and the size Docker reports, and prints the commands that would remove the unused ones. It removes nothing: removing them is the owner's command. With no `infra/kind/kubeconfig` it asks kind: no cluster of that name, and it lists the engine's images, all unused; a cluster that exists (the credentials are in another checkout) is an error, because it cannot tell which images are in use. A cluster that does not answer, or a listing that fails, is an error too. Run on the cluster on 2026-10-06: after three deploys it listed three `meridian:*` images in the engine and in the node, one `in use` and two `rollback` (an old ReplicaSet names each), kept with no removal command, and removed nothing; on the cluster made again from nothing it listed three in the engine, one in use and two unused (no ReplicaSet of the new cluster names them) with the `docker image rm` line printed for them, and one in the node. The refusal in a checkout without the cluster's credentials was tested against stub commands and not tried on the cluster. |
| `make helm-lint` | `helm lint --strict` on the chart with kind's values and every Job on. Needs no cluster; CI runs it. |
| `make alerts` | Prometheus's own checker (`promtool`, from a pinned image) on the alert rules, then their unit tests. Needs Docker and no cluster; CI runs it. |
| `make demo` | Runs `make deploy`, then posts a synthetic claim and finds its trace in Tempo; when the claim waits for an adjuster, posts the decision (`make demo DECISION=reject`; approve by default) and finds that trace too. Prints PASS only when each trace has at least one span from each service it must cross (a service Tempo lists with no span does not count) and its span counts have settled (unchanged for three readings, six seconds). Passed on the cluster on 2026-10-06 with spans from every service, in 30 s; the zero-span rule and the FAIL wording "alternated" were tested against a stub and not seen on the cluster. |
| `make smoke` | One PASS, FAIL or SKIP line per check; exits non-zero on any FAIL. |
| `make grafana` | Port-forward Grafana to <http://127.0.0.1:3000>. User `admin`. |
| `make grafana-password` | Print the Grafana admin password. |
| `make down` | Delete the `meridian` cluster and its credentials file. Destructive; refuses any other cluster name. |

`make smoke` checks eleven things:

1. **Edge.** `curl http://127.0.0.1:8088/` returns 404, and Envoy's own
   request counter went up. That covers laptop, kind port mapping, NodePort
   and Envoy.
2. **Database.** Six lines. The first (S063) reads two objects and prints
   after `make up` alone: the CIDRs of the rule for port 6443 in the
   NetworkPolicy `platform-db` are exactly the addresses of the `kubernetes`
   Service's EndpointSlice in `default` (see "The database pod and the API
   server's address" below). A difference is a FAIL that says "the API
   server's address changed: run make up"; a read that failed is a FAIL that
   says so and never says "changed". It does not prove that the path is closed
   to every other address, which is by hand. Then `pg_extension` lists
   `vector` in the `app` database and in the `meridian` database. Then three
   lines for the stores of
   the `meridian` database, read in the primary's pod: `policy.policies` holds
   policies (the seed Job writes them), `knowledge.chunks` holds chunks (the
   query is the one `make deploy` counts with, in `common.sh`), and the
   migrations ledger's newest file (`public.meridian_migrations`) is the newest
   file under `src/meridian/platform/migrations` of the checkout the script
   runs from, so a cluster deployed from another checkout says so. The lines
   print counts and a file name, never a row. One SKIP line stands in for them
   while the migrations ledger table is not there, which is when nothing was
   ever migrated (after `make up`, before `make deploy`). Once the ledger is
   there, a store's missing table (a half-applied or renamed migration) is that
   store's FAIL, naming the table, and an answer of the probe that is not in
   its form is one FAIL. A failed read of the database keeps its message,
   cleaned and cut to 160 characters, in the FAIL line: it names relations and
   roles, never a row. Every `psql` smoke runs has a statement timeout of 5
   seconds and a lock timeout of 3 seconds (`PGOPTIONS` in the exec), so a
   migration that holds a lock while smoke runs fails that line with psql's
   message instead of hanging it (the two pgvector lines keep no message: they
   say the extension is not installed). Those reads passed on the cluster on
   2026-10-06, so `env` exists in the database's container; the migrations
   line named `0019_audit_trail_seq.sql`, "the newest of this checkout", after
   the deploy applied migrations 0017 to 0019 to the cluster's database. A
   count above zero says the seed and
   the ingestion wrote something, not what or how much, and not that the chunks
   are the running image's (the ingestion Job of the image's tag, which
   `make deploy` keeps, is that proof); the claims and runs tables are not
   read, because `make demo` fills them.
3. **Tools.** One call per tool server through the runtime's own client, run
   inside the Agent Runtime's pod (`python -m meridian.runtime.toolprobe`), so
   with the addresses the runtime itself was given. The call names a run that
   does not exist, and every server must refuse it as `unknown-run`. That
   proves the address, the name lookup, the server's Host allowlist, the MCP
   handshake and the server's database role reading `runtime.runs`. It does
   not prove a completed call: no single role can make up a claim and a run,
   so that is `make demo`'s proof. The calls run over TLS with the runtime's
   certificate (line 9). Before `make deploy` this check prints SKIP.
4. **Telemetry.** Six lines (S063; seen on kind on 2026-10-06: the ConfigMap
   line, the clear-text line answering `400` and telemetrygen's three lines
   passed. Not seen: a renewal of the collector's certificate or of its
   authority, and a cold start. The rule that only a 400 passes, the Jobs'
   deadline and check 8's dependence on the push are tested without a cluster
   until the next run). The first two are about TLS and do not need the
   Meridian services. The ConfigMap `telemetry-ca` in `meridian`, which the six
   services and telemetrygen mount to trust the collector, holds the
   certificate its authority has now: the SHA-256 fingerprint of its `ca.crt`
   equals that of `tls.crt` of the Secret `telemetry-ca` in `observability`
   (only that one field of the Secret is read, the line prints the first twelve
   hex digits of a fingerprint and nothing else, SKIP while the Secret is not
   there, and a FAIL that says to run `make up` when the ConfigMap is missing
   or stale: the services read the mounted file at each new connection and the
   kubelet refreshes it, so `make up` is the whole remedy, within about a
   minute and with no restart). Then a push in clear text is not
   accepted: a Job pod in `meridian`, which the policies admit to the
   collector's port, so that what refuses it is the TLS listener and not a
   NetworkPolicy, runs the database image's `bash` (on the node after `make
   up`: no image is pulled) and sends plain HTTP to port 4318. A `400` (a Go
   TLS listener answers `400 Bad Request`: "Client sent an HTTP request to an
   HTTPS server"; seen on kind on 2026-10-06 as `HTTP/1.0 400 Bad Request`) or
   a connection closed with no answer passes, and the line says which. Any
   other status fails, and the line says what came back and that it does not
   show TLS: a plain HTTP receiver answers 404, 503 or 301 too, so a status
   other than 400 means something answered HTTP in clear text; a 2xx fails as
   the receiver taking the push. A connection that times out, is refused or
   gets no answer fails with "proves nothing", so a policy that cuts the probe
   off is not read as a refusal by the listener. Then three short Jobs run
   `telemetrygen` and send one trace, one log and one metric for a fresh
   service name (`meridian-smoke-<epoch>`) through the collector, over OTLP/HTTP
   with TLS to port 4318 (`--otlp-http` and `--ca-cert`, the authority's file
   from the ConfigMap; the same port the six services use). Those three lines
   prove the TLS path end to end for telemetrygen, and check 5's cost series,
   which the Model Gateway's own exporter pushes, proves it for a service; they
   do not prove that a service refuses another authority (the exporters' test
   does) or that every service has the file (`make demo`'s trace with a span of
   each service does). Since S063 the Jobs run in `meridian`, not in
   `observability`: the collector admits the pods of `meridian` and no pod of
   another namespace, with no exception that exists only while smoke runs, and
   [`manifests/smoke-networkpolicy.yaml`](manifests/smoke-networkpolicy.yaml)
   gives them the egress the chart's `default-deny` would otherwise take (DNS
   and the collector's 4318). On a cluster that `make up` has not brought up
   to date the Jobs time out, and the FAIL line says so. Each Job also ends at
   a deadline of 150 seconds, 30 more than smoke waits for it, so a Job whose
   pod never starts (no ConfigMap `telemetry-ca`) fails and its TTL of 900
   seconds removes it, where a Job that never finishes would stay (tested
   without a cluster). The script then
   reads each back through Grafana's datasource proxy from Tempo, Loki and
   Prometheus, waiting up to 120 seconds each. It prints the trace ID and how
   to find the data in Grafana Explore.
   What a PASS line prints of an answer (the trace ID, the log line, the
   series count) is cleaned of control characters and newlines and cut to 120
   characters: anyone who can push a log line to the collector chooses its
   text.
5. **Cost panel.** Four lines. The dashboard: Grafana serves
   `meridian-gateway-cost` as provisioned, with the same queries as the
   file, and Prometheus runs each of them without an error (a dashboard with
   no query, or a target with no expression, is a FAIL, and the targets of
   panels nested in rows count; the health dashboard of line 11 is read the
   same way). The gateway's
   series: when the gateway is available and its ledger holds an attempt
   settled since its process started, Prometheus must hold its tokens,
   cost and calls series with a sample exported after the first of those
   attempts, so that the series of a process that has just been replaced
   do not count. Before `make deploy`, and while the gateway has settled
   nothing since it started, this line prints SKIP; `make demo` sends a
   claim. Grafana's rights: its service account may not read Secrets in
   `meridian` or `observability`. kube-state-metrics' rights (S063): its
   service account may not get, list or watch Secrets in `meridian` or
   `cert-manager` (six `kubectl auth can-i --as` questions, each answered
   exactly `no`). Neither rights line needs a deployed service, so both print
   after `make up` alone. The second says nothing of the Prometheus
   operator, which still reads Secrets in every namespace.
6. **Adjuster pages.** Three lines. The queue at
   `http://claims.meridian.localhost:8088/adjuster/claims` answers 200 with
   a `Content-Security-Policy` that forbids framing and every script, and
   carries the synthetic-data line. A decision posted with another site's
   `Origin` is refused with 403 before any claim is looked up (threat model
   T-70). The claimant's start page at
   `http://claims.meridian.localhost:8088/claimant/claims` answers 200 with
   the same policy and carries the banner's second sentence, which says that
   every value entered must be fictional (T-04); the request is a GET and
   changes no claim. Before `make deploy` this check prints SKIP.
7. **Sweep.** One line, read-only. The CronJob `meridian-sweep` exists, and
   the last of its Jobs to finish, scheduled or made by hand, succeeded; the
   line says when it finished. It fails when the CronJob is missing, when the
   last finished Job failed (the line gives its reason, and `describe` and
   `logs` commands: a Job that hit its deadline or whose pod never started has
   no log), when the CronJob was last scheduled more than 15 minutes (three
   periods) after that Job finished with nothing running, when that Job
   finished more than 15 minutes before now with nothing running (the
   schedule stopped after a success; the line says how long ago and what the
   bound is), when no Job of it is left (a finished Job is removed a day after
   it finished) and it was last scheduled more than 15 minutes before now with
   nothing running, when it was never scheduled although it was created more
   than 15 minutes ago, and when the clock below cannot be read (the line
   keeps the reason, cleaned and cut). The period is read
   from the CronJob's own `.spec.schedule` when that is `*/N * * * *`, else it
   is the script's constant, five minutes. "Now" is the database's clock, the
   primary's `now()`: the one the identity check already trusts for its audit
   row, not this laptop's `date` (its clock is not the cluster's) and not the
   controller manager's Lease (one more object to trust, for no gain). Before
   `make deploy`, while no Job of it has finished yet, while a CronJob that
   never ran is younger than 15 minutes, while the CronJob was deployed less
   than one period ago (a Job of the CronJob it replaced is old, not overdue)
   and while it is suspended (`.spec.suspend`: it makes no runs, so none is
   overdue), this line prints SKIP. A PASS does not say the sweep did its
   work, only that a Job finished.
8. **Network policy.** Five lines. Each opens a TCP connection and sends
   nothing, from Python in a pod (the image has no curl); a denied path passes
   only when it times out (a refused connection or a name that does not
   resolve fails), and a path that answers fails the line. The first line is
   the control: the Claims API's pod reaches the Agent Runtime, which the
   Claims API's policy and the Agent Runtime's both name, so a "blocked" below
   is not a broken probe (if it fails, the other four are not printed). Then
   the Claims API cannot reach the Model Gateway, which no rule names; the
   Claims API cannot reach the API server's Service address
   (`kubernetes.default.svc:443`), which no service's policy lists and which
   answers when no policy applies, a target inside the cluster so that smoke
   sends nothing off the machine; and a probe pod, which smoke starts from the
   Claims API's own image and securityContext and deletes at the end (a
   delete that fails is not forgotten: the exit trap tries again and says on
   stderr, with the command to run by hand, when it fails too;
   `smoke-network-<time>`, labelled `app.kubernetes.io/name=meridian-sweep` so
   that the sweep's policy lets it reach DNS and the database, and not
   `app.kubernetes.io/part-of=meridian`, which the database's ingress admits by),
   cannot reach `platform-db-rw.meridian.svc:5432` until the same pod is given
   that label, and then can. The fifth line (S063) is the collector's: a
   second probe pod of the same image, in the namespace `default` (outside
   `meridian` and `observability`, and on every cluster) and with no
   workload's label, opens a connection to
   `otel-collector.observability.svc.cluster.local:4318`, and it must time out,
   because the collector admits the pods of `meridian` alone. A missing or
   too wide policy, or a cluster that does not enforce it, makes the collector
   answer and fails the line, and so do a refusal, a name that does not
   resolve and a failed exec. It prints SKIP when the collector's Deployment
   is absent. A timeout alone cannot tell a policy that blocks from a
   collector that is up but hangs, so the line depends on check 4's push from
   `meridian` (the control: a pod the policies admit does reach the
   collector): when that push did not pass in the same run, a timeout from
   `default` is a FAIL that says "the collector was not reached from meridian
   either, so a timeout from default shows nothing", not a PASS; an answer
   from the collector fails as before, whatever check 4 did. It does not prove
   that a pod of `meridian` can push (line 4 does, from the Jobs it runs
   there, and this line depends on it), that 4317 is closed to every pod (it
   probes 4318), or that a namespace other than `default` is refused. The pods
   also carry `meridian-smoke=network-probe`, which no policy, Service or
   Deployment selects. A run that is killed hard (SIGKILL, a power cut) leaves the pod as
   a Failed object with the labels the policies select on, so the check starts
   by listing the pods with that label and deletes by name those older than 300
   seconds (a younger one is another run's; a list that cannot be read is not
   an error). SIGHUP, SIGINT and SIGTERM each run the exit trap, which deletes
   the pod (tested by starting the real script against stub commands; an
   interrupted run was not tried on the cluster). The allowed paths are also
   the tool check's proof (line 3). It fails when the NetworkPolicy
   `default-deny` is missing. Before
   `make deploy` one line prints SKIP in place of the five. It adds about 30
   seconds. On 2026-10-06 the first four lines passed on the cluster (the
   control, the two denied paths out of the Claims API, and the database
   refusing a pod without the label and taking one with it), and no probe pod
   was left in `meridian` afterwards. The fifth line passed on the cluster on
   2026-10-06 (a probe in `default` cannot push to the collector on 4318); its
   dependence on check 4's push is tested without a cluster until the next
   `make smoke`. What it
   does not prove, and stays by hand (S019): that a pod of another namespace
   cannot reach the database, and that an address outside the machine is
   unreachable (smoke sends nothing there); and it does not read the
   policies, which the chart's tests render and compare.
9. **Service identity.** Five lines, run with Python in the Agent Runtime's
   pod against the Model Gateway (the image has no curl; the Claims API's pod
   would be the better caller to refuse, but the policy of line 8 blocks it
   from the gateway altogether, so the runtime's is the pod that reaches it).
   `GET /healthz` with no client certificate answers 200, which is what the
   kubelet's probe sends. `POST /v1/chat` with no client certificate answers
   401: the gateway knows no caller. The same request with the runtime's own
   certificate, naming the tenant `evaluation`, which is real and may run the
   triage agent but is not one the registry lets the runtime name, answers
   403. The 403 alone does not say who refused (the gateway answers 403 for
   its own policy too), so the fourth line reads the audit table in the
   database's primary pod: a row of the gateway's refusal with the reason the
   identity rule writes (`caller-name-not-allowed`), the Agent Runtime as the
   calling service and the tenant `evaluation`, recorded at or after the start
   of this run, which is the database's own clock read just before the 403's
   request. The gateway writes it in a worker thread and at most once per
   reason, tenant and minute, so the check asks for a row that exists, never
   for a count that went up, and tries for about ten seconds; the line says the
   row's age. A second run inside that minute causes no row of its own, and
   the line is then a SKIP, not a PASS, when the newest row is from the minute
   before this run started ("the gateway wrote this minute's refusal row for
   an earlier run; run again in a minute"); with no row at all it is a FAIL.
   What the row proves is a refusal by the identity rule, for that reason,
   caller and tenant, at or after the mark; not that it is this run's own 403.
   Two runs that overlap can share one row (another run's 403, written after
   this run's mark while this run's own is throttled, passes this line), so the
   PASS says "recorded at or after this run's mark". On the cluster on
   2026-10-06 the line said "0 s old, recorded at or after this run's mark",
   and a second run inside the gateway's minute was the SKIP described above.
   The fifth line presents a certificate of another CA: the probe makes a
   throwaway key and a self-signed certificate that carries the runtime's own
   URI (the right name, the wrong CA) in a directory under `/tmp` that is
   removed when the probe ends, and the gateway must end the connection
   before it answers: a status is a FAIL. The probe reads before it writes, so
   that under TLS 1.3, where the alert follows the handshake, it is the first
   thing read, and it tells three endings apart. `refused` is the TLS alert for
   an unknown CA (Python's `ssl` reports `TLSV1_ALERT_UNKNOWN_CA`); `reset` is
   a connection that ended with no alert before any request was sent.
   uvicorn, which the services run
   under, ends an unknown CA's connection without delivering the alert (a
   reset under TLS 1.3 and an EOF under 1.2, measured against the test server
   that has the services' flags), so `reset` is the ending expected of the
   gateway, and the line passes it, in other words than `refused`. On the
   cluster on 2026-10-06 the answer was `reset` on every run (the connection
   ended with no TLS alert, before any request was sent) and `refused`, the
   alert, was never seen; the EOF under TLS 1.2 was measured against the test
   server only. It is wider than a refusal for the unknown CA: a gateway
   that died in that second would end the connection the same way. A
   connection that ends after the request went out is a third answer,
   `closed-after-request`, and a FAIL: it is what a gateway that accepted the
   certificate and then closed without answering does, so the line says the
   gateway may have accepted it. The price is that a gateway slower than the
   probe's one-second read to end the connection turns a refusal into that
   FAIL; it fails closed. Any other
   TLS error (another alert, say) is a FAIL. The key is never printed, passed
   as an argument or kept. The probe checks the gateway's certificate against
   the CA and its DNS name; a traceback (a name that does not resolve, a
   certificate that does not verify, a refused connection) is a FAIL, never a
   refusal. Before `make deploy` this check prints SKIP.
10. **Certificate policy.** Four lines, never SKIP, the first three read-only:
    the objects exist after `make up`, so a missing one is a FAIL. The three
    `CertificateRequestPolicy` objects are Ready. The Deployment
    `cert-manager-approver-policy` in `cert-manager` has an available replica.
    And cert-manager's own approver is off, read two ways that must agree: the
    ClusterRole `cert-manager-controller-approve:cert-manager-io`, which the
    chart renders only with its approver on, does not exist (kubectl's
    NotFound is the pass; any other error is a FAIL), and the controller's
    arguments hold `--controllers=-certificaterequests-approver`. A FAIL on the
    third line says the issuer may be signing for every request again. The
    check is there because nothing else in `make up` or `make smoke` makes a
    certificate request once the cluster has its certificates, so an update of
    cert-manager or approver-policy that turned the built-in approver back on,
    or left the policies or the add-on gone, would pass the pull request that
    brings it. The fourth line is the one request smoke makes on purpose, and
    one that the issuer must refuse (S062): a `CertificateRequest` named
    `meridian-smoke-refused-<pid>-<random>` in the namespace `default`, for the
    issuer `meridian-services`, with a URI under the Meridian prefix and a
    duration that policy allows, so that only its namespace refuses it: the
    namespace selector of `meridian-services` does not list `default`, and
    `meridian-deny-unlisted`, which selects the issuer from every namespace,
    permits nothing. It passes when the request is Denied and the approver's
    whole message, judged before it is cut, names `meridian-deny-unlisted` as a
    policy that evaluated the request and does not name `meridian-services` as
    one (the line says the reason, cut to 60 characters, and the message, cut
    to 120). The form it matches is the one approver-policy v0.28.0 wrote on
    the cluster, `No policy approved this request: [meridian-deny-unlisted:
    [spec.allowed.uris: Invalid value: ...`: a policy's name after `[`, `]` or
    `,` and before a colon, so `meridian-services-ca` and the issuer's name do
    not count. The check depends on approver-policy's wording at the pinned
    version. A Denied request whose message names `meridian-services` as a
    policy is a FAIL (that policy selected a request from another namespace),
    and so is one whose message has neither form (the line says the approver's
    message is not in the form this check reads, and prints it cut). It fails
    when the request is Approved or carries a certificate (the issuer signed a
    request it must refuse; with both conditions true the request is Approved,
    whatever their order), and when neither condition is there after 30
    seconds (the approver did not answer; a request that no policy the
    requester may use selects is left the same way). The request is a
    `CertificateRequest` and not a `Certificate` so that the key is made on
    this machine, by `openssl`, and written to `/dev/null`: it is in no file, no
    variable and no output, and the request holds the public half only. A
    request makes no Secret; the certificate an issuer signed would be in the
    request's own status, is never printed and is deleted with it. The request
    is deleted as soon as it is read, by the script's EXIT trap when the run
    ends first (an error, a FAIL, an interrupt) and, after a run that was
    killed, at the start of the next one, which lists the requests with the
    label `meridian-smoke=refused-request` and deletes by name those older than
    300 seconds (a request lives two seconds). The age is the request's
    `creationTimestamp` read with `jq` against this machine's clock, so a
    skewed clock only delays the sweep, and a younger request is another
    run's, which two runs at once leave to each other; a list that cannot be
    read is not an error. A delete that fails, judged by `kubectl`'s exit
    status and not by what it wrote, is a FAIL that names the request (with
    "kubectl exited non-zero with no message" when it wrote nothing).
    Approved: the line says the issuer signed it, and smoke
    deleted it. What it does not prove: the request is made by whoever runs
    smoke (kind's cluster-admin, which may use every policy), not by
    cert-manager's account, so it shows what the namespace selector and the
    approver do with a request from another namespace, not the role bindings
    that let cert-manager use a policy (the plan's S056 section made those by
    hand); and a request for an issuer that is not Meridian's, which no policy
    answers, is not made. It adds a second or two when the approver is up, 30
    seconds when it does not answer. Run on kind on 2026-10-06: the request in
    `default` was Denied, with the reason `policy.cert-manager.io` and a message
    that began "No policy approved this request: [meridian-deny-unlisted:
    [spec.allowed.uris: Invalid value: ...", and deleted; no request with
    smoke's label was left in `default`. The three other lines passed too. The
    FAIL forms (a request Approved, a message in neither form, a request left
    undecided, a delete that fails) were tested without a cluster and not seen
    there.
11. **Alert rules and health dashboard.** Four lines, read-only, run last.
    The first three read Prometheus' `/api/v1/rules` through Grafana's
    datasource proxy, for the `PrometheusRule` `meridian` that `make up`
    applies. The four groups of `alerts/meridian.yaml` are loaded and every
    rule in them has health `ok`: a FAIL names each rule that has not, with
    its health and Prometheus' last error, cut to 120 printable characters
    (a rule that has not been evaluated yet is `unknown`, which is not `ok`;
    the line waits up to 120 seconds for the first evaluation). The loaded
    group and rule names are the file's, in both directions, so a cluster
    that runs an older rule file says which groups and rules differ (the
    file's names are read with `awk` by their indentation, and a test keeps
    that equal to a YAML parser's reading). And no alert of the Meridian
    groups is firing: a firing alert is a FAIL that names it, and a pending
    one is not a failure, so the line names it and passes. With no group of
    the Meridian prefix loaded the third line fails: there is nothing to be
    firing, so it cannot tell. One FAIL line replaces the three when the
    `PrometheusRule` is not in `observability` (`make up` applies it, so the
    line says to run it; any other error looking for it is a FAIL too), and
    one FAIL line stands in for them when Prometheus does not answer with
    status `success`. An answer of status `success` that has no usable
    `data.groups` list fails each of the three lines, and the dashboard line
    still runs. The fourth line is the cost dashboard's line (check 5)
    for **Meridian: platform health** (uid `meridian-platform-health`):
    Grafana serves it as provisioned with the queries of
    `dashboards/platform-health.json`, and every query runs in Prometheus
    with the range at an hour. No query is left out: the range variable
    becomes 3600, and a query that finds no series on a quiet cluster still
    answers with status `success`. The check does not prove that each series
    a rule or a panel names exists: a rule over a missing series is healthy
    and quiet, and a panel with no data is a success, so the series checklist
    of [the operations index](../../docs/operations/README.md#not-proved-on-a-cluster)
    stays by hand; nor that a threshold is right; nor that anyone would be
    told, because kind runs no Alertmanager. The four lines passed on the
    cluster on every smoke run of 2026-10-06. Smoke was not run while the
    renewal watch's short certificates were in place and the alert fired; a
    firing alert is a FAIL of the third line, so it would have failed (see
    "How long a certificate lasts" below).

`make smoke` creates three Jobs in `observability`. Kubernetes removes each one
15 minutes after it finishes. It creates one Pod in `meridian` for the network
check (line 8) and one `CertificateRequest` in `default` for the certificate
policy check (line 10), and deletes each as soon as its check is done and again
when the script ends. The tool check leaves at most one refused `tool.call` row
per server in the audit log per throttle window, and the identity check one
refusal row per reason and minute.

## The services: `make deploy` and `make demo`

One image, built from the [`Dockerfile`](../../Dockerfile) at the repository
root, runs all six services and the three commands of the Jobs. It is based on
`python:3.13-slim` and `uv`, both pinned by digest, installs the locked
dependencies without the dev group and the package non-editable into a venv,
runs as user 10001 and sets no command of its own (the chart names each
workload's).
It carries the registry and the seed data the Jobs load: the synthetic
policies, their claim history, the four policy wordings and the generator's
manifest (file hashes and counts), and nothing else of `data/synthetic`: not
the claims, not the expected outcomes. Every service's pod therefore holds
the policies file, with each synthetic holder's name and address, although
only the seed Job reads it (threat model T-51).
Its build context is an allowlist ([`.dockerignore`](../../.dockerignore)), so
the kubeconfig, `.env` files and `.context/` can never enter it; key and
certificate files are excluded even inside the allowed folders. There is no
registry: `make deploy` tags the image `meridian:<first 12 hex digits of its
ID>` and loads it into the kind node, so an unchanged tree reuses the same
tag.

| Service | Reached at | Database role |
|---|---|---|
| Claims API | `http://claims.meridian.localhost:8088` (edge) and `http://claims-api.meridian.svc:8000` | `claims_api` |
| Agent Runtime | `https://agent-runtime.meridian.svc:8000`, cluster only | `agent_runtime` |
| Model Gateway | `https://model-gateway.meridian.svc:8000`, cluster only | `model_gateway` |
| Policy tool server (`policy_lookup`, `claim_history`) | `https://policy-mcp.meridian.svc:8000/mcp`, cluster only | `policy_mcp` |
| Knowledge tool server (`wording_search`) | `https://knowledge-mcp.meridian.svc:8000/mcp`, cluster only | `knowledge_mcp` |
| Claims tool server (`add_claim_note`, `request_approval`, `approval_outcome`; the triage graph calls them for a claim referred to an adjuster, S015) | `https://claims-mcp.meridian.svc:8000/mcp`, cluster only | `claims_mcp` |

The runtime finds the tool servers through `MERIDIAN_TOOL_SERVERS`, a map of
the registry's server IDs to those addresses. A tool server answers `/mcp`
only for the one Host name in its `MERIDIAN_ALLOWED_HOSTS`, which is the name
in the runtime's map: the same Service called as
`policy-mcp.meridian.svc.cluster.local`, `policy-mcp.meridian` or `policy-mcp`
gets 421. A test keeps the two values equal.

In order, `make deploy`:

1. Refuses with "run 'make up' first" if the `meridian` Database is not
   applied, a role is not reconciled, a role Secret is missing or the
   database's NetworkPolicy `platform-db` is absent (the chart's
   `default-deny` would otherwise cut the database off from its operator).
   It refuses the same way, before it builds anything or runs a Job, when the
   ClusterIssuer `meridian-services` (the issuer in
   [`values/meridian.yaml`](values/meridian.yaml)) is missing or not Ready,
   or when the cluster does not know the kind at all (a cluster made before
   S055): the chart's Certificates would never be issued, and the
   Jobs would already have run when the upgrade found out. `make up` installs
   the issuer. It refuses the same way when one of the three
   `CertificateRequestPolicy` objects is missing or not Ready (a cluster made
   before S056 does not know the kind) or when the Deployment
   `cert-manager-approver-policy` in `cert-manager` has no available replica:
   the issuer is Ready without them, but with cert-manager's own approver off
   nothing would approve the chart's Certificates, and the deploy would die at
   its wait for them, right after the release, with the Jobs already run. It
   names every one that is wrong, and `make up` installs them. A policy that
   is wrong is refused at once. When only the add-on has no available
   replica, the script looks again every 5 seconds for 60 before it refuses:
   right after a cold `make up` the add-on lost its leader election, exited
   and was back in twenty seconds. The refusal then says the add-on "was not
   available for 60s" and gives both remedies: `make up` for a cluster that
   predates S056, a look at the pod (`kubectl -n cert-manager get pods`) for
   one that has it and shows it restarting. When kubectl said something at
   the last look (an API error, a refused read), the refusal quotes it on one
   line, because that is not an absent add-on and `make up` is not its remedy.
   (Tested against stub commands;
   not yet seen on a cluster.)
2. Builds and loads the image, tagged `meridian:<first 12 hex of its ID>`. A
   deploy of a changed tree leaves the previous image in the Docker engine
   and in the node, and images stay there until a person removes them.
   `make images` lists them, each marked in use or unused by a workload, and
   prints the commands that would remove the unused ones; it removes nothing.
   "In use" means a pod template names the image now, or a Pod that exists
   does. The chart sets no `revisionHistoryLimit`, so after a deploy that
   changed the image the previous tag is what an old ReplicaSet would start
   again on a rollback, and with `pullPolicy: Never` a removed image cannot be
   pulled again: an image only an old ReplicaSet names is marked `rollback`
   (a rollback's target), listed apart, and gets no removal command. In a
   checkout with no `infra/kind/kubeconfig` it asks kind for the cluster: when
   none exists every image is unused by definition, and when one does (its
   credentials are in another checkout) it says it cannot tell which images
   are in use, prints no command and exits non-zero. (The listing, the three
   marks and the printed command were seen on the cluster on 2026-10-06; this
   refusal was tested against stub commands and not run on a cluster.)
3. Runs a Job `meridian-migrate-<tag>` with `meridian db migrate`, then a Job
   `meridian-seed-<tag>` with `meridian db seed-policies`, the first as
   `meridian_owner` and the second as `policy_seed` (S063: the seed reads
   `MERIDIAN_SEED_DATABASE_URL` and has no fallback to the owner's variable).
   Only the migration Job reads the owner's Secret. The script renders
   each Job from the chart (`helm template --show-only`, with that Job's flag
   on) and applies it with its ServiceAccount and its NetworkPolicy; the
   release itself never holds a Job. Both must finish
   before anything else is applied; on failure the script prints the Job's
   log and exits non-zero. They run on every deploy (a Job of the same tag is
   deleted first; the runner skips what is applied and the seed mirrors its
   source, so a rerun takes seconds) and end after 300 seconds at most. A
   finished migrate or seed Job removes itself an hour later
   (`ttlSecondsAfterFinished: 3600`; on the cluster on 2026-10-06 the Jobs of
   earlier images were gone an hour after they finished); only the ingestion
   Job of the image in use stays (step 5), as its record. The
   seed comes before the services because a claim that meets an empty policy
   table gets a stored proposal "policy not found", and a stored proposal is
   final.
4. Installs or upgrades the Helm release `meridian` (`helm upgrade
   --install`, server-side apply): the ServiceAccounts, Deployments,
   Services, PodDisruptionBudgets and NetworkPolicies, the HTTPRoute, the
   BackendTrafficPolicy and the sweep's CronJob. The image's repository and
   tag are the only values the script passes; the rest is the chart's
   `values.yaml` and [`values/meridian.yaml`](values/meridian.yaml). Helm
   does not wait for the rollouts; the next steps do.
5. Waits for the Model Gateway, then runs a Job `meridian-ingest-<tag>` with
   `meridian knowledge ingest` as `knowledge_ingest` (its audit row names that
   role), which embeds the 85 clauses of the four
   wordings through the gateway and replaces the knowledge store in one
   transaction. Once per image: the finished Job has no expiry and is the
   record that this image's corpus is in the store, so the next deploy of
   the same image skips it, after checking that the store is not empty. The
   Job is a record of what was done, not of what the store holds now: after
   an ingestion by hand from other files, delete the Job to ingest again. A
   search before the first ingestion fails its run (`no-corpus`), and that
   claim can simply be posted again.
6. Waits for the other rollouts and for the route to be `Accepted`.
7. After an ingestion, waits until a minute has passed since it finished.
   The ingestion's embedding calls go out under the `claims-triage` tenant,
   whose limits the registry sets: it reserved 7,679 of the tenant's 10,000
   tokens a minute and six of its ten requests per 10 seconds (measured on
   kind, 2026-10-02). A triage that asks the model reserves about 1,090
   tokens in five requests, so a claim posted in the first seconds would be
   refused, and only two would fit in that minute. The wait makes a deploy
   end with the limits clear. A deploy that is interrupted after the
   ingestion and run again within that minute finds the Job succeeded, does
   not run the ingestion and does not wait: the script's only clock is its
   own, and it reads no Kubernetes timestamp, so it cannot know how long ago
   the Job finished and does not guess. It prints one line that says the
   wait was skipped because this run did not run the ingestion (a repeat
   deploy of the same image prints it too). A first request refused for the
   tenant's token limit within a minute of an interrupted deploy is that
   window: wait a minute and run it again. (On 2026-10-06 a repeat deploy
   printed the line that the wait was skipped; the interrupted deploy and
   the refused request were tested against stub commands and not seen on a
   cluster.) Whether ingestion should spend a workload's
   budget at all is an open registry decision (threat model T-60).

Each pod gets its own role's connection string from its Secret, and the
cluster CA's public certificate (`ca.crt` only, not the CA's private key that
shares the Secret `platform-db-ca`) at `/etc/meridian/db-ca/ca.crt`. Spans go
to the collector's OTLP/HTTP port, `:4318`, over TLS (S063); the `/healthz`
probes are not traced. The `*.localhost` name resolves to the loopback address
on macOS and on current Linux resolvers; the edge listens on `127.0.0.1` only.

The route is the whole boundary: the Claims API is the only service with one,
on the host `claims.meridian.localhost`. A request with any other `Host`
header, such as `127.0.0.1:8088` or a page that rebinds its DNS name to
loopback (threat model T-01), matches no route and gets 404 from Envoy.
Envoy buffers each request to the Claims API and answers 413 above 64 KiB, the
app's own limit, before the app sees it. The edge speaks plain HTTP, on
loopback only; TLS there has no step yet.

A `ClusterIP` Service means unrouted, not protected. Since S019 the
namespace's network policies are the protection: the Agent Runtime, the
Model Gateway and the tool servers accept a connection only from the pods
the next section lists. That is a rule about pods' labels, not about who a
caller is: since S055 each of them also reads the calling service from
its certificate and refuses a tenant or agent that service may not name
("Who a service is" below). A tool server gives a caller less than
the others do: it answers only for the run ID of a running run and only with
that run's claim (T-50). The database boundary above does not depend on any
of that.

## The chart and what it locks down

`make deploy` installs one Helm release, `meridian`, from
[`../helm/meridian/`](../helm/meridian/). Status: **implemented on kind**
(S019); the same chart is meant for AKS (S020), where the values that are
kind's here (the gateway's replay mode, the route's host, the policies'
peers) will differ. Neither file holds a secret: Helm keeps a release's
values in a Secret of the namespace, so a value names a Secret and never
holds a password or a connection string.

Every pod of the chart, the Jobs' and the sweep's included:

- runs as user and group 10001, never root, with every capability dropped,
  no privilege escalation, the runtime's default seccomp profile and no
  service-account token. These settings are written in the chart, not in
  its values, so a `--set` cannot loosen them, and the chart refuses user 0;
- has a read-only root filesystem. `/tmp`, an `emptyDir` of at most 16 MiB
  on the node's disk, is its one writable path;
- has a memory limit and CPU and memory requests. There is no CPU limit on
  purpose: a request reserves what a service needs, and a limit would
  throttle it while the node has headroom;
- runs the image under a pinned reference. The chart takes `image.digest`
  and refuses a tag unless `imagePullPolicy` is `Never`; the tag `latest`
  and a repository that carries a tag of its own are refused always. kind
  has no
  registry, so there is no repository digest to pin: `make deploy` loads the
  image into the node under the first 12 hex digits of its image ID, a name
  the content gives itself, and `Never` makes a missing image fail the pod
  instead of being looked up on Docker Hub under the same name.

Each service has readiness and liveness probes on `/healthz` and a
PodDisruptionBudget with `maxUnavailable: 1`. With the one replica each
service runs, that budget permits the pod's eviction: it blocks no node
drain and protects nothing yet, and starts to matter with a second replica.
The chart refuses a second replica of the Model Gateway, whose rate windows
live in one process (threat model T-45).

The namespace denies all traffic by default: the NetworkPolicy
`default-deny` selects every pod in `meridian`, whatever its labels, and
allows nothing. One policy per workload then allows what its configuration
names:

| Workload | May be called by | May call |
|---|---|---|
| Claims API | the edge (Envoy's proxy pods) | Agent Runtime |
| Agent Runtime | Claims API | Model Gateway, the three tool servers |
| Model Gateway | Agent Runtime, Knowledge tool server, the ingest Job | nothing (replay mode) |
| Policy and Claims tool servers | Agent Runtime | nothing |
| Knowledge tool server | Agent Runtime | Model Gateway |
| The migrate and seed Jobs, the sweep | nobody | nothing |
| The ingest Job | nobody | Model Gateway |

The policies are unchanged by TLS: the services listen on the same port, so
every rule names the same peers and the same port as before, and a test
renders the chart with TLS off and compares the policies byte for byte.
Every workload may also reach DNS and the database, and the six services
the collector, and the sweep when it has the address (S064); the Jobs send no
telemetry and may not. A test
derives the table from each container's environment: a service address
without a rule, or a rule without an address, fails it. A Job's policy is
rendered and applied with the Job, so it is never one deploy behind.

The database pod shares the namespace, and its policy is the platform's:
[`manifests/platform-db-networkpolicy.yaml`](manifests/platform-db-networkpolicy.yaml),
applied by `make up`. It admits the Meridian pods on 5432 and the
CloudNativePG operator on 8000 (without that rule the operator reported
`Instance Status Extraction Error` within 40 seconds, measured in S019). The
database pod itself may reach DNS, the pods of its own Cluster and TCP port
6443 at the API server's address alone (S063; below): its instance manager
calls the API server, whose address is the node's own and changes with every
new cluster, so the file holds a placeholder and `make up` fills in the
address it reads. From the database pod a connection to the internet timed
out, and 40 of 40 to the API server were made (S019, when the rule named no
address). That stays by hand; `make smoke` (line 8, passed on the cluster on
2026-10-06) tries the other direction of that policy: a pod without the
`part-of` label on 5432.

What the policies do not do:

- They are not identity. A pod created in `meridian` with a service's label
  is admitted as that service; who may create pods there is the cluster's
  access control. Proving which service calls is S055's mutual TLS (below).
- DNS and the collector are open to the pods that use them, and either
  could carry data out slowly. The database pod may reach port 6443 at the
  API server's address alone since S063, which no test proves is enforced
  (above); the address must be read again when it changes (`make up`).
- They are not enforced by admission. The namespaces warn about and audit
  a pod below their Pod Security level (`restricted`;
  [`manifests/namespaces.yaml`](manifests/namespaces.yaml)); they do not
  refuse one yet (below), and on kind `audit` records nothing (no API server
  audit policy is configured) and `warn` reaches only the client that creates
  a workload, never a controller's pod. `cnpg-system` and
  `envoy-gateway-system` carry neither labels nor a NetworkPolicy.
- The Model Gateway has no rule towards a provider: on kind it calls none.
  The rule for Azure OpenAI is S020's.

kind's network plugin, kindnet, enforces NetworkPolicy; `make smoke` checks
that on every run (line 8). To look at the release and the policies:

```sh
export KUBECONFIG=$PWD/infra/kind/kubeconfig
helm -n meridian status meridian
kubectl -n meridian get networkpolicy,poddisruptionbudget
```

### The database pod and the API server's address (S063)

Until S063 the rule for port 6443 had no peer, so the database pod could open
a connection to port 6443 of any address. Now the rule names the API server's
address alone. **Tested without a cluster** (the scripts against a stub
`kubectl`, the manifest as YAML), until `make up` and `make smoke` have run on
one and the proof by hand below has been made.

- `make up` reads the addresses of the `kubernetes` EndpointSlice in `default`
  (not the deprecated `Endpoints`), refuses anything that is not an IPv4
  address (the text goes into YAML), and applies the policy with one `ipBlock`
  of `/32` for each. It does so on every run, so a cluster whose node was
  given another address by a Docker restart is repaired by `make up`.
  `make up` stops before it changes the policy when the answer is empty or
  not an address.
- The manifest holds the placeholder `API-SERVER-ADDRESS/32`, which is not a
  CIDR: a plain `kubectl apply -f` of the file is refused by the API server
  and cannot install a wrong policy. No address is in any tracked file.
- If the address changes under a running cluster, the database loses the API
  server and CloudNativePG marks the Cluster unhealthy (S019 saw 40 seconds)
  until `make up` runs. `make deploy` refuses to start and the first line of
  smoke's database check fails, both with "the API server's address changed:
  run make up". Both are reads and prove nothing about the path being closed.
- What no test proves, and what stays by hand: that kindnet enforces an
  `ipBlock` egress rule after the Service's address is translated to the
  node's (the manifest's header says why the rule names the endpoint's
  address and not the Service's), and that a connection from the database pod
  to port 6443 of another pod is dropped. The commands follow, from a
  checkout with the cluster's credentials in the environment, against two
  throwaway Pods in `default` (a namespace with no policy) that use the image
  the Claims API runs, which is on the node.

```sh
IMAGE="$(kubectl -n meridian get deployment claims-api -o jsonpath='{.spec.template.spec.containers[0].image}')"
PRIMARY="$(kubectl -n meridian get pod -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}')"
API="$(kubectl -n default get endpointslices -l kubernetes.io/service-name=kubernetes -o jsonpath='{.items[0].endpoints[0].addresses[0]}')"
kubectl -n meridian get cluster platform-db        # before: "Cluster in healthy state"
kubectl -n default run listener-6443 --image="$IMAGE" --image-pull-policy=Never --restart=Never --command -- python -c "import socket, time; s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('0.0.0.0', 6443)); s.listen(8); time.sleep(600)"
kubectl -n default run prober-6443 --image="$IMAGE" --image-pull-policy=Never --restart=Never --command -- python -c "import time; time.sleep(600)"
kubectl -n default wait --for=condition=Ready pod/listener-6443 pod/prober-6443 --timeout=60s
LISTENER="$(kubectl -n default get pod listener-6443 -o jsonpath='{.status.podIP}')"
# 1. the control: a pod with no policy reaches the listener (prints: reached)
kubectl -n default exec prober-6443 -- python -c "import socket; socket.create_connection(('$LISTENER', 6443), 3); print('reached')"
# 2. the database pod to the listener: must time out (rc=124; 0 would mean
#    the rule still lets any address through, 1 a refusal)
kubectl -n meridian exec "$PRIMARY" -c postgres -- timeout 3 bash -c "</dev/tcp/$LISTENER/6443"; echo "rc=$?"
# 3. the database pod to the API server's own address: must connect (rc=0)
kubectl -n meridian exec "$PRIMARY" -c postgres -- timeout 3 bash -c "</dev/tcp/$API/6443"; echo "rc=$?"
# 4. the database pod to the API server's Service, which the node's address
#    replaces on the way (this is the translation the rule relies on): rc=0
kubectl -n meridian exec "$PRIMARY" -c postgres -- timeout 3 bash -c "</dev/tcp/kubernetes.default.svc/443"; echo "rc=$?"
# 5. after about a minute: still "Cluster in healthy state"
kubectl -n meridian get cluster platform-db
# 6. the file as committed is refused by the API server and changes nothing
kubectl apply --dry-run=server -f infra/kind/manifests/platform-db-networkpolicy.yaml
# clean-up
kubectl -n default delete pod listener-6443 prober-6443 --ignore-not-found --wait=false
```

A "reached" in 1 shows that a timeout in 2 is the policy and not the Pods'
network. If 3 or 4 time out, the plugin evaluates the rule before the
translation or the address differs from the node's: revert the change and
record that in the plan.

### The namespaces outside `meridian` (S063)

Until S063 a pod of any namespace could push to the collector, and
`cert-manager` and `observability` had neither a NetworkPolicy nor Pod Security
labels (threat model T-68, T-84). `make up` now applies three policy files
before the releases they guard, beside the database's; all of it is **tested
without a cluster** (the manifests and the script's lines are checked against
stub commands, and the pods' specs were read with `helm template`), until the
first `make up` and `make smoke` after it have run on one.

| File | Namespace | What it says |
|---|---|---|
| [`manifests/cert-manager-networkpolicy.yaml`](manifests/cert-manager-networkpolicy.yaml) | `cert-manager` | Ingress denied, except 10250 to the two webhooks (the API server calls them, `failurePolicy: Fail`; no peer, see below) and 9402 to the controller's metrics from Prometheus. Egress: DNS and TCP 6443 to the API server's address alone (`make up` reads it from the `kubernetes` EndpointSlice and fills it in, as it does the database's; `make deploy` and `make smoke` do not compare this policy with the endpoint) |
| [`manifests/observability-networkpolicy.yaml`](manifests/observability-networkpolicy.yaml) | `observability` | Ingress denied, except one rule per peer: the collector from the namespace `meridian` on 4318; Tempo from the collector (4317) and Grafana (3200); Loki from the collector and Grafana (3100) and from its own pods (7946, its memberlist); Grafana from Prometheus (3000); Prometheus from Grafana and the collector (9090); kube-state-metrics from Prometheus (8080); and 10250 to the Prometheus operator (no peer). Egress is open |
| [`manifests/smoke-networkpolicy.yaml`](manifests/smoke-networkpolicy.yaml) | `meridian` | The pods of smoke's telemetrygen Jobs may reach DNS and the collector's 4318, and nothing reaches them |

Only Meridian's pods push to the collector, on 4318, and 4317 is admitted from
no namespace: the collector's rule selects the namespace `meridian` and no pod
label, and the chart's own policies are what narrow that to the six services
(its `default-deny` leaves every pod of `meridian` without egress, and only a
pod given the collector's address has a rule for it; a test reads both halves
and fails when one drifts from the other). Smoke's Jobs moved into `meridian`
for that reason, so the sentence has no footnote. The gRPC receiver on 4317 is
closed in the collector's values (read from them; no probe of 4317 has run on
a cluster), and the push moved to TLS (below); the policy's refusal of 4317
stays as a second wall.

What stays open, in one list:

- Egress from `observability` is open: Prometheus scrapes the kubelet and the
  API server, which are the node, at an address no selector names, and a
  half-right egress policy that broke a cold `make up` would be worse. A
  compromised pod there can still reach whatever the other namespaces admit.
- The three webhooks (cert-manager's, approver-policy's, the Prometheus
  operator's) are open on one port each to any pod of the cluster, because
  the API server calls them from the node's address, which changes with every
  cluster. They are TLS endpoints that answer admission reviews, and they
  answer anyone: any pod can post a forged review and read the verdict, and
  nothing is applied by one, so nothing is changed. A flood against the two
  that fail closed (cert-manager's and approver-policy's) can stall the
  issuance and renewal of certificates; the operator's is
  `failurePolicy: Ignore`. Nobody has measured whether the address the API
  server's calls arrive from is the endpoint's, so none is narrowed.
- The collector's rule admits every pod of `meridian` that the chart's policies
  let out, and on a cluster where the chart is not installed (`make up` alone)
  every pod of `meridian` can push to it: `default-deny` is the chart's.
- Grafana's port-forward needs no ingress rule if the container runtime opens
  it inside the pod's own network namespace, which was assumed and is the
  first thing to look at; Loki's memberlist rule assumes the plugin sees the
  pod's join through its own Service as coming from the pod itself.
- node-exporter is off (above), so no node's CPU, memory or disk is observed.

Pod Security labels (`warn` and `audit`, never `enforce`, as on `meridian`):

| Namespace | Level | What stops the next one |
|---|---|---|
| `meridian` | `restricted` | nothing |
| `cert-manager` | `restricted` | nothing: its five pods meet it as rendered |
| `observability` | `restricted` | nothing as rendered: `tempo` and `otel-collector` set no `allowPrivilegeEscalation: false`, no `capabilities.drop: [ALL]` and no `seccompProfile`, the collector no `runAsNonRoot` either, until their values files set them (S063, tested without a cluster; the server-side dry run is repeated after `make up`); node-exporter would have stopped `restricted` too, and is off |

The Prometheus pods are the operator's, not rendered by Helm, and were not read:
a server-side dry run on the cluster (`kubectl label --dry-run=server`) is the
check that reads them.

### Who a service is (S055)

Mutual TLS with certificates from cert-manager: each service holds one, and
the one it calls learns which service it is from it. The chart renders a
`Certificate` for each of the six services and one for the ingestion Job
(`meridian-ingest`), always, because `make deploy` applies the Job outside the
release and its Secret must exist before its pod starts; `make deploy` waits
for every Certificate to be Ready right after the release. What the deploy
creates, besides what `make up` made (cert-manager v1.21.2 and the CA above):

- seven `Certificate` objects in `meridian`, signed by the `meridian-services`
  ClusterIssuer: ECDSA P-256, a new key at every renewal (`rotationPolicy:
  Always`, set in the chart), a lifetime of `certificate.duration` (90 days by
  default, renewed at 60: see "How long a certificate lasts" below), the URI
  `spiffe://meridian.kind/ns/meridian/sa/<name>`, and
  for the five services that serve TLS the DNS name `<name>.meridian.svc`;
- seven Secrets `<name>-tls` (`tls.crt`, `tls.key`, `ca.crt`), each mounted
  read-only at `/etc/meridian/tls` in its own pod and in no other;
- no new Service, port or NetworkPolicy.

The values `identity.trustDomain` (`meridian.kind`) and `identity.issuer` are
required and have no off switch; a service with `tls: true` in the chart's
values serves TLS, and the template adds uvicorn's flags, the HTTPS probes and
the prefix its callers' URIs start with. Nothing else of the five's commands
is repeated in the values. The chart fails for a service that another workload
calls (a `serviceUrl` or a `serviceMap` entry, the Jobs' included) and does not
set `tls: true`; only the Claims API, which nobody inside the chart calls,
stays plain HTTP.

**How long a certificate lasts (S062).** Two chart values set the lifetime of
every one of the seven Certificates; the defaults render what the chart
rendered before they existed (`duration: 2160h`, no `renewBefore`, so
cert-manager renews at a third of the lifetime, 60 days in):

| Value | Meaning | Refused |
|---|---|---|
| `certificate.duration` | whole hours and minutes (`2160h`, `1h30m`; no days, no seconds) | above `2160h`, the most the issuer's policy signs (`maxDuration` of `meridian-services`); below `1h`, cert-manager's shortest; anything that is not hours and minutes, empty included |
| `certificate.renewBefore` | how long before the end cert-manager renews; empty leaves it out | under `5m`, which cert-manager's webhook refuses (asked by a server-side dry run on 2026-10-06: `1m` and `4m` refused, `5m` accepted); not shorter than `certificate.duration`; anything that is not hours and minutes |

A refused value fails `helm template`, `helm upgrade` and `make deploy`
before anything is applied, with a message that names the value. The service's
own margin is not one of them: from the smaller of 24 hours and a sixth of the
lifetime before the end (`src/meridian/platform/common/certlife.py`) it reads
the mounted file again, and `/healthz` answers 503 once the file holds a newer
certificate. With the default `renewBefore` the renewal always comes before
that margin starts.

Run on the cluster on 2026-10-06, with the times of what was seen in the
runbook [certificate-expiry](../../docs/operations/runbooks/certificate-expiry.md#watching-a-renewal-on-kind-run-on-2026-10-06):
to watch a renewal, the 503 and the restart on kind, give the certificates a
one-hour life for a while. In a
working copy of `infra/kind/values/meridian.yaml`, never committed, add

```yaml
certificate:
  duration: 1h
  renewBefore: 30m
```

then run `make deploy`, which reissues the seven certificates and waits for
them to be Ready. The services still hold the 90-day certificates they loaded,
and a service looks at its file again only near the end of the one it loaded,
so restart the Deployments once (the owner's command, as in the runbook
[certificate-expiry](../../docs/operations/runbooks/certificate-expiry.md)):

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian "$@"; }
k -n meridian rollout restart deployment
k -n meridian rollout status deployment/agent-runtime
```

The times below count from the issuance (the end of `make deploy`). Expect
the renewal 30 minutes in, the 503 after 50 (the margin is a sixth of an
hour, 10 minutes) and the container's restart about a minute later: the
liveness probe asks every 10 seconds and fails the container on the sixth
503. Each of these only reads:

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian "$@"; }
k -n meridian get certificate -o custom-columns=NAME:.metadata.name,READY:.status.conditions[0].status,NOTAFTER:.status.notAfter,RENEWAL:.status.renewalTime
k -n meridian get certificaterequest
k -n meridian get pods
k -n meridian get events --field-selector type=Warning
k -n meridian logs deployment/agent-runtime --previous
```

The Certificates' `NOTAFTER` moves an hour forward at the renewal, and a new
CertificateRequest appears and is Approved. Pods keep `RESTARTS 0` until about
50 minutes in, then each service shows `RESTARTS 1`; the Warning events say
`Liveness probe failed` with status code 503, then that the container failed
its liveness probe and will be restarted; the previous container's log has the
warning that a renewed certificate is on disk. The renewed certificate lasts
another hour, so every service restarts again half an hour later: stop after
one cycle by removing the two lines and running `make deploy`. The services
keep the one-hour certificates until the end margin of each, then load the
90-day ones and stay (a restart of the Deployments does it at once). While the
short certificates are in place `MeridianCertificateNotRenewed` fires after an
hour (seen pending, then firing, on 2026-10-06), because the rule counts every
certificate under 21 days from its end as a late renewal; any
`certificate.duration` under 21 days trips it an hour after issuance.

What the watch showed, beyond the marks above: all six services restart in the
same minute, because one deploy issues their certificates in the same second,
and with one replica each nothing answered for about a minute; and while the
short certificates are in place `make smoke` fails on check 11, because a
Meridian alert is firing (smoke itself was not run then: the failure follows
from the firing alert and the check's rule). So the last step of the watch is
to wait until the alert has cleared (within five minutes of the 90-day
certificates' reissue it had), and only then to trust a smoke run.

| Caller | What it proves | Callee | What the callee checks |
|---|---|---|---|
| the edge (Envoy) | nothing: plain HTTP | Claims API | nothing yet (a backlog row); the policy lets only the edge's pods in |
| Claims API | its certificate, URI `.../sa/claims-api` | Agent Runtime | the certificate is from the CA, the service ID is in the registry and its `calls` hold `agent-runtime`; the tenant and agent the request names are in the caller's `tenants` and `agents` |
| Agent Runtime | `.../sa/agent-runtime` | Model Gateway, the three tool servers | the same, for each |
| Knowledge tool server | `.../sa/knowledge-mcp` | Model Gateway | the same |
| the ingest Job | `.../sa/meridian-ingest` | Model Gateway | the same |
| any caller | the server's certificate | | the caller checks the CA and the DNS name `<service>.meridian.svc` |

`GET /healthz` needs no client certificate (the kubelet's probe presents none);
every other request without one is refused with 401, and a certificate from
another CA fails the handshake. `config/registry/services.yaml` is the list of
who may call whom, and a test holds it equal to what the chart's environments
call. `make smoke`'s ninth check proves the gateway's 200, 401 and 403, the
audit row of the 403 and the refusal of a certificate from another CA (five
lines).

**A second authority, for the collector (S063, threat T-90).** The services
send their traces and metrics to the collector over OTLP/HTTP, and the owner
chose to encrypt that hop. The collector's server certificate is not signed by
`meridian-services`: S056 made "that issuer signs only for `meridian` and its
URI prefix" the boundary (T-88), and a policy that let it sign for
`observability` would reopen it for one certificate. Instead
[`manifests/telemetry-ca.yaml`](manifests/telemetry-ca.yaml) makes an authority
of its own in `observability`, from namespaced `Issuer`s (a request can name
one only from its own namespace, so it signs only there): a self-signed
`telemetry-selfsigned`, the CA certificate `telemetry-ca` (ECDSA P-256, one
year, key kept at renewal like the service CA's) with the `Issuer` of that name,
and the collector's `Certificate` `otel-collector` (90 days, a new key at each
renewal, the DNS names `otel-collector.observability.svc` and
`otel-collector.observability.svc.cluster.local`, usages `server auth` and
`digital signature`, no URI and no address). Two more policies in
`certificate-policy.yaml` (above) decide those two requests, and
`approveSignerNames` names the Issuers' signers
(`issuers.cert-manager.io/observability.telemetry-ca` and
`.../observability.telemetry-selfsigned`).

- **Who trusts it.** The services trust the service CA through their own
  Secret, so this authority has to reach them: `make up` writes its public
  certificate (`tls.crt` of the Secret `telemetry-ca`, never the key) into the
  ConfigMap `telemetry-ca`, key `ca.crt`, in `meridian`, on every run, and
  `make deploy` refuses to start without it. The chart's `telemetry.caConfigMap`
  names it (kind's values: `telemetry-ca`, with an `https` endpoint); each of
  the six services mounts its one key, read-only, at
  `/etc/meridian/telemetry-ca/ca.crt` (not under its own certificate's
  directory: that CA signs the services, not the collector) and trusts it
  through the SDK's own variable, `OTEL_EXPORTER_OTLP_CERTIFICATE`. The chart
  refuses an `https` endpoint with no ConfigMap and a ConfigMap with an `http`
  endpoint. A service whose endpoint is `https` and whose variable is unset, or
  names a file that cannot be loaded as a CA certificate, does not start: the
  factory raises a `SettingsError` that names the variable and never the path
  (the last line of the traceback `uvicorn --factory` logs), and the container
  restarts, as it does for a certificate it cannot read (not seen on a
  cluster). That is better than starting with telemetry that fails on every
  export. The Jobs set no endpoint and get none of this; the sweep gets it
  (S064). Status: tested without a cluster.
- **What the collector does.** It serves OTLP/HTTP with TLS on `:4318` from the
  Secret `otel-collector-tls`, mounted read-only as a directory, and reads the
  files again at a handshake every five minutes at most (`reload_interval`),
  so a renewed certificate needs no restart. The gRPC port, `:4317`, is closed.
  Its own hops to Tempo, Prometheus and Loki stay clear text, inside
  `observability`.
- **What goes stale.** The authority's certificate lasts a year and keeps its
  key when it is renewed (about eight months in), but the ConfigMap holds the
  old certificate until the next `make up`; a service that mounts a stale
  ConfigMap fails to verify the collector, its exports are dropped (the
  exporters run in background threads and log the failure) and it keeps
  serving. `make up` is the whole remedy: the kubelet refreshes the mounted
  file and the exporters read it at each new connection, within about a minute
  and without a restart (measured by a review outside a cluster, not seen on
  one; the same refresh means that whoever may write the ConfigMap
  `telemetry-ca` in `meridian` changes what the services trust, live). The
  runbook
  [certificate-expiry](../../docs/operations/runbooks/certificate-expiry.md)
  says what to do. The alerts on certificates now read `observability` too.
- **What it does not cover.** The authority's private key is a Secret in
  `observability`, readable by whatever reads Secrets there or cluster-wide:
  cert-manager's controller, and in the rendered kube-prometheus-stack chart
  Prometheus's operator (it reads, creates and changes Secrets in every
  namespace; kube-state-metrics no longer lists them, S063). Whoever reads
  it can issue a certificate for the collector's name. This is tested without a
  cluster; it has not been run on one.

What this does not cover, on purpose: the edge to the Claims API is plain HTTP
and the Claims API's own certificate is for its calls out only (TLS at the edge
is a backlog row); a service loads its certificate once, and cert-manager does
not restart a Deployment's pods, so the service asks for the restart itself:
inside the last 24 hours of the certificate it loaded (the last sixth of its
life, for one that lasts under six days) it answers 503 on `/healthz` as soon
as the mounted file holds a renewed one, and the kubelet restarts the
container, about a minute in which a one-replica service does not answer; if
cert-manager has not renewed it, the service stays healthy until the
certificate ends and is unhealthy from then on, but two alerts fire long
before (21 days left; not Ready); a renewed CA still reaches a service only
when it restarts; no certificate is revoked; nothing limits which service's name a
request in `meridian` asks for (approver-policy lets the `meridian-services`
issuer sign only a request from `meridian` with a URI under the Meridian
prefix, so a request from another namespace is denied, but whoever can create
a `Certificate` in `meridian`, or change a policy or its binding, can still
mint any service's identity); the CA's private key is readable by the
operators that hold a cluster-wide read of Secrets (cert-manager, cainjector,
CloudNativePG), though by no Meridian pod; and the telemetry from the services
to the collector is TLS only by the second authority above (S063, tested without
a cluster), while the collector's own hops to Tempo, Prometheus and Loki are
still plain.

A cluster whose services were first applied as raw manifests (before S019)
keeps them: Helm adopted the objects in place (`--take-ownership`) and no
pod was replaced by the adoption. On such a cluster the old field manager,
`kubectl`, still co-owns the fields it set, so a field a later chart version
drops would stay until the object is re-created; a cluster made after S019
has no such owner.

`make demo` posts the claims in `data/synthetic/claims.json` in order to the
Claims API through the edge, with a W3C `traceparent` header whose trace ID the
script made up. A claim that already has a triage proposal answers 409 and the
script moves on to the next one, so each run uses the next claim (40 are
available); it stops at the first 201. A claim ID someone already submitted with
other content (through the claimant's form, which stamps its own report date)
answers 409 too and is skipped the same way; any other 409 stops the demo. It
prints the claim ID, the status, the route and the deployment the model call
went to, or that no model was called (never a claimant field, and not the
reason: the answer does not carry it), then reads the trace by that ID from
Tempo through Grafana's datasource proxy, retrying for up to 120 seconds. It
prints PASS only if the trace has spans from `claims-api`, `agent-runtime`,
`policy-mcp`, `knowledge-mcp` and `model-gateway` and the span counts have
settled: the same in three readings in a row, six seconds without change,
because the services flush their spans separately and an earlier reading can be
partial. It prints the settled span count of each service and exits non-zero
otherwise; when every service was there but the counts were still changing at
the deadline, the FAIL line says the trace was still growing. Every triage of a
claim whose policy exists touches the five: the graph looks up the policy and
its claim history, then searches the wording, and each search embeds its query
through the gateway even when the rules decide and the model is never asked.
`make grafana` shows the trace in Explore with the TraceQL query
`{ trace:id = "<id>" }`.

The script also prints the claim's state. A claim the rules referred to an
adjuster is `awaiting_adjuster`, its run paused in PostgreSQL (S015). The
script then posts the adjuster's decision, `DECISION` (`approve`, `reject` or
`request_documents`; `approve` when unset, anything else is refused before
any request), to `/claims/<id>/decision` with a trace ID of its own, prints
the claim's new state and the run's status, and PASSes only if that trace has
spans from `claims-api`, `agent-runtime` and `claims-mcp`: the Claims API
records the decision and resumes the run, which reads the recorded decision
and writes a note through the claims tool server. A claim that is not
referred needs no decision. A claim left waiting (the demo was stopped
between the two posts) answers 409 to the next `make demo`, which moves on;
decide it on the adjuster's pages (S016): open
`http://claims.meridian.localhost:8088/adjuster/claims` in a browser on the
laptop, which lists the claims that wait for an adjuster and those whose
triage failed; a claim's page shows its proposal, citations and audit trail
and records the decision. The pages have no sign-in yet (threat model
T-69), and the edge serves them only to the laptop.

## The scheduled sweep

A CronJob `meridian-sweep` (S052, in the chart's
[`templates/sweep.yaml`](../helm/meridian/templates/sweep.yaml))
runs `python -m meridian.workloads.claims_triage.sweep` every five minutes,
in the image `make deploy` built, as the database role `claims_sweep` (Secret
`claims-sweep-db`, never the owner's).

Implemented in S052, and run on kind there. One pass refers a claim whose
documents are overdue to an adjuster, fails a claim stranded in `submitted`
or `triaging`, ends a run no resume takes over and deletes the checkpoints a
finished run left; it logs one line with what it moved. It needs PostgreSQL
only: no call to any service and no model. The deadline for documents is
`MERIDIAN_SWEEP_DOCUMENTS_DEADLINE_DAYS`, 14 days of 24 hours from the
claim's latest request for documents. The Claims API reads the same
variable for the day its status page tells a claimant; the chart sets it
on both from one value, `sweep.documentsDeadlineDays` (14), and a test
holds the two rendered values equal (tested without a cluster).
`make smoke`'s seventh line checks that the job ran and finished on your
cluster.

`concurrencyPolicy: Forbid` governs only what the schedule starts: a scheduled
pass is skipped while another is running. A Job made by hand (below) runs
beside a scheduled one, which is why the role may hold 4 connections. A pass
is cut off after 120 seconds and is not retried (`backoffLimit: 0`): the next
run, five minutes later, is the retry. The CronJob keeps one succeeded and
three failed Jobs, and a by-hand Job counts toward those limits because the
CronJob owns it. A succeeded Job is removed when the next one finishes, about
five minutes later. Kubernetes removes any Job a day after it finishes
(`ttlSecondsAfterFinished`), and that day is what keeps a failure to read in
the morning, and the last success of a suspended CronJob. A pod has no
service-account token, no extra privilege and a read-only root filesystem,
mounts only the CA's public certificate and the collector's, and may reach DNS,
the database and the collector's port 4318, and nothing else (S019; the
collector from S064, where `telemetry.otlpEndpoint` is set, with the SDK's
export deadline `sweep.telemetryTimeoutSeconds`, 5 seconds, for the findings it
sends before it exits; tested without a cluster, not run on one).

To run one pass now, beside the schedule (the name is yours; it must be new):

```sh
kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian \
  create job --from=cronjob/meridian-sweep meridian-sweep-by-hand-1
kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian \
  logs job/meridian-sweep-by-hand-1
```

The by-hand Job counts as the sweep's last Job for `make smoke`. To stop the
schedule, patch `suspend` to `true` on the CronJob; `make smoke` then prints
SKIP for the sweep until it is `false` again.

## The cost dashboard

`make grafana`, then Dashboards, **Meridian: Model Gateway tokens and cost**
(S043). It shows the gateway's tokens, cost and calls for the selected time
range: three totals, tokens and cost broken down by tenant, agent, provider
or model (a selector at the top), a table by all four, and tokens per five
minutes by model. The file is
[`dashboards/gateway-cost.json`](dashboards/gateway-cost.json); edit it
there and rerun `make up`, because Grafana does not save changes made in
its editor to a provisioned dashboard. `make up` also removes the
ConfigMap of a dashboard whose file is gone.

What the numbers are, which the dashboard also says on its first panel:

- **Answered attempts only.** Tokens and cost count what a provider
  answered, with the provider's own token counts and the registry's prices.
  The ledger, table `gateway.usage`, is the record of what a tenant was
  charged, including a reservation kept after a failed attempt.
- **Cost reads 0 on kind.** The simulated `replay` deployments are priced at
  zero in the registry; cost is above zero only on a route to a priced
  deployment.
- **No `increase()` or `rate()`.** A gateway process exports its counters
  once a minute over OTLP, and its first export already carries what it
  counted, so `increase()` reported 0 for 17,319 ingestion tokens on this
  cluster. Each panel subtracts a series' value at the start of the range
  (zero for a process that started inside it) from its last value. Measured
  on 2026-10-03 over 12 hours, every panel and breakdown equalled the
  ledger: 18,512 tokens, 37 calls, EUR 0.
- Choose a range of at least two minutes; a shorter one can hold no export.

## The alert rules and the health dashboard

[`alerts/meridian.yaml`](alerts/meridian.yaml) is one `PrometheusRule`
(S024, S056) in four groups: `meridian.gateway.recording` (a recorded series
for the gateway's calls of the last 15 minutes), `meridian.gateway` (alerts
on that series), `meridian.workloads` (from kube-state-metrics) and
`meridian.certificates` (from cert-manager's controller). It holds 12 alert
rules and one recording rule: five on the gateway, three on the workloads
and four on the certificates. It
carries the label `release: kube-prometheus-stack`, which the chart's
Prometheus selects rules by. Prometheus evaluates the rules; kind runs no
Alertmanager, so nothing is notified, and the dashboard **Meridian:
platform health**
([`dashboards/platform-health.json`](dashboards/platform-health.json))
shows what fires, beside the services, the database, the sweep and the
share of model calls answered.

`make alerts` checks the rules without a cluster: it takes the rule groups
out of the manifest, runs `promtool check rules` on them and then the unit
tests in [`alerts/meridian.test.yaml`](alerts/meridian.test.yaml). The
checker is the `promtool` of the Prometheus version the chart installs.
The gateway's counter is read without `increase()` or `rate()`, for the
cost dashboard's reason (above); a unit test holds the case those would
lose.

`make up` applies both: the rules with `kubectl apply`, the dashboard as a
ConfigMap that Grafana's sidecar provisions. `make smoke` reads them back
(check 11, passed on the cluster on 2026-10-06): Prometheus has loaded the
four groups with every rule healthy, the loaded rule names are the file's,
no Meridian alert is firing, and Grafana serves the health dashboard with
the file's queries, every one of which runs in Prometheus. What stays by
hand is whether each series a rule or a panel names exists: a rule over a
series that is not there is healthy and quiet, and a panel with no data is a
success. The checklist is in
[the operations index](../../docs/operations/README.md#not-proved-on-a-cluster),
which also links the objectives the rules watch and the runbooks they
point to.

## If `make up` was interrupted

Rerunning `make up` is the first thing to try. If a release is stuck in a
`pending-*` state, or the node was only half created, run `make down` and then
`make up` again.

If `make up` times out waiting for the Gateway to be programmed while the
edge's proxy pod is ready, the condition is stale. The script stops with
"the wait for the Gateway edge to be Programmed ended without the condition
(it waits up to 5m ...)", says the edge may be serving all the same, and
prints the three commands below. The message does not say the wait ran five
minutes, because `kubectl wait` also fails at once, with "not found"; the
message kubectl prints above the error says which it was. (The wait after
it, for the proxy Deployment to be `Available`, words its message the same
way and stops with the pods and the controller's log to look at.) Seen
twice, with the edge
serving routes both times: `AddressNotAssigned` on 2026-10-01, on a cluster
that had run for 18 hours, and `NoResources` ("Envoy replicas unavailable")
on 2026-10-04, on one that had run for two. `kubectl -n envoy-gateway-system
get gateway edge -o yaml` shows the reason. Restart the controller, wait for
it, then touch the Gateway:

```sh
K="kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n envoy-gateway-system"
$K rollout restart deploy/envoy-gateway
$K rollout status deploy/envoy-gateway
$K annotate gateway edge meridian.local/reconcile-nudge="$(date -u +%FT%TZ)" --overwrite
```

Then run `make up` again. The message is tested without a cluster; it has
not yet been seen on one.

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
plane, the kubelet and containerd. The six Meridian services add limits of
192 MiB (Claims API, Model Gateway) and 256 MiB (Agent Runtime and each tool
server), 1.4 GiB in all; a Job adds 192 MiB while it runs. Measured from
cAdvisor on 2026-10-02, twice, after demo claims and smoke runs: the working
sets were 54 to 115 MB (the runtime the largest, the tool servers 72 to
87 MB), and `docker stats` showed 4.9 GiB for the node container.

## Deliberately not here yet

- TLS on the gateway: no step yet (the plan's follow-up backlog). The edge
  listens on loopback only.
- `enforce` for Pod Security Admission on the `meridian` namespace, which
  has `warn` and `audit` at `restricted` since S019 (as `cert-manager` has
  since S063, and `observability` at `restricted`): a server-side dry run
  of `enforce=restricted` reported no violation on `meridian`, but a cold
  `make up` under it (CloudNativePG's init Job) was not tried; in the backlog.
- A second replica of any service, and so a budget that protects one:
  whether each service is safe to run twice is not measured (S027).
- Alertmanager: Prometheus evaluates the alert rules and nothing is
  notified (S024). Routing and notification are designed, with the game
  day (S028) as their first use. Its Grafana datasource is off too.
- Persistence beyond the node: PostgreSQL, Loki and Tempo use small volumes on
  the node's disk, Prometheus and Grafana use none. `make down` removes all of
  it.
- Scraping of etcd, controller-manager, scheduler and kube-proxy is off: kind
  does not expose them to the cluster.
- Helm installs the CRDs a chart ships in its `crds/` folder but never
  upgrades them. A chart bump that changes CRDs needs them applied by hand.
