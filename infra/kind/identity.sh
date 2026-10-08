#!/usr/bin/env bash
# Keycloak on kind, the local sign-in issuer, as an add-on that is OFF unless
# MERIDIAN_IDENTITY=keycloak (S021, Y2b; the owner, 2026-10-08: "Keep it, opt-in
# only"). Entra ID is the issuer on Azure; this is its stand-in on kind, and
# nothing here is for Azure.
#
#   identity.sh up      what `make up` runs, after it has recorded `ok`, when the
#                       switch is on: install or converge the add-on (below)
#   identity.sh status  read-only: what is on the cluster (the namespace, the
#                       Deployment, the route, the names of the Secrets and never
#                       their contents) and the memory available
#   identity.sh note    what `make up` runs when the switch is OFF: one line if an
#                       earlier run left the namespace, nothing otherwise
#   identity.sh users   read-only (Y2e): the cast the cluster holds, as a table of
#                       user name, display name, group and role (the role is the one
#                       the group carries); never a password, never a client secret
#   identity.sh passwords
#                       (Y2e) `make identity-passwords`: each cast member's user
#                       name and password, and nothing else of the Secret, after one
#                       line that says they are disposable test passwords of the
#                       local mock issuer. The ONLY command that prints a password.
#                       It refuses a Docker engine that is not the local one, a
#                       kubeconfig whose server is not 127.0.0.1, localhost or [::1]
#                       and a cluster that does not answer, and refuses when
#                       standard output is not a terminal
#                       unless MERIDIAN_IDENTITY_SHOW=1 is set, so that a pipe or a
#                       log file does not collect passwords by accident
#   Both read the realm Secret `keycloak-realm` (the cluster's own copy of the cast:
#   a Secret that an older run made is kept by `up` and may hold other users than
#   STAFF_CAST in identity-realm.sh) through a pipe into jq, which selects the user
#   names, the names and the roles (users) or the user names and passwords (passwords)
#   of the people, never of a service account or a client.
#
# There is no command that removes the add-on. Turning the switch off does not
# remove it either; the README ("The sign-in issuer") says how a person or a session
# removes the namespace by hand. Tested without a cluster, and run on kind once
# (run KR1, 2026-10-08, the first install only); the README lists what that run did
# not show.
#
# `up` does, in this order. Every check that only READS runs first, and nothing
# changes on the cluster until the last of them has passed:
#   - the checks: the switch is keycloak (a bad value is refused at the start); the
#     route's host name is a .localhost name; the pin of the image is
#     quay.io/keycloak/keycloak:<tag>@sha256:<digest> and the manifest holds its
#     placeholders as expected and exactly two `image:` lines; the tools; a docker
#     engine that is local; a cluster that answers (common.sh: the scripts talk to
#     the kubeconfig and the context of the local cluster alone) and is not held by
#     another checkout; at least 2,500 MB of memory available (/proc/meminfo; the
#     figure is printed); and an edge Gateway that already admits routes from
#     `identity` (up.sh applies it so when the switch is on, gateways.sh)
#   - the record of who holds the cluster becomes `changing` (common.sh), and
#     becomes `ok` again at the end: a refusal before this point leaves what up.sh
#     recorded (`ok`), and a half-made add-on leaves `changing`, also when this
#     script is run by hand; a trap says on standard error that the add-on is
#     partly made and points at `status`
#   - the namespace `identity` and its NetworkPolicies (default-deny both ways)
#   - the realm: made at run time by identity-realm.sh into a folder under the
#     user's cache (mode 700, never the repository), removed as soon as the Secrets
#     are made and by the exit trap on every exit this script can see (it ends, a
#     failure, HUP, INT, TERM). A kill -9 or a power cut leaves the folder: the next
#     run sweeps it at the start of this step (sweep_stale_realms says exactly what
#     it removes). It is loaded into two Secrets in `identity`: keycloak-realm (the
#     realm file, which holds the users' passwords and the clients' secrets because
#     Keycloak reads them there) and keycloak-credentials (the same values as
#     KEY=value lines, for the Claims API's and the scripts' copies, which Y3 and
#     Y7 make). Nothing of either is printed or put on a command line. Both carry
#     one annotation, a generation made once per run (Y2f). A second run KEEPS the
#     Secrets it finds when both carry the same generation: a new realm ends every
#     session (new keys, new passwords and secrets), so MERIDIAN_IDENTITY_ROTATE=1 is
#     asked for by name. A pair that is not of one generation (a run stopped between
#     the two, or made before generations were recorded) is made anew, both
#   - the two policies that open the other ends (identity-peers-networkpolicy.yaml)
#   - the Deployment, Service and route (identity.yaml), the image from KEYCLOAK_IMAGE
#     of pins.env (by digest; no tag lives anywhere else), and a pod annotation that
#     holds the SHA-256 of the realm Secret's content, so that a realm that changed
#     rolls the pod on any run (a running pod never imports a realm again), also a
#     run after a rotation that was interrupted; then the wait for the rollout (the
#     route's Accepted condition is not waited for: smoke proves it)
#   - what it made, the memory available after, and the reminders
#
# The memory file is /proc/meminfo; MERIDIAN_MEMINFO_FILE names another, for tests.
set -euo pipefail
{ set +x; } 2>/dev/null # a `bash -x` run must not trace a password

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
# shellcheck source=gateways.sh
. "${KIND_DIR}/gateways.sh" # fill_placeholder
identity_switch_check

