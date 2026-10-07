# shellcheck shell=bash
#  12. telemetry stores: five lines (S072, contract M3b), run after the other
#                 eleven. They prove, from a Pod that is inside the namespace as
#                 the collector is, what the gateway and Tempo's receiver refuse,
#                 and that the pods SERVE the certificate that is in their Secret
#                 now. The check starts one probe Pod in `observability`, of the
#                 Claims API's own image and securityContext (nothing is pulled;
#                 it has no curl, so Python does the work), labelled
#                 app.kubernetes.io/name=opentelemetry-collector, which the
#                 gateway's ingress and the collector's egress name, and
#                 meridian-smoke=telemetry-probe, which kind's policy
#                 smoke-telemetry-probe (manifests/observability-loki-
#                 networkpolicy.yaml) gives an egress rule to Loki's port. It
#                 says so itself: the probe is a LABEL IMPERSONATION of the
#                 collector, which the repository's stance on NetworkPolicy
#                 labels allows (T-84, "said, not solved"); what the lines prove
#                 is the gateway's and the receiver's own checks, not that no pod
#                 may carry the label. The Pod holds no client certificate, ever.
#                 - the gateway (the first line): a POST of /loki/api/v1/push to
#                   loki-gateway.observability.svc.cluster.local:8443 with no
#                   client certificate is answered 403, and a GET of
#                   /loki/api/v1/labels is answered 200. The server's
#                   certificate is not verified by this line (the Pod has no
#                   copy of the authority, and the line is about the refusal);
#                   the fourth line is the one that says WHICH certificate the
#                   gateway serves. A 400 (nginx's own answer to a certificate
#                   that fails) or a connection that ends is a FAIL, not a
#                   refusal.
#                 - Tempo's receiver (the second line): a TLS 1.3 connection to
#                   tempo.observability.svc.cluster.local:4317 with no client
#                   certificate ends in the alert "certificate required"
#                   (TLSV13_ALERT_CERTIFICATE_REQUIRED in Python's words), the
#                   one run R14 saw from the node. A connection that stays open
#                   is a FAIL: the receiver would take a sender with no
#                   certificate.
#                 - Loki's own port (the third line): the same Pod's TCP
#                   connection to loki.observability.svc.cluster.local:3100 must
#                   time out. Its egress is open (smoke-telemetry-probe), so the
#                   timeout is Loki's ingress rule, which admits the gateway's
#                   pods alone, not the sender's. The control: the same Pod
#                   relabelled with the gateway's three labels must reach the
#                   port (up to four tries: the network plugin takes a moment),
#                   and is relabelled back at once (while it carries them it is
#                   an endpoint of the gateway's Service). A refusal, a name
#                   that does not resolve and a failed exec are FAIL lines, never
#                   "blocked"; a control that did not reach is a FAIL.
#                 - the certificates served (the fourth and fifth lines): the
#                   Pod opens a TLS connection to the gateway (8443) and to
#                   Tempo's receiver (4317), reads the SHA-256 of the DER
#                   certificate each serves, and the line compares it with the
#                   SHA-256 of the certificate in the Secret loki-gateway-tls
#                   and tempo-receiver-tls (tls.crt, the first certificate of
#                   the file; never tls.key). It is the one check that finds a
#                   pod that serves a file it loaded before a renewal: nginx
#                   re-reads the gateway's certificate at each handshake, and
#                   Tempo does not, so after a renewal the second pair differs
#                   until `make up` has rolled the pod. A difference is a FAIL
#                   that says which pod and what to do (make up).
#                 Skipped, one line instead of five, while no Meridian Deployment
#                 exists (the probe Pod borrows the Claims API's image: `make
#                 deploy`). A FAIL line that a missing gateway Deployment or
#                 Tempo's StatefulSet gives says make up. A probe Pod that a lost
#                 trap left stays as a Failed object once its five minutes have
#                 passed; the check starts by deleting by name those older than
#                 300 s. The Pod is deleted when the check ends and by the EXIT
#                 trap (cleanup). What the lines do not prove: that the
#                 collector's certificate IS accepted (check 4 shows that logs
#                 and traces arrive through both), that a certificate of another
#                 CA or with another subject is refused (nginx's 400 and the
#                 subject check were seen in a container of the pinned image;
#                 not on the cluster), that a pod outside the namespace is
#                 refused (the policies do that, and check 8 shows it for the
#                 collector's port), and a renewal end to end. Adds a Pod's
#                 start, one timeout of 4 s and the exec calls: about 20 s.

