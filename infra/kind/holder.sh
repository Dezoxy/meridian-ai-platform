#!/usr/bin/env bash
# Print who holds the kind cluster: `make cluster-holder` (S075). It only reads:
# the three values of the ConfigMap meridian-cluster-holder in kube-system (the
# holder, its commit and the time of its last `make up` or `make deploy` that
# ended well), or that there is no record, or that there is no cluster. It
# changes nothing on the cluster; it refreshes the gitignored credentials file
# (`kind export kubeconfig`, as `make up` does) so that a checkout that did not
# make the cluster can ask. A cluster that does not answer is an error.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

need_tools kind kubectl

if ! cluster_exists; then
  printf 'no kind cluster %s\n' "${CLUSTER_NAME}"
  exit 0
fi
kind export kubeconfig --name "${CLUSTER_NAME}" --kubeconfig "${KUBECONFIG_FILE}"
read_cluster_holder ||
  die "could not read who holds the cluster (kubectl's error is above)"
if [[ "${holder_state}" == none ]]; then
  printf 'no record of who holds the cluster\n'
  exit 0
fi
printf 'holder: %s\ncommit: %s\ntime:   %s\n' "${holder_name}" "${holder_commit}" "${holder_time}"
