#!/usr/bin/env bash
# Drive the AWS module (infra/terraform/aws): `make aws-validate`,
# `make aws-plan`, `make aws-apply`, `make aws-destroy` (S036).
#   validate  format check, init with no backend, validate. Needs no AWS
#             credential and no local file, and never calls the aws CLI.
#   plan      init, then plan into aws.tfplan; changes nothing in AWS
#   apply     apply exactly that saved plan, then remove it (creates AWS
#             resources, which cost money)
#   destroy   remove everything the module created. Terraform asks its own
#             question, and this refuses unless standard input is a terminal:
#             only the owner runs it, and no variable changes that.
# plan, apply and destroy first read infra/terraform/local.env-aws (gitignored:
# the pattern infra/terraform/local.env* covers it, and the hook that stops a
# `cat` of a .env file reads the name as one, which `local.env.aws` escapes)
# and refuse unless the
# signed-in AWS account is the one pinned there, as the Azure scripts pin the
# subscription. The file holds four values: the expected account, the Region,
# the one address that may reach the cluster's public endpoint, and the e-mail
# address of the budget's alerts. None of them is ever printed.
# Everything printed from Terraform goes through redact (common.sh), which knows
# the shapes of an AWS account number, an ARN, an access key identifier and the
# host of a cluster or a database. The aws CLI's own words are never printed:
# only a sentence of this script says what to do.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# Terraform reads arguments from TF_CLI_ARGS and TF_CLI_ARGS_<command>: a caller
# could put -auto-approve there and take away the question destroy asks. What
# these commands tell Terraform is what is written below, nothing else.
while IFS= read -r name; do
  unset "${name}"
done < <(compgen -e | grep '^TF_CLI_ARGS' || true)

readonly AWS_MODULE_DIR="${TF_DIR}/aws"
readonly AWS_LOCAL_ENV="${TF_DIR}/local.env-aws"
readonly PLAN_FILE=aws.tfplan
readonly LOCAL_KEYS=(MERIDIAN_AWS_ACCOUNT_ID MERIDIAN_AWS_REGION MERIDIAN_AWS_ENDPOINT_CIDR MERIDIAN_AWS_BUDGET_EMAIL)

usage() {
  printf 'usage: %s <validate|plan|apply|destroy>\n' "$(basename "$0")" >&2
  exit 2
}

# terraform in the module's directory. -chdir also makes the plan file path
# relative to that directory.
tf() { terraform -chdir="${AWS_MODULE_DIR}" "$@"; }

# Read the local file, check it holds the four values, and export what the
# module and the aws CLI read. The names on the right of TF_VAR_ are the
# module's variables (aws/variables.tf): the Region, the address that may reach
# the cluster's endpoint (a /32) and the budget's e-mail address.
load_aws_env() {
  [[ -f "${AWS_LOCAL_ENV}" ]] ||
    die "no ${AWS_LOCAL_ENV}; create it as infra/terraform/aws/README.md describes (four values, mode 600)"
  unset "${LOCAL_KEYS[@]}"
  # shellcheck source=/dev/null
  . "${AWS_LOCAL_ENV}"
  local key
  for key in "${LOCAL_KEYS[@]}"; do
    [[ -n "${!key:-}" ]] ||
      die "${key} is not set in ${AWS_LOCAL_ENV}; infra/terraform/aws/README.md says what the file holds"
  done
  [[ "${MERIDIAN_AWS_ACCOUNT_ID}" =~ ^[0-9]{12}$ ]] ||
    die "MERIDIAN_AWS_ACCOUNT_ID in ${AWS_LOCAL_ENV} is not a twelve-digit account number"
  # One Region for the CLI, the provider and the variable; the default of the
  # caller's profile is not consulted.
  unset AWS_DEFAULT_REGION
  export AWS_REGION="${MERIDIAN_AWS_REGION}"
  export TF_VAR_region="${MERIDIAN_AWS_REGION}"
  export TF_VAR_api_allowed_cidr="${MERIDIAN_AWS_ENDPOINT_CIDR}"
  export TF_VAR_budget_email="${MERIDIAN_AWS_BUDGET_EMAIL}"
}

# The account the caller is signed in to must be the pinned one. Neither number
# is printed, and neither is anything the aws CLI says: its errors carry the
# account.
require_pinned_account() {
  local caller
  caller="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" &&
    [[ -n "${caller}" ]] ||
    die "cannot read the caller's AWS account; sign in (for example 'aws sso login') to the account you mean, then try again"
  [[ "${caller}" == "${MERIDIAN_AWS_ACCOUNT_ID}" ]] ||
    die "the signed-in AWS account is not the one pinned in ${AWS_LOCAL_ENV}; sign in to the right account, or correct the pin if it is wrong"
  log "AWS account: the pinned one"
}

cmd_validate() {
  log "terraform fmt -check"
  tf fmt -check -diff 2>&1 | redact ||
    die "terraform fmt found a file to format; run: terraform -chdir=infra/terraform/aws fmt"
  log "terraform init -backend=false"
  # readonly: the committed lock file decides the provider, and a check does not
  # rewrite it.
  tf init -backend=false -input=false -lockfile=readonly 2>&1 | redact ||
    die "terraform init failed"
  log "terraform validate"
  tf validate 2>&1 | redact ||
    die "terraform validate failed"
}

cmd_plan() {
  load_aws_env
  require_pinned_account
  log "terraform init"
  tf init -input=false -lockfile=readonly 2>&1 | redact ||
    die "terraform init failed"
  log "terraform plan"
  umask 077 # the plan file embeds resource IDs
  # Through redact, with pipefail keeping terraform's exit status.
  tf plan -input=false -out="${PLAN_FILE}" 2>&1 | redact
  log "review the plan above (network, cluster, registry, database, secret, budget), then: make aws-apply"
}

# Applies the saved plan and nothing else, so what runs is what was reviewed.
# The plan file is removed either way: after a failed apply it is stale and
# Terraform would refuse it.
cmd_apply() {
  [[ -f "${AWS_MODULE_DIR}/${PLAN_FILE}" ]] || die "no ${PLAN_FILE}; run 'make aws-plan' first"
  load_aws_env
  require_pinned_account
  log "terraform apply ${PLAN_FILE}"
  local status=0
  tf apply -input=false "${PLAN_FILE}" 2>&1 | redact || status=$?
  rm -f "${AWS_MODULE_DIR}/${PLAN_FILE}"
  ((status == 0)) || die "terraform apply failed (exit ${status}); run 'make aws-plan' again"
  log "applied. It bills by the hour until: make aws-destroy"
}

# No automatic approval and no -input=false: Terraform asks for its own "yes" on
# this terminal. The question passes through redact line by line, so the last
# words of the prompt ("Enter a value:", with no newline after them) show only
# once the answer has been typed; the question above them shows at once.
cmd_destroy() {
  [[ -t 0 ]] ||
    die "destroy needs a terminal: Terraform asks its own question and only the owner answers it; run 'make aws-destroy' yourself, in a terminal"
  load_aws_env
  require_pinned_account
  log "terraform destroy: Terraform asks for the confirmation"
  tf destroy 2>&1 | redact
  log "removed. Look at the console for what is left: infra/terraform/aws/README.md, Removal"
}

[[ $# -eq 1 ]] || usage
case "$1" in
  validate)
    need_tools terraform
    cmd_validate
    ;;
  plan)
    need_tools terraform aws
    cmd_plan
    ;;
  apply)
    need_tools terraform aws
    cmd_apply
    ;;
  destroy)
    need_tools terraform aws
    cmd_destroy
    ;;
  *) usage ;;
esac
