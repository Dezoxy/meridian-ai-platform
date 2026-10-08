#!/usr/bin/env bash
# Fill the local cluster with synthetic claims so the pages show content:
# `make demo-seed` (S098). Kind only, synthetic data only. The gateway runs in
# replay mode on kind: its answers are simulated, no model is called, so it costs
# nothing, and a claim that needs the model's answer is referred to a person.
#   COUNT=40          how many claims of data/synthetic/claims.json, in the file's
#                     order, from 1 to 47 (all 47 are golden claims; the seven after
#                     the first 40 were added later). Anything else stops with a
#                     usage line.
#   PACE_SECONDS=10   the pause after a claim that was triaged, from 0 to 60. The
#                     tenant's gateway windows are 10 requests in 10 seconds and
#                     10,000 tokens a minute, and a triage that asks the model
#                     reserves about 1,300: 10 keeps a run under both.
#   TAKE_CLUSTER=1    as for `make deploy`: go on although another checkout holds
#                     the cluster. This script only reads who holds it.
#   MEMINFO_FILE      the file the free memory is read from (default /proc/meminfo;
#                     a test points it elsewhere).
# What it does, one claim at a time:
#   1. posts the claim to the Claims API through the edge, exactly as demo.sh does
#      (same URL, same body, a traceparent of its own). The answer to a post is
#      synchronous: the 201 carries the state the triage left the claim in.
#   2. reads the claim's state over the JSON route the adjuster's pages have
#      (GET /adjuster/claims/<id>/proposal) only when the post did not settle it:
#      a state of submitted or triaging, a 409 that says another request is
#      triaging it, or a 5xx after which the claim is stored. It polls until the
#      state is another one or SETTLE_TIMEOUT seconds pass; a claim that is still
#      in one of those two states then is counted as "not settled" and the run
#      goes on. A claim that already exists with the same triage proposal (409) is
#      skipped and its state is read once; one that exists with other content (409)
#      is counted apart and named by its ID.
#   3. pauses PACE_SECONDS after a claim that was triaged, then posts the next.
# Decides nothing: a claim referred to an adjuster stays in the adjuster's queue.
# A claim whose triage fails is counted and shown, and the run goes on. It does not
# wait for traces in Tempo (demo.sh does). It prints each claim's ID, what
# happened and its state, then the counts by state and where to look: never a
# claimant's name, a policy holder, a description or any other field.
# A post that curl could not finish in 60 seconds (its exit status 28) may have
# stored the claim: it is read from the route like a 5xx, and refused only when the
# route has no such claim. Any other curl failure, and a claim of the file with no
# claim ID, end the run there with a refusal and the summary of what had happened.
# An answer's sentence is printed only when it is one of the four 409 sentences
# above; any other is printed as a fixed text, with its HTTP status.
# Exit status: non-zero when the edge cannot be reached (at the start, or in the
# middle of the run), a post is refused for any reason but the 409s above (the run
# stops there, after the summary), or no claim could be posted (every claim
# answered 409 with other content, or none was reached). A second run that skips
# every claim exits 0.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

CLAIMS_FILE="${KIND_DIR}/../../data/synthetic/claims.json"
readonly CLAIMS_FILE
readonly CLAIMS_URL=http://claims.meridian.localhost:8088/claims
readonly ADJUSTER_URL=http://claims.meridian.localhost:8088/adjuster/claims
readonly CLAIMANT_URL=http://claims.meridian.localhost:8088/claimant/claims
readonly HEALTH_URL=http://claims.meridian.localhost:8088/healthz
# The Claims API's 409 sentences (triaging.py; a test holds them equal).
readonly ALREADY_TRIAGED="the claim already has a triage proposal"
readonly BEING_TRIAGED="the claim is being triaged"
readonly DIFFERENT_SUBMISSION="the claim exists with a different submission"
readonly TRIAGE_CAP="the claim has been triaged five times; an adjuster decides it"
readonly MAX_CLAIMS=47
readonly DEFAULT_COUNT=40
readonly DEFAULT_PACE=10
readonly MAX_PACE=60
readonly EDGE_TIMEOUT=60
readonly POST_TIMEOUT=60
readonly CURL_TIMEOUT_STATUS=28 # curl's exit status for a -m timeout
readonly READ_TIMEOUT=15
# How long a claim may stay submitted or triaging before it is counted as not
# settled: the Claims API's lease on a triage, which another request may take over
# after (120 s, TRIAGE_LEASE_SECONDS). A post settles a claim before it answers, so
# this is the wait for a claim some other request is triaging.
readonly SETTLE_TIMEOUT=120
readonly POLL_INTERVAL=3
# The machine is small and shared: under this much available memory, and while a
# test database is running, the run does not start.
readonly MIN_AVAILABLE_MB=2500
readonly TEST_DATABASE_PATTERN='pytest-(db|redis)'
readonly STATE_PATTERN='^[a-z_]{1,40}$'
readonly CLAIM_ID_PATTERN='^CLM-[0-9]{4}$'

