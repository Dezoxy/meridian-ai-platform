# shellcheck shell=bash
#   4. telemetry: nine lines (S063, S064, G1). The first two are about TLS and do not need
#                 the Meridian services: the ConfigMap `telemetry-ca` in
#                 `meridian`, which the six services mount to trust the
#                 collector, holds the certificate its authority has now (the
#                 fingerprint of its ca.crt equals that of tls.crt of the Secret
#                 `telemetry-ca` in `observability`; SKIP while the Secret is
#                 not there, FAIL "run make up" when the ConfigMap is missing or
#                 stale: the services read the mounted file at each new
#                 connection and the kubelet refreshes it, so `make up` is the
#                 whole remedy and no restart follows; only the first twelve hex
#                 digits of a fingerprint are
#                 printed, and only that one field of the Secret is read); and a
#                 push in clear text to the collector's port is not accepted: a
#                 Job pod in `meridian` (so one the policies admit, and the
#                 refusal is the TLS listener's, not a NetworkPolicy's) sends
#                 plain HTTP with the database image's bash and the line says
#                 what came back (a 400 from a Go TLS listener, which is what
#                 the collector answered on 2026-10-06, or a connection closed
#                 with no answer, is a PASS; any other status is a FAIL, a 2xx
#                 because the receiver took the push and the rest because
#                 something answered HTTP in clear text, which does not show TLS
#                 (a plain receiver answers 404 or 503 too); a timeout, a
#                 refused connection or silence is a FAIL that proves nothing,
#                 told apart from a refusal by the listener). Then telemetrygen
#                 sends one trace, one log and one metric over OTLP to the
#                 collector, over TLS with the authority's certificate
#                 (--ca-cert; no --otlp-insecure); each is then read back
#                 through Grafana's datasource proxy (Tempo, Loki, Prometheus),
#                 the way an owner would see it. Since S063 these lines prove the TLS path end
#                 to end for telemetrygen (the collector serves TLS only, so a
#                 trace, a log and a metric that arrive came over it), and the
#                 cost series of check 5, which the Model Gateway's own exporter
#                 pushes, proves it for a service (a series that arrives was
#                 sent over TLS and verified against the authority's file: an
#                 exporter that did not trust it would drop its metrics). What
#                 they do not prove: that a service checks the collector's name
#                 and refuses another authority (the exporters' own test,
#                 tests/meridian/common/test_otlp_tls.py, does), that every
#                 service has the file (`make demo`'s trace with a span of each
#                 service does), and that the collector's own hops to Tempo, Loki
#                 and Prometheus are encrypted (they are not). The Jobs run in
#                 `meridian`, not in `observability` (S063), and push over
#                 HTTPS to port 4318, the port and the namespace the six
#                 services push from: the
#                 collector's ingress admits the pods of `meridian` on 4318 and
#                 nothing else (manifests/observability-networkpolicy.yaml), and
#                 smoke's own Jobs get the egress they need from
#                 manifests/smoke-networkpolicy.yaml, which `make up` applies
#                 (the chart's default-deny would cut them off without it). What
#                 each PASS line prints of an answer (the
#                 trace ID, the log line, the series count) goes through
#                 clean_lines and is cut to 120 characters: anyone who can push
#                 a log line to the collector chooses its text.
#                 The last three lines are about the log agent, the DaemonSet in
#                 `logging` that reads the pods' output on the node and sends it
#                 to the collector; they run after the three read-backs. The
#                 seventh line (G1) reads the live DaemonSet, with one
#                 `kubectl get -o json` and no Grafana, and checks the facts of
#                 its pod that a bump of the collector's chart could change
#                 without any test noticing, because no test renders that chart:
#                 the one hostPath volume is /var/log/pods and its mount is
#                 read-only; no host network, host PID or host port; runAsNonRoot;
#                 and no service-account token. PASS says the four facts, each FAIL
#                 names the first that does not hold, and a SKIP says the
#                 DaemonSet is not there. It does not prove the pod runs (that is
#                 the eighth line) or that its security context is complete (the
#                 capabilities and the seccomp profile are the values file's,
#                 tested without a cluster; the user, 10001 with runAsNonRoot,
#                 was seen on kind on 2026-10-06).
#                 The eighth line (S064) is the Claims API's access line, through
#                 the same Grafana forward as the three read-backs. Smoke asks the Claims API,
#                 through the edge the adjuster pages use, for a path that does
#                 not exist and carries this run's marker in the path
#                 (/smoke-<epoch>: a path and not a query, because the access
#                 line keeps no query), expects a 404, and then looks in Loki
#                 for a record of the service claims-api whose path is that
#                 marker and whose status is 404 (the edge's answer), within the
#                 wait the log line above uses. PASS says what
#                 was found (the record's body, cleaned and cut like the other
#                 answers). Three FAILs tell what is wrong apart: the edge did
#                 not answer 404 (what came back, or that it did not answer at
#                 all), Loki answered and has no such line (the agent is not
#                 sending, the services' image does not write the JSON access
#                 line, or the line is not what the query reads), and Loki did
#                 not answer. One SKIP replaces it while the Meridian services
#                 are not deployed (`make deploy`; so it is a SKIP after `make
#                 up` alone) and while the agent's DaemonSet is not there. What
#                 it does not prove: that every service's output arrives (one
#                 service, one line), that a line that is not JSON arrives (a
#                 crash, output before a service set up its logging), and that
#                 the agent's checkpoint survives a restart. The ninth line (G1)
#                 is what the eighth cannot say, that nothing but the services'
#                 output is read: over the last hour Loki holds no stream of a
#                 container named postgres and none whose namespace is not
#                 meridian (two queries; a FAIL says which kind and how many,
#                 never a label), after a control: the Claims API's own stream
#                 by the same two labels must be there, or the line FAILs (the
#                 labels cannot be selected by, so the two empty answers prove
#                 nothing). It runs only after the eighth passed, because
#                 while nothing is shipped an empty answer proves nothing: when
#                 the eighth did not pass, it is a SKIP, and it is one after
#                 `make up` alone. It does not look for a Job's or a smoke pod's
#                 output, which would be in streams of the namespace meridian:
#                 the include list, which a test holds equal to the chart's
#                 services and the sweep, is what keeps them out, and a query
#                 for them is by hand (infra/kind/README.md).

