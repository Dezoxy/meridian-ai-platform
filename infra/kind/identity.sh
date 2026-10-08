#!/usr/bin/env bash
# Keycloak on kind, the local sign-in issuer, as an add-on that is OFF unless
# MERIDIAN_IDENTITY=keycloak (S021, Y2b; the owner, 2026-10-08: "Keep it, opt-in
# only"). Entra ID is the issuer on Azure; this is its stand-in on kind, and
# nothing here is for Azure.
#
#   identity.sh up      what `make up` runs, last, when the switch is on: install
#                       or converge the add-on (below)
#   identity.sh status  read-only: what is on the cluster (the namespace, the
#                       Deployment, the route, the names of the Secrets and never
#                       their contents) and the memory available
#   identity.sh note    what `make up` runs when the switch is OFF: one line if an
#                       earlier run left the namespace, nothing otherwise
#
# There is no command that removes the add-on. Turning the switch off does not
# remove it either; the README ("The sign-in issuer") says how a person or a session
# removes the namespace by hand. Written and tested without a cluster; not run on
# one yet.
#
# `up` does, in this order, and changes nothing before the checks have passed:
#   - refuses unless MERIDIAN_IDENTITY=keycloak, a docker engine that is not local,
#     a cluster that does not answer or is held by another checkout (common.sh: the
#     scripts talk to the kubeconfig and the context of the local cluster alone),
#     under 2,500 MB of memory available (/proc/meminfo; the figure is printed) and
#     an edge Gateway that does not yet admit routes from `identity` (up.sh applies
#     it so when the switch is on, gateways.sh)
#   - the namespace `identity` and its NetworkPolicies (default-deny both ways)
#   - the realm: made at run time by identity-realm.sh into a folder under the
#     user's cache (mode 700, never the repository, removed when this script ends
#     however it ends), and loaded into two Secrets in `identity`: keycloak-realm
#     (the realm file, which holds the users' passwords and the clients' secrets
#     because Keycloak reads them there) and keycloak-credentials (the same values
#     as KEY=value lines, for the Claims API's and the scripts' copies, which Y3 and
#     Y7 make). Nothing of either is printed or put on a command line. A second run
#     KEEPS the Secrets it finds: a new realm ends every session (new keys, new
#     passwords and secrets), so MERIDIAN_IDENTITY_ROTATE=1 is asked for by name,
#     and it restarts a pod that runs, because a running pod never imports again
#   - the two policies that open the other ends (identity-peers-networkpolicy.yaml)
#   - the Deployment, Service and route (identity.yaml), the image from KEYCLOAK_IMAGE
#     of pins.env (by digest; no tag lives anywhere else), and the wait for the
#     rollout (the route's Accepted condition is not waited for: smoke proves it)
#   - what it made, the memory available after, and the reminders
#
# The memory file is /proc/meminfo; MERIDIAN_MEMINFO_FILE names another, for tests.
set -euo pipefail
{ set +x; } 2>/dev/null # a `bash -x` run must not trace a password

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
# shellcheck source=gateways.sh
. "${KIND_DIR}/gateways.sh"

readonly IDENTITY_NAMESPACE=identity
readonly IDENTITY_REALM_SECRET=keycloak-realm
readonly IDENTITY_CREDENTIALS_SECRET=keycloak-credentials
readonly IDENTITY_NAMESPACE_FILE="${KIND_DIR}/manifests/identity-networkpolicy.yaml"
readonly IDENTITY_PEERS_FILE="${KIND_DIR}/manifests/identity-peers-networkpolicy.yaml"
readonly IDENTITY_WORKLOAD_FILE="${KIND_DIR}/manifests/identity.yaml"
readonly IDENTITY_IMAGE_PLACEHOLDER=IMAGE-PLACEHOLDER
# The host name of the route, in identity.yaml and in the Deployment's --hostname
# (a test ties the three). It must be a lower-case name ending in .localhost: the
# same rule as the Meridian chart's route (templates/route.yaml; a test keeps the
# two patterns equal). Envoy matches the Host header, which any client can set; on
# kind the boundary is the published port, bound to 127.0.0.1.
readonly IDENTITY_ROUTE_HOST=id.meridian.localhost
readonly IDENTITY_ROUTE_PATTERN='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\.localhost$'
readonly IDENTITY_FRONT_URL="http://${IDENTITY_ROUTE_HOST}:8088"
# Where the pages' sign-in returns and the pages' origin, for the realm's client
# (the Claims API's host, infra/kind/values/meridian.yaml; the callback's path is
# the page flow's, Y3, and an exact URL: a different path is a new realm).
readonly IDENTITY_CLAIMS_ORIGIN=http://claims.meridian.localhost:8088
readonly IDENTITY_REDIRECT_URI="${IDENTITY_CLAIMS_ORIGIN}/auth/callback"
readonly IDENTITY_MIN_AVAILABLE_MB=2500
readonly IDENTITY_MEMINFO_FILE="${MERIDIAN_MEMINFO_FILE:-/proc/meminfo}"
readonly IDENTITY_ROLLOUT_TIMEOUT=5m

