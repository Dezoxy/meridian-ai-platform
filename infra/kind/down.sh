#!/usr/bin/env bash
# Remove the local platform: `make down`. Deletes the kind cluster "meridian"
# and its credentials file, nothing else. Refuses any other cluster name.
# Destructive, but the cluster is disposable on the development machine (hard
# rule 8): a session may run this when a test needs a fresh cluster, and says
# so when it does; never to clear a fault nobody has looked at.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly ONLY_CLUSTER=meridian

target="${1:-${ONLY_CLUSTER}}"
if [[ "${target}" != "${ONLY_CLUSTER}" || "${CLUSTER_NAME}" != "${ONLY_CLUSTER}" ]]; then
  die "refusing to touch cluster '${target}': this script only removes '${ONLY_CLUSTER}'"
fi

need_tools kind docker
require_local_docker
docker info >/dev/null 2>&1 || die "the Docker daemon is not running; start Docker Desktop"

if cluster_exists; then
  log "deleting kind cluster ${ONLY_CLUSTER}"
  kind delete cluster --name "${ONLY_CLUSTER}" --kubeconfig "${KUBECONFIG_FILE}"
else
  log "kind cluster ${ONLY_CLUSTER} does not exist; nothing to delete"
fi
rm -f "${KUBECONFIG_FILE}"
log "done"
