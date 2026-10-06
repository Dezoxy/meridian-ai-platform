#!/usr/bin/env bash
# Show the platform working end to end: `make demo` (deploys first). With
# `make demo DECISION=reject` (approve, reject or request_documents; approve
# when unset) the adjuster's decision is the one you name.
#   1. posts the first synthetic claim that has no triage proposal yet to the
#      Claims API through the edge, with a W3C traceparent the script generated,
#      and prints the claim's state
#   2. when the rules referred the claim to an adjuster (state
#      awaiting_adjuster, its run paused), posts the decision to
#      /claims/<id>/decision with a traceparent of its own: the Claims API
#      records it and the runtime resumes the run, which writes one note
#   3. reads each trace back from Tempo through Grafana's datasource proxy, the
#      way an owner would see it, and PASSes only when it has spans from every
#      service expected of it and its span counts have settled (the same in
#      three readings in a row, six seconds without change: the services flush
#      their spans separately, so an earlier reading can be partial):
#        the triage trace: claims-api, agent-runtime, policy-mcp, knowledge-mcp
#          and model-gateway. The graph calls policy_lookup and claim_history on
#          the policy server and wording_search on the knowledge server, which
#          embeds the query through the gateway, so every triage of a claim
#          whose policy exists leaves spans of the five. A referred claim's
#          triage also calls claims-mcp (request_approval), which is not
#          required: a claim that is not referred does not call it.
#        the decision trace: claims-api, agent-runtime and claims-mcp (the
#          resumed run's note).
# Each run uses the next claim in data/synthetic/claims.json; a claim that was
# triaged before answers 409 and is skipped (a claim still awaiting its
# adjuster is skipped too: decide it by hand), and so is a claim ID someone
# submitted with other content, through the claimant's form say. A claim that
# is not referred needs no decision, and the next `make demo` posts the next
# claim. Prints identifiers, states and the route, never a claimant field.
# Exits non-zero on any failure.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

CLAIMS_FILE="${KIND_DIR}/../../data/synthetic/claims.json"
readonly CLAIMS_FILE
readonly CLAIMS_URL=http://claims.meridian.localhost:8088/claims
readonly HEALTH_URL=http://claims.meridian.localhost:8088/healthz
readonly EDGE_TIMEOUT=60
readonly ALREADY_TRIAGED="the claim already has a triage proposal"
readonly DIFFERENT_SUBMISSION="the claim exists with a different submission"
readonly AWAITING_ADJUSTER=awaiting_adjuster
readonly GRAFANA_SERVICE=svc/kube-prometheus-stack-grafana
readonly TRIAGE_SERVICES=(claims-api agent-runtime policy-mcp knowledge-mcp model-gateway)
readonly DECISION_SERVICES=(claims-api agent-runtime claims-mcp)
readonly POLL_TIMEOUT=120
readonly POLL_INTERVAL=3
# Readings with unchanged counts before PASS. Three at POLL_INTERVAL=3 is 6 s
# without change, longer than the span exporters' 5 s batch delay.
readonly SETTLE_POLLS=3
readonly POST_TIMEOUT=60

need_tools curl jq openssl base64 kubectl
need_cluster
[[ -f "${CLAIMS_FILE}" ]] || die "no ${CLAIMS_FILE}"

# clean TEXT: TEXT without any byte that is not printable ASCII. Everything this
# script prints that came from an HTTP answer or from Tempo goes through it, so
# a hostile answer cannot inject terminal escape sequences or extra lines.
clean() { printf '%s' "$1" | LC_ALL=C tr -cd '[:print:]'; }

# check_decision: DECISION is one of the three words the Claims API accepts, or
# unset (approve). Nothing is posted before this has passed.
check_decision() {
  DECISION="${DECISION:-approve}"
  case "${DECISION}" in
    approve | reject | request_documents) ;;
    *) die "DECISION must be approve, reject or request_documents (got: $(clean "${DECISION}"))" ;;
  esac
}
check_decision

