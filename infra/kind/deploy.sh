#!/usr/bin/env bash
# Put the walking skeleton on the local platform: `make deploy`. Safe to run
# again; it converges. Needs `make up` first.
#   0. the preconditions, before anything is built or run: the Database, its
#      NetworkPolicy (which must name the API server's address as the cluster
#      has it now, or `make up` is run again, S063), its roles and their
#      Secrets from `make up`, and the
#      ConfigMap `telemetry-ca` (S063: the public certificate of the authority
#      that signed the collector's certificate, which the services mount to
#      verify it); and the
#      ClusterIssuer `meridian-services` (S056), which must exist and be Ready:
#      without it the Certificates of step 3 are never issued, and a cluster
#      made before S055 does not know the Certificate kind, so the upgrade would
#      fail after the Jobs of step 2 had run; and what approves them (S056): the
#      five CertificateRequestPolicies (meridian-services, meridian-services-ca,
#      meridian-deny-unlisted, telemetry-ca, otel-collector) Ready and
#      approver-policy running, because the
#      issuer is Ready without them and with cert-manager's own approver off
#      nothing would approve a request, so the wait of step 4 would run out. A
#      cluster made before S056 does not know the policy kind: the same refusal;
#      and the Secret `rate-store-credentials` (S066), with its two keys, the
#      gateway's address in the rate store and the store's ACL file, which
#      `make up` makes once: a pod that cannot read it would not start
#      Then, the first change (S073): Meridian's alert rules, the one
#      PrometheusRule `make up` applies too (apply_alert_rules, common.sh), so a
#      rule changed in the tree is on the cluster after a deploy. They come after
#      the checks, so a refused deploy changes nothing, and before the build, so
#      a cluster without the Prometheus operator is refused in seconds, not after
#      the image and the Jobs
#   1. docker build of the repository's Dockerfile, tagged meridian:<first 12 hex
#      digits of the image ID> and loaded into the kind node (no registry)
#   2. the migration Job, as the database owner role, then the policy seed Job,
#      as the role `policy_seed`, each run to completion (every time: the
#      runner skips what is applied and the seed mirrors its source, so a rerun
#      takes seconds; the ingestion of step 5 runs as `knowledge_ingest`). The
#      seed comes before the services: a claim that met an empty policy table
#      would get a stored proposal "policy not found", which is final.
#   3. the Helm release `meridian` (infra/helm/meridian, with kind's values in
#      values/meridian.yaml), installed or upgraded: the six Deployments and
#      Services (Claims API, Agent Runtime, Model Gateway and the policy,
#      claims and knowledge tool servers), the rate store that holds the
#      gateway's rate windows (S066: its Deployment, Service, Certificate and
#      NetworkPolicy; its image is RATE_STORE_IMAGE of pins.env, passed as
#      --set-string rateStore.image), the one route (Claims API only)
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
#   5. the rate store's rollout, then the Model Gateway's (every call it makes
#      is counted in the store), then the ingestion Job (it embeds the
#      wordings through the gateway), at most once per image: a finished Job of
#      this image's tag, and rows in knowledge.chunks, are the record that its
#      corpus is in the store
#   6. the other rollouts and the route, then, when an ingestion ran, a wait
#      until its token reservation has left the tenant's one-minute window
# The chart's inputs from this script are the image's repository and tag (a
# Job's name ends in the tag; the CronJob's name has none) and the rate store's
# image, which is a pin and not a build.
# Nothing here prints a Secret's value, or a connection string of a Job's log.
# Who holds the cluster (S075, common.sh): another holder stops this before step
# 0 unless TAKE_CLUSTER=1; the record is written with state `changing` right after
# that check, and with state `ok` as the last step, when the run ended well, so a
# run that fails or is interrupted leaves `changing`.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly TAG_LENGTH=12
readonly NAMESPACE=meridian
# The ClusterIssuer of kind's values (identity.issuer.name), made by `make up`.
readonly ISSUER_NAME=meridian-services
# What approves the Certificates' requests (S056): the CertificateRequestPolicies
# that manifests/certificate-policy.yaml applies (a test keeps the two equal) and
# the Deployment of approver-policy, which `make up` installs.
readonly CERTIFICATE_POLICIES=(meridian-services meridian-services-ca meridian-deny-unlisted telemetry-ca otel-collector)
readonly APPROVER_NAMESPACE=cert-manager
readonly APPROVER_DEPLOYMENT=cert-manager-approver-policy
# How long require_approval looks for an available replica of the add-on, and
# the pause between two looks. Right after a cold `make up` approver-policy lost
# its leader election, exited and was back in twenty seconds; a minute covers a
# restart and ends before it hides a real fault. The tests put a sleep that only logs on PATH.
readonly APPROVER_WAIT_SECONDS=60
readonly APPROVER_INTERVAL=5
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
# The release is installed without --wait (the rollouts above are the wait), so
# this bounds Helm's own work: as long as a rollout may take. S073.
readonly HELM_UPGRADE_TIMEOUT=300s
# How long a deleted Job may take to go, its pod's termination included. Under
# kctl's KCTL_OUTER_TIMEOUT (90 s), which bounds the call whatever happens.
readonly DELETE_TIMEOUT=60s
# The deadlines of a psql in a pod, as smoke.sh's PSQL_OPTIONS (S062): a lock or
# a statement that hangs ends the read, and the script goes on without the count.
readonly PSQL_OPTIONS='-c statement_timeout=5s -c lock_timeout=3s'
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

