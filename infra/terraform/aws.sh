#!/usr/bin/env bash
# Drive the AWS modules: the managed one (infra/terraform/aws) by `make
# aws-validate`, `make aws-plan`, `make aws-apply` and `make aws-destroy` (S036),
# and the self-managed one (infra/terraform/aws-kubeadm, S079) by this script's
# own command line: it has the validate and scan targets and no target that plans,
# applies or removes it yet. Its validate door takes the Azure platform module
# (infra/terraform/azure, S020) too, which has the validate and scan targets and
# nothing that plans, applies or removes it.
#   validate  format check, init with no backend, validate. Needs no AWS
#             credential (it is run with none) and no local file, and never
#             calls the aws CLI. It takes the name of a module, one of aws
#             (the default), gcp, aws-kubeadm, gcp-kubeadm and azure. `validate
#             gcp` runs the same three commands on infra/terraform/gcp (S078),
#             `validate aws-kubeadm` on infra/terraform/aws-kubeadm (S079, the
#             self-managed cluster's module), `validate gcp-kubeadm` on
#             infra/terraform/gcp-kubeadm (S079, its Google Cloud twin) and
#             `validate azure` on infra/terraform/azure (S020, the Azure
#             platform module), with no credential of any cloud. The Google Cloud
#             modules are never planned and never applied: validate is the one
#             command that takes the words gcp and gcp-kubeadm. The Azure
#             platform module has no plan, apply or removal path yet either:
#             validate is the one command that takes the word azure, and the
#             other three refuse it with the usage line.
#             Terraform's data directory, where init puts the providers, is a
#             private directory under ~/.cache/meridian-terraform named for the
#             module, never .terraform in the module's directory
#             (data_dir_for_validate in planguard.sh): a validate leaves nothing in the
#             module.
#   plan      init, then plan into the module's saved plan (aws.tfplan, or
#             aws-kubeadm.tfplan); changes nothing in AWS. Records the module,
#             the commit, the time and the plan file's SHA-256 beside the plan,
#             unless the module's directory has uncommitted changes: the plan is
#             shown then, and no record is written, so apply refuses it.
#   apply     apply exactly that saved plan, then remove it (creates AWS
#             resources, which cost money). Refuses a plan that is not this
#             module's, is not this tree's, is not the file the record names, or
#             is older than thirty minutes.
#   destroy   remove everything the module created. Terraform asks its own
#             question, and this refuses unless standard input is a terminal.
#             That stops an accident and a plain shell. It does NOT stop a
#             session that gives itself a pseudo-terminal: what keeps a session
#             from removing the environment is that no session holds the
#             credentials (infra/terraform/aws/README.md, "What stops a
#             session, and what does not").
# plan, apply and destroy take one word after the command, from a closed list of
# one: `plan aws-kubeadm`, `apply aws-kubeadm` and `destroy aws-kubeadm` work on
# the self-managed module, and with no word they work on the managed one. Any
# other word (aws and gcp included, and a path, and a second word) is refused
# before any program runs; gcp with a sentence that says why. The module is a row
# of literals (select_module below): its directory, its saved plan and the
# record's name, its state's directory and file under the caller's home (the two
# modules never share a state), its variables and the sentences that name it.
# plan, apply and destroy first read infra/terraform/local.env-aws (gitignored:
# the pattern infra/terraform/local.env* covers it, and the hook that stops a
# `cat` of a .env file reads the name as one, which `local.env.aws` escapes).
# The one file serves both modules.
# The file is READ, never run: KEY=value lines for the four known keys, each
# value of a fixed alphabet, owned by the caller and closed to group and others.
# They refuse unless the signed-in AWS account is the one pinned there, as the
# Azure scripts pin the subscription. The file holds four values: the expected
# account, the Region, the one address that may reach the cluster's public
# endpoint, and the e-mail address of the budget's alerts. This script prints
# none of them, and a refusal about the file names a line NUMBER and nothing of
# the line. Terraform's own output is another matter: it prints a variable that
# is not sensitive, which is why the modules mark the address and the e-mail
# sensitive. Everything printed from Terraform goes through redact (common.sh)
# as well, a filter that knows the shapes of an AWS account number, an ARN,
# credentials, an e-mail address, an IPv4 address, the host of a cluster or a
# database, and what a plan of instances prints (instance, image and network
# identifiers, a host written with dashes that embeds an address, compressed user
# data). The aws CLI's own words are never printed: only a sentence of this
# script says what to do.
#
# Terraform and the aws CLI are each run with an environment this script chose
# (run_clean below), not the caller's: TF_LOG*, TF_WORKSPACE, TF_DATA_DIR,
# TF_CLI_CONFIG_FILE, TF_REATTACH_PROVIDERS, TF_CLI_ARGS*, any other TF_VAR_* and
# AWS_ENDPOINT_URL* never reach either. (validate gives Terraform a TF_DATA_DIR of
# its own, the one name of the script's choosing beyond the base list; the
# caller's never.) Every Terraform call is made without colour, so that no escape
# sequence stands between redact and a number.
set +x
set -euo pipefail
umask 077 # the plan, its record and the state are written under this
# A trace would print the four values, so tracing is off from here and nothing
# may turn it back on. (SHELLOPTS is read-only in bash and cannot be unset; it
# only matters at start-up, and run_clean does not pass it on.)
unset BASH_XTRACEFD PS4

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
# The protections that name no cloud (the clean environment's callers, the
# default workspace, the plan record, the checks of the tree), in a file a
# wrapper for another cloud sources too.
# shellcheck source=planguard.sh
. "$(dirname "${BASH_SOURCE[0]}")/planguard.sh"

