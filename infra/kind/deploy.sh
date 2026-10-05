#!/usr/bin/env bash
# Put the walking skeleton on the local platform: `make deploy`. Safe to run
# again; it converges. Needs `make up` first.
#   0. the preconditions, before anything is built or run: the Database, its
#      NetworkPolicy, its roles and their Secrets from `make up`; and the
#      ClusterIssuer `meridian-services` (S056), which must exist and be Ready:
#      without it the Certificates of step 3 are never issued, and a cluster
#      made before S055 does not know the Certificate kind, so the upgrade would
#      fail after the Jobs of step 2 had run
#   1. docker build of the repository's Dockerfile, tagged meridian:<first 12 hex
#      digits of the image ID> and loaded into the kind node (no registry)
#   2. the migration Job, then the policy seed Job, each as the database owner
#      role and run to completion (every time: the runner skips what is
#      applied and the seed mirrors its source, so a rerun takes seconds). The
#      seed comes before the services: a claim that met an empty policy table
#      would get a stored proposal "policy not found", which is final.
#   3. the Helm release `meridian` (infra/helm/meridian, with kind's values in
#      values/meridian.yaml), installed or upgraded: the six Deployments and
#      Services (Claims API, Agent Runtime, Model Gateway and the policy,
#      claims and knowledge tool servers), the one route (Claims API only)
#      with its request-size limit, and the sweep's CronJob (S052). The
#      release never holds a Job (the chart renders those only on request, so
#      step 2 and 4 can run them). The CronJob's spec is mutable (a change
#      reaches the Jobs it starts afterwards, none that is running), so the
#      upgrade updates it in place; no rollout waits for it, as it runs on a
#      schedule. Objects a raw `kubectl apply` made before the chart existed
#      are adopted (--take-ownership). Helm does not wait for the rollouts:
#      the steps below do. The release also holds a cert-manager Certificate
#      for every service and for the ingestion Job (S055): cert-manager turns
#      each into the Secret its pod mounts
#   4. the Certificates, waited for until each is Ready: a pod whose Secret does
#      not exist yet stays in ContainerCreating, and the ingestion Job (applied
#      outside the release, after it) would spend its deadline waiting for one
#   5. the Model Gateway's rollout, then the ingestion Job (it embeds the
#      wordings through the gateway), at most once per image: a finished Job of
#      this image's tag, and rows in knowledge.chunks, are the record that its
#      corpus is in the store
#   6. the other rollouts and the route, then, when an ingestion ran, a wait
#      until its token reservation has left the tenant's one-minute window
# The chart's only inputs from this script are the image's repository and tag
# (a Job's name ends in the tag; the CronJob's name has none).
# Nothing here prints a Secret's value, or a connection string of a Job's log.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

REPO_ROOT="$(cd "${KIND_DIR}/../.." && pwd)"
readonly REPO_ROOT
readonly CHART_DIR="${REPO_ROOT}/infra/helm/meridian"
readonly VALUES_FILE="${KIND_DIR}/values/meridian.yaml"
readonly RELEASE=meridian
readonly IMAGE_REPOSITORY=meridian
readonly TAG_LENGTH=12
readonly NAMESPACE=meridian
# The ClusterIssuer of kind's values (identity.issuer.name), made by `make up`.
readonly ISSUER_NAME=meridian-services
# One list: each service is a Deployment of the same name, and each has a
# Secret <service>-db (tests/meridian/test_kind_manifests.py checks the
# chart against it). The role Secrets come from DATABASE_ROLES (common.sh).
readonly SERVICES=(claims-api agent-runtime model-gateway policy-mcp claims-mcp knowledge-mcp)
# The ingestion calls this one, so it is waited for before the ingestion runs.
readonly GATEWAY_SERVICE=model-gateway
# Above the ingestion Job's activeDeadlineSeconds (360), which is above the 300 s
# the ingestion may wait for the gateway: the Job then needs time to write its
# refusal, and the deadline ends it before this script gives up on it.
readonly JOB_TIMEOUT=420
readonly JOB_INTERVAL=3
readonly ROLLOUT_TIMEOUT=300s
# cert-manager issues the services' certificates in seconds once its webhook and
# the issuer are Ready (`make up` waited for both); two minutes is far more than
# a first deploy needs.
readonly CERTIFICATE_TIMEOUT=120s
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

need_tools docker kind kubectl helm jq
require_local_docker
need_cluster
docker info >/dev/null 2>&1 || die "the Docker daemon is not running; start Docker Desktop"

