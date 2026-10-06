# shellcheck shell=bash
# Shared by up.sh, deploy.sh, demo.sh, smoke.sh, down.sh and grafana.sh. Source it; do not run it.
#
# Safety rules kept in one place:
#  - The cluster's credentials live in infra/kind/kubeconfig (gitignored). The
#    owner's ~/.kube/config and its current context are never read or changed.
#  - Every kubectl and helm call names that file and the kind-meridian context,
#    so nothing depends on the "current context".
#  - Helm's own repository list (~/.config/helm or ~/Library/Preferences/helm)
#    is not read: charts are addressed by URL (--repo) and never `helm repo add`.

KIND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly KIND_DIR
# shellcheck source=pins.env
. "${KIND_DIR}/pins.env"

readonly KUBECONFIG_FILE="${KIND_DIR}/kubeconfig"
readonly KUBE_CONTEXT="kind-${CLUSTER_NAME}"
# The repository of the platform image: deploy.sh builds it, tags it by content
# and loads it into the node, images.sh lists the tags no workload uses.
# shellcheck disable=SC2034  # read by the scripts that source this file
readonly IMAGE_REPOSITORY=meridian
# The query that counts the rows of the knowledge store: deploy.sh reads it to
# decide whether to ingest, smoke.sh to check the store holds chunks. One copy.
# shellcheck disable=SC2034  # read by the scripts that source this file
readonly CHUNK_COUNT_SQL='SELECT count(*) FROM knowledge.chunks'
# Helm reads its repository list even when a chart is given with --repo, and
# fails on a stale entry it finds there. An unreadable file means "no repos".
export HELM_REPOSITORY_CONFIG=/dev/null

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

kctl() { kubectl --kubeconfig "${KUBECONFIG_FILE}" --context "${KUBE_CONTEXT}" "$@"; }
helmc() { helm --kubeconfig "${KUBECONFIG_FILE}" --kube-context "${KUBE_CONTEXT}" "$@"; }

need_tools() {
  local tool
  for tool in "$@"; do
    command -v "${tool}" >/dev/null 2>&1 || die "${tool} is not installed or not on PATH"
  done
}

need_cluster() {
  [[ -f "${KUBECONFIG_FILE}" ]] || die "no ${KUBECONFIG_FILE}; run 'make up' first"
  kctl get nodes >/dev/null 2>&1 || die "cluster ${CLUSTER_NAME} is not reachable; run 'make up'"
}

# True when the kind cluster exists. A failing `kind get clusters` is an error,
# not "no cluster" (down.sh would then remove the credentials of a running
# cluster). With no clusters kind prints "No kind clusters found." and exits 0.
# The list is read whole first: `grep -q` on a pipe can end it early and, with
# pipefail, report a failure for a match.
cluster_exists() {
  local clusters
  clusters="$(kind get clusters 2>&1)" || die "kind get clusters failed: ${clusters}"
  grep -qx "${CLUSTER_NAME}" <<<"${clusters}"
}

# kind publishes the edge port on the machine that runs the Docker engine. With
# a remote engine "127.0.0.1" would be that remote host, so refuse anything but
# a local unix socket (DOCKER_HOST first, else the current Docker context).
require_local_docker() {
  local host
  if [[ -n "${DOCKER_HOST:-}" ]]; then
    host="${DOCKER_HOST}"
  else
    host="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>&1)" ||
      die "cannot read the current Docker context: ${host}"
  fi
  [[ "${host}" == unix://* ]] ||
    die "the Docker engine is not local (${host}); use a unix socket context such as desktop-linux"
}

# The Meridian database's roles (S041). Each role's Secret is named after it with
# "_" as "-" and "-db" appended (meridian_owner -> meridian-owner-db).
readonly DATABASE_ROLES=(meridian_owner claims_api agent_runtime model_gateway policy_mcp claims_mcp knowledge_mcp claims_sweep gateway_upkeep policy_seed knowledge_ingest)

role_secret_name() { printf '%s-db' "${1//_/-}"; }

# True when CloudNativePG reports every DATABASE_ROLES role reconciled in the
# platform-db Cluster's status, with none that cannot be reconciled.
database_roles_reconciled() {
  local wanted status
  wanted="$(printf '%s\n' "${DATABASE_ROLES[@]}" | jq -R . | jq -sc .)"
  status="$(kctl -n meridian get cluster platform-db -o json | jq -c '.status.managedRolesStatus // {}')" ||
    return 1
  jq -e --argjson wanted "${wanted}" \
    '((.byStatus.reconciled // []) as $done | $wanted | all(. as $r | $done | index($r))) and ((.cannotReconcile // {}) == {})' \
    <<<"${status}" >/dev/null
}
