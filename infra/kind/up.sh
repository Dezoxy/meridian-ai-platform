#!/usr/bin/env bash
# Create the local platform on kind: `make up`. Safe to run again on a cluster
# this script made; it converges. A cluster made before the CloudNativePG operator
# moved into `meridian` (S072, contract C) holds a namespace this script no longer
# makes: it refuses it before it changes anything, and says to run `make down`
# and then `make up` (that destroys the kind cluster and its database).
#   1. kind cluster "meridian" (only if absent), credentials in infra/kind/kubeconfig
#   2. namespaces (with Pod Security labels), the NetworkPolicies, Envoy Gateway and
#      the edge Gateway: the database's, the CloudNativePG operator's,
#      cert-manager's, observability's and Envoy Gateway's (S072, contract N:
#      denied by default, the listener's port the one rule without a peer;
#      each applied with the API server's address, read from the `kubernetes`
#      EndpointSlice in `default` on every run, so a cluster whose node got
#      another address is repaired by running this again),
#      the one for smoke's telemetrygen Jobs, the one that gives smoke's probe
#      pod its egress to the rate store (S066) and the one of the log agent's
#      namespace `logging`, all before the releases they guard,
#      cert-manager (its own approver off), approver-policy with the policies
#      that say who may ask for a certificate, the CA that signs the
#      services' certificates, and the CA of its own in `observability` that
#      signs the collector's certificate (its public certificate goes into the
#      ConfigMap telemetry-ca in `meridian` and in `logging`, on every run)
#   3. CloudNativePG operator (in `meridian`, with a Role there and no ClusterRole
#      over every namespace: S072, contract C; the comment above its install says
#      how to take that out) and the platform-db cluster (PostgreSQL 17, pgvector),
#      the database "meridian" and its eleven roles (the owner, six services, the
#      scheduled sweep's, the gateway's ledger upkeep's, the policy seed's and
#      the knowledge ingestion's); their password Secrets are created first,
#      only if absent, and so is the Secret of the rate store (S066, T-45):
#      the gateway's address in Redis and the ACL file of the store, made once
#      and never overwritten (the store itself runs in the Meridian release,
#      `make deploy`)
#   4. Grafana admin Secret (only if absent), Grafana's Role (ConfigMaps in
#      observability, nothing else), kube-prometheus-stack, Prometheus's gateway
#      (S072, contract M4: an nginx of this repository's own, with the
#      NetworkPolicies that close Prometheus's port to every other pod; the
#      OTLP receiver's path needs the collector's client certificate), the Grafana
#      dashboards in infra/kind/dashboards (one ConfigMap each; one no longer
#      there is deleted), Meridian's alert rules (infra/kind/alerts, one
#      PrometheusRule), a ServiceMonitor for cert-manager's metrics, Tempo,
#      Loki, OpenTelemetry Collector, and (S064) the log agent: a second release
#      of the collector's chart, the contrib build, as a DaemonSet in `logging`
#      that sends the output of `meridian`'s pods to the collector
# Every version is pinned in pins.env.
# Who holds the cluster (S075, common.sh): on a cluster that exists, another
# holder stops this before it changes anything unless TAKE_CLUSTER=1; the record
# is written with state `changing` as soon as the cluster answers and the check has
# passed, and with state `ok` as the last step, when the run ended well, so a run
# that fails or is interrupted leaves `changing`.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly HELM_TIMEOUT=10m
readonly ROLES_TIMEOUT=300
readonly ROLES_INTERVAL=3
readonly POLICY_TIMEOUT=120
readonly POLICY_INTERVAL=3
# platform-db-rw is the read-write Service; its name is in the server
# certificate, so verify-full checks it. The CA reaches each pod at this path.
readonly DATABASE_HOST=platform-db-rw.meridian.svc
readonly DATABASE_CA_PATH=/etc/meridian/db-ca/ca.crt
# The rate store (S066): the Secret that holds the gateway's address and the
# store's ACL file. The host is the DNS name of the store's Certificate, which the
# gateway verifies (the chart's, rate-store.<namespace>.svc); the port is the
# store's. The user is the gateway's; its key pattern is the limiter's own prefix
# (meridian.platform.gateway.ratelimit_redis.DEFAULT_PREFIX); its commands are
# exactly those the gateway's connection and script send (HELLO with the
# credentials, EVALSHA, SCRIPT LOAD when the server lost the script, and the five
# the script runs), read from Redis 8.10.2 under that user: no CLIENT command, no
# standalone AUTH, no EVAL. `+script|load` allows the one subcommand alone. A
# test holds this list equal to what the code sends.
readonly RATE_STORE_SECRET=rate-store-credentials
readonly RATE_STORE_HOST=rate-store.meridian.svc
readonly RATE_STORE_PORT=6379
readonly RATE_STORE_USER=gateway
readonly RATE_STORE_KEY_PATTERN='~meridian:rate:*'
readonly RATE_STORE_COMMANDS='+evalsha +script|load +time +zremrangebyscore +zrange +zadd +pexpire +hello'
# The probes' user (the chart's two probes ping as it and pass only on PONG): no
# password (`nopass`), no key, no channel and exactly PING, so it can do nothing
# but ask, and only a client that holds a certificate of the services' CA and has
# a network path reaches it. It lets the kubelet see a store that is frozen by a
# script that never ends (BUSY), which an unauthenticated PING cannot tell.
readonly RATE_STORE_PROBE_USER=probe
readonly RATE_STORE_PROBE_COMMANDS='+ping'
# The database's NetworkPolicy and the text in it that stands for the API
# server's addresses (S063). The placeholder is not a CIDR, so the API server
# refuses the file as it stands; a test keeps this string equal to the file's.
readonly DATABASE_POLICY_FILE="${KIND_DIR}/manifests/platform-db-networkpolicy.yaml"
# The CloudNativePG operator's own policy: it runs in `meridian` (S072, contract
# C), where the chart's default-deny selects its pod. The one egress rule for TCP
# 6443 takes the same placeholder; its webhook port has no ingress rule.
readonly CNPG_OPERATOR_POLICY_FILE="${KIND_DIR}/manifests/cnpg-operator-networkpolicy.yaml"
# cert-manager's policies take the same placeholder, on the one egress rule for
# TCP 6443 (S063, contract FB); the webhooks' port 10250 has no ingress rule.
readonly CERT_MANAGER_POLICY_FILE="${KIND_DIR}/manifests/cert-manager-networkpolicy.yaml"
# observability's takes it on the one egress rule for the node's 6443 and 10250,
# the API server and the kubelet, which are one address on kind (S072, contract E).
readonly OBSERVABILITY_POLICY_FILE="${KIND_DIR}/manifests/observability-networkpolicy.yaml"
# The policies of Loki's pods and its gateway's (S072, contract M3b): no placeholder,
# applied just before Loki's release and not at the start of the run.
readonly LOKI_POLICY_FILE="${KIND_DIR}/manifests/observability-loki-networkpolicy.yaml"
# The policies of Prometheus's port and its gateway's, and the gateway itself (S072,
# contract M4): no placeholder for an address, applied right after the stack's
# release and not at the start of the run (the collector's and Grafana's egress rules
# towards the gateway are in the file applied at the start, which is what opens the
# window of a warm run: see release_failure_note). The gateway's file holds four
# placeholders (its image, its own digest, the authority's certificate's digest, the
# Prometheus Service's cluster address) that prometheus_gateway_manifest fills in.
readonly PROMETHEUS_POLICY_FILE="${KIND_DIR}/manifests/observability-prometheus-networkpolicy.yaml"
readonly PROMETHEUS_GATEWAY_FILE="${KIND_DIR}/manifests/observability-prometheus-gateway.yaml"
readonly PROMETHEUS_GATEWAY_IMAGE_PLACEHOLDER=IMAGE-PLACEHOLDER
readonly PROMETHEUS_GATEWAY_MANIFEST_PLACEHOLDER=MANIFEST-SHA256-PLACEHOLDER
readonly PROMETHEUS_GATEWAY_CA_PLACEHOLDER=CA-SHA256-PLACEHOLDER
readonly PROMETHEUS_GATEWAY_ADDRESS_PLACEHOLDER=SERVICE-ADDRESS-PLACEHOLDER
# Envoy Gateway's namespace (S072, contract N): denied by default, and the one
# egress rule for TCP 6443 (the controller and its pre-install hook Job) takes the
# same placeholder; it is applied before the release, which the Job runs under.
readonly ENVOY_GATEWAY_POLICY_FILE="${KIND_DIR}/manifests/envoy-gateway-networkpolicy.yaml"
readonly API_SERVER_PEERS_PLACEHOLDER='to: [{ipBlock: {cidr: API-SERVER-ADDRESS/32}}]'

