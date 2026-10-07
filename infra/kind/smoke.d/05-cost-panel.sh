# shellcheck shell=bash
#   5. cost panel: four lines. Grafana serves the provisioned dashboard
#                 "Meridian: Model Gateway tokens and cost", its queries equal
#                 the file's and every one of them runs in Prometheus (a
#                 dashboard with no query, or a target with no expression, is a
#                 FAIL, and targets of panels nested in rows count); once the
#                 gateway has settled a call since it started (the ledger says
#                 so), Prometheus holds its tokens, cost and calls series (the
#                 series line is skipped while the services are not deployed,
#                 `make deploy`, or the gateway has settled nothing yet, `make
#                 demo`, and fails when the gateway is not available). Since
#                 S066 that series line also means the rate store answered: the
#                 gateway refuses every call it cannot count (a 503, with the
#                 store unreachable), so a call settled since the gateway
#                 started, which is what the series needs, went through the
#                 store's windows. No line reads the store itself: no check
#                 may hold its credential (the Secret `rate-store-
#                 credentials` is the gateway's and the store's alone), and
#                 check 8's last line shows only that nothing else reaches it;
#                 and
#                 Grafana's service account may not read Secrets in meridian or
#                 observability; and (S063) kube-state-metrics' service account
#                 may not get, list or watch Secrets in meridian or cert-manager
#                 (`kubectl auth can-i --as`, six read-only questions; the line
#                 needs no deployed service and no Grafana forward, so it prints
#                 after `make up` alone). The last line does not prove that the
#                 Prometheus operator's rights are narrow: they are not, the
#                 chart gives it a ClusterRole that reads, creates and changes
#                 Secrets in every namespace and has no value to narrow it
#                 (threat model T-68, still open for the operator); nor that
#                 kube-state-metrics' other rights are small (it still lists
#                 and watches ConfigMaps, Pods and the rest cluster-wide).

# The dashboard's uid (infra/kind/dashboards/gateway-cost.json) and the series
# the gateway exports for it (their names are in src/meridian/platform/gateway).
readonly DASHBOARD_UID=meridian-gateway-cost
readonly DASHBOARD_FILE="${KIND_DIR}/dashboards/gateway-cost.json"
readonly COST_SERIES=(meridian_gateway_tokens_total meridian_gateway_cost_EUR_total meridian_gateway_calls_total)
# The service account the chart makes for Grafana (release name + "-grafana").
readonly GRAFANA_ACCOUNT=system:serviceaccount:observability:kube-prometheus-stack-grafana
# ... and for kube-state-metrics (release name + "-kube-state-metrics").
readonly KSM_ACCOUNT=system:serviceaccount:observability:kube-prometheus-stack-kube-state-metrics

# ── 5. cost panel ────────────────────────────────────────────────────────────
# run_dashboard_query TITLE EXPR: one instant query through Grafana's datasource
# proxy; Prometheus must answer status success. Counts it in ${queries_run};
# on a refusal returns 1 with "<title>: <error>" in ${query_error}.
run_dashboard_query() {
  local title=$1 expr=$2 body status error
  # shellcheck disable=SC2154  # grafana_url is set by open_grafana (shared.sh)
  body="$(gcurl -G "${grafana_url}/api/datasources/proxy/uid/prometheus/api/v1/query" \
    --data-urlencode "query=${expr}" 2>&1)" || true
  queries_run=$((queries_run + 1))
  status="$(jq -r '.status // empty' <<<"${body}" 2>/dev/null || true)"
  [[ "${status}" != success ]] || return 0
  error="$(jq -r '.error // empty' <<<"${body}" 2>/dev/null || true)"
  [[ -n "${error}" ]] || error="${body}"
  query_error="${title}: $(clean_lines "${error:0:200}")"
  return 1
}