# The namespace of telemetrygen's Jobs (see check 4 and manifests/smoke-
# networkpolicy.yaml): the collector's ingress admits the pods of this one.
readonly TELEMETRYGEN_NAMESPACE=meridian
# The collector serves TLS with a certificate from an authority of its own
# (manifests/telemetry-ca.yaml; S063). `make up` copies the authority's public
# certificate, `tls.crt` of the Secret TELEMETRY_CA_SECRET in `observability`,
# into the ConfigMap TELEMETRY_CA_CONFIGMAP (key ca.crt) in `meridian` (a test
# keeps both names equal to up.sh's). telemetrygen's Job pod mounts that key at
# TELEMETRYGEN_CA_FILE's directory and pushes with --ca-cert.
readonly TELEMETRY_CA_SECRET=telemetry-ca
readonly TELEMETRY_CA_CONFIGMAP=telemetry-ca
readonly TELEMETRY_CA_NAMESPACE=meridian
# How many hex digits of a certificate's fingerprint a line may print.
readonly TELEMETRY_FINGERPRINT_SHOWN=12
readonly TELEMETRYGEN_CA_DIRECTORY=/etc/telemetry-ca
readonly TELEMETRYGEN_CA_FILE="${TELEMETRYGEN_CA_DIRECTORY}/ca.crt"
# The clear-text probe of check 4: a Job named CLEAR_TEXT_JOB_PREFIX and the
# epoch, in TELEMETRYGEN_NAMESPACE (the pods of `meridian` are the ones the
# collector's ingress admits, and the label of smoke's Jobs gets the egress, so
# the policies let the probe through and a refusal is the TLS listener's). The
# pod runs the platform database's own image, which `make up` has put on the
# node, so no image is pulled and the Meridian services need not be deployed.
# CLEAR_TEXT_PROBE runs under bash in it with the collector's host, port and
# CLEAR_TEXT_TIMEOUT seconds as arguments. It first connects with `timeout`
# (a connection that hangs is "timeout"; any other failure is "error: ..." with
# bash's own message), then sends a POST with an empty body in clear text and
# prints the status line the server answers ("answered HTTP/1.0 400 Bad
# Request"), "closed" when the server ends the connection without a word, or
# "no answer" when it says nothing for CLEAR_TEXT_TIMEOUT seconds.
readonly CLEAR_TEXT_JOB_PREFIX=smoke-cleartext-
readonly CLEAR_TEXT_TIMEOUT=4
# shellcheck disable=SC2016 # the script's own variables, expanded by the pod's bash
readonly CLEAR_TEXT_PROBE='host=$1 port=$2 limit=$3
err="$(timeout "${limit}" bash -c ": </dev/tcp/\$0/\$1" "${host}" "${port}" 2>&1)"
case $? in
  0) ;;
  124) echo timeout; exit 0 ;;
  *) echo "error: ${err}"; exit 0 ;;
esac
exec 3<>"/dev/tcp/${host}/${port}"
printf "POST /v1/traces HTTP/1.1\r\nHost: %s:%s\r\nContent-Type: application/x-protobuf\r\nContent-Length: 0\r\nConnection: close\r\n\r\n" "${host}" "${port}" >&3
IFS= read -r -t "${limit}" line <&3
status=$?
if [ -n "${line}" ]; then
  echo "answered $(printf "%s" "${line}" | tr -d "\r")"
elif [ "${status}" -gt 128 ]; then
  echo "no answer"
else
  echo closed