# What a failed release adds to its error, set by the caller just before a
# release whose failure leaves something half done (empty: nothing). Set once, right
# after observability's policies are applied (the warm window opens there), and
# emptied after the collector's release.
release_failure_note=""

# install_release NAME NAMESPACE CHART VERSION REPO VALUES_FILE [helm args...]
# REPO is empty for an OCI chart. Helm's output is shown only when it fails.
# What waits for the pods that a changed annotation rolls (S072, contract M4b):
# `--wait` below. Helm's readiness check of a Deployment looks at its NEW
# ReplicaSet and wants the expected number of ready pods there, and of a
# StatefulSet at its updated replicas (recalled from Helm's source, not seen on the
# cluster), so the gateway of Loki, Grafana, the collector and Tempo are each
# waited for by their own release. Prometheus's gateway is no release: its apply
# waits for `rollout status` (apply_prometheus_gateway).
install_release() {
  local name=$1 namespace=$2 chart=$3 version=$4 repo=$5 values=$6
  shift 6
  local repo_args=()
  [[ -n "${repo}" ]] && repo_args=(--repo "${repo}")
  local out
  if ! out="$(helmc upgrade --install "${name}" "${chart}" ${repo_args[@]+"${repo_args[@]}"} \
    --version "${version}" --namespace "${namespace}" \
    --values "${KIND_DIR}/values/${values}" "$@" \
    --wait --timeout "${HELM_TIMEOUT}" 2>&1)"; then
    printf '%s\n' "${out}" >&2
    die "helm release ${name} failed${release_failure_note:+: ${release_failure_note}}"
  fi
  log "release ${name} ${version} ready in ${namespace}"
}

check_prerequisites() {
  log "checking prerequisites"
  need_tools docker kind kubectl helm openssl jq timeout
  require_local_docker
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running; start Docker Desktop"
  local kind_version
  kind_version="$(kind version | awk '{print $2}')"
  [[ "${kind_version}" == "${KIND_VERSION}" ]] ||
    log "warning: kind is ${kind_version}; the node image in pins.env is for ${KIND_VERSION}"
}

# The namespace of the operator's old layout, which is history (S072, contract C):
# the CloudNativePG operator was released into a namespace of its own, and a
# cluster made then still holds it. Nothing here makes it; this script only asks
# whether it is there, to refuse such a cluster before it changes anything.
readonly OLD_OPERATOR_NAMESPACE=cnpg-system

# refuse_the_old_operator_layout: stop, with nothing changed, on a cluster made
# before the operator moved into `meridian`. The operator's release there owns the
# CRDs, ClusterRoles and webhook configurations that a release in `meridian`
# cannot take over (Helm's ownership check), and by then this script would have
# applied the new policies, so the old operator would already have lost the
# database's status port. A read that fails is not "the namespace is not there":
# it stops the run too.
refuse_the_old_operator_layout() {
  local found
  found="$(kctl get namespace "${OLD_OPERATOR_NAMESPACE}" -o name --ignore-not-found)" ||
    die "could not read whether the namespace ${OLD_OPERATOR_NAMESPACE} exists (kubectl's error is above); that is not 'it does not exist', so the run stops here and nothing was changed: run make up again once the API server answers"
  [[ -z "${found}" ]] ||
    die "the namespace ${OLD_OPERATOR_NAMESPACE} exists: this cluster was made before the CloudNativePG operator moved into meridian (S072), and make up cannot bring it to the new layout: the operator's release owns cluster-scoped objects (CRDs, ClusterRoles, webhook configurations) that a release in meridian cannot take over, and Helm would refuse it only after this run had applied the new policies and cut the old operator off from the database; nothing was changed. On a disposable cluster, run 'make down' and then 'make up': that destroys the kind cluster and its database, which holds the only copy of the audit log on kind, so never do it to clear a fault nobody has looked at"
}

create_cluster() {
  if cluster_exists; then
    log "kind cluster ${CLUSTER_NAME} exists"
    # Refresh the credentials file (missing or stale); still nothing global.
    kind export kubeconfig --name "${CLUSTER_NAME}" --kubeconfig "${KUBECONFIG_FILE}"
    # Who holds it (S075): before anything below changes the cluster.
    check_cluster_holder "make up"
    # A cluster made before the operator moved: before the first write below.
    refuse_the_old_operator_layout
    # From here on a failed run leaves the record saying `changing`.
    record_cluster_holder changing
    return
  fi
  log "creating kind cluster ${CLUSTER_NAME} (first run pulls the node image)"
  kind create cluster --name "${CLUSTER_NAME}" --image "${KIND_NODE_IMAGE}" \
    --config "${KIND_DIR}/cluster.yaml" --kubeconfig "${KUBECONFIG_FILE}" --wait 120s
  # The cluster answers now: nobody held it, and this run does.
  record_cluster_holder changing
}

# api_server_policy_manifest FILE PEERS: the policy file FILE with PEERS (the
# text of a flow-style list of ipBlocks) in place of the placeholder, on stdout.
# Stops when the file does not hold the placeholder exactly once: a file that
# lost it would be applied as it stands, and one that holds it twice would be
# half filled. The text is cut and joined with bash's own expansions, not sed or
# a pattern, so nothing in PEERS can be read as an expression.
api_server_policy_manifest() {
  local file=$1 peers=$2 manifest before after
  manifest="$(<"${file}")"
  [[ "${manifest}" == *"${API_SERVER_PEERS_PLACEHOLDER}"* ]] ||
    die "${file} does not hold the placeholder ${API_SERVER_PEERS_PLACEHOLDER}"
  before="${manifest%%"${API_SERVER_PEERS_PLACEHOLDER}"*}"
  after="${manifest#*"${API_SERVER_PEERS_PLACEHOLDER}"}"
  [[ "${after}" != *"${API_SERVER_PEERS_PLACEHOLDER}"* ]] ||
    die "${file} holds the placeholder ${API_SERVER_PEERS_PLACEHOLDER} more than once"
  printf '%s%s%s\n' "${before}" "to: [${peers}]" "${after}"
}

