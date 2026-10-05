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
#   5. cost panel: three lines. Grafana serves the provisioned dashboard
#                 "Meridian: Model Gateway tokens and cost", its queries equal
#                 the file's and every one of them runs in Prometheus; once the
#                 gateway has settled a call since it started (the ledger says
#                 so), Prometheus holds its tokens, cost and calls series (the
#                 series line is skipped while the services are not deployed,
#                 `make deploy`, or the gateway has settled nothing yet, `make
#                 demo`, and fails when the gateway is not available); and
#                 Grafana's service account may not read Secrets in meridian or
#                 observability.
#   6. adjuster pages: three lines, through the edge as demo.sh reaches the Claims
#                 API. The queue page answers 200 with the Content-Security-
#                 Policy (frame-ancestors 'none', default-src 'none') and the
#                 synthetic-data line; a decision posted with a foreign Origin is
#                 refused with 403 before any claim is looked up; the claimant's
#                 start page answers 200 with the same policy and the banner's
#                 fictional-data sentence (T-04), and changes no claim. Skipped
#                 while the Meridian services are not deployed (`make deploy`).
#   7. sweep:     one line, read-only. The CronJob meridian-sweep exists, is not
#                 suspended, and the last of its Jobs to finish (the scheduled
#                 ones and any made by hand) succeeded; the line prints when it
#                 finished. It fails when the last one failed (with its reason),
#                 when the CronJob was last scheduled more than three periods
#                 (15 minutes) after that Job finished with nothing running, and
#                 when it was never scheduled although the API holds a timestamp
#                 more than three periods after its creation. Skipped while the
#                 Meridian services are not deployed (`make deploy`), while no
#                 Job of it has finished yet, and while a CronJob that never ran
#                 is too young to judge. Only the API server's timestamps are
#                 compared; a PASS says when it finished, and a schedule that
#                 stopped since cannot be seen without a clock the script trusts.
#   8. network policy: one line, read-only. From inside the Claims API's pod a
#                 TCP connection to the Model Gateway, which no rule allows,
#                 must time out: the namespace's default-deny is enforced. It
#                 fails when the policy `default-deny` does not exist, and when
#                 the connection succeeds (the cluster does not enforce
#                 NetworkPolicy, or a rule is too wide). Skipped while the
#                 Claims API is not deployed (`make deploy`).
#   9. service identity: five lines, from the Agent Runtime's pod to the Model
#                 Gateway (S055, S056; the Claims API's pod cannot reach the
#                 gateway, which check 8 proves, so the runtime's does). The
#                 image has no curl, so Python opens the connection and checks
#                 the gateway's certificate against the CA. `GET /healthz` with
#                 no client certificate answers 200 (the kubelet's probe
#                 presents none); a `POST /v1/chat` with none answers 401 (no
#                 identity); the same with the runtime's own certificate,
#                 naming a tenant the registry's services.yaml does not let it
#                 name (a real tenant, so the gateway's own tenant check would
#                 let it through), answers 403. The fourth line reads the
#                 reason of that 403 from the audit table, in the database's
#                 primary pod: a row of the gateway's refusal for the identity
#                 rule's reason (`caller-name-not-allowed`), naming the
#                 runtime as the calling service, recorded in the last 120
#                 seconds by the database's clock. The gateway writes it in a
#                 worker thread, at most one per reason and tenant and minute,
#                 so the check asks for a row that exists and is recent, never
#                 for a count that went up, and tries for about ten seconds;
#                 without it a 403 from the gateway's own policy would pass for
#                 the wrong reason. The fifth presents a certificate of another
#                 CA: the probe makes a throwaway key and a self-signed
#                 certificate with the runtime's own URI (the right name, the
#                 wrong CA), and the gateway must end the connection (a TLS
#                 alert, or a close after its own certificate verified) before
#                 any answer: a status is a FAIL. The tools check above is a
#                 further proof: its calls run over TLS with the runtime's
#                 certificate. The two refusals (the 401 and the 403) leave two
#                 refusal rows in the audit table on each run, one per reason
#                 (the gateway throttles its refusal rows to one per reason and
#                 minute). Skipped while the Meridian services are not deployed
#                 (`make deploy`). A traceback is a failure, not a refusal.
# Prints one PASS, FAIL or SKIP line per check and exits non-zero on any FAIL.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly EDGE_URL=http://127.0.0.1:8088/
readonly ADJUSTER_QUEUE_URL=http://claims.meridian.localhost:8088/adjuster/claims
readonly ADJUSTER_DECISION_URL=http://claims.meridian.localhost:8088/adjuster/claims/CLM-9999/decision
# The first sentence of the banner every page carries (templates/base.html).
readonly ADJUSTER_BANNER="Synthetic data only."
readonly SWEEP_CRONJOB=meridian-sweep
# The CronJob's schedule is every five minutes; a run is overdue after three.
readonly SWEEP_PERIOD_SECONDS=300
readonly SWEEP_STALE_PERIODS=3
# The connection the network-policy check tries from the Claims API's pod: the
# Model Gateway's Service, which only the Agent Runtime, the knowledge tool server
# and the ingestion Job may reach. The image has no curl, so Python opens it. The
# snippet prints "reached" or "blocked" and exits 0 either way; anything else (a
# name that does not resolve, a refused connection) is a traceback and a non-zero
# exit, which the check reports as a failure, not as "blocked".
readonly NETWORK_PROBE_HOST=model-gateway.meridian.svc
readonly NETWORK_PROBE_PORT=8000
readonly NETWORK_PROBE_TIMEOUT=4
readonly NETWORK_PROBE='import socket
try:
    socket.create_connection(("'"${NETWORK_PROBE_HOST}"'", '"${NETWORK_PROBE_PORT}"'), timeout='"${NETWORK_PROBE_TIMEOUT}"').close()
    print("reached")