fi'
# What the telemetry check (4) prints of an answer of Tempo, Loki or Prometheus
# is cut to this many characters (see telemetry_answer).
readonly TELEMETRY_ANSWER_LENGTH=120
# The log agent's line (the seventh of check 4, S064): the namespace and the
# DaemonSet of the log agent (the chart names a DaemonSet <fullname>-agent, and
# values/log-agent.yaml sets the fullname; a test keeps them equal), the service
# whose record is looked for (the container's name, which the agent makes the
# service name), and the edge's address for the Claims API, the host the adjuster
# pages are asked on. The marker is a path of this run: /smoke-<epoch>.
readonly LOG_AGENT_NAMESPACE=logging
readonly LOG_AGENT_DAEMONSET=log-agent-agent
readonly LOG_AGENT_SERVICE=claims-api
readonly CLAIMS_EDGE_ORIGIN=http://claims.meridian.localhost:8088
# The log agent's pod shape (G1, the seventh line of check 4): a jq program that
# reads the live DaemonSet and prints one word, the first fact that does not hold
# (hostpath, readonly, hostnetwork, hostpid, hostport, nonroot, token) or nothing
# when all hold. It prints a word of its own and never a value from the object.
# The facts are the header of values/log-agent.yaml's: the one hostPath volume is
# /var/log/pods and every mount of it is read-only; no host network, no host PID,
# no hostPort; every container has runAsNonRoot (its own, or the pod's); and the
# pod mounts no service-account token.
# shellcheck disable=SC2016  # the $variables are jq's, not the shell's
readonly LOG_AGENT_SHAPE_FILTER='
  .spec.template.spec as $pod
  | [($pod.containers // [])[], ($pod.initContainers // [])[]] as $containers
  | [($pod.volumes // [])[] | select(has("hostPath"))] as $host
  | if ($host | map(.hostPath.path)) != ["/var/log/pods"] then "hostpath"
    elif ([$containers[] | (.volumeMounts // [])[] | select(.name == $host[0].name) | .readOnly == true] | (length == 0 or any(not))) then "readonly"
    elif ($pod.hostNetwork // false) then "hostnetwork"
    elif ($pod.hostPID // false) then "hostpid"
    elif ([$containers[] | (.ports // [])[] | .hostPort // empty | select(. != 0)] | length > 0) then "hostport"
    elif ([$containers[] | (if .securityContext.runAsNonRoot != null then .securityContext.runAsNonRoot else $pod.securityContext.runAsNonRoot end) == true] | (length == 0 or any(not))) then "nonroot"
    elif $pod.automountServiceAccountToken != false then "token"
    else "" end'
readonly JOB_TIMEOUT=120s
telemetry_pushed="" # set to yes by check 4 when telemetrygen's push from meridian passed: check 8's control
log_agent_shipped="" # set to yes by check 4 when the Claims API's line was found in Loki: the streams line's control

# ── 4. telemetry ─────────────────────────────────────────────────────────────
# start_job SIGNAL COUNT_FLAG: one Job that sends one item of SIGNAL (traces,
# logs or metrics) for service ${service}. Named uniquely, so reruns never clash.
# The Job runs in ${TELEMETRYGEN_NAMESPACE} (`meridian`) and pushes OTLP over HTTP
# (--otlp-http, which sends each signal to its default path, /v1/traces and so
# on) and TLS: there is no --otlp-insecure, and --ca-cert names the authority's
# certificate, which the pod reads from the ConfigMap ${TELEMETRY_CA_CONFIGMAP}
# of its own namespace (a Job that cannot find it never starts: its pod stays in
# ContainerCreating, and ttlSecondsAfterFinished counts only from a Job that has
# finished or failed). So the Job carries an activeDeadlineSeconds of 150, 30
# more than the 120 s JOB_TIMEOUT that smoke waits for it (a test keeps the two
# apart): a Job that never started fails at its deadline, and the TTL then
# removes it and its pod. The
# collector's ingress admits the pods of that namespace on 4318. The
# Job's pods carry no part-of label, so the database's ingress does not admit
# them, and manifests/smoke-networkpolicy.yaml selects them by the name label.
start_job() {
  local signal=$1 count_flag=$2
  kctl create -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: smoke-${signal}-${epoch}
  namespace: ${TELEMETRYGEN_NAMESPACE}
  labels:
    app.kubernetes.io/name: meridian-smoke
spec:
  ttlSecondsAfterFinished: 900
  activeDeadlineSeconds: 150
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
            - --otlp-http
            - --ca-cert
            - ${TELEMETRYGEN_CA_FILE}
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
          volumeMounts:
            - name: telemetry-ca
              mountPath: ${TELEMETRYGEN_CA_DIRECTORY}
              readOnly: true
      volumes:
        - name: telemetry-ca
          configMap:
            name: ${TELEMETRY_CA_CONFIGMAP}
            items:
              - key: ca.crt
                path: ca.crt
EOF
}

# telemetry_answer TEXT: what a backend returned, as it may be printed: through
# clean_lines, then cut to TELEMETRY_ANSWER_LENGTH characters. Anyone who can
# push a log line to the collector chooses its text, so an escape sequence or a
# line that starts with PASS must not reach the terminal as one.
telemetry_answer() {
  local text
  text="$(clean_lines "$1")"
  printf '%s' "${text:0:TELEMETRY_ANSWER_LENGTH}"
}

# pem_fingerprint PEM: the SHA-256 fingerprint of the first certificate of PEM,
# as upper-case hex without colons; returns 1 when PEM holds no certificate. The
# certificate is public; nothing of PEM but this is kept.
pem_fingerprint() {
  local line
  line="$(openssl x509 -noout -fingerprint -sha256 <<<"$1" 2>/dev/null)" || return 1
  line="${line#*=}"
  printf '%s' "${line//:/}"
}

# check_telemetry_ca: the first line of check 4 (S063). The ConfigMap
# ${TELEMETRY_CA_CONFIGMAP} in `meridian`, which the six services mount to trust
# the collector and telemetrygen's Jobs too, holds the certificate the authority
# has now: the fingerprints of its ca.crt and of tls.crt of the Secret
# ${TELEMETRY_CA_SECRET} in `observability` are equal. Only that one field of
# the Secret is read (a jsonpath, as up.sh does), never the object and never
# tls.key. Prints nothing but the first TELEMETRY_FINGERPRINT_SHOWN hex digits of
# either fingerprint. SKIP while the Secret is not there (nothing of the
# authority exists, `make up`); FAIL when the ConfigMap is missing or differs,
# with the remedy: `make up` publishes the current certificate, and nothing
# follows it. The services mount the ConfigMap as a directory, the kubelet
# refreshes the mounted file, and their exporters read it at each new connection
# (the security review of S063 swapped the file under a live exporter and its
# next new connection used the new one), so they use it within about a minute
# and need no restart.
check_telemetry_ca() {
  local found encoded authority published authority_print published_print
  if ! found="$(kctl -n observability get secret "${TELEMETRY_CA_SECRET}" -o name --ignore-not-found)"; then
    fail "telemetry: could not look for the Secret ${TELEMETRY_CA_SECRET} in observability (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "telemetry: the collector's authority is not there (no Secret ${TELEMETRY_CA_SECRET} in observability: run make up), so the ConfigMap ${TELEMETRY_CA_CONFIGMAP} was not compared with it"
    return
  fi
  if ! encoded="$(kctl -n observability get secret "${TELEMETRY_CA_SECRET}" -o 'jsonpath={.data.tls\.crt}')"; then
    fail "telemetry: could not read tls.crt of the Secret ${TELEMETRY_CA_SECRET} in observability (kubectl's error is above)"
    return
  fi
  if ! published="$(kctl -n "${TELEMETRY_CA_NAMESPACE}" get configmap "${TELEMETRY_CA_CONFIGMAP}" -o 'jsonpath={.data.ca\.crt}' --ignore-not-found)"; then
    fail "telemetry: could not read ca.crt of the ConfigMap ${TELEMETRY_CA_CONFIGMAP} in ${TELEMETRY_CA_NAMESPACE} (kubectl's error is above)"
    return
  fi
  if ! authority="$(base64 -d <<<"${encoded}" 2>/dev/null)" ||
    ! authority_print="$(pem_fingerprint "${authority}")"; then
    fail "telemetry: tls.crt of the Secret ${TELEMETRY_CA_SECRET} in observability is not a certificate: read the Certificate (kubectl -n observability get certificate ${TELEMETRY_CA_SECRET})"
    return
  fi
  if [[ -z "${published}" ]]; then
    fail "telemetry: the ConfigMap ${TELEMETRY_CA_CONFIGMAP} in ${TELEMETRY_CA_NAMESPACE} is missing or has no ca.crt, so the services and telemetrygen cannot verify the collector: run make up"
    return
  fi
  if ! published_print="$(pem_fingerprint "${published}")"; then
    fail "telemetry: ca.crt of the ConfigMap ${TELEMETRY_CA_CONFIGMAP} in ${TELEMETRY_CA_NAMESPACE} is not a certificate: run make up"
    return
  fi
  if [[ "${published_print}" == "${authority_print}" ]]; then
    pass "telemetry: the ConfigMap ${TELEMETRY_CA_CONFIGMAP} in ${TELEMETRY_CA_NAMESPACE} holds the certificate of the collector's authority (fingerprints equal, sha256 ${authority_print:0:TELEMETRY_FINGERPRINT_SHOWN}...)"
  else
    fail "telemetry: the ConfigMap ${TELEMETRY_CA_CONFIGMAP} in ${TELEMETRY_CA_NAMESPACE} holds another certificate (sha256 ${published_print:0:TELEMETRY_FINGERPRINT_SHOWN}...) than the collector's authority has now (sha256 ${authority_print:0:TELEMETRY_FINGERPRINT_SHOWN}...): run make up, which publishes the current one (the kubelet refreshes the mounted file and the services use it for their next connection, within about a minute and without a restart)"
  fi
}

# clear_text_job_spec NAME: the probe Job of check_telemetry_clear_text as JSON.
# The pod carries the label of smoke's Jobs (manifests/smoke-networkpolicy.yaml
# gives it DNS and the collector's port and nothing else), the database image's
# bash runs CLEAR_TEXT_PROBE, and the restrictions are telemetrygen's: no root,
# no privilege escalation, a read-only root filesystem, no capability, no
# service-account token. IfNotPresent, not Always: the image is on the node.
clear_text_job_spec() {
  jq -n --arg name "$1" --arg namespace "${TELEMETRYGEN_NAMESPACE}" \
    --arg image "${POSTGRES_IMAGE}" --arg probe "${CLEAR_TEXT_PROBE}" \
    --arg host "${COLLECTOR_ENDPOINT%:*}" --arg port "${COLLECTOR_ENDPOINT##*:}" \
    --arg limit "${CLEAR_TEXT_TIMEOUT}" '
    {"app.kubernetes.io/name": "meridian-smoke"} as $labels | {
      apiVersion: "batch/v1", kind: "Job",
      metadata: {name: $name, namespace: $namespace, labels: $labels},
      spec: {
        ttlSecondsAfterFinished: 900, backoffLimit: 0, activeDeadlineSeconds: 60,
        template: {
          metadata: {labels: $labels},
          spec: {
            restartPolicy: "Never", automountServiceAccountToken: false,
            securityContext: {runAsNonRoot: true, runAsUser: 10001,
              seccompProfile: {type: "RuntimeDefault"}},
            containers: [{
              name: "probe", image: $image, imagePullPolicy: "IfNotPresent",
              command: ["bash", "-c", $probe, "probe", $host, $port, $limit],
              securityContext: {allowPrivilegeEscalation: false,
                readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}},
              resources: {requests: {cpu: "10m", memory: "32Mi"},
                limits: {memory: "64Mi"}}
            }]
          }
        }
      }
    }'
}

# check_telemetry_clear_text: the second line of check 4 (S063). A push in clear
# text to the collector's port is not accepted. The probe Job (see
# CLEAR_TEXT_PROBE) runs in a pod the policies admit, so what refuses it is the
# TLS listener and not a NetworkPolicy; it sends plain HTTP to ${COLLECTOR_ENDPOINT}
# and the line reports what came back, told apart from a connection that never
# opened. PASS: a 400 (a Go TLS listener answers 400, "Client sent an HTTP
# request to an HTTPS server": seen on kind on 2026-10-06 as `HTTP/1.0 400 Bad
# Request`) or a connection closed with no answer. FAIL: a 2xx (the receiver
# takes clear text); any other status (a plain HTTP receiver answers 404, 503
# and 301 as well, so such an answer does not show TLS); and every answer that
# says nothing about the listener (a timeout, a refused connection, no answer, a
# Job that did not finish), each naming that the line then proves nothing. SKIP
# while the collector is not deployed (`make up`); the Meridian services are not
# needed. What the answer is is cleaned and cut like every answer from a pod.
check_telemetry_clear_text() {
  local found job answer code detail wait_said
  if ! found="$(kctl -n observability get deployment otel-collector -o name --ignore-not-found)"; then
    fail "telemetry: could not look for deployment/otel-collector in observability (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "telemetry: the collector is not deployed (make up), so no clear-text push to it was tried"
    return
  fi
  job="${CLEAR_TEXT_JOB_PREFIX}${epoch}"
  if ! detail="$(clear_text_job_spec "${job}" |
    kctl -n "${TELEMETRYGEN_NAMESPACE}" create -f - 2>&1 >/dev/null)"; then
    fail "telemetry: could not start the clear-text probe ${job} in ${TELEMETRYGEN_NAMESPACE} ($(clean_lines "${detail}")), so the line proves nothing"
    return
  fi
  if ! wait_said="$(kctl -n "${TELEMETRYGEN_NAMESPACE}" wait --for=condition=complete \
    "job/${job}" --timeout="${JOB_TIMEOUT}" 2>&1 >/dev/null)"; then
    show_wait_error "${wait_said}"
    fail "telemetry: the clear-text probe ${job} did not complete (kubectl -n ${TELEMETRYGEN_NAMESPACE} logs job/${job}), so the line proves nothing"
    return
  fi
  if ! answer="$(kctl -n "${TELEMETRYGEN_NAMESPACE}" logs "job/${job}" 2>/dev/null)"; then
    fail "telemetry: could not read the logs of the clear-text probe ${job} (kubectl -n ${TELEMETRYGEN_NAMESPACE} logs job/${job}), so the line proves nothing"
    return
  fi
  answer="$(telemetry_answer "${answer}")"
  read -r _ _ code _ <<<"${answer}"
  if [[ "${answer}" == "answered HTTP/"* && "${code}" =~ ^2[0-9][0-9]$ ]]; then
    fail "telemetry: the collector accepted a push in clear text on ${COLLECTOR_ENDPOINT} (${answer}): its receiver does not serve TLS (make up applies values/otel-collector.yaml)"
  elif [[ "${answer}" == "answered HTTP/"* && "${code}" == 400 ]] || [[ "${answer}" == closed ]]; then
    pass "telemetry: a push in clear text to the collector (${COLLECTOR_ENDPOINT}) is not accepted: a pod the policies admit got \"${answer}\""
  elif [[ "${answer}" == "answered HTTP/"* && "${code}" =~ ^[1-5][0-9][0-9]$ ]]; then
    fail "telemetry: the clear-text push to the collector (${COLLECTOR_ENDPOINT}) was answered with a status other than 400 (${answer}), which does not show TLS: a status other than 400 means something answered HTTP in clear text (make up applies values/otel-collector.yaml)"
  else
    fail "telemetry: the clear-text probe to ${COLLECTOR_ENDPOINT} gave no answer that shows what the listener does (${answer:-nothing}), so the line proves nothing"
  fi
}

# check_telemetry_log_agent_pod: the seventh line of check 4 (G1). The log agent's
# DaemonSet as the cluster holds it, read with one `kubectl get -o json`, has the
# shape values/log-agent.yaml says (the facts are LOG_AGENT_SHAPE_FILTER's), so a
# chart bump that changes a default (a hostPort, a second mount, a preset, a
# user) is seen by `make smoke`, which the Renovate group's note says to run.
# No test renders the collector's chart, so nothing else would see it. PASS says
# the four facts; each FAIL names the first that does not hold (the filter's own
# word, never a value of the object); SKIP while the DaemonSet is not there; a
# read that fails, or an answer that is not JSON, is a FAIL.
check_telemetry_log_agent_pod() {
  local json broken
  if ! json="$(kctl -n "${LOG_AGENT_NAMESPACE}" get daemonset "${LOG_AGENT_DAEMONSET}" -o json --ignore-not-found)"; then
    fail "telemetry: could not read daemonset/${LOG_AGENT_DAEMONSET} in ${LOG_AGENT_NAMESPACE} (kubectl's error is above), so its pod's shape was not checked"
    return
  fi
  if [[ -z "${json}" ]]; then
    skip "telemetry: the log agent is not there (no daemonset/${LOG_AGENT_DAEMONSET} in ${LOG_AGENT_NAMESPACE}: run make up), so its pod's shape was not read"
    return
  fi
  if ! broken="$(jq -r "${LOG_AGENT_SHAPE_FILTER}" <<<"${json}" 2>/dev/null)"; then
    fail "telemetry: what kubectl printed for daemonset/${LOG_AGENT_DAEMONSET} is not a DaemonSet this check can read, so its pod's shape was not checked"
    return
  fi
  case "${broken}" in
    "") pass "telemetry: the log agent's pod is the shape its values say: one hostPath, /var/log/pods, mounted read-only; no host network, PID or port; runAsNonRoot; no service-account token" ;;
    hostpath) fail "telemetry: the log agent's pod is not what its values say: its hostPath volumes are not exactly /var/log/pods (a chart bump added, changed or dropped a volume)" ;;
    readonly) fail "telemetry: the log agent's pod is not what its values say: a mount of the hostPath /var/log/pods is missing or is not read-only" ;;
    hostnetwork) fail "telemetry: the log agent's pod is not what its values say: it uses the host network" ;;
    hostpid) fail "telemetry: the log agent's pod is not what its values say: it uses the host PID namespace" ;;
    hostport) fail "telemetry: the log agent's pod is not what its values say: a container has a host port" ;;
    nonroot) fail "telemetry: the log agent's pod is not what its values say: a container does not have runAsNonRoot" ;;
    token) fail "telemetry: the log agent's pod is not what its values say: it mounts a service-account token" ;;
    *) fail "telemetry: the shape check of daemonset/${LOG_AGENT_DAEMONSET} answered a word it does not know, so the pod's shape was not checked" ;;
  esac
}

# check_telemetry_log_agent: the eighth line of check 4 (S064, the seventh until
# G1 put the pod's shape before it). The log agent
# sent a record of a service's own output to Loki. The Claims API is asked,
# through the edge, for /smoke-${epoch}, a path that does not exist (404), and
# Loki is then asked for a record of the service ${LOG_AGENT_SERVICE} whose
# `path` is that marker: the access line of the request, as the agent ships it
# from the pod's output (the fields other than the message are structured
# metadata in Loki, so the filter reads the field and not the text). The marker
# is in the path, not in a query: the access line keeps no query. SKIP while
# the services are not deployed or the agent's DaemonSet is not there. Needs the
# Grafana forward of check_telemetry (${grafana_url}) and its poll. The three
# FAILs: the edge did not answer 404; Loki answered and has no such line (poll
# keeps the start of its last answer, which for a query that found nothing is a
# `"status":"success"` body); Loki did not answer (a curl error or an answer
# that is not a success). What came back is cleaned and cut by telemetry_answer.
check_telemetry_log_agent() {
  local found marker status
  if ! found="$(deployed_services)"; then
    fail "telemetry: could not look for the Meridian deployments (kubectl's error is above), so no line of theirs was looked for in Loki"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "telemetry: the Meridian services are not deployed (make deploy), so no line of theirs was looked for in Loki"
    return
  fi
  if ! found="$(kctl -n "${LOG_AGENT_NAMESPACE}" get daemonset "${LOG_AGENT_DAEMONSET}" -o name --ignore-not-found)"; then
    fail "telemetry: could not look for daemonset/${LOG_AGENT_DAEMONSET} in ${LOG_AGENT_NAMESPACE} (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "telemetry: the log agent is not there (no daemonset/${LOG_AGENT_DAEMONSET} in ${LOG_AGENT_NAMESPACE}: run make up), so no line of the services was looked for in Loki"
    return
  fi
  marker="/smoke-${epoch}"
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -o /dev/null -w '%{http_code}' "${CLAIMS_EDGE_ORIGIN}${marker}" 2>&1)"; then
    fail "telemetry: the edge did not answer 404 for ${CLAIMS_EDGE_ORIGIN}${marker}: $(telemetry_answer "${status}")"
    return
  fi
  if [[ "${status}" != 404 ]]; then
    fail "telemetry: the edge did not answer 404 for ${CLAIMS_EDGE_ORIGIN}${marker}: it answered $(telemetry_answer "${status}")"
    return
  fi
  # shellcheck disable=SC2154  # grafana_url is set by open_grafana, poll_result and poll_error by poll (shared.sh)
  if poll '.data.result[0].values[0][1] // empty' -G \
    "${grafana_url}/api/datasources/proxy/uid/loki/loki/api/v1/query_range" \
    --data-urlencode "query={service_name=\"${LOG_AGENT_SERVICE}\"} | path=\"${marker}\" | status=\"404\"" \
    --data-urlencode "limit=5"; then
    log_agent_shipped=yes
    pass "telemetry: the log agent shipped the Claims API's access line for ${marker}: Loki has a record of ${LOG_AGENT_SERVICE} with that path and status 404: $(telemetry_answer "${poll_result}")"
  elif [[ "${poll_error}" == *'"status":"success"'* ]]; then
    fail "telemetry: Loki has no line of ${LOG_AGENT_SERVICE} whose path is ${marker} after ${POLL_TIMEOUT}s, though the edge answered 404: the log agent is not sending (kubectl -n ${LOG_AGENT_NAMESPACE} logs daemonset/${LOG_AGENT_DAEMONSET}), or the Claims API does not write its access line as JSON with a path field"
  else
    fail "telemetry: Loki did not answer the question for ${marker} after ${POLL_TIMEOUT}s: $(telemetry_answer "${poll_error}")"
  fi
}

