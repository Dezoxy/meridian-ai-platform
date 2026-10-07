#!/usr/bin/env bash
# Drive the AWS module (infra/terraform/aws): `make aws-validate`,
# `make aws-plan`, `make aws-apply`, `make aws-destroy` (S036).
#   validate  format check, init with no backend, validate. Needs no AWS
#             credential (it is run with none) and no local file, and never
#             calls the aws CLI.
#   plan      init, then plan into aws.tfplan; changes nothing in AWS. Records
#             the commit, the time and the plan file's SHA-256 beside the plan,
#             unless the module's directory has uncommitted changes: the plan is
#             shown then, and no record is written, so apply refuses it.
#   apply     apply exactly that saved plan, then remove it (creates AWS
#             resources, which cost money). Refuses a plan that is not this
#             tree's, is not the file the record names, or is older than thirty
#             minutes.
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
# GIT_WORK_TREE of the caller must not point it at another repository. And with
# less of the caller's configuration: the caller's global and the system
# configuration files are switched off (GIT_CONFIG_GLOBAL, GIT_CONFIG_NOSYSTEM;
# the first needs git 2.32, from 2021, which the next function checks), the two
# settings that make `git status` run a program are set by hand, which outranks
# the repository's own file too (core.fsmonitor, a program asked what changed,
# and core.hooksPath, the hooks), and the caller's default ignore file is
# switched off (core.excludesFile, which would otherwise be ~/.config/git/ignore:
# a line there hid an untracked .tf from `status`).
# The two commands used here that matter, `status --porcelain` and `rev-parse`,
# talk to no remote, so no credential helper is asked, and page nothing when
# their output is not a terminal, so no core.pager or GIT_PAGER runs.
#
# What this does NOT stop: a `filter.<name>.clean` program configured in the
# repository's OWN .git/config, together with an attributes line that names it
# (a committed .gitattributes, or .git/info/attributes), DOES run during
# `status` for a tracked file whose modification time changed and whose size
# did not (the third review ran it with a file made by `touch`). Nothing here
# turns it off. It is listed in infra/terraform/aws/README.md with the rest.
git_here() {
  run_clean plain env GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    git -c core.fsmonitor=false -c core.hooksPath=/dev/null -c core.excludesFile=/dev/null \
    -C "${AWS_MODULE_DIR}" "$@"
}