# apply_api_server_policy FILE WHOSE [PORTS]: apply the NetworkPolicy file FILE
# with the API server's address (S063), on every run: a cluster whose node was
# given another address by a Docker restart is repaired by running `make up`
# again. WHOSE ("the database's", "cert-manager's") is what the messages call the
# policy; PORTS is what its one rule admits at that address, for the log line
# ("TCP 6443" unless the call says otherwise). The address is read and checked
# first (read_api_server_addresses, common.sh), so a bad answer stops here with
# the policy as it was, and the manifest is rendered whole before kubectl sees
# it. The database's file, cert-manager's and observability's are applied
# through this one function.
apply_api_server_policy() {
  local file=$1 whose=$2 ports="${3:-TCP 6443}" address peers="" manifest
  read_api_server_addresses ||
    die "${api_server_problem}; ${whose} NetworkPolicy was not changed"
  while IFS= read -r address; do
    peers+="${peers:+, }{ipBlock: {cidr: ${address}/32}}"
  done <<<"${api_server_addresses}"
  manifest="$(api_server_policy_manifest "${file}" "${peers}")" || exit 1
  kctl apply --server-side --force-conflicts -f - <<<"${manifest}" >/dev/null
  log "network: ${whose} pods may reach ${ports} at $(paste -sd ',' - <<<"${api_server_addresses}") alone"
}

# Apply the policies for approver-policy, trying again until the API server
# accepts them. Helm's --wait returns when the pod is Ready (its /readyz, port
# 6060), which on the first run came eight seconds after the container started
# and before its webhook (port 10250, failurePolicy: Fail) answered: all three
# policies were refused with "failed calling webhook "policy.cert-manager.io":
# ... connection refused". Server-side apply is idempotent, so a retry after a
# partial apply is safe. kubectl's error is printed only when the time is up.
apply_certificate_policy() {
  local deadline=$((SECONDS + POLICY_TIMEOUT)) out
  until out="$(kctl apply --server-side --force-conflicts \
    -f "${KIND_DIR}/manifests/certificate-policy.yaml" 2>&1)"; do
    ((SECONDS < deadline)) ||
      die "approver-policy's webhook did not accept the policies in ${POLICY_TIMEOUT}s (look with: kubectl -n cert-manager get pods, and the logs of the approver-policy pod); kubectl said: ${out}"
    sleep "${POLICY_INTERVAL}"
  done
}

# Publish the public certificate of the collector's authority (S063) as the
# ConfigMap telemetry-ca (key ca.crt) in `meridian`, for the services to mount
# and trust it, (S064) in `logging`, for the log agent, and (S072, contract M3) in
# `observability`, which Grafana's environment reads to trust Loki's gateway and
# (contract M4) Prometheus's
# (a public certificate in a ConfigMap, so Grafana never holds a Secret of
# the authority's). Only the field
# tls.crt of the authority's Secret is read (a jsonpath; never the object and
# never tls.key), once, the text is checked to be a certificate and not to hold
# a key, and nothing is printed. Server-side apply, on every run: a renewed
# authority reaches the ConfigMaps at the next `make up`, and a rerun changes
# nothing when the certificate is the same. The services read the mounted file
# again at each new connection; the log agent's exporter is not known to, so
# after a renewal of the authority restart its DaemonSet (the runbook
# certificate-expiry.md covers the services).
publish_telemetry_ca() {
  local pem namespace
  pem="$(kctl -n observability get secret telemetry-ca \
    -o 'jsonpath={.data.tls\.crt}' | base64 -d)" ||
    die "could not read tls.crt of the Secret telemetry-ca in observability (is the Certificate telemetry-ca Ready? kubectl -n observability get certificate)"
  [[ "${pem}" == "-----BEGIN CERTIFICATE-----"* && "${pem}" != *"PRIVATE KEY"* ]] ||
    die "tls.crt of the Secret telemetry-ca in observability does not hold a certificate; the ConfigMap telemetry-ca was not changed"
  for namespace in meridian logging observability; do
    kctl -n "${namespace}" create configmap telemetry-ca --from-literal=ca.crt="${pem}" \
      --dry-run=client -o json |
      jq '.metadata.labels = {"app.kubernetes.io/part-of": "meridian"}
        | del(.metadata.creationTimestamp)' |
      kctl -n "${namespace}" apply --server-side --force-conflicts -f - >/dev/null
  done
  log "telemetry: the authority's public certificate is the ConfigMap telemetry-ca in meridian, in logging and in observability"
}

# The SHA-256 of one PUBLIC certificate field of a Secret or a ConfigMap in
# `observability`, printed as hex: object_fingerprint secret loki-gateway-tls
# 'ca\.crt'. A pod that reads a certificate file only when it starts (Tempo's
# receiver, nginx's client CA, Grafana's environment variable) is given this as
# a pod annotation, so a `make up` after a renewal changes the pod template and
# the pod is rolled; without it the pod would keep the file it loaded until
# something else restarted it (S072, contract M3b). Only tls.crt and ca.crt are
# ever named here, never tls.key; nothing is printed but the digest.
object_fingerprint() {
  local kind=$1 name=$2 field=$3 value
  value="$(kctl -n observability get "${kind}" "${name}" -o "jsonpath={.data.${field}}")" ||
    die "could not read ${field} of the ${kind} ${name} in observability (kubectl -n observability get ${kind} ${name})"
  [[ -n "${value}" ]] ||
    die "the ${kind} ${name} in observability holds no ${field//\\/}: its Certificate is not Ready, or the field is not there"
  if [[ "${kind}" == secret ]]; then
    value="$(printf '%s' "${value}" | base64 -d)" ||
      die "the ${field//\\/} of the ${kind} ${name} in observability is not base64"
  fi
  printf '%s' "${value}" | sha256sum | cut -d' ' -f1
}

# fill_placeholder TEXT PLACEHOLDER VALUE: TEXT with VALUE in place of PLACEHOLDER,
# on stdout. Stops when TEXT does not hold PLACEHOLDER exactly once: a manifest that
# lost one would be applied with the placeholder as it stands, and one that holds it
# twice would be half filled. Cut and joined with bash's own expansions, not sed or
# a pattern, so nothing in VALUE can be read as an expression.
fill_placeholder() {
  local text=$1 placeholder=$2 value=$3 before after
  [[ "${text}" == *"${placeholder}"* ]] ||
    die "the gateway's manifest does not hold the placeholder ${placeholder}"
  before="${text%%"${placeholder}"*}"
  after="${text#*"${placeholder}"}"
  [[ "${after}" != *"${placeholder}"* ]] ||
    die "the gateway's manifest holds the placeholder ${placeholder} more than once"
  printf '%s%s%s' "${before}" "${value}" "${after}"
}