readonly IDENTITY_NAMESPACE=identity
readonly IDENTITY_REALM_SECRET=keycloak-realm
readonly IDENTITY_REALM_KEY=meridian-staff-realm.json
readonly IDENTITY_CREDENTIALS_SECRET=keycloak-credentials
# The annotation both Secrets carry, one value for the run that made them (Y2f), in
# the prefix of the pod template's realm annotation (identity.yaml).
readonly IDENTITY_GENERATION_KEY=meridian.local/identity-generation
readonly IDENTITY_NAMESPACE_FILE="${KIND_DIR}/manifests/identity-networkpolicy.yaml"
readonly IDENTITY_PEERS_FILE="${KIND_DIR}/manifests/identity-peers-networkpolicy.yaml"
readonly IDENTITY_WORKLOAD_FILE="${KIND_DIR}/manifests/identity.yaml"
readonly IDENTITY_IMAGE_PLACEHOLDER=IMAGE-PLACEHOLDER
readonly IDENTITY_REALM_PLACEHOLDER=REALM-SHA256-PLACEHOLDER
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
# A realm folder that nothing has touched for this many minutes is a killed run's.
readonly IDENTITY_STALE_MINUTES=60

identity_work="" # the folder the realm is made in, while it exists
changes_started=0 # 1 from the first change to the cluster until the end of `up`

# cleanup_work: remove the realm folder, and only a folder this script made (a
# name that has the cache's folder and the prefix mktemp gave it).
cleanup_work() {
  if [[ "${identity_work}" == */meridian-identity/realm.* ]]; then
    rm -rf -- "${identity_work}"
  fi
  identity_work=""
}

# on_exit: the EXIT trap. Removes the realm folder, and when `up` fails after its
# first change says that the add-on is partly made.
on_exit() {
  local status=$?
  cleanup_work
  if ((status != 0 && changes_started == 1)); then
    printf 'identity: the add-on is partly made: this run stopped after its first change. infra/kind/identity.sh status says what is on the cluster; MERIDIAN_IDENTITY=keycloak make up again converges (every apply is server-side; the Secrets are kept when both are of one generation and both are made anew when not), and the record of who holds the cluster says changing until it does\n' >&2
  fi
}
trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

usage() {
  die "usage: identity.sh up | status | note | users | passwords (infra/kind/README.md, 'The sign-in issuer'); passwords prints secrets and needs a terminal or MERIDIAN_IDENTITY_SHOW=1; there is no command that removes the add-on"
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
    die "${available} MB of memory is available and the sign-in issuer needs at least ${IDENTITY_MIN_AVAILABLE_MB} MB; nothing of the add-on was changed (the rest of make up is in place, and the record of who holds the cluster says ok): free memory, stop what is not needed, and run MERIDIAN_IDENTITY=keycloak make up again"
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
    die "could not read whether the Secret $1 exists in ${IDENTITY_NAMESPACE} (kubectl's error is above)"
  [[ -n "${found}" ]]
}

