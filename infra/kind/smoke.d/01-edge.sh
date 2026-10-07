# shellcheck shell=bash
#   1. edge:      laptop -> 127.0.0.1:8088 -> kind port mapping -> NodePort -> Envoy

readonly EDGE_URL=http://127.0.0.1:8088/

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