# clean TEXT: TEXT without any byte that is not printable ASCII, so that an answer
# cannot inject an escape sequence or an extra line into the output.
clean() { printf '%s' "$1" | LC_ALL=C tr -cd '[:print:]'; }
shown() { clean "$1" | cut -c 1-40; }

usage() {
  die "$1; usage: make demo-seed [COUNT=<1 to ${MAX_CLAIMS}, default ${DEFAULT_COUNT}>] [PACE_SECONDS=<0 to ${MAX_PACE}, default ${DEFAULT_PACE}>]"
}

check_arguments() {
  COUNT="${COUNT:-${DEFAULT_COUNT}}"
  PACE_SECONDS="${PACE_SECONDS:-${DEFAULT_PACE}}"
  if ! [[ "${COUNT}" =~ ^[0-9]{1,2}$ ]] || ((10#${COUNT} < 1 || 10#${COUNT} > MAX_CLAIMS)); then
    usage "COUNT must be a whole number from 1 to ${MAX_CLAIMS} (got: $(shown "${COUNT}"))"
  fi
  if ! [[ "${PACE_SECONDS}" =~ ^[0-9]{1,2}$ ]] || ((10#${PACE_SECONDS} > MAX_PACE)); then
    usage "PACE_SECONDS must be a whole number from 0 to ${MAX_PACE} (got: $(shown "${PACE_SECONDS}"))"
  fi
  COUNT=$((10#${COUNT}))
  PACE_SECONDS=$((10#${PACE_SECONDS}))
}

# check_machine: the run starts only with room to spare and no test database up.
check_machine() {
  local file="${MEMINFO_FILE:-/proc/meminfo}" available names running
  if [[ -r "${file}" ]]; then
    available="$(awk '/^MemAvailable:/ { print int($2 / 1024) }' "${file}")"
    [[ "${available}" =~ ^[0-9]+$ ]] || die "could not read MemAvailable from ${file}"
    ((available >= MIN_AVAILABLE_MB)) ||
      die "only ${available} MB of memory is available, less than the ${MIN_AVAILABLE_MB} MB this run needs; stop what you can (the cluster's node, a test run) and run it again"
  else
    log "${file} cannot be read: the free memory was not checked"
  fi
  names="$(docker ps --format '{{.Names}}' 2>&1)" ||
    die "could not list the running containers, so a test database cannot be ruled out: $(shown "${names}")"
  running="$(grep -E "${TEST_DATABASE_PATTERN}" <<<"${names}" | tr '\n' ' ' || true)"
  [[ -z "${running}" ]] ||
    die "a test database container is running ($(shown "${running}")); remove it (the tests' own run does) and run this again"
}

need_tools curl jq openssl docker kubectl
check_arguments
require_local_docker
check_machine
need_cluster
check_cluster_holder "make demo-seed"
[[ -f "${CLAIMS_FILE}" ]] || die "no ${CLAIMS_FILE}"
available_claims="$(jq 'length' "${CLAIMS_FILE}")"
((available_claims >= COUNT)) ||
  die "${CLAIMS_FILE} holds ${available_claims} claims, fewer than COUNT=${COUNT}"

response="" state="" detail="" status="" claim_id="" other_content=""
# The counts. A state that is none of the first six is counted as "another state".
n_referred=0 n_approved=0 n_rejected=0 n_documents=0 n_failed=0 n_withdrawn=0 n_other=0
n_unsettled=0 n_skipped=0 n_different=0 n_capped=0 n_refused=0
n_reached=0 # claims posted, skipped or at the cap: the ones the API took
cleanup() {
  if [[ -n "${response}" ]]; then rm -f "${response}"; fi
}

# Right after a rollout the edge can still answer 503 for a moment: poll /healthz,
# which changes nothing, until it answers 200 before the first post.
wait_for_edge() {
  local deadline=$((SECONDS + EDGE_TIMEOUT)) code=""
  while ((SECONDS < deadline)); do
    code="$(curl -q --noproxy '*' -sS -m 5 -o /dev/null -w '%{http_code}' "${HEALTH_URL}" 2>/dev/null || true)"
    [[ "${code}" == 200 ]] && return 0
    sleep 1
  done
  die "the edge did not answer ${HEALTH_URL} with 200 in ${EDGE_TIMEOUT}s (last: $(shown "${code}"); is 'make deploy' done?)"
}

# post_json URL BODY TRACE_ID: the HTTP status on stdout, the body in ${response}.
post_json() {
  local url=$1 body=$2 trace=$3 parent
  parent="$(openssl rand -hex 8)"
  printf '%s' "${body}" |
    curl -q --noproxy '*' -sS -m "${POST_TIMEOUT}" -o "${response}" -w '%{http_code}' \
      -H 'Content-Type: application/json' \
      -H "traceparent: 00-${trace}-${parent}-01" \
      --data-binary @- "${url}" 2>/dev/null
}

# answer_state: the state in ${response} into ${state}, or empty when there is none
# or it is not a lowercase word. Nothing else of the answer is read.
answer_state() {
  state="$(clean "$(jq -r '.state | strings' "${response}" 2>/dev/null || true)")"
  [[ "${state}" =~ ${STATE_PATTERN} ]] || state=""
}

# answer_detail: the API's error sentence in ${response} into ${detail}, or empty
# when the answer has none or it is not a sentence (a validation error is a list
# that quotes what was sent, and is never printed).
answer_detail() {
  detail="$(clean "$(jq -r '.detail | strings' "${response}" 2>/dev/null || true)" | cut -c 1-200)"
}

# known_sentence TEXT: TEXT when it is one of the Claims API's four 409 sentences,
# and otherwise a fixed text: a refusal prints no sentence the script does not know.
known_sentence() {
  case "$1" in
    "${ALREADY_TRIAGED}" | "${BEING_TRIAGED}" | "${DIFFERENT_SUBMISSION}" | "${TRIAGE_CAP}")
      printf '%s' "$1"
      ;;
    *) printf '%s' "(an answer this script does not know)" ;;
  esac
}

in_progress() { [[ "$1" == submitted || "$1" == triaging ]]; }

# read_state ID: the claim's state over the JSON route into ${state}. 0 when it was
# read, 1 when it could not be (no answer, not 200, no usable state), 2 when the
# route says the claim does not exist (404).
read_state() {
  local code
  state=""
  if ! code="$(curl -q --noproxy '*' -sS -m "${READ_TIMEOUT}" -o "${response}" -w '%{http_code}' \
    "${ADJUSTER_URL}/$1/proposal" 2>/dev/null)"; then
    return 1
  fi
  code="$(clean "${code}")"
  [[ "${code}" != 404 ]] || return 2
  [[ "${code}" == 200 ]] || return 1
  answer_state
  [[ -n "${state}" ]]
}

# settle_claim ID: read the claim's state until it is not in progress or
# SETTLE_TIMEOUT passes. 0 settled (${state}), 1 the time passed (${state} is the
# last state read, or empty), 2 the claim does not exist.
settle_claim() {
  local deadline=$((SECONDS + SETTLE_TIMEOUT)) result=0
  while :; do
    result=0
    read_state "$1" || result=$?
    ((result != 2)) || return 2
    if ((result == 0)) && ! in_progress "${state}"; then return 0; fi
    ((SECONDS < deadline)) || return 1
    sleep "${POLL_INTERVAL}"
  done
}

# tally STATE: one more claim in the count of its state.
tally() {
  case "$1" in
    awaiting_adjuster) n_referred=$((n_referred + 1)) ;;
    approved) n_approved=$((n_approved + 1)) ;;
    rejected) n_rejected=$((n_rejected + 1)) ;;
    documents_requested) n_documents=$((n_documents + 1)) ;;
    triage_failed) n_failed=$((n_failed + 1)) ;;
    withdrawn) n_withdrawn=$((n_withdrawn + 1)) ;;
    *) n_other=$((n_other + 1)) ;;
  esac
}

