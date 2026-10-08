# Local platform on kind

`make up` builds the local Meridian platform on a one-node kind cluster from
pinned Helm charts. `make smoke` proves it works. `make deploy` puts the six
Meridian services on it (Claims API, Agent Runtime, Model Gateway and the
policy, claims and knowledge tool servers) and, since S066, the rate store that
holds the Model Gateway's rate windows, seeds the policy store and ingests
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
| CloudNativePG operator | `cloudnative-pg` | 0.29.1 (operator 1.30.1) | `meridian` |
| PostgreSQL 17 with pgvector (`platform-db`) | `cluster` | 0.8.1 | `meridian` |
| Prometheus, Grafana, kube-state-metrics (node-exporter is off, see below) | `kube-prometheus-stack` | 91.8.2 (Grafana chart 13.2.7) | `observability` |
| Tempo (traces) | `tempo` | 3.1.0 | `observability` |
| Loki (logs), with the chart's own nginx gateway in front of it (S072) | `loki` | 18.13.7 | `observability` |
| Prometheus's gateway: an nginx of this repository's own in front of Prometheus's port (S072, contract M4; no chart: [`manifests/observability-prometheus-gateway.yaml`](manifests/observability-prometheus-gateway.yaml), applied by `up.sh` right after the stack's release) | none | none | `observability` |
| OpenTelemetry Collector | `opentelemetry-collector` | 0.174.0 (collector 0.162.0) | `observability` |
| Log agent: a second release of the collector's chart, the contrib build, as a DaemonSet that ships the services' output to Loki (S064) | `opentelemetry-collector` | 0.174.0 (collector 0.162.0, contrib) | `logging` |

`up.sh` sources [`gateways.sh`](gateways.sh) after `common.sh`: the names and the
functions that fill in and apply Prometheus's gateway (`fill_placeholder`,
`prometheus_service_address`, `prometheus_gateway_manifest` and
`apply_prometheus_gateway`) live there, to keep `up.sh` under the size ceiling;
the call to `apply_prometheus_gateway` is still in `up.sh`, right after the
stack's release.