readonly TELEMETRY_STORES_NAMESPACE=observability
readonly TELEMETRY_STORES_GATEWAY=loki-gateway.observability.svc.cluster.local:8443
readonly TELEMETRY_STORES_TEMPO=tempo.observability.svc.cluster.local:4317
readonly TELEMETRY_STORES_LOKI=loki.observability.svc.cluster.local:3100
readonly TELEMETRY_STORES_GATEWAY_SECRET=loki-gateway-tls
readonly TELEMETRY_STORES_TEMPO_SECRET=tempo-receiver-tls
# The probe Pod's labels: the collector's name label (the gateway's ingress and
# the collector's egress rules select it) and smoke's own (kind's policy
# smoke-telemetry-probe gives the egress to Loki's port; the next run's sweep finds
# a leftover by it).
readonly TELEMETRY_STORES_NAME_LABEL=opentelemetry-collector
readonly TELEMETRY_STORES_SMOKE_LABEL=meridian-smoke=telemetry-probe
# The three labels of the gateway's pods, which Loki's ingress rule names.
readonly TELEMETRY_STORES_GATEWAY_LABELS='app.kubernetes.io/name=loki app.kubernetes.io/instance=loki app.kubernetes.io/component=gateway'
readonly TELEMETRY_STORES_LEFTOVER_AGE=300
readonly TELEMETRY_STORES_POD_LIFETIME=300
readonly TELEMETRY_STORES_POD_READY_TIMEOUT=60s
readonly TELEMETRY_STORES_CONTROL_ATTEMPTS=4
readonly TELEMETRY_STORES_CONTROL_INTERVAL=1
readonly TELEMETRY_STORES_TIMEOUT=4
# The probes are Python (the image has no curl). Each prints one word or number
# and exits 0; anything else is a traceback and a non-zero exit, which the check
# reports as a failure, never as a refusal. Arguments: host and port, and for
# the request also the method and the path.
readonly TELEMETRY_STORES_REQUEST='import http.client, ssl, sys
host, port, method, path = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
connection = http.client.HTTPSConnection(host, port, context=context, timeout='"${TELEMETRY_STORES_TIMEOUT}"')
connection.request(method, path, body=b"" if method == "POST" else None)
print(connection.getresponse().status)'
readonly TELEMETRY_STORES_FINGERPRINT='import hashlib, socket, ssl, sys
host, port = sys.argv[1], int(sys.argv[2])
context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
with socket.create_connection((host, port), timeout='"${TELEMETRY_STORES_TIMEOUT}"') as plain:
    with context.wrap_socket(plain, server_hostname=host) as tls:
        print(hashlib.sha256(tls.getpeercert(True)).hexdigest())'
readonly TELEMETRY_STORES_ALERT='import socket, ssl, sys
host, port = sys.argv[1], int(sys.argv[2])
context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
context.minimum_version = ssl.TLSVersion.TLSv1_3
with socket.create_connection((host, port), timeout='"${TELEMETRY_STORES_TIMEOUT}"') as plain:
    with context.wrap_socket(plain, server_hostname=host) as tls:
        try:
            tls.settimeout('"${TELEMETRY_STORES_TIMEOUT}"')
            tls.sendall(b"\0")
            data = tls.recv(1)
            print("open" if data else "closed")
        except ssl.SSLError as error:
            print(error.reason)
        except TimeoutError:
            print("open")'
readonly TELEMETRY_STORES_TCP='import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout='"${TELEMETRY_STORES_TIMEOUT}"').close()
    print("reached")
except TimeoutError:
    print("blocked")'
stores_answer="" # set by telemetry_probe; the probe Pod's name is telemetry_probe_pod (shared.sh)

# telemetry_stores_sweep_leftovers: delete by name the Pods with the check's label
# that are older than TELEMETRY_STORES_LEFTOVER_AGE seconds, the leftover of a run
# whose trap was lost. A younger one is another run's. A list that cannot be read
# is not an error.
telemetry_stores_sweep_leftovers() {
  local json names leftover
  json="$(kctl -n "${TELEMETRY_STORES_NAMESPACE}" get pod -l "${TELEMETRY_STORES_SMOKE_LABEL}" \
    -o json 2>/dev/null)" || return 0
  names="$(jq -r --argjson age "${TELEMETRY_STORES_LEFTOVER_AGE}" '
    .items[] | select(now - (.metadata.creationTimestamp | fromdateiso8601) > $age)
    | .metadata.name' <<<"${json}" 2>/dev/null)" || return 0
  while IFS= read -r leftover; do
    [[ -n "${leftover}" ]] || continue
    kctl -n "${TELEMETRY_STORES_NAMESPACE}" delete pod "${leftover}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  done <<<"${names}"
}

