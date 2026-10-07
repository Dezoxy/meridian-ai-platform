# shellcheck shell=bash
# Shared by up.sh, deploy.sh, upkeep.sh, demo.sh, smoke.sh, down.sh, holder.sh, grafana.sh and cert-renew.sh. Source it; do not run it.
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

# The bounds of the scripts' kubectl calls (S073). Without one a call waits as
# long as the API server is silent, so a frozen node hung `make smoke` or `make
# deploy` with no word. kctl reads the call it is given and bounds it by what it
# is; the values can be set in the environment for a slow machine.
#  - An ordinary call gets --request-timeout, a bound per request to the API
#    server and not per call: kubectl makes several requests in a call (it tries
#    discovery more than once), so a call can take a multiple of the flag.
#    Measured against a listener that accepts a connection and never answers,
#    with --request-timeout=4s: `kubectl get` took 20 s, five times the flag
#    (S073 review, 2026-10-07; 4 s was measured, 15 s was not, and 15 s is
#    expected to give about 75 s). A call that passes its own keeps it.
#  - attach, port-forward, logs -f and get -w hold a stream by design, and the
#    flag would end them early: they get none. They are not used through kctl.
#  - wait and rollout status carry their own --timeout at every call site (a test
#    reads them), but that flag bounds the waiting loop and not the first request:
#    against the same listener `kubectl wait --timeout=3s` and `kubectl rollout
#    status --timeout=3s` were still running after 25 s. They run under the
#    system's `timeout` for their own --timeout plus KCTL_WAIT_MARGIN seconds, and
#    a call with no --timeout is refused (it would have no bound to add to).
#    --request-timeout is NOT added to them: whether it cuts a legitimate watch
#    short was not checked.
#  - exec and a delete with --wait hold a stream too, and nothing else bounds the
#    call that opens it: they run under the system's `timeout` for
#    KCTL_OUTER_TIMEOUT seconds. A delete with --wait also carries --timeout.
# The same holds for Helm's --timeout (see helmc below).
# What was seen on kind (run R2, 2026-10-07, and later runs): `make deploy` and
# `make smoke` with the request flag and the exec bound on their calls, which
# passed. The outer bound on wait, rollout status and Helm came after those runs:
# deploy and smoke ran under it on the path where nothing goes wrong (run R4e),
# and no bound has fired on kind. A frozen API server (`docker pause` of the
# node) was not seen, for any of these bounds: only the stand-ins of the tests and
# the review's silent listener have met one.
# `timeout` is coreutils': this machine's is uutils 0.10.0 (run R4e used it), not
# GNU's, and the statuses 124 and 137 behaved the same in the re-read's runs
# (need_tools cannot tell them apart).
KCTL_REQUEST_TIMEOUT="${KCTL_REQUEST_TIMEOUT:-15s}"
KCTL_OUTER_TIMEOUT="${KCTL_OUTER_TIMEOUT:-90}"
# The seconds added to a waiting call's own --timeout (kubectl) before the outer
# bound ends it. It is a margin and not a second timeout: the call's own timeout
# ends a healthy wait first, so the outer bound only fires when the API server
# has not answered at all.
KCTL_WAIT_MARGIN="${KCTL_WAIT_MARGIN:-30}"
# The seconds added to `helm upgrade`'s or `helm install`'s own --timeout. It is
# wider than kubectl's because a Helm that is ended in the middle of an upgrade
# can leave the release as `pending-upgrade`: it should be ended only when it is
# stuck, never because it was slow.
HELM_UPGRADE_MARGIN="${HELM_UPGRADE_MARGIN:-60}"
# The seconds a read of the release by helm may take: `helm get` has no timeout
# flag of its own.
HELM_READ_TIMEOUT="${HELM_READ_TIMEOUT:-30}"
# A margin from the environment is seconds in digits, at most six (a longer one
# overflows the arithmetic below and is no bound in any case), or empty for the
# default. Anything else is refused here, before it is made readonly: `abc` would
# read as 0 and race a healthy call's own timeout, `1.5` is an arithmetic error
# with no sentence, `-5` can make `timeout 0`, which means no bound at all. It is
# read as decimal (`08` is 8, not an invalid octal).
for margin_name in KCTL_WAIT_MARGIN HELM_UPGRADE_MARGIN; do
  [[ "${!margin_name}" =~ ^[0-9]{1,6}$ ]] ||
    die "${margin_name} must be a number of seconds in digits only, at most six of them (for example 30); it is empty or unset for the default"
  printf -v "${margin_name}" '%d' "$((10#${!margin_name}))"
