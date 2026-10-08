#!/usr/bin/env bash
# Prove the local platform works end to end: `make smoke`. Changes nothing apart
# from four short-lived Jobs in meridian (unique names; each ends at an
# activeDeadlineSeconds even when its pod never starts, and is removed by
# ttlSecondsAfterFinished once it has finished or failed), two short-lived Pods
# of the network policy check (one
# in meridian, one in default; unique names, each deleted when its line ends and
# again by the EXIT trap), one CertificateRequest in default of
# the certificate policy check (unique name, a request the issuer must refuse,
# deleted as soon as it is read and again by the EXIT trap) and, at most once
# per throttle window per tool server, the refusal's audit row that the tool
# check below causes. The service identity check (9) also leaves two refusal
# rows in the audit table on each run (a 401's and a 403's, which the gateway
# throttles to one a minute per reason) and puts a throwaway key in the probe
# pod's /tmp, which the probe removes when it ends; and the checks that read
# Grafana (4, 5, 7 and 11) read its admin Secret, never printing it.
# A check in smoke.d/ has its paragraph, constants and functions there.
# The checks, in order: 01 edge, 02 database, 03 tools, 04 telemetry, 05 cost
# panel, 06 adjuster pages, 07 sweep, 08 network policy, 09 service identity, 10
# certificate policy, 11 alert rules, 12 telemetry stores (one more short-lived Pod
# in observability, unique name, deleted when the check ends and by the EXIT trap),
# 13 issuer (S021: one SKIP line unless MERIDIAN_IDENTITY=keycloak; reads only).
# Prints one PASS, FAIL or SKIP line per check and exits non-zero on any FAIL.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
# shellcheck source=smoke.d/shared.sh
. "${KIND_DIR}/smoke.d/shared.sh"
# shellcheck source=smoke.d/01-edge.sh
. "${KIND_DIR}/smoke.d/01-edge.sh"
# shellcheck source=smoke.d/02-database.sh
. "${KIND_DIR}/smoke.d/02-database.sh"
# shellcheck source=smoke.d/03-tools.sh
. "${KIND_DIR}/smoke.d/03-tools.sh"
# shellcheck source=smoke.d/04-telemetry.sh
. "${KIND_DIR}/smoke.d/04-telemetry.sh"
# shellcheck source=smoke.d/05-cost-panel.sh
. "${KIND_DIR}/smoke.d/05-cost-panel.sh"
# shellcheck source=smoke.d/06-adjuster-pages.sh
. "${KIND_DIR}/smoke.d/06-adjuster-pages.sh"
# shellcheck source=smoke.d/07-sweep.sh
. "${KIND_DIR}/smoke.d/07-sweep.sh"
# shellcheck source=smoke.d/08-network-policy.sh
. "${KIND_DIR}/smoke.d/08-network-policy.sh"
# shellcheck source=smoke.d/09-service-identity.sh
. "${KIND_DIR}/smoke.d/09-service-identity.sh"
# shellcheck source=smoke.d/10-certificate-policy.sh
. "${KIND_DIR}/smoke.d/10-certificate-policy.sh"
# shellcheck source=smoke.d/11-alert-rules.sh
. "${KIND_DIR}/smoke.d/11-alert-rules.sh"
# shellcheck source=smoke.d/12-telemetry-stores.sh
. "${KIND_DIR}/smoke.d/12-telemetry-stores.sh"
# shellcheck source=smoke.d/13-issuer.sh
. "${KIND_DIR}/smoke.d/13-issuer.sh"

need_tools docker kubectl curl jq base64 openssl timeout
# A mistyped MERIDIAN_IDENTITY stops here, before the cluster is asked (common.sh).
identity_switch_check
# The same for MERIDIAN_SIGNIN (S021, Y4b): with `staff`, check 6 expects the queue to
# send a person with no session to the issuer (smoke.d/06-adjuster-pages.sh).
signin_switch_check
require_local_docker
need_cluster

# The EXIT trap deletes what a run made. Bash does not reliably run it when a
# signal ends the script (SIGHUP: an SSH session that drops, a closed terminal),
# so each signal has a trap that exits with 128 plus its number, which runs the
# EXIT trap; the `pytest-db` recipe of the Makefile does the same.
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
check_edge
check_database
check_tools
check_telemetry
check_cost_panel
check_adjuster_pages
check_sweep
check_network_policy
check_service_identity
check_certificate_policy
check_alert_rules
check_telemetry_stores
check_issuer

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