# publish_secret NAME SOURCE_FLAG GENERATION: make or replace the Secret NAME from the
# file SOURCE_FLAG names (--from-file=KEY=PATH or --from-env-file=PATH), with the
# annotation IDENTITY_GENERATION_KEY set to GENERATION (the run's one value, the same
# on both Secrets: a rotation that stops between the two leaves two values, and the
# next run sees it). The Secret goes from kubectl to jq to kubectl on pipes, so no
# secret value is on a command line (the generation is none), and nothing is printed.
publish_secret() {
  kctl -n "${IDENTITY_NAMESPACE}" create secret generic "$1" "$2" --dry-run=client -o json |
    jq --arg key "${IDENTITY_GENERATION_KEY}" --arg generation "$3" \
      '.metadata.labels = {"app.kubernetes.io/part-of": "meridian-identity"}
      | .metadata.annotations = {($key): $generation}
      | del(.metadata.creationTimestamp)' |
    kctl -n "${IDENTITY_NAMESPACE}" apply --server-side --force-conflicts -f - >/dev/null ||
    die "could not make the Secret $1 in ${IDENTITY_NAMESPACE} (kubectl's error is above)"
}

# secret_generation NAME: the generation annotation of the Secret NAME on stdout
# (empty when it has none). Read with a jsonpath on .metadata.annotations alone:
# never the Secret's data. Run it in a command substitution and stop on its status.
secret_generation() {
  kctl -n "${IDENTITY_NAMESPACE}" get secret "$1" \
    -o "jsonpath={.metadata.annotations.${IDENTITY_GENERATION_KEY//./\\.}}" ||
    die "could not read the generation of the Secret $1 (kubectl's error is above)"
}