identity_work="" # the folder the realm is made in, while it exists
secrets_replaced=0

# cleanup_work: remove the realm folder, and only a folder this script made (a
# name that has the cache's folder and the prefix mktemp gave it).
cleanup_work() {
  if [[ "${identity_work}" == */meridian-identity/realm.* ]]; then
    rm -rf -- "${identity_work}"
  fi
  identity_work=""
}
trap cleanup_work EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

usage() {
  die "usage: identity.sh up | status | note (infra/kind/README.md, 'The sign-in issuer'); there is no command that removes the add-on"
}

# available_mb: the memory available, in whole MB, on stdout.
available_mb() {
  local kilobytes
  kilobytes="$(awk '/^MemAvailable:/ { print $2 }' "${IDENTITY_MEMINFO_FILE}")" ||
    die "could not read ${IDENTITY_MEMINFO_FILE}"
  [[ "${kilobytes}" =~ ^[0-9]+$ ]] ||
    die "could not read MemAvailable from ${IDENTITY_MEMINFO_FILE} (read: '$(printf '%s' "${kilobytes}" | LC_ALL=C tr -cd '[:print:]' | cut -c 1-40)')"
  printf '%d' $((kilobytes / 1024))
}

require_memory() {
  local available
  available="$(available_mb)" || exit 1
  log "identity: ${available} MB of memory available (the add-on needs at least ${IDENTITY_MIN_AVAILABLE_MB} MB: Keycloak asks for 700 MiB and may use 1 GiB)"
  ((available >= IDENTITY_MIN_AVAILABLE_MB)) ||
    die "${available} MB of memory is available and the sign-in issuer needs at least ${IDENTITY_MIN_AVAILABLE_MB} MB; nothing of the add-on was changed (the rest of make up is in place): free memory, stop what is not needed, and run MERIDIAN_IDENTITY=keycloak make up again"
}

# check_route_host: the host name is a lower-case .localhost name, and the manifest
# routes exactly that name.
check_route_host() {
  [[ "${IDENTITY_ROUTE_HOST}" =~ ${IDENTITY_ROUTE_PATTERN} ]] ||
    die "the route's host name '${IDENTITY_ROUTE_HOST}' is not a lower-case host name ending in .localhost: Keycloak is for the local cluster only, and nothing in this script routes a public name"
  grep -qxF -- "    - ${IDENTITY_ROUTE_HOST}" "${IDENTITY_WORKLOAD_FILE}" ||
    die "${IDENTITY_WORKLOAD_FILE} does not route the host name ${IDENTITY_ROUTE_HOST} (a line '    - ${IDENTITY_ROUTE_HOST}' under hostnames); nothing was changed"
}

# require_edge_admits_identity: the Gateway edge lets routes in `identity` attach.
# up.sh applies it so when the switch is on; a Gateway applied by an older run, or
# by hand, would leave the route unaccepted and the issuer unreachable.
require_edge_admits_identity() {
  local gateway
  gateway="$(kctl -n envoy-gateway-system get gateway edge -o json)" ||
    die "could not read the Gateway edge in envoy-gateway-system (kubectl's error is above); nothing was changed"
  jq -e --arg namespace "${IDENTITY_NAMESPACE}" '
    [.spec.listeners[]?.allowedRoutes.namespaces.selector.matchExpressions[]?
     | select(.key == "kubernetes.io/metadata.name" and .operator == "In") | .values[]]
    | index($namespace) != null' <<<"${gateway}" >/dev/null ||
    die "the Gateway edge does not admit routes from the namespace ${IDENTITY_NAMESPACE}, so the route would not be accepted; nothing was changed: run MERIDIAN_IDENTITY=keycloak make up, which applies the Gateway with the namespace in it"
}