readonly AWS_LOCAL_ENV="${TF_DIR}/local.env-aws"
# The one README that says what the script stops and what it does not, and what
# the local file holds: the managed module's. The self-managed module reads the
# same file (one account, one Region, one address, one e-mail address), so the
# sentences about the file and about the script's rules name this README for
# both; the sentences about a module's state and its removal name the module's
# own (MODULE_README, below).
readonly SHARED_README=infra/terraform/aws/README.md
# What the messages of planguard.sh call the environment this script builds (the
# refusal when HOME is not set, in prepare_state).
readonly ENVIRONMENT_WORDS="the AWS environment"
# Where another workspace's state would be, in the refusal of a workspace other
# than the default (require_default_workspace, in planguard.sh): the state of
# this script is a local file, so it would be in the checkout.
readonly WORKSPACE_WORDS="Terraform would keep the state in terraform.tfstate.d/ in the checkout and not in the state under your home"
readonly LOCAL_KEYS=(MERIDIAN_AWS_ACCOUNT_ID MERIDIAN_AWS_REGION MERIDIAN_AWS_ENDPOINT_CIDR MERIDIAN_AWS_BUDGET_EMAIL)
# The Regions of EU member states the module accepts (hard rule 3), the list in
# the validation of "region" in aws/variables.tf; a test holds the two equal. The
# local file's Region goes to the aws CLI as given, so it is checked here first.
readonly ALLOWED_REGIONS=(eu-central-1 eu-west-1 eu-west-3 eu-north-1 eu-south-1 eu-south-2)
# The longest a value in the local file may be: the length of a host name, far
# above anything the four values need, and short enough that no value ends in a
# raw shell error (a 200 KB one did).
readonly VALUE_MAX_LENGTH=253

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
  printf '       %s validate [aws|gcp|aws-kubeadm|gcp-kubeadm|azure]  (the module to check, aws if none)\n' "$(basename "$0")" >&2
  printf '       %s <plan|apply|destroy> [aws-kubeadm]  (the self-managed module; the managed one if no word)\n' "$(basename "$0")" >&2
  exit 2
}

# The Google Cloud modules have no path that creates anything, by the owner's
# decision (S078): validate is the one command that takes their names.
refuse_gcp() {
  printf 'error: the Google Cloud modules are scaffolds that are never planned, applied or removed (the owner decided so in S078: no path of this script creates anything there); validate is the one command that takes the words gcp and gcp-kubeadm\n' >&2
  exit 2
}

