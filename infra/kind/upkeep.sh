#!/usr/bin/env bash
# Run the Model Gateway's upkeep command on the kind cluster, as a Job (S066,
# decision 8): `make gateway-upkeep ARGS="reservations --older-than 15"`. The
# command is `meridian gateway` (src/meridian/platform/cli/gateway.py): it lists
# the open reservations, closes one a dead process left, credits a tenant and
# expires old ledger months and audit rows, as the database role gateway_upkeep,
# whose Secret
# gateway-upkeep-db no workload of the release holds. This script is how a Job
# gets it, for one run. Needs `make up` (the Secret) and `make deploy` (the image).
#   1. the words of ARGS, checked before the cluster is asked anything: split on
#      blanks into an array by `read -r -a`, which neither expands a glob nor
#      reads a quote, and each word must be letters, digits, '.', '_', '=' and
#      '-' (the command's arguments are slugs, numbers, dates and IDs; none needs
#      more). This script reads no word as shell: a word with a quote, a
#      backslash, a '$', a backtick, a newline or a glob or shell character is
#      refused, with the word shown. A newline is refused first, since `read`
#      stops at one. But make expands $(...) and $$ in ARGS, a value on its
#      command line, BEFORE the script sees it (make gateway-upkeep
#      ARGS='close $(shell touch FILE)' runs the touch): that is the operator's
#      own input on the operator's own machine, and what reaches the script is
#      checked as above
#   2. the Secret gateway-upkeep-db must hold a `uri` (only key names are read,
#      never a value, and the connection string is never in a file, a command
#      line or this script's output), and the Helm release must say which image it
#      runs: the Job runs that image, so the command is the deployed one's
#   3. `helm template` of the chart with jobs.upkeep.enabled, the words as a
#      JSON list (--set-json, built by jq) and a run suffix of its own (the
#      image's tag does not change between two runs, so a second run is a new
#      Job), applied outside the release with kubectl, as deploy.sh applies the
#      migration, seed and ingestion Jobs: `make deploy` neither creates nor
#      removes it. Nothing is installed, upgraded or deleted here
#   4. the wait: the Job's conditions every UPKEEP_INTERVAL seconds, at most
#      UPKEEP_LOOKS looks. Its output (the pod's log) is printed through
#      printable_ascii, however it ends
# Exit code: 0 when the Job succeeded; 1 when it failed (the command exits 1 on a
# refusal, `ERROR GUnnn`, and on a failure of its own, and 2 on a usage error:
# each is a Failed Job and none is retried, backoffLimit 0), when it did not
# finish, and when a precondition was not met. (`make` itself returns 2 for a
# recipe that failed, where this script exits 1.) What a failed Job promises
# depends on its output. The two expiries remove in batches, each its own
# transaction, and a failure (a refusal of the ledger's, a lost connection) can
# follow batches that committed: the command then prints, after its error, a line
# that says what it removed "before the failure", and the script says that stays
# removed and that running the command again continues. Without that line, a
# refusal, the command's own `ERROR GUnnn` line, changed nothing. Every other
# failure, and a Job that did not finish, says the change MAY have been applied
# and to read the reservations or the audit rows before a rerun, because a credit
# is a new row on every run.
# The Job and its output are kept for a day (ttlSecondsAfterFinished), so
# `kubectl -n meridian logs job/<name>` still reads them the next morning.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly NAMESPACE=meridian
readonly UPKEEP_SECRET=gateway-upkeep-db
readonly UPKEEP_JOB=meridian-upkeep
# Above the Job's activeDeadlineSeconds (120): the Job ends itself and the
# deadline is its verdict before this script gives up on it.
readonly UPKEEP_LOOKS=90
readonly UPKEEP_INTERVAL=2
readonly WORD_PATTERN='^[A-Za-z0-9._=-]+$'
# deploy.sh tags the image with twelve hex digits of its ID.
readonly TAG_PATTERN='^[0-9a-f]{12}$'
readonly SHOWN_LENGTH=40
readonly USAGE='make gateway-upkeep ARGS="reservations --older-than 15"'
# What a failure that is not a refusal tells the operator to do first: a credit is
# a row of its own (no idempotency key), so a rerun after one that did commit
# credits twice.
readonly READ_BEFORE_RERUN="Read the open reservations (make gateway-upkeep ARGS=\"reservations --older-than 15\") or the audit rows (docs/operations/runbooks/budget-exhaustion.md) before running it again, because a credit is a new row on every run."