need_tools docker kind kubectl helm jq timeout
require_local_docker
need_cluster
docker info >/dev/null 2>&1 || die "the Docker daemon is not running; start Docker Desktop"
# Who holds the cluster (S075): another holder stops this, before anything is
# built or run, unless TAKE_CLUSTER=1. Then the record says `changing` until the
# last line of this script: a run that fails or is interrupted leaves it so. It is
# written before the prerequisites below, which only read: one that fails ("run
# make up first") leaves `changing` too, which costs the same holder nothing.
check_cluster_holder "make deploy"
record_cluster_holder changing

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
  # The operator's pod is in this namespace too (S072, contract C), so the same
  # default-deny selects it: without its own policy it would lose the API server
  # and the database's pods, and the database would stop being reconciled. A
  # cluster made before that change, or one whose policy was removed, lacks it.
  kctl -n "${NAMESPACE}" get networkpolicy cnpg-operator >/dev/null 2>&1 ||
    die "the NetworkPolicy 'cnpg-operator' is missing, and the chart's default-deny would cut the CloudNativePG operator off from the API server and the database's pods, so the database would stop being reconciled; run 'make up' first"
  # The policy names the API server's address (S063), which changes when Docker
  # restarts the node; the database would then be cut off from the API server.
  api_server_matches_policy || die "${api_server_problem}"
  # The public certificate of the authority that signed the collector's
  # certificate (S063): `make up` writes it, and the services mount it to verify
  # the collector. Without it their pods would not start, after the Jobs had run.
  kctl -n "${NAMESPACE}" get configmap telemetry-ca >/dev/null 2>&1 ||
    die "the ConfigMap 'telemetry-ca' is missing in ${NAMESPACE}: it holds the certificate the services verify the collector with, so their pods could not start, and the Jobs would already have run by then; run 'make up' first"
  for role in "${DATABASE_ROLES[@]}"; do
    secret="$(role_secret_name "${role}")"
    kctl -n "${NAMESPACE}" get secret "${secret}" >/dev/null 2>&1 ||
      die "Secret ${secret} does not exist; run 'make up' first"
  done
  database_roles_reconciled ||
    die "the database roles are not all reconciled; run 'make up' first"
}