# The table of the modules this script drives: select_module WORD sets one row.
# Every value of a row is a literal written here; no path, file name or sentence
# is ever built from the word the caller typed (the dispatch at the end of this
# file matches the word against the closed list and calls this with a literal).
# A row is set once and then read-only: one command works on one module.
#   MODULE_DIR     the module's directory, which every check of the tree reads
#   MODULE_REL     that directory as the sentences name it
#   MODULE_NAME    the module's name in the record beside a saved plan
#   PLAN_FILE, PLAN_RECORD_FILE   the saved plan and its record, in MODULE_DIR
#   STATE_DIR_UNDER_HOME, STATE_FILE_NAME   the module's state, under the
#     caller's home and never in a checkout (the sessions of this repository
#     work in worktrees that are deleted); the two modules never share either
#   MODULE_README  the README the sentences on the state and the removal name
#   CMD_PLAN, CMD_APPLY, CMD_DESTROY   how a sentence tells the owner to run the
#     command again: a make target for the managed module, and the script's own
#     command line for the self-managed one, which has no target yet
#   PLAN_REVIEW    what the owner reads in the plan before applying it
#   MODULE_VARS    the module's variables load_aws_env gives it (an array: not
#     read-only); each one is a variable the module declares in variables.tf
# The gcp, gcp-kubeadm and azure rows have a directory and nothing else: validate
# is their only command.
select_module() {
  case "$1" in
    aws)
      MODULE_DIR="${TF_DIR}/aws"
      MODULE_REL=infra/terraform/aws
      MODULE_NAME=aws
      PLAN_FILE=aws.tfplan
      PLAN_RECORD_FILE=aws.tfplan.meta
      STATE_DIR_UNDER_HOME=.local/state/meridian-aws
      STATE_FILE_NAME=aws.tfstate
      MODULE_README=infra/terraform/aws/README.md
      CMD_PLAN="make aws-plan"
      CMD_APPLY="make aws-apply"
      CMD_DESTROY="make aws-destroy"
      PLAN_REVIEW="network, cluster, registry, database, secret, budget"
      MODULE_VARS=(region api_access_cidr budget_email expected_account_id)
      ;;
    aws-kubeadm)
      MODULE_DIR="${TF_DIR}/aws-kubeadm"
      MODULE_REL=infra/terraform/aws-kubeadm
      MODULE_NAME=aws-kubeadm
      PLAN_FILE=aws-kubeadm.tfplan
      PLAN_RECORD_FILE=aws-kubeadm.tfplan.meta
      STATE_DIR_UNDER_HOME=.local/state/meridian-aws-kubeadm
      STATE_FILE_NAME=aws-kubeadm.tfstate
      MODULE_README=infra/terraform/aws-kubeadm/README.md
      CMD_PLAN="infra/terraform/aws.sh plan aws-kubeadm"
      CMD_APPLY="infra/terraform/aws.sh apply aws-kubeadm"
      CMD_DESTROY="infra/terraform/aws.sh destroy aws-kubeadm"
      PLAN_REVIEW="network, security groups, roles, nodes, address, parameter, budget"
      MODULE_VARS=(region api_access_cidr budget_email expected_account_id)
      ;;
    gcp)
      MODULE_DIR="${TF_DIR}/gcp"
      MODULE_REL=infra/terraform/gcp
      MODULE_NAME=gcp
      PLAN_FILE=
      PLAN_RECORD_FILE=
      STATE_DIR_UNDER_HOME=
      STATE_FILE_NAME=
      MODULE_README=
      CMD_PLAN=
      CMD_APPLY=
      CMD_DESTROY=
      PLAN_REVIEW=
      MODULE_VARS=()
      ;;
    gcp-kubeadm)
      MODULE_DIR="${TF_DIR}/gcp-kubeadm"
      MODULE_REL=infra/terraform/gcp-kubeadm
      MODULE_NAME=gcp-kubeadm
      PLAN_FILE=
      PLAN_RECORD_FILE=
      STATE_DIR_UNDER_HOME=
      STATE_FILE_NAME=
      MODULE_README=
      CMD_PLAN=
      CMD_APPLY=
      CMD_DESTROY=
      PLAN_REVIEW=
      MODULE_VARS=()
      ;;
    azure)
      MODULE_DIR="${TF_DIR}/azure"
      MODULE_REL=infra/terraform/azure
      MODULE_NAME=azure
      PLAN_FILE=
      PLAN_RECORD_FILE=
      STATE_DIR_UNDER_HOME=
      STATE_FILE_NAME=
      MODULE_README=
      CMD_PLAN=
      CMD_APPLY=
      CMD_DESTROY=
      PLAN_REVIEW=
      MODULE_VARS=()
      ;;
    *) usage ;;
  esac
  readonly MODULE_DIR MODULE_REL MODULE_NAME PLAN_FILE PLAN_RECORD_FILE
  readonly STATE_DIR_UNDER_HOME STATE_FILE_NAME MODULE_README
  readonly CMD_PLAN CMD_APPLY CMD_DESTROY PLAN_REVIEW
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

