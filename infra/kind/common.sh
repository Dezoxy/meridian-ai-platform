# shellcheck shell=bash
# Shared by up.sh, deploy.sh, upkeep.sh, demo.sh, smoke.sh, down.sh and grafana.sh. Source it; do not run it.
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

kctl() { kubectl --kubeconfig "${KUBECONFIG_FILE}" --context "${KUBE_CONTEXT}" "$@"; }
helmc() { helm --kubeconfig "${KUBECONFIG_FILE}" --kube-context "${KUBE_CONTEXT}" "$@"; }

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