# prometheus_service_address: the cluster address of the stack's Prometheus Service,
# on stdout (S072, contract M4b). nginx resolves the Service's name once, when it
# starts, so a Service that was made again (a stack uninstalled and installed in
# place) has an address the running gateway does not know. The address is a pod
# annotation: a `make up` after that changes the pod template and the pod is
# rolled. Stops on an empty answer (a headless Service has none: "None" is not an
# address either).
prometheus_service_address() {
  local address
  address="$(kctl -n observability get service kube-prometheus-stack-prometheus \
    -o 'jsonpath={.spec.clusterIP}')" ||
    die "could not read the cluster address of the Service kube-prometheus-stack-prometheus in observability (kubectl -n observability get service)"
  [[ "${address}" =~ ^[0-9a-fA-F.:]+$ ]] ||
    die "the Service kube-prometheus-stack-prometheus in observability has no cluster address (read: '${address}'); the gateway's upstream is that Service"
  printf '%s' "${address}"
}

# prometheus_gateway_manifest FILE IMAGE CA_SHA ADDRESS: the gateway's manifest FILE
# with its four placeholders filled in, on stdout (S072, contracts M4 and M4b):
# IMAGE is the whole reference of the image (name:tag@digest), CA_SHA the fingerprint
# of the authority's certificate, ADDRESS the Prometheus Service's cluster address,
# and the SHA-256 of FILE as it stands, placeholders and all, goes where the file's
# own digest is wanted. The last three are pod annotations: a changed configuration
# (it is in the file), a renewed authority or a Service made again changes the pod
# template, and the pod is rolled; nginx reads neither the client CA, its
# configuration nor the upstream's address again by itself. Stops when a placeholder
# word is still in the text after the fills: a placeholder added to the file and not
# filled here would be applied as it stands.
prometheus_gateway_manifest() {
  local file=$1 image=$2 ca_sha=$3 address=$4 manifest file_sha
  file_sha="$(sha256sum "${file}" | cut -d' ' -f1)"
  manifest="$(<"${file}")"
  manifest="$(fill_placeholder "${manifest}" "${PROMETHEUS_GATEWAY_IMAGE_PLACEHOLDER}" "${image}")" || exit 1
  manifest="$(fill_placeholder "${manifest}" "${PROMETHEUS_GATEWAY_MANIFEST_PLACEHOLDER}" "${file_sha}")" || exit 1
  manifest="$(fill_placeholder "${manifest}" "${PROMETHEUS_GATEWAY_CA_PLACEHOLDER}" "${ca_sha}")" || exit 1
  manifest="$(fill_placeholder "${manifest}" "${PROMETHEUS_GATEWAY_ADDRESS_PLACEHOLDER}" "${address}")" || exit 1
  [[ "${manifest}" != *PLACEHOLDER* ]] ||
    die "the gateway's manifest still holds a placeholder word after the fills: a placeholder that prometheus_gateway_manifest does not fill"
  printf '%s\n' "${manifest}"
}

# apply_prometheus_gateway: the policies of Prometheus's port and of the gateway,
# then the gateway (S072, contract M4), and wait until ITS ROLLOUT is done. Called
# right after the stack's release, which points Grafana's Prometheus datasource at
# the gateway: Grafana reads Prometheus again when this returns. The policies come
# first, so Prometheus's port is closed to Grafana and the collector only when the
# gateway that replaces them is about to exist, and the manifest is built (and
# checked) BEFORE the policies are applied, so a manifest that lost a placeholder
# stops the run with nothing closed. The window itself opened earlier: the
# collector's and Grafana's egress rules towards the gateway are in the file applied
# at the start of the run, so on a warm cluster Grafana has been unable to read
# Prometheus (its datasource, until the stack's release, still named Prometheus's
# own port) and the old collector's metrics have timed out since that apply, not
# since the stack's release; release_failure_note, set right after that apply, says
# so. If this run stops before the collector's release the window stays open until a
# re-run of `make up` converges.
# The wait is `rollout status`, not `wait --for=condition=Available`: with one
# replica, one surge and none unavailable, the Deployment stays Available while the
# OLD pod serves, so that condition returns at once on a warm cluster even when the
# new pod (a changed configuration, a renewed authority) crash-loops. `rollout
# status` returns when the new ReplicaSet's pod is Ready and the old one is gone.
apply_prometheus_gateway() {
  local ca_sha image address manifest
  ca_sha="$(object_fingerprint secret prometheus-gateway-tls 'ca\.crt')"
  image="${NGINX_GATEWAY_IMAGE_REPOSITORY}:${NGINX_GATEWAY_IMAGE_TAG}@${NGINX_GATEWAY_IMAGE_DIGEST}"
  address="$(prometheus_service_address)" || exit 1
  manifest="$(prometheus_gateway_manifest "${PROMETHEUS_GATEWAY_FILE}" "${image}" "${ca_sha}" "${address}")" || exit 1
  log "observability: Prometheus's and its gateway's NetworkPolicies"
  kctl apply --server-side --force-conflicts -f "${PROMETHEUS_POLICY_FILE}" >/dev/null
  log "observability: Prometheus's gateway (nginx, TLS 1.3, a client certificate for the OTLP receiver's path)"
  kctl apply --server-side --force-conflicts -f - <<<"${manifest}" >/dev/null
  kctl -n observability rollout status deployment/prometheus-gateway \
    --timeout=5m >/dev/null ||
    die "Prometheus's gateway (deployment/prometheus-gateway in observability) did not finish rolling out in 5m (its new pod was not Ready): Grafana reads no Prometheus and the collector's metrics are refused and dropped until it is, and on a warm cluster the old pod may still serve the old configuration; look at its pods (kubectl -n observability get pods -l app.kubernetes.io/name=prometheus-gateway; describe the newest) and its log, then run make up again"
}

# Create the Grafana admin Secret once. The password is generated here, goes to
# kubectl on stdin, and is never a command-line argument and never printed.
ensure_grafana_secret() {
  if kctl -n observability get secret grafana-admin >/dev/null 2>&1; then
    log "secret grafana-admin exists"
    return
  fi
  log "creating secret grafana-admin"
  local password
  password="$(openssl rand -base64 24)"
  printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: grafana-admin\n  namespace: observability\ntype: Opaque\nstringData:\n  admin-user: admin\n  admin-password: "%s"\n' \
    "${password}" | kctl create -f - >/dev/null
}

# Create one basic-auth Secret per database role, each only if absent. They must
# exist before platform-db installs: CloudNativePG cannot reconcile a role whose
# Secret is missing. Each password is generated here (hex, so the URI needs no
# escaping), goes to kubectl on stdin and is never a command-line argument and
# never printed. Keys: username, password, uri (the shape of platform-db-app).
ensure_database_secrets() {
  { set +x; } 2>/dev/null # a `bash -x` run must not trace a password
  local role secret password
  for role in "${DATABASE_ROLES[@]}"; do
    secret="$(role_secret_name "${role}")"
    if kctl -n meridian get secret "${secret}" >/dev/null 2>&1; then
      log "secret ${secret} exists"
      continue
    fi
    log "creating secret ${secret}"
    password="$(openssl rand -hex 24)"
    printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: meridian\ntype: kubernetes.io/basic-auth\nstringData:\n  username: %s\n  password: "%s"\n  uri: "postgresql://%s:%s@%s:5432/meridian?sslmode=verify-full&sslrootcert=%s"\n' \
      "${secret}" "${role}" "${password}" "${role}" "${password}" "${DATABASE_HOST}" "${DATABASE_CA_PATH}" |
      kctl create -f - >/dev/null
  done
}