# What the local file may hold and what a value may be made of: digits, letters
# and the few characters of an account number, a Region, an address with a prefix
# length and an e-mail address. No quote, space, dollar sign, backtick or
# semicolon: nothing a shell would act on.
local_file_access() {
  [[ -O "${AWS_LOCAL_ENV}" ]] ||
    die "${AWS_LOCAL_ENV} is not owned by the user running this; it must be yours and mode 600"
  [[ -r "${AWS_LOCAL_ENV}" ]] ||
    die "cannot read ${AWS_LOCAL_ENV}: its owner has no read permission; run: chmod 600 ${AWS_LOCAL_ENV}"
  local mode
  mode="$(stat -c '%a' "${AWS_LOCAL_ENV}" 2>/dev/null || stat -f '%Lp' "${AWS_LOCAL_ENV}")"
  mode="000${mode}"
  [[ "${mode: -2}" == 00 ]] ||
    die "the mode of ${AWS_LOCAL_ENV} lets group or others in; run: chmod 600 ${AWS_LOCAL_ENV}"
}

# Read the local file (never run it), check it holds the four values, and export
# what the module and the aws CLI read. The file is one for both modules: the
# account, the Region, the one address and the e-mail address belong to the
# owner's account and not to a module, and a second file would let the two
# modules be pinned to different accounts. The names after TF_VAR_ are the
# selected module's variables (its variables.tf, listed in the module's row as
# MODULE_VARS): region, api_access_cidr (the one address that may reach the
# cluster's public endpoint, a /32), budget_email and expected_account_id (which
# the module's provider enforces). The last three have no default, so a plan
# would stop to ask for them without these exports; a module gets exactly the
# variables of its row and no other; tests/meridian/test_aws_script_plan.py
# fails when the script and either module drift.
load_aws_env() {
  [[ -f "${AWS_LOCAL_ENV}" ]] ||
    die "no ${AWS_LOCAL_ENV}; create it as ${SHARED_README} describes (four values, mode 600)"
  local_file_access
  unset "${LOCAL_KEYS[@]}"
  local keys line number=0 seen=" " key value
  keys="$(
    IFS='|'
    printf '%s' "${LOCAL_KEYS[*]}"
  )"
  local pattern="^(${keys})=([A-Za-z0-9@._/+-]{0,${VALUE_MAX_LENGTH}})\$"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    number=$((number + 1))
    if [[ -z "${line}" || "${line}" == \#* ]]; then continue; fi
    [[ "${line}" =~ ${pattern} ]] ||
      die "line ${number} of ${AWS_LOCAL_ENV} is not KEY=value for one of the four keys ${SHARED_README} lists (nothing of the line is printed); the file is read, not run"
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    [[ "${seen}" != *" ${key} "* ]] ||
      die "line ${number} of ${AWS_LOCAL_ENV} gives a key a second time"
    seen="${seen}${key} "
    printf -v "${key}" '%s' "${value}"
  done <"${AWS_LOCAL_ENV}"
  for key in "${LOCAL_KEYS[@]}"; do
    [[ -n "${!key:-}" ]] ||
      die "${key} is not set in ${AWS_LOCAL_ENV}; ${SHARED_README} says what the file holds"
  done
  [[ "${MERIDIAN_AWS_ACCOUNT_ID}" =~ ^[0-9]{12}$ ]] ||
    die "MERIDIAN_AWS_ACCOUNT_ID in ${AWS_LOCAL_ENV} is not a twelve-digit account number"
  local region allowed=no
  for region in "${ALLOWED_REGIONS[@]}"; do
    if [[ "${region}" == "${MERIDIAN_AWS_REGION}" ]]; then allowed=yes; fi
  done
  [[ "${allowed}" == yes ]] ||
    die "MERIDIAN_AWS_REGION in ${AWS_LOCAL_ENV} is not one of the six Regions of EU member states the module allows (hard rule 3: EU residency); the list is the validation of region in ${MODULE_REL}/variables.tf, and nothing of the value is printed"
  # One Region for the CLI, the provider and the variable; the default of the
  # caller's profile is not consulted (and is not passed on).
  unset AWS_DEFAULT_REGION
  export AWS_REGION="${MERIDIAN_AWS_REGION}"
  # Exactly the variables of the selected module's row, each from its own key.
  local variable
  for variable in "${MODULE_VARS[@]}"; do
    case "${variable}" in
      region) export TF_VAR_region="${MERIDIAN_AWS_REGION}" ;;
      api_access_cidr) export TF_VAR_api_access_cidr="${MERIDIAN_AWS_ENDPOINT_CIDR}" ;;
      budget_email) export TF_VAR_budget_email="${MERIDIAN_AWS_BUDGET_EMAIL}" ;;
      expected_account_id) export TF_VAR_expected_account_id="${MERIDIAN_AWS_ACCOUNT_ID}" ;;
      *) die "internal error: the row of module ${MODULE_NAME} names a variable this script has no value for" ;;
    esac
  done
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