# dashboard_targets: the targets of the dashboard JSON on stdin, those of panels
# nested in rows included, as a JSON list of {title, expr} in document order
# (the title is the panel's).
dashboard_targets() {
  jq -c '[.panels[]? | .. | objects | select(has("targets"))
    | .title as $title | .targets[]? | {title: ($title // "(untitled)"), expr: .expr}]'
}

# dashboard_target_problem SERVED: why the served dashboard has nothing to run,
# on stdout, with status 1; nothing and 0 when every target has an expression and
# there is at least one. A dashboard with no target would otherwise "run all 0
# queries" and pass.
dashboard_target_problem() {
  local targets empty
  targets="$(dashboard_targets <<<"$1")"
  if [[ "$(jq 'length' <<<"${targets}")" == 0 ]]; then
    printf 'it has no query to run (no panel, nested in a row or not, has a target), so "all queries ran" would prove nothing'
    return 1
  fi
  empty="$(jq -r 'map(select((.expr // "") == "") | .title) | unique | join(", ")' <<<"${targets}")"
  if [[ -n "${empty}" ]]; then
    printf 'a target of panel(s) %s has no expression' "$(clean_lines "${empty}")"
    return 1
  fi
}

# run_dashboard_queries SERVED: every target of the served dashboard, with the
# range variable set to an hour (3600) and, for a target that names $dimension,
# once per option of that variable. Leaves the count in ${queries_run}; the first
# refusal returns 1 (see run_dashboard_query).
run_dashboard_queries() {
  local served=$1 targets total i title expr options option
  # shellcheck disable=SC2016 # both are Grafana's variable names, written literally
  local range_ref='${__range_s}' dimension_ref='$dimension'
  queries_run=0
  query_error=""
  options="$(jq -r '(.templating.list // [])[] | select(.name == "dimension") | .options[].value' <<<"${served}")"
  targets="$(dashboard_targets <<<"${served}")"
  total="$(jq 'length' <<<"${targets}")"
  for ((i = 0; i < total; i++)); do
    title="$(clean_lines "$(jq -r --argjson i "${i}" '.[$i].title' <<<"${targets}")")"
    expr="$(jq -r --argjson i "${i}" '.[$i].expr' <<<"${targets}")"
    expr="${expr//"${range_ref}"/3600}"
    if [[ "${expr}" != *"${dimension_ref}"* ]]; then
      run_dashboard_query "${title}" "${expr}" || return 1
      continue
    fi
    if [[ -z "${options}" ]]; then
      query_error="${title}: the dashboard has no dimension options"
      return 1
    fi
    while IFS= read -r option; do
      run_dashboard_query "${title}" "${expr//"${dimension_ref}"/${option}}" || return 1
    done <<<"${options}"
  done
}

# check_dashboard UID FILE: the dashboard line, for the cost dashboard (check 5)
# and the health dashboard (check 11). What Grafana serves under UID must be the
# file's: a stale provisioned copy fails. Its queries are then run, so a renamed
# series or a typo shows here. A dashboard with no target, or a target with no
# expression, is a FAIL: "all 0 queries ran" proves nothing. Targets of panels
# nested in rows count (dashboard_targets).
check_dashboard() {
  local uid=$1 file=$2 served title file_exprs served_exprs problem
  if ! poll '(select(.meta.provisioned == true) | .dashboard) // empty | tojson' \
    "${grafana_url}/api/dashboards/uid/${uid}"; then
    # shellcheck disable=SC2154  # poll_error is set by poll (shared.sh)
    fail "dashboard: Grafana has no provisioned dashboard ${uid} after ${POLL_TIMEOUT}s (run make up) (last answer: ${poll_error})"
    return
  fi
  # shellcheck disable=SC2154  # poll_result is set by poll (shared.sh)
  served="${poll_result}"
  title="$(clean_lines "$(jq -r '.title // empty' <<<"${served}" 2>/dev/null)")"
  if ! file_exprs="$(dashboard_targets <"${file}" | jq -c 'map(.expr)')"; then
    fail "dashboard: could not read the queries of ${file}"
    return
  fi
  served_exprs="$(dashboard_targets <<<"${served}" 2>/dev/null | jq -c 'map(.expr)' 2>/dev/null || true)"
  if [[ "${served_exprs}" != "${file_exprs}" ]]; then
    fail "dashboard: Grafana serves \"${title}\" but its queries differ from infra/kind/dashboards/${file##*/} (run make up)"
    return
  fi
  if ! problem="$(dashboard_target_problem "${served}")"; then
    fail "dashboard: Grafana serves \"${title}\" (uid ${uid}) but ${problem} (infra/kind/dashboards/${file##*/})"
    return
  fi
  if run_dashboard_queries "${served}"; then
    pass "dashboard: Grafana serves \"${title}\" (uid ${uid}), provisioned, with the file's queries; all ${queries_run} queries ran in Prometheus (range 3600s)"
  else
    fail "dashboard: a query of \"${title}\" failed in Prometheus: ${query_error}"
  fi
}

# The series line. A gateway process exports once a minute, so the ledger says
# whether a series is due: only attempts settled since the process started can
# be in Prometheus. The start time is checked before it goes into SQL.
check_cost_series() {
  local found available started primary err_file detail answer settled first_settled
  local name got final prom_status missing series_list query last
  series_list="$(printf '%s, ' "${COST_SERIES[@]}")"
  series_list="${series_list%, }"
  local start_pattern='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
  local ledger_pattern='^([0-9]+)\|([0-9]*)$' # "<count>|<epoch second>"
  local query_url="${grafana_url}/api/datasources/proxy/uid/prometheus/api/v1/query"
  local selector='{__name__=~"meridian_gateway_(tokens|cost_EUR|calls)_total", job="model-gateway"}'
  if ! found="$(deployed_services)"; then
    fail "cost series: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "cost series: the Meridian services are not deployed (make deploy)"
    return
  fi
  # A gateway that is down has settled nothing since its start, and that must not
  # read as one waiting for a claim: it fails here instead of skipping below.
  available="$(kctl -n meridian get deployment model-gateway \
    -o jsonpath='{.status.availableReplicas}' || true)"
  [[ "${available}" =~ ^[0-9]+$ ]] || available=0
  if ((available < 1)); then
    fail "cost series: the gateway is not available (Deployment model-gateway reports ${available} available replicas)"
    return
  fi
  # The newest container start among the gateway's pods (RFC 3339, so it sorts).
  if ! started="$(kctl -n meridian get pod -l app.kubernetes.io/name=model-gateway \
    -o jsonpath='{range .items[*]}{.status.containerStatuses[?(@.name=="model-gateway")].state.running.startedAt}{"\n"}{end}' |
    sort | tail -n 1)"; then
    fail "cost series: could not read the gateway's pods (kubectl's error is above)"
    return
  fi
  if ! [[ "${started}" =~ ${start_pattern} ]]; then
    fail "cost series: could not read when the gateway process started (the newest model-gateway pod has no running start time in the form 2026-01-31T08:00:00Z)"
    return
  fi
  # kubectl's stderr goes to a file (as in check_tools) and into the FAIL line.
  err_file="$(mktemp)"
  if ! primary="$(kctl -n meridian get pod \
    -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary \
    -o jsonpath='{.items[0].metadata.name}' 2>"${err_file}")" || [[ -z "${primary}" ]]; then
    detail="$(clean_lines "$(<"${err_file}")")"
    rm -f "${err_file}"
    fail "cost series: no primary pod found for platform-db${detail:+ (kubectl said: ${detail})}"
    return
  fi
  if ! answer="$(kctl -n meridian exec "${primary}" -c postgres -- \
    env "PGOPTIONS=${PSQL_OPTIONS}" psql -d meridian -tAc "SELECT count(*) || '|' || coalesce(floor(extract(epoch FROM min(closed_at)))::bigint::text, '') FROM gateway.usage WHERE state = 'settled' AND closed_at > '${started}'" \
    2>"${err_file}")"; then
    detail="$(clean_lines "$(<"${err_file}")")"
    rm -f "${err_file}"
    fail "cost series: could not count the settled attempts in gateway.usage of ${primary}${detail:+ (kubectl said: ${detail})}"
    return
  fi
  rm -f "${err_file}"
  answer="$(clean_lines "${answer}")"
  if ! [[ "${answer}" =~ ${ledger_pattern} ]]; then
    fail "cost series: the ledger's answer was not a count and an epoch second"
    return
  fi
  settled="${BASH_REMATCH[1]}"
  first_settled="${BASH_REMATCH[2]}"
  if ((10#${settled} == 0)); then
    skip "cost series: the gateway has settled no call since it started at ${started} (make demo sends a claim)"
    return
  fi
  if [[ -z "${first_settled}" ]]; then
    fail "cost series: the ledger has ${settled} settled attempt(s) since ${started} but no time for the first"
    return
  fi
  # The previous process's series stay visible for five minutes after a restart: keep
  # samples exported since the first settled attempt (`and`: timestamp() drops __name__).
  query="count by (__name__) (${selector} and (timestamp(${selector}) >= ${first_settled}))"
  if poll "[.data.result[].metric.__name__] as \$got
    | if ($(printf '%s\n' "${COST_SERIES[@]}" | jq -R . | jq -sc .) - \$got | length) == 0
      then \$got | sort | join(\", \") else empty end" \
    -G "${query_url}" --data-urlencode "query=${query}"; then
    pass "cost series: ${settled} settled attempt(s) in the ledger since the gateway started at ${started}; Prometheus has ${series_list}"
    return
  fi
  # Which of them did not come, and did Prometheus answer at all: one more look,
  # for the message only. ${poll_error} is what the last attempt saw.
  last="(last answer: ${poll_error})"
  final="$(gcurl -G "${query_url}" --data-urlencode "query=${query}" 2>/dev/null || true)"
  prom_status="$(jq -r '.status // empty' <<<"${final}" 2>/dev/null || true)"
  if [[ "${prom_status}" != success ]]; then
    fail "cost series: Prometheus did not answer the query with status success after ${POLL_TIMEOUT}s ${last}"
    return
  fi
  got="$(clean_lines "$(jq -r '[.data.result[].metric.__name__] | join(" ")' <<<"${final}" 2>/dev/null || true)")"
  missing=""
  for name in "${COST_SERIES[@]}"; do
    [[ " ${got} " == *" ${name} "* ]] || missing="${missing:+${missing}, }${name}"
  done
  if [[ "${missing}" == "${series_list}" ]]; then
    fail "cost series: none of the three series came from the gateway after ${POLL_TIMEOUT}s, though the ledger has ${settled} settled attempt(s) since ${started} (it exports once a minute; is the collector up?) ${last}"
  else
    fail "cost series: missing ${missing} in Prometheus after ${POLL_TIMEOUT}s ${last}"
  fi
}

# Grafana's service account must not read Secrets, in the namespace of the
# database roles' passwords or in its own (the Helm release Secrets, T-68).
# `can-i` prints yes or no on stdout and exits 1 for no; stderr is not an answer.
check_grafana_rights() {
  local namespace answer
  for namespace in meridian observability; do
    answer="$(kctl auth can-i get secrets -n "${namespace}" --as "${GRAFANA_ACCOUNT}" 2>/dev/null || true)"
    if [[ "${answer}" != no ]]; then
      fail "grafana rights: expected \"no\" to reading Secrets in ${namespace} as ${GRAFANA_ACCOUNT}, got \"$(clean_lines "${answer}")\""
      return
    fi
  done
  pass "grafana rights: Grafana's service account may not read Secrets in meridian or observability (T-68)"
}

# kube-state-metrics' service account must not get, list or watch Secrets in the
# namespace of the database roles' passwords or in the one of the CA's key: the
# chart's `secrets` collector is off (values/kube-prometheus-stack.yaml), and the
# ClusterRole it derives from the collectors has no rule for them. Same shape as
# check_grafana_rights: stdout is the answer, stderr is not. A role that came
# back with a rule for Secrets fails here, naming the verb and the namespace.
check_kube_state_metrics_rights() {
  local namespace verb answer
  for namespace in meridian cert-manager; do
    for verb in get list watch; do
      answer="$(kctl auth can-i "${verb}" secrets -n "${namespace}" --as "${KSM_ACCOUNT}" 2>/dev/null || true)"
      if [[ "${answer}" != no ]]; then
        fail "kube-state-metrics rights: expected \"no\" to ${verb} of Secrets in ${namespace} as ${KSM_ACCOUNT}, got \"$(clean_lines "${answer}")\""
        return
      fi
    done
  done
  pass "kube-state-metrics rights: its service account may not get, list or watch Secrets in meridian or cert-manager (T-68)"
}

check_cost_panel() {
  if open_grafana; then # otherwise it printed the one FAIL line
    check_dashboard "${DASHBOARD_UID}" "${DASHBOARD_FILE}"
    check_cost_series
  fi
  check_grafana_rights
  check_kube_state_metrics_rights
}