# sweep_stale_realms CACHE: what a killed run (kill -9, a power cut) left. It looks
# only at entries of CACHE named realm.<six letters or digits> (the shape mktemp
# gave them), skips any that is a symbolic link or not a folder, and any that was
# touched in the last IDENTITY_STALE_MINUTES minutes (a run in progress, or one
# that is about to be). In a folder left to remove it deletes REGULAR FILES and then
# the folder itself with rmdir, which refuses a folder that is not empty. A folder
# that holds anything else (a link, a folder, a device) is left alone with a line
# that says so. It follows no link and removes nothing outside CACHE.
sweep_stale_realms() {
  local cache=$1 folder file clean
  [[ -d "${cache}" && ! -L "${cache}" ]] || return 0
  for folder in "${cache}"/realm.??????; do
    [[ -e "${folder}" || -L "${folder}" ]] || continue
    [[ "${folder##*/}" =~ ^realm\.[A-Za-z0-9]{6}$ ]] || continue
    if [[ -L "${folder}" || ! -d "${folder}" ]]; then
      log "identity: ${folder} is not a plain folder; left alone"
      continue
    fi
    [[ -n "$(find "${folder}" -maxdepth 0 -mmin "+${IDENTITY_STALE_MINUTES}")" ]] || continue
    clean=1
    for file in "${folder}"/* "${folder}"/.[!.]*; do
      [[ -e "${file}" || -L "${file}" ]] || continue
      if [[ -L "${file}" || ! -f "${file}" ]]; then clean=0; fi
    done
    if ((clean == 0)); then
      log "identity: ${folder} holds something that is not a regular file; left alone"
      continue
    fi
    for file in "${folder}"/* "${folder}"/.[!.]*; do
      [[ -f "${file}" ]] && rm -f -- "${file}"
    done
    rmdir -- "${folder}"
    log "identity: removed the stale realm folder ${folder} (older than ${IDENTITY_STALE_MINUTES} minutes: its regular files and the folder itself)"
  done
}

# ensure_secrets: the two Secrets exist. They are KEPT only when both are there, no
# rotation is asked for and both carry the same non-empty generation (the annotation
# publish_secret sets, one value for the run that made them). The two are replaced one
# after the other, so a run that dies between them leaves a new realm beside old
# credentials; a pair that is not of one generation (that, or made before generations
# were recorded) is never kept. Otherwise the realm is made anew (a folder under the
# user's cache, mode 700, removed at once) and both Secrets are made or replaced, each
# with this run's generation.
ensure_secrets() {
  { set +x; } 2>/dev/null
  local realm_there=0 credentials_there=0 cache work generation
  local realm_generation credentials_generation
  cache="${XDG_CACHE_HOME:-${HOME:?HOME is not set}/.cache}/meridian-identity"
  sweep_stale_realms "${cache}"
  if secret_exists "${IDENTITY_REALM_SECRET}"; then realm_there=1; fi
  if secret_exists "${IDENTITY_CREDENTIALS_SECRET}"; then credentials_there=1; fi
  if ((realm_there && credentials_there)) && [[ "${MERIDIAN_IDENTITY_ROTATE:-}" != 1 ]]; then
    realm_generation="$(secret_generation "${IDENTITY_REALM_SECRET}")" || exit 1
    credentials_generation="$(secret_generation "${IDENTITY_CREDENTIALS_SECRET}")" || exit 1
    if [[ -n "${realm_generation}" && "${realm_generation}" == "${credentials_generation}" ]]; then
      log "identity: the Secrets ${IDENTITY_REALM_SECRET} and ${IDENTITY_CREDENTIALS_SECRET} exist, are of one generation and are kept (MERIDIAN_IDENTITY_ROTATE=1 makes new ones; that ends every session, because the passwords, the clients' secrets and the keys all change)"
      return 0
    fi
    log "identity: the Secrets ${IDENTITY_REALM_SECRET} and ${IDENTITY_CREDENTIALS_SECRET} are not of one generation (a run stopped between the two, or they were made before generations were recorded); both are made anew"
  elif ((realm_there != credentials_there)); then
    log "identity: only one of the two Secrets exists; both are made anew"
  fi
  generation="$(openssl rand -hex 8)" || die "openssl rand failed"
  [[ "${generation}" =~ ^[0-9a-f]{16}$ ]] ||
    die "openssl rand did not give 8 random bytes as 16 hex characters"
  mkdir -p -- "${cache}"
  chmod 700 -- "${cache}"
  work="$(mktemp -d "${cache}/realm.XXXXXX")" ||
    die "could not make a folder under ${cache} for the realm"
  identity_work="${work}"
  "${KIND_DIR}/identity-realm.sh" "${work}" "${IDENTITY_REDIRECT_URI}" "${IDENTITY_CLAIMS_ORIGIN}" ||
    die "the realm generator failed (its message is above); nothing was loaded"
  [[ -s "${work}/${IDENTITY_REALM_KEY}" && -s "${work}/secrets.env" ]] ||
    die "the realm generator left no realm file or no secrets file in ${work}"
  publish_secret "${IDENTITY_REALM_SECRET}" "--from-file=${IDENTITY_REALM_KEY}=${work}/${IDENTITY_REALM_KEY}" "${generation}"
  publish_secret "${IDENTITY_CREDENTIALS_SECRET}" "--from-env-file=${work}/secrets.env" "${generation}"
  cleanup_work
  log "identity: the Secrets ${IDENTITY_REALM_SECRET} and ${IDENTITY_CREDENTIALS_SECRET} are made in ${IDENTITY_NAMESPACE}, generation ${generation} (nothing of them is printed; the folder they came from is removed)"
}

# realm_fingerprint: the SHA-256 (hex) of the realm Secret's content as the cluster
# holds it (its base64 text, read once into a variable and piped to sha256sum; never
# printed, never an argument), for the pod annotation. A Secret whose content
# changed gives another digest and so another pod template.
realm_fingerprint() {
  { set +x; } 2>/dev/null
  local value
  value="$(kctl -n "${IDENTITY_NAMESPACE}" get secret "${IDENTITY_REALM_SECRET}" \
    -o "jsonpath={.data.${IDENTITY_REALM_KEY//./\\.}}")" ||
    die "could not read the realm Secret's content to fingerprint it (kubectl's error is above)"
  [[ -n "${value}" ]] ||
    die "the Secret ${IDENTITY_REALM_SECRET} holds no ${IDENTITY_REALM_KEY}"
  printf '%s' "${value}" | sha256sum | cut -d' ' -f1
}

# identity_workload_manifest: identity.yaml with KEYCLOAK_IMAGE (pins.env) in the
# places that take the image, on stdout; the realm fingerprint's placeholder is
# left for the caller (it needs the Secret). It only READS a file and a variable,
# so it runs before the first change. Stops when the pin is not
# name:tag@sha256:digest, when the file does not hold the image placeholder exactly
# twice and the fingerprint's exactly once, or when it has other than two `image:`
# lines (a third, written by hand, would run an image nobody pinned).
identity_workload_manifest() {
  local manifest stripped images
  [[ "${KEYCLOAK_IMAGE:-}" =~ ^quay\.io/keycloak/keycloak:[0-9][0-9.]*@sha256:[0-9a-f]{64}$ ]] ||
    die "KEYCLOAK_IMAGE in pins.env is not quay.io/keycloak/keycloak:<tag>@sha256:<64 hex digits>"
  manifest="$(<"${IDENTITY_WORKLOAD_FILE}")"
  stripped="${manifest//"${IDENTITY_IMAGE_PLACEHOLDER}"/}"
  ((${#manifest} - ${#stripped} == 2 * ${#IDENTITY_IMAGE_PLACEHOLDER})) ||
    die "${IDENTITY_WORKLOAD_FILE} does not hold the placeholder ${IDENTITY_IMAGE_PLACEHOLDER} exactly twice (the init container and the container)"
  stripped="${manifest//"${IDENTITY_REALM_PLACEHOLDER}"/}"
  ((${#manifest} - ${#stripped} == ${#IDENTITY_REALM_PLACEHOLDER})) ||
    die "${IDENTITY_WORKLOAD_FILE} does not hold the placeholder ${IDENTITY_REALM_PLACEHOLDER} exactly once (the pod annotation)"
  images="$(grep -cE '^[[:space:]]*image:' "${IDENTITY_WORKLOAD_FILE}" || true)"
  [[ "${images}" == 2 ]] ||
    die "${IDENTITY_WORKLOAD_FILE} has ${images} image: lines and must have exactly two, both the placeholder: an image written by hand would not be the pinned one"
  manifest="${manifest//"${IDENTITY_IMAGE_PLACEHOLDER}"/${KEYCLOAK_IMAGE}}"
  printf '%s\n' "${manifest}"
}

install_addon() {
  local workload realm_sha available
  identity_on ||
    die "MERIDIAN_IDENTITY is not keycloak, so the sign-in issuer add-on is off and this installs nothing: MERIDIAN_IDENTITY=keycloak make up switches it on"
  [[ "${MERIDIAN_IDENTITY_ROTATE:-}" =~ ^1?$ ]] ||
    die "MERIDIAN_IDENTITY_ROTATE must be empty or 1 (1 makes a new realm, new passwords and new secrets, and ends every session)"
  check_route_host
  workload="$(identity_workload_manifest)" || exit 1
  need_tools kubectl jq openssl awk timeout sha256sum
  require_local_docker
  need_cluster
  check_cluster_holder "MERIDIAN_IDENTITY=keycloak make up"
  require_memory
  require_edge_admits_identity

  # The last check has passed: from here on the cluster changes, and a stop leaves
  # `changing` (and the trap's line).
  record_cluster_holder changing
  changes_started=1
  log "identity: the namespace ${IDENTITY_NAMESPACE} and its NetworkPolicies (denied both ways, DNS and the edge and the Claims API on 8080 admitted)"
  kctl apply --server-side --force-conflicts -f "${IDENTITY_NAMESPACE_FILE}" >/dev/null
  ensure_secrets
  log "identity: the policies that let the edge's proxy pods and the Claims API's pods send to Keycloak"
  kctl apply --server-side --force-conflicts -f "${IDENTITY_PEERS_FILE}" >/dev/null
  realm_sha="$(realm_fingerprint)" || exit 1
  workload="$(fill_placeholder "${workload}" "${IDENTITY_REALM_PLACEHOLDER}" "${realm_sha}")" || exit 1
  log "identity: Keycloak (${KEYCLOAK_IMAGE}), its Service and the route for ${IDENTITY_ROUTE_HOST}; the pod's annotation holds the realm's fingerprint, so a realm that changed rolls it"
  kctl apply --server-side --force-conflicts -f - <<<"${workload}" >/dev/null
  kctl -n "${IDENTITY_NAMESPACE}" rollout status deployment/keycloak \
    --timeout="${IDENTITY_ROLLOUT_TIMEOUT}" >/dev/null ||
    die "Keycloak (deployment/keycloak in ${IDENTITY_NAMESPACE}) was not Ready in ${IDENTITY_ROLLOUT_TIMEOUT}. Read the pod before anything is deleted: kubectl -n ${IDENTITY_NAMESPACE} get pods; describe the pod. Three things to look for: the pod Pending with 'Insufficient memory' in its events (the node's allocatable memory minus the requests already made is under 700Mi even when the host has memory free: this script checks the host, not the scheduler); an image pull that is slow (the image is about 716 MiB); and a crash loop, read in the logs of the container and of the init container copy-quarkus, whose log saying a path is read-only is the one thing this was written to expect: infra/kind/manifests/identity.yaml names the one line to change (readOnlyRootFilesystem). Its start takes about 13 seconds on an idle machine"
  available="$(available_mb)" || exit 1
  record_cluster_holder ok
  changes_started=0
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

# The jq programs of `users` and `passwords`. Each picks fields of the people in the
# realm file, never of a service account (it has serviceAccountClientId) and never of
# a client, and gives one tab-separated line a person. A user with no name stops both
# (a null would shift the columns), and a user with no password stops the second: a
# half list of logins would read as a whole one.
# shellcheck disable=SC2016 # the $names in the program are jq's
readonly IDENTITY_USERS_FILTER='. as $realm | .users[]? | select(.serviceAccountClientId == null)
  | ((.groups // []) | map(ltrimstr("/"))) as $member
  | ([$realm.groups[]? | select(.name as $n | any($member[]; . == $n)) | .realmRoles[]?]
     + (.realmRoles // []) | unique) as $roles
  | [(.username // error("a user has no name")), "\(.firstName // "") \(.lastName // "")",
     ($member | if length == 0 then "(none)" else join(",") end),
     ($roles | if length == 0 then "(none)" else join(",") end)] | @tsv'
readonly IDENTITY_PASSWORDS_FILTER='.users[]? | select(.serviceAccountClientId == null)
  | [(.username // error("a user has no name")),
     (.credentials[0].value // error("a user has no password"))] | @tsv'

# read_cast FILTER: the realm file as the cluster holds it in the Secret
# keycloak-realm, through a pipe into jq with FILTER, on stdout. The Secret's content
# (which holds every password and client secret) is read once into a variable, is
# never printed and is never an argument; jq's own errors are dropped, because they
# can quote a value, and a fixed sentence is said instead. It stops, with a
# sentence, when the Secret is not there (the add-on was never made on this cluster),
# holds no realm file, is not a realm file, or gives no user. Run it in a command
# substitution and stop on its status, as realm_fingerprint is. What it gives is
# stripped of control characters (all but the tab and the newline), so that a name in
# the Secret cannot move the cursor or recolour the terminal of the person reading it
# (as `status` does with what it prints).
read_cast() {
  { set +x; } 2>/dev/null
  local encoded cast
  secret_exists "${IDENTITY_REALM_SECRET}" ||
    die "the Secret ${IDENTITY_REALM_SECRET} is not in the namespace ${IDENTITY_NAMESPACE}: the sign-in issuer add-on is not on this cluster (MERIDIAN_IDENTITY=keycloak make up makes it), so there is no cast to show"
  encoded="$(kctl -n "${IDENTITY_NAMESPACE}" get secret "${IDENTITY_REALM_SECRET}" \
    -o "jsonpath={.data.${IDENTITY_REALM_KEY//./\\.}}")" ||
    die "could not read the Secret ${IDENTITY_REALM_SECRET} (kubectl's error is above)"
  [[ -n "${encoded}" ]] ||
    die "the Secret ${IDENTITY_REALM_SECRET} holds no ${IDENTITY_REALM_KEY}"
  cast="$(printf '%s' "${encoded}" | base64 -d 2>/dev/null | jq -r "$1" 2>/dev/null |
    LC_ALL=C tr -cd '[:print:]\t\n')" ||
    die "the content of the Secret ${IDENTITY_REALM_SECRET} could not be read as a realm file with a name for every user (and a password, for passwords); nothing is printed"
  [[ -n "${cast}" ]] ||
    die "the realm in the Secret ${IDENTITY_REALM_SECRET} holds no users"
  printf '%s\n' "${cast}"
}

# users: read-only. The cast the cluster holds: user name, display name, role.
users() {
  { set +x; } 2>/dev/null
  local cast name display group role
  need_tools kubectl jq base64 awk timeout
  need_cluster
  cast="$(read_cast "${IDENTITY_USERS_FILTER}")" || exit 1
  printf '%-18s  %-22s  %-26s  %s\n' "USER NAME" "DISPLAY NAME" "GROUP" "ROLE"
  printf '%s\n' "${cast}" | while IFS=$'\t' read -r name display group role; do
    printf '%-18s  %-22s  %-26s  %s\n' "${name}" "${display}" "${group}" "${role}"
  done
}

# require_loopback_cluster: the server of the context kctl uses is on this machine.
# need_cluster trusts whatever infra/kind/kubeconfig says for the context, and
# require_local_docker reads the Docker endpoint only, so a kubeconfig of a developer's
# own, with the context's name and a remote server, would pass both and have a password
# printed from the wrong cluster. The URL is read from the kubeconfig file with
# `config view --minify` (no call to the cluster) and its host must be 127.0.0.1,
# localhost or [::1], exactly. Anything else, or a value that cannot be read, stops
# before a Secret is read, with a sentence that names the host.
require_loopback_cluster() {
  local server hostport host shown
  server="$(kctl config view --minify -o 'jsonpath={.clusters[0].cluster.server}' 2>/dev/null)" ||
    server=""
  if [[ "${server}" =~ ^[A-Za-z][A-Za-z0-9+.-]*://([^/?#]*) ]]; then
    hostport="${BASH_REMATCH[1]##*@}"
    if [[ "${hostport}" == \[* ]]; then
      host="${hostport%%]*}]"
    else
      host="${hostport%%:*}"
    fi
  else
    host=""
  fi
  case "${host}" in
    127.0.0.1 | localhost | "[::1]") return 0 ;;
  esac
  shown="$(printf '%s' "${host}" | LC_ALL=C tr -cd '[:print:]' | cut -c 1-80)"
  if [[ -z "${shown}" ]]; then
    die "could not read the server of the cluster's context from ${KUBECONFIG_FILE}, so it cannot be shown to be on this machine; nothing was read"
  fi
  die "the cluster's context in ${KUBECONFIG_FILE} points at the host ${shown}, which is not 127.0.0.1, localhost or [::1]: the passwords are of the local kind cluster only; nothing was read"
}

# passwords: the one place a password is printed, and only on request. The checks
# that cost nothing come first and read nothing: the variable, and the terminal. A
# pipe or a file would keep the passwords; a person who means it says so with
# MERIDIAN_IDENTITY_SHOW=1. Then a local engine, a kubeconfig whose server is on this
# machine (require_loopback_cluster) and a cluster that answers (the kubeconfig and
# the context of the local cluster alone, common.sh). The notice is printed after the
# read, so that a refusal prints nothing on standard output.
passwords() {
  { set +x; } 2>/dev/null
  local shown="${MERIDIAN_IDENTITY_SHOW:-}" cast name password
  [[ "${shown}" =~ ^1?$ ]] ||
    die "MERIDIAN_IDENTITY_SHOW must be empty or 1 (1 allows the passwords to go to a pipe or a file); nothing was read"
  if [[ "${shown}" != 1 && ! -t 1 ]]; then
    die "identity.sh passwords prints secrets and its output is not a terminal, so it prints nothing: a pipe or a log file would keep the passwords. Run it in a terminal (make identity-passwords), or set MERIDIAN_IDENTITY_SHOW=1 when you mean it; nothing was read"
  fi
  need_tools kubectl jq base64 awk timeout
  require_local_docker
  require_loopback_cluster
  need_cluster
  cast="$(read_cast "${IDENTITY_PASSWORDS_FILTER}")" || exit 1
  printf 'These are disposable test passwords of the local mock issuer (Keycloak on kind), made for this cluster; MERIDIAN_IDENTITY_ROTATE=1 replaces them.\n'
  printf '%s\n' "${cast}" | while IFS=$'\t' read -r name password; do
    printf '%-18s  %s\n' "${name}" "${password}"
  done
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
  users) users ;;
  passwords) passwords ;;
  *) usage ;;
esac