# Create the rate store's Secret once (S066). Two keys, `uri` (the gateway's
# address: rediss://<user>:<password>@<host>:<port>/0) and `users.acl` (Redis's
# ACL file: the `default` user off; one user for the gateway that is on, with
# the SHA-256 of the password and not the password, the key pattern of the
# limiter, no channel, and exactly the commands the gateway sends; and the
# probes' user, with no password, no key, no channel and exactly PING). The
# Secret carries the annotation RATE_STORE_ACL_ANNOTATION (common.sh): the
# SHA-256 of the ACL file with the password's hash masked, which `make deploy`
# compares with what this would write now. The password is 32 random bytes as 64
# lower-case hex digits, an alphabet that needs no percent-encoding, so the
# address is exact. It goes to kubectl on stdin and is never a command-line
# argument and never printed. A Secret that exists is kept: a changed ACL
# reaches a running cluster only by deleting the Secret and running `make up`
# again, then restarting the store and the gateway
# (docs/operations/runbooks/rate-store.md).
ensure_rate_store_secret() {
  # Tracing is off inside, and on again at each return when it was on: a `bash -x`
  # run must not trace a password, and the rest of `make up` should stay traced.
  local traced=0 password digest acl
  if [[ "$-" == *x* ]]; then traced=1; fi
  { set +x; } 2>/dev/null # a `bash -x` run must not trace a password
  if kctl -n meridian get secret "${RATE_STORE_SECRET}" >/dev/null 2>&1; then
    log "secret ${RATE_STORE_SECRET} exists"
    ((traced == 0)) || set -x
    return
  fi
  log "creating secret ${RATE_STORE_SECRET}"
  password="$(openssl rand -hex 32)"
  digest="$(printf '%s' "${password}" | openssl dgst -sha256 -r | awk '{print $1}')"
  [[ "${digest}" =~ ^[0-9a-f]{64}$ ]] || die "could not hash the rate store's password"
  acl="$(printf 'user default off\nuser %s on #%s %s resetchannels -@all %s\nuser %s on nopass resetkeys resetchannels -@all %s\n' \
    "${RATE_STORE_USER}" "${digest}" "${RATE_STORE_KEY_PATTERN}" "${RATE_STORE_COMMANDS}" \
    "${RATE_STORE_PROBE_USER}" "${RATE_STORE_PROBE_COMMANDS}")"
  printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: meridian\n  annotations:\n    %s: "%s"\ntype: Opaque\nstringData:\n  uri: "rediss://%s:%s@%s:%s/0"\n  users.acl: |\n%s\n' \
    "${RATE_STORE_SECRET}" "${RATE_STORE_ACL_ANNOTATION}" "$(printf '%s\n' "${acl}" | rate_store_acl_rules_hash)" \
    "${RATE_STORE_USER}" "${password}" "${RATE_STORE_HOST}" "${RATE_STORE_PORT}" \
    "    ${acl//$'\n'/$'\n'    }" |
    kctl create -f - >/dev/null
  ((traced == 0)) || set -x
}

# Wait until CloudNativePG reports every role reconciled (common.sh).
wait_for_database_roles() {
  local deadline=$((SECONDS + ROLES_TIMEOUT))
  while ((SECONDS < deadline)); do
    database_roles_reconciled && return 0
    sleep "${ROLES_INTERVAL}"
  done
  die "database roles were not reconciled in ${ROLES_TIMEOUT}s: $(kctl -n meridian get cluster platform-db -o json |
    jq -c '.status.managedRolesStatus.cannotReconcile // {}')"
}

# Each infra/kind/dashboards/*.json becomes a ConfigMap in observability that
# Grafana's dashboard sidecar loads (the label; the sidecar reads that namespace
# only, see the values file). Server-side apply, so a rerun converges. Every
# file is checked first, so a broken one stops the run before anything changes.
# Then a dashboard whose file is gone is deleted: only ConfigMaps labelled
# part-of=meridian are looked at, and the chart's dashboards carry no such label.
apply_dashboards() {
  local file name wanted="" stale count=0
  for file in "${KIND_DIR}"/dashboards/*.json; do
    [[ -e "${file}" ]] || continue
    jq empty "${file}" 2>/dev/null || die "${file} is not valid JSON"
  done
  for file in "${KIND_DIR}"/dashboards/*.json; do
    [[ -e "${file}" ]] || continue
    name="${file##*/}"
    wanted+="meridian-dashboard-${name%.json}"$'\n'
    kctl -n observability create configmap "meridian-dashboard-${name%.json}" \
      --from-file="${name}=${file}" --dry-run=client -o json |
      jq '.metadata.labels = {"grafana_dashboard": "1", "app.kubernetes.io/part-of": "meridian"}
        | del(.metadata.creationTimestamp)' |
      kctl -n observability apply --server-side --force-conflicts -f - >/dev/null
    count=$((count + 1))
  done
  ((count > 0)) || die "no dashboard (*.json) in ${KIND_DIR}/dashboards"
  local existing
  existing="$(kctl -n observability get configmap \
    -l grafana_dashboard=1,app.kubernetes.io/part-of=meridian -o name)" ||
    die "could not list the dashboard ConfigMaps in observability"
  while IFS= read -r stale; do
    stale="${stale#configmap/}"
    [[ -n "${stale}" ]] || continue
    grep -qxF -- "${stale}" <<<"${wanted}" && continue
    kctl -n observability delete configmap "${stale}" >/dev/null
    log "observability: dashboard ConfigMap ${stale} deleted (its file is gone)"
  done <<<"${existing}"
  log "observability: ${count} Grafana dashboard(s) applied"
}

check_prerequisites
create_cluster

log "namespaces"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/namespaces.yaml" >/dev/null

log "network: the database's NetworkPolicy, with the API server's address (before the database exists)"
apply_api_server_policy "${DATABASE_POLICY_FILE}" "the database's"

log "network: the CloudNativePG operator's NetworkPolicy, with the API server's address (before the operator exists)"
apply_api_server_policy "${CNPG_OPERATOR_POLICY_FILE}" "the CloudNativePG operator's"

log "network: cert-manager's NetworkPolicies, with the API server's address (before cert-manager is installed)"
apply_api_server_policy "${CERT_MANAGER_POLICY_FILE}" "cert-manager's"

