#!/usr/bin/env bash
# List the meridian:* images no workload uses: `make images`. A listing, not a
# check: it exits 0 whether or not an image is unused, and it removes nothing.
#   1. the meridian:* images in the Docker engine (each deploy of a changed tree
#      adds one, tagged by content), and in the kind node's containerd, where
#      `kind load` put the same ones under docker.io/library
#   2. the tags the pod templates of the Deployments, CronJobs and Jobs in the
#      namespace name now (a finished Job still names its image until its TTL
#      removes it, and the ingestion Job stays as a record), and the Pods that
#      exist (a bare Pod, or one of a rollout the template does not show yet)
#   3. the tags only a ReplicaSet names. The chart keeps revisionHistoryLimit
#      old ReplicaSets of each Deployment (2), so after a deploy that changed
#      the image the previous tags are what an old ReplicaSet would start again
#      on a rollback, and kind runs with pullPolicy Never: a removed image
#      cannot be pulled again. A ReplicaSet beyond the limit goes at the next
#      rollout, and the image only it named is then unused
#   4. each image marked `in use`, `rollback` (a rollback's target) or `unused`,
#      the counts, and the one command per place (the engine, the node) that a
#      person would run to remove the images nothing names, printed and never
#      run. A rollback's target gets no command.
# A workload that names an image of this repository by a digest and no tag is an
# error, not a guess: which tag runs cannot be told, and a guess could print a
# command that removes an image in use.
# With no credentials file it asks kind (cluster_exists): no cluster of that
# name, and it lists the engine's images, every one unused by definition; a
# cluster that exists (the credentials are in another checkout) is an error, as
# the images in use cannot be told. A cluster that does not answer, or a read
# that fails, is an error too: taking its images for unused would suggest
# removing one a workload uses. An image with no tag is not listed.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly NAMESPACE=meridian
readonly NODE="${CLUSTER_NAME}-control-plane"
# How containerd names an image that `kind load` took from the engine.
readonly NODE_REPOSITORY="docker.io/library/${IMAGE_REPOSITORY}"
readonly ENGINE_FORMAT='{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}'
readonly CANNOT_READ_TAGS="cannot read the tags in use from the cluster's answer (it is not a list; jq's error, when it printed one, is above): make images prints no listing, since an empty list that could not be read would mark every image unused"

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