# The rate store's Secret (S066, T-45), made by `make up`: the key `uri` is the
# gateway's address in Redis and `users.acl` is the store's ACL file. The chart
# makes the gateway's pod read the first through a required reference and the
# store's pod mount the second, so a missing Secret or key would keep a pod in
# CreateContainerConfigError after the Jobs had run; this stops before the image
# is built. Only the names of the keys are read (jq prints them), never a value,
# and a key that is there and empty counts as missing: an empty address stops the
# gateway's start. `make up` keeps a Secret that exists, so one made before the
# ACL file changed (or with a key short) is deleted first. A Secret that exists
# with both keys is still refused when its annotation (RATE_STORE_ACL_ANNOTATION,
# common.sh: the hash of the ACL's rules with the password's hash masked) is
# missing or is not the one `make up` would write now: a Secret from before the
# probes' user has no such user, and the store's probes would never pass, so the
# pod would not be Ready and nothing but its restarts would say why. The annotation
# is only a note `make up` left, so the ACL file the Secret holds is checked too:
# jq decodes its key users.acl and the hash function masks and hashes it in the same
# pipe, and the hash must be the one `make up` would write now; a file edited or
# replaced after `make up` under an annotation that still matches is refused. The
# rules below are up.sh's, copied (a test holds the two equal). Nothing of the
# Secret is printed, traced (tracing is off inside the function) or kept: the
# JSON is cleared once the hashes are taken, and only hashes are compared.
readonly RATE_STORE_SECRET=rate-store-credentials
readonly RATE_STORE_SECRET_KEYS=(uri users.acl)
readonly RATE_STORE_USER=gateway
readonly RATE_STORE_KEY_PATTERN='~meridian:rate:*'
readonly RATE_STORE_COMMANDS='+evalsha +script|load +time +zremrangebyscore +zrange +zadd +pexpire +hello'
readonly RATE_STORE_PROBE_USER=probe
readonly RATE_STORE_PROBE_COMMANDS='+ping'
# The store's Deployment: waited for before the gateway's, whose every call needs it.
readonly RATE_STORE_DEPLOYMENT=rate-store

# The hash of the rules `make up` writes now: up.sh's ACL file with a placeholder
# where the password's hash goes (it is masked in the hash anyway).
rate_store_expected_acl_hash() {
  printf 'user default off\nuser %s on #%s %s resetchannels -@all %s\nuser %s on nopass resetkeys resetchannels -@all %s\n' \
    "${RATE_STORE_USER}" "$(printf '0%.0s' {1..64})" "${RATE_STORE_KEY_PATTERN}" "${RATE_STORE_COMMANDS}" \
    "${RATE_STORE_PROBE_USER}" "${RATE_STORE_PROBE_COMMANDS}" | rate_store_acl_rules_hash
}

