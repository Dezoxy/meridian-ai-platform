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
#
# aws.sh's output meets the same filter, which also knows AWS's shapes (S036):
# an ARN becomes <arn>, whole, and runs first so that the account inside it goes
# with it; an access key identifier (the documented prefixes and then sixteen
# uppercase letters and digits, or seventeen for the 21-character identifier of
# a role, a user or a group) becomes <access-key-id>; a host under
# eks.amazonaws.com or rds.amazonaws.com (a cluster's endpoint, a database's)
# and the cluster's identity issuer (oidc.eks.<region>.amazonaws.com, which has
# the label order the other way round) become <host>; and a twelve-digit number
# that is a whole token, which is an account number, becomes <account>. A whole
# token is bounded by anything but a letter or a digit, so a thirteen-digit
# timestamp in milliseconds is left alone and any other twelve-digit integer in
# the output is hidden too, which costs a reader nothing. sed has no word
# boundary on macOS, so the bounds are captured, and a match consumes the
# character after it: the account rule runs twice so that two numbers side by
# side are both found.
#
# The credential shapes below are removed only where they follow the label that
# gives them away (a secret access key after secret_access_key, a session token
# after session_token or X-Amz-Security-Token, an encoded authorization failure
# message after its words) or are a parameter of a name (Signature=): a run of
# letters with no label cannot be told from any other. Terraform prints none of
# these in a plan; its debug log (TF_LOG, which aws.sh does not pass on) and an
# AWS error can. An e-mail address becomes <email>. An IPv4 address, with or
# without a prefix length, becomes <ip>; the rule has no word boundary (sed has
# none on macOS) so it also hides a four-part version number such as 1.2.3.4 and
# the VPC's 10.0.0.0/16, which costs a reader of Terraform's and the aws CLI's
# output nothing, where a miss would leak an address. An ARN may hold commas (a
# session name can), so one comma followed by more ARN characters stays in it.
#
# A plan of instances (the self-managed cluster's module, S079) prints more, and
# redact knows it: compressed user data (a gzip stream in base64 starts with
# H4sI, and holds the boot scripts with the cluster's address in them) becomes
# <user-data>, ahead of every rule but the three of the Azure paragraph below
# that must come first, so that no other rule cuts into it; the identifier of
# an instance, an image, a VPC, a subnet, a security group and its rules, a route
# table and its association, an internet gateway, an Elastic IP's allocation and
# association, a network interface (and its attachment) and a volume becomes
# <resource-id> (the prefix, a hyphen, and eight or seventeen lower-case
# hexadecimal digits, a whole token: the rule runs twice, as the account's does,
# for two side by side); and a host written with dashes that embeds an address
# (ip- or ec2-, four numbers, an optional domain) becomes <host>, which the
# dotted-quad rule cannot see. The instance profile's, the role's and the
# parameter's ARN are <arn> already, with the account inside them.
#
# An Azure platform module's plan, outputs and errors show five more shapes (S020),
# and each rule has its comment, a line of the script, beside it. The labelled
# values of a kubeconfig run first, so that the label decides the mask; then a
# certificate or key in base64, and a token of three parts, both before the user
# data rule (a run of base64 can hold H4sI and be cut short by it, which would
# leave the start of a key) and before the session token rule (it stops at a
# dot, which would leave the token's second and third parts). A host is removed
# whole before the suffix of the name in it is, because with the suffix gone the
# domain has no label left to start from. The hosts are those of the AKS API
# server, PostgreSQL, the registry, the cluster's identity issuer, a vault's
# private link, the model account and storage, each with an optional port, so
# the fixed private link zone names (privatelink.vaultcore.azure.net) are
# <host> too. vault.azure.net is not among them: tests/test_terraform_redact.py
# holds https://example.vault.azure.net/ unchanged, and a vault of the module
# has kv-meridian-<suffix> in its name, which the suffix rule covers. The suffix
# is the six hex digits the module's names end with (name_suffix below).
redact() {
  sed -E \
    -e '# a kubeconfig value after its label, which stays (not after aws_session_token, the AWS rule masks that)' \
    -e 's#(^|[^A-Za-z0-9_])((certificate[-_]authority[-_]data|client[-_]certificate[-_]data|client[-_]key[-_]data|token)"?[[:space:]]*[:=][[:space:]]*"?)[A-Za-z0-9+/=._-]{16,}#\1\2<kubeconfig-value>#g' \
    -e '# a certificate or key in base64: the base64 of five dashes and BEGIN, and the rest of the run' \
    -e 's#LS0tLS1CRUdJTi[A-Za-z0-9+/]{16,}={0,2}#<pem>#g' \
    -e '# a token of three base64url parts (an Entra or Kubernetes token) that begins eyJ' \
    -e 's#eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*#<token>#g' \
    -e '# compressed user data: a gzip stream in base64 holds the boot scripts' \
    -e 's#H4sI[A-Za-z0-9+/]{40,}={0,2}#<user-data>#g' \
    -e 's#arn:aws[a-z-]*:[A-Za-z0-9-]+:[a-z0-9-]*:[0-9]*:[^][:space:]"'\''<>,()]+(,[^][:space:]"'\''<>,()]+)*#<arn>#g' \
    -e 's#([Ss][Ee][Cc][Rr][Ee][Tt][_-]?[Aa][Cc][Cc][Ee][Ss][Ss][_-]?[Kk][Ee][Yy])([[:space:]=:"'\'']{1,8})[A-Za-z0-9/+=]{16,}#\1\2<secret-access-key>#g' \
    -e 's#([Ss][Ee][Ss][Ss][Ii][Oo][Nn][_-]?[Tt][Oo][Kk][Ee][Nn]|[Ss][Ee][Cc][Uu][Rr][Ii][Tt][Yy][_-]?[Tt][Oo][Kk][Ee][Nn])([[:space:]=:"'\'']{1,8})[A-Za-z0-9/+=%_-]{16,}#\1\2<session-token>#g' \
    -e 's#([Ss]ignature=)[A-Za-z0-9%/+=_-]{16,}#\1<signature>#g' \
    -e 's#([Ee]ncoded authorization failure message:?[[:space:]]*)[A-Za-z0-9_+/=-]{16,}#\1<encoded-message>#g' \
    -e 's/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/<guid>/g' \
    -e 's#Y2xpZW50Q29uZmlncy9j[A-Za-z0-9+/]*={0,2}#<client-config-id>#g' \
    -e 's/(ABIA|ACCA|AGPA|AIDA|AIPA|AKIA|ANPA|ANVA|APKA|AROA|ASCA|ASIA)[A-Z0-9]{16,17}/<access-key-id>/g' \
    -e 's#[A-Za-z0-9.-]+\.(eks|rds)\.amazonaws\.com#<host>#g' \
    -e 's#oidc\.eks\.[a-z0-9-]+\.amazonaws\.com(/id/[A-Za-z0-9]+)?#<host>#g' \
    -e '# a host under an Azure service the module uses (not vault.azure.net, see above), with its port' \
    -e 's#[A-Za-z0-9.-]+\.(azmk8s\.io|postgres\.database\.azure\.com|azurecr\.io|oic\.prod-aks\.azure\.com|vaultcore\.azure\.net|openai\.azure\.com|core\.windows\.net)(:[0-9]+)?#<host>#g' \
    -e '# the six hex digits that end the names of the vault, server, registry, state account and model account' \
    -e 's#(kv-meridian-|psql-meridian-|crmeridian|stmeridiantf|oai-meridian-[a-z]{2,4}-)[0-9a-f]{6}([^0-9a-f]|$)#\1<suffix>\2#g' \
    -e 's#(^|[^A-Za-z0-9])(ip|ec2)-[0-9]{1,3}(-[0-9]{1,3}){3}(\.[A-Za-z0-9.-]+)?#\1<host>#g' \
    -e 's#(^|[^A-Za-z0-9])(i|ami|vpc|subnet|sgr|sg|rtbassoc|rtb|igw|eipalloc|eipassoc|eni-attach|eni|vol)-[0-9a-f]{8}([0-9a-f]{9})?([^A-Za-z0-9]|$)#\1<resource-id>\4#g' \
    -e 's#(^|[^A-Za-z0-9])(i|ami|vpc|subnet|sgr|sg|rtbassoc|rtb|igw|eipalloc|eipassoc|eni-attach|eni|vol)-[0-9a-f]{8}([0-9a-f]{9})?([^A-Za-z0-9]|$)#\1<resource-id>\4#g' \
    -e 's#(^|[^0-9.])[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?#\1<ip>#g' \
    -e 's/[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}/<email>/g' \
    -e 's/(^|[^A-Za-z0-9])[0-9]{12}([^A-Za-z0-9]|$)/\1<account>\2/g' \
    -e 's/(^|[^A-Za-z0-9])[0-9]{12}([^A-Za-z0-9]|$)/\1<account>\2/g'
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