pf_pid="" pf_log="" response="" decision_trace_id=""
cleanup() {
  if [[ -n "${pf_pid}" ]]; then
    kill "${pf_pid}" 2>/dev/null || true
    wait "${pf_pid}" 2>/dev/null || true # kubectl is gone when demo.sh returns
  fi
  if [[ -n "${pf_log}" ]]; then rm -f "${pf_log}"; fi
  if [[ -n "${response}" ]]; then rm -f "${response}"; fi
}

# Right after a rollout the edge can still answer 503 for a moment while Envoy
# learns the new pod. /healthz changes nothing, so poll it until it answers 200
# before the first POST (a POST that Envoy refused would be safe to repeat, but
# one the app refused with a 503 would not).
wait_for_edge() {
  local deadline=$((SECONDS + EDGE_TIMEOUT)) code=""
  while ((SECONDS < deadline)); do
    code="$(curl -q --noproxy '*' -sS -m 5 -o /dev/null -w '%{http_code}' "${HEALTH_URL}" 2>/dev/null || true)"
    [[ "${code}" == 200 ]] && return 0
    sleep 1
  done
  die "the edge did not answer ${HEALTH_URL} with 200 in ${EDGE_TIMEOUT}s (last: $(clean "${code}"); is 'make deploy' done?)"
}

# post_json URL BODY TRACE_ID: print the HTTP status; the body is left in
# ${response}. The parent span ID is random: the Claims API's span becomes a
# child of a span nobody exported, which Tempo shows as the trace's root.
post_json() {
  local url=$1 body=$2 trace=$3 parent
  parent="$(openssl rand -hex 8)"
  printf '%s' "${body}" |
    curl -q --noproxy '*' -sS -m "${POST_TIMEOUT}" -o "${response}" -w '%{http_code}' \
      -H 'Content-Type: application/json' \
      -H "traceparent: 00-${trace}-${parent}-01" \
      --data-binary @- "${url}"
}

# Post claims in order until one is triaged. Sets ${claim_id}, ${status} and
# ${trace_id}. A claim that already has a proposal, or whose ID exists with
# other content (the claimant's form stamps its own report date), is skipped;
# any other answer stops the demo, with the API's fixed error text.
submit_next_claim() {
  local claim detail
  response="$(mktemp)"
  while IFS= read -r claim; do
    claim_id="$(clean "$(jq -r '.claim_id' <<<"${claim}")")"
    trace_id="$(openssl rand -hex 16)"
    if ! status="$(post_json "${CLAIMS_URL}" "${claim}" "${trace_id}")"; then
      die "could not reach ${CLAIMS_URL} (is 'make deploy' done?)"
    fi
    status="$(clean "${status}")"
    case "${status}" in
      201) return 0 ;;
      409)
        detail="$(clean "$(jq -r '.detail // empty' "${response}" 2>/dev/null || true)")"
        if [[ "${detail}" == "${ALREADY_TRIAGED}" ]]; then
          log "${claim_id}: already triaged, trying the next claim"
          continue
        fi
        if [[ "${detail}" == "${DIFFERENT_SUBMISSION}" ]]; then
          log "${claim_id}: exists with a different submission (the claimant's form stamps its own report date), trying the next claim"
          continue
        fi
        die "${claim_id}: 409 ${detail}"
        ;;
      *)
        detail="$(jq -r '.detail | tostring' "${response}" 2>/dev/null || head -c 200 "${response}")"
        detail="$(clean "${detail}")"
        die "${claim_id}: HTTP ${status} ${detail}"
        ;;
    esac
  done < <(jq -c '.[]' "${CLAIMS_FILE}")
  die "every claim in data/synthetic/claims.json is already triaged; nothing left to post"
}

# decide_claim: post ${DECISION} for ${claim_id}, which waits for an adjuster.
# Sets ${decision_trace_id}; the answer is left in ${response}. Any answer but
# 200 stops the demo, with the API's fixed error text.
decide_claim() {
  local code detail
  decision_trace_id="$(openssl rand -hex 16)"
  if ! code="$(post_json "${CLAIMS_URL}/${claim_id}/decision" \
    "$(jq -cn --arg decision "${DECISION}" '{decision: $decision}')" "${decision_trace_id}")"; then
    die "could not reach ${CLAIMS_URL}/${claim_id}/decision"
  fi
  code="$(clean "${code}")"
  if [[ "${code}" != 200 ]]; then
    detail="$(jq -r '.detail | tostring' "${response}" 2>/dev/null || head -c 200 "${response}")"
    die "${claim_id}: decision HTTP ${code} $(clean "${detail}")"
  fi
}

