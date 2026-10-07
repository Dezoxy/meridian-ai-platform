# shellcheck shell=bash
#   8. network policy: six lines (S019, S062, S063, S066). Each opens a TCP connection and
#                 nothing more; a path that no rule allows is a PASS only when it
#                 times out, and a connection refused or a name that does not
#                 resolve is a FAIL, never "blocked". A policy that is missing,
#                 or too wide, makes a denied path answer, so each denied line
#                 can fail. Run in this order, the control first:
#                 - the control: from the Claims API's pod to the Agent Runtime,
#                   which the Claims API's policy and the Agent Runtime's both
#                   name. It must be reached. The probe, the name resolution and
#                   the pod's egress to a named peer work, so a "blocked" below
#                   is not a broken probe. If it fails, the four lines below
#                   would prove nothing and are not printed.
#                 - the Claims API to the Model Gateway, which no rule of the
#                   Claims API's policy names: it must time out.
#                 - the Claims API to the API server's Service address
#                   (kubernetes.default.svc:443), the check's "egress to what
#                   the policy does not list": no service's policy has a rule for
#                   an address or for 443 or 6443, and without a policy the
#                   connection is reached (the database's policy lists 6443,
#                   which is how the instance manager reaches it). It is a
#                   target inside the cluster, so smoke sends nothing off the
#                   machine; an address in a documentation range (192.0.2.1)
#                   was not used because it times out with or without a policy.
#                 - the database's ingress: a probe Pod of the Claims API's own
#                   image and securityContext (nothing is pulled), labelled
#                   app.kubernetes.io/name=meridian-sweep and not
#                   app.kubernetes.io/part-of=meridian. The sweep's policy lets
#                   such a pod reach DNS and the database, so its egress is not
#                   what blocks it (a pod no policy names has none, and its
#                   "blocked" would be default-deny's egress, not the database's
#                   ingress); the database's ingress admits pods by part-of
#                   alone, so the connection to platform-db-rw.meridian.svc:5432
#                   must time out. Then the same pod is given the part-of label
#                   and the same probe must reach the database (a few tries: the
#                   network plugin takes a moment): it is the control, one pod
#                   with and without the label. No Service or Deployment selects
#                   the name label of the sweep, so the pod takes no traffic.
#                 - the collector (S063): a second probe Pod, of the same image
#                   and securityContext, in the namespace `default` (one that
#                   exists on every cluster, outside `meridian` and
#                   `observability`), with no name label of a workload and none
#                   that a policy selects, opens a connection to the collector's
#                   OTLP HTTP port, otel-collector.observability.svc.cluster.
#                   local:4318, the port the six services push to. The
#                   collector's ingress admits the pods of `meridian` alone
#                   (manifests/observability-networkpolicy.yaml, which `make up`
#                   applies), so it must time out. A policy that is missing or
#                   too wide, or a cluster that does not enforce it, makes the
#                   collector answer, and the line fails; a refusal, a name that
#                   does not resolve and a failed exec are failures too, never
#                   "blocked". It is skipped, one line, when the collector's
#                   Deployment is not there. The control above is this line's
#                   control for the probe: the same probe, the same image, the
#                   name resolution and the cluster's enforcement work, and it is
#                   not printed when the control failed. A timeout alone could
#                   also be a collector that is up but hangs, so the line has a
#                   second control, check 4's push: a pod the policies admit (a
#                   telemetrygen Job in `meridian`, over TLS) reached the
#                   collector in this run (check 4 runs first and leaves the
#                   word in ${telemetry_pushed}). When it did not, a timeout
#                   from `default` is a FAIL that proves nothing ("the collector
#                   was not reached from meridian either, so a timeout from
#                   default shows nothing"), not a PASS; an answer from the
#                   collector is the same FAIL as ever, whatever check 4 did.
#                   What the fifth line does not prove:
#                   that a pod of `meridian` can push (check 4 does, with the
#                   telemetrygen Jobs it runs there, and this line depends on
#                   it); that 4317, the collector's gRPC port, is closed to every
#                   pod (the collector's values leave the receiver off, and the
#                   policy admits no one; this line probes 4318 only, the port a
#                   Meridian pod is let in on); or
#                   that a pod of another namespace than `default` is refused
#                   (the rule is "the namespace meridian only", and `default`
#                   stands for all the others). It adds about 10 s: one timeout of 4 s and
#                   the second Pod's start, and at most 60 s more for its start
#                   when something is wrong.
#                 - the rate store (S066), last, from a probe Pod of its own,
#                   which proves the store's INGRESS rule and not the
#                   sender's egress rule: the connection to
#                   rate-store.meridian.svc:6379, the store that holds the Model
#                   Gateway's rate windows, must time out. The Pod has the
#                   sweep's name label and the label of smoke's own, and
#                   kind's policy manifests/smoke-rate-store-networkpolicy.yaml
#                   (which `make up` applies) gives the pods with that label an
#                   egress rule to the store's pods on its port, so nothing but
#                   the store's ingress rule, which admits the Model Gateway's
#                   pods alone, can make it time out (from the Claims API's
#                   pod, which has no egress rule to the store, the packets
#                   were dropped at the sender and the line passed with the
#                   store's policy deleted). A pod that could connect would
#                   read or reset every tenant's window; a refusal and a name
#                   that does not resolve (a store that is not deployed) are
#                   FAIL lines, never "blocked", and so is a missing kind policy
#                   (without it the timeout would be the sender's) and a kind policy
#                   of the wrong shape (read from its JSON before any Pod starts:
#                   it must select the probe label, list Egress and name the
#                   store's pods on TCP 6379, or the timeout would again be the
#                   sender's). The
#                   control, as the database's line does it: the same Pod given
#                   the label app.kubernetes.io/name=model-gateway, which the
#                   gateway's own egress rule and the store's ingress rule
#                   admit, must reach the port (a connection that is then reset
#                   or answered is "reached": the Pod holds no certificate and
#                   no password, so it can do nothing there), in up to four
#                   tries; when it does not, the control did not reach and the
#                   line proves nothing, which is a FAIL. While it carries
#                   that name the Pod, which has no readiness probe, is an
#                   endpoint of the model-gateway Service, so it is labelled
#                   back to the sweep's name straight after the control, and
#                   deleted at the end. The line is one PASS when both hold
#                   (the count of lines does not move), and is not printed when
#                   the check's own control failed. What the sixth line does not
#                   prove: that the store is up and serving, since a timeout is
#                   also what a store that hangs gives. That is read elsewhere:
#                   `make deploy` waits for the store's Deployment and its
#                   Certificate, and a completed model call (check 5's series)
#                   shows the gateway reached the store and was answered. It
#                   adds a Pod's start and one timeout of 4 s.
#                 The Pod carries one label of smoke's own as well,
#                 meridian-smoke=network-probe, which no policy of the chart,
#                 Service or Deployment selects (kind's policy for the rate
#                 store line does, and gives egress to the store alone), so it
#                 changes nothing they see. A Pod
#                 that a lost trap left (a run killed with SIGKILL, or a power
#                 cut) stays as a Failed object once its five minutes have
#                 passed, with the labels the policies select on; so the check
#                 starts by listing the Pods with that label, even when it will
#                 skip, and deletes by name those older than 300 s (a younger
#                 one is another run's, which two runs at once leave to each
#                 other; a list that cannot be read is not an error).
#                 The Pod is deleted when the check ends and by the EXIT trap,
#                 which also runs on SIGHUP, SIGINT and SIGTERM (each has a trap
#                 that exits, as the Makefile's pytest-db recipe does: bash does
#                 not reliably run the EXIT trap on a hang-up without it). A
#                 delete that failed is not forgotten: the trap tries again
#                 and says on stderr, with the command to run by hand, when it
#                 fails too. The Pod ends on its own after five minutes, and is
#                 not created when the control failed. The check fails when the
#                 policy `default-deny` does not exist.
#                 Every service's policy OBJECT is read, and one is probed
#                 (S073, K4). The check lists the Meridian Deployments as
#                 checks 3, 5 and 7 do (the label part-of=meridian) and, from
#                 one listing of the namespace's NetworkPolicies, prints one
#                 FAIL line that names each service whose policy of the same
#                 name is not there (the chart makes one per Deployment, the
#                 rate store's included). The probes stay as they are: the
#                 Claims API's pod and the one probe Pod each, no Pod per
#                 service, so what the policies DO is proved for the Claims
#                 API's egress and the database, the collector and the rate
#                 store's ingress alone. When the Deployments exist and the
#                 Claims API's is not among them the check fails, because the
#                 probes run in its pod. The line prints nothing when all
#                 policies are there (the count of lines does not move).
#                 Skipped, one line instead of six, while no Meridian
#                 Deployment exists (`make deploy`). What it does not prove: that
#                 every other pair of pods is allowed or denied as the chart
#                 says (the chart's tests render and compare the rules); that a
#                 pod of another namespace cannot reach the database, or that an
#                 address outside the machine is unreachable (S019 proved the
#                 database pod's 443 and 80 by hand; it stays by hand, because
#                 smoke sends nothing off the machine); that the API server
#                 answers without a policy (it does for the database's pod,
#                 which S019 measured); and nothing about UDP. Adds about 20 s:
#                 three timeouts of 4 s, the Pod's start and the exec calls
#                 (when something is wrong, at most 60 s for the Pod to be
#                 Ready and 16 s for the label's tries).

