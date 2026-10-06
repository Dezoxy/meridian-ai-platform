#!/usr/bin/env bash
# Drive the AWS module (infra/terraform/aws): `make aws-validate`,
# `make aws-plan`, `make aws-apply`, `make aws-destroy` (S036).
#   validate  format check, init with no backend, validate. Needs no AWS
#             credential (it is run with none) and no local file, and never
#             calls the aws CLI.
#   plan      init, then plan into aws.tfplan; changes nothing in AWS. Records
#             the commit and the time beside the plan.
#   apply     apply exactly that saved plan, then remove it (creates AWS
#             resources, which cost money). Refuses a plan that is not this
#             tree's or is older than thirty minutes.
#   destroy   remove everything the module created. Terraform asks its own
#             question, and this refuses unless standard input is a terminal.
#             That stops an accident and a plain shell. It does NOT stop a
#             session that gives itself a pseudo-terminal: what keeps a session
#             from removing the environment is that no session holds the
#             credentials (infra/terraform/aws/README.md, "What stops a
#             session, and what does not").
# plan, apply and destroy first read infra/terraform/local.env-aws (gitignored:
# the pattern infra/terraform/local.env* covers it, and the hook that stops a
# `cat` of a .env file reads the name as one, which `local.env.aws` escapes).
# The file is READ, never run: KEY=value lines for the four known keys, each
# value of a fixed alphabet, owned by the caller and closed to group and others.
# They refuse unless the signed-in AWS account is the one pinned there, as the
# Azure scripts pin the subscription. The file holds four values: the expected
# account, the Region, the one address that may reach the cluster's public
# endpoint, and the e-mail address of the budget's alerts. This script prints
# none of them, and a refusal about the file names a line NUMBER and nothing of
# the line. Terraform's own output is another matter: it prints a variable that
# is not sensitive, which is why the module marks the address and the e-mail
# sensitive. Everything printed from Terraform goes through redact (common.sh)
# as well, a filter that knows the shapes of an AWS account number, an ARN,
# credentials, an e-mail address, an IPv4 address and the host of a cluster or
# a database. The aws CLI's own words are never printed: only a sentence of this
# script says what to do.
#
# Terraform and the aws CLI are each run with an environment this script chose
# (run_clean below), not the caller's: TF_LOG*, TF_WORKSPACE, TF_DATA_DIR,
# TF_CLI_CONFIG_FILE, TF_REATTACH_PROVIDERS, TF_CLI_ARGS*, any other TF_VAR_* and
# AWS_ENDPOINT_URL* never reach either. Every Terraform call is made without
# colour, so that no escape sequence stands between redact and a number.
set +x
set -euo pipefail
umask 077 # the plan, its record and the state are written under this
# A trace would print the four values, so tracing is off from here and nothing
# may turn it back on. (SHELLOPTS is read-only in bash and cannot be unset; it
# only matters at start-up, and run_clean does not pass it on.)
unset BASH_XTRACEFD PS4

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly AWS_MODULE_DIR="${TF_DIR}/aws"
readonly AWS_LOCAL_ENV="${TF_DIR}/local.env-aws"
readonly PLAN_FILE=aws.tfplan
readonly PLAN_RECORD_FILE=aws.tfplan.meta
# A plan older than this is not applied: the account, the quotas and the
# prices it was made against may have changed, and the owner read it a while
# ago. Thirty minutes is the length of one plan-read-apply sitting.
readonly PLAN_MAX_AGE_SECONDS=1800
# The state is a file under the caller's home, never in a checkout: the
# sessions of this repository work in worktrees that are deleted.
readonly STATE_DIR_UNDER_HOME=.local/state/meridian-aws
readonly STATE_FILE_NAME=aws.tfstate
readonly LOCAL_KEYS=(MERIDIAN_AWS_ACCOUNT_ID MERIDIAN_AWS_REGION MERIDIAN_AWS_ENDPOINT_CIDR MERIDIAN_AWS_BUDGET_EMAIL)

