# shellcheck shell=bash
# The protections of a Terraform wrapper that name no cloud, moved here from the
# first wrapper (S020), so that a second wrapper sources the same code and the
# checks stay one copy. Source it after common.sh; do not run it.
#
# What it holds: the Terraform and git calls (tf_plain, tf_signed, tf_validating,
# tf_state_list, git_here), the git version they need, the refusal of a variable
# or an override file, the state's directory under home, the default workspace,
# init with the local state, the checks of the tree (the commit, the changes, the
# untracked .tf files), the saved plan and its record (drop_plan, file_sha256 and
# the check that the plan is this tree's and fresh) and the directory validate
# keeps the providers in.
#
# What it needs from the wrapper, which defines all of it before any function
# here runs. The environment each program is given is the cloud's own, so the
# one function is run_clean; the globals are the selected module's row, the
# words of two sentences and the README those sentences cite. The functions of
# common.sh (die, log, redact) are the other shared file's. A test holds these
# two lists equal to what the code below calls and reads.
#   functions: run_clean
#   globals:   MODULE_DIR, MODULE_REL, MODULE_NAME, PLAN_FILE, PLAN_RECORD_FILE,
#              MODULE_README, CMD_PLAN, STATE_DIR_UNDER_HOME, STATE_FILE_NAME,
#              SHARED_README, ENVIRONMENT_WORDS
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  printf 'planguard.sh is sourced by a Terraform wrapper; it is not run\n' >&2
  exit 1
fi

# A plan older than this is not applied: the account, the quotas and the
# prices it was made against may have changed, and the owner read it a while
# ago. Thirty minutes is the length of one plan-read-apply sitting.
readonly PLAN_MAX_AGE_SECONDS=1800