# secret_exists NAME: 0 when the Secret is in `identity`. A read that fails stops
# the run: it is not "the Secret is absent".
secret_exists() {
  local found
  found="$(kctl -n "${IDENTITY_NAMESPACE}" get secret "$1" -o name --ignore-not-found)" ||
    die "could not read whether the Secret $1 exists in ${IDENTITY_NAMESPACE} (kubectl's error is above); nothing was changed"
  [[ -n "${found}" ]]
}

# publish_secret NAME SOURCE_FLAG: make or replace the Secret NAME from the file
# SOURCE_FLAG names (--from-file=KEY=PATH or --from-env-file=PATH). The Secret goes
# from kubectl to jq to kubectl on pipes, so no value is on a command line, and
# nothing is printed.
publish_secret() {
  kctl -n "${IDENTITY_NAMESPACE}" create secret generic "$1" "$2" --dry-run=client -o json |
    jq '.metadata.labels = {"app.kubernetes.io/part-of": "meridian-identity"}
      | del(.metadata.creationTimestamp)' |
    kctl -n "${IDENTITY_NAMESPACE}" apply --server-side --force-conflicts -f - >/dev/null ||
    die "could not make the Secret $1 in ${IDENTITY_NAMESPACE} (kubectl's error is above)"
}

# ensure_secrets: the two Secrets exist. Both found and no rotation asked for: kept.
# Otherwise the realm is made anew (a folder under the user's cache, mode 700,
# removed at once) and both Secrets are made or replaced.
ensure_secrets() {
  { set +x; } 2>/dev/null
  local realm_there=0 credentials_there=0 cache work
  if secret_exists "${IDENTITY_REALM_SECRET}"; then realm_there=1; fi
  if secret_exists "${IDENTITY_CREDENTIALS_SECRET}"; then credentials_there=1; fi
  if ((realm_there && credentials_there)) && [[ "${MERIDIAN_IDENTITY_ROTATE:-}" != 1 ]]; then
    log "identity: the Secrets ${IDENTITY_REALM_SECRET} and ${IDENTITY_CREDENTIALS_SECRET} exist and are kept (MERIDIAN_IDENTITY_ROTATE=1 makes new ones; that ends every session, because the passwords, the clients' secrets and the keys all change)"
    return 0
  fi
  if ((realm_there != credentials_there)); then
    log "identity: only one of the two Secrets exists; both are made anew"
  fi
  cache="${XDG_CACHE_HOME:-${HOME:?HOME is not set}/.cache}/meridian-identity"
  mkdir -p -- "${cache}"
  chmod 700 -- "${cache}"
  work="$(mktemp -d "${cache}/realm.XXXXXX")" ||
    die "could not make a folder under ${cache} for the realm"
  identity_work="${work}"
  "${KIND_DIR}/identity-realm.sh" "${work}" "${IDENTITY_REDIRECT_URI}" "${IDENTITY_CLAIMS_ORIGIN}" ||
    die "the realm generator failed (its message is above); nothing was loaded"
  [[ -s "${work}/meridian-staff-realm.json" && -s "${work}/secrets.env" ]] ||
    die "the realm generator left no realm file or no secrets file in ${work}"
  publish_secret "${IDENTITY_REALM_SECRET}" "--from-file=meridian-staff-realm.json=${work}/meridian-staff-realm.json"
  publish_secret "${IDENTITY_CREDENTIALS_SECRET}" "--from-env-file=${work}/secrets.env"
  cleanup_work
  secrets_replaced=1
  log "identity: the Secrets ${IDENTITY_REALM_SECRET} and ${IDENTITY_CREDENTIALS_SECRET} are made in ${IDENTITY_NAMESPACE} (nothing of them is printed; the folder they came from is removed)"
}