# gcurl ARGS...: curl against Grafana with basic auth. The password reaches curl
# on stdin as a config line, so it never appears in a process listing.
gcurl() {
  printf 'user = "admin:%s"\n' "${password}" | curl -q --noproxy '*' -sS -m 15 -K - "$@"
}

start_grafana_forward() {
  pf_log="$(mktemp)"
  # kubectl itself is backgrounded (not the kctl function), so $! is its PID and
  # the EXIT trap can stop it.
  kubectl --kubeconfig "${KUBECONFIG_FILE}" --context "${KUBE_CONTEXT}" \
    -n observability port-forward --address 127.0.0.1 "${GRAFANA_SERVICE}" :80 \
    >"${pf_log}" 2>&1 &
  pf_pid=$!
  local port="" waited=0
  while [[ -z "${port}" && ${waited} -lt 30 ]]; do
    port="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9]*\) ->.*/\1/p' "${pf_log}" | head -n 1)"
    [[ -n "${port}" ]] || { sleep 1; waited=$((waited + 1)); }
  done
  [[ -n "${port}" ]] || die "could not port-forward to Grafana"
  proxy="http://127.0.0.1:${port}/api/datasources/proxy/uid"
}

# The services in the trace and their span counts, one "service count" line
# each, from Tempo's trace answer (OTLP JSON; "batches" in the v1 shape).
readonly SPANS_PER_SERVICE='
  (.batches // .resourceSpans // .trace.resourceSpans // [])
  | map({
      service: (first(.resource.attributes[]? | select(.key == "service.name") | .value.stringValue) // "unknown"),
      spans: ([.scopeSpans[]?.spans | length] | add // 0)
    })
  | group_by(.service)[]
  | "\(.[0].service) \(map(.spans) | add)"'

# has_every_service SERVICE...: true when ${counts} lists every service given.
has_every_service() {
  local service
  for service in "$@"; do
    grep -q "^${service} " <<<"${counts}" || return 1
  done
}

# wait_for_trace TRACE_ID SERVICE...: wait until Tempo has spans of every
# service given for the trace and the per-service counts have settled: the same
# text in SETTLE_POLLS readings in a row, each with every service. Leaves the
# last counts in ${counts}, and in ${complete} whether the last reading had
# every service (yes or no). ${complete_readings} and ${partial_readings} count
# all the readings that had every service and all that did not (a 404 is one
# of those). Tempo answers 404 until it has the trace, and the services flush
# their spans separately, so an earlier answer can be partial.
wait_for_trace() {
  local id=$1 deadline=$((SECONDS + POLL_TIMEOUT)) out http body
  local previous="" settled=0
  shift
  counts="" complete=no complete_readings=0 partial_readings=0
  while ((SECONDS < deadline)); do
    kill -0 "${pf_pid}" 2>/dev/null ||
      die "the Grafana port-forward died: $(clean "$(tail -n 3 "${pf_log}")")"
    # The status follows the body on its own line. Only 200 and 404 (Tempo has
    # not got the trace yet) are expected; anything else ends the demo.
    out="$(gcurl -w '\n%{http_code}' "${proxy}/tempo/api/traces/${id}" 2>&1)" ||
      die "could not query Tempo through Grafana: $(clean "${out}")"
    http="${out##*$'\n'}"
    body="${out%$'\n'*}"
    case "${http}" in
      200)
        counts="$(jq -r "${SPANS_PER_SERVICE}" <<<"${body}" 2>/dev/null | LC_ALL=C tr -cd '[:print:]\n')" || counts=""
        if has_every_service "$@"; then
          complete=yes complete_readings=$((complete_readings + 1))
          if [[ "${counts}" == "${previous}" ]]; then settled=$((settled + 1)); else settled=1; fi
          previous="${counts}"
          if ((settled >= SETTLE_POLLS)); then return 0; fi
        else
          complete=no settled=0 previous="" partial_readings=$((partial_readings + 1))
        fi
        ;;
      404) complete=no settled=0 previous="" partial_readings=$((partial_readings + 1)) ;;
      *) die "Tempo answered HTTP $(clean "${http}") through Grafana: $(clean "${body:0:200}")" ;;
    esac
    sleep "${POLL_INTERVAL}"
  done
  return 1
}