# check_telemetry_log_agent_streams: the ninth line of check 4 (G1). Over the last
# hour Loki holds no stream of a container named postgres (the database's output
# is PostgreSQL's, not the services', and is not redacted) and none whose
# namespace is not meridian (the agent's mount reaches every namespace's output
# and its include list names the services and the sweep): two queries, and the
# first stream found ends the line as a FAIL that says what kind it was and how
# many, never a label from the answer (the labels come from paths and from
# whatever pushes to the collector). Before the two, a control (M1 of the second
# review): {k8s_namespace_name="meridian", k8s_container_name="claims-api"} must
# return a stream in the same window, or the line FAILs (not SKIP: the eighth
# line has just found the Claims API's access line by service_name) because the
# two labels are not there to select by and the two negatives prove nothing; PASS
# says what was found and what was not. The second query leads with a matcher that
# cannot match an empty value, because Loki refuses a selector made only of
# matchers that can (`!=` is one). It runs only after the eighth line passed (
# ${log_agent_shipped}): while nothing is shipped an empty answer proves nothing,
# so it is a SKIP, which is also what it is after `make up` alone. Needs the
# Grafana forward of check_telemetry (${grafana_url}) and its poll. A Loki that
# does not answer, or answers with anything but a success, is a FAIL of its own.
check_telemetry_log_agent_streams() {
  local url="${grafana_url}/api/datasources/proxy/uid/loki/loki/api/v1/query_range"
  local what query kind
  if [[ "${log_agent_shipped}" != yes ]]; then
    skip "telemetry: the Claims API's line above did not pass, so no stream that must not be in Loki was looked for: an empty answer proves nothing while nothing is shipped"
    return
  fi
  # The control: the Claims API's own stream, by the two labels the negatives
  # use. Without it a Loki that stopped indexing either label answers both
  # negatives with nothing, and the line would say the agent ships only its list.
  if ! poll 'select(.status == "success") | .data.result | length | tostring' -G "${url}" \
    --data-urlencode "query={k8s_namespace_name=\"meridian\", k8s_container_name=\"${LOG_AGENT_SERVICE}\"}" \
    --data-urlencode "since=1h" --data-urlencode "limit=5"; then
    fail "telemetry: Loki did not answer the question for the stream of the Claims API's container after ${POLL_TIMEOUT}s: $(telemetry_answer "${poll_error}")"
    return
  fi
  if [[ "${poll_result}" == 0 ]]; then
    fail "telemetry: Loki holds no stream of the Claims API's container (k8s_container_name=${LOG_AGENT_SERVICE} in k8s_namespace_name=meridian) from the last hour, though its access line is there by service_name: the labels k8s_container_name and k8s_namespace_name are not there to select by (a Loki that stopped indexing them), so the questions for streams of postgres and of other namespaces would prove nothing and were not asked"
    return
  fi
  for what in database outside; do
    if [[ "${what}" == database ]]; then
      query='{k8s_container_name="postgres"}'
      kind="a container named postgres"
    else
      query='{k8s_namespace_name=~".+", k8s_namespace_name!="meridian"}'
      kind="any namespace but meridian"
    fi
    if ! poll 'select(.status == "success") | .data.result | length | tostring' -G "${url}" \
      --data-urlencode "query=${query}" --data-urlencode "since=1h" --data-urlencode "limit=5"; then
      fail "telemetry: Loki did not answer the question for streams of ${kind} after ${POLL_TIMEOUT}s: $(telemetry_answer "${poll_error}")"
      return
    fi
    if [[ "${poll_result}" != 0 ]]; then
      fail "telemetry: Loki holds $(telemetry_answer "${poll_result}") stream(s) of ${kind} from the last hour, which the log agent must not ship: its include list (values/log-agent.yaml) names the services and the sweep only"
      return
    fi
  done
  pass "telemetry: Loki holds a stream of the Claims API's container (${LOG_AGENT_SERVICE} in meridian), so its labels select, and holds no stream of a container named postgres and none outside the namespace meridian, all from the last hour: the log agent ships only what its include list names"
}

