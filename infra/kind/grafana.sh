#!/usr/bin/env bash
# Grafana access on kind. Grafana is deliberately not routed through the edge
# gateway (threat model T-03), so the only way in is a port-forward.
#   grafana.sh forward    `make grafana`: 127.0.0.1:3000 until Ctrl-C
#   grafana.sh password   `make grafana-password`: prints the admin password
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly GRAFANA_SERVICE=svc/kube-prometheus-stack-grafana
readonly GRAFANA_LOCAL_PORT=3000

need_tools kubectl
need_cluster

case "${1:-}" in
  forward)
    printf 'Grafana: http://127.0.0.1:%s\nuser admin; password: make grafana-password\nCtrl-C stops the forward.\n' \
      "${GRAFANA_LOCAL_PORT}"
    exec kubectl --kubeconfig "${KUBECONFIG_FILE}" --context "${KUBE_CONTEXT}" \
      -n observability port-forward --address 127.0.0.1 "${GRAFANA_SERVICE}" \
      "${GRAFANA_LOCAL_PORT}:80"
    ;;
  password)
    need_tools base64
    kctl -n observability get secret grafana-admin -o jsonpath='{.data.admin-password}' |
      base64 -d
    printf '\n'
    ;;
  *)
    die "usage: grafana.sh forward|password"
    ;;
esac
