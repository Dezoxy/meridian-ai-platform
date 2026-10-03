#!/usr/bin/env bash
# Prove the local platform works end to end: `make smoke`. Changes nothing apart
# from three short-lived Jobs (unique names, removed by ttlSecondsAfterFinished)
# and, at most once per throttle window per tool server, the refusal's audit row
# that the tool check below causes.
#   1. edge:      laptop -> 127.0.0.1:8088 -> kind port mapping -> NodePort -> Envoy
#   2. database:  pgvector is installed in platform-db, in the `app` database and
#                 in the `meridian` database
#   3. tools:     one call per MCP tool server through the runtime's own client,
#                 run in the agent-runtime pod (so with the addresses the runtime
#                 was given), with a run ID that does not exist: each server must
#                 refuse it as `unknown-run`. Skipped, not failed, while the
#                 Meridian services are not deployed (`make deploy`).
#   4. telemetry: telemetrygen sends one trace, one log and one metric over OTLP
#                 to the collector; each is then read back through Grafana's
#                 datasource proxy (Tempo, Loki, Prometheus), the way an owner
#                 would see it.
#   5. cost panel: Grafana serves the provisioned dashboard "Meridian: Model
#                 Gateway tokens and cost"; and, once the gateway has settled a
#                 call since it started (the ledger says so), Prometheus holds
#                 its tokens, cost and calls series. The series line is skipped
#                 while the services are not deployed (`make deploy`) or the
#                 gateway has settled nothing yet (`make demo`).
# Prints one PASS, FAIL or SKIP line per check and exits non-zero on any FAIL.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly EDGE_URL=http://127.0.0.1:8088/
readonly COLLECTOR_ENDPOINT=otel-collector.observability.svc.cluster.local:4317
readonly GRAFANA_SERVICE=svc/kube-prometheus-stack-grafana
readonly POLL_TIMEOUT=120
readonly POLL_INTERVAL=3
readonly JOB_TIMEOUT=120s
# The dashboard's uid (infra/kind/dashboards/gateway-cost.json) and the series
# the gateway exports for it (their names are in src/meridian/platform/gateway).
readonly DASHBOARD_UID=meridian-gateway-cost
readonly COST_SERIES=(meridian_gateway_tokens_total meridian_gateway_cost_EUR_total meridian_gateway_calls_total)

failures=0
skips=0
grafana_url="" # set by open_grafana
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; failures=$((failures + 1)); }
skip() { printf 'SKIP  %s\n' "$*"; skips=$((skips + 1)); }

# clean_lines TEXT: TEXT on one line, its lines joined with ";", without any byte
# that is not printable ASCII (see `clean` in demo.sh). Whatever this script
# prints that came out of a pod goes through it, so a hostile answer cannot
# inject terminal escape sequences or extra lines.
clean_lines() { printf '%s' "$1" | LC_ALL=C tr -cd '[:print:]\n' | paste -sd ';' -; }

need_tools docker kubectl curl jq base64
require_local_docker
need_cluster

# ── 1. edge ──────────────────────────────────────────────────────────────────
# Envoy sends no identifying header on a 404 with no route, so the request
# counter of the Gateway's listener is the proof that Envoy answered. Listener
# "http-10080" is the Gateway's port 80 (Envoy Gateway adds 10000 to a
# privileged port). The counter is read through the API server's pod proxy.
edge_request_count() {
  local proxy
  proxy="$(kctl -n envoy-gateway-system get pod \
    -l app.kubernetes.io/component=proxy,gateway.envoyproxy.io/owning-gateway-name=edge \
    -o jsonpath='{.items[0].metadata.name}')"
  kctl get --raw "/api/v1/namespaces/envoy-gateway-system/pods/${proxy}:19001/proxy/stats/prometheus" |
    awk '/^envoy_http_downstream_rq_total\{envoy_http_conn_manager_prefix="http-10080"\}/ { print $2 }'
}