done
unset margin_name
readonly KCTL_REQUEST_TIMEOUT KCTL_OUTER_TIMEOUT KCTL_WAIT_MARGIN HELM_UPGRADE_MARGIN HELM_READ_TIMEOUT
# How long `timeout` waits after its signal before it kills the call.
readonly KCTL_KILL_AFTER=5

# kctl_classify ARGS...: sets ${kctl_class} to "request" (add the flag), "outer"
# (run under timeout), "waits" (wait and rollout status: run under timeout for
# their own --timeout plus a margin) or "none" (a stream, or a call that has its
# own --request-timeout), and ${kctl_verb}, ${kctl_label} (the verb, or `rollout
# status`) and ${kctl_timeout} (the text of the call's --timeout, or empty). The
# words after `--` are the command of an exec, not kubectl's, so they are not
# read; a namespace is skipped as the value of its flag, so a namespace called
# `wait` decides nothing.
kctl_classify() {
  local word skip=0 sub="" follow=0 watch=0 waits=0 own=0 take_timeout=0
  kctl_verb=""
  kctl_label=""
  kctl_timeout=""
  kctl_class=request
  for word in "$@"; do
    [[ "${word}" == -- ]] && break
    if ((take_timeout)); then
      take_timeout=0
      kctl_timeout="${word}"
      continue
    fi
    if ((skip)); then
      skip=0
      continue
    fi
    case "${word}" in
      -n | --namespace | -s | --server | --context | --cluster | --user | --kubeconfig) skip=1 ;;
      --timeout) take_timeout=1 ;;
      --timeout=*) kctl_timeout="${word#--timeout=}" ;;
      --request-timeout | --request-timeout=*) own=1 ;;
      -f | --follow | --follow=true) [[ "${kctl_verb}" == logs ]] && follow=1 ;;
      -w | --watch | --watch=true | --watch-only) [[ "${kctl_verb}" == get ]] && watch=1 ;;
      --wait | --wait=true) [[ "${kctl_verb}" == delete ]] && waits=1 ;;
      -*) ;;
      *) if [[ -z "${kctl_verb}" ]]; then kctl_verb="${word}"; elif [[ -z "${sub}" ]]; then sub="${word}"; fi ;;
    esac
  done
  # A wait is bounded from its --timeout whatever else it passes.
  if [[ "${kctl_verb}" == wait ]]; then
    kctl_class=waits
    kctl_label="wait"
    return 0
  fi
  if [[ "${kctl_verb}" == rollout && "${sub}" == status ]]; then
    kctl_class=waits
    kctl_label="rollout status"
    return 0
  fi
  if ((own)); then
    kctl_class=none
    return 0
  fi
  case "${kctl_verb}" in
    attach | port-forward) kctl_class=none ;;
    logs) ((follow == 0)) || kctl_class=none ;;
    get) ((watch == 0)) || kctl_class=none ;;
    exec) kctl_class=outer ;;
    delete) ((waits == 0)) || kctl_class=outer ;;
  esac
}

kctl() {
  local status=0
  kctl_classify "$@"
  case "${kctl_class}" in
    request) kubectl --kubeconfig "${KUBECONFIG_FILE}" --context "${KUBE_CONTEXT}" --request-timeout="${KCTL_REQUEST_TIMEOUT}" "$@" ;;
    none) kubectl --kubeconfig "${KUBECONFIG_FILE}" --context "${KUBE_CONTEXT}" "$@" ;;
    outer)
      # --foreground: the call is not put in a process group of its own, so a
      # signal to the script's group (a terminal's ^C, smoke.sh's traps) reaches it.
      timeout --foreground --kill-after="${KCTL_KILL_AFTER}" "${KCTL_OUTER_TIMEOUT}" \
        kubectl --kubeconfig "${KUBECONFIG_FILE}" --context "${KUBE_CONTEXT}" "$@" || status=$?
      if ((status == 124 || status == 137)); then
        # Its own exit status can be either number too, so the line says "or".
        printf 'kctl: kubectl %s ended with status %d: it ran past KCTL_OUTER_TIMEOUT (%ss), or the command it ran exited with that status\n' \
          "${kctl_verb}" "${status}" "${KCTL_OUTER_TIMEOUT}" >&2
      fi
      return "${status}"
      ;;
    waits) kctl_waiting "$@" ;;
  esac
}