check_telemetry() {
  epoch="$(date +%s)"
  service="meridian-smoke-${epoch}"
  check_telemetry_ca
  check_telemetry_clear_text
  log "telemetry: sending one trace, log and metric as service ${service}"
  start_job traces --traces
  start_job logs --logs
  start_job metrics --metrics

  local signal wait_said
  for signal in traces logs metrics; do
    if ! wait_said="$(kctl -n "${TELEMETRYGEN_NAMESPACE}" wait --for=condition=complete \
      "job/smoke-${signal}-${epoch}" --timeout="${JOB_TIMEOUT}" 2>&1 >/dev/null)"; then
      show_wait_error "${wait_said}"
      fail "telemetry: telemetrygen ${signal} job did not complete (kubectl -n ${TELEMETRYGEN_NAMESPACE} logs job/smoke-${signal}-${epoch}; a Job with no egress to the collector, or one that cannot find the ConfigMap ${TELEMETRY_CA_CONFIGMAP}, is the likeliest cause on a cluster that make up has not brought up to date: it applies manifests/smoke-networkpolicy.yaml and manifests/observability-networkpolicy.yaml and makes the ConfigMap)"
      return
    fi
  done
  pass "telemetry: telemetrygen sent trace, log and metric to ${COLLECTOR_ENDPOINT}"
  # The control of check 8's collector line: a pod the policies admit reached the
  # collector over TLS in this run. Set before the read-backs, which prove
  # something else (that the stores answered) and can fail on their own.
  # shellcheck disable=SC2034  # read by check_network_collector (check 8), which smoke.sh still holds
  telemetry_pushed=yes

  open_grafana || return 0 # it printed the FAIL line
  local proxy="${grafana_url}/api/datasources/proxy/uid"

  # Traces: THE done-when of S006.
  if poll '.traces[0].traceID // empty' \
    "${proxy}/tempo/api/search?tags=service.name%3D${service}&limit=5"; then
    pass "trace: Tempo has trace $(telemetry_answer "${poll_result}") for ${service}"
  else
    fail "trace: no trace for ${service} in Tempo after ${POLL_TIMEOUT}s"
  fi

  if poll '.data.result[0].values[0][1] // empty' -G \
    "${proxy}/loki/loki/api/v1/query_range" \
    --data-urlencode "query={service_name=\"${service}\"}" --data-urlencode "limit=5"; then
    pass "log: Loki has a line for ${service}: $(telemetry_answer "${poll_result}")"
  else
    fail "log: no line for ${service} in Loki after ${POLL_TIMEOUT}s"
  fi

  if poll '.data.result | select(length > 0) | length | tostring' -G \
    "${proxy}/prometheus/api/v1/query" \
    --data-urlencode "query={__name__=~\"gen.*\",job=\"${service}\"}"; then
    pass "metric: Prometheus has $(telemetry_answer "${poll_result}") series from telemetrygen for ${service}"
  else
    fail "metric: no series for ${service} in Prometheus after ${POLL_TIMEOUT}s"
  fi

  check_telemetry_log_agent_pod
  check_telemetry_log_agent
  check_telemetry_log_agent_streams
}