# The connections the network-policy check (8) tries, as host:port. The Agent
# Runtime is the control: the Claims API's policy and its own name each other. The
# Model Gateway is a Service which only the Agent Runtime, the knowledge tool
# server and the ingestion Job may reach. The API server's Service is one no
# service's policy lists. The database's read-write Service is the one every
# workload's policy lists and only the pods labelled part-of=meridian are admitted
# to (manifests/platform-db-networkpolicy.yaml).
readonly NETWORK_RUNTIME=agent-runtime.meridian.svc:8000
readonly NETWORK_GATEWAY=model-gateway.meridian.svc:8000
readonly NETWORK_API_SERVER=kubernetes.default.svc:443
readonly NETWORK_DATABASE=platform-db-rw.meridian.svc:5432
# The rate store's Service (S066): the host is the DNS name of its Certificate
# and the port its Service's; only the Model Gateway's pods are admitted to it.
readonly NETWORK_RATE_STORE=rate-store.meridian.svc:6379
# Kind's own policy that gives the probe Pod its egress to the store
# (manifests/smoke-rate-store-networkpolicy.yaml, applied by `make up`): the line
# looks for it, because without it a timeout would be the sender's and the line
# would pass with the store wide open.
readonly NETWORK_RATE_STORE_POLICY=meridian-smoke-rate-store
# The label of the store's pods, which that policy's rule must name, as the chart's
# own do (the line also reads the policy's shape, not only that it exists).
readonly NETWORK_RATE_STORE_LABEL=app.kubernetes.io/name=rate-store
# The probe Pod: the sweep's name label (its policy reaches DNS and the database
# and no Service or Deployment selects it), a sleep that ends on its own, and how
# long to wait for it and for the network plugin to see the label added to it.
readonly NETWORK_POD_NAME_LABEL=meridian-sweep
# The label of smoke's own that the Pod carries as well (no policy, Service or
# Deployment selects by it, so it changes nothing they see): the next run lists
# the Pods with it and deletes by name those older than NETWORK_LEFTOVER_AGE
# seconds, as it does for check 10's request (REFUSED_LEFTOVER_AGE).
readonly NETWORK_POD_SMOKE_LABEL=meridian-smoke=network-probe
# The collector line (the fifth of check 8) probes from a Pod in this namespace:
# one that exists on every cluster and is neither `meridian` nor `observability`.
# The Pod's name is NETWORK_OUTSIDER_PREFIX and the time.
readonly NETWORK_OUTSIDER_NAMESPACE=default
readonly NETWORK_OUTSIDER_PREFIX=smoke-outsider-
readonly NETWORK_LEFTOVER_AGE=300
readonly NETWORK_POD_LIFETIME=300
readonly NETWORK_POD_READY_TIMEOUT=60s
readonly NETWORK_LABEL_ATTEMPTS=4
readonly NETWORK_LABEL_INTERVAL=1
# The image has no curl, so Python opens the connection; the host and the port are
# its two arguments. The snippet prints "reached" or "blocked" and exits 0 either
# way; anything else (a name that does not resolve, a refused connection) is a
# traceback and a non-zero exit, which the check reports as a failure, not as
# "blocked": only a timeout is what a policy that drops packets looks like.
readonly NETWORK_PROBE_TIMEOUT=4
readonly NETWORK_PROBE='import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout='"${NETWORK_PROBE_TIMEOUT}"').close()
    print("reached")