check_edge() {
  local before after status
  before="$(edge_request_count 2>/dev/null || true)"
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -o /dev/null -w '%{http_code}' "${EDGE_URL}" 2>&1)"; then
    fail "edge: ${EDGE_URL} did not answer: ${status}"
    return
  fi
  after="$(edge_request_count 2>/dev/null || true)"
  if [[ "${status}" != 404 ]]; then
    fail "edge: expected 404 (no route for this host), got ${status}"
  elif [[ -z "${before}" || -z "${after}" ]]; then
    fail "edge: could not read Envoy's request counter"
  elif ((after > before)); then
    pass "edge: ${EDGE_URL} -> 404, counted by Envoy (${before} -> ${after} requests); no route for this host, as expected"
  else
    fail "edge: got 404 but Envoy did not count the request (${before} -> ${after})"
  fi
}

# ── 2. database ──────────────────────────────────────────────────────────────
# One line per database: `app` (the platform's own) and `meridian` (the services').
check_database() {
  local primary database version
  primary="$(kctl -n meridian get pod \
    -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -z "${primary}" ]]; then
    fail "database: no primary pod found for platform-db"
    return
  fi
  for database in app meridian; do
    version="$(kctl -n meridian exec "${primary}" -c postgres -- \
      psql -d "${database}" -tAc "SELECT extversion FROM pg_extension WHERE extname='vector'" \
      2>/dev/null || true)"
    version="$(clean_lines "${version}")"
    if [[ -n "${version}" ]]; then
      pass "database: pgvector ${version} installed in ${primary}, database ${database}"
    else
      fail "database: extension vector is not installed in ${primary}, database ${database}"
    fi
  done
}

# ── 3. tools ─────────────────────────────────────────────────────────────────
# deployed_services: the Meridian Deployments, one name per line (empty when
# there are none). Stdout only: a warning on stderr is not a Deployment; it goes
# to the terminal. Shared with the cost check (5).
deployed_services() {
  kctl -n meridian get deployment \
    -l app.kubernetes.io/part-of=meridian -o name --ignore-not-found
}

# The probe runs in the runtime's own pod, so it uses the addresses the runtime
# was given. Its stdout is one "<server> <tool> <answer>" line per tool server;
# it exits 0 only when every answer is unknown-run (the refusal of a run that
# does not exist, which the servers check before anything else of the caller's).
check_tools() {
  local found out err_file servers
  # Skipped only when no Meridian Deployment exists. When any does, the probe is
  # required: a missing or renamed agent-runtime fails its exec below.
  if ! found="$(deployed_services)"; then
    fail "tools: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "tools: the Meridian services are not deployed (make deploy)"
    return
  fi
  # The names come from stdout alone: a warning on stderr is not a server. A
  # failure shows both.
  err_file="$(mktemp)"
  if out="$(kctl -n meridian exec deploy/agent-runtime -- \
    python -m meridian.runtime.toolprobe 2>"${err_file}")"; then
    servers="$(clean_lines "$(awk '{ print $1 }' <<<"${out}")")"
    pass "tools: each server (${servers//;/, }) answered unknown-run through the runtime's client"
  else
    fail "tools: the probe in deployment/agent-runtime failed: stdout: $(clean_lines "${out}"); stderr: $(clean_lines "$(<"${err_file}")")"
  fi
  rm -f "${err_file}"
}

# ── 4. telemetry ─────────────────────────────────────────────────────────────
# start_job SIGNAL COUNT_FLAG: one Job that sends one item of SIGNAL (traces,
# logs or metrics) for service ${service}. Named uniquely, so reruns never clash.
start_job() {
  local signal=$1 count_flag=$2
  kctl create -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: smoke-${signal}-${epoch}
  namespace: observability
  labels:
    app.kubernetes.io/name: meridian-smoke
spec:
  ttlSecondsAfterFinished: 900
  backoffLimit: 2
  template:
    metadata:
      labels:
        app.kubernetes.io/name: meridian-smoke
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: telemetrygen
          image: ${TELEMETRYGEN_IMAGE}
          args:
            - ${signal}
            - --otlp-endpoint
            - ${COLLECTOR_ENDPOINT}
            - --otlp-insecure
            - --service
            - ${service}
            - ${count_flag}
            - "1"
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              memory: 64Mi
EOF
}

