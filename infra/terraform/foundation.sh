#!/usr/bin/env bash
# Drive the Azure foundation (infra/terraform/foundation): `make azure-plan`,
# `make azure-apply`, `make azure-smoke`, `make registry-snapshot`.
#   init   terraform init against the remote state that state.sh created
#   plan   init, then plan into foundation.tfplan; changes nothing in Azure
#   apply  apply exactly that saved plan, then remove it (creates Azure resources)
#   smoke  prove the deployed foundation: the live models match Terraform's
#          outputs and stay in the EU, key authentication is off, and each
#          account answers one chat completion and one embedding through Entra
#          ID. Read-only apart from a few tiny model calls (well under EUR 0.01).
#   outputs print the model deployments from Terraform's outputs as JSON, only
#          the fields the registry compares (no account names or endpoints):
#          the snapshot the registry is checked against (T-12). Read-only.
#   gateway-live  two real chat calls and one embedding call through the Model
#          Gateway in live mode on this laptop, one chat call with the first
#          candidate made to fail, with this az login and a throwaway
#          PostgreSQL (needs Docker). Read-only
#          in Azure apart from those calls (well under EUR 0.01).
# Everything printed from az and Terraform is GUID-redacted (redact in common.sh).
# Prints one PASS or FAIL line per smoke check and exits non-zero on any FAIL.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly PLAN_FILE=foundation.tfplan
readonly OPENAI_API_VERSION=2024-10-21
readonly TOKEN_RESOURCE=https://cognitiveservices.azure.com

failures=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() {
  printf 'FAIL  %s\n' "$*" | redact
  failures=$((failures + 1))
}

usage() {
  printf 'usage: %s <init|plan|apply|smoke|outputs|gateway-live>\n' "$(basename "$0")" >&2
  exit 2
}

# terraform in the foundation directory. -chdir also makes the plan file path
# relative to that directory.
tf() { terraform -chdir="${FOUNDATION_DIR}" "$@"; }

# The account name and the subscription are the two values the backend block in
# versions.tf leaves out: they are derived per owner, not stored in the code.
# Quiet on success; the redacted output is shown when init fails.
tf_init() {
  load_local_env
  log "terraform init"
  local out
  if ! out="$(tf init -input=false \
    -backend-config="storage_account_name=${TF_STATE_STORAGE_ACCOUNT}" \
    -backend-config="subscription_id=${ARM_SUBSCRIPTION_ID}" 2>&1)"; then
    printf '%s\n' "${out}" | redact >&2
    die "terraform init failed"
  fi
}

cmd_plan() {
  tf_init
  log "terraform plan"
  umask 077 # the plan file embeds resource IDs, like local.env
  # Through redact, with pipefail keeping terraform's exit status.
  tf plan -input=false -out="${PLAN_FILE}" 2>&1 | redact
  log "review the plan above (accounts, deployments, budget, vault), then: make azure-apply"
}

# Applies the saved plan and nothing else, so what runs is what was reviewed.
# The plan file is removed either way: after a failed apply it is stale and
# Terraform would refuse it.
cmd_apply() {
  [[ -f "${FOUNDATION_DIR}/${PLAN_FILE}" ]] || die "no ${PLAN_FILE}; run 'make azure-plan' first"
  load_local_env
  log "terraform apply ${PLAN_FILE}"
  local status=0
  tf apply -input=false "${PLAN_FILE}" 2>&1 | redact || status=$?
  rm -f "${FOUNDATION_DIR}/${PLAN_FILE}"
  ((status == 0)) || die "terraform apply failed (exit ${status}); run 'make azure-plan' again"
  log "applied. Next: make azure-smoke"
}

# ── smoke ────────────────────────────────────────────────────────────────────
# Azure returns locations lower-case without spaces ("swedencentral"); compare
# after removing spaces and case.
normalise() { printf '%s' "$1" | tr -d ' ' | tr '[:upper:]' '[:lower:]'; }

# capture CMD...: run CMD with standard output in ${cap_out} and standard error
# (GUID-redacted) in ${cap_err}, kept apart so that a warning never lands in JSON
# that jq parses. Returns CMD's status.
capture() {
  local errors status=0
  errors="$(mktemp)"
  cap_out="$("$@" 2>"${errors}")" || status=$?
  cap_err="$(redact <"${errors}")"
  rm -f "${errors}"
  return "${status}"
}

