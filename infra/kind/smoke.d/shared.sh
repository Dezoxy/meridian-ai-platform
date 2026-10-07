# shellcheck shell=bash

# Every psql this script runs gets these server settings, through PGOPTIONS in
# the exec (`env`: kubectl exec runs no shell, and a `SET` in the same -c would
# print a line of its own): a statement that runs longer than five seconds, or
# waits longer than three for a lock (a migration that holds one while smoke
# runs), is cancelled, and psql's message ends in the FAIL line of that check
# instead of the line hanging (the pgvector lines of check 2 keep the first line
# of it too since S073, K4: a read that failed is not "not installed").
# shellcheck disable=SC2034  # read by 02-database.sh, 05-cost-panel.sh and 09-service-identity.sh
readonly PSQL_OPTIONS='-c statement_timeout=5s -c lock_timeout=3s'
# The collector's OTLP HTTP port: the one the six services push to
# (telemetry.otlpEndpoint in values/meridian.yaml; a test keeps them equal), the
# one the collector's ingress admits the pods of `meridian` on, and, since S063,
# the one telemetrygen pushes to (--otlp-http). Never 4317, OTLP over gRPC, which
# the collector's ingress admits from no one.
# shellcheck disable=SC2034  # read by 04-telemetry.sh and by check 8, which smoke.sh still holds
readonly COLLECTOR_ENDPOINT=otel-collector.observability.svc.cluster.local:4318
readonly GRAFANA_SERVICE=svc/kube-prometheus-stack-grafana
readonly POLL_TIMEOUT=120
readonly POLL_INTERVAL=3

failures=0
skips=0
grafana_url=""     # set by open_grafana
grafana_failed=0   # open_grafana failed once: later calls fail quietly
network_pod=""     # the probe Pod of check 8 while it may exist
network_outsider="" # the probe Pod of check 8's collector line, in NETWORK_OUTSIDER_NAMESPACE
refused_request="" # the CertificateRequest of check 10 while it may exist
refused_err_file="" # the messages of check 10's commands, while it runs
poll_error=""      # what the last failed poll attempt saw
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; failures=$((failures + 1)); }
skip() { printf 'SKIP  %s\n' "$*"; skips=$((skips + 1)); }

# deployed_services: the Meridian Deployments, one name per line (empty when
# there are none). Stdout only: a warning on stderr is not a Deployment; it goes
# to the terminal. Shared with the cost check (5).
deployed_services() {
  kctl -n meridian get deployment \
    -l app.kubernetes.io/part-of=meridian -o name --ignore-not-found
}

# clean_lines TEXT: TEXT on one line, its lines joined with ";", without any byte
# that is not printable ASCII (see `clean` in demo.sh). Whatever this script
# prints that came out of a pod goes through it, so a hostile answer cannot
# inject terminal escape sequences or extra lines.
clean_lines() { printf '%s' "$1" | LC_ALL=C tr -cd '[:print:]\n' | paste -sd ';' -; }

# show_wait_error TEXT: what a failed `kctl wait` printed on standard error (TEXT,
# captured by the site), on standard error again, a line at a time and without
# any byte that is not printable ASCII, except kubectl's own line for a wait that
# merely ran out of time ("error: timed out waiting for the condition on ..."):
# the FAIL line that follows says that already. Anything else is shown, so a wait
# that was ended some other way (for example by the bound of the call, when the
# API server did not answer, which dies with a sentence of its own) does not stop
# smoke without a word. It does not read the wrapper's text: it filters the one
# line it knows to be noise.
show_wait_error() {
  local line
  while IFS= read -r line; do
    line="$(printf '%s' "${line}" | LC_ALL=C tr -cd '[:print:]')"
    [[ -n "${line}" && "${line}" != "error: timed out waiting for the condition"* ]] || continue
    printf '%s\n' "${line}" >&2
  done <<<"$1"
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
      # shellcheck disable=SC2034  # read by 04-telemetry.sh, 05-cost-panel.sh, 07-sweep.sh and 11-alert-rules.sh
      poll_error="curl exit ${status}: $(clean_lines "$(<"${err_file}")")"
    fi
    sleep "${POLL_INTERVAL}"
  done
  rm -f "${err_file}"
  poll_result=""
  return 1
}

# network_delete_pod: delete the probe Pod of check 8 when one was named, and
# forget it only when kubectl said it is gone (a pod that never existed is not
# an error), so a delete that failed is tried again by the EXIT trap, as
# refused_delete_request does. Returns 1 when the delete failed. Nothing is
# waited for: the Pod's process is a sleep.
network_delete_pod() {
  [[ -n "${network_pod}" ]] || return 0
  kctl -n meridian delete pod "${network_pod}" --ignore-not-found --wait=false >/dev/null 2>&1 || return 1
  network_pod=""
}

# refused_delete_request [ERR_FILE]: delete the CertificateRequest of check 10
# when one was named, and forget it only when kubectl said it is gone (a
# request that never existed is not an error), so a delete that failed is tried
# again by the EXIT trap. kubectl's stderr goes to ERR_FILE when one is given.
# Nothing is waited for: a request has no finalizer.
refused_delete_request() {
  [[ -n "${refused_request}" ]] || return 0
  kctl -n "${REFUSED_NAMESPACE}" delete certificaterequest "${refused_request}" \
    --ignore-not-found --wait=false >/dev/null 2>"${1:-/dev/null}" || return 1
  refused_request=""
}

cleanup() {
  # On stderr, not stdout: the lines a reader counts do not change.
  network_delete_pod || echo "smoke: could not delete the probe pod ${network_pod} in meridian; delete it by hand: kubectl -n meridian delete pod ${network_pod}" >&2
  network_outsider_delete || echo "smoke: could not delete the probe pod ${network_outsider} in ${NETWORK_OUTSIDER_NAMESPACE}; delete it by hand: kubectl -n ${NETWORK_OUTSIDER_NAMESPACE} delete pod ${network_outsider}" >&2
  refused_delete_request || true
  if [[ -n "${refused_err_file}" ]]; then rm -f "${refused_err_file}"; fi
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

# network_outsider_delete: delete the probe Pod of the collector line when one was
# named, and forget it only when kubectl said it is gone, as network_delete_pod
# does for the other Pod, so a delete that failed is tried again by the EXIT trap.
# Returns 1 when the delete failed.
network_outsider_delete() {
  [[ -n "${network_outsider}" ]] || return 0
  kctl -n "${NETWORK_OUTSIDER_NAMESPACE}" delete pod "${network_outsider}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || return 1
  network_outsider=""
}