# report WORD STATE: the claim's line. WORD is what happened to it.
report() {
  printf '%-9s  %-12s  %s\n' "${claim_id}" "$1" "${2:-(unread)}"
}

# finish_claim WORD: the line and the count of a claim that was posted or waited
# for, once settle_claim (or the post's own answer) has left ${state}.
finish_claim() {
  local word=$1 result=${2:-0}
  if ((result == 1)); then
    word=not-settled
    n_unsettled=$((n_unsettled + 1))
  else
    tally "${state}"
  fi
  n_reached=$((n_reached + 1))
  report "${word}" "${state}"
}

# refuse TEXT: the post was refused; the run stops here, non-zero.
refuse() {
  n_refused=1
  printf 'error: %s\n' "$1" >&2
}

# handle_conflict: the 409 of the last post. 0 when the run goes on, 1 when the
# post was refused for a reason the run does not expect. Sets ${paced} to yes for
# a claim another request was triaging and this run waited for.
handle_conflict() {
  local result=0
  answer_detail
  case "${detail}" in
    "${ALREADY_TRIAGED}")
      read_state "${claim_id}" || true
      n_skipped=$((n_skipped + 1))
      n_reached=$((n_reached + 1))
      report skipped "${state}"
      ;;
    "${DIFFERENT_SUBMISSION}")
      n_different=$((n_different + 1))
      other_content="${other_content:+${other_content}, }${claim_id}"
      report different ""
      ;;
    "${TRIAGE_CAP}")
      read_state "${claim_id}" || true
      n_capped=$((n_capped + 1))
      n_reached=$((n_reached + 1))
      report at-cap "${state}"
      ;;
    "${BEING_TRIAGED}")
      settle_claim "${claim_id}" || result=$?
      ((result != 2)) || {
        refuse "${claim_id}: HTTP 409 $(known_sentence "${detail}"), and the claim is not there to read"
        return 1
      }
      finish_claim waited "${result}"
      ;;
    *)
      refuse "${claim_id}: HTTP 409 $(known_sentence "${detail}")"
      return 1
      ;;
  esac
}