# report_trace LABEL TRACE_ID SERVICE...: wait for the trace, print PASS or
# FAIL; the status is the result. PASS means every service has spans and the
# counts printed are the settled ones; FAIL says whether a service is missing,
# the counts were still changing at the deadline, or the readings alternated
# between complete and partial (some complete, the last one not).
report_trace() {
  local label=$1 id=$2 service count
  shift 2
  if wait_for_trace "${id}" "$@"; then
    printf 'PASS  %s %s has spans from all of: %s\n' "${label}" "${id}" "$*"
    while read -r service count; do
      printf '        %-14s %s span(s)\n' "${service}" "${count}"
    done <<<"${counts}"
    printf 'See it yourself: make grafana (user admin; password: make grafana-password), then Explore:\n'
    printf '  Tempo       TraceQL    { trace:id = "%s" }\n' "${id}"
    return 0
  fi
  if [[ "${complete}" == yes ]]; then
    printf 'FAIL  %s %s was still growing after %ss\n' "${label}" "${id}" "${POLL_TIMEOUT}"
  elif ((complete_readings > 0)); then
    printf 'FAIL  %s %s: readings alternated between complete and partial (%s complete, %s partial or missing) over %ss, Tempo was still settling\n' \
      "${label}" "${id}" "${complete_readings}" "${partial_readings}" "${POLL_TIMEOUT}"
    printf '      Look it up in a moment: make grafana, then Explore, Tempo, TraceQL { trace:id = "%s" }\n' "${id}"
  else
    printf 'FAIL  no %s %s with spans from all of: %s after %ss\n' \
      "${label}" "${id}" "$*" "${POLL_TIMEOUT}"
  fi
  if [[ -n "${counts}" ]]; then
    printf '      Tempo returned:\n'
    while read -r service count; do
      printf '        %-14s %s span(s)\n' "${service}" "${count}"
    done <<<"${counts}"
  fi
  return 1
}

trap cleanup EXIT
wait_for_edge
submit_next_claim
route="$(jq -r '.proposal.route' "${response}")"
state="$(clean "$(jq -r '.state' "${response}")")"
drafted_by="$(jq -r '.proposal.drafted_by | if . == null then "none (the rules decided; no model was called)" else "\(.deployment) (provider \(.provider), mode \(.mode))" end' "${response}")"
printf 'claim       %s\n' "${claim_id}"
printf 'status      %s\n' "${status}"
printf 'state       %s\n' "${state}"
printf 'route       %s\n' "$(clean "${route}")"
printf 'drafted by  %s\n' "$(clean "${drafted_by}")"
printf 'trace       %s\n' "${trace_id}"

if [[ "${state}" == "${AWAITING_ADJUSTER}" ]]; then
  decide_claim
  printf 'decision    %s\n' "${DECISION}"
  printf 'state       %s\n' "$(clean "$(jq -r '.state' "${response}")")"
  printf 'run status  %s\n' "$(clean "$(jq -r '.run_status' "${response}")")"
  printf 'trace       %s (the decision)\n' "${decision_trace_id}"
else
  printf 'no adjuster was needed; the next make demo posts the next claim\n'
fi

if ! password="$(kctl -n observability get secret grafana-admin \
  -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d)" || [[ -z "${password}" ]]; then
  die "could not read the Grafana admin password from Secret grafana-admin"
fi
start_grafana_forward

failed=0
report_trace trace "${trace_id}" "${TRIAGE_SERVICES[@]}" || failed=1
if [[ -n "${decision_trace_id}" ]]; then
  report_trace "decision trace" "${decision_trace_id}" "${DECISION_SERVICES[@]}" || failed=1
fi
# The status of the last command is the script's: 1 when a trace check failed.
[[ "${failed}" == 0 ]]
