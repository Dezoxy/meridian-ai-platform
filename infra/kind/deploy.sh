#!/usr/bin/env bash
# Put the walking skeleton on the local platform: `make deploy`. Safe to run
# again; it converges. Needs `make up` first.
#   1. docker build of the repository's Dockerfile, tagged meridian:<first 12 hex
#      digits of the image ID> and loaded into the kind node (no registry)
#   2. the migration Job, then the policy seed Job, each as the database owner
#      role and run to completion (every time: the runner skips what is
#      applied and the seed mirrors its source, so a rerun takes seconds). The
#      seed comes before the services: a claim that met an empty policy table
#      would get a stored proposal "policy not found", which is final.
#   3. every manifest in manifests/meridian/ but the three Jobs: the six
#      Deployments and Services (Claims API, Agent Runtime, Model Gateway and
#      the policy, claims and knowledge tool servers), and the one route
#      (Claims API only) with its request-size limit
#   4. the Model Gateway's rollout, then the ingestion Job (it embeds the
#      wordings through the gateway), at most once per image: a finished Job of
#      this image's tag is the record that its corpus is in the store
#   5. the other rollouts and the route, then, when an ingestion ran, a wait
#      until its token reservation has left the tenant's one-minute window
# The only text replaced in the manifests is @IMAGE@ (and @TAG@ in a Job's name).
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
readonly SEED_MANIFEST=seed-job.yaml
readonly INGEST_MANIFEST=ingest-job.yaml
# One list: each service is a Deployment of the same name, and each has a
# Secret <service>-db (tests/meridian/test_kind_manifests.py checks the
# manifests against it). The role Secrets come from DATABASE_ROLES (common.sh).
readonly SERVICES=(claims-api agent-runtime model-gateway policy-mcp claims-mcp knowledge-mcp)
# The ingestion calls this one, so it is waited for before the ingestion runs.
readonly GATEWAY_SERVICE=model-gateway
readonly JOB_TIMEOUT=300
readonly JOB_INTERVAL=3
readonly ROLLOUT_TIMEOUT=300s
readonly ROUTE_TIMEOUT=120s
# The gateway counts a tenant's tokens over a sliding 60 s window and its
# requests over 10 s. Measured on kind (2026-10-02): the ingestion reserves
# 7,679 of the claims-triage tenant's 10,000 tokens in six of its ten requests,
# and a triage that asks the model reserves about 1,090 in five. So a claim
# posted in the first seconds is refused, and only two fit in that minute.
# The wait counts from the moment this script saw the Job complete, which is
# never earlier than the last reservation; two seconds are margin on top.
readonly TOKEN_WINDOW_SECONDS=62
# SECONDS at which this deploy saw its ingestion complete; empty when none ran.
ingested_at=""

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

# run_job NAME MANIFEST: the Job NAME, from MANIFEST, run to completion; its log
# is printed when it succeeds. A Job of the same name from an earlier deploy
# (finished, or failed and not yet removed) is deleted first: a Job's spec cannot
# change, and what the Jobs run is idempotent.
run_job() {
  local job="$1" manifest="$2" state deadline
  kctl -n "${NAMESPACE}" delete "job/${job}" --ignore-not-found --wait >/dev/null
  log "job ${job}"
  render "${MANIFEST_DIR}/${manifest}" | kctl apply --server-side --force-conflicts -f - >/dev/null
  deadline=$((SECONDS + JOB_TIMEOUT))
  while ((SECONDS < deadline)); do
    # A transient kubectl error is not a verdict: ask again.
    state="$(job_state "${job}" 2>/dev/null)" || state=unknown
    case "${state}" in
      succeeded)
        log "job ${job} done: $(kctl -n "${NAMESPACE}" logs "job/${job}" 2>&1 | tr '\n' ' ')"
        return 0
        ;;
      failed)
        kctl -n "${NAMESPACE}" logs "job/${job}" >&2 || true
        die "the job ${job} failed (logs above)"
        ;;
    esac
    sleep "${JOB_INTERVAL}"
  done
  kctl -n "${NAMESPACE}" logs "job/${job}" >&2 || true
  die "the job ${job} did not finish in ${JOB_TIMEOUT}s"
}