# duration_seconds TEXT: a Go duration as kubectl and Helm read it (one or more
# of <digits>h, <digits>m, <digits>s, or 0) as whole seconds on stdout. Returns 1
# for anything else: a bare number, a fraction, a sign, a word.
duration_seconds() {
  local text=$1 total=0
  [[ "${text}" == 0 ]] && {
    printf '0'
    return 0
  }
  [[ "${text}" =~ ^([0-9]+[hms])+$ ]] || return 1
  while [[ "${text}" =~ ^([0-9]+)([hms])(.*)$ ]]; do
    case "${BASH_REMATCH[2]}" in
      h) total=$((total + 10#${BASH_REMATCH[1]} * 3600)) ;;
      m) total=$((total + 10#${BASH_REMATCH[1]} * 60)) ;;
      s) total=$((total + 10#${BASH_REMATCH[1]})) ;;
    esac
    text="${BASH_REMATCH[3]}"
  done
  printf '%d' "${total}"
}

# kctl_waiting ARGS...: kubectl wait or rollout status (${kctl_label} and
# ${kctl_timeout} are kctl_classify's) under `timeout` for its own --timeout plus
# KCTL_WAIT_MARGIN seconds. The flag bounds the waiting loop, not the first
# request, so against an API server that never answers the call would not end.
# A call with no usable --timeout is refused before anything runs. When the bound
# ends the call the script stops, with the sentence (kubectl's own statuses are 0
# and 1, so 124 and 137 are the bound's, or 137 is the kernel's: the sentence says
# both).
kctl_waiting() {
  local seconds status=0
  [[ -n "${kctl_timeout}" ]] ||
    die "kctl: kubectl ${kctl_label} has no --timeout of its own, so it has no bound to add the margin to; every call that waits carries one (a mistake in the script; nothing was run)"
  seconds="$(duration_seconds "${kctl_timeout}")" ||
    die "kctl: kubectl ${kctl_label} has a --timeout that is not a duration of the form 300s or 5m (nothing was run)"
  timeout --foreground --kill-after="${KCTL_KILL_AFTER}" "$((seconds + KCTL_WAIT_MARGIN))" \
    kubectl --kubeconfig "${KUBECONFIG_FILE}" --context "${KUBE_CONTEXT}" "$@" || status=$?
  if ((status == 124 || status == 137)); then
    die "kubectl ${kctl_label} was ended after its --timeout of ${seconds}s plus a margin of ${KCTL_WAIT_MARGIN}s (KCTL_WAIT_MARGIN): the API server did not answer, or the call was killed (status 137 can also be the kernel's, for lack of memory). The cluster may be frozen or overloaded; read the node's state and the Docker containers before anything is deleted (on kind the database is the only copy of the audit log), then run the command again"
  fi
  return "${status}"
}

# helmc VERB ARGS...: Helm on the kind cluster. An install or an upgrade waits, and
# its --timeout is "time to wait for any individual Kubernetes operation" (Helm's
# help), which does not end a call against an API server that never answers: it
# runs under `timeout` for its --timeout plus HELM_UPGRADE_MARGIN seconds. A call
# with no usable --timeout is refused before anything runs. Other verbs (template,
# which reads no cluster) are not bounded here; helm reads of the cluster go
# through helmc_bounded.
helmc() {
  case "${1:-}" in
    upgrade | install) helmc_waiting "$@" ;;
    *) helm --kubeconfig "${KUBECONFIG_FILE}" --kube-context "${KUBE_CONTEXT}" "$@" ;;
  esac
}

# helmc_waiting VERB ARGS...: helmc for an install or an upgrade. When the bound
# ends it the script stops, and the sentence says what to read next: Helm ended in
# the middle of an upgrade can leave the release `pending-install` or
# `pending-upgrade`.
# Helm's --timeout is per operation ("time to wait for any individual Kubernetes
# operation (like Jobs for hooks)", Helm v4.3.0's help, quoted in the S073
# re-read), not per call. A chart with hooks can lawfully take a pre-hook, the
# wait and a post-hook, each up to the timeout, so the bound is three timeouts
# plus HELM_UPGRADE_MARGIN: the charts of up.sh are taken to carry hooks (the
# S073 re-read names cert-manager's startupapicheck and the Envoy Gateway certgen
# Job, and says it did not render those charts; nor was it done here). Three is
# a ceiling, not a measurement: nobody measured how long the hooks of these charts take, and a
# chart with more than two hook phases could pass it. The chart of
# deploy.sh has no hook and no --wait, so helm_chart sets ${helmc_phases} to 1
# (a local of its own, seen here because bash scopes locals dynamically) and its
# bound stays one timeout plus the margin. Any other caller gets three.
helmc_waiting() {
  local verb=$1 word previous="" value="" release="" in_namespace="" seconds status=0
  local phases=3 bound_text
  [[ "${helmc_phases:-}" == 1 ]] && phases=1
  shift
  # The release is the first word that is no flag and no flag's value, which holds
  # for both callers: the name comes right after the verb and its own flags.
  for word in "$@"; do
    case "${previous}" in
      --timeout) value="${word}" ;;
      -n | --namespace) in_namespace="${word}" ;;
    esac
    case "${previous}" in
      --timeout | -n | --namespace)
        previous=""
        continue
        ;;
    esac
    case "${word}" in
      --timeout=*) value="${word#--timeout=}" ;;
      --namespace=*) in_namespace="${word#--namespace=}" ;;
      -*) ;;
      *) [[ -n "${release}" ]] || release="${word}" ;;
    esac
    previous="${word}"
  done
  [[ -n "${value}" ]] ||
    die "helmc: helm ${verb} has no --timeout of its own, so it has no bound to add the margin to; every upgrade carries one (a mistake in the script; nothing was run)"
  seconds="$(duration_seconds "${value}")" ||
    die "helmc: helm ${verb} has a --timeout that is not a duration of the form 300s or 10m (nothing was run)"
  if ((phases == 3)); then
    bound_text="3 x its --timeout of ${seconds}s (a hook before, the wait, a hook after) plus a margin of ${HELM_UPGRADE_MARGIN}s (HELM_UPGRADE_MARGIN)"
  else
    bound_text="its --timeout of ${seconds}s plus a margin of ${HELM_UPGRADE_MARGIN}s (HELM_UPGRADE_MARGIN)"
  fi
  timeout --foreground --kill-after="${KCTL_KILL_AFTER}" "$((phases * seconds + HELM_UPGRADE_MARGIN))" \
    helm --kubeconfig "${KUBECONFIG_FILE}" --kube-context "${KUBE_CONTEXT}" "${verb}" "$@" || status=$?
  if ((status == 124 || status == 137)); then
    die "helm ${verb} of release ${release:-unknown}: the bound was reached and Helm was ended after ${bound_text}: the API server did not answer, or the call was killed. Helm ended in the middle of an install or an upgrade can leave the release as pending-install or pending-upgrade, so change nothing yet: read 'helm --kubeconfig ${KUBECONFIG_FILE} --kube-context ${KUBE_CONTEXT} -n ${in_namespace:-<namespace>} status ${release:-<release>}' and 'history ${release:-<release>}', and infra/kind/README.md, the section 'How long the scripts wait for the API server'. The way out is the owner's: helm rollback and an uninstall are on the list of things a session asks before, so a session does not run either"
  fi
  return "${status}"
}
# helmc for a call that reads the cluster and has no --timeout of its own.
helmc_bounded() {
  timeout --foreground --kill-after="${KCTL_KILL_AFTER}" "${HELM_READ_TIMEOUT}" \
    helm --kubeconfig "${KUBECONFIG_FILE}" --kube-context "${KUBE_CONTEXT}" "$@"
}

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