# What each program is given. Both lists are the whole of it; the names
# TF_VAR_* are the ones load_aws_env exports and no other, because every other
# TF_VAR_* of the caller is unset below.
#  ENV_BASE: the path, the home (the aws CLI's sign-in cache and Terraform's
#    plugin directory are under it), the temporary directory, the terminal and
#    the locale.
#  ENV_AWS: the names AWS's page "Configuring environment variables for the AWS
#    CLI" documents for credentials and for where they are read from:
#    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, AWS_PROFILE,
#    AWS_CONFIG_FILE, AWS_SHARED_CREDENTIALS_FILE. A sign-in with `aws sso
#    login` needs the profile and the home: the page names no variable of SSO's
#    own. AWS_REGION is set by this script from the local file, and AWS_PAGER
#    empty, so no pager waits for a key. Left out on purpose: AWS_ENDPOINT_URL*
#    (they would send the calls somewhere else than AWS), AWS_CA_BUNDLE,
#    AWS_DEFAULT_REGION, AWS_ROLE_ARN and AWS_WEB_IDENTITY_TOKEN_FILE, the
#    metadata-service settings, and the proxy variables (a machine that needs
#    one adds it here, on purpose).
readonly ENV_BASE=(PATH HOME TMPDIR TERM LANG LANGUAGE LC_ALL LC_CTYPE LC_MESSAGES)
readonly ENV_AWS=(AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE AWS_CONFIG_FILE AWS_SHARED_CREDENTIALS_FILE AWS_REGION)

# Whatever TF_VAR_* the caller has is gone before load_aws_env exports the
# module's own: a value the caller set for another variable (an instance type)
# must not reach a plan.
while IFS= read -r name; do
  unset "${name}"
done < <(compgen -e | grep '^TF_VAR_' || true)

usage() {
  printf 'usage: %s <validate|plan|apply|destroy>\n' "$(basename "$0")" >&2
  exit 2
}

# run_clean KIND COMMAND...: the command with an environment of this script's
# choosing and nothing else (env -i). KIND is "plain" (ENV_BASE alone: no AWS
# credential, no module variable), "cli" (ENV_BASE, ENV_AWS and an empty
# AWS_PAGER: what the aws CLI needs to sign in and say who it is) or "signed"
# (all of that and the TF_VAR_* that load_aws_env exported: what a plan, an
# apply and a removal need).
run_clean() {
  local kind="$1" name
  shift
  local -a pass=()
  for name in "${ENV_BASE[@]}"; do
    if [[ -n "${!name+x}" ]]; then pass+=("${name}=${!name}"); fi
  done
  if [[ "${kind}" != plain ]]; then
    for name in "${ENV_AWS[@]}"; do
      if [[ -n "${!name+x}" ]]; then pass+=("${name}=${!name}"); fi
    done
    pass+=(AWS_PAGER=)
  fi
  if [[ "${kind}" == signed ]]; then
    while IFS= read -r name; do
      pass+=("${name}=${!name}")
    done < <(compgen -e | grep '^TF_VAR_' || true)
  fi
  env -i "${pass[@]}" "$@"
}

# terraform in the module's directory, without colour. -chdir also makes the
# plan file path relative to that directory. The sub-command comes first because
# -no-color is an option of the sub-command. "plain" has no AWS credential:
# format, init, validate and the state's list need none.
tf_plain() {
  local sub="$1"
  shift
  run_clean plain terraform -chdir="${AWS_MODULE_DIR}" "${sub}" -no-color "$@"
}
tf_signed() {
  local sub="$1"
  shift
  run_clean signed terraform -chdir="${AWS_MODULE_DIR}" "${sub}" -no-color "$@"
}
tf_state_list() { run_clean plain terraform -chdir="${AWS_MODULE_DIR}" state list -no-color; }

# git with the environment of this script's choosing: a GIT_DIR or a
# GIT_WORK_TREE of the caller must not point it at another repository.
git_here() { run_clean plain git -C "${AWS_MODULE_DIR}" "$@"; }

# What the local file may hold and what a value may be made of: digits, letters
# and the few characters of an account number, a Region, an address with a prefix
# length and an e-mail address. No quote, space, dollar sign, backtick or
# semicolon: nothing a shell would act on.
local_file_access() {
  [[ -O "${AWS_LOCAL_ENV}" ]] ||
    die "${AWS_LOCAL_ENV} is not owned by the user running this; it must be yours and mode 600"
  local mode
  mode="$(stat -c '%a' "${AWS_LOCAL_ENV}" 2>/dev/null || stat -f '%Lp' "${AWS_LOCAL_ENV}")"
  mode="000${mode}"
  [[ "${mode: -2}" == 00 ]] ||
    die "the mode of ${AWS_LOCAL_ENV} lets group or others in; run: chmod 600 ${AWS_LOCAL_ENV}"
}