# gcurl ARGS...: curl against Grafana with basic auth. The password reaches curl
# on stdin as a config line, so it never appears in a process listing.
gcurl() {
  printf 'user = "admin:%s"\n' "${password}" | curl -q --noproxy '*' -sS -m 15 -K - "$@"
}

# poll FILTER CURL_ARGS...: run gcurl until jq FILTER prints a non-empty value
# or POLL_TIMEOUT passes. The value is left in ${poll_result}.
poll() {
  local filter=$1
  shift
  local deadline=$((SECONDS + POLL_TIMEOUT)) body
  while ((SECONDS < deadline)); do
    if body="$(gcurl "$@" 2>/dev/null)" &&
      poll_result="$(jq -r "${filter}" <<<"${body}" 2>/dev/null)" &&
      [[ -n "${poll_result}" ]]; then
      return 0
    fi
    sleep "${POLL_INTERVAL}"
  done
  poll_result=""
  return 1
}

cleanup() {
  if [[ -n "${pf_pid:-}" ]]; then
    kill "${pf_pid}" 2>/dev/null || true
    wait "${pf_pid}" 2>/dev/null || true # kubectl is gone when smoke.sh returns
  fi
  [[ -n "${pf_log:-}" ]] && rm -f "${pf_log}"
}

# open_grafana: read the admin password into ${password} (gcurl uses it) and
# forward a local port to Grafana, once per run; the address is left in
# ${grafana_url}. Returns 0 at once when it is open already, and 1 after a FAIL
# line when it cannot open. The password is never an argument and never printed.
open_grafana() {
  [[ -z "${grafana_url}" ]] || return 0
  if ! password="$(kctl -n observability get secret grafana-admin \
    -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d)" || [[ -z "${password}" ]]; then
    fail "grafana: could not read the Grafana admin password from Secret grafana-admin"
    return 1
  fi
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
  if [[ -z "${port}" ]]; then
    fail "grafana: could not port-forward to Grafana"
    cleanup || true # a later check may try again with a fresh forward
    pf_pid=""
    pf_log=""
    return 1
  fi
  grafana_url="http://127.0.0.1:${port}"
}

check_telemetry() {
  epoch="$(date +%s)"
  service="meridian-smoke-${epoch}"
  log "telemetry: sending one trace, log and metric as service ${service}"
  start_job traces --traces
  start_job logs --logs
  start_job metrics --metrics

  local signal
  for signal in traces logs metrics; do
    if ! kctl -n observability wait --for=condition=complete \
      "job/smoke-${signal}-${epoch}" --timeout="${JOB_TIMEOUT}" >/dev/null 2>&1; then
      fail "telemetry: telemetrygen ${signal} job did not complete (kubectl -n observability logs job/smoke-${signal}-${epoch})"
      return
    fi
  done
  pass "telemetry: telemetrygen sent trace, log and metric to ${COLLECTOR_ENDPOINT}"

  open_grafana || return 0 # it printed the FAIL line
  local proxy="${grafana_url}/api/datasources/proxy/uid"

  # Traces: THE done-when of S006.
  if poll '.traces[0].traceID // empty' \
    "${proxy}/tempo/api/search?tags=service.name%3D${service}&limit=5"; then
    pass "trace: Tempo has trace ${poll_result} for ${service}"
  else
    fail "trace: no trace for ${service} in Tempo after ${POLL_TIMEOUT}s"
  fi

  if poll '.data.result[0].values[0][1] // empty' -G \
    "${proxy}/loki/loki/api/v1/query_range" \
    --data-urlencode "query={service_name=\"${service}\"}" --data-urlencode "limit=5"; then
    pass "log: Loki has a line for ${service}: ${poll_result}"
  else
    fail "log: no line for ${service} in Loki after ${POLL_TIMEOUT}s"
  fi

  if poll '.data.result | select(length > 0) | length | tostring' -G \
    "${proxy}/prometheus/api/v1/query" \
    --data-urlencode "query={__name__=~\"gen.*\",job=\"${service}\"}"; then
    pass "metric: Prometheus has ${poll_result} series from telemetrygen for ${service}"
  else
    fail "metric: no series for ${service} in Prometheus after ${POLL_TIMEOUT}s"
  fi
}