# handle_stored: the claim is stored and its triage ran (a 201) or failed after
# the claim was stored (a 5xx), or the post timed out (${status} is "timeout"; the
# claim may be stored): its state is the answer's, or the route's when the answer
# settled nothing. 1 when the route says there is no such claim, which makes the
# post a plain refusal (a 503 of the edge, say).
handle_stored() {
  local result=0 post_detail=""
  state=""
  if [[ "${status}" == 201 ]]; then answer_state; fi
  # The post's sentence, kept before the route's answers take ${response}. A post
  # that timed out has no answer to read.
  if [[ "${status}" != timeout ]]; then
    answer_detail
    post_detail="$(known_sentence "${detail}")"
  fi
  if [[ -z "${state}" ]] || in_progress "${state}"; then
    settle_claim "${claim_id}" || result=$?
    if ((result == 2)); then
      if [[ "${status}" == timeout ]]; then
        refuse "${claim_id}: the post timed out after ${POST_TIMEOUT}s, and the claim is not stored"
      else
        refuse "${claim_id}: HTTP ${status} ${post_detail}, and the claim is not stored"
      fi
      return 1
    fi
  fi
  finish_claim posted "${result}"
}

# post_claim CLAIM_JSON: one claim, to the end of its line. 1 stops the run.
post_claim() {
  local trace_id curl_status=0
  trace_id="$(openssl rand -hex 16)"
  paced=no
  status="$(post_json "${CLAIMS_URL}" "$1" "${trace_id}")" || curl_status=$?
  status="$(clean "${status}")"
  if ((curl_status == CURL_TIMEOUT_STATUS)); then
    # The claim may be stored and its triage running: read it like a 5xx.
    status=timeout
  elif ((curl_status != 0)); then
    # curl's own sentence can hold the URL and is not printed; its status is.
    local advice=""
    ((index > 1)) || advice=" (is 'make deploy' done?)"
    refuse "could not reach the Claims API at claim ${index} of ${COUNT} (${claim_id}; curl exit status ${curl_status})${advice}"
    return 1
  fi
  case "${status}" in
    201 | 500 | 502 | 503 | 504 | timeout)
      handle_stored || return 1
      paced=yes
      ;;
    409)
      handle_conflict || return 1
      [[ "${detail}" != "${BEING_TRIAGED}" ]] || paced=yes
      ;;
    *)
      answer_detail
      refuse "${claim_id}: HTTP ${status} $(known_sentence "${detail}")"
      return 1
      ;;
  esac
}