# Read the local file (never run it), check it holds the four values, and export
# what the module and the aws CLI read. The names after TF_VAR_ are the module's
# variables (aws/variables.tf): region, api_access_cidr (the one address that
# may reach the cluster's public endpoint, a /32), budget_email and
# expected_account_id (which the module's provider enforces). The last three have
# no default, so a plan would stop to ask for them without these exports;
# tests/meridian/test_aws_script.py fails when the script and the module drift.
load_aws_env() {
  [[ -f "${AWS_LOCAL_ENV}" ]] ||
    die "no ${AWS_LOCAL_ENV}; create it as infra/terraform/aws/README.md describes (four values, mode 600)"
  local_file_access
  unset "${LOCAL_KEYS[@]}"
  local keys line number=0 seen=" " key value
  keys="$(
    IFS='|'
    printf '%s' "${LOCAL_KEYS[*]}"
  )"
  local pattern="^(${keys})=([A-Za-z0-9@._/+-]*)\$"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    number=$((number + 1))
    if [[ -z "${line}" || "${line}" == \#* ]]; then continue; fi
    [[ "${line}" =~ ${pattern} ]] ||
      die "line ${number} of ${AWS_LOCAL_ENV} is not KEY=value for one of the four keys infra/terraform/aws/README.md lists (nothing of the line is printed); the file is read, not run"
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    [[ "${seen}" != *" ${key} "* ]] ||
      die "line ${number} of ${AWS_LOCAL_ENV} gives a key a second time"
    seen="${seen}${key} "
    printf -v "${key}" '%s' "${value}"
  done <"${AWS_LOCAL_ENV}"
  for key in "${LOCAL_KEYS[@]}"; do
    [[ -n "${!key:-}" ]] ||
      die "${key} is not set in ${AWS_LOCAL_ENV}; infra/terraform/aws/README.md says what the file holds"
  done
  [[ "${MERIDIAN_AWS_ACCOUNT_ID}" =~ ^[0-9]{12}$ ]] ||
    die "MERIDIAN_AWS_ACCOUNT_ID in ${AWS_LOCAL_ENV} is not a twelve-digit account number"
  # One Region for the CLI, the provider and the variable; the default of the
  # caller's profile is not consulted (and is not passed on).
  unset AWS_DEFAULT_REGION
  export AWS_REGION="${MERIDIAN_AWS_REGION}"
  export TF_VAR_region="${MERIDIAN_AWS_REGION}"
  export TF_VAR_api_access_cidr="${MERIDIAN_AWS_ENDPOINT_CIDR}"
  export TF_VAR_budget_email="${MERIDIAN_AWS_BUDGET_EMAIL}"
  export TF_VAR_expected_account_id="${MERIDIAN_AWS_ACCOUNT_ID}"
}

# The account the caller is signed in to must be the pinned one. Neither number
# is printed, and neither is anything the aws CLI says: its errors carry the
# account.
require_pinned_account() {
  local caller
  caller="$(run_clean cli aws sts get-caller-identity --query Account --output text 2>/dev/null)" &&
    [[ -n "${caller}" ]] ||
    die "cannot read the caller's AWS account; sign in (for example 'aws sso login') to the account you mean, then try again"
  [[ "${caller}" == "${MERIDIAN_AWS_ACCOUNT_ID}" ]] ||
    die "the signed-in AWS account is not the one pinned in ${AWS_LOCAL_ENV}; sign in to the right account, or correct the pin if it is wrong"
  log "AWS account: the pinned one"
}

# Nothing in the module's directory may change what a plan does without the
# plan showing why: Terraform loads these files by itself, and git ignores the
# variable files, so a reviewer would not see one. Only the KIND is named.
refuse_files_that_change_the_plan() {
  local path name
  for path in "${AWS_MODULE_DIR}"/*; do
    if [[ ! -e "${path}" && ! -L "${path}" ]]; then continue; fi
    name="$(basename "${path}")"
    case "${name}" in
      terraform.tfvars | terraform.tfvars.json | *.auto.tfvars | *.auto.tfvars.json)
        die "the module directory holds a variable file, which Terraform loads by itself and which would change the plan unseen (terraform.tfvars, terraform.tfvars.json, *.auto.tfvars and *.auto.tfvars.json are the ones it loads); remove it and give values through the local file"
        ;;
      override.tf | override.tf.json | *_override.tf | *_override.tf.json)
        die "the module directory holds an override file, which Terraform merges over the module's own and which would change the plan unseen (override.tf, override.tf.json, *_override.tf and *_override.tf.json are the ones it merges); remove it and change the module itself, in a commit"
        ;;
    esac
  done
}

# The state's directory under home, made private, and the path Terraform is
# given at init. validate never calls this: it inits with no backend.
prepare_state() {
  [[ -n "${HOME:-}" ]] ||
    die "HOME is not set, and the state of the AWS environment is kept in a directory under it (infra/terraform/aws/README.md, State)"
  STATE_DIR="${HOME}/${STATE_DIR_UNDER_HOME}"
  mkdir -p "${STATE_DIR}"
  chmod 700 "${STATE_DIR}"
  STATE_PATH="${STATE_DIR}/${STATE_FILE_NAME}"
}

# init with the local state's path, and with the committed lock file as the
# only authority on the provider.
init_with_state() {
  log "terraform init"
  tf_plain init -input=false -lockfile=readonly -backend-config="path=${STATE_PATH}" 2>&1 | redact ||
    die "terraform init failed"
}

# The names of what the state holds, without data sources, one a line.
count_state() {
  local out status=0
  out="$(tf_state_list 2>&1)" || status=$?
  if ((status != 0)); then
    # Terraform says so when there is no state file at all; anything else (a
    # lock, a permission) is not "empty".
    if [[ "${out}" == *"No state file was found"* ]]; then
      printf '0\n'
      return 0
    fi
    return 1
  fi
  printf '%s\n' "${out}" | grep -E '^[^[:space:]]+$' | grep -c -v '^data\.' || true
}

current_commit() {
  git_here rev-parse HEAD 2>/dev/null ||
    die "cannot read the git commit of the module; this has to run in a checkout of the repository"
}

# Anything in the module's directory that git does not have as committed: a
# changed file, a new one. Ignored files (the plan, the state) do not show.
module_changes() {
  git_here status --porcelain --untracked-files=all -- . 2>/dev/null ||
    die "cannot read the git status of the module; this has to run in a checkout of the repository"
}

drop_plan() { rm -f "${AWS_MODULE_DIR}/${PLAN_FILE}" "${AWS_MODULE_DIR}/${PLAN_RECORD_FILE}"; }

cmd_validate() {
  log "terraform fmt -check"
  tf_plain fmt -check -diff 2>&1 | redact ||
    die "terraform fmt found a file to format; run: terraform -chdir=infra/terraform/aws fmt"
  log "terraform init -backend=false"
  # readonly: the committed lock file decides the provider, and a check does not
  # rewrite it.
  tf_plain init -backend=false -input=false -lockfile=readonly 2>&1 | redact ||
    die "terraform init failed"
  log "terraform validate"
  tf_plain validate 2>&1 | redact ||
    die "terraform validate failed"
}

cmd_plan() {
  refuse_files_that_change_the_plan
  load_aws_env
  prepare_state
  require_pinned_account
  # Each is read into a variable first: a die inside $(...) would end only the
  # substitution, not the script.
  local commit changes
  commit="$(current_commit)"
  changes="$(module_changes)"
  if [[ -n "${changes}" ]]; then
    log "warning: the module directory has uncommitted changes; this plan cannot be applied until they are committed and the plan is made again"
  fi
  drop_plan # a plan that fails must not leave an older one to be applied
  init_with_state
  log "terraform plan"
  # Through redact, with pipefail keeping terraform's exit status. A plan that
  # failed may still have written a file; it is not one to apply.
  tf_signed plan -input=false -out="${PLAN_FILE}" 2>&1 | redact || {
    drop_plan
    die "terraform plan failed; nothing was changed in AWS"
  }
  printf 'commit=%s\ntime=%s\n' "${commit}" "$(date +%s)" >"${AWS_MODULE_DIR}/${PLAN_RECORD_FILE}"
  log "review the plan above (network, cluster, registry, database, secret, budget), then: make aws-apply"
}

# A saved plan is applied only if it was made at the commit that is checked out
# now, from a module directory that has not changed since, and not too long ago.
# Otherwise it is dropped (it is of no use any more) and the sentence says to
# plan again.
require_a_plan_that_is_this_trees_and_fresh() {
  local record="${AWS_MODULE_DIR}/${PLAN_RECORD_FILE}" line_commit line_time
  local planned_commit planned_time now age commit changes
  stale() {
    drop_plan
    die "$1; run 'make aws-plan' again"
  }
  [[ -f "${record}" ]] || stale "the saved plan has no record beside it of the commit and the time it was made at"
  { IFS= read -r line_commit && IFS= read -r line_time && ! IFS= read -r _; } <"${record}" ||
    stale "the record beside the saved plan is not a commit and a time"
  [[ "${line_commit}" =~ ^commit=([0-9a-f]{40})$ ]] ||
    stale "the record beside the saved plan is not a commit and a time"
  planned_commit="${BASH_REMATCH[1]}"
  [[ "${line_time}" =~ ^time=([0-9]{1,12})$ ]] ||
    stale "the record beside the saved plan is not a commit and a time"
  planned_time="${BASH_REMATCH[1]}"
  commit="$(current_commit)"
  [[ "${planned_commit}" == "${commit}" ]] ||
    stale "the saved plan was made at another commit than the one checked out now"
  changes="$(module_changes)"
  [[ -z "${changes}" ]] ||
    stale "the module directory has uncommitted changes that the saved plan was not made from"
  now="$(date +%s)"
  age=$((now - planned_time))
  ((age >= 0 && age <= PLAN_MAX_AGE_SECONDS)) ||
    stale "the saved plan is too old (more than $((PLAN_MAX_AGE_SECONDS / 60)) minutes) or dated in the future"
}

# Applies the saved plan and nothing else, so what runs is what was reviewed.
# The plan and its record are removed either way: after a failed apply the plan
# is stale and Terraform would refuse it.
cmd_apply() {
  [[ -f "${AWS_MODULE_DIR}/${PLAN_FILE}" ]] || die "no ${PLAN_FILE}; run 'make aws-plan' first"
  refuse_files_that_change_the_plan
  require_a_plan_that_is_this_trees_and_fresh
  load_aws_env
  require_pinned_account
  log "terraform apply ${PLAN_FILE}"
  local status=0
  tf_signed apply -input=false "${PLAN_FILE}" 2>&1 | redact || status=$?
  drop_plan
  ((status == 0)) || die "terraform apply failed (exit ${status}); run 'make aws-plan' again"
  log "applied. It bills by the hour until: make aws-destroy"
}

# No automatic approval and no -input=false: Terraform asks for its own "yes" on
# this terminal. The question passes through redact line by line, so the last
# words of the prompt ("Enter a value:", with no newline after them) show only
# once the answer has been typed; the question above them shows at once.
#
# Init runs first (a removal does not run it by itself), and "removed" is printed
# only when the state held something before and holds nothing after: over an
# empty state Terraform would remove nothing and say it was done.
cmd_destroy() {
  [[ -t 0 ]] ||
    die "destroy needs a terminal: run 'make aws-destroy' yourself, in a terminal (this check stops an accident and a plain shell, not a session that makes itself a terminal; infra/terraform/aws/README.md says what does)"
  refuse_files_that_change_the_plan
  load_aws_env
  prepare_state
  require_pinned_account
  init_with_state
  local before after
  before="$(count_state)" ||
    die "cannot read the state (terraform state list failed); nothing was touched, and the console is where to look"
  ((before > 0)) ||
    die "the state holds nothing, so Terraform would remove nothing: the file is ${STATE_PATH}. If the state was lost (a deleted checkout, another machine, another user), what it described may still exist and bill: look in the console, in the Region of the local file (infra/terraform/aws/README.md, Removal, 'If the state is lost')"
  log "the state holds ${before} resources"
  log "terraform destroy: Terraform asks for the confirmation"
  tf_signed destroy 2>&1 | redact
  after="$(count_state)" ||
    die "cannot read the state after the removal; look in the console for what is left"
  ((after == 0)) ||
    die "the state still holds ${after} resources, so the removal is not finished; run 'make aws-destroy' again"
  log "removed. Look at the console for what is left: infra/terraform/aws/README.md, Removal"
}

[[ $# -eq 1 ]] || usage
case "$1" in
  validate)
    need_tools terraform
    cmd_validate
    ;;
  plan)
    need_tools terraform aws git
    cmd_plan
    ;;
  apply)
    need_tools terraform aws git
    cmd_apply
    ;;
  destroy)
    need_tools terraform aws
    cmd_destroy
    ;;
  *) usage ;;
esac