# api_post URL BODY: POST JSON with the Entra token. The token reaches curl as a
# config line on stdin, so it never appears in a process listing (same idea as
# gcurl in infra/kind/smoke.sh). Sets api_status and api_body; returns 0 only on
# HTTP 200.
api_post() {
  local out
  if ! out="$(printf 'header = "Authorization: Bearer %s"\n' "${token}" |
    curl -q --noproxy '*' -sS -m 30 -K - -H 'Content-Type: application/json' \
      -d "$2" -w '\n%{http_code}' "$1" 2>&1)"; then
    api_status=000
    api_body="${out}"
    return 1
  fi
  api_status="${out##*$'\n'}"
  api_body="${out%$'\n'*}"
  [[ "${api_status}" == 200 ]]
}

# The status and Azure's error code and message, nothing else. Never the token.
api_failure() {
  local detail
  detail="$(jq -r '.error | "\(.code // "unknown"): \(.message // "no message")"' \
    <<<"${api_body}" 2>/dev/null || printf '%s' "${api_body}")"
  detail="$(printf '%s' "${detail}" | tr '\n' ' ' | cut -c1-300)"
  if [[ "${api_status}" == 401 || "${api_status}" == 403 ]]; then
    detail="${detail} (a new role assignment can take several minutes to reach the model; rerun)"
  fi
  printf 'HTTP %s: %s' "${api_status}" "${detail}"
}

# check_account ACCOUNT: the account is where Terraform says and in an EU region
# (checked here, not trusted from Terraform), key authentication is off (T-18),
# and each deployment on it is the model, version and SKU that Terraform's
# outputs promise, on a SKU that is not Global (T-12, hard rule 3).
check_account() {
  local account=$1 location auth
  if ! capture azc cognitiveservices account show --resource-group "${resource_group}" \
    --name "${account}" -o json; then
    fail "account ${account}: az could not read it: ${cap_err}"
    return
  fi
  location="$(jq -r '.location' <<<"${cap_out}")"
  auth="$(jq -r '.properties.disableLocalAuth | tostring' <<<"${cap_out}")"
  if [[ "${auth}" == true ]]; then
    pass "account ${account}: key authentication is off (disableLocalAuth true)"
  else
    fail "account ${account}: disableLocalAuth is ${auth}, expected true"
  fi
  case "$(normalise "${location}")" in
    swedencentral | westeurope) pass "account ${account}: ${location} is an EU region" ;;
    *) fail "account ${account}: ${location} is not swedencentral or westeurope" ;;
  esac

  local key dep model version sku want_location live_model live_version live_sku
  while IFS=$'\t' read -r key dep model version sku want_location; do
    if ! capture azc cognitiveservices account deployment show --resource-group "${resource_group}" \
      --name "${account}" --deployment-name "${dep}" -o json; then
      fail "deployment ${key}: az could not read it: ${cap_err}"
      continue
    fi
    live_model="$(jq -r '.properties.model.name' <<<"${cap_out}")"
    live_version="$(jq -r '.properties.model.version' <<<"${cap_out}")"
    live_sku="$(jq -r '.sku.name' <<<"${cap_out}")"
    if [[ "${live_sku}" == Global* ]]; then
      fail "deployment ${key}: SKU ${live_sku} may process data outside the EU"
    elif [[ "${live_model}" == "${model}" && "${live_version}" == "${version}" &&
      "${live_sku}" == "${sku}" &&
      "$(normalise "${location}")" == "$(normalise "${want_location}")" ]]; then
      pass "deployment ${key}: ${live_model} ${live_version} on ${live_sku} in ${location}"
    else
      fail "deployment ${key}: expected ${model} ${version} on ${sku} in ${want_location}; live is ${live_model} ${live_version} on ${live_sku} in ${location}"
    fi
  done < <(jq -r --arg account "${account}" 'to_entries[]
    | select(.value.account_name == $account)
    | [.key, .value.deployment_name, .value.model_name, .value.model_version,
       .value.sku_name, .value.location] | @tsv' <<<"${deployments}")
}

