# shellcheck shell=bash
# shellcheck disable=SC2034 # the constants are used by the scripts that source this file
# Shared by state.sh and foundation.sh. Source it; do not run it.
#
# Safety rules kept in one place:
#  - The subscription is pinned in infra/terraform/local.env (gitignored), which
#    state.sh writes. The owner has more than one subscription, so nothing here
#    relies on the az default: every ARM call passes
#    --subscription "${ARM_SUBSCRIPTION_ID}" (through azc), the way the kind
#    scripts pin --context. Terraform reads the same value from
#    ARM_SUBSCRIPTION_ID.
#  - The tenant is the pinned subscription's own (ARM_TENANT_ID), so the
#    provider, the backend and every token follow it, not the az default
#    account.
#  - Subscription, tenant and object IDs are never printed: the subscription is
#    logged by name, and every error string passes through redact, which
#    turns any GUID into <guid> and the base64 ID that holds them into
#    <client-config-id>.
#  - No secret exists here. Authentication is the owner's az login; the state
#    account has shared keys off.

TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TF_DIR
readonly FOUNDATION_DIR="${TF_DIR}/foundation"
readonly LOCAL_ENV="${TF_DIR}/local.env"

readonly STATE_RESOURCE_GROUP=rg-meridian-tfstate
readonly STATE_LOCATION=swedencentral
readonly STATE_CONTAINER=tfstate

# Keep az quiet about upgrades and preview warnings; errors still print.
export AZURE_CORE_ONLY_SHOW_ERRORS=true

# redact: stdin to stdout with every GUID (subscription, tenant, object and
# role IDs, in either case) replaced by <guid>, and the ID of Terraform's
# azurerm_client_config data source by <client-config-id>. That ID is the
# base64 of "clientConfigs/clientId=...;objectId=...;subscriptionId=...;
# tenantId=...", so it holds the same IDs where no GUID pattern sees them, and
# Terraform prints it on every plan and apply ("Read complete ... [id=...]").
# The second pattern starts with the base64 of "clientConfigs/c", which is the
# same whatever follows, and takes the rest of the base64 run.
redact() {
  sed -E \
    -e 's/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/<guid>/g' \
    -e 's#Y2xpZW50Q29uZmlncy9j[A-Za-z0-9+/]*={0,2}#<client-config-id>#g'
}

log() { printf '==> %s\n' "$*"; }
die() {
  printf 'error: %s\n' "$*" | redact >&2
  exit 1
}

need_tools() {
  local tool
  for tool in "$@"; do
    command -v "${tool}" >/dev/null 2>&1 || die "${tool} is not installed or not on PATH"
  done
}

# azc ARGS...: az pinned to the subscription in ARM_SUBSCRIPTION_ID. Use it for
# every ARM call. Standard output is untouched (callers parse it); standard
# error is redacted, so an error message never carries an ID into the terminal
# or a log. The exit status is az's.
azc() {
  local errors status=0
  errors="$(mktemp)"
  az "$@" --subscription "${ARM_SUBSCRIPTION_ID:?ARM_SUBSCRIPTION_ID is not set}" 2>"${errors}" || status=$?
  redact <"${errors}" >&2
  rm -f "${errors}"
  return "${status}"
}

# pin_tenant: export ARM_TENANT_ID as the tenant of the pinned subscription. The
# owner has subscriptions in more than one tenant; the az default account may
# belong to another one. The Terraform provider and backend read ARM_TENANT_ID.
pin_tenant() {
  ARM_TENANT_ID="$(az account show --subscription "${ARM_SUBSCRIPTION_ID:?ARM_SUBSCRIPTION_ID is not set}" \
    --query tenantId -o tsv 2>/dev/null)" && [[ -n "${ARM_TENANT_ID}" ]] ||
    die "cannot read the tenant of the pinned subscription; run 'az login'"
  export ARM_TENANT_ID
}

# base64url_decode: stdin (base64url, unpadded, as in a JWT) to stdout. Portable
# to macOS's bash 3.2 and base64: translate the alphabet, pad to a multiple of
# four, then decode (-d, or -D on a base64 that only knows that).
base64url_decode() {
  local data
  data="$(tr -- '-_' '+/')"
  while ((${#data} % 4 != 0)); do data+="="; done
  printf '%s' "${data}" | { base64 -d 2>/dev/null || base64 -D; }
}

# signed_in_object_id: print the object ID of the signed-in user from the oid
# claim of an Entra token for the pinned subscription's tenant. `az ad
# signed-in-user show` cannot take --subscription and follows the az default
# account's tenant. Neither the token nor the ID is logged.
signed_in_object_id() {
  local token oid
  token="$(az account get-access-token --subscription "${ARM_SUBSCRIPTION_ID:?ARM_SUBSCRIPTION_ID is not set}" \
    --query accessToken -o tsv 2>/dev/null)" && [[ -n "${token}" ]] ||
    die "cannot get an Entra token for the pinned subscription; run 'az login'"
  oid="$(cut -d. -f2 <<<"${token}" | base64url_decode | jq -r '.oid // empty' 2>/dev/null)" || true
  [[ -n "${oid}" ]] || die "the Entra token has no oid claim; state.sh expects 'az login' with a user account"
  printf '%s' "${oid}"
}

# name_suffix ID: six hex characters of the SHA-1 of the subscription ID. The
# Terraform local.suffix in foundation/main.tf computes the same value, so the
# state account here and the vault and OpenAI names there share one suffix.
name_suffix() { printf '%s' "$1" | shasum -a 1 | cut -c1-6; }

# state_account_name ID: storage account names are 3 to 24 lowercase letters
# and digits, globally unique. 12 + 6 = 18 characters.
state_account_name() { printf 'stmeridiantf%s' "$(name_suffix "$1")"; }

# Read infra/terraform/local.env, export ARM_SUBSCRIPTION_ID and
# TF_STATE_STORAGE_ACCOUNT, and prove the signed-in az can see that
# subscription. Logs the subscription name, never its ID.
load_local_env() {
  [[ -f "${LOCAL_ENV}" ]] || die "no ${LOCAL_ENV}; run 'make azure-state' first"
  unset ARM_SUBSCRIPTION_ID TF_STATE_STORAGE_ACCOUNT
  # shellcheck source=/dev/null
  . "${LOCAL_ENV}"
  [[ -n "${ARM_SUBSCRIPTION_ID:-}" ]] || die "ARM_SUBSCRIPTION_ID is not set in ${LOCAL_ENV}; rerun 'make azure-state'"
  [[ -n "${TF_STATE_STORAGE_ACCOUNT:-}" ]] || die "TF_STATE_STORAGE_ACCOUNT is not set in ${LOCAL_ENV}; rerun 'make azure-state'"
  [[ "${TF_STATE_STORAGE_ACCOUNT}" == "$(state_account_name "${ARM_SUBSCRIPTION_ID}")" ]] ||
    die "TF_STATE_STORAGE_ACCOUNT in ${LOCAL_ENV} does not belong to ARM_SUBSCRIPTION_ID; rerun 'make azure-state'"
  export ARM_SUBSCRIPTION_ID TF_STATE_STORAGE_ACCOUNT

  local name
  name="$(az account show --subscription "${ARM_SUBSCRIPTION_ID}" --query name -o tsv 2>/dev/null)" ||
    die "az cannot see the subscription pinned in ${LOCAL_ENV}; run 'az login' with the right account"
  pin_tenant
  log "subscription: ${name}"
}