except TimeoutError:
    print("blocked")'
# The service identity check (9): the Model Gateway's Service from the Agent
# Runtime's pod, with the pod's own certificate files (MERIDIAN_TLS_*). The probe
# prints the HTTP status of one request and nothing else (but for the last mode);
# its argument says which:
#   health          GET /healthz, no client certificate
#   anonymous       POST /v1/chat, no client certificate
#   foreign-tenant  POST /v1/chat with the runtime's certificate, naming a tenant
#                   that is not the runtime's (services.yaml) but is one the
#                   gateway would serve (tenants.yaml: `evaluation` may run the
#                   claims-triage agent), so only the identity rule refuses it;
#                   an unknown tenant would be refused without S055
#   foreign-ca      the same request with a throwaway key and a self-signed
#                   certificate that carries the runtime's own URI (the pod's
#                   MERIDIAN_IDENTITY_PREFIX and the service's ID): the right
#                   name, the wrong CA. The key and certificate are made here,
#                   written to a directory under /tmp (the pod's one writable
#                   path) that is removed when the probe ends, and never printed
#                   or passed as an argument. It prints "refused" when the
#                   server ends the connection with a TLS alert or closes it
#                   once its own certificate verified (under TLS 1.3 the alert
#                   reaches the client with the first request, not the
#                   handshake), and the status when an answer came, which is a
#                   FAIL. Anything else (the server's certificate not
#                   verifying, a name that does not resolve, a refused
#                   connection, a timeout) stays a traceback.
# The audit line's constants (the row of the 403 above): the gateway's service
# name, the reason the identity rule writes for a name the caller may not use
# (a test keeps it equal to NAME_REFUSAL_REASON), the calling service, which is
# the deployment the probe runs in, and how far back and how long to look.
readonly IDENTITY_HOST=model-gateway.meridian.svc
readonly IDENTITY_PORT=8000
readonly IDENTITY_FOREIGN_TENANT=evaluation
readonly IDENTITY_CALLER=agent-runtime
readonly IDENTITY_GATEWAY_SERVICE=model-gateway
readonly IDENTITY_AUDIT_REASON=caller-name-not-allowed
readonly IDENTITY_AUDIT_WINDOW=120
readonly IDENTITY_AUDIT_ATTEMPTS=6
readonly IDENTITY_AUDIT_INTERVAL=2
readonly IDENTITY_PROBE='import datetime, http.client, json, os, ssl, sys, tempfile, uuid
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
mode, host, port, tenant = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
context = ssl.create_default_context(cafile=os.environ["MERIDIAN_TLS_CA_FILE"])
def answer():
    connection = http.client.HTTPSConnection(host, port, context=context, timeout=10)
    connection.connect()
    try:
        if mode == "health":
            connection.request("GET", "/healthz")
        else:
            headers = {"Content-Type": "application/json"}
            if mode != "anonymous":
                headers.update({"X-Meridian-Tenant": tenant, "X-Meridian-Agent": "claims-triage", "X-Meridian-Run": str(uuid.uuid4())})
            body = json.dumps({"messages": [{"role": "user", "content": "identity check"}]})
            connection.request("POST", "/v1/chat", body=body, headers=headers)
        return str(connection.getresponse().status)
    except ssl.SSLCertVerificationError:
        raise
    except (ssl.SSLError, ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        if mode != "foreign-ca":
            raise
        return "refused"
if mode == "foreign-ca":
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "foreign-ca probe")])
    now = datetime.datetime.now(datetime.timezone.utc)
    uri = os.environ["MERIDIAN_IDENTITY_PREFIX"] + "agent-runtime"
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(minutes=10))
        .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    with tempfile.TemporaryDirectory(prefix="foreign-ca-", dir="/tmp") as directory:
        with open(directory + "/tls.crt", "wb") as file:
            file.write(certificate.public_bytes(serialization.Encoding.PEM))
        with open(directory + "/tls.key", "wb") as file:
            file.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        context.load_cert_chain(directory + "/tls.crt", directory + "/tls.key")
        print(answer())