# What deploy.sh and upkeep.sh share about the Meridian chart and its Jobs (S066,
# moved from deploy.sh unchanged). The chart, kind's values and the release's
# name are one place; a script that calls helm_chart or job_state sets NAMESPACE
# (and, for helm_chart, ${tag}) first, as each script already has its own.
REPO_ROOT="$(cd "${KIND_DIR}/../.." && pwd)"
readonly REPO_ROOT
readonly CHART_DIR="${REPO_ROOT}/infra/helm/meridian"
readonly VALUES_FILE="${KIND_DIR}/values/meridian.yaml"
readonly RELEASE=meridian

# helm_chart VERB [ARGUMENT...]: `helm VERB` on the release's chart with what
# every call shares: the release, the chart, the namespace, kind's values and
# the image just built (--set-string: twelve hex digits can be all digits, which
# --set would turn into a number) and the rate store's pinned image (S066: kind's
# values turn the store on and name no image, so the pin has one place). The
# tests render the chart with these same arguments
# (tests/meridian/chartsupport.py).
# shellcheck disable=SC2154  # NAMESPACE and tag are the calling script's
helm_chart() {
  local verb="$1"
  # The chart has no hook and no --wait: one timeout is its bound (helmc_waiting).
  # shellcheck disable=SC2034  # read by helmc_waiting, which this function calls
  local helmc_phases=1
  shift
  helmc "${verb}" "${RELEASE}" "${CHART_DIR}" --namespace "${NAMESPACE}" -f "${VALUES_FILE}" --set-string "image.repository=${IMAGE_REPOSITORY}" --set-string "image.tag=${tag}" --set-string "rateStore.image=${RATE_STORE_IMAGE}" "$@"
}

