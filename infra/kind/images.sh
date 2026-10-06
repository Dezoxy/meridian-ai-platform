#!/usr/bin/env bash
# List the meridian:* images no workload uses: `make images`. A listing, not a
# check: it exits 0 whether or not an image is unused, and it removes nothing.
#   1. the meridian:* images in the Docker engine (each deploy of a changed tree
#      adds one, tagged by content), and in the kind node's containerd, where
#      `kind load` put the same ones under docker.io/library
#   2. the tags the pod templates of the Deployments, CronJobs and Jobs in the
#      namespace name now (a finished Job still names its image until its TTL
#      removes it, and the ingestion Job stays as a record)
#   3. each image marked `in use` or `unused`, the counts, and the commands a
#      person would run to remove the unused ones, printed and never run
# With no cluster (no credentials file) it lists the engine's images, every one
# unused by definition. A cluster that does not answer is an error: taking its
# images for unused would suggest removing one a workload uses. An image with
# no tag is not listed.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly NAMESPACE=meridian
readonly NODE="${CLUSTER_NAME}-control-plane"
# How containerd names an image that `kind load` took from the engine.
readonly NODE_REPOSITORY="docker.io/library/${IMAGE_REPOSITORY}"
readonly ENGINE_FORMAT='{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}'

# Each line of the output of the next three functions is "image ID size".
engine_rows() {
  local rows
  rows="$(docker image ls --format "${ENGINE_FORMAT}" "${IMAGE_REPOSITORY}")" ||
    die "docker image ls failed"
  awk 'NF && $1 !~ /:<none>$/' <<<"${rows}"
}

# crictl prints a header, then IMAGE TAG "IMAGE ID" SIZE per image.
node_rows() {
  local table
  table="$(docker exec "${NODE}" crictl images)" || die "crictl images failed in ${NODE}"
  awk -v repository="${NODE_REPOSITORY}" \
    '$1 == repository && $2 != "<none>" { print $1 ":" $2, $3, $4 }' <<<"${table}"
}

# The tags of IMAGE_REPOSITORY that a pod template (init containers too) names.
in_use_tags() {
  local workloads
  workloads="$(kctl -n "${NAMESPACE}" get deployments,cronjobs,jobs -o json)" ||
    die "cannot read the workloads of namespace ${NAMESPACE}"
  jq -r '.items[] | (.spec.template // .spec.jobTemplate.spec.template).spec
    | ((.initContainers // []) + .containers)[].image' <<<"${workloads}" |
    awk -F: -v repository="${IMAGE_REPOSITORY}" '$1 == repository { print $2 }'
}

# contains_line LINES LINE: true when LINE is one of the lines of LINES.
contains_line() {
  [[ $'\n'"$1"$'\n' == *$'\n'"$2"$'\n'* ]]
}

# report_place TITLE ROWS USED RESULT: print each image of ROWS marked, then the
# counts. USED is the tags in use. RESULT names the variable that gets the
# unused images, space-separated.
report_place() {
  local title="$1" rows="$2" used="$3" result="$4"
  local ref size mark total=0 in_use=0 unused=0 unused_refs=""
  log "${title}"
  while read -r ref _ size; do
    [[ -n "${ref}" ]] || continue
    total=$((total + 1))
    if contains_line "${used}" "${ref##*:}"; then
      mark="in use"
      in_use=$((in_use + 1))
    else
      mark=unused
      unused=$((unused + 1))
      unused_refs="${unused_refs:+${unused_refs} }${ref}"
    fi
    printf '  %-7s %s  %s\n' "${mark}" "${ref}" "${size}"
  done <<<"${rows}"
  printf '  images: %d, in use: %d, unused: %d\n' "${total}" "${in_use}" "${unused}"
  printf -v "${result}" '%s' "${unused_refs}"
}

# Print how to remove the unused images. Nothing here runs them.
suggest_removal() {
  local unused_engine="$1" unused_node="$2"
  if [[ -z "${unused_engine}${unused_node}" ]]; then
    log "no unused ${IMAGE_REPOSITORY}:* image; nothing to remove"
    return 0
  fi
  log "nothing was removed or changed; removing images is the owner's command:"
  if [[ -n "${unused_engine}" ]]; then
    printf 'docker image rm %s\n' "${unused_engine}"
  fi
  if [[ -n "${unused_node}" ]]; then
    printf 'docker exec %s crictl rmi %s\n' "${NODE}" "${unused_node}"
  fi
}

need_tools docker
engine="$(engine_rows)"
used=""
unused_engine=""
unused_node=""
if [[ -f "${KUBECONFIG_FILE}" ]]; then
  need_tools kubectl jq
  need_cluster
  used="$(in_use_tags)"
  node="$(node_rows)"
  report_place "${IMAGE_REPOSITORY}:* images in the Docker engine" "${engine}" "${used}" unused_engine
  report_place "${IMAGE_REPOSITORY}:* images in the kind node ${NODE}" "${node}" "${used}" unused_node
else
  report_place "${IMAGE_REPOSITORY}:* images in the Docker engine" "${engine}" "${used}" unused_engine
  log "no infra/kind/kubeconfig, so no cluster: every image above is unused by definition"
fi
suggest_removal "${unused_engine}" "${unused_node}"