# GIT_CONFIG_GLOBAL is read by git 2.32 and newer; an older git ignores it and
# reads the caller's ~/.gitconfig, so the sentence above would be false. The
# version is read from `git --version` ("git version 2.43.0", "git version 2.39.5
# (Apple Git-154)") and compared as numbers, major and then minor, in base ten
# (10#: a leading zero would be read as octal). A line it cannot read is refused.
# Only major and minor are printed: a four-part version would be taken for an
# address by redact. plan and apply call git, so they call this; validate and
# destroy make no git call.
readonly GIT_NEEDED_MAJOR=2
readonly GIT_NEEDED_MINOR=32
require_a_git_that_reads_its_own_settings() {
  local line major minor
  line="$(run_clean plain git --version 2>/dev/null)" ||
    die "cannot run 'git --version'; this needs git ${GIT_NEEDED_MAJOR}.${GIT_NEEDED_MINOR} or newer, the first that reads GIT_CONFIG_GLOBAL"
  [[ "${line}" =~ ^git\ version\ ([0-9]{1,6})\.([0-9]{1,6}) ]] ||
    die "cannot read a version number from what 'git --version' printed; this needs git ${GIT_NEEDED_MAJOR}.${GIT_NEEDED_MINOR} or newer, the first that reads GIT_CONFIG_GLOBAL"
  major="${BASH_REMATCH[1]}"
  minor="${BASH_REMATCH[2]}"
  if ((10#${major} > GIT_NEEDED_MAJOR || (10#${major} == GIT_NEEDED_MAJOR && 10#${minor} >= GIT_NEEDED_MINOR))); then
    return 0
  fi
  die "git ${major}.${minor} is too old: this needs git ${GIT_NEEDED_MAJOR}.${GIT_NEEDED_MINOR} or newer, the first that reads GIT_CONFIG_GLOBAL; an older one reads the caller's own git configuration, which the script switches off for the calls it makes (infra/terraform/aws/README.md, 'What stops a session, and what does not')"
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
  local pattern="^(${keys})=([A-Za-z0-9@._/+-]{0,${VALUE_MAX_LENGTH}})\$"
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
  local region allowed=no
  for region in "${ALLOWED_REGIONS[@]}"; do
    if [[ "${region}" == "${MERIDIAN_AWS_REGION}" ]]; then allowed=yes; fi
  done
  [[ "${allowed}" == yes ]] ||
    die "MERIDIAN_AWS_REGION in ${AWS_LOCAL_ENV} is not one of the six Regions of EU member states the module allows (hard rule 3: EU residency); the list is the validation of region in infra/terraform/aws/variables.tf, and nothing of the value is printed"
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
#
# dotglob makes the loop see names that begin with a dot, which the shell's own
# * skips: Terraform loads .auto.tfvars and .x.auto.tfvars, and git ignores them
# (*.tfvars), so a hidden one would change the plan with nothing to show it.
# nullglob leaves no literal * to look at in an empty directory. The names are
# compared in lower case: on a file system that ignores case (macOS, Windows)
# Terraform finds Terraform.tfvars as terraform.tfvars. On Linux it would not,
# and such a file is refused all the same: a refusal there costs a rename, and
# telling the two file systems apart costs more than that.
refuse_files_that_change_the_plan() {
  local path name
  shopt -s dotglob nullglob
  for path in "${AWS_MODULE_DIR}"/*; do
    name="$(printf '%s' "${path##*/}" | tr '[:upper:]' '[:lower:]')"
    case "${name}" in
      terraform.tfvars | terraform.tfvars.json | *.auto.tfvars | *.auto.tfvars.json)
        die "the module directory holds a variable file, which Terraform loads by itself and which would change the plan unseen (terraform.tfvars, terraform.tfvars.json, *.auto.tfvars and *.auto.tfvars.json are the ones it loads); remove it and give values through the local file"
        ;;
      override.tf | override.tf.json | *_override.tf | *_override.tf.json)
        die "the module directory holds an override file, which Terraform merges over the module's own and which would change the plan unseen (override.tf, override.tf.json, *_override.tf and *_override.tf.json are the ones it merges); remove it and change the module itself, in a commit"
        ;;
    esac
  done
  shopt -u dotglob nullglob
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

# The state is under home only in the default workspace. A workspace made by hand
# is named in .terraform/environment and stays selected across inits; Terraform
# then keeps its state in terraform.tfstate.d/<name>/ in the module's directory (a
# checkout), not at the path given at init, and a removal would find the state
# empty. TF_WORKSPACE and TF_DATA_DIR are not passed on (run_clean), so this file
# is the one Terraform reads. Selecting the default workspace again leaves the
# file in place, with the word default in it: the content is read, not the
# file's existence. This reads the file rather than asking `terraform workspace
# show`: no further call, and nothing for a stand-in to imitate.
#
# The whole file is the name, as Terraform reads it: the content with leading and
# trailing white space trimmed (observed with `terraform workspace show` on
# v1.16.5: " default \r\n\t\n" is the default workspace, and so is an empty or a
# blank file; "default\nother" is not a name at all). A second line is therefore
# not skipped. A file that cannot be read (a directory, a mode of 000) is
# refused, and the shell's own words about it are not printed.
require_default_workspace() {
  local file="${AWS_MODULE_DIR}/.terraform/environment" name=default readable=yes
  if [[ -e "${file}" || -L "${file}" ]]; then
    { name="$(cat -- "${file}")"; } 2>/dev/null || readable=no
    name="${name#"${name%%[![:space:]]*}"}"
    name="${name%"${name##*[![:space:]]}"}"
    if [[ -z "${name}" ]]; then name=default; fi
  fi
  [[ "${readable}" == yes && "${name}" == default ]] ||
    die "the module's directory is not on Terraform's default workspace, or .terraform/environment cannot be read: Terraform would keep the state in terraform.tfstate.d/ in the checkout and not in the state under your home, and a removal would find it empty. Get back with: terraform -chdir=infra/terraform/aws workspace select default (infra/terraform/aws/README.md, State)"
}

# init with the local state's path, and with the committed lock file as the
# only authority on the provider. -reconfigure: the path is always this script's
# own, so an init made earlier with another path (by hand) is replaced instead of
# stopping with "Backend configuration changed". It copies no state and asks no
# question (with -input=false): a state at the other path is left where it is
# and this one's is read from the new path. Then the workspace must be the default.
init_with_state() {
  log "terraform init"
  tf_plain init -input=false -reconfigure -lockfile=readonly -backend-config="path=${STATE_PATH}" 2>&1 | redact ||
    die "terraform init failed"
  require_default_workspace
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

# The number of *.tf and *.tf.json files in the module's directory that git does
# not track, WHATEVER an ignore rule says: `ls-files --others` with no
# --exclude-standard reads no ignore file at all (not ~/.config/git/ignore, not
# .git/info/exclude, not a .gitignore in the module), so a file hidden by one is
# counted as well as a plain untracked one. Terraform reads every such file in
# the directory, and `status` does not show an ignored one. Only the directory's
# own files count (':(glob)*' does not cross a slash): what init downloads under
# .terraform/ is not read as part of this module, and the plan, its record and
# the state are not .tf files. icase: a file system that ignores case hands
# Main.TF to Terraform as main.tf. The names are never printed, only this count.
untracked_terraform_files() {
  local count
  count="$(git_here ls-files --others -z -- ':(glob,icase)*.tf' ':(glob,icase)*.tf.json' 2>/dev/null |
    tr -cd '\0' | wc -c | tr -d ' ')" ||
    die "cannot read the list of untracked files in the module; this has to run in a checkout of the repository"
  printf '%s\n' "${count}"
}

# "1 untracked Terraform file" or "3 untracked Terraform files", for a sentence.
untracked_terraform_files_phrase() {
  if (($1 == 1)); then
    printf '1 untracked Terraform file (*.tf or *.tf.json)'
  else
    printf '%s untracked Terraform files (*.tf or *.tf.json)' "$1"
  fi
}

drop_plan() { rm -f "${AWS_MODULE_DIR}/${PLAN_FILE}" "${AWS_MODULE_DIR}/${PLAN_RECORD_FILE}"; }

# The SHA-256 of a file, as sixty-four lower-case hex digits and nothing else.
file_sha256() {
  local out
  # From standard input: given a path, sha256sum puts a backslash in front of the
  # digest when the path holds a backslash or a newline. (2>/dev/null comes
  # first so that it also covers the shell's own words about a file it cannot
  # open for the redirection.)
  out="$(sha256sum 2>/dev/null <"$1" || shasum -a 256 2>/dev/null <"$1")" ||
    die "cannot compute a SHA-256 (neither sha256sum nor shasum is available, or the file cannot be read)"
  printf '%s\n' "${out%% *}"
}

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
  local commit changes untracked
  commit="$(current_commit)"
  changes="$(module_changes)"
  untracked="$(untracked_terraform_files)"
  if [[ -n "${changes}" ]]; then
    log "warning: the module directory has uncommitted changes; the plan is shown (reading it is free) but no record is written for it, so 'make aws-apply' will refuse it until they are committed and the plan is made again"
  fi
  if ((untracked > 0)); then
    log "warning: the module directory holds $(untracked_terraform_files_phrase "${untracked}"), which Terraform reads whatever an ignore rule says; the plan is shown but no record is written for it, so 'make aws-apply' will refuse it. Commit the file if it belongs to the module; git cannot commit one that an ignore rule hides, so remove it. Then make the plan again"
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
    log "no record was written: the plan above was made from a module directory that no commit describes, so 'make aws-apply' will refuse it; commit the change, then make the plan again"
    return 0
  fi
  local digest
  digest="$(file_sha256 "${AWS_MODULE_DIR}/${PLAN_FILE}")"
  printf 'commit=%s\ntime=%s\nsha256=%s\n' "${commit}" "$(date +%s)" "${digest}" >"${AWS_MODULE_DIR}/${PLAN_RECORD_FILE}"
  log "review the plan above (network, cluster, registry, database, secret, budget), then: make aws-apply"
}

# A saved plan is applied only if it was made at the commit that is checked out
# now, from a module directory that has not changed since, and not too long ago.
# Otherwise it is dropped (it is of no use any more) and the sentence says to
# plan again.
require_a_plan_that_is_this_trees_and_fresh() {
  local record="${AWS_MODULE_DIR}/${PLAN_RECORD_FILE}" line_commit line_time line_hash
  local planned_commit planned_time planned_hash now age commit changes untracked
  stale() {
    drop_plan
    die "$1; run 'make aws-plan' again"
  }
  [[ -f "${record}" ]] || stale "the saved plan has no record beside it of the commit, the time and the file it was made as (a plan made from a module directory with uncommitted changes gets none)"
  { IFS= read -r line_commit && IFS= read -r line_time && IFS= read -r line_hash && ! IFS= read -r _; } <"${record}" ||
    stale "the record beside the saved plan is not a commit, a time and a SHA-256"
  [[ "${line_commit}" =~ ^commit=([0-9a-f]{40})$ ]] ||
    stale "the record beside the saved plan is not a commit, a time and a SHA-256"
  planned_commit="${BASH_REMATCH[1]}"
  # Ten digits, no sign, no leading zero: a plain decimal. The shell reads a
  # number with a leading zero as octal (08 is an error, and the octal spelling of
  # the clock is read as the clock), so nothing else gets as far as arithmetic.
  [[ "${line_time}" =~ ^time=([1-9][0-9]{9})$ ]] ||
    stale "the record beside the saved plan is not a commit, a time and a SHA-256"
  planned_time="${BASH_REMATCH[1]}"
  [[ "${line_hash}" =~ ^sha256=([0-9a-f]{64})$ ]] ||
    stale "the record beside the saved plan is not a commit, a time and a SHA-256"
  planned_hash="${BASH_REMATCH[1]}"
  # The record is the script's and the plan file may not be: a plan written by
  # hand over it (a -target, another variable) would carry this record's commit.
  [[ "$(file_sha256 "${AWS_MODULE_DIR}/${PLAN_FILE}")" == "${planned_hash}" ]] ||
    stale "the saved plan file is not the one the record beside it was made for (its SHA-256 differs): something wrote it after 'make aws-plan'"
  commit="$(current_commit)"
  [[ "${planned_commit}" == "${commit}" ]] ||
    stale "the saved plan was made at another commit than the one checked out now"
  changes="$(module_changes)"
  [[ -z "${changes}" ]] ||
    stale "the module directory has uncommitted changes that the saved plan was not made from"
  # What no ignore rule can hide: git status does not show a file an ignore file
  # of the repository hides, and Terraform reads it.
  untracked="$(untracked_terraform_files)"
  ((untracked == 0)) ||
    stale "the module directory holds $(untracked_terraform_files_phrase "${untracked}") that the saved plan was not made from, and Terraform reads it whatever an ignore rule says; remove it (git cannot commit a file that an ignore rule hides), or commit it if it belongs to the module"
  now="$(date +%s)"
  age=$((now - 10#${planned_time}))
  ((age >= 0 && age <= PLAN_MAX_AGE_SECONDS)) ||
    stale "the saved plan is too old (more than $((PLAN_MAX_AGE_SECONDS / 60)) minutes) or dated in the future"
}

# Applies the saved plan and nothing else, so what runs is what was reviewed.
# The plan and its record are removed either way: after a failed apply the plan
# is stale and Terraform would refuse it.
cmd_apply() {
  [[ -f "${AWS_MODULE_DIR}/${PLAN_FILE}" ]] || die "no ${PLAN_FILE}; run 'make aws-plan' first"
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
  local status=0
  tf_signed destroy 2>&1 | redact || status=$?
  ((status == 0)) ||
    die "terraform's removal failed (exit ${status}); the state still holds what is left. Read it with 'terraform -chdir=infra/terraform/aws state list', look in the console (in the Region of the local file) for what is left, then run 'make aws-destroy' again: Terraform removes what is still in the state (infra/terraform/aws/README.md, Removal)"
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
  *) usage ;;
esac