# terraform in the module's directory, without colour. -chdir also makes the
# plan file path relative to that directory. The sub-command comes first because
# -no-color is an option of the sub-command. "plain" has no cloud credential:
# format, init, validate and the state's list need none.
tf_plain() {
  local sub="$1"
  shift
  run_clean plain terraform -chdir="${MODULE_DIR}" "${sub}" -no-color "$@"
}
tf_signed() {
  local sub="$1"
  shift
  run_clean signed terraform -chdir="${MODULE_DIR}" "${sub}" -no-color "$@"
}
# validate's own terraform: the same, with Terraform's data directory (the
# providers init downloads, .terraform in the module's directory by default) set
# to DATA_DIR, which data_dir_for_validate made. TF_DATA_DIR is set here and
# nowhere else, from nothing the caller gave, so no other command's directory
# moves (the default-workspace check of plan, apply and the removal reads
# .terraform/environment in the module's directory) and the caller's own value is
# never passed on (run_clean drops it).
tf_validating() {
  local sub="$1"
  shift
  run_clean plain env "TF_DATA_DIR=${DATA_DIR}" terraform -chdir="${MODULE_DIR}" "${sub}" -no-color "$@"
}
tf_state_list() { run_clean plain terraform -chdir="${MODULE_DIR}" state list -no-color; }

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
# turns it off. It is described in infra/terraform/README.md, in the paragraph
# on planguard.sh.
git_here() {
  run_clean plain env GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    git -c core.fsmonitor=false -c core.hooksPath=/dev/null -c core.excludesFile=/dev/null \
    -C "${MODULE_DIR}" "$@"
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
  die "git ${major}.${minor} is too old: this needs git ${GIT_NEEDED_MAJOR}.${GIT_NEEDED_MINOR} or newer, the first that reads GIT_CONFIG_GLOBAL; an older one reads the caller's own git configuration, which the script switches off for the calls it makes (${SHARED_README}, 'What stops a session, and what does not')"
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
  for path in "${MODULE_DIR}"/*; do
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
    die "HOME is not set, and the state of ${ENVIRONMENT_WORDS} is kept in a directory under it (${MODULE_README}, State)"
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
  local file="${MODULE_DIR}/.terraform/environment" name=default readable=yes
  if [[ -e "${file}" || -L "${file}" ]]; then
    { name="$(cat -- "${file}")"; } 2>/dev/null || readable=no
    name="${name#"${name%%[![:space:]]*}"}"
    name="${name%"${name##*[![:space:]]}"}"
    if [[ -z "${name}" ]]; then name=default; fi
  fi
  [[ "${readable}" == yes && "${name}" == default ]] ||
    die "the module's directory is not on Terraform's default workspace, or .terraform/environment cannot be read: Terraform would keep the state in terraform.tfstate.d/ in the checkout and not in the state under your home, and a removal would find it empty. Get back with: terraform -chdir=${MODULE_REL} workspace select default (${MODULE_README}, State)"
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

drop_plan() { rm -f "${MODULE_DIR}/${PLAN_FILE}" "${MODULE_DIR}/${PLAN_RECORD_FILE}"; }

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

# Where validate's Terraform keeps the providers it downloads: a directory of the
# caller's cache named for the module, made private (the umask is 077 for the
# whole script, and the mode is set again, so a directory that was open to others
# is closed). It is under HOME and not under XDG_CACHE_HOME, which the script
# does not pass on: no value of the caller chooses it. HOME is one of the names
# run_clean passes on; it has to be an absolute path, or the directory would be
# made wherever the script happens to run. A symbolic link in its place is
# refused, not followed. Sets DATA_DIR (read-only once set).
data_dir_for_validate() {
  [[ "${HOME-}" == /* ]] ||
    die "HOME is not an absolute path, and validate keeps Terraform's providers under \$HOME/.cache/meridian-terraform, not in the module's directory"
  local dir="${HOME}/.cache/meridian-terraform/${MODULE_NAME}"
  [[ ! -L "${dir}" ]] ||
    die "${dir} is a symbolic link; validate will not put Terraform's providers behind one (remove the link)"
  # Every directory this makes is 700 by the script's umask; -m would reach only
  # the last one of a path (shellcheck SC2174), so the last is closed by chmod.
  mkdir -p "${dir}" ||
    die "cannot make ${dir}, where validate keeps Terraform's providers"
  chmod 700 "${dir}" ||
    die "cannot close ${dir} to other users"
  DATA_DIR="${dir}"
  readonly DATA_DIR
}

# A saved plan is applied only if it was made for this module, at the commit that
# is checked out now, from a module directory that has not changed since, and not
# too long ago. Otherwise it is dropped (it is of no use any more) and the
# sentence says to plan again.
require_a_plan_that_is_this_trees_and_fresh() {
  local record="${MODULE_DIR}/${PLAN_RECORD_FILE}" line_module line_commit line_time line_hash
  local planned_commit planned_time planned_hash now age commit changes untracked size
  stale() {
    drop_plan
    die "$1; run '${CMD_PLAN}' again"
  }
  [[ -f "${record}" ]] || stale "the saved plan has no record beside it of the module, the commit, the time and the file it was made as (a plan made from a module directory with uncommitted changes gets none)"
  { IFS= read -r line_module && IFS= read -r line_commit && IFS= read -r line_time && IFS= read -r line_hash; } <"${record}" ||
    stale "the record beside the saved plan is not a module, a commit, a time and a SHA-256"
  # The four lines are the whole record, byte for byte. A read of a fifth line
  # sees the end of the file for a fifth line with no newline, and read drops a
  # NUL, so what stands after the four lines, or inside one, is found by the size:
  # the four lines and their four newlines, counted once each line has been held
  # to its shape below (every character of a line that passes is one byte).
  size="$(wc -c <"${record}" | tr -d ' ')"
  # The module's name is a lower-case word with dashes, nothing else, and it is
  # this command's own module: a record that names the other one (its files moved
  # or copied by hand) is refused whatever its hash says.
  [[ "${line_module}" =~ ^module=([a-z][a-z-]{0,30})$ ]] ||
    stale "the record beside the saved plan is not a module, a commit, a time and a SHA-256"
  [[ "${BASH_REMATCH[1]}" == "${MODULE_NAME}" ]] ||
    stale "the record beside the saved plan names another module than the one this command was given"
  [[ "${line_commit}" =~ ^commit=([0-9a-f]{40})$ ]] ||
    stale "the record beside the saved plan is not a module, a commit, a time and a SHA-256"
  planned_commit="${BASH_REMATCH[1]}"
  # Ten digits, no sign, no leading zero: a plain decimal. The shell reads a
  # number with a leading zero as octal (08 is an error, and the octal spelling of
  # the clock is read as the clock), so nothing else gets as far as arithmetic.
  [[ "${line_time}" =~ ^time=([1-9][0-9]{9})$ ]] ||
    stale "the record beside the saved plan is not a module, a commit, a time and a SHA-256"
  planned_time="${BASH_REMATCH[1]}"
  [[ "${line_hash}" =~ ^sha256=([0-9a-f]{64})$ ]] ||
    stale "the record beside the saved plan is not a module, a commit, a time and a SHA-256"
  planned_hash="${BASH_REMATCH[1]}"
  ((size == ${#line_module} + ${#line_commit} + ${#line_time} + ${#line_hash} + 4)) ||
    stale "the record beside the saved plan is not a module, a commit, a time and a SHA-256"
  # The record is the script's and the plan file may not be: a plan written by
  # hand over it (a -target, another variable) would carry this record's commit.
  [[ "$(file_sha256 "${MODULE_DIR}/${PLAN_FILE}")" == "${planned_hash}" ]] ||
    stale "the saved plan file is not the one the record beside it was made for (its SHA-256 differs): something wrote it after '${CMD_PLAN}'"
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