# The database, its roles and the role Secrets come from `make up`.
require_database() {
  local role secret
  [[ "$(kctl -n "${NAMESPACE}" get database platform-db-meridian \
    -o jsonpath='{.status.applied}' 2>/dev/null)" == true ]] ||
    die "the Database 'meridian' is missing or not applied; run 'make up' first"
  # The chart's default-deny selects the database pod too: without this policy
  # (it admits the operator and the services) the Cluster would go unhealthy.
  kctl -n "${NAMESPACE}" get networkpolicy platform-db >/dev/null 2>&1 ||
    die "the NetworkPolicy 'platform-db' is missing, and the chart's default-deny would cut the database off from its operator; run 'make up' first"
  for role in "${DATABASE_ROLES[@]}"; do
    secret="$(role_secret_name "${role}")"
    kctl -n "${NAMESPACE}" get secret "${secret}" >/dev/null 2>&1 ||
      die "Secret ${secret} does not exist; run 'make up' first"
  done
  database_roles_reconciled ||
    die "the database roles are not all reconciled; run 'make up' first"
}

# The issuer of the services' certificates (S056): the ClusterIssuer that kind's
# values name (identity.issuer; a test keeps the two equal) must exist and be
# Ready, or the chart's Certificates would never be issued, and the migration
# and seed Jobs would already have run when the upgrade found out. A cluster
# made before S055 does not know the kind at all, and kubectl then fails: that
# is the same refusal, with its own error left out.
require_issuer() {
  local ready
  ready="$(kctl get clusterissuer "${ISSUER_NAME}" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" || ready=""
  [[ "${ready}" == True ]] ||
    die "the ClusterIssuer '${ISSUER_NAME}' is missing or not Ready (a cluster made before S055 does not even know the kind), so the chart's Certificates would never be issued, and the Jobs would already have run by then; run 'make up' first"
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

# helm_chart VERB [ARGUMENT...]: `helm VERB` on the release's chart with what
# every call shares: the release, the chart, the namespace, kind's values and
# the image just built (--set-string: twelve hex digits can be all digits, which
# --set would turn into a number). The tests render the chart with these same
# arguments (tests/meridian/chartsupport.py).
helm_chart() {
  local verb="$1"
  shift
  helmc "${verb}" "${RELEASE}" "${CHART_DIR}" --namespace "${NAMESPACE}" -f "${VALUES_FILE}" --set-string "image.repository=${IMAGE_REPOSITORY}" --set-string "image.tag=${tag}" "$@"
}

# render_job NAME: the manifest of the Job `migrate`, `seed` or `ingest` with
# its ServiceAccount, from the chart with that Job's flag on. The release never
# holds a Job: deploy.sh applies each one itself.
render_job() {
  local name="$1"
  helm_chart template --set "jobs.${name}.enabled=true" --show-only "templates/job-${name}.yaml"
}

# job_state NAME: "succeeded", "failed" or "running", from the Job's conditions.
job_state() {
  kctl -n "${NAMESPACE}" get job "$1" -o json |
    jq -r 'if any(.status.conditions[]?; .type == "Complete" and .status == "True") then "succeeded"
           elif any(.status.conditions[]?; .type == "Failed" and .status == "True") then "failed"
           else "running" end'
}

# printable_ascii: stdin without any byte that is not printable ASCII or a
# newline, and with anything that looks like a PostgreSQL URL (postgres:// or
# postgresql:// up to the next whitespace) replaced by postgresql://[redacted].
# A Job's log can quote data of a checkout (a manifest key, a database message),
# and an escape sequence in it must not reach the terminal; a driver's error can
# quote the connection string, and its password must not reach the log.
printable_ascii() {
  LC_ALL=C tr -cd '[:print:]\n' |
    sed -E 's#postgres(ql)?://[^[:space:]]+#postgresql://[redacted]#g'
}

# run_job NAME CHART_JOB: the Job NAME, rendered by render_job CHART_JOB, run to
# completion; its log is printed (through printable_ascii) when it ends, however
# it ends. A Job of the same name from an earlier deploy (finished, or failed
# and not yet removed) is deleted first: a Job's spec cannot change, and what
# the Jobs run is idempotent.
run_job() {
  local job="$1" chart_job="$2" state deadline
  kctl -n "${NAMESPACE}" delete "job/${job}" --ignore-not-found --wait >/dev/null
  log "job ${job}"
  render_job "${chart_job}" | kctl apply --server-side --force-conflicts -f - >/dev/null
  deadline=$((SECONDS + JOB_TIMEOUT))
  while ((SECONDS < deadline)); do
    # A transient kubectl error is not a verdict: ask again.
    state="$(job_state "${job}" 2>/dev/null)" || state=unknown
    case "${state}" in
      succeeded)
        log "job ${job} done: $(kctl -n "${NAMESPACE}" logs "job/${job}" 2>&1 | printable_ascii | tr '\n' ' ')"
        return 0
        ;;
      failed)
        kctl -n "${NAMESPACE}" logs "job/${job}" 2>&1 | printable_ascii >&2 || true
        die "the job ${job} failed (logs above)"
        ;;
    esac
    sleep "${JOB_INTERVAL}"
  done
  kctl -n "${NAMESPACE}" logs "job/${job}" 2>&1 | printable_ascii >&2 || true
  die "the job ${job} did not finish in ${JOB_TIMEOUT}s"
}