# deployments_for ACCOUNT PURPOSE: the names of the deployments with that
# purpose (chat or embedding) on the account, one per line, from Terraform's
# outputs. An account can hold two chat deployments (S042).
deployments_for() {
  jq -r --arg account "$1" --arg purpose "$2" \
    '.[] | select(.account_name == $account and .purpose == $purpose) | .deployment_name' \
    <<<"${deployments}"
}

# A deployment name becomes part of a URL the token is sent to.
url_safe_name() {
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]]
}

# check_calls ACCOUNT ENDPOINT: one chat completion or one embedding to every
# deployment the outputs name for those purposes.
check_calls() {
  local account=$1 endpoint="${2%/}/" chats embeddings name model tokens dimensions
  # The token is valid for any Cognitive Services account, and the endpoint
  # comes from Terraform state, so it goes only to an Azure OpenAI endpoint.
  if [[ ! "${endpoint}" =~ ^https://[a-z0-9-]+\.openai\.azure\.com/$ ]]; then
    fail "calls ${account}: ${endpoint} is not an Azure OpenAI endpoint; no token sent"
    return
  fi
  chats="$(deployments_for "${account}" chat)"
  embeddings="$(deployments_for "${account}" embedding)"

  [[ -n "${chats}" ]] || fail "chat ${account}: no deployment with purpose chat in the outputs"
  while IFS= read -r name; do
    [[ -n "${name}" ]] || continue
    if ! url_safe_name "${name}"; then
      fail "chat ${account}: a deployment name in the outputs is not a plain name; no token sent"
    elif api_post "${endpoint}openai/deployments/${name}/chat/completions?api-version=${OPENAI_API_VERSION}" \
      '{"messages":[{"role":"user","content":"Reply with the single word: ok"}],"max_tokens":5,"temperature":0}'; then
      model="$(jq -r '.model // "unknown"' <<<"${api_body}")"
      tokens="$(jq -r '.usage.total_tokens // "unknown"' <<<"${api_body}")"
      pass "chat ${account}: ${name} answered (model ${model}, ${tokens} tokens)"
    else
      fail "chat ${account}: ${name}: $(api_failure)"
    fi
  done <<<"${chats}"

  [[ -n "${embeddings}" ]] || fail "embedding ${account}: no deployment with purpose embedding in the outputs"
  while IFS= read -r name; do
    [[ -n "${name}" ]] || continue
    if ! url_safe_name "${name}"; then
      fail "embedding ${account}: a deployment name in the outputs is not a plain name; no token sent"
    elif api_post "${endpoint}openai/deployments/${name}/embeddings?api-version=${OPENAI_API_VERSION}" \
      '{"input":"meridian smoke"}'; then
      dimensions="$(jq -r '.data[0].embedding | length' <<<"${api_body}")"
      if [[ "${dimensions}" =~ ^[0-9]+$ ]] && ((dimensions > 0)); then
        pass "embedding ${account}: ${name} returned a ${dimensions}-dimension vector"
      else
        fail "embedding ${account}: ${name}: no vector in the response"
      fi
    else
      fail "embedding ${account}: ${name}: $(api_failure)"
    fi
  done <<<"${embeddings}"
}

cmd_smoke() {
  need_tools az terraform jq curl
  # The outputs live in the remote state, so the backend must be initialised.
  tf_init
  resource_group="$(tf output -raw resource_group_name 2>/dev/null)" ||
    die "no Terraform outputs; run 'make azure-plan' and 'make azure-apply' first"
  deployments="$(tf output -json openai_deployments 2>/dev/null)" ||
    die "Terraform has no openai_deployments output; run 'make azure-apply' first"
  local accounts
  accounts="$(jq -r '[.[].account_name] | unique | .[]' <<<"${deployments}")"
  [[ -n "${accounts}" ]] || die "openai_deployments is empty; has the foundation been applied?"

  local account
  while IFS= read -r account; do
    check_account "${account}"
  done <<<"${accounts}"

  if ! token="$(az account get-access-token --resource "${TOKEN_RESOURCE}" \
    --subscription "${ARM_SUBSCRIPTION_ID}" --query accessToken -o tsv 2>/dev/null)" ||
    [[ -z "${token}" ]]; then
    fail "token: could not get an Entra ID token for ${TOKEN_RESOURCE}; run 'az login'"
  else
    while IFS= read -r account; do
      check_calls "${account}" "$(jq -r --arg account "${account}" \
        '[.[] | select(.account_name == $account)][0].endpoint' <<<"${deployments}")"
    done <<<"${accounts}"
  fi

  if ((failures > 0)); then
    printf '\n%s check(s) FAILED\n' "${failures}"
    exit 1
  fi
  printf '\nAll checks passed.\n'
}

# ── outputs ──────────────────────────────────────────────────────────────────
# The snapshot is committed to a public repository, so it keeps an allowlist of
# fields, the ones the registry compares: a field added to the output later,
# such as an account name or a resource ID, stays out until it is added here.
# The GUID check is the backstop. Logs and Terraform's warnings go to stderr,
# redacted; stdout is the JSON alone, ready to redirect into a file.
readonly SNAPSHOT_FIELDS='{capacity, deployment_name, location, model_name, model_version, purpose, sku_name}'
readonly GUID_PATTERN='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

cmd_outputs() {
  tf_init >&2
  local out errors status=0 snapshot
  errors="$(mktemp)"
  out="$(tf output -json openai_deployments 2>"${errors}")" || status=$?
  redact <"${errors}" >&2
  rm -f "${errors}"
  ((status == 0)) || die "terraform output failed (exit ${status})"
  snapshot="$(jq -S "map_values(${SNAPSHOT_FIELDS})" <<<"${out}" 2>/dev/null)" ||
    die "the openai_deployments output is not a map of deployments"
  jq -e 'length > 0' <<<"${snapshot}" >/dev/null ||
    die "the openai_deployments output has no deployments"
  [[ ! "${snapshot}" =~ ${GUID_PATTERN} ]] || die "the snapshot would contain a GUID; refusing to print it"
  printf '%s\n' "${snapshot}"
}

# ── gateway-live ─────────────────────────────────────────────────────────────
# Real chat and embedding calls through the Model Gateway on this laptop (S010,
# S042, S045). The endpoints come from Terraform's outputs and the token from
# this az login, so nothing is stored; both reach the test through the
# environment only. An
# endpoint holds the account name and a failed login can name the signed-in
# user, so the test's output is filtered for any Azure OpenAI host, the account
# name and anything shaped like an email address, as well as for GUIDs.
redact_account() {
  sed -E \
    -e 's/[A-Za-z0-9-]+\.openai\.azure\.com/<account>.openai.azure.com/g' \
    -e 's/oai-meridian-[a-z0-9-]+/oai-meridian-<redacted>/g' \
    -e 's/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<email>/g'
}

cmd_gateway_live() {
  tf_init
  local deployments endpoints
  deployments="$(tf output -json openai_deployments 2>/dev/null)" ||
    die "Terraform has no openai_deployments output; run 'make azure-apply' first"
  # "<location key>/<deployment>" => endpoint becomes {"<location key>": endpoint}.
  endpoints="$(jq -ce 'with_entries(.key |= split("/")[0] | .value |= .endpoint) | select(length > 0)' \
    <<<"${deployments}" 2>/dev/null)" ||
    die "the openai_deployments output has no endpoints"
  log "two chat calls and one embedding call through the gateway as tenant development (synthetic text)"
  MERIDIAN_LIVE_AZURE=1 \
    MERIDIAN_AZURE_OPENAI_ENDPOINTS="${endpoints}" \
    MERIDIAN_AZURE_TENANT_ID="${ARM_TENANT_ID}" \
    make -C "${TF_DIR}/../.." --no-print-directory pytest-db \
    PYTEST_ARGS='tests/meridian/gateway/test_live_azure.py -s -q -p no:cacheprovider' 2>&1 |
    redact | redact_account
}

[[ $# -eq 1 ]] || usage
case "$1" in
  init)
    need_tools terraform az
    tf_init
    ;;
  plan)
    need_tools terraform az
    cmd_plan
    ;;
  apply)
    need_tools terraform az
    cmd_apply
    ;;
  smoke) cmd_smoke ;;
  outputs)
    need_tools terraform az jq
    cmd_outputs
    ;;
  gateway-live)
    need_tools terraform az jq docker uv make
    cmd_gateway_live
    ;;
  *) usage ;;
esac