# tags_of SELECTION JSON: the tags of IMAGE_REPOSITORY that the objects of the
# list JSON (the items SELECTION, a jq filter, keeps) name, in a pod template (a
# Deployment's, a ReplicaSet's, a Job's, a CronJob's) or in a Pod's own spec,
# init containers too. A reference is read as a person would: a digest after
# "@" is cut off, the tag is what follows the last ":" after the last "/" (so a
# port in a registry's host is not a tag), and the repository is what comes
# before it, with docker.io/library/ taken as the engine's own name. A
# repository other than IMAGE_REPOSITORY (another registry's, or another name)
# is another image and gives nothing. No tag and no digest is "latest", as
# Docker reads it; no tag and a digest gives the line "@digest", which no tag
# can be: refuse_digest_references stops on it.
tags_of() {
  jq -r "${1} | (.spec.template // .spec.jobTemplate.spec.template // .).spec
    | ((.initContainers // []) + .containers)[].image" <<<"$2" |
    awk -v repository="${IMAGE_REPOSITORY}" '
      {
        reference = $0
        digest = index(reference, "@")
        if (digest) reference = substr(reference, 1, digest - 1)
        slash = 0
        colon = 0
        for (i = 1; i <= length(reference); i++) {
          character = substr(reference, i, 1)
          if (character == "/") slash = i
          if (character == ":") colon = i
        }
        name = reference
        tag = ""
        if (colon > slash) {
          name = substr(reference, 1, colon - 1)
          tag = substr(reference, colon + 1)
        }
        sub(/^docker\.io\/library\//, "", name)
        if (name != repository) next
        if (tag != "") print tag
        else if (digest) print "@digest"
        else print "latest"
      }'
}

# refuse_digest_references LINES...: an image of IMAGE_REPOSITORY named by a
# digest and no tag leaves open which of the engine's tags runs, and a listing that
# guessed could print a removal command for an image in use. Dies, naming no
# digest.
refuse_digest_references() {
  if contains_line "$1" "@digest" || contains_line "$2" "@digest"; then
    die "a workload of namespace ${NAMESPACE} names an ${IMAGE_REPOSITORY} image by a digest and no tag, so make images cannot tell which tag it runs and prints no removal command"
  fi
}

# The JSON of the Deployments, CronJobs and Jobs, and of the ReplicaSets and
# Pods, of the namespace. A failing read is an error, never an empty list.
read_workloads() {
  kctl -n "${NAMESPACE}" get deployments,cronjobs,jobs -o json ||
    die "cannot read the workloads of namespace ${NAMESPACE}"
}
read_replicasets_and_pods() {
  kctl -n "${NAMESPACE}" get replicasets,pods -o json ||
    die "cannot read the replica sets and pods of namespace ${NAMESPACE}"
}

# has_items JSON: true when JSON is a list answer: an object whose "items" is an
# array (empty is fine). An empty or blank answer is not.
has_items() {
  jq -e '.items | type == "array"' <<<"$1" >/dev/null
}

# contains_line LINES LINE: true when LINE is one of the lines of LINES.
contains_line() {
  [[ $'\n'"$1"$'\n' == *$'\n'"$2"$'\n'* ]]
}

# report_place TITLE ROWS USED ROLLBACK UNUSED_RESULT ROLLBACK_RESULT: print each
# image of ROWS marked, then the counts. USED is the tags in use, ROLLBACK the
# tags only an old ReplicaSet names. The two RESULT names get the unused images
# and the rollback's targets, space-separated. An image in both lists is in use.
report_place() {
  local title="$1" rows="$2" used="$3" rollback="$4" unused_result="$5" rollback_result="$6"
  local ref size mark total=0 in_use=0 unused=0 targets=0 unused_refs="" rollback_refs="" counts
  log "${title}"
  while read -r ref _ size; do
    [[ -n "${ref}" ]] || continue
    total=$((total + 1))
    if contains_line "${used}" "${ref##*:}"; then
      mark="in use"
      in_use=$((in_use + 1))
    elif contains_line "${rollback}" "${ref##*:}"; then
      mark=rollback
      targets=$((targets + 1))
      rollback_refs="${rollback_refs:+${rollback_refs} }${ref}"
    else
      mark=unused
      unused=$((unused + 1))
      unused_refs="${unused_refs:+${unused_refs} }${ref}"
    fi
    printf '  %-8s %s  %s\n' "${mark}" "${ref}" "${size}"
  done <<<"${rows}"
  counts="$(printf 'images: %d, in use: %d, unused: %d' "${total}" "${in_use}" "${unused}")"
  if ((targets > 0)); then counts+=", rollback target: ${targets}"; fi
  printf '  %s\n' "${counts}"
  printf -v "${unused_result}" '%s' "${unused_refs}"
  printf -v "${rollback_result}" '%s' "${rollback_refs}"
}

# Print how to remove the unused images. Nothing here runs them. A rollback's
# target is named, kept, and has no command.
suggest_removal() {
  local unused_engine="$1" unused_node="$2" kept="$3"
  if [[ -z "${unused_engine}${unused_node}" ]]; then
    log "no unused ${IMAGE_REPOSITORY}:* image; nothing to remove"
  else
    log "nothing was removed or changed; removing images is the owner's command:"
    if [[ -n "${unused_engine}" ]]; then
      printf 'docker image rm %s\n' "${unused_engine}"
    fi
    if [[ -n "${unused_node}" ]]; then
      printf 'docker exec %s crictl rmi %s\n' "${NODE}" "${unused_node}"
    fi
  fi
  if [[ -n "${kept}" ]]; then
    log "kept, with no command: ${kept} (a rollback's target: an old ReplicaSet that the chart's revisionHistoryLimit keeps names it, a rollback would start it again, and with pullPolicy Never a removed image cannot be pulled again)"
  fi
}

# This checkout has no credentials file. A cluster that does not exist makes
# every image unused by definition; one that exists (the credentials are in
# another checkout) leaves the question open, and a listing that guessed would
# print removal commands for images a workload may use. cluster_exists asks
# kind, and dies when kind fails.
refuse_without_credentials() {
  need_tools kind
  if cluster_exists; then
    die "the kind cluster ${CLUSTER_NAME} exists, but this checkout has no ${KUBECONFIG_FILE}, so make images cannot tell which images are in use and prints no removal command; run it from the checkout that made the cluster"
  fi
}

need_tools docker
engine="$(engine_rows)"
used=""
rollback=""
unused_engine=""
unused_node=""
kept_engine=""
kept_node=""
if [[ -f "${KUBECONFIG_FILE}" ]]; then
  need_tools kubectl jq
  need_cluster
  workloads="$(read_workloads)"
  replicasets_and_pods="$(read_replicasets_and_pods)"
  # In use: a pod template now, or a Pod that exists (a bare Pod, or one of a
  # rollout that the Deployment's template does not show yet). A rollback's
  # target: only a ReplicaSet names it, the kind of one `kubectl rollout undo`
  # scales up again.
  # Each read on its own line with its own check: two reads in one $(...) leave
  # the status of the last, and an empty list that could not be read would mark
  # every image unused. An empty answer with status 0 gives jq no input, no
  # output and status 0, so each answer's shape is checked first: a list.
  has_items "${workloads}" || die "${CANNOT_READ_TAGS}"
  has_items "${replicasets_and_pods}" || die "${CANNOT_READ_TAGS}"
  from_templates="$(tags_of '.items[]' "${workloads}")" || die "${CANNOT_READ_TAGS}"
  from_pods="$(tags_of '.items[] | select(.kind == "Pod")' "${replicasets_and_pods}")" || die "${CANNOT_READ_TAGS}"
  rollback="$(tags_of '.items[] | select(.kind == "ReplicaSet")' "${replicasets_and_pods}")" || die "${CANNOT_READ_TAGS}"
  used="${from_templates}"$'\n'"${from_pods}"
  refuse_digest_references "${used}" "${rollback}"
  node="$(node_rows)"
  report_place "${IMAGE_REPOSITORY}:* images in the Docker engine" "${engine}" "${used}" "${rollback}" unused_engine kept_engine
  report_place "${IMAGE_REPOSITORY}:* images in the kind node ${NODE}" "${node}" "${used}" "${rollback}" unused_node kept_node
else
  refuse_without_credentials
  report_place "${IMAGE_REPOSITORY}:* images in the Docker engine" "${engine}" "${used}" "${rollback}" unused_engine kept_engine
  log "no infra/kind/kubeconfig, and kind lists no cluster ${CLUSTER_NAME}: every image above is unused by definition"
fi
kept="${kept_engine}${kept_engine:+ }${kept_node}"
suggest_removal "${unused_engine}" "${unused_node}" "${kept% }"
