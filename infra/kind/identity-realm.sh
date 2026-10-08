#!/usr/bin/env bash
# Write the staff realm of the mock issuer (Keycloak) and the credentials that go
# with it: `identity-realm.sh OUTPUT_DIR REDIRECT_URI WEB_ORIGIN` (S021, Y2a).
# Nothing here touches a cluster or a container; it makes two files:
#
#   OUTPUT_DIR/meridian-staff-realm.json   what Keycloak imports at start
#                                          (`start-dev --import-realm`, the file
#                                          in /opt/keycloak/data/import)
#   OUTPUT_DIR/secrets.env                 KEY=value lines: the password of each
#                                          test user and the secret of each
#                                          client, for a Kubernetes Secret
#
# Both are mode 600 and are secret-bearing: Keycloak reads a user's password and
# a client's secret from the realm file, so the realm file holds them too (once
# each, at the place Keycloak reads; a test checks it). The values are made here
# with `openssl rand`, a new set at every run, and never committed, never
# printed, never a command-line argument (jq reads them from a pipe). The
# directory must not be tracked by git: inside a work tree it must be ignored
# (.gitignore ignores infra/kind/.identity/), or the script refuses.
#
#   REDIRECT_URI  where the pages' sign-in returns: the callback of the Claims
#                 API, exact, or ending in one `*` for a prefix. The callback's
#                 path is the pages' contract (Y3's); on kind the host is the
#                 Claims API's (infra/kind/values/meridian.yaml), so the value
#                 assumed is http://claims.meridian.localhost:8088/auth/callback*
#   WEB_ORIGIN    the pages' origin: scheme, host and port, no path
#
# The realm `meridian-staff` (S021 is the staff half; the claimants' realm is
# step S089's: a second realm is one more spec function and one more call of
# write_realm below, and its secrets join the same file under their own prefix):
#   - four realm roles: platform-admin, agent-developer, adjuster, auditor, and
#     a test user per role (test-<role>) holding only that role
#   - meridian-claims-web: the pages. Confidential; the authorization-code flow
#     with PKCE required (S256); direct grants, implicit flow and service
#     account off
#   - meridian-scripts: for `make demo`, smoke and the evaluation client. Client
#     credentials only; its service account holds adjuster and auditor
#   - both clients' ACCESS tokens carry a top-level list claim `roles` (also in
#     the ID token) and an `aud` of meridian-claims-api, which is no client's id
#     (the audience mapper leaves the ID token alone: its aud stays the client).
#     Each client is scoped to the four roles (fullScopeAllowed off), so neither
#     claim lists Keycloak's own default roles
#   - access token 300 s; SSO session idle 1800 s and at most 28800 s (the
#     app's own cap is 8 hours: the issuer's session is never longer)
#   - each user has an id derived from the realm and the user name (no secret,
#     the same at every run), so the token's `sub` is the same after Keycloak
#     starts again on an empty database; see stable_id

# The jq programs below are single-quoted on purpose: their $names are jq's.
# shellcheck disable=SC2016
set -euo pipefail
{ set +x; } 2>/dev/null # a `bash -x` run must not trace a password

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly SECRETS_FILE_NAME=secrets.env
readonly API_AUDIENCE=meridian-claims-api
readonly STAFF_ROLES='["platform-admin","agent-developer","adjuster","auditor"]'
readonly SCRIPTS_ROLES='["adjuster","auditor"]'
readonly SECRET_BYTES=24

# Shared by the two jq programs: the name of a secret in secrets.env.
readonly JQ_DEFS='
def envname: ascii_upcase | gsub("[^A-Z0-9]"; "_");
def password_key($realm; $user): "\($realm | envname)_USER_\($user | envname)_PASSWORD";
def secret_key($realm; $client): "\($realm | envname)_CLIENT_\($client | envname)_SECRET";
'

# The names of the secrets a spec needs, one per line.
readonly JQ_KEYS='
.realm as $realm
| (.users[] | password_key($realm; .username)),
  (.clients[] | secret_key($realm; .clientId))
'

# The user names of a spec, the service accounts' included, one per line.
readonly JQ_USERNAMES='
.users[].username, (.clients[] | select(.flow == "credentials") | "service-account-\(.clientId)")
'

# The realm file from a spec, the secrets (--rawfile secrets: KEY=value lines) and
# the users' ids (--argjson ids: user name to id).
readonly JQ_REALM='
def secrets_map:
  $secrets | split("\n") | map(select(length > 0) | capture("^(?<k>[A-Z0-9_]+)=(?<v>.*)$"))
  | map({(.k): .v}) | add;