# Every manifest but the three Jobs, found by glob: a new file is applied
# without editing this script.
apply_manifests() {
  local file
  for file in "${MANIFEST_DIR}"/*.yaml; do
    case "$(basename "${file}")" in
      "${MIGRATE_MANIFEST}" | "${SEED_MANIFEST}" | "${INGEST_MANIFEST}") continue ;;
    esac
    log "applying $(basename "${file}" .yaml)"
    render "${file}" | kctl apply --server-side --force-conflicts -f - >/dev/null
  done
}

# The ingestion of this image's corpus, at most once per image. Its Job is kept
# when it finishes (no TTL), so a succeeded Job of this tag means the corpus of
# this image is in the store. Any other ingestion Job (another tag, or a failed
# one) is deleted first. Sets ${ingested_at} when it ran.
ingest_corpus() {
  local job="meridian-ingest-${tag}" found state
  # An error is not "no such Job" (that would ingest again): it stops the
  # deploy. An empty answer means there is no such Job.
  found="$(kctl -n "${NAMESPACE}" get job "${job}" -o name --ignore-not-found 2>&1)" ||
    die "could not look for job/${job}: ${found}"
  state=absent
  if [[ -n "${found}" ]]; then
    state="$(job_state "${job}")" || die "could not read the state of job/${job}"
  fi
  if [[ "${state}" == succeeded ]]; then
    log "the corpus of image ${image} is already in the store (job ${job} succeeded); not ingesting again"
    return 0
  fi
  kctl -n "${NAMESPACE}" delete jobs -l app.kubernetes.io/name=meridian-ingest --ignore-not-found --wait >/dev/null
  run_job "${job}" "${INGEST_MANIFEST}"
  ingested_at=${SECONDS}
}

wait_for_deployment() {
  kctl -n "${NAMESPACE}" rollout status "deployment/$1" --timeout="${ROLLOUT_TIMEOUT}" >/dev/null ||
    die "deployment $1 did not roll out (kubectl -n ${NAMESPACE} logs deploy/$1)"
  log "deployment $1 is ready"
}

wait_for_other_rollouts() {
  local name
  for name in "${SERVICES[@]}"; do
    [[ "${name}" == "${GATEWAY_SERVICE}" ]] || wait_for_deployment "${name}"
  done
}

wait_for_route() {
  kctl -n "${NAMESPACE}" wait \
    --for=jsonpath='{.status.parents[0].conditions[?(@.type=="Accepted")].status}'=True \
    httproute/claims-api --timeout="${ROUTE_TIMEOUT}" >/dev/null ||
    die "the HTTPRoute claims-api was not accepted by the edge"
  log "route claims.meridian.localhost accepted"
}

# When an ingestion ran in this deploy, wait until its token reservation has
# left the window. The time is the script's own clock (SECONDS): the node's
# clock is not the laptop's, so no Kubernetes timestamp is compared with it.
wait_for_token_window() {
  local remaining
  [[ -n "${ingested_at}" ]] || return 0
  remaining=$((ingested_at + TOKEN_WINDOW_SECONDS - SECONDS))
  ((remaining > 0)) || return 0
  log "waiting ${remaining}s: the ingestion reserved about 7,700 of the claims-triage tenant's 10,000 tokens a minute, so until that minute has passed a triage that asks the model can be refused"
  sleep "${remaining}"
}

require_database
build_image
run_job "meridian-migrate-${tag}" "${MIGRATE_MANIFEST}"
run_job "meridian-seed-${tag}" "${SEED_MANIFEST}"
apply_manifests
wait_for_deployment "${GATEWAY_SERVICE}"
ingest_corpus
wait_for_other_rollouts
wait_for_route
wait_for_token_window
log "done. Next: make demo"
