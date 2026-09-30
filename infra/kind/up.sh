#!/usr/bin/env bash
# Create the local platform on kind: `make up`. Safe to run again; it converges.
#   1. kind cluster "meridian" (only if absent), credentials in infra/kind/kubeconfig
#   2. namespaces, Envoy Gateway and the edge Gateway
#   3. CloudNativePG operator and the platform-db cluster (PostgreSQL 17, pgvector)
#   4. Grafana admin Secret (only if absent), kube-prometheus-stack, Tempo, Loki,
#      OpenTelemetry Collector
# Every version is pinned in pins.env.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly HELM_TIMEOUT=10m

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
  need_tools docker kind kubectl helm openssl
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

check_prerequisites
create_cluster

log "namespaces"
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/namespaces.yaml" >/dev/null

log "edge: Envoy Gateway"
install_release envoy-gateway envoy-gateway-system "${ENVOY_GATEWAY_CHART}" \
  "${ENVOY_GATEWAY_VERSION}" "" envoy-gateway.yaml
kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/gateway.yaml" >/dev/null

log "database: CloudNativePG operator"
install_release cnpg cnpg-system "${CNPG_OPERATOR_CHART}" "${CNPG_OPERATOR_VERSION}" \
  "${CNPG_REPO}" cnpg.yaml
log "database: platform-db (PostgreSQL 17, pgvector)"
install_release platform-db meridian "${CNPG_CLUSTER_CHART}" "${CNPG_CLUSTER_VERSION}" \
  "${CNPG_REPO}" platform-db.yaml --set "cluster.imageName=${POSTGRES_IMAGE}"
# Helm returns when the objects exist; wait for the database itself.
kctl -n meridian wait --for=condition=Ready cluster/platform-db --timeout=10m >/dev/null
kctl -n meridian wait --for=jsonpath='{.status.applied}'=true database/platform-db-app \
  --timeout=5m >/dev/null
log "platform-db is ready"

ensure_grafana_secret
log "observability: Prometheus and Grafana"
install_release kube-prometheus-stack observability "${PROMETHEUS_STACK_CHART}" \
  "${PROMETHEUS_STACK_VERSION}" "${PROMETHEUS_STACK_REPO}" kube-prometheus-stack.yaml
kctl -n observability wait --for=condition=Available \
  prometheus/kube-prometheus-stack-prometheus --timeout=10m >/dev/null
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
kctl -n envoy-gateway-system wait --for=condition=Programmed gateway/edge --timeout=5m >/dev/null
# Programmed does not mean the proxy pods are serving yet.
kctl -n envoy-gateway-system wait --for=condition=Available deployment \
  -l gateway.envoyproxy.io/owning-gateway-name=edge --timeout=5m >/dev/null

log "done. Next: make smoke | make grafana | export KUBECONFIG=${KUBECONFIG_FILE}"