def roles_mapper: {
  name: "roles", protocol: "openid-connect",
  protocolMapper: "oidc-usermodel-realm-role-mapper", consentRequired: false,
  config: {
    "claim.name": "roles", "jsonType.label": "String", "multivalued": "true",
    "access.token.claim": "true", "id.token.claim": "true",
    "userinfo.token.claim": "true", "introspection.token.claim": "true"
  }
};
def audience_mapper($audience): {
  name: "api-audience", protocol: "openid-connect",
  protocolMapper: "oidc-audience-mapper", consentRequired: false,
  config: {
    "included.custom.audience": $audience,
    "access.token.claim": "true", "id.token.claim": "false",
    "introspection.token.claim": "true"
  }
};
def lookup($s; $key): $s[$key] // error("nothing generated for \($key)");
def client($spec; $s): {
  clientId: .clientId, name: .clientId, enabled: true, protocol: "openid-connect",
  publicClient: false, clientAuthenticatorType: "client-secret",
  secret: lookup($s; secret_key($spec.realm; .clientId)),
  implicitFlowEnabled: false, fullScopeAllowed: false,
  protocolMappers: [roles_mapper, audience_mapper($spec.audience)]
} + (if .flow == "code" then {
  standardFlowEnabled: true, directAccessGrantsEnabled: false,
  serviceAccountsEnabled: false,
  redirectUris: .redirectUris, webOrigins: .webOrigins,
  attributes: {"pkce.code.challenge.method": "S256", "post.logout.redirect.uris": "+"}
} else {
  standardFlowEnabled: false, directAccessGrantsEnabled: false,
  serviceAccountsEnabled: true, redirectUris: [], webOrigins: []
} end);
$spec as $spec | secrets_map as $s
| {
    realm: $spec.realm, enabled: true, displayName: $spec.realm,
    sslRequired: "external", registrationAllowed: false,
    resetPasswordAllowed: false, loginWithEmailAllowed: false,
    accessTokenLifespan: $spec.accessTokenLifespan,
    ssoSessionIdleTimeout: $spec.ssoSessionIdleTimeout,
    ssoSessionMaxLifespan: $spec.ssoSessionMaxLifespan,
    roles: {realm: [$spec.roles[] | {name: ., description: "Meridian role \(.)"}]},
    users: (
      [$spec.users[] | {
        id: lookup($ids; .username),
        username: .username, enabled: true, emailVerified: true,
        firstName: "Test", lastName: .role,
        email: "\(.username)@meridian.test", requiredActions: [],
        credentials: [{
          type: "password", temporary: false,
          value: lookup($s; password_key($spec.realm; .username))
        }],
        realmRoles: [.role]
      }]
      + [$spec.clients[] | select(.flow == "credentials") | {
        id: lookup($ids; "service-account-\(.clientId)"),
        username: "service-account-\(.clientId)", enabled: true,
        serviceAccountClientId: .clientId, realmRoles: .roles
      }]
    ),
    clients: [$spec.clients[] | client($spec; $s)],
    scopeMappings: [$spec.clients[] | {client: .clientId, roles: $spec.roles}]
  }
'

usage() {
  die "usage: identity-realm.sh OUTPUT_DIR REDIRECT_URI WEB_ORIGIN (see the comment at the top of the script)"
}

# The spec of the staff realm: what is chosen, no secret in it.
staff_spec() {
  jq -n --arg redirect "$1" --arg origin "$2" --arg audience "${API_AUDIENCE}" \
    --argjson roles "${STAFF_ROLES}" --argjson scripts_roles "${SCRIPTS_ROLES}" '
    {
      realm: "meridian-staff", audience: $audience, roles: $roles,
      accessTokenLifespan: 300, ssoSessionIdleTimeout: 1800, ssoSessionMaxLifespan: 28800,
      users: [$roles[] | {username: "test-\(.)", role: .}],
      clients: [
        {clientId: "meridian-claims-web", flow: "code",
         redirectUris: [$redirect], webOrigins: [$origin]},
        {clientId: "meridian-scripts", flow: "credentials", roles: $scripts_roles}
      ]
    }'
}