# job_state NAME: "succeeded", "failed" or "running", from the Job's conditions.
# shellcheck disable=SC2154  # NAMESPACE is the calling script's
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

# apply_alert_rules: Meridian's alert rules (alerts/meridian.yaml, one
# PrometheusRule) applied to the cluster. `make up` and `make deploy` both call
# it, so a rule changed in the tree reaches the cluster by either and the two
# cannot drift (S073). It stops the script with a sentence when the apply fails,
# and with its own when the cluster does not serve the kind (no Prometheus
# operator: kubectl says "no matches for kind"). kubectl's error is printed
# first, cleaned (printable_ascii) and cut short.
apply_alert_rules() {
  local errors
  log "observability: Meridian's alert rules"
  errors="$(mktemp)"
  if ! kctl apply --server-side --force-conflicts -f "${KIND_DIR}/alerts/meridian.yaml" >/dev/null 2>"${errors}"; then
    printable_ascii <"${errors}" | cut -c 1-300 >&2
    if grep -q 'no matches for kind' "${errors}"; then
      rm -f "${errors}"
      die "the cluster does not serve the kind PrometheusRule, so Meridian's alert rules cannot be applied: the Prometheus operator is not installed (kubectl's error is above); run 'make up' first"
    fi
    rm -f "${errors}"
    die "could not apply Meridian's alert rules (kubectl's error is above); read it, then run the command again"
  fi
  rm -f "${errors}"
}

# The rate store's Secret carries, as this annotation, the SHA-256 of the rules of
# its ACL file (S066): up.sh writes it when it makes the Secret, deploy.sh
# computes the same from what `make up` would write now and stops on a difference.
# The password's hash is masked in what is hashed, so the annotation holds the
# users, their command lists and their key patterns, and no secret.
# shellcheck disable=SC2034  # read by the scripts that source this file
readonly RATE_STORE_ACL_ANNOTATION=meridian.kind/rate-store-acl-rules

