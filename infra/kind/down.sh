#!/usr/bin/env bash
# Remove the local platform: `make down`. Deletes the kind cluster "meridian"
# and its credentials file, nothing else. Refuses any other cluster name.
# Destructive, but the cluster is disposable on the development machine (hard
# rule 8): a session may run this when a test needs a fresh cluster, and says
# so when it does; never to clear a fault nobody has looked at. Since S075 it
# reads who holds the cluster first (common.sh) and stops when another holder has
# it, or when the cluster does not answer, unless TAKE_CLUSTER=1.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly ONLY_CLUSTER=meridian

target="${1:-${ONLY_CLUSTER}}"
if [[ "${target}" != "${ONLY_CLUSTER}" || "${CLUSTER_NAME}" != "${ONLY_CLUSTER}" ]]; then
  die "refusing to touch cluster '${target}': this script only removes '${ONLY_CLUSTER}'"
fi

need_tools kind docker kubectl
require_local_docker
docker info >/dev/null 2>&1 || die "the Docker daemon is not running; start Docker Desktop"

# Who holds the cluster (S075): another holder stops this before the cluster is
# deleted, unless TAKE_CLUSTER=1. The credentials are refreshed first, as `make up`
# does (a checkout that did not make the cluster has none), and a file this run
# made is removed again when it stops. A cluster that does not answer is not a
# cluster nobody holds (another step's `make up` may be restarting the node), so
# who holds it cannot be told and this stops too; TAKE_CLUSTER=1 deletes a broken
# cluster all the same.
check_holder_before_delete() {
  local me had_credentials=yes
  [[ -f "${KUBECONFIG_FILE}" ]] || had_credentials=no
  me="$(own_holder_name)" || exit 1
  kind export kubeconfig --name "${ONLY_CLUSTER}" --kubeconfig "${KUBECONFIG_FILE}"
  if read_cluster_holder; then
    # decide_cluster_holder dies on a refusal; the file this run made goes first.
    (decide_cluster_holder "make down" "${me}") || {
      [[ "${had_credentials}" == yes ]] || rm -f "${KUBECONFIG_FILE}"
      exit 1
    }
  elif [[ "${TAKE_CLUSTER:-}" == 1 ]]; then
    log "who holds the cluster cannot be read (it does not answer); TAKE_CLUSTER=1, so deleting it all the same"
  else
    [[ "${had_credentials}" == yes ]] || rm -f "${KUBECONFIG_FILE}"
    die "could not read who holds the cluster (kubectl's error is above); nothing was changed, and TAKE_CLUSTER=1 in front of the same command (TAKE_CLUSTER=1 make down) deletes it all the same"
  fi
}

if cluster_exists; then
  check_holder_before_delete
  log "deleting kind cluster ${ONLY_CLUSTER}"
  kind delete cluster --name "${ONLY_CLUSTER}" --kubeconfig "${KUBECONFIG_FILE}"
else
  log "kind cluster ${ONLY_CLUSTER} does not exist; nothing to delete"
fi
rm -f "${KUBECONFIG_FILE}"
log "done"