except TimeoutError:
    print("blocked")'
network_answer=""  # set by network_probe

# ── 8. network policy ────────────────────────────────────────────────────────
# The tool check above proves the paths the policies allow; this one proves
# that the paths they do not allow are closed, and that its probe can tell. Skipped
# like it, when no Meridian Deployment exists.

# network_sweep_leftovers: at the start of check 8, delete by name the Pods with
# NETWORK_POD_SMOKE_LABEL that are older than NETWORK_LEFTOVER_AGE seconds: what
# a run whose trap was lost (SIGKILL, a power cut) left. A Pod that passed its
# activeDeadlineSeconds stays as a Failed object, with the labels the policies
# select on. A younger one is another run's that is under way, and is its own to
# delete. The age is this machine's clock against the Pod's creationTimestamp
# (the API server's), so a skewed clock only delays the sweep. A list that cannot
# be read is not an error: the check goes on, and the leftover waits.
network_sweep_leftovers() {
  local namespace=${1:-meridian} json names leftover
  json="$(kctl -n "${namespace}" get pod -l "${NETWORK_POD_SMOKE_LABEL}" \
    -o json 2>/dev/null)" || return 0
  names="$(jq -r --argjson age "${NETWORK_LEFTOVER_AGE}" '
    .items[] | select(now - (.metadata.creationTimestamp | fromdateiso8601) > $age)
    | .metadata.name' <<<"${json}" 2>/dev/null)" || return 0
  while IFS= read -r leftover; do
    [[ -n "${leftover}" ]] || continue
    kctl -n "${namespace}" delete pod "${leftover}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  done <<<"${names}"
}