# rate_store_acl_rules_hash: the SHA-256 (hex) of an ACL file from standard input,
# each password hash (#<64 hex digits>) replaced by #<hash> first.
rate_store_acl_rules_hash() {
  sed -E 's/#[0-9a-f]{64}/#<hash>/g' | openssl dgst -sha256 -r | awk '{print $1}'
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

# The API server's address (S063). The database's NetworkPolicy lets its pod open
# TCP 6443 at the API server's address alone. On kind the API server is the node,
# at an address that changes with every new cluster, so no file holds it: `make
# up` reads it from the `kubernetes` EndpointSlice in `default` and applies the
# policy with it, and `make deploy` and `make smoke` read both again and compare.
# All three read it here, so they cannot disagree about what counts as an address.
readonly API_SERVER_SLICE_LABEL=kubernetes.io/service-name=kubernetes
readonly API_SERVER_READ_TIMEOUT=15s
readonly API_SERVER_SHOWN=120

# What the functions below leave, in the shell that called them (call them
# directly, never inside $(...)): the endpoint's IPv4 addresses and the CIDRs of
# the policy's 6443 rule, one per line and sorted, and what was wrong when one
# returned 1. Nothing is ever printed that came out of the cluster uncleaned.
# shellcheck disable=SC2034  # read by the scripts that source this file
api_server_addresses=""
# shellcheck disable=SC2034  # read by the scripts that source this file
database_policy_cidrs=""
# shellcheck disable=SC2034  # read by the scripts that source this file
api_server_problem=""

# api_server_shown TEXT: TEXT on one line, printable ASCII only, cut short.
api_server_shown() {
  printf '%s' "$1" | LC_ALL=C tr -cd '[:print:]' | cut -c "1-${API_SERVER_SHOWN}"
}

# read_api_server_addresses: the IPv4 addresses of the `kubernetes` Service's
# endpoint into ${api_server_addresses}. The slice is read, not v1 Endpoints
# (deprecated); an IPv6 slice is not read. The address goes into YAML, so each
# one must be four decimal octets without a leading zero, and anything else (or
# no address at all) is a problem, never a guess.
read_api_server_addresses() {
  local json lines line octet='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
  local pattern="^(${octet}\\.){3}${octet}\$"
  api_server_addresses=""
  api_server_problem=""
  json="$(kctl -n default get endpointslices -l "${API_SERVER_SLICE_LABEL}" \
    -o json --request-timeout="${API_SERVER_READ_TIMEOUT}")" || {
    api_server_problem="could not read the EndpointSlice of the kubernetes Service in default (kubectl's error is above)"
    return 1
  }
  lines="$(jq -r '.items[] | select(.addressType == "IPv4") | .endpoints[]?.addresses[]?' \
    <<<"${json}" 2>/dev/null)" || {
    api_server_problem="could not read the EndpointSlice of the kubernetes Service in default: the answer is not a list of slices"
    return 1
  }
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    [[ "${line}" =~ ${pattern} ]] || {
      api_server_problem="the endpoint of the kubernetes Service in default holds '$(api_server_shown "${line}")', which is not an IPv4 address"
      return 1
    }
  done <<<"${lines}"
  api_server_addresses="$(printf '%s\n' "${lines}" | sed '/^$/d' | LC_ALL=C sort -u)"
  [[ -n "${api_server_addresses}" ]] || {
    api_server_problem="the EndpointSlice of the kubernetes Service in default holds no IPv4 address"
    return 1
  }
}

# read_database_policy_cidrs: the CIDRs of the ipBlocks of the platform-db
# policy's rule for port 6443 into ${database_policy_cidrs} (empty for a rule with
# no peer, which is the rule as it was before S063).
read_database_policy_cidrs() {
  local json
  database_policy_cidrs=""
  api_server_problem=""
  json="$(kctl -n meridian get networkpolicy platform-db -o json --ignore-not-found \
    --request-timeout="${API_SERVER_READ_TIMEOUT}")" || {
    api_server_problem="could not read the NetworkPolicy platform-db in meridian (kubectl's error is above)"
    return 1
  }
  [[ -n "${json}" ]] || {
    api_server_problem="the NetworkPolicy platform-db is missing in meridian: run make up"
    return 1
  }
  database_policy_cidrs="$(jq -r '.spec.egress[]? | select(any(.ports[]?; .port == 6443)) | .to[]?.ipBlock.cidr // empty' \
    <<<"${json}" | LC_ALL=C sort -u)" || {
    api_server_problem="could not read the NetworkPolicy platform-db in meridian: the answer is not a policy"
    return 1
  }
}

# api_server_matches_policy: 0 when the policy's CIDRs for port 6443 are exactly
# the endpoint's addresses as /32, in any order. 1 otherwise, with the reason in
# ${api_server_problem}: unreadable and different are told apart, and only
# "different" says the address changed.
api_server_matches_policy() {
  local address wanted=""
  read_api_server_addresses || return 1
  read_database_policy_cidrs || return 1
  while IFS= read -r address; do
    wanted+="${address}/32"$'\n'
  done <<<"${api_server_addresses}"
  wanted="$(printf '%s' "${wanted}" | LC_ALL=C sort -u)"
  [[ "${wanted}" == "${database_policy_cidrs}" ]] && return 0
  # shellcheck disable=SC2034  # read by the scripts that source this file
  api_server_problem="the API server's address changed: run make up (the endpoint holds $(api_server_shown "$(paste -sd ',' - <<<"${wanted}")"); the database's policy lists $(api_server_shown "$(paste -sd ',' - <<<"${database_policy_cidrs:-none}")"))"
  return 1
}

# Who holds the cluster (S075, M2). One plan step uses the kind cluster at a
# time, by rule; this makes the rule visible. The ConfigMap below, in kube-system,
# holds four values and nothing else (no path, no user or host name, no address of
# a remote): `holder` (CLUSTER_HOLDER, else the branch, else detached@<commit>),
# `commit` (the short hash of the checkout that ran the command), `time` (UTC, to
# the second) and `state`. `make up`, `make deploy` and `make down` read it before
# they change anything (check_cluster_holder): another holder is named and the
# command stops, unless TAKE_CLUSTER=1. `make up` and `make deploy` write it
# (record_cluster_holder) when they START to change the cluster, with state
# `changing`, and again when they end well, with state `ok`: a run that fails or
# is interrupted leaves `changing`, so the cluster never looks like nobody's
# right after a failure (the evidence of the fault is in its database and pods).
# A record with no `state` (written before this) reads as `ok`. It is a notice
# for an honest mistake, not a lock: two commands started in the same second both
# pass the check.
readonly HOLDER_CONFIGMAP=meridian-cluster-holder
readonly HOLDER_NAMESPACE=kube-system
readonly HOLDER_MAX_LENGTH=100
readonly HOLDER_PATTERN='^[A-Za-z0-9._/-]{1,100}$'

# What read_cluster_holder leaves, in the shell that called it (call it directly,
# never inside $(...)): "none", "found" or "unreadable", and the record's three
# values, cleaned of anything but printable ASCII and cut short.
# shellcheck disable=SC2034  # read by the scripts that source this file
holder_state=""
# shellcheck disable=SC2034  # read by the scripts that source this file
holder_name=""
# shellcheck disable=SC2034  # read by the scripts that source this file
holder_commit=""
# shellcheck disable=SC2034  # read by the scripts that source this file
holder_time=""
# "ok" or "changing": the state of the last run that wrote the record. Anything
# but "ok" or nothing (a record from before the state) reads as "changing".
# shellcheck disable=SC2034  # read by the scripts that source this file
holder_last_run=""
# What the state "changing" means, in the one wording that the refusal, the take,
# the line to the same holder and `make cluster-holder` all use: the record is
# written when a run starts to change the cluster, so while it goes on the record
# looks as it does after a run that failed.
# shellcheck disable=SC2034  # read by holder.sh
readonly HOLDER_CHANGING_MEANS='a make up or make deploy is running, or the last one did not end well'

# own_holder_name: the name this checkout records itself under, on stdout. A
# failure prints its sentence on stderr and returns 1 (call it as
# name="$(own_holder_name)" || exit 1: a die inside $(...) would leave only the
# subshell). A name that CLUSTER_HOLDER sets, or a branch, outside the pattern is
# refused, never changed into another: two branches must not become one holder.
own_holder_name() {
  local LC_ALL=C name
  if [[ -n "${CLUSTER_HOLDER+set}" ]]; then
    [[ "${CLUSTER_HOLDER}" =~ ${HOLDER_PATTERN} ]] || {
      printf 'error: CLUSTER_HOLDER must be 1 to %s characters from letters, digits, ., _, / and -\n' "${HOLDER_MAX_LENGTH}" >&2
      return 1
    }
    printf '%s' "${CLUSTER_HOLDER}"
    return 0
  fi
  name="$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null)" || {
    printf 'error: cannot tell the branch of this checkout (git failed in %s); set CLUSTER_HOLDER to the name this checkout should hold the cluster under\n' "${REPO_ROOT}" >&2
    return 1
  }
  if [[ "${name}" == HEAD ]]; then
    name="detached@$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null)" || {
      printf 'error: cannot read the commit of this detached checkout; set CLUSTER_HOLDER to the name this checkout should hold the cluster under\n' >&2
      return 1
    }
  fi
  [[ "${name}" =~ ${HOLDER_PATTERN} || "${name}" =~ ^detached@[0-9a-f]+$ ]] || {
    printf 'error: the name of this branch is not usable as a holder (1 to %s characters from letters, digits, ., _, / and -); set CLUSTER_HOLDER to the name this checkout should hold the cluster under\n' "${HOLDER_MAX_LENGTH}" >&2
    return 1
  }
  printf '%s' "${name}"
}

# holder_clean TEXT: TEXT as printable ASCII on one line, cut to the longest name.
holder_clean() {
  printf '%s' "$1" | LC_ALL=C tr -cd '[:print:]' | cut -c "1-${HOLDER_MAX_LENGTH}"
}

# read_cluster_holder: the record into holder_state and the three values. 0 when
# the cluster answered ("none" with no ConfigMap: --ignore-not-found prints
# nothing for one), 1 when it did not (kubectl's error is above and the state is
# "unreadable"): an answer that failed is never read as "no record". The values
# are cleaned: anyone who can edit a ConfigMap in kube-system chooses their text.
read_cluster_holder() {
  local out=""
  holder_state=unreadable
  holder_name=""
  holder_commit=""
  holder_time=""
  holder_last_run=""
  out="$(kctl -n "${HOLDER_NAMESPACE}" get configmap "${HOLDER_CONFIGMAP}" --ignore-not-found \
    -o 'jsonpath={.data.holder}|{.data.commit}|{.data.time}|{.data.state}' \
    --request-timeout="${API_SERVER_READ_TIMEOUT}")" || return 1
  if [[ -z "${out}" ]]; then
    holder_state=none
    return 0
  fi
  IFS='|' read -r holder_name holder_commit holder_time holder_last_run <<<"${out}" || true
  holder_name="$(holder_clean "${holder_name}")"
  holder_commit="$(holder_clean "${holder_commit}")"
  holder_time="$(holder_clean "${holder_time}")"
  holder_last_run="$(holder_clean "${holder_last_run}")"
  # Nothing is a record from before the state, and reads as "ok"; any other text
  # than "ok" is read as "changing", the state that asks for a look.
  [[ -z "${holder_last_run}" ]] && holder_last_run=ok
  [[ "${holder_last_run}" == ok ]] || holder_last_run=changing
  holder_state=found
}

# decide_cluster_holder COMMAND NAME: after read_cluster_holder answered. COMMAND
# ("make deploy") is what the refusal tells the person to put TAKE_CLUSTER=1 in
# front of; NAME is this checkout's. No record and the same holder go on (the
# commit is not compared: a holder moves on to a newer commit of its own branch;
# a retry after a run that did not end well is the ordinary case, and is told so in
# one line); another holder stops the command, with the one sentence, whatever the
# state, unless TAKE_CLUSTER is exactly 1. No record is the one case nothing
# protects: a cluster made before the record.
decide_cluster_holder() {
  local command=$1 me=$2 whose failed=""
  case "${holder_state}" in
    none)
      log "no record of who holds the cluster (made before the record); going on"
      return 0
      ;;
    found) ;;
    *) die "decide_cluster_holder was called with no record read" ;;
  esac
  if [[ "${holder_name}" == "${me}" ]]; then
    if [[ "${holder_last_run}" == changing ]]; then
      log "the cluster is held by ${me}, and the record says changing (${HOLDER_CHANGING_MEANS}); going on"
    else
      log "the cluster is held by ${me}"
    fi
    return 0
  fi
  whose="${holder_name:-unknown} (commit ${holder_commit:-unknown}, since ${holder_time:-unknown})"
  [[ "${holder_last_run}" != changing ]] || failed=yes
  if [[ "${TAKE_CLUSTER:-}" == 1 ]]; then
    if [[ -n "${failed}" ]]; then
      log "taking the cluster from ${whose}, whose record says changing (${HOLDER_CHANGING_MEANS})"
    else
      log "taking the cluster from ${whose}"
    fi
    return 0
  fi
  if [[ -n "${failed}" ]]; then
    die "the cluster is held by ${whose}, whose record says changing (${HOLDER_CHANGING_MEANS}): wait for it, or look at what failed before anything is deleted; it is not held by ${me}, nothing was changed, and TAKE_CLUSTER=1 in front of the same command (TAKE_CLUSTER=1 ${command}) takes it"
  fi
  die "the cluster is held by ${whose}, not by ${me}; nothing was changed, and TAKE_CLUSTER=1 in front of the same command (TAKE_CLUSTER=1 ${command}) takes it"
}

