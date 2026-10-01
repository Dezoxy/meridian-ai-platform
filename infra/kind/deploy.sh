#!/usr/bin/env bash
# Put the walking skeleton on the local platform: `make deploy`. Safe to run
# again; it converges. Needs `make up` first.
#   1. docker build of the repository's Dockerfile, tagged meridian:<first 12 hex
#      digits of the image ID> and loaded into the kind node (no registry)
#   2. the migration Job, as the database owner role, run to completion (every
#      time: the runner skips what is applied, so a rerun takes seconds)
#   3. every manifest in manifests/meridian/ but the Job: the Claims API, Agent
#      Runtime and Model Gateway Deployments and Services, and the one route
#      (Claims API only) with its request-size limit
# The only text replaced in the manifests is @IMAGE@ (and @TAG@ in the Job name).
# Nothing here prints a Secret's value.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

REPO_ROOT="$(cd "${KIND_DIR}/../.." && pwd)"
readonly REPO_ROOT
readonly MANIFEST_DIR="${KIND_DIR}/manifests/meridian"
readonly IMAGE_REPOSITORY=meridian
readonly TAG_LENGTH=12
readonly NAMESPACE=meridian
readonly MIGRATE_MANIFEST=migrate-job.yaml
# One list: each service is a Deployment of the same name, and each has a
# Secret <service>-db (tests/meridian/test_kind_manifests.py checks the
# manifests against it). The role Secrets come from DATABASE_ROLES (common.sh).
readonly SERVICES=(claims-api agent-runtime model-gateway)
readonly JOB_TIMEOUT=300
readonly JOB_INTERVAL=3
readonly ROLLOUT_TIMEOUT=300s
readonly ROUTE_TIMEOUT=120s

need_tools docker kind kubectl jq sed
require_local_docker
need_cluster
docker info >/dev/null 2>&1 || die "the Docker daemon is not running; start Docker Desktop"

# The database, its roles and the role Secrets come from `make up`.
require_database() {
  local role secret
  [[ "$(kctl -n "${NAMESPACE}" get database platform-db-meridian \
    -o jsonpath='{.status.applied}' 2>/dev/null)" == true ]] ||
    die "the Database 'meridian' is missing or not applied; run 'make up' first"
  for role in "${DATABASE_ROLES[@]}"; do
    secret="$(role_secret_name "${role}")"
    kctl -n "${NAMESPACE}" get secret "${secret}" >/dev/null 2>&1 ||
      die "Secret ${secret} does not exist; run 'make up' first"
  done
  database_roles_reconciled ||
    die "the database roles are not all reconciled; run 'make up' first"
}

# Build the image and tag it by content. Sets ${image} and ${tag}. Docker's
# output is shown only when the build fails.
build_image() {
  local iidfile out id
  iidfile="$(mktemp)"
  log "building the image"
  if ! out="$(docker build --provenance=false --sbom=false --iidfile "${iidfile}" "${REPO_ROOT}" 2>&1)"; then
    rm -f "${iidfile}"
    printf '%s\n' "${out}" >&2
    die "docker build failed"
  fi
  id="$(<"${iidfile}")"
  rm -f "${iidfile}"
  id="${id#sha256:}"
  [[ -n "${id}" ]] || die "docker build did not report an image ID"
  tag="${id:0:${TAG_LENGTH}}"
  image="${IMAGE_REPOSITORY}:${tag}"
  docker tag "sha256:${id}" "${image}"
  log "image ${image}"
  kind load docker-image "${image}" --name "${CLUSTER_NAME}" >/dev/null
  log "image ${image} loaded into cluster ${CLUSTER_NAME}"
}

# render FILE: the manifest with the image reference (and tag) filled in.
render() {
  sed -e "s|@IMAGE@|${image}|g" -e "s|@TAG@|${tag}|g" "$1"
}

# job_state NAME: "succeeded", "failed" or "running", from the Job's conditions.
job_state() {
  kctl -n "${NAMESPACE}" get job "$1" -o json |
    jq -r 'if any(.status.conditions[]?; .type == "Complete" and .status == "True") then "succeeded"
           elif any(.status.conditions[]?; .type == "Failed" and .status == "True") then "failed"
           else "running" end'
}

# The migration Job of this image tag, run to completion every time. A Job of
# the same tag from an earlier deploy (finished, or failed and not yet removed)
# is deleted first: a Job's spec cannot change, and the runner is idempotent.
run_migrations() {
  local job="meridian-migrate-${tag}" state deadline
  kctl -n "${NAMESPACE}" delete "job/${job}" --ignore-not-found --wait >/dev/null
  log "migrations: job ${job}"
  render "${MANIFEST_DIR}/${MIGRATE_MANIFEST}" | kctl apply --server-side --force-conflicts -f - >/dev/null
  deadline=$((SECONDS + JOB_TIMEOUT))
  while ((SECONDS < deadline)); do
    # A transient kubectl error is not a verdict: ask again.
    state="$(job_state "${job}" 2>/dev/null)" || state=unknown
    case "${state}" in
      succeeded)
        log "migrations done: $(kctl -n "${NAMESPACE}" logs "job/${job}" 2>&1 | tr '\n' ' ')"
        return 0
        ;;
      failed)
        kctl -n "${NAMESPACE}" logs "job/${job}" >&2 || true
        die "the migration job ${job} failed (logs above)"
        ;;
    esac
    sleep "${JOB_INTERVAL}"
  done
  kctl -n "${NAMESPACE}" logs "job/${job}" >&2 || true
  die "the migration job ${job} did not finish in ${JOB_TIMEOUT}s"
}

# Every manifest but the Job, found by glob: a new file is applied without
# editing this script.
apply_manifests() {
  local file
  for file in "${MANIFEST_DIR}"/*.yaml; do
    [[ "$(basename "${file}")" == "${MIGRATE_MANIFEST}" ]] && continue
    log "applying $(basename "${file}" .yaml)"
    render "${file}" | kctl apply --server-side --force-conflicts -f - >/dev/null
  done
}

wait_for_rollout() {
  local name
  for name in "${SERVICES[@]}"; do
    kctl -n "${NAMESPACE}" rollout status "deployment/${name}" --timeout="${ROLLOUT_TIMEOUT}" >/dev/null ||
      die "deployment ${name} did not roll out (kubectl -n ${NAMESPACE} logs deploy/${name})"
    log "deployment ${name} is ready"
  done
  kctl -n "${NAMESPACE}" wait \
    --for=jsonpath='{.status.parents[0].conditions[?(@.type=="Accepted")].status}'=True \
    httproute/claims-api --timeout="${ROUTE_TIMEOUT}" >/dev/null ||
    die "the HTTPRoute claims-api was not accepted by the edge"
  log "route claims.meridian.localhost accepted"
}

require_database
build_image
run_migrations
apply_manifests
wait_for_rollout
log "done. Next: make demo"