# telemetry_stores_delete_pod: delete the probe Pod when one was named, and forget
# it only when kubectl said it is gone, so a delete that failed is tried again by
# the EXIT trap (cleanup). Returns 1 when the delete failed.
telemetry_stores_delete_pod() {
  [[ -n "${telemetry_probe_pod}" ]] || return 0
  kctl -n "${TELEMETRY_STORES_NAMESPACE}" delete pod "${telemetry_probe_pod}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || return 1
  telemetry_probe_pod=""
}

# telemetry_stores_pod_spec NAME: the probe Pod as JSON, from the Claims API's
# Deployment: its image, pull policy and security contexts, a sleep that ends on
# its own, no service-account token, and the two labels of the header.
telemetry_stores_pod_spec() {
  kctl -n meridian get deployment claims-api -o json | jq --arg name "$1" \
    --arg namespace "${TELEMETRY_STORES_NAMESPACE}" --arg label "${TELEMETRY_STORES_NAME_LABEL}" \
    --argjson lifetime "${TELEMETRY_STORES_POD_LIFETIME}" \
    --arg smoke_key "${TELEMETRY_STORES_SMOKE_LABEL%%=*}" \
    --arg smoke_value "${TELEMETRY_STORES_SMOKE_LABEL#*=}" '
    .spec.template.spec as $pod | $pod.containers[0] as $container | {
      apiVersion: "v1", kind: "Pod",
      metadata: {
        name: $name, namespace: $namespace,
        labels: {($smoke_key): $smoke_value, "app.kubernetes.io/name": $label}
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

# telemetry_stores_start_pod: start the probe Pod and wait until it is Ready.
# One FAIL line and 1 when it cannot. ${telemetry_probe_pod} is set before the Pod
# exists, so the trap deletes it whichever step fails.
telemetry_stores_start_pod() {
  local err_file detail wait_said
  telemetry_probe_pod="smoke-telemetry-$(date +%s)"
  err_file="$(mktemp)"
  if ! telemetry_stores_pod_spec "${telemetry_probe_pod}" 2>"${err_file}" |
    kctl -n "${TELEMETRY_STORES_NAMESPACE}" create -f - >/dev/null 2>>"${err_file}"; then
    detail="$(clean_lines "$(<"${err_file}")")"
    rm -f "${err_file}"
    fail "telemetry stores: could not create the probe Pod in ${TELEMETRY_STORES_NAMESPACE} (kubectl said: ${detail})"
    return 1
  fi
  if ! wait_said="$(kctl -n "${TELEMETRY_STORES_NAMESPACE}" wait --for=condition=Ready \
    "pod/${telemetry_probe_pod}" --timeout="${TELEMETRY_STORES_POD_READY_TIMEOUT}" 2>&1 >/dev/null)"; then
    rm -f "${err_file}"
    fail "telemetry stores: the probe Pod ${telemetry_probe_pod} was not Ready in ${TELEMETRY_STORES_POD_READY_TIMEOUT} ($(clean_lines "${wait_said}"))"
    return 1
  fi
  rm -f "${err_file}"
}

# telemetry_probe SNIPPET HOST:PORT [ARG...]: run a Python snippet in the probe
# Pod with HOST and PORT, then the other arguments; its answer in
# ${stores_answer}: what it printed, or "error: ..." with what it wrote on
# stderr when it failed (a traceback is never read as an answer).
telemetry_probe() {
  local snippet=$1 target=$2 err_file
  shift 2
  err_file="$(mktemp)"
  if stores_answer="$(kctl -n "${TELEMETRY_STORES_NAMESPACE}" exec "${telemetry_probe_pod}" -- \
    python -c "${snippet}" "${target%:*}" "${target##*:}" "$@" 2>"${err_file}")"; then
    stores_answer="$(clean_lines "${stores_answer}")"
  else
    stores_answer="error: $(clean_lines "${stores_answer}") $(clean_lines "$(<"${err_file}")")"
  fi
  rm -f "${err_file}"
}

# telemetry_stores_secret_fingerprint SECRET: the SHA-256 of the DER form of the
# first certificate in tls.crt of the Secret SECRET in observability, on stdout;
# nothing and 1 when it cannot be read. Only the public field is read, never
# tls.key, and no more than the digest is printed.
telemetry_stores_secret_fingerprint() {
  local pem
  pem="$(kctl -n "${TELEMETRY_STORES_NAMESPACE}" get secret "$1" \
    -o 'jsonpath={.data.tls\.crt}' 2>/dev/null | base64 -d 2>/dev/null)" || return 1
  [[ "${pem}" == "-----BEGIN CERTIFICATE-----"* ]] || return 1
  printf '%s\n' "${pem}" | openssl x509 -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1
}

# telemetry_stores_gateway_line: the first line. A push without a certificate is
# 403 and a read is 200.
telemetry_stores_gateway_line() {
  local push read
  telemetry_probe "${TELEMETRY_STORES_REQUEST}" "${TELEMETRY_STORES_GATEWAY}" POST /loki/api/v1/push
  push="${stores_answer}"
  telemetry_probe "${TELEMETRY_STORES_REQUEST}" "${TELEMETRY_STORES_GATEWAY}" GET /loki/api/v1/labels
  read="${stores_answer}"
  if [[ "${push}" == 403 && "${read}" == 200 ]]; then
    pass "telemetry stores: a pod with the collector's label and no client certificate gets 403 for a push to Loki's gateway (${TELEMETRY_STORES_GATEWAY}) and 200 for a read: the gateway, not the label, decides who writes"
  elif [[ "${push}" == 403 ]]; then
    fail "telemetry stores: the gateway refused the push with 403, but a read of /loki/api/v1/labels was answered ${read}, not 200 (is Loki ready? kubectl -n observability get pods)"
  else
    fail "telemetry stores: a push to Loki's gateway with no client certificate was answered ${push}, not 403: the gateway would take a write from a pod that holds no certificate (a read gave ${read})"
  fi
}

# telemetry_stores_tempo_line: the second line. No client certificate: the alert.
telemetry_stores_tempo_line() {
  telemetry_probe "${TELEMETRY_STORES_ALERT}" "${TELEMETRY_STORES_TEMPO}"
  case "${stores_answer}" in
    TLSV13_ALERT_CERTIFICATE_REQUIRED)
      pass "telemetry stores: Tempo's receiver (${TELEMETRY_STORES_TEMPO}) ends a TLS 1.3 connection that presents no client certificate with the alert \"certificate required\"" ;;
    open | closed)
      fail "telemetry stores: Tempo's receiver (${TELEMETRY_STORES_TEMPO}) did not refuse a connection with no client certificate (the connection was ${stores_answer}): it would take a trace from a sender that holds none" ;;
    *)
      fail "telemetry stores: the connection to Tempo's receiver (${TELEMETRY_STORES_TEMPO}) without a client certificate did not end in the alert \"certificate required\": ${stores_answer}" ;;
  esac
}

# telemetry_stores_loki_line: the third line. Loki's own port times out for the
# Pod, and the Pod relabelled with the gateway's labels reaches it.
telemetry_stores_loki_line() {
  local blocked control="" attempt err_file labels_kv
  telemetry_probe "${TELEMETRY_STORES_TCP}" "${TELEMETRY_STORES_LOKI}"
  blocked="${stores_answer}"
  if [[ "${blocked}" == reached ]]; then
    fail "telemetry stores: a pod that is not the gateway reached Loki's own port (${TELEMETRY_STORES_LOKI}), which only the gateway's pods may reach: Loki's ingress rule is missing or too wide"
    return
  elif [[ "${blocked}" != blocked ]]; then
    fail "telemetry stores: the probe to Loki's own port (${TELEMETRY_STORES_LOKI}) gave no answer of reached or blocked: ${blocked}"
    return
  fi
  # The control: the same Pod with the gateway's labels. While it carries them it
  # is an endpoint of the gateway's Service, so they are taken off again at once,
  # whatever the probe said.
  err_file="$(mktemp)"
  # shellcheck disable=SC2086  # the labels are separate arguments
  if kctl -n "${TELEMETRY_STORES_NAMESPACE}" label pod "${telemetry_probe_pod}" \
    ${TELEMETRY_STORES_GATEWAY_LABELS} --overwrite >/dev/null 2>"${err_file}"; then
    for ((attempt = 1; attempt <= TELEMETRY_STORES_CONTROL_ATTEMPTS; attempt++)); do
      telemetry_probe "${TELEMETRY_STORES_TCP}" "${TELEMETRY_STORES_LOKI}"
      control="${stores_answer}"
      [[ "${control}" == reached ]] && break
      sleep "${TELEMETRY_STORES_CONTROL_INTERVAL}"
    done
    labels_kv="app.kubernetes.io/name=${TELEMETRY_STORES_NAME_LABEL}"
    kctl -n "${TELEMETRY_STORES_NAMESPACE}" label pod "${telemetry_probe_pod}" \
      "${labels_kv}" app.kubernetes.io/instance- app.kubernetes.io/component- \
      --overwrite >/dev/null 2>>"${err_file}" ||
      echo "smoke: could not take the gateway's labels off ${telemetry_probe_pod}; delete the Pod by hand" >&2
  else
    control="error: could not label the Pod ($(clean_lines "$(<"${err_file}")"))"
  fi
  rm -f "${err_file}"
  if [[ "${control}" == reached ]]; then
    pass "telemetry stores: Loki's own port (${TELEMETRY_STORES_LOKI}) times out for a pod that is not the gateway, which can send to it, and the same pod with the gateway's labels reaches it: Loki's ingress rule is what refuses"
  else
    fail "telemetry stores: the control did not reach Loki's own port with the gateway's labels (${control}), so the timeout shows nothing"
  fi
}

# telemetry_stores_served_line TARGET SECRET WHO RESTART: the fourth and fifth
# lines. The certificate TARGET serves has the fingerprint of the Secret SECRET.
telemetry_stores_served_line() {
  local target=$1 secret=$2 who=$3 restart=$4 served wanted
  telemetry_probe "${TELEMETRY_STORES_FINGERPRINT}" "${target}"
  served="${stores_answer}"
  if ! wanted="$(telemetry_stores_secret_fingerprint "${secret}")" || [[ -z "${wanted}" ]]; then
    fail "telemetry stores: could not read the certificate in the Secret ${secret} in observability (is its Certificate Ready? make up)"
  elif [[ ! "${served}" =~ ^[0-9a-f]{64}$ ]]; then
    fail "telemetry stores: the connection to ${who} (${target}) gave no certificate: ${served}"
  elif [[ "${served}" == "${wanted}" ]]; then
    pass "telemetry stores: ${who} serves the certificate that is in the Secret ${secret} now (SHA-256 ${served:0:16}...)"
  else
    fail "telemetry stores: ${who} serves a certificate that is NOT the one in the Secret ${secret} (served ${served:0:16}..., Secret ${wanted:0:16}...): it loaded a file before a renewal; ${restart}"
  fi
}

# ── 12. telemetry stores ─────────────────────────────────────────────────────
check_telemetry_stores() {
  telemetry_stores_sweep_leftovers
  if [[ -z "$(deployed_services)" ]]; then
    skip "telemetry stores: no Meridian Deployment, so there is no image for the probe Pod to borrow (make deploy)"
    return
  fi
  if ! kctl -n meridian get deployment claims-api >/dev/null 2>&1; then
    fail "telemetry stores: the Claims API's Deployment is not there, and the probe Pod borrows its image (make deploy)"
    return
  fi
  if ! kctl -n "${TELEMETRY_STORES_NAMESPACE}" get deployment loki-gateway >/dev/null 2>&1; then
    fail "telemetry stores: Loki's gateway (deployment/loki-gateway in observability) is not there: make up"
    return
  fi
  telemetry_stores_start_pod || return 0
  telemetry_stores_gateway_line
  telemetry_stores_tempo_line
  telemetry_stores_loki_line
  telemetry_stores_served_line "${TELEMETRY_STORES_GATEWAY}" "${TELEMETRY_STORES_GATEWAY_SECRET}" \
    "Loki's gateway" "run make up, which rolls the pod when its client CA changed, or restart deployment/loki-gateway"
  telemetry_stores_served_line "${TELEMETRY_STORES_TEMPO}" "${TELEMETRY_STORES_TEMPO_SECRET}" \
    "Tempo's receiver" "run make up, which rolls the pod when its certificate changed, or restart statefulset/tempo"
  telemetry_stores_delete_pod || echo "smoke: could not delete the probe Pod ${telemetry_probe_pod} in ${TELEMETRY_STORES_NAMESPACE}; delete it by hand" >&2
}