The CloudNativePG operator runs in `meridian`, beside the database it manages,
and has no namespace of its own (S072, contract C). The release is made with
`config.clusterWide=false`, so its rules over Secrets, ConfigMaps, pods
(`pods/exec` too) and roles are a Role in `meridian` and no longer a
ClusterRole over every namespace; the ClusterRole is left with nodes (read),
the webhook configurations (get, patch) and image catalogs (read). The chart
also renders two ClusterRoles, `cnpg-cloudnative-pg-view` and
`cnpg-cloudnative-pg-edit` (read and write on the operator's own objects), that
nothing binds or aggregates; they stay. Its pod has a NetworkPolicy of its own,
[`manifests/cnpg-operator-networkpolicy.yaml`](manifests/cnpg-operator-networkpolicy.yaml),
which `make up` applies before the release: the chart's `default-deny`, added
by `make deploy`, selects every pod of `meridian`. **Implemented in files and
tested without a cluster; not seen on kind.** If one cold `make up` does not
bring the database up under it, the change is taken out again and the
operator's reach is recorded as accepted with "tried, and what failed" (the
comment above the install in [`up.sh`](up.sh) says how, and what that looks
like). A cluster made before this change needs `make down` and then `make up`;
`make up` refuses such a cluster, before it changes anything, when the old
namespace exists, and says so (F2 of S072; not seen on kind)
(`make down` deletes the kind cluster, `down.sh` says so; that is allowed on a
disposable cluster and never to clear a fault nobody has looked at, as the
cluster's database holds the only copy of the audit log): Helm does not move a
release, and the old one owns the CRDs and the webhook configurations.

Besides the releases, `make up` makes the Secrets the platform needs and
never overwrites one: the eleven database roles', Grafana's admin password
and, since S066, `rate-store-credentials` in `meridian`. That one holds two
keys for the rate store that the Meridian chart runs (`make deploy`, below):
`uri`, the Model Gateway's address in Redis,
`rediss://gateway:<password>@rate-store.meridian.svc:6379/0` (the host is the
DNS name of the store's Certificate, which the gateway verifies), and
`users.acl`, Redis's access-control file, which only the store's pod mounts:
the `default` user off, and one user, `gateway`, that is on, with the SHA-256
of the password and not the password, the keys `~meridian:rate:*`, no channel
and exactly the commands the gateway's connection and script send: `evalsha`,
`script|load` (that one subcommand alone), `time`, `zremrangebyscore`,
`zrange`, `zadd`, `pexpire` and `hello`. Nothing else: no `client`, no
`script flush` or `script kill`, no `eval`, no `function`, no `keys`, no
`del`. Within those five commands the gateway's user can do more than run the
gateway's script: it can run them directly on any tenant's key, so it can read,
fill, empty or freeze any tenant's windows, and it can load a script that never
ends (the ledger in PostgreSQL it cannot touch). A second user, `probe`, is on
with no password (`nopass`), no key, no channel and exactly `ping`: the store's
two probes run `PING` as it and pass only when the answer is `PONG`, so a store
frozen by such a script (every other client gets `BUSY`) fails them and the
kubelet restarts the pod within about a minute. It may only run `ping`, which is
not nothing: with no password, any client that holds a certificate of the
services' CA (all six services do) and has a network path to the port is an
authenticated session of that user, and may fill its query buffer; what bounds
that is the store's NetworkPolicy and `maxmemory-clients` (below), not the
user. The password is 32 random bytes as 64 lower-case hex digits, an
alphabet that needs no percent-encoding in an address, so the address is
exact; it goes to kubectl on standard input and is never an argument, a file
or output. The Secret carries the annotation
`meridian.kind/rate-store-acl-rules`: the SHA-256 of the ACL file with the
password's hash masked, so it names the users, their command lists and their
key patterns and holds no secret. **A Secret that exists is kept**: an ACL
that changed in `up.sh` reaches a running cluster only by deleting the Secret
(the cluster is disposable on the development machine, and deleting it is a
rotation of the store's password: the owner's to confirm), running `make up`
again and restarting the store and then the gateway
([runbook](../../docs/operations/runbooks/rate-store.md)); `make deploy`
refuses a cluster whose Secret lacks a key, lacks the annotation (a Secret
from before the `probe` user has none and no such user) or has one that is not
the hash of what `make up` would write now, and says to do that. The gateway's
list was read from Redis 8.10.2 under that user, on plain TCP outside a
cluster, with the gateway's own client and limiter: a cold call, a warm call
and a call after the server lost the script ran with an empty access-control
log, and `GET`, `KEYS`, `DEL`, `FLUSHALL`, `CLIENT LIST`, `SCRIPT FLUSH`,
`EVAL` and a key outside the pattern were refused. The `probe` user and the
probes were run on the pinned image over TLS, read-only, as the image's user,
with the chart's rendered configuration and probe scripts (a healthy store, a
store frozen by a looping script, and the restart that ended it). Those
proofs, and the refusals above, were made outside a cluster: no session has
frozen the store on kind or tried a refused command there. Seen on kind on
2026-10-06 (third run, on a cluster made from nothing; local only): `make up`
made the Secret, the store started under its file and its probes, and its pod
was 1/1 Running with no restart ten minutes later; a ping as `probe` in the
store's container, over TLS with the container's own certificate, returned
`PONG`; and the gateway's calls completed (`make demo`), so its user works.
`ACL LIST` needs a credential that a session does not have, so the file on a
cluster cannot be listed whole. What can be read without one: that ping (the
chart's probe does that); a login as the gateway with a wrong password is
refused (`WRONGPASS`); and the annotation is the hash `make deploy` checks.

Every version and image digest is in [`pins.env`](pins.env), the only place
to change one. `.github/renovate.json` reads them, so Renovate, once the
owner has installed the app, proposes newer ones in grouped pull requests
each month. CI does not start this platform: such a pull request needs
`make up` and `make smoke` before it merges, and the table above follows
by hand. The values that override chart defaults are in
[`values/`](values/); the Gateway, the namespaces, Grafana's Role and the
NetworkPolicies of the database, `cert-manager`, `observability`,
`envoy-gateway-system`, the log agent's namespace and smoke's Jobs are in
[`manifests/`](manifests/). The Meridian services have a chart of their own,
[`../helm/meridian/`](../helm/meridian/), which `make deploy` installs (below).

### The images the charts run (S063)

Pinned means a digest, or an image that is never pulled (S019). Since S063
every image a chart starts here is pinned by the multi-architecture index
digest of its tag, each in [`pins.env`](pins.env) as `X_IMAGE_TAG` and
`X_IMAGE_DIGEST` under a `# renovate:` comment, and `make up` passes both to
the chart with `--set`. The tag is the one the chart installs by default at
its pinned version, except the collector's and the log agent's (S064): the
chart's appVersion is 0.161.0 and the pins are 0.162.0 (the collector's was
pinned before this step, and the agent's contrib build is the same release,
so the two move together). A chart upgrade moves
the tags with it, by hand, in the chart's pull request: Renovate does not
propose a new tag for an image a chart installs (`.github/renovate.json`
switches off its major, minor and patch updates for them, so no image arrives
before the chart that installs it), only a new digest of the tag in place. The
pull request that moves a chart reads the chart's defaults again (below) and
writes each new tag, and the index digest of that tag, into `pins.env`. The
collector's and the log agent's images are outside that rule until their pins
and their chart agree, and Renovate still proposes their tags. What a chart's
key takes is not
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
| meridian (the rate store, S066) | `docker.io/library/redis:8.10.2-alpine` | `rateStore.image` (`RATE_STORE_IMAGE`), passed by `make deploy` with `--set-string`, not by `make up`; the same image and digest as the Makefile's `PYTEST_REDIS_IMAGE`, which the tests and CI run against, and one Renovate group moves both | yes |
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
| loki | `docker.io/nginxinc/nginx-unprivileged:1.31-alpine` | `gateway.image.tag`, `.digest`: the chart's gateway in front of Loki (S072); the same pin, `NGINX_GATEWAY_IMAGE_*`, is also the image of Prometheus's gateway, which `up.sh` writes into that Deployment | yes |
| otel-collector | `ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector:0.162.0` | `image.repository`, `.tag`, `.digest` (before S063) | yes |
| log-agent | `ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib:0.162.0` | `image.repository`, `.tag`, `.digest`: the contrib build of the collector's release, which has the receiver that reads files (S064; digest read 2026-10-06) | yes |

The Prometheus tag is also the Makefile's `PROMTOOL_IMAGE`. Every image left
by tag is one that nothing starts: if an ACME issuer, a rate-limit policy, a
Thanos sidecar or `helm test` is added, its image needs a pin first.

The mock issuer for sign-in (S021) is Keycloak, `KEYCLOAK_IMAGE` in
`pins.env`: one line, `quay.io/keycloak/keycloak:26.8.0` by the index digest,
because no chart installs it (the Deployment in the namespace `identity` does,
`identity.sh` reads the pin, and `up.sh` passes it to no chart) and Renovate
proposes its tag in a group of its own. It is an add-on that `make up` makes
only with `MERIDIAN_IDENTITY=keycloak`; "The sign-in issuer" below is its
section. `identity-realm.sh OUTPUT_DIR REDIRECT_URI WEB_ORIGIN` writes the
staff realm it imports (`meridian-staff`: four roles, a test user per role, the
pages' client with the code flow and PKCE, the scripts' client with client
credentials) and the password of each user and the secret of each client, new
at every run, into a mode-600 `secrets.env` beside it; nothing is printed, and
the directory must be one git ignores (`infra/kind/.identity/`) or outside the
repository (`identity.sh` uses a folder under the user's cache). Status: the
generator is implemented and tested without a cluster (`make test`); the image
was run in a container, outside the cluster, by an opt-in rig
(`MERIDIAN_KEYCLOAK_RIG=1 uv run pytest tests/meridian/test_keycloak_rig.py`,
about 2 minutes and 2.5 GB free; 6 passed against the pinned image on
2026-10-08). By the owner's answer of 2026-10-08 ("Keep it, opt-in only
(Recommended)") Keycloak on kind is an add-on that is off unless switched on
and not part of plain `make up`; the switch, the script, the manifests and the
smoke lines are tested without a cluster and were run on kind once (run KR1,
2026-10-08); what that run did not show is listed under "The sign-in issuer".

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

The digest to write next to a tag is the one of the tag's multi-architecture
index. This prints it without pulling the image (it reads the registry):

```sh
docker buildx imagetools inspect quay.io/jetstack/cert-manager-webhook:v1.21.2 \
  --format '{{.Manifest.Digest}}'
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
logs to Loki's OTLP endpoint, and since S072 those three hops are TLS with the
collector's client certificate: Tempo's receiver asks for it on its own port,
and Prometheus and Loki are reached through a gateway each (an nginx, below)
that asks for it on the one write path (the OTLP path) and serves the reads to
a client that has none. The threat model's T-90 has the decision and its dated
reversal. Grafana reads Prometheus and Loki through the same gateways, over
TLS, with no certificate; it has three datasources with fixed uids:
`prometheus`, `tempo` and `loki`. Retention is 24 hours everywhere.

The services' logs reach Loki another way (S064): they write to their output,
and the log agent, a DaemonSet in `logging`, reads those files on the node and
sends each line to the same collector as an OTLP log record (see "The log agent
and the namespace `logging`" below, which also says what that pod can read).

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
(cert-manager's controller and cainjector; the render of 2026-10-07 shows
Envoy Gateway's controller and the Prometheus operator with a cluster-wide
rule over Secrets too). The CloudNativePG operator reads
Secrets in `meridian` only since S072 (contract C; implemented in files, not
seen on kind): there it still reads and writes every Secret and ConfigMap, the
services' keys among them, as before, but no longer those of `cert-manager` or
`observability`. The
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
  only from the `meridian` namespace, with a URI and DNS names out of the
  lists of what the chart renders (no wildcard; a test holds them equal to the
  chart's Certificates; it stops a request for a new name, not a second
  Certificate for a listed one), the
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
`meridian` has any listed service's identity issued (the policy checks the
namespace and the names, eight URIs and six DNS names, each listed, not which
service), and whoever can change a policy or its binding undoes the limit; on
kind that is the cluster's administrator.

`make up` installs approver-policy and applies the policies before the CA,
waits for the five to be Ready, and then waits for the issuer to be Ready
before it installs the database. On a cluster where cert-manager already ran
with its approver on, `make up` turns the approver off first and brings the
policies seconds later: the certificates already issued are not touched, and
a request made in between waits and is then decided.

Between the install and the policies `make up` gives approver-policy's
Deployment a liveness probe (S073). The chart has a readiness probe, no value
for a liveness probe, and a values schema that refuses a key it does not know,
so the probe cannot be a chart value:
[`manifests/approver-policy-liveness.yaml`](manifests/approver-policy-liveness.yaml)
holds the one field and `up.sh` applies it server-side under the field manager
`meridian-kind`, without forcing conflicts, then waits for the rollout (five
minutes at most) before the policies are applied. The binary serves `/readyz` on
its health port and nothing at `/healthz`, so the probe asks `/readyz` of the
port the chart names `healthcheck`: every 20 seconds, five seconds to answer, six
failures in a row (two minutes) before the kubelet restarts the container. It
catches a frozen process or a dead listener; a reconciler that is stuck while
HTTP still answers it cannot see, which is
`MeridianCertificateRenewalOverdue`'s to see, and a hang that comes back and is
restarted again and again is `MeridianCertificateIssuingRestartLoop`'s. Seen on
kind on 2026-10-07 (run R13): the probe on the Deployment; a second `make up` of
the same chart a no-op, the same pod and the field still owned by
`meridian-kind` alone; ten quiet minutes; a frozen process restarted by the
kubelet 140 seconds after it stopped. Not seen: an upgrade to a newer chart, the
probe under real load and a cold install with this step.

What follows from the probe not being the chart's, and is written, not seen:
- A bare `helm install` after a `helm uninstall` has no probe until the next
  `make up`; a plain `helm upgrade` and a `helm rollback` leave the field
  alone (neither rendered manifest holds it, by the review's reading of Helm's
  apply).
- The probe names the port `healthcheck`. A chart that renames the port makes
  the kubelet discard the probe's result and nothing restarts, silently; the
  guard is `tests/meridian/test_kind_approver_liveness.py`, which pins the chart
  version, the image tag and the names it was read against and fails when
  `pins.env` moves, so a Renovate pull request for the chart goes red there.
- If a newer chart renders its own liveness probe, Helm fails on a conflict on
  `livenessProbe` owned by `meridian-kind` (inside `make up`'s Helm step on a
  cluster that has the probe, which names no file; inside the apply, with the
  remedy in its message, on a cold install). The remedy has two steps, in this
  order, and was not tried on a cluster: apply once, under the field manager
  `meridian-kind`, a copy of the manifest without its `livenessProbe` lines,
  which drops the old owner without a force flag (deleting the manifest and the
  function alone leaves that owner and Helm conflicts again); then delete the
  manifest and `apply_approver_liveness_probe` from `up.sh` and set the chart's
  value.

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
`knowledge_ingest`, which holds the knowledge chunks, an insert on the audit
log and, since S067, a read of seven columns of the chunks (not the vector):
only the migration Job holds the owner's Secret, and each of the
other two Jobs alone holds its role's. Those two roles have no connection
limit, as the owner's Jobs had none. The sweep's role may hold at most 4
connections: its job runs one pod at a time and holds one connection at a
time, a run by hand beside the scheduled one makes two pods, and each may open
a second connection while it replaces a broken one. The upkeep role may hold at
most 2: the command opens one connection for milliseconds. That bounds what a
holder of the credential can hold open; it does not stop one session from
sitting in an open transaction. Only an operator uses `gateway_upkeep`, and on
the cluster through `make gateway-upkeep` (below; the runbook
[budget exhaustion](../../docs/operations/runbooks/budget-exhaustion.md#the-upkeep-command)
says how): a Job of its own that `infra/kind/upkeep.sh` renders from the chart
and applies outside the release, so no workload of the release holds its Secret
`gateway-upkeep-db` (the Deployments, the sweep's CronJob and the three Jobs
`make deploy` runs), and a test keeps it so. The Job exists only for the one
run, and is kept for a day for its output. Implemented and tested with stub
commands and the real chart, and seen on kind on 2026-10-06 (second and third
runs): the read of the open reservations, a refusal that failed the Job (`ERROR
GU304`) and a credit of one token, each as a Job of its own. The audit row of
that credit was not read on the cluster. The three tool-server roles may each
hold at most 20 connections: a tool server runs at most eight calls at once, one connection
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
adds one route, for the Claims API only (below; a second one for the file
paths when the uploads switch is on, which it is not on kind); every other
host and path answers 404.

## Prerequisites

Docker (running), `kind`, `kubectl`, `helm`, `openssl` and `jq`. `make smoke`
and `make demo` also need `curl`. `make up`, `make deploy`, `make smoke` and
`make gateway-upkeep` also need `timeout` (coreutils; it bounds a call of
`kubectl` that no flag bounds, see "How long the scripts wait for the API
server", S073), and refuse to start without it. The wrapper reads two behaviours
of it, the statuses 124 (ended) and 137 (killed after the grace); GNU `timeout`
gives both, and so did uutils 0.10.0, the one on the machine of the S073 runs
(run R4e used it): the re-read of K9, K10 and K12 saw both statuses behave the
same there. Tested with:

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
| `make up` | Create the cluster if absent, install every release, provision the Grafana dashboards and apply the alert rules. Safe to rerun; it converges. On a cluster that exists it first reads who holds it and stops when another holder has it, unless `TAKE_CLUSTER=1` is in front of it; it records itself as the holder with state `changing` before it changes anything and with state `ok` when it ends well, so a run that fails leaves `changing` (S075; see "Who holds the cluster" below). Took 4 to 5 minutes from no cluster (245 s and 304 s, images already local, on the laptop; 5 min 04 s and 4 min 28 s on the Linux machine of the table above), under a minute after. |
| `make deploy` | Needs `make up`. Builds the image, loads it into the node, runs the migration and seed Jobs, installs or upgrades the Helm release `meridian` from [`../helm/meridian/`](../helm/meridian/) (the sweep's CronJob and the network policies among its objects), ingests the wordings once per image and waits for the six Deployments and the route. Safe to rerun. Before it builds or runs anything it reads who holds the cluster and stops when another holder has it, unless `TAKE_CLUSTER=1` is in front of it; it records itself as the holder with state `changing` right after that check and with state `ok` when it ends well, so a run that fails leaves `changing` (S075; see "Who holds the cluster" below). Its first change is Meridian's alert rules, the same `PrometheusRule` file `make up` applies, through one function of `common.sh`: after its checks, so a refused deploy changes nothing, and before the build, so a cluster without the Prometheus operator is refused in seconds with a sentence that names the kind (S073; tested against stub commands, not yet seen on a cluster). The first deploy of an image waits a minute after the ingestion (below). |
| `make images` | Lists the `meridian:*` images in the Docker engine and in the kind node, each marked `in use` (a pod template of the namespace's Deployments, CronJobs and Jobs names it, or a Pod that exists), `rollback` (only an old ReplicaSet names it: a rollback's target, kept, with no command) or `unused`, with the counts and the size Docker reports, and prints the commands that would remove the unused ones. It removes nothing: removing them is the owner's command. With no `infra/kind/kubeconfig` it asks kind: no cluster of that name, and it lists the engine's images, all unused; a cluster that exists (the credentials are in another checkout) is an error, because it cannot tell which images are in use. A cluster that does not answer, or a listing that fails, is an error too. Run on the cluster on 2026-10-06: after three deploys it listed three `meridian:*` images in the engine and in the node, one `in use` and two `rollback` (an old ReplicaSet names each), kept with no removal command, and removed nothing; on the cluster made again from nothing it listed three in the engine, one in use and two unused (no ReplicaSet of the new cluster names them) with the `docker image rm` line printed for them, and one in the node. The refusal in a checkout without the cluster's credentials was tested against stub commands and not tried on the cluster. |
| `make helm-lint` | `helm lint --strict` on the chart with kind's values and every Job on (the upkeep Job with one argument and a suffix, which it needs to render). Needs no cluster; CI runs it. |
| `make alerts` | Prometheus's own checker (`promtool`, from a pinned image) on the alert rules, then their unit tests. Needs Docker and no cluster; CI runs it. |
| `make demo` | Runs `make deploy`, then posts a synthetic claim and finds its trace in Tempo; when the claim waits for an adjuster, posts the decision (`make demo DECISION=reject`; approve by default) and finds that trace too. Prints PASS only when each trace has at least one span from each service it must cross (a service Tempo lists with no span does not count) and its span counts have settled (unchanged for three readings, six seconds). Passed on the cluster on 2026-10-06 with spans from every service, in 30 s; the zero-span rule and the FAIL wording "alternated" were tested against a stub and not seen on the cluster. |
| `make smoke` | One PASS, FAIL or SKIP line per check; exits non-zero on any FAIL. |
| `make gateway-upkeep ARGS="..."` | Needs `make up` (the Secret `gateway-upkeep-db`) and `make deploy` (the image). Runs the gateway's upkeep command (`meridian gateway`, S066) as a Job of its own under the database role `gateway_upkeep` and prints its output; `ARGS` is the subcommand and its arguments (`reservations --older-than 15`, `close ATTEMPT_ID --reason SLUG`, `credit TENANT --tokens N --reason SLUG`, `expire --before YYYY-MM --reason SLUG [--limit N] [--confirm]`, `expire-audit --before YYYY-MM-DD --reason SLUG [--limit N] [--confirm]`; only the date form of `expire-audit` passes the word check below, which allows no colon or plus sign). `infra/kind/upkeep.sh` splits `ARGS` on blanks into an array without reading any of it as shell and refuses, before it asks the cluster anything, a word with a character outside letters, digits, `.`, `_`, `=` and `-`: a quote, a backslash, a `$`, a backtick, a newline or a glob character among them. It passes the words to `helm template` as one JSON list (`--set-json`), with a suffix of its own so that a second run is a new Job, applies the Job outside the release (`make deploy` neither creates nor removes it) with the image the release runs, waits for it and prints its log through the same filter as the deploy's Jobs. Exit code 0 when the Job succeeded; 1 when it failed (the command exits 1 on a refusal, `ERROR GUnnn`, and 2 on a usage error: both are a Failed Job, never retried), when it did not finish in three minutes or when `make up` or `make deploy` is missing (`make` itself returns 2 for the failed recipe). A failure whose output holds a line that says what the command removed "before the failure" (the two expiries remove in batches, and a failure can follow batches that committed) says what stays removed and that running the command again continues; one that holds the command's own `ERROR GUnnn` line and no such line says that the refusal changed nothing; every other failure, and a Job that did not finish, says that the change may have been applied and to read the reservations or the audit rows before running it again, because a credit is a new row on every run. Make itself expands `$(...)` and `$$` in a value given on its command line before the script sees it (the script's header and the target's help line say so); what reaches the script is then checked as above. The Job and its output are kept for a day. Implemented and tested (stub `kubectl`, the real chart), and run on kind on 2026-10-06 (second and third runs of S066): a read, a refusal and a credit of one token; the audit row of the credit was not read on the cluster. |
| `make grafana` | Port-forward Grafana to <http://127.0.0.1:3000>. User `admin`. |
| `make grafana-password` | Print the Grafana admin password. |
| `make cert-renew CERT=<name>` | Ask cert-manager to issue one Certificate of the namespace `meridian` again, now (S073). After a denied or failed request cert-manager waits before it asks again (an hour, doubling to 32), so a repaired policy does not help a deploy for an hour; `cmctl renew` asks at once and is not installed, so `infra/kind/cert-renew.sh` does what it does with `kubectl`: it sets the Certificate's `Issuing` condition to `True` (reason `ManuallyTriggered`, the time now, the Certificate's generation) through the status subresource, as a JSON merge patch that carries every other condition and the `resourceVersion` it read (a Certificate cert-manager changed in between is refused, and the command is run again). It refuses, before it asks the cluster anything, a missing `CERT` and a name that is not a DNS label, without repeating the value, and then a name that is not a Certificate of the namespace, listing the names it found; a Certificate that is already being issued is left alone. It reads no Secret and writes nothing but that status. It reads who holds the cluster first, stops when another holder has it unless `TAKE_CLUSTER=1` is in front of it, and writes the record `ok` right after the write succeeded (a refused write leaves the record as it was). With `CERT=rate-store` it first says what the renewal costs: the store restarts itself when its certificate file is newer than its start, so every model call answers 503 for one to three minutes (worked out from the probes' numbers, not measured) and the tenants' rate windows are lost (the six services do not restart on a renewal by hand). `make deploy`'s stop at the Certificates names it. Tested against a stub `kubectl`. Seen on kind on 2026-10-07: a refusal for each bad name (no `CERT`, a name that is not a Certificate, a name that is not a DNS label), each with its own sentence and nothing written, and one renewal of a healthy Certificate (revision 1 to 2 within the same second, a new CertificateRequest Approved and Ready, the record back at `ok`). Not seen: the renewal of a Certificate whose request was denied (the case it is for), a refusal by a `409` between the read and the write, the rate store's renewal, and the script's one write of the record (the renewal seen ran the script before that change, when it wrote `changing` and then `ok`). |
| `make cluster-holder` | Print who holds the cluster: the holder, its commit, the time its last `make up` or `make deploy` started or ended and the state, `ok` or `changing` with a sentence that says to look at what failed (S075); or that there is no record, or no cluster. It changes nothing on the cluster and refreshes the gitignored credentials file as `make up` does. A cluster that does not answer is an error. See "Who holds the cluster" below. |
| `make down` | Delete the `meridian` cluster and its credentials file. Destructive; refuses any other cluster name. Since S075 it reads the record of who holds the cluster first and stops when another holder has it, unless `TAKE_CLUSTER=1` is in front of it; a cluster that does not answer stops it too (who holds it cannot be told, and another step's `make up` may be restarting the node), and `TAKE_CLUSTER=1 make down` deletes a broken cluster all the same. The record goes with the cluster. |

`make smoke` checks twelve things:

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
   message instead of hanging it (the two pgvector lines keep the first line
   of it since S073, K4, cleaned and cut like the others: a read that failed
   says "could not read pg_extension", not "not installed"; tested with
   stand-ins, not yet seen on a cluster). Those reads passed on the cluster on
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
4. **Telemetry.** Nine lines (six from S063; seen on kind on 2026-10-06: the
   ConfigMap line, the clear-text line answering `400` and telemetrygen's three
   lines passed. Not seen: a renewal of the collector's certificate or of its
   authority, and a cold start. The rule that only a 400 passes, the Jobs'
   deadline and check 8's dependence on the push are tested without a cluster
   until the next run; the eighth line, S064's, passed on kind on 2026-10-06
   in all three of S064's runs (with the agent as root in the first two and as
   user 10001 in the third); the seventh and the ninth, from the infra review,
   are described at the end of this item and passed in the third run, where
   `make smoke` printed 44 PASS, 0 FAIL and 0 SKIP). The first two are about
   TLS and do not need the Meridian services. The ConfigMap `telemetry-ca` in
   `meridian`, which the six
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
   text. The last three lines are about the log agent, after the three
   read-backs.
   The seventh line (from the infra review of S064) reads the agent's live
   DaemonSet with one `kubectl get -o json` and checks the facts of its pod
   that a bump of the collector's chart could change without a test noticing
   (no test renders that chart, and the Renovate group's note already says to
   run `make smoke`): the one hostPath volume is `/var/log/pods` and its mount
   is read-only; no host network, host PID or host port; `runAsNonRoot`; and no
   service-account token. PASS says the four facts; a FAIL names the first that
   does not hold, in a word of the script's own and never a value of the
   object; a SKIP says the DaemonSet is not there. It is read after `make up`
   alone too, so it PASSes there. It does not prove the pod runs, or that its
   security context is complete. The user was seen on kind on 2026-10-06
   (the third run: uid and gid 10001, `runAsNonRoot`, the supplementary group
   0, and a server-side dry run of `enforce=restricted` on `logging` that
   warned of the hostPath volume alone); the capabilities and the seccomp
   profile are the values file's, tested without a cluster.
   The eighth line (S064) asks the Claims API, through the edge the adjuster
   pages use, for `/smoke-<epoch>`, a path that does not exist, and expects a
   404; the marker is in the path and not in a query, because the access line
   keeps no query. It then looks in Loki, within the same wait as the log line
   above, for a record of the service `claims-api` whose `path` is that marker
   and whose `status` is 404 (`{service_name="claims-api"} |
   path="/smoke-<epoch>" | status="404"`, filters on the record's fields,
   which Loki keeps as structured metadata). PASS prints the record's body.
   Three FAILs: the edge did not answer 404; Loki answered and has no such
   line (the agent is not sending, the services' image does not write the JSON
   access line, or the line is not what the query reads); Loki did not
   answer. One SKIP replaces it while the Meridian services are not deployed,
   so it is a SKIP after `make up` alone, and while the agent's DaemonSet is
   not there. It proves one service's one line made the whole way; it does not
   prove that every service's output arrives, that a line that is not JSON
   arrives (a crash, output before a service set up its logging), or that the
   checkpoint survives a restart.
   The ninth line (from the infra review) is what the eighth cannot say: that
   nothing but the services' output is read. Over the last hour Loki holds no
   stream of a container named `postgres` (`{k8s_container_name="postgres"}`)
   and none whose namespace is not `meridian`
   (`{k8s_namespace_name=~".+", k8s_namespace_name!="meridian"}`: Loki refuses
   a selector whose every matcher can match an empty value, and `!=` is one,
   so the first matcher cannot). A FAIL says which kind and how many, never a
   label. Before the two, a control: the Claims API's own stream by the same
   two labels (`{k8s_namespace_name="meridian",
   k8s_container_name="claims-api"}`) must be there in the same window, or the
   line FAILs (not SKIP: the eighth line has just found its access line by
   `service_name`) with a text that says the two labels are not there to
   select by, so the two empty answers would prove nothing, as they would if a
   Loki stopped indexing either label. PASS says what was found and what was
   not. It runs only after the eighth line passed, because while nothing is
   shipped an empty answer proves nothing: otherwise it is a SKIP, which it is
   after `make up` alone (so that run prints 32 lines, and one after
   `make deploy` 45, with S066's rate store line). It does not look for a
   Job's or a smoke pod's output, which would be in streams of the namespace
   `meridian`; the include list keeps them out, and the query for them is by hand:
   `{k8s_container_name=~"migrate|seed|ingest|probe|telemetrygen"}` over a
   window that starts after the `make up` that installed this agent, because
   Loki keeps 24 hours and the first form of the agent shipped them.
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
7. **Sweep.** Two lines, read-only (the second is S064's, below). The first:
   the CronJob `meridian-sweep` exists, and
   the last of its Jobs that the schedule made to finish succeeded (a Job made
   by hand is not one: see below); the
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
   A Job made by hand (`kubectl create job --from=cronjob/...`) has the
   CronJob as its owner like a scheduled one, so by the owner alone a recent one
   would pass for the schedule's success (S073, K4). The verdict tells them
   apart by two annotations seen on kind: a by-hand Job carries
   `cronjob.kubernetes.io/instantiate: manual` and a scheduled one carries
   `batch.kubernetes.io/cronjob-scheduled-timestamp`. Only a Job that has the
   first and not the second is left out (the scheduled timestamp is the
   positive fact and wins); a Job with neither, as on an older cluster, counts
   as before and the line says the annotation was absent. When the newest
   finished Job of all was made by hand the line says so, with its name and
   time, and that a by-hand run is not a run of the schedule: a recent by-hand
   success beside a schedule that stopped is the stopped verdict, and a by-hand
   failure beside a healthy schedule is a PASS. Seen on kind on 2026-10-07: the
   CronJob kept ONE successful Job, so a by-hand success evicted the schedule's
   own, and the verdict, resting on the failed scheduled Job of the evening
   before, failed a healthy schedule for four minutes. The chart now keeps
   three (`successfulJobsHistoryLimit: 3`), and the verdict has a third case
   for a history that still lacks the schedule's newest run (its newest
   finished Job finished before the CronJob's `lastScheduleTime` and a by-hand
   Job finished after it): that Job is not judged, and `lastScheduleTime`
   decides. Older than 15 minutes is the stopped verdict (FAIL); within it,
   with a Job of the schedule running or not, the line is a SKIP that says the
   schedule fired at that time, its Job is no longer in the history and its
   outcome was not read: run smoke again after the next scheduled run. After a
   by-hand run, then, the sweep lines may print two SKIP in place of two PASS
   (the findings line follows the first) until the schedule's next run. The fix
   is tested with stand-ins. Seen on kind on 2026-10-07 (run R4c): with the
   limit of three, a by-hand Job made after one scheduled run did not evict the
   schedule's success, and the line passed on the schedule's Job and said in
   brackets that the newest finished Job of all was made by hand and not
   judged; with the by-hand Job removed smoke passed again. Not seen:
   the third case itself (a history that lacks the schedule's newest run: its
   SKIP, and the FAIL once the schedule's time is older than the bound), which
   the limit of three keeps from happening. Someone who edits a
   Job's annotations can pass for the schedule, and the alert
   `MeridianSweepStale` reads the CronJob's last successful time, which a
   by-hand success moves too (seen on kind on 2026-10-07): a by-hand run can
   hide a stopped schedule from the alert for one staleness window.
   Seen on kind on 2026-10-07 as well: after seven runs of `make smoke` in an
   hour this line failed with `jq: Argument list too long`. It listed every
   Job of the namespace and handed the list to `jq` as one argument, which the
   kernel limits to 131,072 bytes; each run leaves four Jobs of its own (the
   telemetry and log probes, kept 15 minutes after they finish), and 28 of them
   made the list 227,658 bytes. The cause was older than S073. Now the line
   lists the Jobs by the sweep's label (`app.kubernetes.io/name=meridian-sweep`,
   which the CronJob gives every Job it makes, scheduled or by hand), and no
   answer of the cluster or of Prometheus goes to `jq` as an argument anywhere
   in the script: each goes in on standard input or as a file from a process
   substitution (a test reads the script for it). The fix is tested with
   stand-ins and the real `jq`, with a list above the limit. Seen on kind on
   2026-10-07 (run R4c): with 17 Jobs in the namespace, a list of 123,794
   bytes, `make smoke` passed this line, 45 PASS and no FAIL, three times in
   six minutes, where run R4b had failed it at 227,658 bytes. Not seen: a
   list over 131,072 bytes again: the label leaves the namespace's size out of
   the line, so only the test with the real `jq` holds that case.
   The second line (S064) asks Prometheus, through Grafana's datasource proxy
   as check 5 does, whether the six findings of the pass have arrived: the
   gauge `meridian_sweep_last_pass` for job `claims-sweep`, each of
   `documents-overdue`, `triage-not-started`, `triage-abandoned`, `runs-ended`,
   `threads-cleaned` and `failures` with a sample in the last 15 minutes, in
   one query that takes the last value of each over every `instance`, waited
   for up to 120 seconds. PASS names the six values. FAIL tells three causes
   apart: Prometheus did not answer with status `success`, it has no finding at
   all, or it has some (the line names the ones missing). Only the six words
   the script holds are printed, never a label from the answer. SKIP, in one
   line, when the first line was not a PASS (no pass has finished), when the
   Job it passed was made without `OTEL_EXPORTER_OTLP_ENDPOINT` (the first
   smoke after a deploy that added the address reads a Job made before it: wait
   for a pass), and when Grafana could not be reached. After `make up` alone it
   prints SKIP, with the first line. It passed on kind on 2026-10-06 in the
   third run (the six values, from one `instance`, `claims-sweep`; all six
   findings were 0 in the first run); its
   FAIL and SKIP branches are tested without a cluster and were not seen.
8. **Network policy.** Six lines. Each opens a TCP connection and sends
   nothing, from Python in a pod (the image has no curl); a denied path passes
   only when it times out (a refused connection or a name that does not
   resolve fails), and a path that answers fails the line. The first line is
   the control: the Claims API's pod reaches the Agent Runtime, which the
   Claims API's policy and the Agent Runtime's both name, so a "blocked" below
   is not a broken probe (if it fails, the other five are not printed). Then
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
   probes 4318), or that a namespace other than `default` is refused. The
   sixth line (S066) is the rate store's and it proves the store's ingress
   rule, not the sender's egress rule. From a probe pod of its own (the sweep's
   name label and smoke's) the connection to `rate-store.meridian.svc:6379`
   must time out. [`manifests/smoke-rate-store-networkpolicy.yaml`](manifests/smoke-rate-store-networkpolicy.yaml),
   which `make up` applies, gives the pods with smoke's label an egress rule to
   the store's pods on that port and nothing else, so the only rule between
   the pod and the store is the store's ingress, which admits the Model
   Gateway's pods alone. (The first version connected from the Claims API's
   pod, which has no egress rule to the store: the packets were dropped at the
   sender, and the line passed with the store's policy deleted.) The control,
   as the database's line does it: the same pod, given the name label
   `app.kubernetes.io/name=model-gateway`, which the gateway's egress rule and
   the store's ingress rule admit, must reach the port (a connection that is
   then reset or answered is "reached": the pod holds no certificate and no
   password, so it can do nothing there), in up to four tries; when it does
   not, the control did not reach and the line says it proves nothing. While
   it carries that name the pod, which has no readiness probe, is an endpoint
   of the model-gateway Service, so it is labelled back to the sweep's name
   straight after the control and deleted at the end. A missing kind policy is
   a FAIL before any pod starts (without it a timeout would be the sender's and
   the line would pass with the store open), and so is one that exists with the
   wrong shape: the line reads the policy's JSON and fails with a sentence of
   its own unless the policy selects exactly the pods with smoke's label, lists
   `Egress`, and has a rule whose peers include the store's pods of this
   namespace (a pod selector of `app.kubernetes.io/name=rate-store` and neither
   a namespace selector nor an address block) on TCP 6379 (no ports at all is
   every port). With another port or selector the first attempt would time out
   at the sender, the control would reach through the gateway's own egress
   rule, and the line would pass with the store's ingress unproven; the count
   of lines does not change (implemented and tested with a stub `kubectl`, one
   test for each wrong shape; the main session runs it on kind after this
   lands). It fails like the others on an
   answer, a refusal and a name that does not resolve (a store that is not
   deployed), and prints one line when both parts hold. It does not prove that
   the store is up: `make deploy` waits for its Deployment and its Certificate,
   and the cost-series line of check 5 now also means the store answered,
   because the gateway refuses every call it cannot count. No line reads the
   store itself: that would need its credential. The pods also carry
   `meridian-smoke=network-probe`, which no policy of the chart, Service or
   Deployment selects (kind's policy for the rate store line does). A run that
   is killed hard (SIGKILL, a power cut) leaves the pod as
   a Failed object with the labels the policies select on, so the check starts
   by listing the pods with that label and deletes by name those older than 300
   seconds (a younger one is another run's; a list that cannot be read is not
   an error). SIGHUP, SIGINT and SIGTERM each run the exit trap, which deletes
   the pod (tested by starting the real script against stub commands; an
   interrupted run was not tried on the cluster). The allowed paths are also
   the tool check's proof (line 3). It fails when the NetworkPolicy
   `default-deny` is missing. Before
   `make deploy` one line prints SKIP in place of the six. It adds about 45
   seconds (the rate store's line is a second probe pod's start and one more
   timeout of 4 s; an estimate, the pod's start was not timed). On 2026-10-06
   the first four lines passed on the cluster (the
   control, the two denied paths out of the Claims API, and the database
   refusing a pod without the label and taking one with it), and no probe pod
   was left in `meridian` afterwards. The fifth line passed on the cluster on
   2026-10-06 (a probe in `default` cannot push to the collector on 4318); its
   dependence on check 4's push is tested without a cluster until the next
   `make smoke`. The sixth line passed on the cluster on 2026-10-06 (third
   run, among its 45 PASS lines): the pod without the gateway's label could not
   reach the store and, given the label, reached it. Its FAIL branches (the kind
   policy missing, a pod that reached the store, a control that did not reach)
   are tested against stub commands and were not seen on a cluster. What it
   does not prove, and stays by hand (S019): that a pod of another namespace
   cannot reach the database, and that an address outside the machine is
   unreachable (smoke sends nothing there); and it does not read what the
   policies say, which the chart's tests render and compare. It does read
   whether each one is there (S073, K4): the check lists the Meridian
   Deployments as checks 3, 5 and 7 do and, from one listing of the
   NetworkPolicies of `meridian`, prints one FAIL line that names each service
   whose policy of the same name is missing (the chart makes one per
   Deployment, the rate store's included, and prints no new line when all are
   there). The probes are as before, from the Claims API's pod and one probe
   pod: what the policies do is proved for the Claims API's egress and for the
   database's, the collector's and the rate store's ingress only. When
   Deployments exist and the Claims API's is not among them, the check fails,
   because the probes run in its pod. Tested with stand-ins. Seen on kind on
   2026-10-07 (runs R4, R4c and R4d): the green smoke runs, 45 PASS and then
   46, with all seven policy objects there. Not seen: the FAIL that names a
   service whose policy is missing.
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
   uvicorn ends an unknown CA's connection without delivering the alert (a
   reset under TLS 1.3 and an EOF under 1.2, measured on 2026-10-06 against a
   test server on a plain `uvicorn.Config` with the services' flags). The five
   services that serve TLS have since started through `python -m
   meridian.platform.common.tlsstart`, which hands uvicorn its own context
   (a test holds the two equal); the ending was not measured against the
   module, and smoke's 46 lines passed after the deploy that brought it, with
   the ending that line read not in that run's record (S069, run K2). So
   `reset` is the ending expected of the
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
10. **Certificate policy.** Five lines, never SKIP, the first three read-only:
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
    issuer `meridian-services`, with a URI that policy lists (the Claims API's;
    since S072 the policy lists each URI, so an unlisted one would be refused
    in `meridian` too) and a duration and usages it allows, so that only its
    namespace refuses it: the namespace selector of `meridian-services` does
    not list `default`, and `meridian-deny-unlisted`, which selects the issuer
    from every namespace, permits nothing. It passes when the request is
    Denied and the approver's whole message, judged before it is cut, names
    `meridian-deny-unlisted` as a policy that evaluated the request and does
    not name `meridian-services` as
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
    there. The fifth line (S073) is read-only and for kind only: on Azure the
    database and its certificates are the provider's. CloudNativePG signs the
    database's server and replication client certificates with an authority of
    its own (cert-manager does not issue them, and Prometheus holds no series
    for them), and writes their three expirations into the status of the
    Cluster `platform-db`, as text in Go's default time format
    (`2027-01-04 18:05:31 +0000 UTC`, not RFC 3339). The line reads that
    status once and judges the earliest: it passes, naming the certificate
    and the days left, while more than 84 hours remain (half of the operator's
    renewal threshold, `EXPIRING_CHECK_THRESHOLD`, 7 days by default, in whole
    days, as the pinned operator's documentation says), and fails when less
    remains, when a certificate has ended, when the status holds no
    expiration at all, and when a date is not in exactly that form with a
    `+0000 UTC` zone or has it and is no date, as month 13 ("cannot tell",
    and the line names which certificate without repeating the text). The
    lifetime, `CERTIFICATE_DURATION`, is in whole days too (default 90), so
    the shortest is one day and a renewal cannot be seen inside one cluster
    run; smoke does not shorten it. Tested
    with a stand-in and the real jq. Seen on kind on 2026-10-07: a pass on
    the real Cluster, naming `platform-db-ca` with 89 days left. Not seen: the
    line failing (no certificate on kind is near its end, and the shortest
    lifetime is one day), and a renewal by the operator. It tells
    nothing between two runs of smoke: no alert rule watches these dates (see
    [the certificate expiry runbook](../../docs/operations/runbooks/certificate-expiry.md)).
11. **Alert rules and health dashboard.** Four lines, read-only, run after the
    first ten (check 12 follows it).
    The first three read Prometheus' `/api/v1/rules` through Grafana's
    datasource proxy, for the `PrometheusRule` `meridian` that `make up`
    applies. The five groups of `alerts/meridian.yaml` are loaded and every
    rule in them has health `ok`: a FAIL names each rule that has not, with
    its health and Prometheus' last error, cut to 120 printable characters
    (a rule that has not been evaluated yet is `unknown`, which is not `ok`;
    the line waits up to 120 seconds for the first evaluation). The loaded
    group and rule names are the file's, in both directions, so a cluster
    that runs an older rule file says which groups and rules differ (the
    file's names are read with `awk` by their indentation, and a test keeps
    that equal to a YAML parser's reading), and so are each rule's expression
    and an alert's `for` (S073, K4: the second line again; the FAIL names the
    rule and which of the two differs, never an expression's text, and says
    to run `make deploy` or `make up`). Prometheus returns the parsed
    expression, not the file's text, with the matchers of a selector sorted by
    name and a duration in its largest units (`[24h]` comes back as `[1d]`:
    seen on 2026-10-07 with the pinned Prometheus image, the cluster's own,
    running the file's rules in a container on the development machine, not on
    the cluster), so both sides go through one
    `jq` filter that collapses whitespace, writes every duration in
    milliseconds, removes all whitespace and sorts the matchers between a pair
    of braces; it is not a PromQL parser. It does not see a change that only
    moves whitespace (also inside a string literal), a label value or regular
    expression that holds a comma, a brace or a word like `1h`, an
    expression written another way that is the same one (a quote that is not
    a double quote; expected, not tried), the rule's labels and annotations, or
    a `keep_firing_for`. Tested with stand-ins. Seen on kind on 2026-10-07: the
    expressions and `for` of the cluster's rules compared with the file's in
    run R4 for the first time, all equal after the filter, and in every green
    run after it; in run R4b a rule's `for` changed on the object (2m to 59m)
    was named, with `make deploy` or `make up` as the remedy, and `make deploy`
    put it back; and the `PrometheusRule` taken away was named as missing and
    `make deploy` made it again with its five groups. Not seen: a changed
    expression (only a `for` was changed on the cluster) and the other
    differences the filter cannot see.
    And no alert of the Meridian
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
    cluster on every smoke run of 2026-10-06 (in the third of S064's runs with
    the five groups and all 19 rules, none firing or pending). Smoke was not
    run while the
    renewal watch's short certificates were in place and the alert fired; a
    firing alert is a FAIL of the third line, so it would have failed (see
    "How long a certificate lasts" below).
12. **Telemetry stores.** Ten lines (S072, contracts M3b, M4 and M4b), run after
    the other eleven, from one probe Pod in `observability` (the Claims API's
    image, the collector's name label and smoke's own; a label impersonation,
    said, not solved: T-84): a push to Loki's gateway with no client certificate
    is 403 and a read is 200; Tempo's receiver ends a connection with none in
    the TLS alert "certificate required"; Loki's own port times out for the Pod
    (kind's policy `smoke-telemetry-probe` lets it send there, so the timeout is
    Loki's ingress rule) and the same Pod with the gateway's labels reaches it;
    and the gateway and Tempo's receiver each serve the certificate that is in
    their Secret, which finds a pod that was not rolled after a renewal. The
    three of contract M4: Prometheus's gateway answers 403 to the OTLP
    receiver's path, remote write and `/-/reload` with no client certificate and
    200 to a query; Prometheus's own port times out for the Pod (the policy
    `smoke-telemetry-probe-prometheus` is the same arrangement) and is reached
    by the Pod with the gateway's labels; and the gateway serves the certificate
    that is in its Secret. The two of contract M4b: the same read written with a
    doubled slash, a per-cent-encoded letter and a dot segment is 403 each beside
    the plain read's 200 (the one live check of the gateway's comparison of the
    raw and the normalised path), and a POST of a form body `query=1` is answered
    `200 success` (the gateway forwards the body).
    Skipped, one line, while no Meridian Deployment exists. Tested with
    stand-ins, and the probes' Python run against nginx of the pinned image in
    a container; run on the cluster on 2026-10-07: the first five lines in run
    R16b (51 PASS) and all ten in run R17 (56 PASS). The paragraph of the script
    (`smoke.d/12-telemetry-stores.sh`) says what the lines do not prove.

`make smoke` creates three Jobs in `observability`. Kubernetes removes each one
15 minutes after it finishes. It creates one Pod in `meridian` for the network
check (line 8), one Pod in `observability` for the telemetry stores (line 12)
and one `CertificateRequest` in `default` for the certificate
policy check (line 10), and deletes each as soon as its check is done and again
when the script ends. The tool check leaves at most one refused `tool.call` row
per server in the audit log per throttle window, and the identity check one
refusal row per reason and minute.

Where a check lives, and how to add one (S074; the split is tested and was
seen on kind once, 2026-10-07: `make smoke` printed the same 46 PASS lines as
the unsplit script). `smoke.sh` is the entry: the preconditions, the traps, the
eleven calls and the summary, and a pair of lines for each part it sources. A
check is a file in `smoke.d/`, `NN-name.sh` (`shared.sh` holds what several
checks and the trap use), and it holds definitions only: its paragraph from the
header, its constants and its functions, so that sourcing it runs nothing.
All eleven checks are parts now (twelve files with `shared.sh`, each under 800
lines), and the entry is 88 lines. To add a check:

- write `smoke.d/NN-name.sh` with no execute bit: the first line `# shellcheck
  shell=bash`, the paragraph (its first line `#   N. name:`, as the others),
  a blank line, then the constants and the functions;
- add the pair `# shellcheck source=smoke.d/NN-name.sh` and
  `. "${KIND_DIR}/smoke.d/NN-name.sh"` to the entry after the last part's pair,
  and call the check's function in the sequence of calls near the end of the
  entry (no source line goes among the calls).

`test_smoke_parts.py` fails on a part that is executable, is not sourced once in
that form, holds a statement or a name another part defines, or holds the text
`need_tools `; `test_smoke_line_count.py` fails until the check is counted in
the number of lines a healthy run prints.

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
   applied, a role is not reconciled, a role Secret is missing, the
   database's NetworkPolicy `platform-db` is absent (the chart's
   `default-deny` would otherwise cut the database off from its operator) or
   the operator's NetworkPolicy `cnpg-operator` is absent (the same
   `default-deny` selects the operator's pod, in `meridian` since S072, and
   would cut it off from the API server and the database's pods; a cluster
   made before that change lacks it). The second guard is tested with a stub
   `kubectl`; it is not seen on kind.
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
   not yet seen on a cluster.) After those, and again before it builds
   anything, it refuses when the Secret `rate-store-credentials` is missing or
   lacks a key, or holds one empty (S066): the gateway's pod reads `uri` and
   the store's pod mounts `users.acl`, and a pod that cannot read its Secret
   would not start after the Jobs had run. Only the names of the keys are read,
   never a value. It also refuses a Secret whose annotation
   `meridian.kind/rate-store-acl-rules` is missing (a Secret from before the
   store's probes had a user of their own) or is not the hash of the ACL that
   `make up` would write now: the store's pod would never be Ready, or would
   refuse the gateway, and nothing but its restarts would say why. The
   annotation is only a note `make up` left, so it also decodes the Secret's
   own `users.acl`, hashes it the same way (the password's hash masked, in one
   pipe from `jq` to the hash: the text is never printed, traced or kept, and
   tracing is off inside the function) and refuses a file whose hash is not the
   one `make up` would write now, with a sentence of its own: an ACL file edited
   or replaced after `make up` under an annotation that still matches is no
   longer let through (implemented and tested with stub commands; not yet run
   on a cluster). Such a Secret
   (or one `make up` made with an ACL that has since changed) is deleted first,
   because `make up` keeps a Secret that exists; the message gives the order:
   delete it, `make up`, restart the store, then the gateway (above, and the
   [rate store runbook](../../docs/operations/runbooks/rate-store.md)).
   (Tested against stub commands. Seen on kind on 2026-10-06: the third run's
   `make deploy` passed these checks on the Secret `make up` had just made. The
   refusals were not seen on a cluster: that run made the cluster again instead
   of meeting an old Secret.)
2. Builds and loads the image, tagged `meridian:<first 12 hex of its ID>`. A
   deploy of a changed tree leaves the previous image in the Docker engine
   and in the node, and images stay there until a person removes them.
   `make images` lists them, each marked in use or unused by a workload, and
   prints the commands that would remove the unused ones; it removes nothing.
   "In use" means a pod template names the image now, or a Pod that exists
   does. The chart keeps two old ReplicaSets of each Deployment
   (`revisionHistoryLimit: 2`; a ReplicaSet beyond the limit goes at the next
   rollout and does not come back), so after a deploy that changed the image
   the previous tags are what an old ReplicaSet would start again on a
   rollback, and with `pullPolicy: Never` a removed image cannot be pulled
   again: an image only an old ReplicaSet names is marked `rollback` (a
   rollback's target), listed apart, and gets no removal command; what nothing
   names, not a pod and not a ReplicaSet the limit keeps, is `unused` and gets
   the command. It reads a reference as `repository:tag`, with or without a
   registry (a port in its host included) or a digest after the tag; another
   registry's image of the same name is another image, and an image named by a
   digest and no tag stops the listing, because it cannot say which tag runs.
   (The reading of references is tested against stub commands and not seen on
   a cluster; the limit is tested with the chart rendered, and seen on kind on
   2026-10-07, run R2: `revisionHistoryLimit: 2` on all seven Deployments and
   20 ReplicaSets after the deploy, 13 before it. Not seen: the limit removing
   an old ReplicaSet over three deploys, and `make images` with the new
   reading.) In a
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
   BackendTrafficPolicy and the sweep's CronJob, and, since kind's values turn
   it on (S066), the rate store: its own ServiceAccount, ConfigMap, Deployment,
   Service, Certificate and NetworkPolicy. The image's repository and
   tag are the values the script passes, and the store's image
   (`RATE_STORE_IMAGE` of [`pins.env`](pins.env), as `--set-string
   rateStore.image=`, so it has one place and one Renovate reader); the rest is
   the chart's `values.yaml` and [`values/meridian.yaml`](values/meridian.yaml).
   Helm does not wait for the rollouts; the next steps do.
5. Waits for the rate store, then for the Model Gateway (every call it makes
   needs the store), then runs a Job `meridian-ingest-<tag>` with
   `meridian knowledge ingest` as `knowledge_ingest` (its audit row names that
   role), which embeds the 85 clauses of the four
   wordings through the gateway and replaces the knowledge store in one
   transaction. The same Job then runs `meridian knowledge verify` (S067),
   which compares each stored clause with the manifest-verified wordings and
   fails the Job on a difference
   ([the runbook](../../docs/operations/runbooks/knowledge-store.md); built
   and tested without a cluster, not yet run on one). Once per image: the
   finished Job has no expiry and is the
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
app's own limit, before the app sees it, except on the two upload paths when
the uploads switch is on (next). The edge speaks plain HTTP, on loopback only;
TLS there has no step yet.

### Files for a claim: two switches, both off

S080 (the commits and code comments call it "S070 uploads") built, behind two
switches that are **off in kind's values and off in the chart's**, a
claimant's upload of a file to a claim and the adjuster's download of it.
Status: implemented and tested, and **run once on kind** (RU1, 2026-10-07,
with both switches added to kind's values for that run and turned off again by
a second deploy; smoke passed 46 lines afterwards). That run saw the uploads
route win over the first route, the three policies `Accepted` (the downloads
policy on the route's named rule, replacing the route's own policy for it), the
edge's 413 over the buffer and its 429 at the seventh upload and at the 31st
download in a minute, the two buckets separate, a download's headers through
the edge, the cross-site refusals and one audit row for each download served.
**Not seen:** a browser, slow bodies and readers, the app's own 429 and 503
(the edge answered first), the byte budget, memory under four uploads, a retry
at the edge, and the database's size and log at the ceiling.

- **Uploads** (`route.uploads.enabled`). A second HTTPRoute for exactly
  `POST /claims/CLM-nnnn/files` and the claimant form's post to
  `/claimant/claims/CLM-nnnn/files`, with a request buffer of 1,126,400 bytes
  (1 MiB of file and 76 KiB of envelope; the other routes keep 64 KiB) and a
  local rate limit of six requests a minute for the whole route, in a
  BackendTrafficPolicy of its own, and `MERIDIAN_CLAIMS_UPLOADS=on` on the
  Claims API. The chart refuses the switch while `route.enabled` is off. Envoy
  counts the limit for each of its proxy pods, and on kind there is one.
- **Downloads** (`route.downloads.enabled`), its own switch, which needs the
  uploads switch (the chart and the app each refuse downloads without uploads):
  the adjuster's `GET` and `HEAD` of
  `/adjuster/claims/CLM-nnnn/files/<file id>`, a second named rule of the
  uploads route with a limit of 30 requests a minute of its own and no larger
  buffer, and `MERIDIAN_CLAIMS_DOWNLOADS=on`. A file is sent only as an
  attachment, under a sandbox policy. The app has brakes of its own for it (a
  cross-site check, 30 a minute and four at once, a HEAD that reads no
  bytes), and the adjuster's claim page shows the newest 200 events and counts
  the downloads on one line.
- **The guard.** Until sign-in exists (S021) the pages have no identity, so
  whoever reaches the route stores files on any claim and, with downloads on,
  reads any claim's files by walking the claim IDs. The chart therefore
  **refuses either switch unless the route's host name as a whole is a
  lower-case name that ends in `.localhost`** (the name
  `claims.meridian.localhost` of kind's values is), and unless the switches are
  booleans, with a sentence that names S021. **The guard checks a string.**
  Envoy matches the Host header, which any client can set, so the same release
  answers anyone who can open a connection to the Gateway; on kind the
  boundary is the cluster config that binds the published port to 127.0.0.1
  (`cluster.yaml`), not the chart. The guard stops a release that turns the
  switches on beside a public name in one values file. It does not stop a
  Gateway that is reachable from a network (a LoadBalancer, a changed
  `listenAddress`), a port-forward, other local users or containers that reach
  the loopback port or the node's address, a `parentRef` to another Gateway, or
  a variable set by hand outside the chart. So do not turn either switch on for
  a cluster that other people or machines reach, and S021 removes the guard
  when sign-in exists.
- **Turning them on for a local run.** Neither switch has a `make` target or
  a flag: edit [`values/meridian.yaml`](values/meridian.yaml) under `route:`,
  uncomment the commented `uploads:` lines (and, to try the download too,
  add `downloads:` with `enabled: true` beside them), and run `make deploy`.
  Put the file back afterwards, so the change is not committed. Nothing the
  services read can be set through an `env` item: the chart refuses one that
  names either variable.
- **What the app does as well.** The edge's buffer and rate limit are not the
  app's only brakes, and the app does not depend on which edge route served a
  path: it takes at most four uploads at once (503), 30 files and 8 MiB a
  minute for the whole store (429), a body within 20 seconds (408), a ceiling of
  128 MiB and 2,000 stored files (507), and five files and 3 MiB to a claim. It
  also refuses a raw path that holds a percent sign, but RU1 showed that Envoy
  normalises the path first, so behind this edge that refusal does not fire;
  RU2 showed that the uploads route serves the normalised request and that
  its limit counts it. Files go to the one database (2 Gi on kind, shared with
  the audit trail); `claims_api` has a connection limit of 50 there. Nothing
  scans a file: malware scanning is designed, not built.

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

Every pod of the chart, the Jobs' and the sweep's included, but the rate
store's (S066; the next paragraphs, and the list below say where it differs):

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
The chart refuses a second replica of the Model Gateway unless the rate store
is on (threat model T-45): without it the rate windows live in one process, and
each replica would allow the full limits. Kind's values turn the store on and
leave the gateway at one replica, the laptop's memory being the reason; the
chart allows a second one now, and no load test has run two.

**The rate store (S066)** is a seventh workload of the release, with a
Deployment, Service, ServiceAccount, ConfigMap, Certificate and NetworkPolicy
named `rate-store`, and it is not like the six services. It runs the official
Redis image (`RATE_STORE_IMAGE`, by digest, pulled: `IfNotPresent`, not kind's
`Never`, which is for the loaded Meridian image), without the image's
entrypoint, as the image's own user, 999 and group 1000, which the chart
writes down; the rest of the hardening is the services': no capability, no
privilege escalation, the default seccomp profile, a read-only root and no
service-account token. It has no `/tmp` and nothing writable at all (Redis
writes nothing: no snapshot, no append-only file), its volumes are its
certificate, the access-control file (the one key `users.acl` of the Secret)
and its configuration, one replica that is replaced rather than rolled, no
PodDisruptionBudget and a memory limit of 64 MiB, which is the real bound on
what it can hold (Redis's own `maxmemory` does not stop the gateway's script
writing). It serves TLS 1.3 alone, asks every client for a certificate of the
services' CA, and its `default` user is off; a bulk and a client's query buffer
are bounded at 1 MB (the gateway's script is a little over 1 KB) and every
client's buffers together at 8 MB (`maxmemory-clients`: past it Redis
disconnects the largest clients; without it 240 authenticated connections each
holding the head of a 1 MB request killed the store under its 64 MiB limit, with
it the same flood held it between 13 and 27 MiB, the probe and the gateway's
calls still answered; measured on the pinned image outside a cluster), and no
directive bounds a script that writes without end, which the pod's memory limit
ends by restarting the store, so every tenant has its windows again. Both
probes ping as the ACL user `probe` and pass only on `PONG`, so a store frozen
by a looping script is restarted within about a minute (every window starts
again); their `redis-cli` runs beside a `sleep 2` that the script waits for
with `wait -n`, inside the kubelet's 5 seconds, so a store frozen below the
protocol fails the probe after 2 seconds and leaves no process behind (S073,
K7: the image's `timeout` left one defunct process per probe, 2,024 of them
under the Redis server after about two hours on kind on 2026-10-06, then "can't
fork" in every probe and the gateway's 503 to every call; measured on the
pinned image with the server stopped by `SIGSTOP`, not `docker pause`, which
stops `docker exec` too). Seen on kind on 2026-10-07 (run R2): the store's
new pod held no defunct process at five readings a minute apart and after
smoke, six minutes in (the old probes had left about 90 by then), Ready with
no restart. Seen on kind on 2026-10-07 (run R4e): the store's new pod with
both probes at `timeoutSeconds: 5`, Ready, 0 restarts, and 0 defunct processes
on the node. Not seen: the store over the two hours the fault took, and a store
frozen below the protocol on kind. Its
NetworkPolicy admits the Model Gateway's pods on 6379 and nobody
else, and gives it no egress; it is the store's only control before
authentication, so the chart refuses the store with `networkPolicy.enabled`
false. The gateway's policy has the matching rule to it, and no other
workload's has. The gateway reads its address from the key `uri` of the Secret
through a required reference, and the chart refuses a values `env` item of any
service named `MERIDIAN_GATEWAY_RATE_STORE_URL`, so the address comes from the
Secret and from nowhere else. A test of each
(`tests/meridian/test_kind_rate_store.py`, `test_helm_rate_store_hardening.py`)
renders kind's values; the checks of the six services leave the store out by
name, so no check on them was loosened. Implemented and tested, and seen on
kind on 2026-10-06 (third run): the store running under this configuration
with the gateway's calls counted by it, its probes passing, its certificate's
renewal followed by one restart, and the policy's ingress rule enforced on a
pod without the gateway's label. Not seen on a cluster: a store frozen by a
script and restarted by its probe, and a TLS 1.2 client or an oversized bulk
refused. Seen on kind on 2026-10-07 (run R11): the 503 of a store that is down
(scaled to 0 for 10 seconds): an ingest Job ended on it with the word
`rate-store-unavailable`, the gateway stayed Ready with no restart, and it
admitted calls again with the same pod once the store answered.

The namespace denies all traffic by default: the NetworkPolicy
`default-deny` selects every pod in `meridian`, whatever its labels, and
allows nothing. One policy per workload then allows what its configuration
names:

| Workload | May be called by | May call |
|---|---|---|
| Claims API | the edge (Envoy's proxy pods) | Agent Runtime |
| Agent Runtime | Claims API | Model Gateway, the three tool servers |
| Model Gateway | Agent Runtime, Knowledge tool server, the ingest Job | the rate store (replay mode: no provider) |
| Rate store (S066) | Model Gateway | nothing, not even DNS |
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
`Instance Status Extraction Error` within 40 seconds, measured in S019); the
operator's pod is in this namespace since S072 and is admitted by its two
labels, with no namespace named. The operator has a policy of its own, below.
The
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
  a workload, never a controller's pod. No namespace of the add-ons is left
  without a NetworkPolicy since S072 (contract N): `envoy-gateway-system` has
  its own (below).
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
before the releases they guard, beside the database's (a fourth, for the log
agent's namespace, came with S064, below, and was applied on kind on
2026-10-06, where the agent sent through it; a fifth, in `meridian`, for the
probe pod of smoke's rate store line, came with S066 and was applied on kind
on 2026-10-06, in the third run, where that line passed); all of it is **tested
without a cluster** (the manifests and the script's lines are checked against
stub commands, and the pods' specs were read with `helm template`), until the
first `make up` and `make smoke` after it have run on one.

| File | Namespace | What it says |
|---|---|---|
| [`manifests/cert-manager-networkpolicy.yaml`](manifests/cert-manager-networkpolicy.yaml) | `cert-manager` | Ingress denied, except 9402 to the controller's metrics from Prometheus; the two webhooks' port (10250, `failurePolicy: Fail`) is admitted from no pod, and the API server, which calls from the node, needs no rule (see below). Egress: DNS and TCP 6443 to the API server's address alone (`make up` reads it from the `kubernetes` EndpointSlice and fills it in, as it does the database's; `make deploy` and `make smoke` do not compare this policy with the endpoint) |
| [`manifests/observability-networkpolicy.yaml`](manifests/observability-networkpolicy.yaml) | `observability` | Ingress denied, except one rule per peer: the collector from the namespace `meridian` and from the log agent's pods in `logging` (namespace and pod label) on 4318; Tempo from the collector (4317) and Grafana (3200); Loki's gateway and Prometheus's gateway from the collector and Grafana (8443, the one TLS port of each); Grafana from Prometheus (3000); kube-state-metrics from Prometheus (8080); and 10250 to the Prometheus operator from Prometheus alone (its webhook and its metrics share the port). Egress denied for every pod and admitted by one policy per pod: DNS for all; the node (the API server and the kubelet: one address on kind, 6443 and 10250, filled in by `make up` as in cert-manager's) for Prometheus, the operator and its hook Jobs, kube-state-metrics and Grafana; Prometheus to its targets (Grafana, kube-state-metrics, the operator, cert-manager's controller 9402, the DNS pods 9153); Grafana to Tempo (3200) and the two gateways (8443); the collector to Tempo (4317) and the two gateways (8443); Loki's own pods (7946, its memberlist); Tempo to nothing. Two more files hold the rest, each applied by `make up` just before what it is about: [`manifests/observability-loki-networkpolicy.yaml`](manifests/observability-loki-networkpolicy.yaml) (Loki's port 3100 from its gateway's pods ALONE, not the collector and not Grafana; the gateway's egress to Loki; the probe pod's of `make smoke`) and [`manifests/observability-prometheus-networkpolicy.yaml`](manifests/observability-prometheus-networkpolicy.yaml) (Prometheus's port 9090 from its gateway's pods ALONE, not Grafana and not the collector; the gateway's egress to Prometheus; the probe pod's). These two ports have no TLS and no certificate check: network policy alone closes them, so a pod that can reach one of them, by a label it should not carry (T-84) or by a gap in the plugin, writes without a certificate |
| [`manifests/cnpg-operator-networkpolicy.yaml`](manifests/cnpg-operator-networkpolicy.yaml) | `meridian` | The CloudNativePG operator's pod (S072, contract C): ingress denied, so no pod reaches its webhook port (9443) or its metrics port (8080), and the API server, which calls from the node, needs no rule (as for the other webhooks, below). Egress: DNS, TCP 6443 to the API server's address alone (filled in by `make up` as in the others), and TCP 8000 to the pods of the Cluster `platform-db`, the one port the database's policy admits the operator on; not 5432, and nothing towards the collector. Implemented in files and tested without a cluster; not seen on kind |
| [`manifests/envoy-gateway-networkpolicy.yaml`](manifests/envoy-gateway-networkpolicy.yaml) | `envoy-gateway-system` | Ingress and egress denied for every pod; five policies, applied together before the release (S072, contract N). The controller receives xDS (18000) from the proxy pods alone; the proxy pods receive the listener's port 10080 from any address, which is the public entry and the one rule of the file without a peer, and may send to the controller (18000) and to the Claims API's pods (8000); every pod may reach DNS; the controller and its hook Job reach TCP 6443 at the node's address (filled in by `make up`, as in the others). The webhook's port 9443, the metrics and the probes have no rule: the API server and the kubelet call from the node. The proxy Service sets no `externalTrafficPolicy`, so the controller's default, `Local`, keeps the request's source; which address a request from the laptop has when the proxy sees it only a run shows, and the rule is right whichever it is. Implemented in files and tested without a cluster; not seen on kind |
| [`manifests/smoke-networkpolicy.yaml`](manifests/smoke-networkpolicy.yaml) | `meridian` | The pods of smoke's telemetrygen Jobs may reach DNS and the collector's 4318, and nothing reaches them |
| [`manifests/smoke-rate-store-networkpolicy.yaml`](manifests/smoke-rate-store-networkpolicy.yaml) | `meridian` | The probe pod of smoke's rate store line (the pods with the label `meridian-smoke=network-probe`) may send to the rate store on 6379, so that only the store's ingress rule can stop it |
| [`manifests/logging-networkpolicy.yaml`](manifests/logging-networkpolicy.yaml) | `logging` | Ingress and egress denied for every pod; the log agent may reach DNS and the collector's 4318 and nothing else (S064) |

Only Meridian's pods push to the collector, on 4318, and the log agent, which
sends only what `meridian`'s pods wrote (S064); 4317 is admitted from
no namespace: the collector's rule selects the namespace `meridian` and no pod
label, and the chart's own policies are what narrow that to the six services
(its `default-deny` leaves every pod of `meridian` without egress, and only a
pod given the collector's address has a rule for it; a test reads both halves
and fails when one drifts from the other). The CloudNativePG operator's pod is
in `meridian` too since S072, and the operator's own policy, which has no rule
towards the collector, is what keeps it out. Smoke's Jobs moved into
`meridian` for that reason, so the sentence has no footnote. The gRPC receiver on 4317 is
closed in the collector's values (read from them; no probe of 4317 has run on
a cluster), and the push moved to TLS (below); the policy's refusal of 4317
stays as a second wall.

What stays open, in one list:

- The edge's listener is open to any address, by design: port 10080 of the
  proxy pods (the Gateway's port 80 plus 10000) has no peer, because it is
  the public entry and a request from outside keeps its source address
  (`Local`, above). The rule admits every source, the pods of the cluster
  included; a narrower one would need an address range, which the file's own
  test forbids. The rest of
  `envoy-gateway-system` is closed (S072, contract N). Not seen on kind: a cold
  `make up` must show the certgen hook Job completed (it runs under the default
  deny before the controller exists), the proxy configured, and `make smoke`'s
  first line and its lines through the edge passing. If the edge stops
  answering, the way back is in the file's header. The topology-injector
  webhook (9443) is `failurePolicy: Ignore`, so a call that does not get
  through raises no error. `make deploy` refuses, with "run 'make up' first",
  when the operator's policy `cnpg-operator` is missing, as it does for the
  database's, because the chart's `default-deny` would cut the operator off.
- Egress from `observability` is denied by default and admitted pod by pod
  (S072), from a list of what each pod calls that the manifest's header holds,
  read from the render of the pinned charts. Implemented in files and tested
  without a cluster; not seen on kind. A cold `make up` must show every pod
  Ready, every target up on Prometheus's Targets page and smoke's lines
  passing. What stays: the one rule for the node gives the kubelet's port
  10250 to every pod it selects, though only Prometheus scrapes it (the file
  holds the placeholder once, as the function that fills it requires); a
  cluster with more nodes has a kubelet per node, which that rule does not
  name; and the pods may still reach the DNS pods, which answer any name. A
  target found down is added once; a second round of surprises takes the
  egress policy out again and states the gap.
- The three webhooks (cert-manager's, approver-policy's, the Prometheus
  operator's) listen on 10250, and no pod may reach that port except
  Prometheus on the operator's, which serves its metrics there. No rule
  admits the API server, the one caller: it calls from the node, and on
  kind a call that starts on the node passes every NetworkPolicy. Seen on
  kind on 2026-10-07, for the API server's call to a pod by way of the pod
  proxy: it arrived from the node's own address on the pod network
  (10.244.0.1) and reached a pod of `meridian`, whose ingress is denied by
  default; not seen separately, for a webhook call through the Service's
  address. A cluster with more than one node, or a plugin that polices the
  node, would need one `ipBlock` peer on each, filled in the way the
  egress address is; if a cold `make up` shows a webhook unreachable (no
  certificate issued, smoke fails), each rule comes back with the node's
  pod-network address as its one peer, and with the node's published address
  too when that is not the source (after the Service's translation a call
  through a Service could carry either; not seen). A cold run shows a wrong
  premise in this order: cert-manager's release, approver-policy's apply, the
  database's install and, silently, the Prometheus operator's target. The
  change is in the files and
  tested without a cluster; a cold `make up` has not run it. The two that
  fail closed (cert-manager's and approver-policy's) are the ones a flood
  could have stalled; the operator's is `failurePolicy: Ignore`.
- The collector's rule admits every pod of `meridian` that the chart's policies
  let out, and on a cluster where the chart is not installed (`make up` alone)
  every pod of `meridian` can push to it: `default-deny` is the chart's. The
  operator's pod is the exception, as above.
- Grafana's port-forward needs no ingress rule if the container runtime opens
  it inside the pod's own network namespace, which was assumed and is the
  first thing to look at; Loki's memberlist rule assumes the plugin sees the
  pod's join through its own Service as coming from the pod itself.
- node-exporter is off (above), so no node's CPU, memory or disk is observed.

Pod Security labels (`warn` and `audit`, never `enforce`, as on `meridian`):

| Namespace | Level | What stops the next one |
|---|---|---|
| `meridian` | `restricted` | nothing; the operator's pod, since S072, is read in the render of 2026-10-07 and meets it, not confirmed by the API server |
| `cert-manager` | `restricted` | nothing: its five pods meet it as rendered |
| `observability` | `restricted` | nothing as rendered: `tempo` and `otel-collector` set no `allowPrivilegeEscalation: false`, no `capabilities.drop: [ALL]` and no `seccompProfile`, the collector no `runAsNonRoot` either, until their values files set them (S063, tested without a cluster; the server-side dry run is repeated after `make up`); node-exporter would have stopped `restricted` too, and is off |
| `envoy-gateway-system` | `restricted` | nothing as rendered for the controller and its Job; the proxy pods are made at run time and were read in the source, not seen |
| `logging` | `privileged` | `baseline` is stopped by the hostPath volume (`/var/log/pods`); `restricted` by that volume alone (it allows no hostPath): the pod runs as user 10001 with `runAsNonRoot` (S064, read as `helm template` renders it, 2026-10-06, and seen on kind the same day: a server-side dry run of `enforce=restricted` warned of "restricted volume types" alone; the label warns of nothing) |

The Prometheus pods are the operator's, not rendered by Helm, and were not read:
a server-side dry run on the cluster (`kubectl label --dry-run=server`) is the
check that reads them.

### The log agent and the namespace `logging` (S064)

Status: **implemented; it ran on the kind cluster three times on 2026-10-06
(local only), the third time in the form below.** The first form (as root, every
pod of `meridian` that a pattern matched) ran at 11:10 UTC and, with the
reviews' fixes in the services, at 11:34 UTC: the pod was Running with no
permission or TLS error in its output, smoke's line found the Claims API's
access line in Loki (41 PASS, 0 FAIL, 0 SKIP) and the canaries (a query string,
an address in a path) were in no line there. The third run, at the final tip
(12:56 to 13:06 UTC), ran the form below: the pod 1/1 Running with no restart,
as uid and gid 10001 with `runAsNonRoot` and the supplementary group 0; 14
"Started watching file" lines, all for pods of the six services and the sweep,
and no line with "permission denied", "x509" or "error"; Loki held the six
services and `sweep` and nothing of the Jobs or smoke's pods; smoke printed 44
PASS, 0 FAIL, 0 SKIP. Not seen: a restart of the agent, a renewal of the
authority, a flood of lines. The form below changed three things since the
first: the user (10001, no longer root), the files it opens (a list of the
workloads, no longer the whole namespace) and what it does with a line (names
removed, `CRITICAL` mapped). The first two were seen in the third run; the
third was run only over fixture files with the pinned image and is tested
without a cluster, not seen on one.
The owner chose, on 2026-10-06, that a node agent reads the pods' output and
ships it to Loki, over each service pushing its own records (which would not
have carried uvicorn's access line, output before a service's logging was set
up, or a crash). The agent is a second release of the collector's chart, the
contrib build (the core build has no receiver that reads files), as a
DaemonSet named `log-agent-agent` in the namespace `logging`:
[`values/log-agent.yaml`](values/log-agent.yaml), its image in `pins.env` as
`LOG_AGENT_IMAGE_*`, its policy in
[`manifests/logging-networkpolicy.yaml`](manifests/logging-networkpolicy.yaml).
`make up` installs it after the collector, once the ConfigMap `telemetry-ca`
(the authority's public certificate, which `up.sh` now publishes to `meridian`
and to `logging`) is there.

**What the owner's decision costs.** The agent mounts the node's pod-log
directory, `/var/log/pods`, read-only. That directory holds the output of every
pod on the node, in every namespace, and the pod can read all of it: a
compromised agent reads the output of every pod on its node, not only
`meridian`'s. The receiver is configured to open only the files of the
workloads in its include list (below), and to drop a record whose file resolves
to a path outside that list, but that is configuration of the pod, not a limit
on it: the include list is a set of path patterns, not the identity of a pod,
and writing under the directory needs the node. What bounds the pod:

- one host path and no other, mounted read-only and recursively read-only
  (`recursiveReadOnly: Enabled`, so a mount made under `/var/log/pods` is
  read-only too; Kubernetes v1.36 and containerd 2.3 on kind take it, and a
  node that cannot refuses the pod with an error, where `IfPossible` would run
  it without); the checkpoint is an `emptyDir`, not a writable host directory
- no host network, no host PID, no `hostPort` (every port of the chart is off,
  so the pod receives nothing; the chart still binds its health check, 13133,
  and its own metrics, 8888, on the pod's address, and the policy below denies
  ingress), no service-account token and no ClusterRole;
  namespace, pod and container are read from each file's path by the
  receiver's own `container` operator, so nothing calls the API server
- the container runs as the image's own user and group, 10001, not as root, with
  `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, every capability
  dropped, the default seccomp profile and a read-only root filesystem. The
  node's files are readable by their group: measured on the node on
  2026-10-06 (containerd v2.3.4), `/var/log/pods` is `drwxr-x---` (0750)
  `root:root`, a pod's directory is 0755 `root:root` and a container's log file
  is `-rw-r-----` (0640) `root:root`. So the pod holds gid 0 as a
  supplementary group (`podSecurityContext.supplementalGroups: [0]`), to read
  and for nothing else; `fsGroup` does not apply to a hostPath. The primary
  group is 10001, set explicitly (`runAsUser` alone leaves gid 0 where the
  image has no passwd entry, which would make the checkpoint group-root). The
  `emptyDir` is 0777, so the user writes its checkpoint. A pod **without** the
  group does not fail loudly: the pinned image, run as 10001 over a tree with
  these modes and without gid 0, started, logged no error and read no file, so
  "no `permission denied` in its output" proves nothing; smoke's line, which
  looks for a record in Loki, is what notices (on kind, in the third run, the
  pod with the group shipped the services' lines; the pod without it was run
  only over fixture files)
- a NetworkPolicy of its own: no ingress, and egress to DNS and the
  collector's 4318 alone, so what it reads can go to Loki and nowhere else (the
  cluster's DNS pods answer any name, which a few bytes can ride; the policy
  does not stop that)
- memory and CPU requests, a memory limit of 384 MiB (192 MiB until S073, see
  "Its resources" below), a CPU limit of 500m and the `memory_limiter` first in
  the pipeline
- smoke reads the live DaemonSet on every run (check 4) and fails when a chart
  bump changed one of: the one hostPath and its read-only mount, no host
  network, PID or port, `runAsNonRoot`, no service-account token

**What it ships.** Only the output of the pods its include list names, one
pattern for each pod family of the Meridian chart: the six services (a
Deployment's pod is `<service>-<hash>-<id>`) and the sweep (`meridian-sweep`,
whose Jobs' pods end in an id). They are the pods whose output went through the
JSON log format and the redaction of personal data. The three Jobs (`migrate`,
`seed`, `ingest`) run the CLI, which prints and does not log, so its tracebacks
and database messages are not redacted; smoke's own pods print what they like;
the database's output is PostgreSQL's, and the rate store's (S066, a
Deployment of the chart) is Redis's, in neither format and not redacted by the
platform's. **None of those is shipped**: the store's output stays in its pod's
output on the node (`kubectl -n meridian logs deploy/rate-store`), a Job's
output stays in `kubectl -n meridian logs job/<name>`, and `make deploy` prints
each Job's output as it ends. The list is positive, so a
workload the chart gains later is not shipped until someone lists it, and
`tests/meridian/test_log_agent.py` fails first (it holds the list equal to the
Deployments and CronJobs the chart renders, less the rate store). The
receiver's `filter` holds the same list as a regular expression over the
resolved path, and a test holds the two equal.

**What the agent itself loses, and who would notice.** The agent's own metrics
(its exporter's failed sends, the `memory_limiter`'s refusals) are served on
port 8888 of the pod and **nothing scrapes them**: an open item, not built.
What is seen is the DaemonSet not being ready (`MeridianLogAgentNotReady`,
[the runbook](../../docs/operations/runbooks/telemetry-missing.md)) and smoke's
line. A flood of lines from one pod shares the limiter with the others and may
stall or drop theirs (not tried), and kubelet's rotation (five files of 10 MiB
for each container) loses what the agent had not read before a rename. A
DaemonSet that does not exist leaves no series, so no alert says so.

**Its resources, and the spin of 2026-10-08 (S073).** On the kind cluster the
agent used 1.1 to 1.3 cores minutes after a start, sat at its memory limit
(192 MiB, then 191 in use) and read 2.2 GB/s from disk (read from the pod's
control group on the host; the node's pod log files are 20 MB). Prometheus's own
CPU series for the pod said 0.001 cores in the same minute. The pod's output
held one error before the spin, "Failed to open file ... no such file or
directory", for a finished CronJob pod's log file that the cluster had just
removed. A rig of one container (the pinned image, the receiver, processors and
checkpoint of `values/log-agent.yaml`, a tree of fixture files in the node's
layout, 192 MiB with no swap and two CPUs, `GOMEMLIMIT` at four fifths of the
limit, which is what the chart is taken to set: it was not available offline to
check) showed that the file is not the cause. **The cause measured is
the memory limit.** The contrib binary is 520 MB; the process wants well over
100 MiB of its pages in the control group's page cache besides a heap of about
50 MiB when idle. At 192 MiB there is room for the first only while the heap
stays small: a burst of lines (700 lines began it) grows the heap by 15 to
50 MiB and the cache pays; the mapped file pages of the control group fell from
110 MiB to 3, and the process reads them again as fast as it can evict them (6
to 7 GB/s of reads, 700,000 page refaults a second, a core spent in page
faults), with the exporter idle. It does not end when the burst does, because
the heap stays large. All of this needs the control group to have been charged
for the binary's pages: a container that finds them resident (in the cache of
another, deleted control group) looked calm in the same runs, which is how six
repeats of the first run missed it and why a rig must drop them with
`posix_fadvise(DONTNEED)` before each start. The executable as the evicted file
is inferred from the mapped pages, not profiled.

| Case (192 MiB, 2 CPUs, one change at a time) | CPU, cores | Reads |
| --- | --- | --- |
| Idle, 6.5 minutes | 0.01 | 0 |
| A watched file removed; its directory removed | 0.01 | 0 |
| The checkpoint names a file that is gone at start | 0.01 | 0 |
| A file rotated (300 new lines) | 0.08 to 0.76 | 0.5 to 5.6 GB/s |
| 700, 1,400, 2,800, 5,600 lines at once, one after the other | 0.04, 0.08, 0.09, 0.28 | 0.24, 0.6, 0.6, 2 GB/s |
| 7,000 then 28,000 lines at once | 0.1 to 0.95 | 0.6 to 6.7 GB/s |
| 28,000 lines, no `memory_limiter` | 1.1 | 7.3 GB/s |
| 28,000 lines, `retry_on_failure` off | 1.1 | 7.4 GB/s |
| 28,000 lines, no checkpoint (`storage` off) | 1.0 | 7.5 GB/s |
| 7,000 lines, the exporter failing (nothing listens) | 1.3 | 7.4 GB/s |
| 28,000 lines, the CPU limited to 0.25 cores | 0.25 (throttled) | 1.7 GB/s |
| 28,000 lines, 256 MiB | 0.04 | 0.02 GB/s |
| 140,000 lines, 256 MiB | 0.14 | 0.05 GB/s |
| 7,000 then 28,000 lines, the exporter failing, 256 MiB | 1.0 | 7.4 GB/s |
| 7,000 then 28,000 lines, the exporter failing, 320 MiB | 0.05 | 0.005 GB/s |
| 28,000 lines, 384 MiB (35,000 in, 35,000 out); 140,000 lines (140,000 in, 140,000 out) | 0.04; 0.11 | 0.002; 0 |
| 7,000 then 28,000 lines, the exporter failing, 384 MiB | 0.05 | 0.003 GB/s |
| 28,000 lines, 384 MiB, the CPU limited to 0.5 cores | 0.04 (throttled 0.08) | 0 |
| 140,000 lines, the exporter failing, 384 MiB | 0.55, then 0.05 to 0.12 | 2.9, then 0.3 to 0.8 GB/s |
| 28,000 lines, 512 MiB | 0.03 | 0 |

The rig ran 15 to 20 s samples of the container's CPU (from `/proc/<pid>/stat`)
and reads (the control group's `io.stat`) after each event, with one container
at a time. A file removed, a directory removed and a checkpoint naming a gone
file do nothing by themselves, and a rotation does only through its 300 new
lines; the "Failed to open file" line is therefore consistent with a
coincidence, and no trigger of its own was found. **What the values do now** (`resources` in
[`values/log-agent.yaml`](values/log-agent.yaml), tested): a memory limit of
384 MiB (320 MiB held and 256 MiB did not with the exporter failing; 384 MiB
keeps 180 MiB of cache then) and a CPU limit of 500m (the idle agent uses 0.01
cores and the heaviest burst measured averaged 0.11). **A CPU limit throttles
and does not end a spin**: at 0.25 cores the pod still read 1.7 GB/s, a quarter
of the rate; the memory limit is what removes the cause. It is not a cure for
everything: 140,000 lines at once with the exporter failing still pushed the
pod's anonymous memory to 270 MiB at 384 MiB and the reads to 0.3 to 2.9 GB/s.
No line was lost or repeated by the larger limit (the rows' counts, from the
exporter's own count). **Not known:** what raised the
heap on the cluster's instance (its trigger is not identified, only a
mechanism that reproduces it); whether the node's cache accounting matches the
rig's; whether the instance of 8.7 % of a core over a day was calm for the
same reason; the pod's CPU profile (none was taken). **To see it again on the
cluster**, measure the agent's process on the node, not Prometheus's series
(which said 0.001 cores while the process used more than one): read its CPU
time twice, 30 s apart (`docker top` on the node's container, the `TIME`
column of `otelcol-contrib`), and the control group's `io.stat` (`rbytes`) and
`memory.stat` (`workingset_refault_file`, `file_mapped`) for the pod. A
healthy agent shows a CPU time that moves by under a second in 30, no reads and
a few dozen refaults a second at most (0 to 43 on kind at 384 MiB, run LR1 of
2026-10-08); the spin shows a core and hundreds of thousands of refaults a
second. The pod's memory is no signal: the healthy agent reads 383 of 384 MiB,
the page cache filling to the limit.

Pod Security: `logging` is labelled `privileged` for `warn` and `audit`, as the
other namespaces are labelled and never enforced. `baseline` forbids a hostPath
volume, which this pod has; `restricted` forbids it too, and the hostPath volume
alone is what stops it, now that the user is not root. The label warns of
nothing: it says what the pod is. The pod is not in `observability` because
that would make `observability`'s `restricted` untrue of one of its pods.

Where the files are on kind: the node runs containerd, whose CRI plugin writes
`/var/log/pods/<namespace>_<pod>_<uid>/<container>/<restart>.log` as the files
themselves, in the CRI format; `/var/log/containers/*.log` are symlinks into
that directory, and `/var/lib/docker/containers` is Docker's own and is not
there. So one mount reaches the files. The collector chart's `logsCollection`
preset, which was not used, would also mount `/var/lib/docker/containers` and a
writable `/var/lib/otelcol` from the host.

The pipeline: the receiver reads each line (the CRI format gives the time, the
stream and the partial-line flag, and partial lines are joined), takes the
namespace, pod and container from the path, and sets the resource attribute
`service.name` to the container's name. The six services' containers are named
for their service and the sweep's is `sweep`; a CronJob's pod is
`meridian-sweep-<number>-<suffix>`. A line that is a JSON object becomes
attributes, its `level` the record's severity and its `message` the body (the
other fields, `logger`, `service`, `method`, `path`, `status` and `time`, stay
attributes, which Loki keeps as structured metadata); a line that is not (a
crash, output before the service's factory ran, a line cut off) is sent as it
is, with no severity. No line is dropped for failing to parse. Python's
`CRITICAL` is given the severity fatal (the receiver's own table has no entry
for it, and the record would carry no severity). Three things are done to keep
a pod's own words apart from what the file's path says:

- the receiver records the path of each file with its symlinks resolved, and a
  `filter` operator drops a record whose resolved path is not a file of a listed
  family. A symlink under a listed directory that leads to another pod's file
  ships nothing, and neither does a record that has no resolved path
- the stream (`stdout` or `stderr`) is parked on the resource, a `transform`
  processor removes from the record every attribute whose name starts with
  `service.`, `k8s.` or `log.` (the file's path and name, and whatever a
  line's own JSON named like them, `service.name`, `k8s.namespace.name`,
  `log.iostream`), and puts the stream back. The resource, which the path made,
  is not touched: in Loki `service_name` and `k8s_*` are the file's, and
  `service`, `level` and `logger` are the line's own word
- a line cannot take another service's name by carrying `service.name`; one
  that carries `"service": "policy-mcp"` is still only claiming it in its own
  stream's metadata

The pinned image was run over a fixture tree that had the node's modes
(a `0750` directory, `0640` files, owned as the node owns them, in rootless
Docker), with the debug exporter in place of OTLP: lines of both kinds arrived,
partial lines were joined, a file present at start was read from its end, the
Jobs', the database's, smoke's and other namespaces' files and a symlink to
one of them were not shipped, a line that carried the resource's names arrived
without them, `CRITICAL` arrived as severity fatal, user 10001 with gid 0 read
the files and wrote its checkpoint, and user 10001 without it read nothing and
said nothing. That check is by hand and is not a test of the repository. The
third run on kind confirmed the user, the group and the include list (see the
status above) and not the rest of this paragraph.

What a restart re-sends. The checkpoint, the offset of each file read so far,
is on an `emptyDir` of at most 32 MiB. It survives a restart of the container
(a crash, an OOM kill), which then re-sends nothing. It is gone when the pod is
replaced (a new release of the chart, a deleted pod, a reboot of the node):
the new pod starts each file it finds at its end, so it re-sends nothing
either, and the lines written while no agent ran are not sent. A file that
appears later is read from its beginning. Lines read and not yet exported (the
batch and the exporter's queue are in memory) are lost when the pod stops, and
an export that keeps failing for the exporter's retry window (five minutes) is
dropped. Kubelet keeps five rotated files of 10 MiB for each container; the
agent does not read the rotated ones. None of this was seen on kind: the agent
was not restarted in the three runs.

The agent verifies the collector's certificate against the ConfigMap
`telemetry-ca` in `logging`. The services re-read that file at each new
connection; the exporter of the agent is not known to, so after the authority
is renewed (`make up` publishes the new certificate to both namespaces),
restart the DaemonSet: `kubectl -n logging rollout restart
daemonset/log-agent-agent` (a renewal of the authority was not seen on kind,
so this is tested without a cluster, not seen on one). Reading the logs in
Grafana is in
[`docs/operations/README.md`](../../docs/operations/README.md), "Reading logs".

The agent ships whatever a service prints. What a line holds is the service's
doing: the JSON log format of this step's code half builds the access line from
the method, the path without its query and the status, and the redaction of
personal data runs before it. A service that still ran uvicorn's plain access
line would send the client's address and the query string to Loki, and the
agent would ship it unchanged. The edge's own access log is not the
services': its pod (`envoy-gateway-system`, container `envoy`) writes one JSON
line per request that keeps the whole request target, query string included,
and a client address (seen on kind on 2026-10-06). It stays in that pod's
output on the node and is not shipped to Loki: the include list names the six
services and the sweep in `meridian`, and smoke's ninth telemetry line holds
that Loki has no stream outside `meridian`.

### Who a service is (S055)

Mutual TLS with certificates from cert-manager: each service holds one, and
the one it calls learns which service it is from it. The chart renders a
`Certificate` for each of the six services and one for the ingestion Job
(`meridian-ingest`), always, because `make deploy` applies the Job outside the
release and its Secret must exist before its pod starts; `make deploy` waits
for every Certificate to be Ready right after the release. What the deploy
creates, besides what `make up` made (cert-manager v1.21.2 and the CA above):

- seven `Certificate` objects in `meridian` and, since S066, the rate store's
  eighth (`rate-store`, with the DNS name `rate-store.meridian.svc` and the same
  usages as a service that serves TLS: the gateway verifies that name, and the
  store asks every client for a certificate of this issuer's CA), signed by the
  `meridian-services` ClusterIssuer: ECDSA P-256, a new key at every renewal (`rotationPolicy:
  Always`, set in the chart), a lifetime of `certificate.duration` (90 days by
  default, renewed at 60: see "How long a certificate lasts" below), the URI
  `spiffe://meridian.kind/ns/meridian/sa/<name>`, and
  for the five services that serve TLS the DNS name `<name>.meridian.svc`;
- seven Secrets `<name>-tls` (`tls.crt`, `tls.key`, `ca.crt`) and the store's
  `rate-store-tls`, each mounted read-only at `/etc/meridian/tls` in its own pod
  and in no other;
- no new Service, port or NetworkPolicy.

The values `identity.trustDomain` (`meridian.kind`) and `identity.issuer` are
required and have no off switch; a service with `tls: true` in the chart's
values serves TLS, and the template adds the TLS flags (`--ssl-certfile`,
`--ssl-keyfile`, `--ssl-ca-certs`, `--ssl-cert-reqs 1`, `--http` with the
protocol class and `--ws none`: the words uvicorn's command line takes), the
HTTPS probes and the prefix its callers' URIs start with. The five commands in
the values start `python -m meridian.platform.common.tlsstart --factory <app>
--host 0.0.0.0 --port 8000` (S069: that module takes the same words, reads the
certificate once for uvicorn's context and for the health check, and ends a
start it cannot make safely with one `tlsstart:` line and exit status 3, which
the operations page lists), and the Claims API's stays `uvicorn --factory ...`.
Nothing else of the five's commands is repeated in the values. A values
override that sets `tls: true` on a command that starts `uvicorn` serves TLS
but reads the certificate twice, and the chart does not refuse it (a comment
in `values.yaml` says so, and a test holds the default values). The chart
fails for a service that another workload calls (a `serviceUrl` or a
`serviceMap` entry, the Jobs' included) and does not set `tls: true`; only the
Claims API, which nobody inside the chart calls, stays plain HTTP.

**How long a certificate lasts (S062).** Two chart values set the lifetime of
every one of the eight Certificates; the defaults render what the chart
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

then run `make deploy`, which reissues the eight certificates and waits for
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

What the watch showed, beyond the marks above, before the restarts were spread
(S073): all six services restarted in the same minute, because one deploy
issues their certificates in the same second, and with one replica each nothing
answered for about a minute. The chart now gives each service a share of the
restart margin, its place in the sorted list of services over their count (the
first none, the last five sixths) as `MERIDIAN_TLS_RESTART_SHARE`, and the
service looks at the file that share of a margin earlier than it did: the
restarts are spread across the margin, 100 seconds apart for one-hour
certificates and four hours apart for the default of 90 days, and never later
than before. Two replicas of one service would still restart together, because
they mount one Secret (not built: each service has one replica on kind). The
spread is implemented and tested with the chart rendered and the clock
injected. Seen on kind on 2026-10-07 (run R2): the shares on the six services,
0, 1/6, 2/6, 3/6, 4/6 and 5/6 in name order, and none on the rate store. Seen
on kind on 2026-10-07 (run R7, the watch above run again, one replica of each
service): the six services' containers each stopped once at a renewal, 99 or
100 seconds apart, in the reverse order of the shares, and in no reading were
fewer than five of the six Ready. Not seen: two replicas of one service, a
`renewBefore` shorter than one and five sixths of the margin, and the alert
`MeridianCertificateNotRenewed` in that run (it was given the values back
before the alert's hour). And
while the
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
  (the last line of the traceback in the pod's log, with exit status 1, for
  the Claims API under `uvicorn --factory` and for the five under the start
  module alike, since the module does not catch an app factory's error), and
  the container restarts (not seen on a cluster). A certificate the five
  services cannot read or build is not that: the start module ends it before
  any factory runs with one `tlsstart:` line and exit status 3, no traceback
  (tested with the real `python -m`, not seen on a cluster; the operations
  page lists the forms). That is better than starting with telemetry that fails on every
  export. The Jobs set no endpoint and get none of this; the sweep gets it
  (S064; its six findings arrived in Prometheus through this path on kind on
  2026-10-06). Status: tested without a cluster.
- **What the collector does.** It serves OTLP/HTTP with TLS on `:4318` from the
  Secret `otel-collector-tls`, mounted read-only as a directory, and reads the
  files again at a handshake every five minutes at most (`reload_interval`),
  so a renewed certificate needs no restart. The gRPC port, `:4317`, is closed.
  Its own hops to Tempo, Prometheus and Loki are TLS with its client
  certificate since S072 (the second Secret, `otel-collector-client-tls`): Tempo's
  receiver, and the two gateways in front of Prometheus and Loki, which admit a
  write from the one subject `CN=otel-collector-client`.
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
when it restarts; no certificate is revoked; nothing limits which listed
service's name a request in `meridian` asks for (approver-policy lets the
`meridian-services` issuer sign only a request from `meridian` with one of
eight listed URIs and, where it names one, one of six listed DNS names, so a
request from another namespace, or for a name not listed, is denied, but
whoever can create a `Certificate` in `meridian`, or change a policy or its
binding, can still mint any listed service's identity); the CA's private key
is readable by the operators that hold a cluster-wide read of Secrets
(cert-manager, cainjector; the CloudNativePG operator no longer does, since
S072), though by no Meridian pod; and the telemetry from the services to the
collector is TLS only by the second authority above (S063, tested without a
cluster), and the collector's own hops to Tempo, Prometheus and Loki are TLS
with its client certificate since S072 (implemented and tested, and seen on
kind in runs R14 to R17 of 2026-10-07; the threat model's T-90 says what it
does not stop).

A cluster whose services were first applied as raw manifests (before S019)
keeps them: Helm adopted the objects in place (`--take-ownership`) and no
pod was replaced by the adoption. On such a cluster the old field manager,
`kubectl`, still co-owns the fields it set, so a field a later chart version
drops would stay until the object is re-created; a cluster made after S019
has no such owner.

`make demo` posts the claims in `data/synthetic/claims.json` in order to the
Claims API through the edge, with a W3C `traceparent` header whose trace ID the
script made up. A claim that already has a triage proposal answers 409 and the
script moves on to the next one, so each run uses the next claim (47 are
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
run, five minutes later, is the retry. The CronJob keeps three succeeded and
three failed Jobs, and a by-hand Job counts toward those limits because the
CronJob owns it. With one succeeded Job kept, a by-hand success evicted the
schedule's own (seen on kind on 2026-10-07), so it keeps three: a by-hand run
or two leave the schedule's last success in the history. The oldest succeeded
Job is removed when a fourth finishes, about fifteen minutes later.
Kubernetes removes any Job a day after it finishes
(`ttlSecondsAfterFinished`), and that day is what keeps a failure to read in
the morning, and the last success of a suspended CronJob. A pod has no
service-account token, no extra privilege and a read-only root filesystem,
mounts only the CA's public certificate and the collector's, and may reach DNS,
the database and the collector's port 4318, and nothing else (S019; the
collector from S064, where `telemetry.otlpEndpoint` is set, with the SDK's
export deadline `sweep.telemetryTimeoutSeconds`, 5 seconds, for the findings it
sends before it exits, and, in S064's C3, the fixed instance ID
`service.instance.id=claims-sweep` so that every pass writes the same six
series; on kind on 2026-10-06 the findings arrived and the six series were one
`instance`, `claims-sweep`, in the third run; the 5-second deadline is tested
without a cluster, not seen on one). Prometheus keeps the sample with the later
timestamp, and a sample that arrives with an earlier timestamp is refused as
out of order (the collector logs it). Only a Job run by hand can overlap a
scheduled one (`concurrencyPolicy: Forbid`).

To run one pass now, beside the schedule (the name is yours; it must be new):

```sh
kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian \
  create job --from=cronjob/meridian-sweep meridian-sweep-by-hand-1
kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian \
  logs job/meridian-sweep-by-hand-1
```

`make smoke` does not take the by-hand Job for the schedule's: it carries the
annotation `cronjob.kubernetes.io/instantiate: manual`, so the seventh line
leaves it out of its verdict and says, when it is the newest Job of all, that
it was made by hand. Seen on kind on 2026-10-07: with one success kept, a
by-hand success evicted the schedule's, and the line failed a healthy schedule
for one period; the history limit of three and the verdict's third case (a
SKIP that says the schedule's newest run is not in the history, a FAIL when
the schedule's last time is older than the bound) are the fix, tested with
stand-ins; what run R4c saw of it on 2026-10-07 (the by-hand Job not evicting
the schedule's success, the line passing with its note) is under check 7
above, and the case of the history that lacks the schedule's newest run was not
seen. To
stop the schedule, patch `suspend` to `true` on the CronJob; `make smoke` then
prints SKIP for the sweep until it is `false` again.

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
(S024, S056, S064, S066) in five groups: `meridian.gateway.recording` (a recorded
series for the gateway's calls of the last 15 minutes), `meridian.gateway`
(alerts on that series), `meridian.workloads` (from kube-state-metrics),
`meridian.certificates` (from cert-manager's controller) and
`meridian.telemetry` (two recorded series of the same kind, for the
runtime's completed model calls and the Claims API's stored triages, and
three alerts that notice a series that is not there, and a fourth that
notices the log agent not being ready). It holds 18 alert
rules and three recording rules: six on the gateway, four on the workloads
and four on the certificates (the gateway's sixth, `MeridianRateStoreRefusing`,
fires when, of the calls of the last 15 minutes that the rate store could have
counted, more than 5 percent and at least 5, or more than half and at least 2,
were refused because the store gave no answer, for 2 minutes: one refused call
no longer fires it, and calls refused before the store is asked do not dilute
it; loaded and healthy on kind on 2026-10-06 in this form, after `make up`,
before S073 only `make up` applied the rules and `make deploy` did not; now
`make deploy` applies the alert rules too, seen on kind on 2026-10-07: its log
line in run R3, and in run R4b a `for` put back and the rule object made again;
never seen firing), and, from
S064, four on missing telemetry
(loaded and healthy on kind on 2026-10-06, in the third run, and none seen
firing; tested without a cluster, not seen firing on one): the Model Gateway's,
the Agent Runtime's and the sweep's metrics, each fired when the upstream end
counted something in the last 15 minutes and the downstream end has no
sample at all, and `MeridianLogAgentNotReady`, which fires when the log
agent's DaemonSet in `logging` has had fewer ready pods than nodes for 10
minutes (it reads kube-state-metrics' two numbers of a DaemonSet, which the
stack serves: only the `secrets` collector is excluded, and the two series
for `logging` were seen on kind on 2026-10-06; a DaemonSet that does
not exist leaves no series and so no alert, and smoke's line says it is not
there). The CronJob of the sweep sets one instance ID
(`service.instance.id=claims-sweep`), so its six series are the same from
pass to pass (seen on kind on 2026-10-06). The workloads' fourth alert,
`MeridianRateStoreRestartLoop` (S072), fires on three restarts of the rate
store's container in 15 minutes: implemented and unit-tested, loaded by `make
up` and `make deploy`, not seen firing. It
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
ConfigMap that Grafana's sidecar provisions. `make deploy` applies the rules
again (the same function, S073), so a rule changed in the tree needs no `make
up`; the dashboard is `make up`'s alone. `make smoke` reads them back
(check 11, passed on the cluster on 2026-10-06 with four groups and, in the
third of S064's runs, with five groups and all 19 rules, and in the third of
S066's runs the same day with all 20, `MeridianRateStoreRefusing` among them):
Prometheus has loaded the
five groups with every rule healthy, the loaded rule names are the file's,
no Meridian alert is firing, and Grafana serves the health dashboard with
the file's queries, every one of which runs in Prometheus. What stays by
hand is whether each series a rule or a panel names exists: a rule over a
series that is not there is healthy and quiet, and a panel with no data is a
success. The checklist is in
[the operations index](../../docs/operations/README.md#not-proved-on-a-cluster),
which also links the objectives the rules watch and the runbooks they
point to.

## Who holds the cluster

One plan step uses the cluster at a time (the plan's Part A). Since S075 the
rule leaves a record that the commands read. Status: **implemented**, tested
against stub commands, and seen on kind once (the record written
through a deploy, a deploy refused while another ran, and `make down`
refused with the record at `ok`). Seen on kind on 2026-10-07 (run R2):
`TAKE_CLUSTER=1 CLUSTER_HOLDER=S073 make deploy` took the record from another
holder, S075, and left it at S073, `ok`. Not seen on a cluster: a record left
`changing` by a run that failed, and `make up` on an existing cluster; the
stub tests hold each.

The record is the ConfigMap `meridian-cluster-holder` in `kube-system`, with
four values and nothing else (no path, no user or host name, no address of a
remote):

- `holder`: `CLUSTER_HOLDER` from the environment when it is set (letters,
  digits, `.`, `_`, `/` and `-`, at most 100 characters; anything else stops the
  command with a sentence), otherwise the current branch's name, otherwise, in
  a detached checkout, `detached@<short commit>`. A session that runs from a
  detached checkout, as the main session does, names itself with
  `CLUSTER_HOLDER`. A branch name outside those characters, or a checkout git
  cannot read, stops the command and asks for `CLUSTER_HOLDER`.
- `commit`: the short hash of the checkout that ran the command.
- `time`: UTC, to the second: when the holder's last run started to change
  the cluster, or ended well.
- `state`: `changing` from the moment a run starts to change the cluster,
  `ok` when it ends well. A record with no `state` (written by the first
  version of this rule) reads as `ok`; any other text reads as `changing`.

Which commands read it and which do not:

| Command | Reads the record | Writes it |
|---|---|---|
| `make up` | Once the cluster exists, before it changes anything; on a machine with no cluster it creates one and reads nothing | `changing` right after the check passes (on a new cluster, as soon as it answers), `ok` at the end, when it ended well |
| `make deploy` | Before it builds or runs anything | `changing` right after the check passes, `ok` at the end, when it ended well |
| `make cert-renew` | After it checked `CERT` and found the cluster, before it reads the Certificates; a refusal after that (not a Certificate, none in the namespace) leaves the record as it was | `ok` right after the write succeeded; nothing when it only refused, found the Certificate already being issued, or the write was refused (the record is then as it was) |
| `make demo` | Only through `make deploy`, which it runs first; then it posts a claim without asking | Through `make deploy` |
| `make down` | Before it deletes the cluster; a cluster that does not answer stops it (`TAKE_CLUSTER=1` deletes it all the same) | No: the record goes with the cluster |
| `make cluster-holder` | Yes, and prints it | No |
| `make smoke`, `make gateway-upkeep`, `make images`, `make grafana`, `make grafana-password` | No | No |

With no record (a cluster made before S075) a command says so in one line and
goes on: this is the one case nothing protects, so a cluster that predates the
record is anybody's until the first `make up` or `make deploy` writes it. With
the same holder it goes on, whatever commit the record names and whatever the
state (a retry after a failure is the ordinary case); when the state is
`changing` it says so in one line, with what that means (a `make up` or
`make deploy` is running, or the last one did not end well). With
another holder it stops before it changes anything, whatever the state, with
one sentence that names the holder, its commit and its time, for example:

```text
error: the cluster is held by s075-f1 (commit abc1234, since 2026-10-06T12:00:00Z), not by s075-m2; nothing was changed, and TAKE_CLUSTER=1 in front of the same command (TAKE_CLUSTER=1 make deploy) takes it
```

When the state is `changing` the sentence says what that means, a run going
or one that failed, and to wait for it or to look at what failed before
anything is deleted (the take and `make cluster-holder` use the same words):

```text
error: the cluster is held by s075-f1 (commit abc1234, since 2026-10-06T12:00:00Z), whose record says changing (a make up or make deploy is running, or the last one did not end well): wait for it, or look at what failed before anything is deleted; it is not held by s075-m2, nothing was changed, and TAKE_CLUSTER=1 in front of the same command (TAKE_CLUSTER=1 make down) takes it
```

`TAKE_CLUSTER=1` (exactly `1`) in front of the same command takes the cluster:
the command says whose cluster it takes in one line and goes on, and writes
itself as the holder when it ends well. A cluster that does not answer stops
`make up`, `make deploy` and `make down` (who holds it cannot be told), and so
does a record that cannot be read; an answer that failed is never taken for
"no record". `make down` of a broken cluster that nobody uses is therefore
`TAKE_CLUSTER=1 make down`.

The record is written when `make up` or `make deploy` starts to change the
cluster, and again when it ends well, because a run that failed once left the
cluster with no record, and the next command deleted it with the evidence of
the failure. A run that fails or is interrupted leaves `changing`: the cluster
is still its holder's, and another holder is stopped until it has looked at what
failed (the run's own output, the pods' logs, the audit rows) and takes the
cluster. `changing` is also what a run in progress shows. A `make deploy` that
stops at a prerequisite (`make up` was not run) leaves `changing` too, and the
same holder runs it again at no cost. `make down` does not write the record: it
deletes the cluster, and the record goes with it.

It is a notice for an honest mistake, not a lock: two commands started in the
same second both pass the check, two checkouts that use the same holder name are
one holder to it, `TAKE_CLUSTER=1` passes it, and anyone who can use `kubectl`
on the cluster can edit or delete the ConfigMap. It says nothing about what is
changed by hand or by a command that does not read it.

## The sign-in issuer (S021)

Keycloak is the local stand-in for the issuer of sign-in. Entra ID is the
issuer on Azure; nothing here is for Azure. It is an **add-on that is off unless
switched on** and is not part of plain `make up` (the owner, 2026-10-08: "Keep
it, opt-in only"). Status: **tested without a cluster, and seen on kind once
(run KR1, 2026-10-08): `make up` with the switch on ended 0 in 54 seconds, the
pod was Ready at its first start, and smoke passed all but one line, whose
expectation is corrected here.** Keycloak 26.8.0 had run in a container before
(the opt-in rig of S021's Y2a). What KR1 did not show is listed at the end of
this section.

```sh
MERIDIAN_IDENTITY=keycloak make up      # makes the add-on, last
MERIDIAN_IDENTITY=keycloak make smoke   # six lines for it
infra/kind/identity.sh status           # read-only
```

`MERIDIAN_IDENTITY` is empty (off, the default) or `keycloak`. Any other value
stops `make up`, `make smoke` and `identity.sh` before they do anything, with a
usage line: a typo must not read as "off". Those three are the only scripts that
read the switch and so the only ones that check it; `make down`, `make
cluster-holder`, `make deploy` and the rest never stop for it, so an exported
typo cannot block a teardown or the holder diagnostic. With the switch off
nothing of this is made: no namespace, no manifest, no image pull, and the
edge's manifest is the committed `manifests/gateway.yaml` byte for byte (a test
holds it). The only differences in output are one more line in `make smoke`,
`SKIP  issuer: ...` (so its last line reads "All checks that ran passed; 1
skipped." instead of "All checks passed."), and, on a cluster where an earlier
run made the add-on, one line from `make up` saying that the namespace still
exists.

What the switch makes (`infra/kind/identity.sh`, which `up.sh` calls last).
**Tested without a cluster; every row was made on kind once (run KR1,
2026-10-08), and the list at the end of this section says what that run did not
show.**

| Object | Where | What it is |
|---|---|---|
| Namespace `identity` | `manifests/identity-networkpolicy.yaml` | Pod Security `warn` and `audit` at `restricted`; made here and not in `namespaces.yaml`, so the off path has none |
| Policies `default-deny`, `egress-dns`, `keycloak` | the same file | Denied both ways; DNS is the only egress; the pod admits the edge's proxy pods and the Claims API's pods on 8080 and nothing else |
| Policies `identity-edge-egress`, `identity-claims-api-egress` | `manifests/identity-peers-networkpolicy.yaml`, in `envoy-gateway-system` and `meridian` | The other ends of those two connections, as policies of their own that add to the existing ones, so the edge's file and the chart do not change with the switch |
| Secrets `keycloak-realm`, `keycloak-credentials` | `identity`, made by the script | The realm file, and the same passwords and secrets as `KEY=value` lines |
| Deployment, Service `keycloak`, ServiceAccount | `manifests/identity.yaml` | One replica from `KEYCLOAK_IMAGE` (by digest, from `pins.env`); the Service has port 8080 only, never the health port 9000 |
| HTTPRoute `keycloak` | `manifests/identity.yaml` | Host `id.meridian.localhost` (a `.localhost` name, the chart's own rule, tested); forwards what the sign-in flow needs and nothing else: `/realms/meridian-staff/.well-known/`, `/realms/meridian-staff/protocol/openid-connect/`, `/realms/meridian-staff/login-actions/` and `/resources/`; so `/admin/`, `/realms/master/`, the staff realm's `account/` and `clients-registrations/` get the edge's 404 (seen on kind, run KR1, for the first two prefixes and the closed paths; that the login page still loads in a browser with all four is untried) |
| The edge Gateway `edge` | `gateways.sh`, applied by `up.sh` | With the switch on, the listener admits routes from the namespaces named `meridian` or `identity` (a selector on the name label, never `All`) |

The Claims API's own egress needs no change in the chart: the policy
`identity-claims-api-egress` gives its pods the one rule, to Keycloak's pods on
8080, and only when the switch has been on.

**What it refuses.** Every check that only reads runs first, and nothing
changes on the cluster until the last has passed. `identity.sh up` stops with
the switch off, a route host name that is not a `.localhost` name, an image pin
that is not `name:tag@sha256:digest` or a manifest that does not hold its
placeholders as expected (or a third `image:` line), a Docker engine that is not
local, a cluster that does not answer or that another checkout holds
(common.sh's helpers; every call names the local cluster's kubeconfig and
context), under 2,500 MB of memory available (it reads `/proc/meminfo` and
prints the figure, before and after), and an edge that does not yet admit routes
from `identity`. It has no command that removes anything. The memory figure is
the host's, not the scheduler's: a node whose allocatable memory is used up by
the platform's requests leaves the pod Pending with "Insufficient memory" even
when the host has memory free, and the message after a timed-out rollout says
so.

**Who holds the cluster.** `up.sh` records `ok` before it calls the add-on. The
script records `changing` after its last check, just before its first change,
and `ok` at its end. A refusal that changed nothing (memory, a bad pin) leaves
`ok`; a run that stops half way leaves `changing`, also when `identity.sh up` is
run by hand, and prints on standard error that the add-on is partly made and
that `identity.sh status` says what is there. Running `make up` again converges:
every apply is server-side, and the Secrets are kept when both are of one
generation and made anew when not (see "The realm and the Secrets").

**The realm and the Secrets.** `identity-realm.sh` makes the realm at run time
into a folder under `${XDG_CACHE_HOME:-~/.cache}/meridian-identity/`, mode 700,
never the repository. The folder is removed as soon as the Secrets are made and
by the script's exit trap on every exit it can see (it ends, a failure, HUP,
INT, TERM). A `kill -9` or a power cut leaves it, so each run, at the start of
its Secrets step, sweeps: it looks only at entries named `realm.` and six
letters or digits in that cache folder, skips a symbolic link and anything that
is not a folder, skips a folder touched in the last 60 minutes (a run in
progress), deletes the regular files of the others and then the folder itself
with `rmdir`, and leaves alone, with a line that says so, a folder that holds
anything else. The values are loaded into the two Secrets through pipes and are
never printed or put on a command line. Both carry one annotation,
`meridian.local/identity-generation`, 16 hex characters made once per run (no
secret; read from the annotations, never the data). A second run keeps the
Secrets it finds only when both are there and carry the same generation, so a
rotation that died between the two is made anew by the next plain run; a
cluster whose Secrets were made before generations were recorded gets new ones,
and so new passwords, at its next `make up` with the switch on.
`MERIDIAN_IDENTITY_ROTATE=1` makes new ones on request, and that ends every
session: the test users' passwords, the clients' secrets and
the signing keys all change. A pod that is running never imports a realm again,
so the pod template carries the SHA-256 of the realm Secret's content (an
annotation, read as the gateway of the telemetry stack reads its certificate's;
the content is never printed): a changed realm rolls the pod on any run, also a
plain run after a rotation that was interrupted before the Deployment was
applied. The Claims API's own
copy of its client secret is Y3's and Y4's to make.

**The cast (Y2e).** The realm holds seven test people of one fictional
insurer, to sign in with (the owner, 2026-10-08: "Users, one organisation"),
organised in groups as a business directory is, and a user's role comes from
the group:

| Group | Its one realm role | Members |
|---|---|---|
| `meridian-platform-admins` | `platform-admin` | 1 |
| `meridian-agent-developers` | `agent-developer` | 1 |
| `meridian-adjusters` | `adjuster` | 3 |
| `meridian-auditors` | `auditor` | 1 |
| none (to show a refusal) | none | 1 |

On Azure the same four groups are meant to exist in Entra ID (designed; nothing
is built or applied there). Each person has a user name (`firstname.lastname`),
a first and a last name, membership of one group and no role of their own (the
seventh is in no group, so has no role), and an e-mail address under the
reserved `.example` domain. Nothing about groups goes into a token: the access
token carries the top-level `roles` list as before, which is what the services
read, so Keycloak and Entra stay interchangeable. The pinned Keycloak gives a
user in no group no `roles` claim at all (seen in a container, not on the
cluster), and `check_bearer` of `signin.py` reads an absent claim as no roles.
The names are written in `identity-realm.sh` (`STAFF_CAST`), are
Nordic on purpose, and a test fails on any word they share with the synthetic
claimants' and policy holders' names. Each has a password made at run time like
the clients' secrets: new at every rotation, never committed, never printed by
`up`, `status`, `users`, smoke or a log.

```sh
infra/kind/identity.sh users     # user name, display name, group, role; no password
make identity-passwords          # each user name and password; prints secrets
```

`users` is read-only and reads the realm Secret, so it lists the people the
cluster really holds, with the group and the role each has: `up` keeps the
Secrets it finds when they are of one generation, and a cluster made before the
cast still holds the four `test-<role>` users, with roles of their own and no
group, until `MERIDIAN_IDENTITY=keycloak make up` makes the new realm (a pair
made before generations were recorded is made anew by a plain run).
`make identity-passwords` is the only code path that prints a password.
Its first line says they are disposable test passwords of the local mock issuer,
made for this cluster, and that a rotation replaces them. It
refuses a Docker engine that is not local, a kubeconfig whose server for the
cluster's context is not `127.0.0.1`, `localhost` or `[::1]` (read from the
file, before any Secret is read, so a stale kubeconfig of another cluster
cannot print its passwords) and a cluster that does not answer, and it refuses
when its output is not a terminal, so a pipe or a log file does not keep the
passwords by accident; `MERIDIAN_IDENTITY_SHOW=1` says that you mean it. Like
`make grafana-password`, it is for a terminal of your own: in a session its
output is the transcript. It prints what the realm Secret holds, which is what
Keycloak runs once the pod has rolled onto that Secret: after a rotation that
stopped half way, run `make up` again first. `users` and `passwords` print
ASCII only (control characters and any other byte of a name are dropped, so
that a name cannot write to your terminal); the cast's names are ASCII.

This is **kind only** and a **mock issuer**: on Azure the issuer is Entra ID and
there is no cast. And signing in with these users **does nothing in the pages
yet**: no route of the Claims API is wired to the issuer, so the cast is a list
of logins the realm accepts and not a working sign-in. The tokens a signed-in
user would carry are measured by the opt-in rig (`test-adjuster` in its older
runs is `ingrid.strand` now); that all seven sign in through the pages' flow,
that each carries the role of their group in `roles` and no `groups` claim, and
that the group-less one carries no `roles` claim at all, was seen twice, in a
container, not on the cluster.

**Memory.** The pod requests 700Mi and may use 1Gi (no CPU limit); 600 to 700
MiB were observed in a container against that limit, and the container cost
about 570 MB of the host's available memory. On kind (run KR1) the machine's
available memory went from 4,984 to 4,472 MB, about 510 MB, with the platform
up. The script refuses under 2,500 MB. The suite's own room check wants 3,500
MB; that a whole suite still starts with the add-on up was not tried (the plan's
S021 section makes that Y2's stop).

**Restarts.** Development mode keeps its database in the pod and nothing
persists: a restart makes new signing keys and imports the realm again. The
users keep their subjects (the generator derives their ids from the realm and
the name). A service with a cold key cache refuses a bearer token from before
the restart; one with a warm cache accepts it for up to an hour; a session
cookie is not checked against the issuer again (threat model T-120).

**Plain HTTP inside the cluster, said.** The edge reaches Keycloak over plain
HTTP, and so does the Claims API (the key set, and with Y3 the token). That is
accepted on a disposable development cluster and is **not** how a business runs
its issuer: it runs it behind TLS end to end. The backlog row is S094's (TLS to
the issuer); the threat model's T-06, T-117 and T-120 say the same.

**Removal is by hand, and turning the switch off does not do it.** A later `make
up` with the switch off applies the committed Gateway again (the listener admits
`meridian` only, so the route is no longer accepted) and says in one line that
the namespace is still there. To remove the add-on, on this disposable cluster
(the Secrets, and so the realm, go with the namespace):

```sh
export KUBECONFIG=$PWD/infra/kind/kubeconfig
kubectl delete namespace identity
kubectl -n meridian delete networkpolicy identity-claims-api-egress
kubectl -n envoy-gateway-system delete networkpolicy identity-edge-egress
```

**Seen on kind, run KR1 (2026-10-08).** `make up` with the switch on ended 0 in
54 seconds. The scheduler accepted the pod, and the pod was Ready at its first
start with the read-only root file system and no restart: the init container
`copy-quarkus` (`cp -R`) completed with an empty log, and the main container
ran with every path that `start-dev` writes (`/opt/keycloak/lib/quarkus`,
`/opt/keycloak/data` and `/tmp`) an `emptyDir`. The realm Secret, mounted at
`/opt/keycloak/data/import` inside the `emptyDir` at `/opt/keycloak/data`, was
read ("Realm 'meridian-staff' imported"), and Keycloak started in 8.3 seconds
with no ERROR line. The probe on 9000 passed under the default deny, after one
refused connection while the server started; the management interface listens
on 9000 over plain HTTP, and the pod had no restart in the three minutes of the
run (the liveness probe, `/health/live`, has not failed). The `imageID` ends in
the pinned digest. The route is Accepted, and the listener admits `meridian`
and `identity`. Through the edge the discovery document (path `.well-known/`)
and the key document (path `protocol/openid-connect/`) were served, and
`/admin/`, `/realms/master/`, `account/` and `clients-registrations/
openid-connect` were the edge's own 404 with an empty body. From the Claims
API's pod, the discovery document fetched by the Service's name
(`keycloak.identity.svc:8080`) names the front URL as its issuer, which shows
the policy that admits the Claims API, the path and `--hostname` working, and
that the edge reaches the pod through its policy. The machine's available
memory went from 4,984 to 4,472 MB (about 510 MB) with the platform up. Smoke
with the switch on passed 61 lines and failed one, the climbing line, whose
expectation was wrong and not the route: for an escaped slash (`..%2f`) the
edge unescapes, normalises and answers 307 with an empty body and a Location of
`/realms/master/`, which is itself the edge's 404 (the main session's
measurement; nothing reaches Keycloak). The line now accepts that, and nothing
else but the edge's own empty 404. Smoke with the switch off passed 56 lines
and skipped one. The fallback, should a later image need a path that is not
covered, is `readOnlyRootFilesystem: false` in `manifests/identity.yaml`.

**Seen on kind, runs KR2 and KR3 (2026-10-08).** KR2, a second `make up` with
the switch on, kept both Secrets (the line that says so) and the same pod, with
no restart; smoke passed 62 lines with the switch on. KR3 was the rotation, on
a cluster that still held the four `test-<role>` users (`identity.sh users`
listed them, each with a role of its own and no group):
`MERIDIAN_IDENTITY_ROTATE=1 MERIDIAN_IDENTITY=keycloak make up` ended 0 in 50
seconds, made both Secrets anew, and the pod rolled through its annotation: a
new pod, Ready with no restart, "Realm 'meridian-staff' imported", started in
8.1 seconds, no ERROR line. `identity.sh users` then listed the cast of seven
as the table above has it: six people in the four groups, each with the role
of the group, and one in no group with no role. Smoke passed 62 lines with the
switch on and none failed; with it off 56 passed and one was skipped. The
machine had 4,080 MB available before the run and 4,820 MB after it, with 2.2
to 2.9 GB of its 4 GB of swap in use throughout. **Not seen in KR3:** a person
of the cast signing in on the cluster, or any token issued there (the sign-ins
are the rig's, in a container); the passwords command (it is for the owner's
terminal); what the old pod's tokens are worth after the roll.

**Untried: what the runs did not show.** Each is a thing to tick off, in this
order:

1. A token by client credentials from inside the cluster (the `iss` it carries
   when asked by the Service's name), and what `/` and `/admin/master/console/`
   show to the Claims API's pod.
2. Which built-in clients exist after the import (`admin-cli`,
   `account-console`), and what the master realm and the welcome page show on
   8080 to the two admitted peers.
3. A refusal by the network policy from a pod that is not admitted, and from a
   pod of `meridian` that is not the Claims API (smoke has no line that proves a
   refusal yet).
4. The login page in a browser with the narrowed prefixes: only two of the four
   were exercised (`.well-known/` and `protocol/openid-connect/`, by smoke);
   `login-actions/` and `/resources/` need the page itself, and a path that the
   form or the page needs and the route lacks would show as a broken page or a
   404 in the network tab. And the cookie question: Keycloak's login cookies are
   `Secure; SameSite=None` on an HTTP name, and whether a browser keeps them on
   `id.meridian.localhost` is not established.
5. The switch turned off and on again with `make up` (the Gateway narrows and
   widens, the one line appears); only smoke with the switch off was run, with
   the add-on left up.
6. A rotation interrupted on purpose, between the two Secrets and before the
   Deployment is applied, then a plain run (a whole rotation was seen in KR3).
7. A whole suite starting with the add-on up (the suite's room check wants
   3,500 MB; 4,452 MB were available after the run).
8. Not on the list of the run but still not seen: the image pull on a node that
   does not have the image yet (it was already there) against the five-minute
   rollout timeout.

## If `make up` was interrupted

Rerunning `make up` is the first thing to try. If a release is stuck in a
`pending-*` state, or the node was only half created, run `make down` and then
`make up` again. A chart that could not be downloaded in time is such a case:
on 2026-10-06 `make up` on a cluster made from nothing stopped after 296
seconds at the Tempo chart (`context deadline exceeded`, fetching it from
GitHub, with the machine under load), and running `make up` again completed in
125 seconds, so a cold start depends on the chart hosts being reachable.

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
| `127.0.0.1:8088` | The edge (Envoy), from kind's `extraPortMappings` to node port 30080. Loopback only. Hosts: `claims.meridian.localhost` (the Claims API) and, with `MERIDIAN_IDENTITY=keycloak`, `id.meridian.localhost` (the sign-in issuer). |
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

### How long the scripts wait for the API server (S073)

Every `kubectl` call of the scripts goes through `kctl` (`common.sh`), which
bounds it by what the call is. A frozen node used to hang `make smoke` or `make
deploy` with no word; now a call ends with kubectl's own error, or with a line
that names the bound.

| Call | Bound | Set by |
|---|---|---|
| An ordinary call (`get`, `apply`, `create`, `patch`, `label`, `logs`, a `delete` with no `--wait`) | `--request-timeout=15s`, a request | `KCTL_REQUEST_TIMEOUT`, for instance `20s` |
| `exec`, and a `delete` with `--wait` (also `--timeout=60s`) | the system's `timeout`, 90 s, which then prints the line `kctl: kubectl exec ended with status 124 ...` | `KCTL_OUTER_TIMEOUT`, in seconds |
| `wait` and `rollout status` | their own `--timeout` at every call site, and the system's `timeout` for that value plus 30 s, which then stops the script with a line that says the API server did not answer; a call with no `--timeout` is refused; no request flag, which may end the watch early | the call site; `KCTL_WAIT_MARGIN`, in seconds |
| `port-forward`, `attach`, `logs -f`, `get -w` | none, they are streams; `port-forward` is started raw (`kubectl ... &`) by `smoke.sh` (in `open_grafana`, which is in `smoke.d/shared.sh`) and `demo.sh`, which kill it and look for its port with a counted loop, and by `grafana.sh` in the foreground, which ends with ^C; no script uses the other three | |
| `helm get` (`upkeep.sh`), which has no timeout flag | the system's `timeout`, 30 s | `HELM_READ_TIMEOUT`, in seconds |
| `helm upgrade --install` | `--wait --timeout 10m` for each of `make up`'s ten releases; `--timeout 300s` for the chart in `make deploy`; and the system's `timeout`: for `make up`'s releases (their charts are taken to carry hooks; not rendered to check) three times that value plus 60 s, for the chart in `make deploy` (no hook, no `--wait`) that value plus 60 s; it stops the script with a line that names `helm status` and `helm history`; a call with no `--timeout` is refused | `up.sh`, `deploy.sh`; `HELM_UPGRADE_MARGIN`, in seconds |

A call that passes its own `--request-timeout` (the reads of the API server's
address and of the holder's record) keeps it. `kctl` reads the words of the
call up to `--`, so the command an `exec` runs decides nothing, and a namespace
called `wait` is a namespace. The scripts need `timeout` (coreutils: GNU's, or
uutils 0.10.0, which run R4e used and whose statuses 124 and 137 behaved the
same in the re-read's runs; on macOS, `brew install coreutils` puts GNU's on the
PATH as `gtimeout`, so add a `timeout` link) and `up.sh`, `smoke.sh`,
`deploy.sh` and `upkeep.sh` say so at their start. Why the flag is not on every
call: kubectl's help says the flag bounds "a single server request", and says
nothing of what it does to a watch, a log stream or an exec session, so those
calls get the bound that is written for them.

Two things the bounds do not mean. The request flag is per request and not per
call: against a listener that accepts a connection and never answers, `kubectl
get --request-timeout=4s` took 20 s, because the client tries discovery five
times (measured by the review of S073, 2026-10-07, at 4 s; 15 s was not
measured and gives about 75 s). And a `--timeout` of `wait`, `rollout status`
or `helm upgrade` bounds the waiting loop and not the first request: against
the same listener all three were still running after 25 s with a `--timeout`
of 3 s, which is why each now has the outer bound. The Helm margin is the
wider one because a Helm ended in the middle of an install or an upgrade can
leave the release `pending-install` or `pending-upgrade`, and Helm's
`--timeout` is per operation ("time to wait for any individual Kubernetes
operation (like Jobs for hooks)", Helm v4.3.0's help), so a chart with hooks
may lawfully take a pre-hook, the wait and a post-hook: for `make up`'s
releases the bound is three timeouts plus the margin, a ceiling and not a
measurement (nobody measured the hooks' time). When the line says it ended
one, read `helm status` and `helm history` of the release before anything
else, and change nothing until they say what state it is in; the way out
(`helm rollback` or an uninstall) is the owner's, on the list of things a
session asks before. Each margin is digits only, at most six, or empty for the
default; anything else stops the script at its start.

What was seen on kind: `make deploy` and `make smoke` with the request flag
and the `exec` bound on their calls (run R2, 2026-10-07, and the runs after
it), which passed. `make deploy` and `make smoke` also ran under the outer
bound on `wait`, `rollout status` and Helm on the path where nothing goes wrong
(run R4e); no bound has fired on kind. A frozen API server (the node paused
with `docker pause` for thirty seconds) was met once (run R5b, 2026-10-07):
every call ended after 10 seconds at the client's own handshake timeout and
never reached these bounds. A bound of the wrapper ending a call is not seen:
only the tests' stand-ins and the review's silent listener have met a server
that accepts and never answers.

## Memory

The laptop this was built on gives Docker Desktop 7.65 GiB. The memory limits
of the components add up to about 4.4 GiB (the log agent of S064 adds a limit
of 192 MiB, a request of 64 MiB, and no measurement yet; S073 raised its limit
to 384 MiB, which the sum does not include). Measured with
`docker stats` on the node container on 2026-09-30, after a `make up` from no
cluster, a second `make up` and one `make smoke`: 3.6 GiB, which includes the
Kubernetes control plane, the kubelet and containerd. The six Meridian services
add limits of
192 MiB (Claims API, Model Gateway) and 256 MiB (Agent Runtime and each tool
server), 1.4 GiB in all; a Job adds 192 MiB while it runs. Measured from
cAdvisor on 2026-10-02, twice, after demo claims and smoke runs: the working
sets were 54 to 115 MB (the runtime the largest, the tool servers 72 to
87 MB), and `docker stats` showed 4.9 GiB for the node container.

The rate store (S066) adds a limit of 64 MiB, a request of 32 MiB, to those
limits (about 4.5 GiB in all). The two gateways of S072 (Loki's, and the nginx
in front of Prometheus) add a limit of 64 MiB and a request of 32 MiB each, so
128 MiB of limits and 64 MiB of requests more, still about 4.6 GiB in all; the
Prometheus one held 4.3 MiB in a container of the pinned image, run as the pod
runs it. On the cluster, read once from Prometheus after run R17 (2026-10-07),
its working set was 4.3 MiB with a peak of 9.6 MiB in its first minutes, and
Loki's gateway peaked at 19.8 MiB in 30 minutes; neither was read under load.
The rate
store was not measured on the cluster: outside one,
on the pinned image without its modules, over TLS, read-only and as user 999, it
held 12 MB when idle and a peak of 17 MB after 12,000 admissions from 40
connections on 3 tenants (2026-10-06), and its own ceiling, `maxmemory`, is
32 MB, half the limit. The next `make deploy` on the cluster should read the
pod's working set from cAdvisor as the services' were read.

The sign-in issuer (S021) is not in these figures: with
`MERIDIAN_IDENTITY=keycloak` it adds one pod that requests 700Mi and may use
1Gi, and `make up` refuses to start it under 2,500 MB of memory available (see
"The sign-in issuer").

## Deliberately not here yet

- TLS on the gateway: no step yet (the plan's follow-up backlog). The edge
  listens on loopback only.
- TLS to the sign-in issuer: the edge and the Claims API reach Keycloak over
  plain HTTP (S021, said under "The sign-in issuer"); S094 is the backlog row.
- Malware scanning of an uploaded file, and any delete or retention of one
  (S080): designed, not built. The two switches that store and serve files
  are off here (see "Files for a claim" above).
- `enforce` for Pod Security Admission on the `meridian` namespace, which
  has `warn` and `audit` at `restricted` since S019 (as `cert-manager` has
  since S063, and `observability` at `restricted`): a server-side dry run
  of `enforce=restricted` reported no violation on `meridian`, but a cold
  `make up` under it (CloudNativePG's init Job) was not tried; in the backlog.
- A second replica of any service, and so a budget that protects one:
  whether each service is safe to run twice is not measured (S027). The Model
  Gateway's rate windows no longer stop a second replica (they are in the rate
  store, S066), but its circuit breaker and its refusal throttles are still per
  process, and no load test has run two. A rolling update of the gateway under
  load, and a restart of the store under load, were not tried on the cluster.
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