# ── 5. cost panel ────────────────────────────────────────────────────────────
# The series line. A gateway process exports once a minute, so the ledger says
# whether a series is due: only attempts settled since the process started can
# be in Prometheus. The start time is checked before it goes into SQL.
check_cost_series() {
  local found started primary answer settled first_settled name got missing series_list query
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
  # The newest container start among the gateway's pods (RFC 3339, so it sorts).
  if ! started="$(kctl -n meridian get pod -l app.kubernetes.io/name=model-gateway \
    -o jsonpath='{range .items[*]}{.status.containerStatuses[0].state.running.startedAt}{"\n"}{end}' |
    sort | tail -n 1)"; then
    fail "cost series: could not read the gateway's pods (kubectl's error is above)"
    return
  fi
  if ! [[ "${started}" =~ ${start_pattern} ]]; then
    fail "cost series: could not read when the gateway process started (the newest model-gateway pod has no running start time in the form 2026-01-31T08:00:00Z)"
    return
  fi
  primary="$(kctl -n meridian get pod \
    -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -z "${primary}" ]]; then
    fail "cost series: no primary pod found for platform-db"
    return
  fi
  if ! answer="$(kctl -n meridian exec "${primary}" -c postgres -- \
    psql -d meridian -tAc "SELECT count(*) || '|' || coalesce(floor(extract(epoch FROM min(closed_at)))::bigint::text, '') FROM gateway.usage WHERE state = 'settled' AND closed_at > '${started}'" \
    2>/dev/null)"; then
    fail "cost series: could not count the settled attempts in gateway.usage of ${primary}"
    return
  fi
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
  # Which of them did not come: one more look, for the message only.
  got="$(clean_lines "$(gcurl -G "${query_url}" --data-urlencode "query=${query}" 2>/dev/null |
    jq -r '[.data.result[].metric.__name__] | join(" ")' 2>/dev/null || true)")"
  missing=""
  for name in "${COST_SERIES[@]}"; do
    [[ " ${got} " == *" ${name} "* ]] || missing="${missing:+${missing}, }${name}"
  done
  if [[ "${missing}" == "${series_list}" ]]; then
    fail "cost series: none of the three series came from the gateway after ${POLL_TIMEOUT}s, though the ledger has ${settled} settled attempt(s) since ${started} (it exports once a minute; is the collector up?)"
  else
    fail "cost series: missing ${missing} in Prometheus after ${POLL_TIMEOUT}s"
  fi
}

check_cost_panel() {
  open_grafana || return 0 # it printed the FAIL line
  if poll '(select(.meta.provisioned == true) | .dashboard.title) // empty' \
    "${grafana_url}/api/dashboards/uid/${DASHBOARD_UID}"; then
    pass "dashboard: Grafana serves \"$(clean_lines "${poll_result}")\" (uid ${DASHBOARD_UID}), provisioned from infra/kind/dashboards"
  else
    fail "dashboard: Grafana has no provisioned dashboard ${DASHBOARD_UID} after ${POLL_TIMEOUT}s (run make up)"
  fi
  check_cost_series
}

trap cleanup EXIT
check_edge
check_database
check_tools
check_telemetry
check_cost_panel

if ((failures > 0)); then
  printf '\n%s check(s) FAILED\n' "${failures}"
  exit 1
fi
if ((skips > 0)); then
  printf '\nAll checks that ran passed; %s skipped.\n' "${skips}"
else
  printf '\nAll checks passed.\n'
fi
printf 'See it yourself: make grafana (user admin; password: make grafana-password), then Explore:\n'
printf '  Tempo       TraceQL    { resource.service.name = "%s" }\n' "${service}"
printf '  Loki        LogQL      {service_name="%s"}\n' "${service}"
printf '  Prometheus  PromQL     {__name__=~"gen.*", job="%s"}\n' "${service}"
printf '  Grafana     Dashboards > Meridian: Model Gateway tokens and cost\n'