else:
    if mode == "foreign-tenant":
        context.load_cert_chain(os.environ["MERIDIAN_TLS_CERT_FILE"], os.environ["MERIDIAN_TLS_KEY_FILE"])
    print(answer())'

readonly CLAIMANT_START_URL=http://claims.meridian.localhost:8088/claimant/claims
# The second sentence of the claimant banner (templates/claimant_base.html).
readonly CLAIMANT_BANNER="Every name, address and description you enter must be fictional: never a real person's."
readonly COLLECTOR_ENDPOINT=otel-collector.observability.svc.cluster.local:4317
readonly GRAFANA_SERVICE=svc/kube-prometheus-stack-grafana
readonly POLL_TIMEOUT=120
readonly POLL_INTERVAL=3
readonly JOB_TIMEOUT=120s
# The dashboard's uid (infra/kind/dashboards/gateway-cost.json) and the series
# the gateway exports for it (their names are in src/meridian/platform/gateway).
readonly DASHBOARD_UID=meridian-gateway-cost
readonly DASHBOARD_FILE="${KIND_DIR}/dashboards/gateway-cost.json"
readonly COST_SERIES=(meridian_gateway_tokens_total meridian_gateway_cost_EUR_total meridian_gateway_calls_total)
# The service account the chart makes for Grafana (release name + "-grafana").
readonly GRAFANA_ACCOUNT=system:serviceaccount:observability:kube-prometheus-stack-grafana

failures=0
skips=0
grafana_url=""     # set by open_grafana
grafana_failed=0   # open_grafana failed once: later calls fail quietly
identity_answer="" # set by identity_status
poll_error=""      # what the last failed poll attempt saw
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
    pass "tools: each server (${servers//;/, }) answered unknown-run through the runtime's client, over TLS with its certificate"
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
  { set +x; } 2>/dev/null # a `bash -x` run must not trace the password
  printf 'user = "admin:%s"\n' "${password}" | curl -q --noproxy '*' -sS -m 15 -K - "$@"
}

# poll FILTER CURL_ARGS...: run gcurl until jq FILTER prints a non-empty value
# or POLL_TIMEOUT passes. The value is left in ${poll_result}. A failed attempt
# leaves its reason in ${poll_error}: curl's status and stderr, or the start of
# an answer the filter found nothing in; a success clears it.
poll() {
  local filter=$1
  shift
  local deadline=$((SECONDS + POLL_TIMEOUT)) body status err_file
  err_file="$(mktemp)"
  poll_error=""
  while ((SECONDS < deadline)); do
    if body="$(gcurl "$@" 2>"${err_file}")"; then
      if poll_result="$(jq -r "${filter}" <<<"${body}" 2>/dev/null)" &&
        [[ -n "${poll_result}" ]]; then
        poll_error=""
        rm -f "${err_file}"
        return 0
      fi
      poll_error="$(clean_lines "${body:0:160}")"
    else
      status=$?
      poll_error="curl exit ${status}: $(clean_lines "$(<"${err_file}")")"
    fi
    sleep "${POLL_INTERVAL}"
  done
  rm -f "${err_file}"
  poll_result=""
  return 1
}

cleanup() {
  if [[ -n "${pf_pid:-}" ]]; then
    kill "${pf_pid}" 2>/dev/null || true
    wait "${pf_pid}" 2>/dev/null || true # kubectl is gone when smoke.sh returns
  fi
  if [[ -n "${pf_log:-}" ]]; then rm -f "${pf_log}"; fi
}