log "network: observability's NetworkPolicies, with the node's address (before Prometheus, Tempo, Loki and the collector)"
apply_api_server_policy "${OBSERVABILITY_POLICY_FILE}" "observability's" "TCP 6443 and 10250"
# From this apply on, on a warm cluster the collector's and Grafana's egress rules
# name the two gateways and no longer the stores' own ports (S072, contracts M3 and
# M4): the old collector's exporters to Loki's and Prometheus's own ports time out
# (a refused write is not retried: those logs and metrics are lost) and Grafana's
# old datasources cannot reach them, until the stack's release and the collector's
# are in. A failure anywhere before the collector's release leaves that open, so
# every release's failure message says so, from here to the collector's (a test
# holds the two ends); the note ends with the collector's release below.
release_failure_note="telemetry stays refused and dropped, and Grafana's Prometheus and Loki reads fail, until a re-run of make up converges (observability's policies, which the collector's and Grafana's egress rules to the two gateways are in, are applied, and the collector's release has not run: its metrics and logs, which the gateways admit only with its client certificate, and its traces go nowhere, and the old collector's exporters still name the stores' own ports)"

log "network: Envoy Gateway's NetworkPolicies, with the node's address (before the controller, its hook Job and the proxy pods)"
apply_api_server_policy "${ENVOY_GATEWAY_POLICY_FILE}" "Envoy Gateway's controller and hook Job" "TCP 6443"

log "network: the NetworkPolicy of smoke's telemetrygen Jobs in meridian"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/smoke-networkpolicy.yaml" >/dev/null

log "network: the NetworkPolicy of smoke's probe pod for the rate store in meridian"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/smoke-rate-store-networkpolicy.yaml" >/dev/null

log "network: the log agent's NetworkPolicies in logging (before the agent)"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/logging-networkpolicy.yaml" >/dev/null

log "edge: Envoy Gateway"
install_release envoy-gateway envoy-gateway-system "${ENVOY_GATEWAY_CHART}" \
  "${ENVOY_GATEWAY_VERSION}" "" envoy-gateway.yaml \
  --set "global.images.envoyGateway.image=${ENVOY_GATEWAY_IMAGE_REPOSITORY}:${ENVOY_GATEWAY_IMAGE_TAG}@${ENVOY_GATEWAY_IMAGE_DIGEST}"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/gateway.yaml" >/dev/null

log "identity: cert-manager, who may ask for a certificate, and the CA for the services"
install_release cert-manager cert-manager "${CERT_MANAGER_CHART}" \
  "${CERT_MANAGER_VERSION}" "${CERT_MANAGER_REPO}" cert-manager.yaml \
  --set "image.tag=${CERT_MANAGER_CONTROLLER_IMAGE_TAG}" \
  --set "image.digest=${CERT_MANAGER_CONTROLLER_IMAGE_DIGEST}" \
  --set "webhook.image.tag=${CERT_MANAGER_WEBHOOK_IMAGE_TAG}" \
  --set "webhook.image.digest=${CERT_MANAGER_WEBHOOK_IMAGE_DIGEST}" \
  --set "cainjector.image.tag=${CERT_MANAGER_CAINJECTOR_IMAGE_TAG}" \
  --set "cainjector.image.digest=${CERT_MANAGER_CAINJECTOR_IMAGE_DIGEST}" \
  --set "startupapicheck.image.tag=${CERT_MANAGER_STARTUPAPICHECK_IMAGE_TAG}" \
  --set "startupapicheck.image.digest=${CERT_MANAGER_STARTUPAPICHECK_IMAGE_DIGEST}"
# cert-manager's own approver is off (values/cert-manager.yaml), so nothing is
# approved until approver-policy and its policies are there. On a cluster where
# cert-manager already ran with its approver on, this order turns the approver
# off first and brings the policies later: the certificates already issued are
# not touched, and a request made in between waits and is then decided. Later
# can be minutes (Helm's wait for approver-policy, then the apply's retries).
install_release approver-policy cert-manager "${APPROVER_POLICY_CHART}" \
  "${APPROVER_POLICY_VERSION}" "${CERT_MANAGER_REPO}" approver-policy.yaml \
  --set "image.tag=${APPROVER_POLICY_IMAGE_TAG}" \
  --set "image.digest=${APPROVER_POLICY_IMAGE_DIGEST}"
apply_certificate_policy
# The policies must be Ready before the CA is requested, or its request would
# find none that is appropriate and wait.
kctl wait --for=condition=Ready certificaterequestpolicy/meridian-services \
  certificaterequestpolicy/meridian-services-ca \
  certificaterequestpolicy/meridian-deny-unlisted \
  certificaterequestpolicy/telemetry-ca \
  certificaterequestpolicy/otel-collector \
  certificaterequestpolicy/otel-collector-client \
  certificaterequestpolicy/tempo-receiver \
  certificaterequestpolicy/loki-gateway \
  certificaterequestpolicy/prometheus-gateway --timeout=2m >/dev/null ||
  die "the certificate policies were not Ready in 2m: read the Ready condition of each (kubectl get certificaterequestpolicy -o yaml) and approver-policy's pod (kubectl -n cert-manager get pods; logs deploy/cert-manager-approver-policy)"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/service-ca.yaml" >/dev/null
# Helm returns when cert-manager runs (its startupapicheck hook has proved the
# webhook answers); the issuer is Ready once the CA certificate is issued and
# its Secret holds the key.
kctl wait --for=condition=Ready clusterissuer/meridian-services \
  --timeout=5m >/dev/null ||
  die "the issuer meridian-services was not Ready in 5m: read the CertificateRequest of the Certificate meridian-services-ca in cert-manager (kubectl -n cert-manager get certificaterequest; describe it) for its Approved or Denied condition, and the Certificate's events"

# The collector's own authority (S063, T-90): namespaced Issuers in
# observability, so no policy for the services' issuer changes. The collector's
# Certificate being Ready means the authority's was issued before it. The
# release of the collector, further on, mounts the Secrets they make: the server
# certificate's and, since S072 (contract M1), the client certificate's; Tempo's
# release mounts the receiver certificate's (contract M2), Loki's mounts the
# gateway's (contract M3) and Prometheus's gateway, applied after the stack's
# release, mounts its own (contract M4). All five are waited for here, before the
# first store is installed (a Secret that does not exist leaves the pod in
# ContainerCreating and stops `make up` at that release).
log "telemetry: the CA for the collector's certificate, in observability"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/telemetry-ca.yaml" >/dev/null
kctl -n observability wait --for=condition=Ready certificate/otel-collector \
  certificate/otel-collector-client certificate/tempo-receiver \
  certificate/loki-gateway certificate/prometheus-gateway --timeout=5m >/dev/null ||
  die "the Certificate otel-collector, otel-collector-client, tempo-receiver, loki-gateway or prometheus-gateway in observability was not Ready in 5m: read the CertificateRequests of the Certificates telemetry-ca, otel-collector, otel-collector-client, tempo-receiver, loki-gateway and prometheus-gateway (kubectl -n observability get certificaterequest; describe each) for their Approved or Denied condition, the Ready condition of the policies telemetry-ca, otel-collector, otel-collector-client, tempo-receiver, loki-gateway and prometheus-gateway (the add-on's pod logs say why one was not applied), and the Certificates' events"
publish_telemetry_ca