# The release: everything in the chart but the three Jobs, installed, or
# upgraded when it exists. Helm adopts the objects of an earlier raw apply
# (--take-ownership) and applies server-side, forcing the conflict with the
# field manager that made them. It does not wait: the rollouts below do, and it
# does not create the namespace: make up did.
install_release() {
  log "installing release ${RELEASE}"
  helm_chart upgrade --install --take-ownership --server-side=true --force-conflicts >/dev/null ||
    die "helm could not install release ${RELEASE} (its error is above; to see why: helm --kubeconfig ${KUBECONFIG_FILE} --kube-context ${KUBE_CONTEXT} -n ${NAMESPACE} status ${RELEASE}, or history ${RELEASE})"
}

# stored_chunk_count: the number of rows in knowledge.chunks, read in the
# database's primary pod the way smoke.sh reaches psql. Fails when it cannot be
# read.
stored_chunk_count() {
  local primary
  primary="$(kctl -n "${NAMESPACE}" get pod \
    -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || return 1
  [[ -n "${primary}" ]] || return 1
  kctl -n "${NAMESPACE}" exec "${primary}" -c postgres -- \
    psql -d meridian -tAc 'SELECT count(*) FROM knowledge.chunks' 2>/dev/null
}

# The ingestion of this image's corpus, at most once per image. Its Job is kept
# when it finishes (no TTL), so a succeeded Job of this tag means the corpus of
# this image was stored; the rows are counted too, because a finished Job is not
# proof that the store still holds them (a database that was recreated). Any
# other ingestion Job (another tag, or a failed one) is deleted first. Sets
# ${ingested_at} when it ran.
ingest_corpus() {
  local job="meridian-ingest-${tag}" found state chunks
  # An error is not "no such Job" (that would ingest again): it stops the
  # deploy. An empty answer means there is no such Job. Stderr is not part of
  # the answer: a warning there is not a Job.
  found="$(kctl -n "${NAMESPACE}" get job "${job}" -o name --ignore-not-found)" ||
    die "could not look for job/${job} (kubectl's error is above)"
  state=absent
  if [[ -n "${found}" ]]; then
    state="$(job_state "${job}")" || die "could not read the state of job/${job}"
  fi
  if [[ "${state}" == succeeded ]]; then
    if chunks="$(stored_chunk_count)" && [[ "${chunks}" =~ ^[0-9]+$ ]] && ((10#${chunks} > 0)); then
      log "the corpus of image ${image} is already in the store (job ${job} succeeded, ${chunks} chunks in knowledge.chunks); not ingesting again"
      return 0
    fi
    log "job ${job} succeeded, but knowledge.chunks holds no rows or could not be read; ingesting again"
  fi
  kctl -n "${NAMESPACE}" delete jobs -l app.kubernetes.io/name=meridian-ingest --ignore-not-found --wait >/dev/null
  run_job "${job}" ingest
  ingested_at=${SECONDS}
}

# The Certificates of the release (the chart's, labelled part-of=meridian; not
# the CA's, which is in another namespace): each Ready means cert-manager made
# its Secret. kubectl wait fails when it finds none, so a chart without
# Certificates is not mistaken for a ready one.
wait_for_certificates() {
  kctl -n "${NAMESPACE}" wait --for=condition=Ready certificate \
    -l app.kubernetes.io/part-of=meridian --timeout="${CERTIFICATE_TIMEOUT}" >/dev/null ||
    die "the Certificates were not all Ready in ${CERTIFICATE_TIMEOUT} (kubectl -n ${NAMESPACE} describe certificate; is the issuer 'meridian-services' Ready? run 'make up' first)"
  log "the services' certificates are ready"
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
require_issuer
build_image
run_job "meridian-migrate-${tag}" migrate
run_job "meridian-seed-${tag}" seed
install_release
wait_for_certificates
wait_for_deployment "${GATEWAY_SERVICE}"
ingest_corpus
wait_for_other_rollouts
wait_for_route
wait_for_token_window
log "done. Next: make demo"