require_rate_store_secret() {
  # Tracing is off inside, and on again when the function returns if it was on: the
  # Secret's JSON holds the gateway's password, and a `bash -x` run would trace the
  # variable it is assigned to. (A `die` ends the script, so it needs no restore.)
  local traced=0 secret present key annotation acl_hash
  if [[ "$-" == *x* ]]; then traced=1; fi
  { set +x; } 2>/dev/null
  secret="$(kctl -n "${NAMESPACE}" get secret "${RATE_STORE_SECRET}" -o json 2>/dev/null)" ||
    die "Secret ${RATE_STORE_SECRET} does not exist; run 'make up' first (it holds the gateway's address in the rate store and the store's ACL file)"
  present="$(jq -r '.data // {} | to_entries[] | select(.value != "") | .key' <<<"${secret}")"
  for key in "${RATE_STORE_SECRET_KEYS[@]}"; do
    grep -qxF -- "${key}" <<<"${present}" ||
      die "Secret ${RATE_STORE_SECRET} has no key '${key}', or it is empty; run 'make up' first, after deleting the Secret (kubectl -n ${NAMESPACE} delete secret ${RATE_STORE_SECRET}): 'make up' keeps a Secret that exists"
  done
  annotation="$(jq -r --arg name "${RATE_STORE_ACL_ANNOTATION}" '.metadata.annotations[$name] // ""' <<<"${secret}")"
  # The ACL file itself, decoded by jq and hashed masked in the same pipe: its text
  # is never in a variable, an argument or a message, only the hash is. `-j`: no
  # newline of jq's own after the text, which the hash would see. jq's error output
  # goes nowhere: on a value it cannot decode it quotes the value's first characters,
  # and the sentence below already says what failed.
  acl_hash="$(jq -j '.data["users.acl"] // "" | @base64d' <<<"${secret}" 2>/dev/null | rate_store_acl_rules_hash)" ||
    die "Secret ${RATE_STORE_SECRET}: could not read its key 'users.acl' as base64 text, so the store's ACL file cannot be checked; run 'make up' first, after deleting the Secret (kubectl -n ${NAMESPACE} delete secret ${RATE_STORE_SECRET})"
  secret=""
  local remedy="delete the Secret (kubectl -n ${NAMESPACE} delete secret ${RATE_STORE_SECRET}), run 'make up' (it makes the Secret again), restart the rate store (kubectl -n ${NAMESPACE} rollout restart deployment/${RATE_STORE_DEPLOYMENT}) and then the Model Gateway (deployment/model-gateway), which read the ACL file and the address at their start and not before, then run 'make deploy' again; the order is docs/operations/runbooks/rate-store.md's"
  [[ -n "${annotation}" ]] ||
    die "Secret ${RATE_STORE_SECRET} has no annotation ${RATE_STORE_ACL_ANNOTATION}: it was made before the store's probes had a user of their own (probe), so its ACL file has none and the store's pod would never be Ready. ${remedy}"
  local expected
  expected="$(rate_store_expected_acl_hash)"
  [[ "${annotation}" == "${expected}" ]] ||
    die "the ACL file of Secret ${RATE_STORE_SECRET} was made for other users, commands or keys than 'make up' writes now (its annotation ${RATE_STORE_ACL_ANNOTATION} differs), so the store would refuse the gateway or its probes. ${remedy}"
  [[ "${acl_hash}" == "${expected}" ]] ||
    die "the ACL file in Secret ${RATE_STORE_SECRET} (its key users.acl) is not what 'make up' writes now, though its annotation ${RATE_STORE_ACL_ANNOTATION} says it is: the file was edited or replaced after 'make up' made it (or the annotation was copied onto another Secret), so the store would run other users, commands or keys than the chart and its tests assume. ${remedy}"
  ((traced == 0)) || set -x
  return 0
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

# What kubectl said at the last look at the add-on is kept in ${approver_error},
# on one clean line, and require_approval's refusal after the wait quotes it: an
# API error or a refused read is not an absent add-on, and "run 'make up'" is
# not its remedy. A look that read cleanly leaves it empty.
approver_error=""
readonly APPROVER_ERROR_LENGTH=300

# one_line TEXT: TEXT on one line: printable ASCII only (printable_ascii, common.sh),
# line breaks and runs of blanks squeezed to one blank, trimmed, cut short.
one_line() {
  printable_ascii <<<"$1" | tr '\n' ' ' | tr -s ' ' |
    sed -E 's/^ //; s/ $//' | cut -c "1-${APPROVER_ERROR_LENGTH}"
}

# approver_available: true when the add-on's Deployment has an available
# replica. It leaves what kubectl said in ${approver_error}.
approver_available() {
  local available errors
  errors="$(mktemp)"
  available="$(kctl -n "${APPROVER_NAMESPACE}" get deployment "${APPROVER_DEPLOYMENT}" \
    -o jsonpath='{.status.availableReplicas}' 2>"${errors}")" || available=""
  approver_error="$(one_line "$(<"${errors}")")"
  rm -f "${errors}"
  [[ "${available}" =~ ^[0-9]+$ ]] && ((10#${available} > 0))
}

# What approves the services' certificates (S056). cert-manager's own approver
# is off, so a Certificate's request waits for approver-policy: the three
# CertificateRequestPolicies must exist and be Ready and the add-on must have a
# replica available. The issuer is Ready without them, so require_issuer does not
# see this; the deploy would build, run its Jobs and die at the wait for the
# Certificates. A cluster made before S056 does not know the policy kind, and
# kubectl then fails: that is the same refusal, with its own error left out.
# Every policy that is wrong is named, not the first, and the add-on is named
# with them. A policy that is missing or not Ready is not what a restart
# explains, so the refusal is at once. When the policies are fine and only the
# add-on has no replica, it is looked at again, every APPROVER_INTERVAL seconds
# for APPROVER_WAIT_SECONDS: right after a cold `make up` it left and came back
# within twenty seconds, and "run 'make up'" was not the remedy for that.
require_approval() {
  local policy ready look looks said="" wrong=""
  for policy in "${CERTIFICATE_POLICIES[@]}"; do
    ready="$(kctl get certificaterequestpolicy "${policy}" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" || ready=""
    [[ "${ready}" == True ]] || wrong+="; the CertificateRequestPolicy '${policy}' is missing or not Ready"
  done
  if [[ -n "${wrong}" ]]; then
    approver_available ||
      wrong+="; the Deployment ${APPROVER_DEPLOYMENT} in ${APPROVER_NAMESPACE} has no available replica"
    die "nothing would approve the chart's Certificates (cert-manager's own approver is off), so none would be issued, and the Jobs would already have run by then${wrong} (a cluster made before S056 does not know the policy kind); run 'make up' first"
  fi
  looks=$((APPROVER_WAIT_SECONDS / APPROVER_INTERVAL + 1))
  for ((look = 1; look <= looks; look++)); do
    approver_available && return 0
    ((look == looks)) || sleep "${APPROVER_INTERVAL}"
  done
  if [[ -n "${approver_error}" ]]; then
    said=" (kubectl said at the last look: ${approver_error}; an error from the API or a refused read is not an absent add-on, so read it before running 'make up')"
  fi
  die "nothing would approve the chart's Certificates (cert-manager's own approver is off), so none would be issued, and the Jobs would already have run by then: the Deployment ${APPROVER_DEPLOYMENT} in ${APPROVER_NAMESPACE} was not available for ${APPROVER_WAIT_SECONDS}s${said}. A cluster that predates S056 does not have it: run 'make up' first. A cluster that has it and shows it restarting or not ready needs a look at the pod instead (kubectl -n ${APPROVER_NAMESPACE} get pods; logs deploy/${APPROVER_DEPLOYMENT}); run 'make deploy' again when it is Running"
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

# render_job NAME: the manifest of the Job `migrate`, `seed` or `ingest` with
# its ServiceAccount, from the chart with that Job's flag on. The release never
# holds a Job: deploy.sh applies each one itself.
render_job() {
  local name="$1"
  helm_chart template --set "jobs.${name}.enabled=true" --show-only "templates/job-${name}.yaml"
}

# run_job NAME CHART_JOB: the Job NAME, rendered by render_job CHART_JOB, run to
# completion; its log is printed (through printable_ascii) when it ends, however
# it ends. A Job of the same name from an earlier deploy (finished, or failed
# and not yet removed) is deleted first: a Job's spec cannot change, and what
# the Jobs run is idempotent.
run_job() {
  local job="$1" chart_job="$2" state deadline
  kctl -n "${NAMESPACE}" delete "job/${job}" --ignore-not-found --wait --timeout="${DELETE_TIMEOUT}" >/dev/null
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
  helm_chart upgrade --install --take-ownership --server-side=true --force-conflicts --timeout "${HELM_UPGRADE_TIMEOUT}" >/dev/null ||
    die "helm could not install release ${RELEASE} (its error is above; to see why: helm --kubeconfig ${KUBECONFIG_FILE} --kube-context ${KUBE_CONTEXT} -n ${NAMESPACE} status ${RELEASE}, or history ${RELEASE})"
}

# stored_chunk_count: the number of rows in knowledge.chunks (the query is
# CHUNK_COUNT_SQL of common.sh, which smoke.sh reads too), read in the
# database's primary pod the way smoke.sh reaches psql. Prints psql's answer and
# returns 0 when the exec ended with status 0; returns 1 for anything else: the
# pod was not found or not reached, the call ended at its outer bound (124 or
# 137), or psql ran and failed (kubectl exits with psql's own status then, and
# says "command terminated with exit code N"). Only status 0 says what the table
# holds, and its text is the caller's to check: an error never means "no rows",
# because `migrate` runs before this and the table exists. kubectl's error is
# shown (cleaned and cut short) when the call failed.
stored_chunk_count() {
  local primary errors answer status=0
  primary="$(kctl -n "${NAMESPACE}" get pod \
    -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary \
    -o jsonpath='{.items[0].metadata.name}')" || return 1
  [[ -n "${primary}" ]] || return 1
  errors="$(mktemp)"
  answer="$(kctl -n "${NAMESPACE}" exec "${primary}" -c postgres -- \
    env "PGOPTIONS=${PSQL_OPTIONS}" psql -d meridian -tAc "${CHUNK_COUNT_SQL}" 2>"${errors}")" || status=$?
  if ((status == 0)); then
    rm -f "${errors}"
    printf '%s\n' "${answer}"
    return 0
  fi
  printable_ascii <"${errors}" | cut -c 1-300 >&2
  rm -f "${errors}"
  return 1
}

# The ingestion of this image's corpus, at most once per image. Its Job is kept
# when it finishes (no TTL), so a succeeded Job of this tag means the corpus of
# this image was stored; the rows are counted too, because a finished Job is not
# proof that the store still holds them (a database that was recreated). Any
# other ingestion Job (another tag, or a failed one) is deleted first. Sets
# ${ingested_at} when it ran.
ingest_corpus() {
  local job="meridian-ingest-${tag}" found state chunks count_status=0
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
    chunks="$(stored_chunk_count)" || count_status=$?
    # Ingesting again removes the kept ingestion Jobs and embeds the corpus
    # again, which calls the model: the only answer that starts it is exit 0
    # with the text 0. Everything else that is not a positive number with
    # exit 0 stops the deploy with nothing removed: an error says nothing about
    # the table (`migrate` ran before this, so the table exists and an error
    # never means "no rows"), nor does an empty or garbled answer.
    ((count_status == 0)) && [[ "${chunks}" =~ ^[0-9]+$ ]] ||
      die "could not read how many rows knowledge.chunks holds (the database's pod was not found, did not answer, or psql failed, or the answer was not a number; kubectl's error, if any, is above), so it is not known whether the corpus of image ${image} is in the store. Nothing was removed and no ingestion started: it would embed the corpus again, which calls the model and costs money. Find out why the count cannot be read (kubectl -n ${NAMESPACE} get pods -l cnpg.io/cluster=platform-db), then run make deploy again"
    if ((10#${chunks} > 0)); then
      log "the corpus of image ${image} is already in the store (job ${job} succeeded, ${chunks} chunks in knowledge.chunks); not ingesting again"
      return 0
    fi
    log "job ${job} succeeded, but knowledge.chunks holds no rows; ingesting again"
  fi
  kctl -n "${NAMESPACE}" delete jobs -l app.kubernetes.io/name=meridian-ingest --ignore-not-found --wait --timeout="${DELETE_TIMEOUT}" >/dev/null
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
    die "the Certificates were not all Ready in ${CERTIFICATE_TIMEOUT}. Look at the requests first (since S056 the usual cause is one that approver-policy denied or never decided): kubectl -n ${NAMESPACE} get certificaterequest, then describe the one of the Certificate that is not Ready and read its Approved or Denied condition and the reason. Then the issuer '${ISSUER_NAME}': is it Ready? (kubectl get clusterissuer ${ISSUER_NAME}; 'make up' makes it). After a failed request cert-manager waits before it asks again (an hour, doubling), so once the cause is repaired ask it now: make cert-renew CERT=<name> (the name of a Certificate that is not Ready: kubectl -n ${NAMESPACE} get certificate). The runbook: docs/operations/runbooks/certificate-expiry.md"
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
# When this run did not run the ingestion (its Job had already succeeded, as
# after a deploy that was interrupted and run again) there is no moment of the
# script's own to count from, and it does not guess: it says that it skipped.
wait_for_token_window() {
  local remaining
  if [[ -z "${ingested_at}" ]]; then
    log "the wait for the claims-triage tenant's token window is skipped: this run did not run the ingestion, so it cannot tell when the ingestion's tokens were reserved. If a first request is refused for the tenant's token limit within a minute of a deploy that was interrupted, that is the window: wait a minute and run it again"
    return 0
  fi
  remaining=$((ingested_at + TOKEN_WINDOW_SECONDS - SECONDS))
  ((remaining > 0)) || return 0
  log "waiting ${remaining}s: the ingestion reserved about 7,700 of the claims-triage tenant's 10,000 tokens a minute, so until that minute has passed a triage that asks the model can be refused"
  sleep "${remaining}"
}

require_database
require_issuer
require_approval
require_rate_store_secret
apply_alert_rules
build_image
run_job "meridian-migrate-${tag}" migrate
run_job "meridian-seed-${tag}" seed
install_release
wait_for_certificates
wait_for_deployment "${RATE_STORE_DEPLOYMENT}"
wait_for_deployment "${GATEWAY_SERVICE}"
ingest_corpus
wait_for_other_rollouts
wait_for_route
wait_for_token_window
record_cluster_holder ok
log "done. Next: make demo"