# The operator runs in `meridian`, with the chart's `config.clusterWide=false`
# (S072, contract C): its rules over Secrets, ConfigMaps, pods (and pods/exec) and
# roles are one Role in `meridian`, where the Cluster is, and no longer a
# ClusterRole over every namespace; the ClusterRole is left with nodes (read), the
# webhook configurations (get, patch) and image catalogs (read). One of the four
# cluster-wide writers of Secrets is gone, and no namespace of its own is made
# for it. Its pod is selected by its own policy (CNPG_OPERATOR_POLICY_FILE,
# applied above) and by the chart's default-deny, which `make deploy` adds.
#
# Fall-back, written before the first cold run: this change is implemented in
# files and tested without a cluster; it has not run on kind. If one cold `make
# up` does not bring the database up under the confined operator, take the change
# out again (the release back in a namespace of its own, without
# `config.clusterWide=false`, the policy file and its call removed, the database
# policy's rule for the operator back to a namespace and a pod selector) and
# record the operator's reach as accepted with "tried, and what failed". What
# "does not bring the database up" looks like: the install of platform-db ends
# with "helm release platform-db failed" and a webhook call refused or timed out
# (the operator's webhooks fail closed: its pod unreachable from the API server);
# or the wait for cluster/platform-db below ends after 10m with the Cluster not
# Ready; or the wait for the roles ends with cannotReconcile. The operator's log
# (the pod of the Deployment cnpg-cloudnative-pg in `meridian`) would say, for a
# policy that is too narrow, a timeout dialling the API server's address on 6443
# or "Instance Status Extraction Error: HTTP communication issue" (its call to
# the instance manager on 8000); for a Role that is too narrow, "forbidden" and
# the verb and resource it lacked.
# A cluster from before this change keeps the release in the namespace of its
# own, and Helm does not move a release: run `make down` (it deletes the kind
# cluster: only a disposable one, and never to clear a fault nobody has looked at)
# and then `make up`.
log "database: CloudNativePG operator (in meridian, with a Role there instead of rules over every namespace)"
install_release cnpg meridian "${CNPG_OPERATOR_CHART}" "${CNPG_OPERATOR_VERSION}" \
  "${CNPG_REPO}" cnpg.yaml \
  --set "config.clusterWide=false" \
  --set "image.tag=${CNPG_OPERATOR_IMAGE_TAG}@${CNPG_OPERATOR_IMAGE_DIGEST}"
ensure_database_secrets
ensure_rate_store_secret
log "database: platform-db (PostgreSQL 17, pgvector)"
install_release platform-db meridian "${CNPG_CLUSTER_CHART}" "${CNPG_CLUSTER_VERSION}" \
  "${CNPG_REPO}" platform-db.yaml --set "cluster.imageName=${POSTGRES_IMAGE}"
# Helm returns when the objects exist; wait for the database itself.
kctl -n meridian wait --for=condition=Ready cluster/platform-db --timeout=10m >/dev/null
kctl -n meridian wait --for=jsonpath='{.status.applied}'=true database/platform-db-app \
  --timeout=5m >/dev/null
# The roles first: the "meridian" database is owned by one of them.
wait_for_database_roles
kctl -n meridian wait --for=jsonpath='{.status.applied}'=true database/platform-db-meridian \
  --timeout=5m >/dev/null
log "platform-db is ready (database meridian, roles ${DATABASE_ROLES[*]})"

ensure_grafana_secret
log "observability: Grafana's Role (ConfigMaps in observability only)"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/grafana-rbac.yaml" >/dev/null
log "observability: Prometheus and Grafana"
# Grafana reads the authority's certificate from its environment at start (the
# ConfigMap telemetry-ca published above, grafana.envValueFrom): the annotation
# with its fingerprint rolls the pod when the authority is renewed (S072,
# contract M3b). The ConfigMap must be published BEFORE this release, or the pod
# cannot start (a test holds that order).
grafana_ca_sha="$(object_fingerprint configmap telemetry-ca 'ca\.crt')"
install_release kube-prometheus-stack observability "${PROMETHEUS_STACK_CHART}" \
  "${PROMETHEUS_STACK_VERSION}" "${PROMETHEUS_STACK_REPO}" kube-prometheus-stack.yaml \
  --set "prometheusOperator.image.tag=${PROMETHEUS_OPERATOR_IMAGE_TAG}" \
  --set "prometheusOperator.image.sha=${PROMETHEUS_OPERATOR_IMAGE_DIGEST#sha256:}" \
  --set "prometheusOperator.prometheusConfigReloader.image.tag=${PROMETHEUS_CONFIG_RELOADER_IMAGE_TAG}" \
  --set "prometheusOperator.prometheusConfigReloader.image.sha=${PROMETHEUS_CONFIG_RELOADER_IMAGE_DIGEST#sha256:}" \
  --set "prometheusOperator.admissionWebhooks.patch.image.tag=${KUBE_WEBHOOK_CERTGEN_IMAGE_TAG}" \
  --set "prometheusOperator.admissionWebhooks.patch.image.sha=${KUBE_WEBHOOK_CERTGEN_IMAGE_DIGEST#sha256:}" \
  --set "prometheus.prometheusSpec.image.tag=${PROMETHEUS_IMAGE_TAG}" \
  --set "prometheus.prometheusSpec.image.sha=${PROMETHEUS_IMAGE_DIGEST#sha256:}" \
  --set "kube-state-metrics.image.tag=${KUBE_STATE_METRICS_IMAGE_TAG}" \
  --set "kube-state-metrics.image.sha=${KUBE_STATE_METRICS_IMAGE_DIGEST}" \
  --set "grafana.image.tag=${GRAFANA_IMAGE_TAG}" \
  --set "grafana.image.sha=${GRAFANA_IMAGE_DIGEST#sha256:}" \
  --set "grafana.sidecar.image.tag=${GRAFANA_SIDECAR_IMAGE_TAG}" \
  --set "grafana.sidecar.image.sha=${GRAFANA_SIDECAR_IMAGE_DIGEST#sha256:}" \
  --set-string "grafana.podAnnotations.meridian-ca-sha256=${grafana_ca_sha}"
kctl -n observability wait --for=condition=Available \
  prometheus/kube-prometheus-stack-prometheus --timeout=10m >/dev/null
# Prometheus is written and read through its gateway (S072, contract M4): the
# release above pointed Grafana's datasource at it, so it comes up right away, with
# the policies that close Prometheus's own port to everything else. The collector's
# metrics exporter points at it from the collector's release, last.
apply_prometheus_gateway
apply_dashboards
apply_alert_rules
log "observability: Prometheus scrapes cert-manager's metrics"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/cert-manager-metrics.yaml" >/dev/null
log "observability: Tempo"
# Tempo's receiver asks the sender for a client certificate (S072, contract M2),
# and its Secret was waited for above. On a WARM cluster this release asks for a
# certificate about a minute before the collector's new release presents one
# (the collector is installed last), so the traces of that minute are refused and
# dropped; smoke's read-backs wait for the collector's release. On a first
# install no sender exists yet. Tempo reads its certificate and its client CA only
# when it starts, and the chart's receiver settings are not known to re-read them
# (a reload_interval is not established for Tempo 3.1.0, values/tempo.yaml), so
# the pod is given the fingerprints of both as annotations: a `make up` after a
# renewal rolls it.
tempo_cert_sha="$(object_fingerprint secret tempo-receiver-tls 'tls\.crt')"
tempo_ca_sha="$(object_fingerprint secret tempo-receiver-tls 'ca\.crt')"
install_release tempo observability "${TEMPO_CHART}" "${TEMPO_VERSION}" \
  "${GRAFANA_COMMUNITY_REPO}" tempo.yaml \
  --set "tempo.tag=${TEMPO_IMAGE_TAG}@${TEMPO_IMAGE_DIGEST}" \
  --set-string "podAnnotations.meridian-cert-sha256=${tempo_cert_sha}" \
  --set-string "podAnnotations.meridian-ca-sha256=${tempo_ca_sha}"
