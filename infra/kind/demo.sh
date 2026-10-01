#!/usr/bin/env bash
# Show the walking skeleton working: `make demo` (deploys first).
#   1. posts the first synthetic claim that has no triage proposal yet to the
#      Claims API through the edge, with a W3C traceparent the script generated
#   2. reads that trace back from Tempo through Grafana's datasource proxy, the
#      way an owner would see it, and PASSes only when it has spans from all
#      three services: claims-api, agent-runtime and model-gateway
# Each run uses the next claim in data/synthetic/claims.json; a claim that was
# triaged before answers 409 and is skipped. Prints identifiers and the route,
# never a claimant field. Exits non-zero on any failure.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

CLAIMS_FILE="${KIND_DIR}/../../data/synthetic/claims.json"
readonly CLAIMS_FILE
readonly CLAIMS_URL=http://claims.meridian.localhost:8088/claims
readonly HEALTH_URL=http://claims.meridian.localhost:8088/healthz
readonly EDGE_TIMEOUT=60
readonly ALREADY_TRIAGED="the claim already has a triage proposal"
readonly GRAFANA_SERVICE=svc/kube-prometheus-stack-grafana
readonly EXPECTED_SERVICES=(claims-api agent-runtime model-gateway)
readonly POLL_TIMEOUT=120
readonly POLL_INTERVAL=3
readonly POST_TIMEOUT=60

need_tools curl jq openssl base64 kubectl
need_cluster
[[ -f "${CLAIMS_FILE}" ]] || die "no ${CLAIMS_FILE}"

# clean TEXT: TEXT without any byte that is not printable ASCII. Everything this
# script prints that came from an HTTP answer or from Tempo goes through it, so
# a hostile answer cannot inject terminal escape sequences or extra lines.
clean() { printf '%s' "$1" | LC_ALL=C tr -cd '[:print:]'; }

pf_pid="" pf_log="" response=""
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

# post_claim CLAIM_JSON TRACE_ID: print the HTTP status; the body is left in
# ${response}. The parent span ID is random: the Claims API's span becomes a
# child of a span nobody exported, which Tempo shows as the trace's root.
post_claim() {
  local claim=$1 trace_id=$2 parent
  parent="$(openssl rand -hex 8)"
  printf '%s' "${claim}" |
    curl -q --noproxy '*' -sS -m "${POST_TIMEOUT}" -o "${response}" -w '%{http_code}' \
      -H 'Content-Type: application/json' \
      -H "traceparent: 00-${trace_id}-${parent}-01" \
      --data-binary @- "${CLAIMS_URL}"
}

# Post claims in order until one is triaged. Sets ${claim_id}, ${status} and
# ${trace_id}. A claim that already has a proposal is skipped; any other
# answer stops the demo, with the API's fixed error text.
submit_next_claim() {
  local claim detail
  response="$(mktemp)"
  while IFS= read -r claim; do
    claim_id="$(clean "$(jq -r '.claim_id' <<<"${claim}")")"
    trace_id="$(openssl rand -hex 16)"
    if ! status="$(post_claim "${claim}" "${trace_id}")"; then
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

# has_every_service: true when ${counts} lists every expected service.
has_every_service() {
  local service
  for service in "${EXPECTED_SERVICES[@]}"; do
    grep -q "^${service} " <<<"${counts}" || return 1
  done
}

# Wait until Tempo has spans of every expected service for the trace. Leaves the
# per-service counts in ${counts}. Tempo answers 404 until it has the trace, and
# the three services flush their spans separately, so the answer can be partial.
wait_for_trace() {
  local deadline=$((SECONDS + POLL_TIMEOUT)) out http body
  counts=""
  while ((SECONDS < deadline)); do
    kill -0 "${pf_pid}" 2>/dev/null ||
      die "the Grafana port-forward died: $(clean "$(tail -n 3 "${pf_log}")")"
    # The status follows the body on its own line. Only 200 and 404 (Tempo has
    # not got the trace yet) are expected; anything else ends the demo.
    out="$(gcurl -w '\n%{http_code}' "${proxy}/tempo/api/traces/${trace_id}" 2>&1)" ||
      die "could not query Tempo through Grafana: $(clean "${out}")"
    http="${out##*$'\n'}"
    body="${out%$'\n'*}"
    case "${http}" in
      200)
        counts="$(jq -r "${SPANS_PER_SERVICE}" <<<"${body}" 2>/dev/null | LC_ALL=C tr -cd '[:print:]\n')" || counts=""
        if has_every_service; then return 0; fi
        ;;
      404) ;;
      *) die "Tempo answered HTTP $(clean "${http}") through Grafana: $(clean "${body:0:200}")" ;;
    esac
    sleep "${POLL_INTERVAL}"
  done
  return 1
}

# report_trace: wait for the trace, print PASS or FAIL; the status is the result.
report_trace() {
  local service count
  if wait_for_trace; then
    printf 'PASS  trace %s has spans from all of: %s\n' "${trace_id}" "${EXPECTED_SERVICES[*]}"
    while read -r service count; do
      printf '        %-14s %s span(s)\n' "${service}" "${count}"
    done <<<"${counts}"
    printf 'See it yourself: make grafana (user admin; password: make grafana-password), then Explore:\n'
    printf '  Tempo       TraceQL    { trace:id = "%s" }\n' "${trace_id}"
    return 0
  fi
  printf 'FAIL  no trace %s with spans from all of: %s after %ss\n' \
    "${trace_id}" "${EXPECTED_SERVICES[*]}" "${POLL_TIMEOUT}"
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
drafted_by="$(jq -r '.proposal.drafted_by | "\(.deployment) (provider \(.provider), mode \(.mode))"' "${response}")"
printf 'claim       %s\n' "${claim_id}"
printf 'status      %s\n' "${status}"
printf 'route       %s\n' "$(clean "${route}")"
printf 'drafted by  %s\n' "$(clean "${drafted_by}")"
printf 'trace       %s\n' "${trace_id}"

if ! password="$(kctl -n observability get secret grafana-admin \
  -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d)" || [[ -z "${password}" ]]; then
  die "could not read the Grafana admin password from Secret grafana-admin"
fi
start_grafana_forward

report_trace