# identity_workload_manifest: identity.yaml with KEYCLOAK_IMAGE (pins.env) in the
# two places that take the image. Stops when the pin is not name:tag@sha256:digest,
# or the file does not hold the placeholder exactly twice.
identity_workload_manifest() {
  local manifest stripped
  [[ "${KEYCLOAK_IMAGE:-}" =~ ^quay\.io/keycloak/keycloak:[0-9][0-9.]*@sha256:[0-9a-f]{64}$ ]] ||
    die "KEYCLOAK_IMAGE in pins.env is not quay.io/keycloak/keycloak:<tag>@sha256:<64 hex digits>"
  manifest="$(<"${IDENTITY_WORKLOAD_FILE}")"
  stripped="${manifest//"${IDENTITY_IMAGE_PLACEHOLDER}"/}"
  ((${#manifest} - ${#stripped} == 2 * ${#IDENTITY_IMAGE_PLACEHOLDER})) ||
    die "${IDENTITY_WORKLOAD_FILE} does not hold the placeholder ${IDENTITY_IMAGE_PLACEHOLDER} exactly twice (the init container and the container)"
  manifest="${manifest//"${IDENTITY_IMAGE_PLACEHOLDER}"/${KEYCLOAK_IMAGE}}"
  [[ "${manifest}" != *PLACEHOLDER* ]] ||
    die "${IDENTITY_WORKLOAD_FILE} holds a placeholder word that this script does not fill"
  printf '%s\n' "${manifest}"
}

install_addon() {
  local existing workload available
  identity_on ||
    die "MERIDIAN_IDENTITY is not keycloak, so the sign-in issuer add-on is off and this installs nothing: MERIDIAN_IDENTITY=keycloak make up switches it on"
  [[ "${MERIDIAN_IDENTITY_ROTATE:-}" =~ ^1?$ ]] ||
    die "MERIDIAN_IDENTITY_ROTATE must be empty or 1 (1 makes a new realm, new passwords and new secrets, and ends every session)"
  check_route_host
  need_tools kubectl jq openssl awk timeout
  require_local_docker
  need_cluster
  check_cluster_holder "MERIDIAN_IDENTITY=keycloak make up"
  require_memory
  require_edge_admits_identity

  log "identity: the namespace ${IDENTITY_NAMESPACE} and its NetworkPolicies (denied both ways, DNS and the edge and the Claims API on 8080 admitted)"
  kctl apply --server-side --force-conflicts -f "${IDENTITY_NAMESPACE_FILE}" >/dev/null
  ensure_secrets
  log "identity: the policies that let the edge's proxy pods and the Claims API's pods send to Keycloak"
  kctl apply --server-side --force-conflicts -f "${IDENTITY_PEERS_FILE}" >/dev/null
  existing="$(kctl -n "${IDENTITY_NAMESPACE}" get deployment keycloak -o name --ignore-not-found)" ||
    die "could not read whether the Deployment keycloak exists (kubectl's error is above)"
  workload="$(identity_workload_manifest)" || exit 1
  log "identity: Keycloak (${KEYCLOAK_IMAGE}), its Service and the route for ${IDENTITY_ROUTE_HOST}"
  kctl apply --server-side --force-conflicts -f - <<<"${workload}" >/dev/null
  if ((secrets_replaced)) && [[ -n "${existing}" ]]; then
    log "identity: the realm was made anew and a pod was running: restarting it, because a running pod never imports a realm again"
    kctl -n "${IDENTITY_NAMESPACE}" rollout restart deployment/keycloak >/dev/null
  fi
  kctl -n "${IDENTITY_NAMESPACE}" rollout status deployment/keycloak \
    --timeout="${IDENTITY_ROLLOUT_TIMEOUT}" >/dev/null ||
    die "Keycloak (deployment/keycloak in ${IDENTITY_NAMESPACE}) was not Ready in ${IDENTITY_ROLLOUT_TIMEOUT}: its start takes about 13 seconds on an idle machine. Read the pod before anything is deleted: kubectl -n ${IDENTITY_NAMESPACE} get pods; describe the pod; logs of the container and of the init container copy-quarkus. A crash loop whose log says a path is read-only is the one thing this was written to expect: infra/kind/manifests/identity.yaml names the one line to change (readOnlyRootFilesystem)"
  available="$(available_mb)" || exit 1
  log "identity: Keycloak is Ready in ${IDENTITY_NAMESPACE}; the issuer is ${IDENTITY_FRONT_URL}/realms/meridian-staff (pods reach it as keycloak.${IDENTITY_NAMESPACE}.svc:8080)"
  log "identity: ${available} MB of memory available now"
  log "identity: a restart of the pod makes new signing keys and imports the realm again (development mode keeps nothing); make smoke checks the route; infra/kind/README.md, 'The sign-in issuer', lists what is untried"
}

# status: read-only. Names of Secrets, never their contents.
status() {
  local namespace found text
  need_tools kubectl jq awk timeout
  need_cluster
  if identity_on; then
    log "identity: MERIDIAN_IDENTITY is keycloak (the add-on is switched on)"
  else
    log "identity: MERIDIAN_IDENTITY is off (the default; this shell's value)"
  fi
  namespace="$(kctl get namespace "${IDENTITY_NAMESPACE}" -o name --ignore-not-found)" ||
    die "could not read whether the namespace ${IDENTITY_NAMESPACE} exists (kubectl's error is above)"
  if [[ -z "${namespace}" ]]; then
    log "identity: the namespace ${IDENTITY_NAMESPACE} is not on the cluster (the add-on was never made here, or was removed by hand)"
    log "identity: $(available_mb) MB of memory available"
    return 0
  fi
  log "identity: the namespace ${IDENTITY_NAMESPACE} exists"
  found="$(kctl -n "${IDENTITY_NAMESPACE}" get deployment keycloak -o json --ignore-not-found)" ||
    die "could not read the Deployment keycloak (kubectl's error is above)"
  if [[ -z "${found}" ]]; then
    log "identity: no Deployment keycloak"
  else
    text="$(jq -r '"\(.status.readyReplicas // 0)/\(.spec.replicas // .status.replicas // 0) ready, image \(.spec.template.spec.containers[0].image // "unknown")"' <<<"${found}")" ||
      die "the Deployment keycloak's answer is not a Deployment"
    log "identity: deployment keycloak: $(printf '%s' "${text}" | LC_ALL=C tr -cd '[:print:]' | cut -c 1-200)"
  fi
  found="$(kctl -n "${IDENTITY_NAMESPACE}" get httproute keycloak -o json --ignore-not-found)" ||
    die "could not read the HTTPRoute keycloak (kubectl's error is above)"
  if [[ -z "${found}" ]]; then
    log "identity: no HTTPRoute keycloak"
  else
    text="$(jq -r '[.status.parents[]?.conditions[]? | select(.type == "Accepted") | .status] | if length == 0 then "not reported" else join(",") end' <<<"${found}")" ||
      die "the HTTPRoute keycloak's answer is not a route"
    log "identity: route ${IDENTITY_ROUTE_HOST}: Accepted=$(printf '%s' "${text}" | LC_ALL=C tr -cd '[:print:]' | cut -c 1-40)"
  fi
  for text in "${IDENTITY_REALM_SECRET}" "${IDENTITY_CREDENTIALS_SECRET}"; do
    if secret_exists "${text}"; then
      log "identity: Secret ${text}: present (its contents are never read here)"
    else
      log "identity: Secret ${text}: absent"
    fi
  done
  log "identity: $(available_mb) MB of memory available"
}

# note_when_off: what `make up` says with the switch off. A read that fails is
# said and does not stop make up, which has done everything else by now.
note_when_off() {
  local found
  if ! found="$(kctl get namespace "${IDENTITY_NAMESPACE}" -o name --ignore-not-found)"; then
    log "identity: could not read whether the namespace ${IDENTITY_NAMESPACE} exists (kubectl's error is above); the sign-in issuer add-on is off"
    return 0
  fi
  [[ -z "${found}" ]] ||
    log "identity: MERIDIAN_IDENTITY is off and the namespace ${IDENTITY_NAMESPACE} still exists: turning the switch off does not remove it (infra/kind/README.md, 'The sign-in issuer', says how to remove it by hand)"
}

case "${1:-}" in
  up) install_addon ;;
  status) status ;;
  note) note_when_off ;;
  *) usage ;;
esac