# The absolute path of a path that may not exist yet (its nearest existing
# directory is resolved, so a link in it is followed).
absolute_path() {
  local path=$1 rest="" head
  [[ ${path} == /* ]] || path="${PWD}/${path}"
  path="${path%/}"
  while [[ ! -d ${path} ]]; do
    rest="/$(basename "${path}")${rest}"
    path="$(dirname "${path}")"
  done
  head="$(cd -P "${path}" && pwd -P)"
  printf '%s%s' "${head%/}" "${rest}"
}

# Refuse a directory that git would track: inside a work tree, both files must
# be ignored.
require_untracked() {
  local file ancestor="$1"
  while [[ ! -d ${ancestor} ]]; do ancestor="$(dirname "${ancestor}")"; done
  git -C "${ancestor}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
  for file in "$1/${SECRETS_FILE_NAME}" "$1/$2"; do
    git -C "${ancestor}" check-ignore -q -- "${file}" ||
      die "$1 is inside a git work tree and ${file##*/} is not ignored by .gitignore; use a directory git ignores (infra/kind/.identity) or one outside the repository"
  done
}

# stable_id NAME: the id a user is given, a UUID-shaped text derived from NAME
# alone. Keycloak makes a random id for a user the import does not name, and the
# id is the token's `sub`: a Keycloak that starts again on an empty database (the
# Deployment has no volume) would give the same person a new subject at every
# start, and a subject kept in a row (S021's actor, S089's owner) would stop
# matching. A name-derived id is no secret and the same at every run.
stable_id() {
  local hex
  hex="$(printf '%s' "$1" | openssl dgst -sha256 -r | cut -c1-32)"
  printf '%s-%s-%s-%s-%s' "${hex:0:8}" "${hex:8:4}" "${hex:12:4}" "${hex:16:4}" "${hex:20:12}"
}

# write_realm SPEC OUT_DIR REALM_FILE: make the secrets the spec needs, write the
# realm file, and append the secrets to the secrets file.
write_realm() {
  local spec=$1 out=$2 file=$3 key value name secrets="" ids="{}" realm_name realm
  realm_name="$(jq -r .realm <<<"${spec}")"
  while IFS= read -r key; do
    value="$(openssl rand -hex "${SECRET_BYTES}")"
    [[ ${value} =~ ^[0-9a-f]{48}$ ]] || die "openssl rand did not give ${SECRET_BYTES} random bytes"
    secrets+="${key}=${value}"$'\n'
  done < <(jq -r "${JQ_DEFS}${JQ_KEYS}" <<<"${spec}")
  while IFS= read -r name; do
    ids="$(jq -c --arg name "${name}" --arg id "$(stable_id "${realm_name}/${name}")" \
      '. + {($name): $id}' <<<"${ids}")"
  done < <(jq -r "${JQ_USERNAMES}" <<<"${spec}")
  realm="$(jq -n --argjson spec "${spec}" --argjson ids "${ids}" \
    --rawfile secrets <(printf '%s' "${secrets}") \
    "${JQ_DEFS}${JQ_REALM}")" || die "could not build the realm file"
  : >"${out}/${file}"
  chmod 600 "${out}/${file}"
  printf '%s\n' "${realm}" >"${out}/${file}"
  printf '%s' "${secrets}" >>"${out}/${SECRETS_FILE_NAME}"
}

main() {
  (($# == 3)) || usage
  local out redirect=$2 origin=$3 realm_file=meridian-staff-realm.json
  [[ ${redirect} =~ ^https?://[^[:space:]*]+\*?$ ]] ||
    die "REDIRECT_URI must be an http(s) URL with no space, and at most one '*', at its end"
  [[ ${origin} =~ ^https?://[^/[:space:]*]+$ ]] ||
    die "WEB_ORIGIN must be scheme, host and port only: no path, no trailing slash, no '*'"
  need_tools jq openssl
  out="$(absolute_path "$1")"
  [[ ! -e ${out} || -d ${out} ]] || die "${out} exists and is not a directory"
  require_untracked "${out}" "${realm_file}"
  if [[ ! -d ${out} ]]; then
    mkdir -p "${out}"
    chmod 700 "${out}" # only a directory made here: an existing one is not touched
  fi
  : >"${out}/${SECRETS_FILE_NAME}"
  chmod 600 "${out}/${SECRETS_FILE_NAME}"
  write_realm "$(staff_spec "${redirect}" "${origin}")" "${out}" "${realm_file}"
  log "wrote ${out}/${realm_file} and ${out}/${SECRETS_FILE_NAME} (mode 600)"
  log "the passwords and client secrets are new and are in ${SECRETS_FILE_NAME} only; they are not printed"
}

main "$@"