# check_cluster_holder COMMAND: read the record and decide, before anything is
# changed. A cluster that does not answer stops the command: who holds it cannot
# be told (`make down` is the exception, it reads the record itself).
check_cluster_holder() {
  local me
  me="$(own_holder_name)" || exit 1
  read_cluster_holder ||
    die "could not read who holds the cluster (kubectl's error is above); nothing was changed"
  decide_cluster_holder "$1" "${me}"
}

# record_cluster_holder STATE: this checkout becomes the holder, now. STATE is
# `changing` (a run starts to change the cluster; call it right after the check
# and before the first change) or `ok` (the run ended well). The ConfigMap is
# rendered by kubectl on the client and applied server-side, as the other
# ConfigMaps of `make up` are.
record_cluster_holder() {
  local state=${1:-} me commit now reached
  [[ "${state}" == changing || "${state}" == ok ]] ||
    die "record_cluster_holder takes the state changing or ok, not '${state}'"
  me="$(own_holder_name)" || exit 1
  if [[ "${state}" == changing ]]; then
    reached="this run went no further"
  else
    reached="the cluster was changed"
  fi
  commit="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null)" ||
    die "cannot read the commit of this checkout; who holds the cluster was not recorded (${reached})"
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  kctl -n "${HOLDER_NAMESPACE}" create configmap "${HOLDER_CONFIGMAP}" \
    --from-literal=holder="${me}" --from-literal=commit="${commit}" \
    --from-literal=time="${now}" --from-literal=state="${state}" \
    --dry-run=client -o json |
    jq 'del(.metadata.creationTimestamp)' |
    kctl -n "${HOLDER_NAMESPACE}" apply --server-side --force-conflicts -f - >/dev/null ||
    die "could not record who holds the cluster (kubectl's error is above); ${reached}"
  if [[ "${state}" == changing ]]; then
    log "the cluster is now marked as being changed by ${me} (commit ${commit}, ${now}, state changing)"
  else
    log "the cluster is now held by ${me} (commit ${commit}, ${now}, state ok)"
  fi
}