# count_row LABEL N: one row of the summary, when N is not zero.
count_row() {
  if (($2 > 0)); then printf '  %-48s %d\n' "$1" "$2"; fi
}

print_summary() {
  printf '\n'
  log "claims by what they are now"
  count_row "referred to an adjuster" "${n_referred}"
  count_row "approved" "${n_approved}"
  count_row "rejected" "${n_rejected}"
  count_row "awaiting documents" "${n_documents}"
  count_row "triage failed" "${n_failed}"
  count_row "withdrawn" "${n_withdrawn}"
  count_row "another state" "${n_other}"
  count_row "not settled" "${n_unsettled}"
  count_row "skipped (already there, same content)" "${n_skipped}"
  count_row "at the triage cap (an adjuster decides it)" "${n_capped}"
  count_row "exists with other content" "${n_different}"
  if [[ -n "${other_content}" ]]; then
    printf '  claims with other content: %s\n' "${other_content}"
  fi
  if ((n_unsettled > 0)); then
    printf '  not settled: still submitted or triaging after %ss\n' "${SETTLE_TIMEOUT}"
  fi
  if ((n_failed > 0)); then
    printf 'A claim whose triage failed is triaged again when this is run again.\n'
    printf 'The gateway refuses a call over a window of the tenant: tenant-request-rate (10 requests in 10 seconds) or tenant-token-rate (10,000 tokens a minute) (docs/demo.md).\n'
    printf 'The default pause of 10 seconds keeps a run under both; a larger PACE_SECONDS spaces the claims further apart.\n'
  fi
  printf 'Look at the claims: %s (the adjuster'\''s queue: the claims that wait for an adjuster and those whose triage failed)\n' "${ADJUSTER_URL}"
  printf '                    %s (the claimant'\''s page: look a claim up by its ID)\n' "${CLAIMANT_URL}"
}

trap cleanup EXIT
response="$(mktemp)"
wait_for_edge
log "seeding ${COUNT} claims of data/synthetic/claims.json one at a time, ${PACE_SECONDS}s apart; a claim still in progress after ${SETTLE_TIMEOUT}s counts as not settled"
index=0
while IFS= read -r claim <&3; do
  index=$((index + 1))
  claim_id="$(clean "$(jq -r '.claim_id' <<<"${claim}")")"
  if ! [[ "${claim_id}" =~ ${CLAIM_ID_PATTERN} ]]; then
    refuse "claim ${index} of ${CLAIMS_FILE} has no claim ID of the form CLM-0000"
    break
  fi
  post_claim "${claim}" || break
  if [[ "${paced}" == yes ]] && ((index < COUNT && PACE_SECONDS > 0)); then
    sleep "${PACE_SECONDS}"
  fi
done 3< <(jq -c --argjson count "${COUNT}" '.[:$count][]' "${CLAIMS_FILE}")
print_summary
((n_refused == 0)) || exit 1
((n_reached > 0)) || die "no claim could be posted: every claim of the ${COUNT} answered that it exists with other content"