# The words of ARGS, and the image's tag helm_chart passes to the chart. The
# first is set by split_arguments, the second by read_release_image.
words=()
# shellcheck disable=SC2034  # read by helm_chart (common.sh)
tag=""
job=""
verdict=""
job_output=""

# split_arguments: ${ARGS} into ${words}, or a refusal. The check is for the
# letters of the C locale, whatever the operator's locale says.
split_arguments() {
  local text="${ARGS:-}" word shown
  local LC_ALL=C
  [[ "${text}" != *$'\n'* ]] ||
    die "ARGS holds a newline; give the subcommand and its arguments on one line: ${USAGE}"
  IFS=$' \t' read -r -a words <<<"${text}"
  ((${#words[@]} > 0)) ||
    die "ARGS is empty; name the subcommand (reservations, close, credit, expire or expire-audit) and its arguments: ${USAGE}"
  for word in "${words[@]}"; do
    [[ "${word}" =~ ${WORD_PATTERN} ]] && continue
    shown="$(printf '%s' "${word}" | printable_ascii | cut -c "1-${SHOWN_LENGTH}")"
    die "ARGS holds a word with a character the command never needs: '${shown}'. A word is letters, digits, '.', '_', '=' and '-' only (the command's arguments are slugs, numbers, dates and IDs); a quote, a backslash, a '\$', a backtick, a newline, a glob or a shell character is refused, and no word is ever read as shell"
  done
}

# The Secret of the role gateway_upkeep (`make up` makes it): its `uri` is the
# connection string the Job reads. Only the names of the keys are read (jq prints
# them), never a value, and a key that is there and empty counts as missing.
require_upkeep_secret() {
  local present
  present="$(kctl -n "${NAMESPACE}" get secret "${UPKEEP_SECRET}" -o json 2>/dev/null |
    jq -r '.data // {} | to_entries[] | select(.value != "") | .key')" ||
    die "Secret ${UPKEEP_SECRET} does not exist; run 'make up' first (it holds the connection string of the database role gateway_upkeep)"
  grep -qxF -- uri <<<"${present}" ||
    die "Secret ${UPKEEP_SECRET} has no key 'uri', or it is empty; run 'make up' first, after deleting the Secret (kubectl -n ${NAMESPACE} delete secret ${UPKEEP_SECRET}): 'make up' keeps a Secret that exists"
}

# The image the Job runs: the release's own (deploy.sh passes it as image.tag,
# which Helm keeps), so the command is the one the deployed gateway was built
# with. Sets ${tag}. A release that is missing, or whose image is not one that
# `make deploy` built (twelve hex digits of meridian), is a refusal.
read_release_image() {
  local values repository
  values="$(helmc_bounded -n "${NAMESPACE}" get values "${RELEASE}" -o json 2>/dev/null)" ||
    die "the Helm release ${RELEASE} is not installed, so there is no image to run; run 'make deploy' first"
  repository="$(jq -r '.image.repository // empty' <<<"${values}" 2>/dev/null)" || repository=""
  tag="$(jq -r '.image.tag // empty' <<<"${values}" 2>/dev/null)" || tag=""
  [[ "${repository}" == "${IMAGE_REPOSITORY}" && "${tag}" =~ ${TAG_PATTERN} ]] ||
    die "the Helm release ${RELEASE} does not say it runs an image of '${IMAGE_REPOSITORY}' tagged by 'make deploy' (twelve hex digits); run 'make deploy' first"
}

# wait_for_job: sets ${verdict} to succeeded or failed, or leaves it empty when
# the Job did not finish within the looks. A transient kubectl error is not a
# verdict: ask again.
wait_for_job() {
  local look state
  for ((look = 1; look <= UPKEEP_LOOKS; look++)); do
    state="$(job_state "${job}" 2>/dev/null)" || state=unknown
    case "${state}" in
      succeeded | failed)
        verdict="${state}"
        return 0
        ;;
    esac
    ((look == UPKEEP_LOOKS)) || sleep "${UPKEEP_INTERVAL}"
  done
}

# read_job_output: the pod's log into ${job_output}, through printable_ascii: a
# log can quote data, an escape sequence must not reach the terminal, and a
# driver's error can quote a connection string. Read once, so the same text is
# printed and searched for the command's refusal.
read_job_output() {
  job_output="$(kctl -n "${NAMESPACE}" logs "job/${job}" 2>&1 | printable_ascii || true)"
}

# print_job_output: ${job_output} on stdout, nothing for an empty log.
print_job_output() {
  [[ -z "${job_output}" ]] || printf '%s\n' "${job_output}"
}

# The command's refusal is a line of its own that starts `ERROR GU` and three
# digits (src/meridian/platform/cli/gateway.py). Any other failure may have come
# after the commit.
job_output_is_a_refusal() {
  grep -Eq '^ERROR GU[0-9]{3}( |$)' <<<"${job_output}"
}

# A refusal changed nothing only when no batch ran before it. The two expiries
# remove in batches, each its own transaction, and when a failure follows some,
# the command prints a line of its own after the error: `removed N usage rows in
# B batch(es) before the failure ...` (`audit rows` for expire-audit). That line
# is read first: with it the state is known, whatever the error above it was.
job_output_has_removals_before_the_failure() {
  grep -Eq '^removed [0-9]+ (usage|audit) rows in [0-9]+ batch\(es\) before the failure' <<<"${job_output}"
}

split_arguments
need_tools kubectl helm jq timeout
need_cluster
require_upkeep_secret
read_release_image

# A run's own name: the time to the second and this process's ID, which no
# other run shares at once. A DNS label of 21 characters (the chart allows 32).
job="${UPKEEP_JOB}-$(date -u +%y%m%d-%H%M%S)-$$"
args_json="$(printf '%s\n' "${words[@]}" | jq -R . | jq -sc .)"
manifest="$(helm_chart template --set jobs.upkeep.enabled=true \
  --set-string "jobs.upkeep.runSuffix=${job#"${UPKEEP_JOB}"-}" \
  --set-json "jobs.upkeep.args=${args_json}" \
  --show-only templates/job-upkeep.yaml)" ||
  die "helm could not render the upkeep Job (its error is above)"
log "job ${job}: meridian gateway ${words[*]}"
kctl apply --server-side --force-conflicts -f - <<<"${manifest}" >/dev/null ||
  die "kubectl could not apply the upkeep Job (its error is above)"
wait_for_job
case "${verdict}" in
  succeeded)
    log "job ${job} succeeded; its output:"
    read_job_output
    print_job_output
    ;;
  failed)
    log "job ${job} failed; its output:"
    read_job_output
    print_job_output
    if job_output_has_removals_before_the_failure; then
      die "the upkeep job ${job} failed after batches that had committed: the line above that says 'before the failure' tells how much the command removed, and that stays removed; running it again continues from there once the cause above is dealt with. The Job and its output stay for a day: kubectl -n ${NAMESPACE} logs job/${job}"
    fi
    if job_output_is_a_refusal; then
      die "the upkeep job ${job} failed with a refusal of the command (an ERROR GUnnn line above): a refusal changed nothing, so fix the arguments and run it again. The Job and its output stay for a day: kubectl -n ${NAMESPACE} logs job/${job}"
    fi
    die "the upkeep job ${job} failed, and its output holds no refusal of the command (no ERROR GUnnn line), so the change may have been applied: a deadline, a lost connection or a crash can come after the commit. ${READ_BEFORE_RERUN} The Job and its output stay for a day: kubectl -n ${NAMESPACE} logs job/${job}"
    ;;
  *)
    log "job ${job} did not finish; its output so far:"
    read_job_output
    print_job_output
    die "the upkeep job ${job} did not finish in $((UPKEEP_LOOKS * UPKEEP_INTERVAL))s (its deadline is 120s, so the pod may not have started: kubectl -n ${NAMESPACE} describe job/${job}), so the change may have been applied. ${READ_BEFORE_RERUN}"
    ;;
esac