# open_grafana: read the admin password into ${password} (gcurl uses it) and
# forward a local port to Grafana, once per run; the address is left in
# ${grafana_url}. Returns 0 at once when the forward is open and alive. Returns
# 1 after one FAIL line when it cannot open or has died; after that every call
# returns 1 without a line. The password is never an argument and never printed.
open_grafana() {
  { set +x; } 2>/dev/null # a `bash -x` run must not trace the password
  ((grafana_failed == 0)) || return 1
  local port="" waited=0 detail
  if [[ -n "${grafana_url}" ]]; then
    kill -0 "${pf_pid}" 2>/dev/null && return 0
    grafana_failed=1
    fail "grafana: the port-forward to Grafana died (kubectl said: $(clean_lines "$(<"${pf_log}")"))"
    return 1
  fi
  if ! password="$(kctl -n observability get secret grafana-admin \
    -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d)" || [[ -z "${password}" ]]; then
    grafana_failed=1
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
  while :; do
    port="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9]*\) ->.*/\1/p' "${pf_log}" | head -n 1)"
    [[ -z "${port}" ]] || break
    kill -0 "${pf_pid}" 2>/dev/null || break # kubectl has exited: no use waiting
    ((waited < 30)) || break
    sleep 1
    waited=$((waited + 1))
  done
  if [[ -z "${port}" ]]; then
    detail="$(clean_lines "$(<"${pf_log}")")"
    grafana_failed=1
    cleanup || true
    pf_pid=""
    pf_log=""
    fail "grafana: could not port-forward to Grafana (kubectl said: ${detail})"
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
# run_dashboard_query TITLE EXPR: one instant query through Grafana's datasource
# proxy; Prometheus must answer status success. Counts it in ${queries_run};
# on a refusal returns 1 with "<title>: <error>" in ${query_error}.
run_dashboard_query() {
  local title=$1 expr=$2 body status error
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

# run_dashboard_queries SERVED: every target of the served dashboard, with the
# range variable set to an hour (3600) and, for a target that names $dimension,
# once per option of that variable. Leaves the count in ${queries_run}; the first
# refusal returns 1 (see run_dashboard_query).
run_dashboard_queries() {
  local served=$1 total i title expr options option
  # shellcheck disable=SC2016 # both are Grafana's variable names, written literally
  local range_ref='${__range_s}' dimension_ref='$dimension'
  queries_run=0
  query_error=""
  options="$(jq -r '.templating.list[] | select(.name == "dimension") | .options[].value' <<<"${served}")"
  total="$(jq '[.panels[].targets[]?] | length' <<<"${served}")"
  for ((i = 0; i < total; i++)); do
    title="$(clean_lines "$(jq -r --argjson i "${i}" '[.panels[] | .title as $t | .targets[]? | $t][$i]' <<<"${served}")")"
    expr="$(jq -r --argjson i "${i}" '[.panels[].targets[]?][$i].expr' <<<"${served}")"
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

# The dashboard line. What Grafana serves must be the file's: a stale provisioned
# copy fails. Its queries are then run, so a renamed series or a typo shows here.
check_cost_dashboard() {
  local served title file_exprs served_exprs
  if ! poll '(select(.meta.provisioned == true) | .dashboard) // empty | tojson' \
    "${grafana_url}/api/dashboards/uid/${DASHBOARD_UID}"; then
    fail "dashboard: Grafana has no provisioned dashboard ${DASHBOARD_UID} after ${POLL_TIMEOUT}s (run make up) (last answer: ${poll_error})"
    return
  fi
  served="${poll_result}"
  title="$(clean_lines "$(jq -r '.title // empty' <<<"${served}" 2>/dev/null)")"
  if ! file_exprs="$(jq -c '[.panels[].targets[]?.expr]' "${DASHBOARD_FILE}")"; then
    fail "dashboard: could not read the queries of ${DASHBOARD_FILE}"
    return
  fi
  served_exprs="$(jq -c '[.panels[].targets[]?.expr]' <<<"${served}" 2>/dev/null || true)"
  if [[ "${served_exprs}" != "${file_exprs}" ]]; then
    fail "dashboard: Grafana serves \"${title}\" but its queries differ from infra/kind/dashboards/gateway-cost.json (run make up)"
    return
  fi
  if run_dashboard_queries "${served}"; then
    pass "dashboard: Grafana serves \"${title}\" (uid ${DASHBOARD_UID}), provisioned, with the file's queries; all ${queries_run} queries ran in Prometheus (range 3600s)"
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
    psql -d meridian -tAc "SELECT count(*) || '|' || coalesce(floor(extract(epoch FROM min(closed_at)))::bigint::text, '') FROM gateway.usage WHERE state = 'settled' AND closed_at > '${started}'" \
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

check_cost_panel() {
  if open_grafana; then # otherwise it printed the one FAIL line
    check_cost_dashboard
    check_cost_series
  fi
  check_grafana_rights
}

# ── 6. adjuster pages ────────────────────────────────────────────────────────
# The pages are served by the Claims API (S016), through the edge, in the form
# demo.sh reaches it. No line changes a claim: the second is refused by the
# origin check before any lookup, so CLM-9999 need not exist, and the third is a
# GET of the claimant's start page (S049).
check_adjuster_pages() {
  local found headers_file body_file status csp missing=""
  # Skipped only when no Meridian Deployment exists, as in check_tools.
  if ! found="$(deployed_services)"; then
    fail "adjuster pages: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "adjuster pages: the Meridian services are not deployed (make deploy)"
    return
  fi

  headers_file="$(mktemp)"
  body_file="$(mktemp)"
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -D "${headers_file}" -o "${body_file}" \
    -w '%{http_code}' "${ADJUSTER_QUEUE_URL}" 2>&1)"; then
    fail "adjuster pages: ${ADJUSTER_QUEUE_URL} did not answer: $(clean_lines "${status}")"
  else
    status="$(clean_lines "${status}")"
    # Header names are case-insensitive; the lines end in a carriage return.
    csp="$(grep -i '^content-security-policy:' "${headers_file}" | LC_ALL=C tr -cd '[:print:]' || true)"
    [[ "${status}" == 200 ]] || missing="status 200 (got ${status})"
    [[ "${csp}" == *"frame-ancestors 'none'"* ]] ||
      missing="${missing:+${missing}, }header Content-Security-Policy with frame-ancestors 'none'"
    [[ "${csp}" == *"default-src 'none'"* ]] ||
      missing="${missing:+${missing}, }header Content-Security-Policy with default-src 'none'"
    grep -qF "${ADJUSTER_BANNER}" "${body_file}" ||
      missing="${missing:+${missing}, }text \"${ADJUSTER_BANNER}\""
    if [[ -z "${missing}" ]]; then
      pass "adjuster pages: ${ADJUSTER_QUEUE_URL} -> 200 with a Content-Security-Policy of frame-ancestors 'none' and default-src 'none', and the synthetic-data line"
    else
      fail "adjuster pages: ${ADJUSTER_QUEUE_URL} lacks: ${missing}"
    fi
  fi
  rm -f "${headers_file}" "${body_file}"

  if ! status="$(curl -q --noproxy '*' -sS -m 10 -o /dev/null -w '%{http_code}' \
    -H 'Origin: http://attacker.example' --data 'decision=approve' \
    "${ADJUSTER_DECISION_URL}" 2>&1)"; then
    fail "adjuster pages: ${ADJUSTER_DECISION_URL} did not answer: $(clean_lines "${status}")"
  else
    status="$(clean_lines "${status}")"
    if [[ "${status}" == 403 ]]; then
      pass "adjuster pages: a decision posted with Origin http://attacker.example -> 403, refused before any lookup"
    else
      fail "adjuster pages: a decision posted with Origin http://attacker.example expected 403, got ${status}"
    fi
  fi

  # The claimant's start page (S049), served by the same app with the same
  # headers; its banner asks for fictional data (T-04). A GET, so no claim.
  headers_file="$(mktemp)"
  body_file="$(mktemp)"
  missing=""
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -D "${headers_file}" -o "${body_file}" \
    -w '%{http_code}' "${CLAIMANT_START_URL}" 2>&1)"; then
    fail "adjuster pages: ${CLAIMANT_START_URL} did not answer: $(clean_lines "${status}")"
  else
    status="$(clean_lines "${status}")"
    csp="$(grep -i '^content-security-policy:' "${headers_file}" | LC_ALL=C tr -cd '[:print:]' || true)"
    [[ "${status}" == 200 ]] || missing="status 200 (got ${status})"
    [[ "${csp}" == *"frame-ancestors 'none'"* ]] ||
      missing="${missing:+${missing}, }header Content-Security-Policy with frame-ancestors 'none'"
    [[ "${csp}" == *"default-src 'none'"* ]] ||
      missing="${missing:+${missing}, }header Content-Security-Policy with default-src 'none'"
    grep -qF "${CLAIMANT_BANNER}" "${body_file}" ||
      missing="${missing:+${missing}, }text \"${CLAIMANT_BANNER}\""
    if [[ -z "${missing}" ]]; then
      pass "adjuster pages: ${CLAIMANT_START_URL} -> 200 with a Content-Security-Policy of frame-ancestors 'none' and default-src 'none', and the fictional-data line (T-04)"
    else
      fail "adjuster pages: ${CLAIMANT_START_URL} lacks: ${missing}"
    fi
  fi
  rm -f "${headers_file}" "${body_file}"
}

# ── 7. sweep ─────────────────────────────────────────────────────────────────
# Only timestamps the API server set are compared (the CronJob's creation and
# lastScheduleTime, a Job's completionTime, creation and conditions): this
# laptop's clock is not the node's (see deploy.sh), so there is no "now". The
# newest of those timestamps is a lower bound of the server's now.
#
# sweep_verdict CRONJOB_JSON JOBS_JSON: one line, fields separated by "|":
#   succeeded|JOB|FINISHED_AT          the newest finished Job of the CronJob
#   failed|JOB|FINISHED_AT|REASON      (scheduled or made by hand with
#                                      `kubectl create job --from=cronjob/...`,
#                                      which has the same owner) and how it ended
#   stale|SCHEDULED_AT|FINISHED_AT     last scheduled more than three periods
#                                      after that Job finished, nothing running:
#                                      the schedule makes no finished runs
#   never|SECONDS                      never scheduled, and a timestamp the API
#                                      holds is more than three periods after
#                                      the CronJob's creation
#   unscheduled|SECONDS|CREATED_AT     never scheduled, no proof it is overdue
#   running                            no Job has finished; one is running
#   none|SCHEDULED_AT                  no Job has finished, none is running
sweep_verdict() {
  jq -nr --arg cronjob "${SWEEP_CRONJOB}" --argjson cj "$1" --argjson jobs "$2" \
    --argjson tolerance "$((SWEEP_PERIOD_SECONDS * SWEEP_STALE_PERIODS))" '
    def epoch: sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601;
    ($cj.status.lastScheduleTime // null) as $scheduled
    | (($cj.status.active // []) | length) as $active
    | [$jobs.items[]
        | select(any(.metadata.ownerReferences[]?; .kind == "CronJob" and .name == $cronjob))
        | . as $job
        | ([$job.status.conditions[]? | select(.status == "True" and (.type == "Complete" or .type == "Failed"))] | first // empty) as $done
        | {name: $job.metadata.name, type: $done.type, reason: ($done.reason // ""),
           at: ((if $done.type == "Complete" then ($job.status.completionTime // $done.lastTransitionTime) else $done.lastTransitionTime end) // "")}]
    | (sort_by([.at, .name]) | last) as $newest
    | ([$cj.metadata.creationTimestamp, $scheduled,
        ($jobs.items[] | .metadata.creationTimestamp, .status.startTime, .status.completionTime, (.status.conditions[]? | .lastTransitionTime))]
       | map(select(. != null and . != "") | epoch) | max) as $latest
    | ($latest - ($cj.metadata.creationTimestamp | epoch)) as $age
    | if $scheduled == null and $age > $tolerance then "never|\($age)"
      elif $newest == null then
        if $active > 0 then "running"
        elif $scheduled == null then "unscheduled|\($age)|\($cj.metadata.creationTimestamp)"
        else "none|\($scheduled)" end
      elif $scheduled != null and $active == 0 and (($scheduled | epoch) - ($newest.at | epoch)) > $tolerance then
        "stale|\($scheduled)|\($newest.at)"
      elif $newest.type == "Complete" then "succeeded|\($newest.name)|\($newest.at)"
      else "failed|\($newest.name)|\($newest.at)|\($newest.reason)" end
  '
}

# Same skip rule as the tool check: only when no Meridian Deployment exists. Only
# reads; the Jobs of the whole namespace are listed and filtered by owner.
check_sweep() {
  local found cronjob jobs verdict kind first second third
  if ! found="$(deployed_services)"; then
    fail "sweep: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "sweep: the Meridian services are not deployed (make deploy)"
    return
  fi
  if ! cronjob="$(kctl -n meridian get cronjob "${SWEEP_CRONJOB}" -o json --ignore-not-found)"; then
    fail "sweep: could not read cronjob/${SWEEP_CRONJOB} (kubectl's error is above)"
    return
  fi
  if [[ -z "${cronjob}" ]]; then
    fail "sweep: cronjob/${SWEEP_CRONJOB} does not exist (make deploy)"
    return
  fi
  if [[ "$(jq -r '.spec.suspend // false' <<<"${cronjob}")" == true ]]; then
    fail "sweep: cronjob/${SWEEP_CRONJOB} is suspended, so the sweep does not run"
    return
  fi
  if ! jobs="$(kctl -n meridian get job -o json)"; then
    fail "sweep: could not read the Jobs in meridian (kubectl's error is above)"
    return
  fi
  if ! verdict="$(sweep_verdict "${cronjob}" "${jobs}")"; then
    fail "sweep: could not read the CronJob's and the Jobs' timestamps and conditions"
    return
  fi
  IFS='|' read -r kind first second third <<<"${verdict}"
  first="$(clean_lines "${first}")"
  second="$(clean_lines "${second}")"
  third="$(clean_lines "${third}")"
  case "${kind}" in
    succeeded)
      pass "sweep: cronjob/${SWEEP_CRONJOB} is not suspended and its last finished Job, ${first}, succeeded at ${second}"
      ;;
    failed)
      fail "sweep: the last finished Job of cronjob/${SWEEP_CRONJOB}, ${first}, failed at ${second} (${third:-no reason given}); kubectl -n meridian describe job/${first} shows why, and kubectl -n meridian logs job/${first} what its pod printed, if a pod started"
      ;;
    stale)
      fail "sweep: the schedule is not producing finished runs: cronjob/${SWEEP_CRONJOB} was last scheduled at ${first}, more than $((SWEEP_PERIOD_SECONDS * SWEEP_STALE_PERIODS)) s after its newest finished Job finished at ${second}, and no Job is running"
      ;;
    never)
      fail "sweep: cronjob/${SWEEP_CRONJOB} has never been scheduled, although the API holds a timestamp ${first} s after its creation (more than three periods): the schedule is not producing runs"
      ;;
    unscheduled)
      skip "sweep: cronjob/${SWEEP_CRONJOB} has not been scheduled yet (created ${second}); there is no server-side clock to say how long that has been, and the newest timestamp the API holds is ${first} s after its creation, within the $((SWEEP_PERIOD_SECONDS * SWEEP_STALE_PERIODS)) s it allows"
      ;;
    running)
      skip "sweep: no Job of cronjob/${SWEEP_CRONJOB} has finished yet; the first one is running"
      ;;
    none)
      skip "sweep: no Job of cronjob/${SWEEP_CRONJOB} has finished yet (last scheduled at ${first})"
      ;;
    *)
      fail "sweep: unexpected verdict from the timestamps"
      ;;
  esac
}

# ── 8. network policy ────────────────────────────────────────────────────────
# The tool check above proves the paths the policies allow; this one proves a
# path they do not. Skipped like it, when the Claims API is not deployed.
check_network_policy() {
  local found policy out err_file
  if ! found="$(kctl -n meridian get deployment claims-api -o name --ignore-not-found)"; then
    fail "network policy: could not look for deployment/claims-api (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "network policy: the Meridian services are not deployed (make deploy)"
    return
  fi
  if ! policy="$(kctl -n meridian get networkpolicy default-deny -o name --ignore-not-found)"; then
    fail "network policy: could not look for networkpolicy/default-deny (kubectl's error is above)"
    return
  fi
  if [[ -z "${policy}" ]]; then
    fail "network policy: networkpolicy/default-deny does not exist in meridian: the chart was installed with networkPolicy.enabled=false, or not at all (make deploy)"
    return
  fi
  err_file="$(mktemp)"
  if out="$(kctl -n meridian exec deploy/claims-api -- python -c "${NETWORK_PROBE}" 2>"${err_file}")" && [[ "${out}" == blocked ]]; then
    pass "network policy: the Claims API cannot reach the Model Gateway (${NETWORK_PROBE_HOST}:${NETWORK_PROBE_PORT}), which no rule allows"
  elif [[ "${out}" == reached ]]; then
    fail "network policy: the Claims API reached the Model Gateway, which no rule allows: the cluster does not enforce NetworkPolicy, or a rule is too wide"
  else
    fail "network policy: the probe in deployment/claims-api gave no answer of reached or blocked: stdout: $(clean_lines "${out}"); stderr: $(clean_lines "$(<"${err_file}")")"
  fi
  rm -f "${err_file}"
}

# ── 9. service identity ──────────────────────────────────────────────────────
# identity_status MODE: the probe's answer for MODE in ${identity_answer}: the
# HTTP status it printed, or "error: ..." with what it wrote on stderr when it
# failed (a name that does not resolve, a certificate that does not verify, a
# refused connection: a traceback is never read as a status).
identity_status() {
  local err_file
  err_file="$(mktemp)"
  if identity_answer="$(kctl -n meridian exec deploy/agent-runtime -- \
    python -c "${IDENTITY_PROBE}" "$1" "${IDENTITY_HOST}" "${IDENTITY_PORT}" "${IDENTITY_FOREIGN_TENANT}" 2>"${err_file}")"; then
    identity_answer="$(clean_lines "${identity_answer}")"
  else
    identity_answer="error: $(clean_lines "$(<"${err_file}")")"
  fi
  rm -f "${err_file}"
}

# expect_identity_status MODE STATUS WHAT: PASS when the probe's answer is STATUS.
expect_identity_status() {
  local mode=$1 expected=$2 what=$3
  identity_status "${mode}"
  if [[ "${identity_answer}" == "${expected}" ]]; then
    pass "service identity: ${what} -> ${expected}"
  else
    fail "service identity: ${what}: expected ${expected}, got ${identity_answer}"
  fi
}

# check_gateway_refusal_row: the audit row of the 403 above, read in the
# database's primary pod the way check_cost_series reads the ledger. The reason
# the identity rule writes for a caller that names a tenant it may not
# (NAME_REFUSAL_REASON), the gateway as the service that wrote it (`service`),
# the calling service in `reference` (refuse_name in the gateway) and the tenant
# the probe named; `recorded_at` is the database's own clock (a timestamptz its
# insert trigger sets), so the age is the database's, not this laptop's. The
# gateway writes in a worker thread and at most one such row per reason, tenant
# and minute, so the query asks for a row that exists and is recent (never for a
# count that went up) and runs again for about ten seconds while there is none.
# Every value in the SQL is a constant of this script; none came from a pod.
check_gateway_refusal_row() {
  local primary err_file detail answer attempt what
  what="a refusal for ${IDENTITY_AUDIT_REASON} by ${IDENTITY_CALLER} (tenant ${IDENTITY_FOREIGN_TENANT})"
  err_file="$(mktemp)"
  if ! primary="$(kctl -n meridian get pod \
    -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary \
    -o jsonpath='{.items[0].metadata.name}' 2>"${err_file}")" || [[ -z "${primary}" ]]; then
    detail="$(clean_lines "$(<"${err_file}")")"
    rm -f "${err_file}"
    fail "service identity: no primary pod found for platform-db to read the audit row of the 403${detail:+ (kubectl said: ${detail})}"
    return
  fi
  for ((attempt = 1; attempt <= IDENTITY_AUDIT_ATTEMPTS; attempt++)); do
    if ! answer="$(kctl -n meridian exec "${primary}" -c postgres -- \
      psql -d meridian -tAc "SELECT floor(extract(epoch FROM now() - recorded_at))::bigint FROM audit.events WHERE service = '${IDENTITY_GATEWAY_SERVICE}' AND event = 'model.call' AND outcome = 'refused' AND reason = '${IDENTITY_AUDIT_REASON}' AND reference = '${IDENTITY_CALLER}' AND tenant = '${IDENTITY_FOREIGN_TENANT}' AND recorded_at > now() - interval '${IDENTITY_AUDIT_WINDOW} seconds' ORDER BY recorded_at DESC LIMIT 1" \
      2>"${err_file}")"; then
      detail="$(clean_lines "$(<"${err_file}")")"
      rm -f "${err_file}"
      fail "service identity: could not read audit.events in ${primary}${detail:+ (kubectl said: ${detail})}"
      return
    fi
    answer="$(clean_lines "${answer}")"
    [[ -z "${answer}" ]] || break
    ((attempt == IDENTITY_AUDIT_ATTEMPTS)) || sleep "${IDENTITY_AUDIT_INTERVAL}"
  done
  rm -f "${err_file}"
  if [[ -z "${answer}" ]]; then
    fail "service identity: the gateway's audit log has no row for ${what} in the last ${IDENTITY_AUDIT_WINDOW} s after ${IDENTITY_AUDIT_ATTEMPTS} tries: the 403 was not recorded, or was not the identity rule's"
  elif [[ "${answer}" =~ ^[0-9]+$ ]]; then
    pass "service identity: the gateway's audit log has ${what}, ${answer} s old"
  else
    fail "service identity: the audit query's answer was not a number of seconds"
  fi
}

check_service_identity() {
  local found
  if ! found="$(deployed_services)"; then
    fail "service identity: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "service identity: the Meridian services are not deployed (make deploy)"
    return
  fi
  expect_identity_status health 200 "GET /healthz on the Model Gateway with no certificate (the kubelet's probe sends none)"
  expect_identity_status anonymous 401 "POST /v1/chat on the Model Gateway with no certificate"
  expect_identity_status foreign-tenant 403 "POST /v1/chat with the Agent Runtime's certificate, naming a tenant it may not name"
  check_gateway_refusal_row
  expect_identity_status foreign-ca refused "POST /v1/chat with a certificate of the Agent Runtime's own name from another CA"
}

trap cleanup EXIT
check_edge
check_database
check_tools
check_telemetry
check_cost_panel
check_adjuster_pages
check_sweep
check_network_policy
check_service_identity

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