# Which module this run is about, said first: a plan, an apply and a removal
# print the names of resources, and the word after the command may have been
# left out, which selects the managed module (the costlier one).
log_module() { log "module: ${MODULE_NAME}"; }

cmd_validate() {
  data_dir_for_validate
  log "terraform fmt -check"
  tf_validating fmt -check -diff 2>&1 | redact ||
    die "terraform fmt found a file to format; run: terraform -chdir=${MODULE_REL} fmt"
  log "terraform init -backend=false"
  # readonly: the committed lock file decides the provider, and a check does not
  # rewrite it.
  tf_validating init -backend=false -input=false -lockfile=readonly 2>&1 | redact ||
    die "terraform init failed"
  log "terraform validate"
  tf_validating validate 2>&1 | redact ||
    die "terraform validate failed"
}

cmd_plan() {
  log_module
  refuse_files_that_change_the_plan
  load_aws_env
  prepare_state
  require_pinned_account
  # Each is read into a variable first: a die inside $(...) would end only the
  # substitution, not the script.
  local commit changes untracked
  commit="$(current_commit)"
  changes="$(module_changes)"
  untracked="$(untracked_terraform_files)"
  if [[ -n "${changes}" ]]; then
    log "warning: the module directory has uncommitted changes; the plan is shown (reading it is free) but no record is written for it, so '${CMD_APPLY}' will refuse it until they are committed and the plan is made again"
  fi
  if ((untracked > 0)); then
    log "warning: the module directory holds $(untracked_terraform_files_phrase "${untracked}"), which Terraform reads whatever an ignore rule says; the plan is shown but no record is written for it, so '${CMD_APPLY}' will refuse it. Commit the file if it belongs to the module; git cannot commit one that an ignore rule hides, so remove it. Then make the plan again"
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
  # The record says "made from this commit, from a directory no change had
  # touched". The directory is read again now: a file edited while the plan ran
  # is a change the commit does not describe either.
  local commit_after changes_after untracked_after
  commit_after="$(current_commit)"
  changes_after="$(module_changes)"
  untracked_after="$(untracked_terraform_files)"
  if [[ -n "${changes}" || -n "${changes_after}" || "${commit}" != "${commit_after}" ||
    "${untracked}" != 0 || "${untracked_after}" != 0 ]]; then
    log "no record was written: the plan above was made from a module directory that no commit describes, so '${CMD_APPLY}' will refuse it; commit the change, then make the plan again"
    return 0
  fi
  local digest
  digest="$(file_sha256 "${MODULE_DIR}/${PLAN_FILE}")"
  # Four lines: the module the plan was made for, the commit, the time and the
  # plan file's hash. The module's name keeps a plan of one module from being
  # applied as the other's even if its files were moved or copied by hand.
  printf 'module=%s\ncommit=%s\ntime=%s\nsha256=%s\n' "${MODULE_NAME}" "${commit}" "$(date +%s)" "${digest}" >"${MODULE_DIR}/${PLAN_RECORD_FILE}"
  log "review the plan above (${PLAN_REVIEW}), then: ${CMD_APPLY}"
}