# network_probe WHERE TARGET [NAMESPACE]: the probe from WHERE (deploy/claims-api,
# or the name of the probe Pod) in NAMESPACE (meridian unless given) to TARGET
# (host:port), its answer in ${network_answer}:
# "reached", "blocked", or "error: ..." with what it wrote on stderr when it failed
# (a traceback is never read as an answer).
network_probe() {
  local where=$1 target=$2 namespace=${3:-meridian} err_file
  err_file="$(mktemp)"
  if network_answer="$(kctl -n "${namespace}" exec "${where}" -- \
    python -c "${NETWORK_PROBE}" "${target%:*}" "${target##*:}" 2>"${err_file}")"; then
    network_answer="$(clean_lines "${network_answer}")"
  else
    network_answer="error: $(clean_lines "${network_answer}") $(clean_lines "$(<"${err_file}")")"
  fi
  rm -f "${err_file}"
}

# network_expect EXPECTED WHERE TARGET PASS_TEXT WRONG_TEXT [NAMESPACE
# [UNPROVEN_TEXT]]: a PASS line with PASS_TEXT when the probe's answer is
# EXPECTED (reached or blocked); a FAIL with WRONG_TEXT when it is the other
# word; a FAIL with what it said when it is neither. WHERE is in NAMESPACE
# (meridian unless given). A non-empty UNPROVEN_TEXT turns the PASS into a FAIL
# with that text: the answer is the expected one, but a control the caller
# needs for it to mean anything did not hold. Returns 1 unless it passed.
network_expect() {
  local expected=$1 where=$2 target=$3 pass_text=$4 wrong_text=$5 namespace=${6:-meridian} unproven_text=${7:-}
  network_probe "${where}" "${target}" "${namespace}"
  if [[ "${network_answer}" == "${expected}" && -n "${unproven_text}" ]]; then
    fail "network policy: ${unproven_text}"
  elif [[ "${network_answer}" == "${expected}" ]]; then
    pass "network policy: ${pass_text}"
    return 0
  elif [[ "${network_answer}" == reached || "${network_answer}" == blocked ]]; then
    fail "network policy: ${wrong_text}"
  else
    fail "network policy: the probe in ${where} to ${target} gave no answer of reached or blocked: ${network_answer}"
  fi
  return 1
}

# network_pod_spec NAME NAMESPACE NAME_LABEL: the probe Pod as JSON, from the
# Claims API's Deployment: its image, pull policy and security contexts, so the
# Pod runs what is on the node under the same restrictions, and a sleep that ends
# on its own. Its labels are NAME_LABEL as the name label (the sweep's, and not
# part-of: see the header; empty: no name label at all, for the Pod in `default`)
# and smoke's own, which the next run's sweep finds a leftover by.
network_pod_spec() {
  kctl -n meridian get deployment claims-api -o json | jq --arg name "$1" \
    --arg namespace "$2" --arg label "$3" --argjson lifetime "${NETWORK_POD_LIFETIME}" \
    --arg smoke_key "${NETWORK_POD_SMOKE_LABEL%%=*}" --arg smoke_value "${NETWORK_POD_SMOKE_LABEL#*=}" '
    .spec.template.spec as $pod | $pod.containers[0] as $container | {
      apiVersion: "v1", kind: "Pod",
      metadata: {
        name: $name, namespace: $namespace,
        labels: ({($smoke_key): $smoke_value}
          + (if $label == "" then {} else {"app.kubernetes.io/name": $label} end))
      },
      spec: {
        restartPolicy: "Never", automountServiceAccountToken: false,
        activeDeadlineSeconds: $lifetime, securityContext: $pod.securityContext,
        containers: [{
          name: "probe", image: $container.image, imagePullPolicy: $container.imagePullPolicy,
          command: ["python", "-c", "import time; time.sleep(\($lifetime))"],
          securityContext: $container.securityContext,
          resources: {requests: {cpu: "10m", memory: "32Mi"}, limits: {memory: "128Mi"}}
        }]
      }
    }'
}

# network_start_pod: start the probe Pod and wait until it is Ready. One FAIL
# line (the database line) and 1 when it cannot. ${network_pod} is set before the
# Pod exists, so the trap deletes it whichever step fails.
network_start_pod() {
  local err_file detail wait_said
  network_pod="smoke-network-$(date +%s)"
  err_file="$(mktemp)"
  if ! network_pod_spec "${network_pod}" meridian "${NETWORK_POD_NAME_LABEL}" 2>"${err_file}" |
    kctl -n meridian create -f - >/dev/null 2>>"${err_file}"; then
    detail="$(clean_lines "$(<"${err_file}")")"
    rm -f "${err_file}"
    fail "network policy: could not start the probe pod ${network_pod} in meridian (${detail})"
    return 1
  fi
  rm -f "${err_file}"
  # The wait's error is captured and not sent to a file: when the call's bound
  # ends it, kctl dies with a sentence in the capturing subshell, and the sentence
  # must reach the terminal (show_wait_error) and the FAIL line, not a file nobody
  # reads. Smoke goes on: every later call is bounded too.
  if ! wait_said="$(kctl -n meridian wait --for=condition=Ready "pod/${network_pod}" \
    --timeout="${NETWORK_POD_READY_TIMEOUT}" 2>&1 >/dev/null)"; then
    show_wait_error "${wait_said}"
    fail "network policy: the probe pod ${network_pod} did not become Ready within ${NETWORK_POD_READY_TIMEOUT} ($(clean_lines "${wait_said}"))"
    return 1
  fi
}

# network_database_lines: the database's line, from the probe Pod. Unlabelled it
# must time out; given the part-of label (the rule the database admits by) the same
# probe must reach, for up to NETWORK_LABEL_ATTEMPTS tries.
network_database_lines() {
  local part_of=app.kubernetes.io/part-of=meridian attempt
  network_probe "${network_pod}" "${NETWORK_DATABASE}"
  if [[ "${network_answer}" == reached ]]; then
    fail "network policy: a pod without the label ${part_of} reached the database (${NETWORK_DATABASE}): the database's ingress is too wide, or the cluster does not enforce it"
    return
  elif [[ "${network_answer}" != blocked ]]; then
    fail "network policy: the probe in ${network_pod} to ${NETWORK_DATABASE} gave no answer of reached or blocked: ${network_answer}"
    return
  fi
  if ! kctl -n meridian label pod "${network_pod}" "${part_of}" >/dev/null 2>&1; then
    fail "network policy: could not add the label ${part_of} to the probe pod ${network_pod}"
    return
  fi
  for ((attempt = 1; attempt <= NETWORK_LABEL_ATTEMPTS; attempt++)); do
    network_probe "${network_pod}" "${NETWORK_DATABASE}"
    [[ "${network_answer}" == blocked ]] || break
    ((attempt == NETWORK_LABEL_ATTEMPTS)) || sleep "${NETWORK_LABEL_INTERVAL}"
  done
  if [[ "${network_answer}" == reached ]]; then
    pass "network policy: a pod without the label ${part_of} cannot reach the database (${NETWORK_DATABASE}), and with that label added the same pod can"
  elif [[ "${network_answer}" == blocked ]]; then
    fail "network policy: the pod still cannot reach the database (${NETWORK_DATABASE}) after the label ${part_of} was added, in ${NETWORK_LABEL_ATTEMPTS} tries: the sweep's egress or the database's ingress is too narrow, and the line above would prove nothing"
  else
    fail "network policy: the probe in ${network_pod} to ${NETWORK_DATABASE} gave no answer of reached or blocked: ${network_answer}"
  fi
}

check_network_database() {
  if network_start_pod; then
    network_database_lines
  fi
  network_delete_pod || true # the Pod stays named: the EXIT trap tries again
}

# network_outsider_start: start the probe Pod of the collector line in
# NETWORK_OUTSIDER_NAMESPACE, with no name label, and wait until it is Ready. One
# FAIL line and 1 when it cannot. ${network_outsider} is set before the Pod
# exists, so the trap deletes it whichever step fails.
network_outsider_start() {
  local err_file detail wait_said
  network_outsider="${NETWORK_OUTSIDER_PREFIX}$(date +%s)"
  err_file="$(mktemp)"
  if ! network_pod_spec "${network_outsider}" "${NETWORK_OUTSIDER_NAMESPACE}" "" 2>"${err_file}" |
    kctl -n "${NETWORK_OUTSIDER_NAMESPACE}" create -f - >/dev/null 2>>"${err_file}"; then
    detail="$(clean_lines "$(<"${err_file}")")"
    rm -f "${err_file}"
    fail "network policy: could not start the probe pod ${network_outsider} in ${NETWORK_OUTSIDER_NAMESPACE} (${detail})"
    return 1
  fi
  rm -f "${err_file}"
  # Captured, not sent to a file, as in network_start_pod: a bound's sentence
  # reaches the terminal and the FAIL line.
  if ! wait_said="$(kctl -n "${NETWORK_OUTSIDER_NAMESPACE}" wait --for=condition=Ready "pod/${network_outsider}" \
    --timeout="${NETWORK_POD_READY_TIMEOUT}" 2>&1 >/dev/null)"; then
    show_wait_error "${wait_said}"
    fail "network policy: the probe pod ${network_outsider} in ${NETWORK_OUTSIDER_NAMESPACE} did not become Ready within ${NETWORK_POD_READY_TIMEOUT} ($(clean_lines "${wait_said}"))"
    return 1
  fi
}

# check_network_collector: the fifth line of check 8 (S063). A pod outside
# `meridian` and `observability` cannot push to the collector: the probe Pod in
# NETWORK_OUTSIDER_NAMESPACE must time out on the collector's HTTP port, which
# only the pods of `meridian` are admitted to. SKIP when the collector's
# Deployment is not there. The control of the probe is check 8's own (the caller
# returns before this when it failed). A timeout alone cannot tell a policy that
# blocks from a collector that is up but hangs, so the line also needs check 4's
# push from `meridian` (${telemetry_pushed}, set by check_telemetry): when that
# did not pass in this run, a timeout from `default` is a FAIL that proves
# nothing, not a PASS. The probe still runs then, so an answer from the
# collector is told as ever.
check_network_collector() {
  local found unproven=""
  network_sweep_leftovers "${NETWORK_OUTSIDER_NAMESPACE}"
  if ! found="$(kctl -n observability get deployment otel-collector -o name --ignore-not-found)"; then
    fail "network policy: could not look for deployment/otel-collector in observability (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "network policy: the collector is not deployed (make up), so no push to it was tried from outside meridian"
    return
  fi
  # shellcheck disable=SC2154  # telemetry_pushed is set by check_telemetry (04-telemetry.sh)
  if [[ "${telemetry_pushed}" != yes ]]; then
    unproven="a probe in ${NETWORK_OUTSIDER_NAMESPACE} timed out on the collector (${COLLECTOR_ENDPOINT}), but check 4's push from meridian did not pass in this run: the collector was not reached from meridian either, so a timeout from ${NETWORK_OUTSIDER_NAMESPACE} shows nothing, and the line proves nothing (a collector that is up but hangs times out for every pod; fix check 4's lines first)"
  fi
  if network_outsider_start; then
    network_expect blocked "${network_outsider}" "${COLLECTOR_ENDPOINT}" \
      "a pod outside meridian and observability (a probe in ${NETWORK_OUTSIDER_NAMESPACE}) cannot push to the collector (${COLLECTOR_ENDPOINT}), which only the pods of meridian may reach, and a pod of meridian did reach it in this run (check 4's push)" \
      "a pod in ${NETWORK_OUTSIDER_NAMESPACE} reached the collector (${COLLECTOR_ENDPOINT}): its ingress admits more than the pods of meridian, or is missing (make up applies manifests/observability-networkpolicy.yaml), or the cluster does not enforce it" \
      "${NETWORK_OUTSIDER_NAMESPACE}" "${unproven}" ||
      true
  fi
  network_outsider_delete || true # the Pod stays named: the EXIT trap tries again
}

# network_rate_store_lines: the rate store's line, from the probe Pod. With the
# sweep's name label and kind's egress rule to the store (the policy the caller
# looked for) it must time out, which only the store's ingress rule can cause;
# given the Model Gateway's name label, which the gateway's egress rule and the
# store's ingress rule admit, the same Pod must reach the port, for up to
# NETWORK_LABEL_ATTEMPTS tries. The Pod has no readiness probe, so with the
# gateway's name it is an endpoint of the model-gateway Service: it is labelled
# back to the sweep's name as soon as the control has run. One PASS or FAIL line.
network_rate_store_lines() {
  local gateway_label=app.kubernetes.io/name=model-gateway attempt
  network_probe "${network_pod}" "${NETWORK_RATE_STORE}"
  if [[ "${network_answer}" == reached ]]; then
    fail "network policy: a pod that is not the Model Gateway's reached the rate store (${NETWORK_RATE_STORE}): its ingress admits more than the Model Gateway's pods, or is missing (the chart's rate-store policy), or the cluster does not enforce it, and a pod that can connect can read or reset every tenant's window"
    return
  elif [[ "${network_answer}" != blocked ]]; then
    fail "network policy: the probe in ${network_pod} to ${NETWORK_RATE_STORE} gave no answer of reached or blocked: ${network_answer}"
    return
  fi
  if ! kctl -n meridian label pod "${network_pod}" "${gateway_label}" --overwrite >/dev/null 2>&1; then
    fail "network policy: could not give the probe pod ${network_pod} the label ${gateway_label}"
    return
  fi
  for ((attempt = 1; attempt <= NETWORK_LABEL_ATTEMPTS; attempt++)); do
    network_probe "${network_pod}" "${NETWORK_RATE_STORE}"
    [[ "${network_answer}" == blocked ]] || break
    ((attempt == NETWORK_LABEL_ATTEMPTS)) || sleep "${NETWORK_LABEL_INTERVAL}"
  done
  kctl -n meridian label pod "${network_pod}" "app.kubernetes.io/name=${NETWORK_POD_NAME_LABEL}" --overwrite >/dev/null 2>&1 || true
  if [[ "${network_answer}" == reached ]]; then
    pass "network policy: a pod that is not the Model Gateway's cannot reach the rate store (${NETWORK_RATE_STORE}) though its egress is open to it, which only the store's ingress rule can cause, and with the Model Gateway's name label the same pod can"
  elif [[ "${network_answer}" == blocked ]]; then
    fail "network policy: the same pod still cannot reach the rate store (${NETWORK_RATE_STORE}) after the label ${gateway_label} was added, in ${NETWORK_LABEL_ATTEMPTS} tries: the Model Gateway's egress rule or the store's ingress rule is too narrow, and the control did not reach, so the line proves nothing"
  else
    fail "network policy: the probe in ${network_pod} to ${NETWORK_RATE_STORE} gave no answer of reached or blocked: ${network_answer}"
  fi
}

# check_network_rate_store: the sixth line of check 8 (S066), which proves the
# store's INGRESS rule: a probe Pod that kind's policy lets send to the store must
# time out, and with the gateway's name label reach it. Without that policy a
# timeout would be the sender's egress and the line would pass with the store open,
# so a missing policy is a FAIL before any Pod starts, and so is one that exists
# but does not give the probe pod egress to the store's pods on TCP 6379 (read
# from its JSON: the selector, the policy type, the peer and the port). The probe is check 8's own
# and its control, the caller's, has passed before this runs. The header says what
# the line does not prove.
check_network_rate_store() {
  local found
  if ! found="$(kctl -n meridian get networkpolicy "${NETWORK_RATE_STORE_POLICY}" -o json --ignore-not-found)"; then
    fail "network policy: could not look for networkpolicy/${NETWORK_RATE_STORE_POLICY} in meridian (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    fail "network policy: networkpolicy/${NETWORK_RATE_STORE_POLICY} does not exist in meridian: it gives the probe pod its egress to the rate store, so without it the pod has no egress to the rate store, a timeout would be the sender's and the line would pass with the store open (make up applies infra/kind/manifests/smoke-rate-store-networkpolicy.yaml)"
    return
  fi
  # That it exists is not enough: a policy that selects another pod, names another
  # peer, port or protocol, or does not list Egress leaves the probe pod without a
  # path to the store, so the first attempt would time out at the sender, the
  # control would reach through the gateway's own egress rule, and the line would
  # PASS with the store's ingress unproven. The shape the line needs, from the
  # manifest's own: exactly the probe pods selected by their label, Egress listed,
  # and a rule whose peers include the store's pods of this namespace (a pod
  # selector of that one label and neither a namespace selector nor an address
  # block) on TCP 6379 (no ports at all is every port, and the API server may
  # leave the protocol out for TCP).
  if ! jq -e --arg smoke_key "${NETWORK_POD_SMOKE_LABEL%%=*}" --arg smoke_value "${NETWORK_POD_SMOKE_LABEL#*=}" \
    --arg store_key "${NETWORK_RATE_STORE_LABEL%%=*}" --arg store_value "${NETWORK_RATE_STORE_LABEL#*=}" \
    --argjson port "${NETWORK_RATE_STORE##*:}" '
    (.spec.podSelector == {matchLabels: {($smoke_key): $smoke_value}})
    and ((.spec.policyTypes // []) | index("Egress") != null)
    and ([.spec.egress[]? | select(
      ([.to[]? | select(
        .podSelector == {matchLabels: {($store_key): $store_value}}
        and (has("namespaceSelector") | not) and (has("ipBlock") | not))] | length) > 0
      and (((.ports // []) | length) == 0
        or ([.ports[] | select((.protocol // "TCP") == "TCP" and .port == $port)] | length) > 0)
    )] | length) > 0' >/dev/null 2>&1 <<<"${found}"; then
    fail "network policy: networkpolicy/${NETWORK_RATE_STORE_POLICY} exists but does not give the probe pod (the label ${NETWORK_POD_SMOKE_LABEL}) egress to the rate store's pods (${NETWORK_RATE_STORE_LABEL}) on TCP ${NETWORK_RATE_STORE##*:}: with another selector, peer, port, protocol or policy type the pod's packets are dropped at the sender, a timeout would not be the store's ingress rule, the control would still reach through the gateway's own egress rule, and the line would pass with the store's ingress unproven (delete the policy and run make up: it applies infra/kind/manifests/smoke-rate-store-networkpolicy.yaml)"
    return
  fi
  if network_start_pod; then
    network_rate_store_lines
  fi
  network_delete_pod || true # the Pod stays named: the EXIT trap tries again
}

# check_network_service_policies FOUND: one FAIL line naming each service of
# FOUND (the lines of deployed_services, "deployment.apps/NAME") whose
# NetworkPolicy object of the same name is not in meridian, from one listing of
# the namespace's policies. Prints nothing when every service has its own. It
# reads objects only: the probes below prove what the policies do, for the
# Claims API's pod, and a service whose policy is missing is covered by
# default-deny alone, which no probe from the Claims API would show.
check_network_service_policies() {
  local policies service policy has missing=""
  if ! policies="$(kctl -n meridian get networkpolicy -o name)"; then
    fail "network policy: could not list the NetworkPolicies in meridian (kubectl's error is above)"
    return
  fi
  while IFS= read -r service; do
    [[ -n "${service}" ]] || continue
    service="${service##*/}"
    # The name part of "kind/name" is compared as text, never as a pattern.
    has=""
    while IFS= read -r policy; do
      if [[ "${policy}" == */* && "${policy#*/}" == "${service}" ]]; then
        has=1
        break
      fi
    done <<<"${policies}"
    [[ -n "${has}" ]] || missing+="${missing:+, }${service}"
  done <<<"$1"
  [[ -z "${missing}" ]] ||
    fail "network policy: no networkpolicy object for $(clean_lines "${missing}") in meridian, whose Deployment exists: the pods of a service without its own policy have no rule at all, so default-deny leaves them with no path in or out (make deploy applies the chart's policies)"
}

check_network_policy() {
  local found policy
  network_sweep_leftovers
  if ! found="$(deployed_services)"; then
    fail "network policy: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "network policy: the Meridian services are not deployed (make deploy)"
    return
  fi
  if ! grep -qx '[^/]*/claims-api' <<<"${found}"; then
    fail "network policy: deployment/claims-api is not deployed, and the probes run in its pod"
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
  check_network_service_policies "${found}"
  # The control first: when the probe cannot reach what a policy allows, a
  # "blocked" below would mean nothing, so none is printed.
  network_expect reached deploy/claims-api "${NETWORK_RUNTIME}" \
    "the probe reaches what a policy allows: the Claims API to the Agent Runtime (${NETWORK_RUNTIME})" \
    "the Claims API cannot reach the Agent Runtime (${NETWORK_RUNTIME}), which its policy allows: the probe or a policy is broken, so a blocked path would prove nothing" ||
    return 0
  network_expect blocked deploy/claims-api "${NETWORK_GATEWAY}" \
    "the Claims API cannot reach the Model Gateway (${NETWORK_GATEWAY}), which no rule allows" \
    "the Claims API reached the Model Gateway (${NETWORK_GATEWAY}), which no rule allows: the cluster does not enforce NetworkPolicy, or a rule is too wide" ||
    true
  network_expect blocked deploy/claims-api "${NETWORK_API_SERVER}" \
    "the Claims API cannot reach the API server's Service (${NETWORK_API_SERVER}), which no rule of its policy lists" \
    "the Claims API reached the API server's Service (${NETWORK_API_SERVER}), which no rule of its policy lists: the cluster does not enforce egress rules, or one is too wide" ||
    true
  check_network_database
  check_network_collector
  check_network_rate_store
}