log "observability: Loki"
# Loki is written through its gateway, which asks for the collector's client
# certificate (S072, contract M3); its Secret, loki-gateway-tls, was waited for
# above, and the image pin of the gateway is NGINX_GATEWAY_IMAGE_* in pins.env (the
# same pin is Prometheus's gateway's image, applied further up).
# Loki's own policies and the gateway's are in a file of their own, applied here
# and not at the start of the run (S072, contract M3b), so Loki's port stays as it
# was until the pods that the policies are about are being replaced. What a warm
# cluster still loses, once, when it moves from Loki's own port to the gateway:
# the collector's and Grafana's egress rules (observability-networkpolicy.yaml,
# applied at the start) already point at the gateway, so the old collector's
# exporter to Loki's own port is cut at that apply, and Grafana's datasource (the
# stack's release, above) already points at the gateway, so its Loki reads fail
# until this release is Ready. The logs of that window are dropped, and smoke's
# read-backs wait for the collector's release, as for Tempo above. On a first
# install no sender exists. If this run stops before the collector's release the
# window stays open until a re-run of `make up` converges; the message says so.
# nginx re-reads the gateway's certificate at each handshake but its client CA
# only at start, so the pod is given the CA's fingerprint as an annotation.
log "observability: Loki's and its gateway's NetworkPolicies"
kctl apply --server-side --force-conflicts -f "${LOKI_POLICY_FILE}" >/dev/null
loki_ca_sha="$(object_fingerprint secret loki-gateway-tls 'ca\.crt')"
install_release loki observability "${LOKI_CHART}" "${LOKI_VERSION}" \
  "${GRAFANA_COMMUNITY_REPO}" loki.yaml \
  --set "loki.image.tag=${LOKI_IMAGE_TAG}" \
  --set "loki.image.digest=${LOKI_IMAGE_DIGEST}" \
  --set "gateway.image.registry=${NGINX_GATEWAY_IMAGE_REPOSITORY%%/*}" \
  --set "gateway.image.repository=${NGINX_GATEWAY_IMAGE_REPOSITORY#*/}" \
  --set "gateway.image.tag=${NGINX_GATEWAY_IMAGE_TAG}" \
  --set "gateway.image.digest=${NGINX_GATEWAY_IMAGE_DIGEST}" \
  --set-string "gateway.podAnnotations.meridian-ca-sha256=${loki_ca_sha}"
log "observability: OpenTelemetry Collector"
# The collector re-reads its client pair by itself, but only every five minutes
# (reload_interval), and the two gateways admit a write from ONE common name. When
# `make up` re-issued the client certificate with another subject (run R16: after
# the gateways began to check it), the old pod went on presenting the old
# certificate, was refused with a 403, and its writes of those minutes were lost
# (a 403 is not retried). So the pod is given the fingerprints of the client
# certificate and of the authority's certificate (the file that verifies the
# gateways and Tempo) as annotations, and a `make up` that changed either rolls
# it: the new pod starts with the new pair. A plain renewal that keeps the subject
# rolls it too, which costs the telemetry in flight and nothing else.
collector_client_sha="$(object_fingerprint secret otel-collector-client-tls 'tls\.crt')"
collector_ca_sha="$(object_fingerprint secret otel-collector-client-tls 'ca\.crt')"
install_release otel-collector observability "${OTEL_COLLECTOR_CHART}" \
  "${OTEL_COLLECTOR_VERSION}" "${OTEL_REPO}" otel-collector.yaml \
  --set "image.repository=${OTEL_COLLECTOR_IMAGE_REPOSITORY}" \
  --set "image.tag=${OTEL_COLLECTOR_IMAGE_TAG}" \
  --set "image.digest=${OTEL_COLLECTOR_IMAGE_DIGEST}" \
  --set-string "podAnnotations.meridian-client-cert-sha256=${collector_client_sha}" \
  --set-string "podAnnotations.meridian-ca-sha256=${collector_ca_sha}"
release_failure_note=""

# The log agent (S064) needs the collector's Service to send to and the
# ConfigMap telemetry-ca in `logging` (published above): both exist by now. Its
# pod is the one pod of this script's releases that mounts a host path.
log "logging: the log agent (OpenTelemetry Collector, contrib build, one pod per node)"
install_release log-agent logging "${OTEL_COLLECTOR_CHART}" \
  "${OTEL_COLLECTOR_VERSION}" "${OTEL_REPO}" log-agent.yaml \
  --set "image.repository=${LOG_AGENT_IMAGE_REPOSITORY}" \
  --set "image.tag=${LOG_AGENT_IMAGE_TAG}" \
  --set "image.digest=${LOG_AGENT_IMAGE_DIGEST}"

log "edge: waiting for the Gateway to be programmed"
# Envoy Gateway has left Programmed False for hours while the edge served
# (AddressNotAssigned, NoResources; README, "If make up was interrupted"), so
# the message says the edge may be fine and gives the remedy.
kctl -n envoy-gateway-system wait --for=condition=Programmed gateway/edge --timeout=5m >/dev/null ||
  die "the wait for the Gateway edge to be Programmed ended without the condition (it waits up to 5m; kubectl's own message above says whether the time ran out or the wait failed at once, for instance with not found). The edge may be serving all the same: Envoy Gateway has left this condition False for hours with the proxy pod ready (the reason is in kubectl -n envoy-gateway-system get gateway edge -o yaml). If the proxy pod is ready, do what infra/kind/README.md says under 'If make up was interrupted', and then run 'make up' again: kubectl -n envoy-gateway-system rollout restart deploy/envoy-gateway; kubectl -n envoy-gateway-system rollout status deploy/envoy-gateway; kubectl -n envoy-gateway-system annotate gateway edge meridian.local/reconcile-nudge=<the time now> --overwrite"
# Programmed does not mean the proxy pods are serving yet.
kctl -n envoy-gateway-system wait --for=condition=Available deployment \
  -l gateway.envoyproxy.io/owning-gateway-name=edge --timeout=5m >/dev/null ||
  die "the wait for the edge's proxy Deployment to be Available ended without the condition (it waits up to 5m; kubectl's own message above says whether the time ran out or the wait failed at once, for instance with not found): look at its pods (kubectl -n envoy-gateway-system get pods -l gateway.envoyproxy.io/owning-gateway-name=edge; describe the one that is not ready) and at the controller's log (kubectl -n envoy-gateway-system logs deploy/envoy-gateway)"

# Every wait above ended well: only now is the cluster claimed (S075). A run that
# stopped earlier leaves the record as it was.
record_cluster_holder ok
log "done. Next: make smoke | make grafana | export KUBECONFIG=${KUBECONFIG_FILE}"