# Applies the saved plan and nothing else, so what runs is what was reviewed.
# The plan and its record are removed either way: after a failed apply the plan
# is stale and Terraform would refuse it.
cmd_apply() {
  log_module
  [[ -f "${MODULE_DIR}/${PLAN_FILE}" ]] || die "no ${PLAN_FILE}; run '${CMD_PLAN}' first"
  refuse_files_that_change_the_plan
  # apply runs no init, so a workspace file left since the plan is checked here.
  # The plan is not dropped for it: it is still good once the workspace is back.
  require_default_workspace
  load_aws_env
  require_pinned_account
  # Last of the checks, just before Terraform reads the plan: the hash and the
  # age are taken after the sign-in call, which can hang or wait for a person,
  # so neither is older than the call that follows them.
  require_a_plan_that_is_this_trees_and_fresh
  log "terraform apply ${PLAN_FILE}"
  local status=0
  tf_signed apply -input=false "${PLAN_FILE}" 2>&1 | redact || status=$?
  drop_plan
  ((status == 0)) || die "terraform apply failed (exit ${status}); run '${CMD_PLAN}' again"
  log "applied. It bills by the hour until: ${CMD_DESTROY}"
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
  log_module
  [[ -t 0 ]] ||
    die "destroy needs a terminal: run '${CMD_DESTROY}' yourself, in a terminal (this check stops an accident and a plain shell, not a session that makes itself a terminal; ${SHARED_README} says what does)"
  refuse_files_that_change_the_plan
  load_aws_env
  prepare_state
  require_pinned_account
  init_with_state
  local before after
  before="$(count_state)" ||
    die "cannot read the state (terraform state list failed); nothing was touched, and the console is where to look"
  ((before > 0)) ||
    die "the state holds nothing, so Terraform would remove nothing: the file is ${STATE_PATH}. If the state was lost (a deleted checkout, another machine, another user), what it described may still exist and bill: look in the console, in the Region of the local file (${MODULE_README}, Removal, 'If the state is lost')"
  # Terraform's question names no module, and the word after the command may have
  # been left out (which means the managed module): the owner reads the module
  # here, in the last sentence above the question, before typing the answer.
  log "module ${MODULE_NAME}: the state holds ${before} resources"
  log "terraform destroy of module ${MODULE_NAME}: Terraform asks for the confirmation"
  local status=0
  tf_signed destroy 2>&1 | redact || status=$?
  ((status == 0)) ||
    die "terraform's removal failed (exit ${status}); the state still holds what is left. Read it with 'terraform -chdir=${MODULE_REL} state list', look in the console (in the Region of the local file) for what is left, then run '${CMD_DESTROY}' again: Terraform removes what is still in the state (${MODULE_README}, Removal)"
  after="$(count_state)" ||
    die "cannot read the state after the removal; look in the console for what is left"
  ((after == 0)) ||
    die "the state still holds ${after} resources, so the removal is not finished; run '${CMD_DESTROY}' again"
  log "removed. Look at the console for what is left: ${MODULE_README}, Removal"
}

# Every command takes at most one word after it, the name of a module from a
# closed list, and a word that is not on the list (a path, another spelling, a
# second word) is refused with the usage line before any program runs:
#   validate           aws (the default), gcp, aws-kubeadm, gcp-kubeadm or azure
#   plan, apply, destroy   aws-kubeadm, or no word for the managed module; gcp and
#                      gcp-kubeadm are refused with a sentence that says why, and
#                      so is the word aws on these three (the managed module is no
#                      word at all)
# The word is matched against literals, and each match calls select_module with a
# literal of its own: the caller's text is never part of a path or a file name.
case "${1-}" in
  validate | plan | apply | destroy) [[ $# -le 2 ]] || usage ;;
  *) usage ;;
esac
case "$1" in
  validate)
    case "${2-aws}" in
      aws) select_module aws ;;
      gcp) select_module gcp ;;
      aws-kubeadm) select_module aws-kubeadm ;;
      gcp-kubeadm) select_module gcp-kubeadm ;;
      azure) select_module azure ;;
      *) usage ;;
    esac
    need_tools terraform
    cmd_validate
    ;;
  *)
    if [[ $# -eq 1 ]]; then
      select_module aws
    else
      case "$2" in
        aws-kubeadm) select_module aws-kubeadm ;;
        gcp | gcp-kubeadm) refuse_gcp ;;
        *) usage ;;
      esac
    fi
    case "$1" in
      plan)
        need_tools terraform aws git
        require_a_git_that_reads_its_own_settings
        cmd_plan
        ;;
      apply)
        need_tools terraform aws git
        require_a_git_that_reads_its_own_settings
        cmd_apply
        ;;
      destroy)
        need_tools terraform aws
        cmd_destroy
        ;;
    esac
    ;;
esac
