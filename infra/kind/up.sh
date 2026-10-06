#!/usr/bin/env bash
# Create the local platform on kind: `make up`. Safe to run again; it converges.
#   1. kind cluster "meridian" (only if absent), credentials in infra/kind/kubeconfig
#   2. namespaces, the database's NetworkPolicy, Envoy Gateway and the edge Gateway
#      cert-manager (its own approver off), approver-policy with the policies
#      that say who may ask for a certificate, and the CA that signs the
#      services' certificates
#   3. CloudNativePG operator and the platform-db cluster (PostgreSQL 17, pgvector),
#      the database "meridian" and its nine roles (the owner, six services, the
#      scheduled sweep's and the gateway's ledger upkeep's); their password
#      Secrets are created first, only if absent
#   4. Grafana admin Secret (only if absent), Grafana's Role (ConfigMaps in
#      observability, nothing else), kube-prometheus-stack, the Grafana
#      dashboards in infra/kind/dashboards (one ConfigMap each; one no longer
#      there is deleted), Meridian's alert rules (infra/kind/alerts, one
#      PrometheusRule), a ServiceMonitor for cert-manager's metrics, Tempo,
#      Loki, OpenTelemetry Collector
# Every version is pinned in pins.env.
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

# install_release NAME NAMESPACE CHART VERSION REPO VALUES_FILE [helm args...]
# REPO is empty for an OCI chart. Helm's output is shown only when it fails.
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
    die "helm release ${name} failed"
  fi
  log "release ${name} ${version} ready in ${namespace}"
}

check_prerequisites() {
  log "checking prerequisites"
  need_tools docker kind kubectl helm openssl jq
  require_local_docker
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running; start Docker Desktop"
  local kind_version
  kind_version="$(kind version | awk '{print $2}')"
  [[ "${kind_version}" == "${KIND_VERSION}" ]] ||
    log "warning: kind is ${kind_version}; the node image in pins.env is for ${KIND_VERSION}"
}

create_cluster() {
  if cluster_exists; then
    log "kind cluster ${CLUSTER_NAME} exists"
    # Refresh the credentials file (missing or stale); still nothing global.
    kind export kubeconfig --name "${CLUSTER_NAME}" --kubeconfig "${KUBECONFIG_FILE}"
    return
  fi
  log "creating kind cluster ${CLUSTER_NAME} (first run pulls the node image)"
  kind create cluster --name "${CLUSTER_NAME}" --image "${KIND_NODE_IMAGE}" \
    --config "${KIND_DIR}/cluster.yaml" --kubeconfig "${KUBECONFIG_FILE}" --wait 120s
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

log "network: the database's NetworkPolicy (before the database exists)"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/platform-db-networkpolicy.yaml" >/dev/null

log "edge: Envoy Gateway"
install_release envoy-gateway envoy-gateway-system "${ENVOY_GATEWAY_CHART}" \
  "${ENVOY_GATEWAY_VERSION}" "" envoy-gateway.yaml
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/gateway.yaml" >/dev/null

log "identity: cert-manager, who may ask for a certificate, and the CA for the services"
install_release cert-manager cert-manager "${CERT_MANAGER_CHART}" \
  "${CERT_MANAGER_VERSION}" "${CERT_MANAGER_REPO}" cert-manager.yaml
# cert-manager's own approver is off (values/cert-manager.yaml), so nothing is
# approved until approver-policy and its policies are there. On a cluster where
# cert-manager already ran with its approver on, this order turns the approver
# off first and brings the policies later: the certificates already issued are
# not touched, and a request made in between waits and is then decided. Later
# can be minutes (Helm's wait for approver-policy, then the apply's retries).
install_release approver-policy cert-manager "${APPROVER_POLICY_CHART}" \
  "${APPROVER_POLICY_VERSION}" "${CERT_MANAGER_REPO}" approver-policy.yaml
apply_certificate_policy
# The policies must be Ready before the CA is requested, or its request would
# find none that is appropriate and wait.
kctl wait --for=condition=Ready certificaterequestpolicy/meridian-services \
  certificaterequestpolicy/meridian-services-ca \
  certificaterequestpolicy/meridian-deny-unlisted --timeout=2m >/dev/null ||
  die "the certificate policies were not Ready in 2m: read the Ready condition of each (kubectl get certificaterequestpolicy -o yaml) and approver-policy's pod (kubectl -n cert-manager get pods; logs deploy/cert-manager-approver-policy)"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/service-ca.yaml" >/dev/null
# Helm returns when cert-manager runs (its startupapicheck hook has proved the
# webhook answers); the issuer is Ready once the CA certificate is issued and
# its Secret holds the key.
kctl wait --for=condition=Ready clusterissuer/meridian-services \
  --timeout=5m >/dev/null ||
  die "the issuer meridian-services was not Ready in 5m: read the CertificateRequest of the Certificate meridian-services-ca in cert-manager (kubectl -n cert-manager get certificaterequest; describe it) for its Approved or Denied condition, and the Certificate's events"

log "database: CloudNativePG operator"
install_release cnpg cnpg-system "${CNPG_OPERATOR_CHART}" "${CNPG_OPERATOR_VERSION}" \
  "${CNPG_REPO}" cnpg.yaml
ensure_database_secrets
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
install_release kube-prometheus-stack observability "${PROMETHEUS_STACK_CHART}" \
  "${PROMETHEUS_STACK_VERSION}" "${PROMETHEUS_STACK_REPO}" kube-prometheus-stack.yaml
kctl -n observability wait --for=condition=Available \
  prometheus/kube-prometheus-stack-prometheus --timeout=10m >/dev/null
apply_dashboards
log "observability: Meridian's alert rules"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/alerts/meridian.yaml" >/dev/null
log "observability: Prometheus scrapes cert-manager's metrics"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/cert-manager-metrics.yaml" >/dev/null
log "observability: Tempo"
install_release tempo observability "${TEMPO_CHART}" "${TEMPO_VERSION}" \
  "${GRAFANA_COMMUNITY_REPO}" tempo.yaml
log "observability: Loki"
install_release loki observability "${LOKI_CHART}" "${LOKI_VERSION}" \
  "${GRAFANA_COMMUNITY_REPO}" loki.yaml
log "observability: OpenTelemetry Collector"
install_release otel-collector observability "${OTEL_COLLECTOR_CHART}" \
  "${OTEL_COLLECTOR_VERSION}" "${OTEL_REPO}" otel-collector.yaml \
  --set "image.repository=${OTEL_COLLECTOR_IMAGE_REPOSITORY}" \
  --set "image.tag=${OTEL_COLLECTOR_IMAGE_TAG}" \
  --set "image.digest=${OTEL_COLLECTOR_IMAGE_DIGEST}"

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

log "done. Next: make smoke | make grafana | export KUBECONFIG=${KUBECONFIG_FILE}"
