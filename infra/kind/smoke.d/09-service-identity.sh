# shellcheck shell=bash
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
#                 runtime as the calling service, recorded at or after this
#                 run's start, which is the database's clock read just before
#                 the 403's request. The gateway writes it in a worker thread,
#                 at most one per reason, tenant and minute, so the check asks
#                 for a row that exists, never for a count that went up, and
#                 tries for about ten seconds; without it a 403 from the
#                 gateway's own policy would pass for the wrong reason. A
#                 second run inside that minute causes no row of its own: when
#                 the newest row is from the minute before this run started and
#                 none came after it, the line is a SKIP, not a PASS ("the
#                 gateway wrote this minute's refusal row for an earlier run;
#                 run again in a minute"), and with no row at all it is a FAIL.
#                 What the row proves is a refusal by the identity rule, for
#                 that reason, caller and tenant, at or after the mark; not
#                 that it is this run's own 403: two runs that overlap can
#                 share one row (another run's 403, written after this run's
#                 mark while this run's own is throttled, passes this line),
#                 so the PASS says "recorded at or after this run's mark".
#                 The fifth presents a certificate of another
#                 CA: the probe makes a throwaway key and a self-signed
#                 certificate with the runtime's own URI (the right name, the
#                 wrong CA), and the gateway must end the connection before any
#                 answer, with the TLS alert for an unknown CA (`refused`,
#                 ssl.SSLError with reason TLSV1_ALERT_UNKNOWN_CA) or with no
#                 alert at all (`reset`: the connection ended before any
#                 request was sent), worded apart; a status is a FAIL, and so
#                 is any other TLS error, and so is a connection that ended
#                 after the request went out (`closed-after-request`: the
#                 gateway took the request and closed without answering, which
#                 is also what a gateway that accepted the certificate does).
#                 The tools check above is a
#                 further proof: its calls run over TLS with the runtime's
#                 certificate. The two refusals (the 401 and the 403) leave two
#                 refusal rows in the audit table on each run, one per reason
#                 (the gateway throttles its refusal rows to one per reason and
#                 minute). Skipped while the Meridian services are not deployed
#                 (`make deploy`). A traceback is a failure, not a refusal.
#                 What the fifth line does not prove: uvicorn, which the
#                 services run under, ends an unknown CA's connection without
#                 delivering the alert (against the test server with its flags:
#                 a reset under TLS 1.3, an EOF under 1.2), so `reset` is the
#                 answer expected of the gateway, and it is the answer the
#                 cluster gave on every run of 2026-10-06 (`refused`, the
#                 alert, was never seen there; the EOF under 1.2 is the test
#                 server's only). It is wider than a refusal for the unknown
#                 CA: a gateway
#                 that died in that second would end the connection the same
#                 way (the three requests before it were answered by the same
#                 gateway). Only `refused`, the alert, names the unknown CA.
#                 The price of `closed-after-request`: the probe reads for one
#                 second before it sends, so a gateway slower than that to end
#                 the connection turns a refusal into this FAIL; it fails
#                 closed, and a run again tells.

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
#                   or passed as an argument. It reads before it writes (under
#                   TLS 1.3 the alert follows the handshake, and a request sent
#                   first can lose it to a reset; under 1.2 it comes inside the
#                   handshake, so the connection is opened inside the same
#                   try), and prints one of four answers, in this mode only:
#                   "refused", when the server sent the TLS alert for an
#                   unknown CA (ssl.SSLError, reason TLSV1_ALERT_UNKNOWN_CA);
#                   "reset", when the connection ended with no TLS alert
#                   before any request was sent (a reset, a broken pipe, an
#                   abort, or an EOF in violation of protocol:
#                   ssl.SSLEOFError); "closed-after-request", when it ended
#                   the same way after the request went out, which is what a
#                   server that accepted the certificate and then closed
#                   without answering does (http.client.RemoteDisconnected is
#                   a ConnectionResetError), so it is a FAIL, not a refusal;
#                   and the status when an answer came, which is a FAIL. The
#                   price: the one-second read is all the server has to end
#                   the connection, so a gateway slower than that turns a
#                   refusal into `closed-after-request`; it fails closed.
#                   Measured against the test server
#                   that has the services' uvicorn flags, the alert never
#                   arrives: a reset under TLS 1.3, an EOF under 1.2, so `reset`
#                   is the answer expected of the gateway (the cluster gave it
#                   on every run of 2026-10-06; `refused` was never seen
#                   there), and the check passes it, worded apart from
#                   `refused`. Anything else (another alert,
#                   the server's certificate not verifying, a name that does
#                   not resolve, a refused connection, a timeout) stays a
#                   traceback, in every mode.
# The audit line's constants (the row of the 403 above): the gateway's service
# name, the reason the identity rule writes for a name the caller may not use
# (a test keeps it equal to NAME_REFUSAL_REASON), the calling service, which is
# the deployment the probe runs in, the database's clock (read before the 403's
# request: a row recorded at or after it counts for this run), how far before it to
# look for the row of an earlier run (the gateway's REFUSAL_AUDIT_SECONDS: a
# test keeps it equal) and how long to wait for this run's own.
readonly IDENTITY_HOST=model-gateway.meridian.svc
readonly IDENTITY_PORT=8000
readonly IDENTITY_FOREIGN_TENANT=evaluation
readonly IDENTITY_CALLER=agent-runtime
readonly IDENTITY_GATEWAY_SERVICE=model-gateway
readonly IDENTITY_AUDIT_REASON=caller-name-not-allowed
readonly IDENTITY_CLOCK_SQL='SELECT extract(epoch FROM now())'
readonly IDENTITY_AUDIT_THROTTLE=60
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
    sent = False
    try:
        connection.connect()
        if mode == "foreign-ca":
            connection.sock.settimeout(1)
            try:
                if not connection.sock.recv(1):
                    return "reset"
            except TimeoutError:
                connection.sock.settimeout(10)
        sent = True
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
    except ssl.SSLError as error:
        if mode == "foreign-ca" and "TLSV1_ALERT_UNKNOWN_CA" in (error.reason or ""):
            return "refused"
        if mode == "foreign-ca" and isinstance(error, ssl.SSLEOFError):
            return "closed-after-request" if sent else "reset"
        raise
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        if mode != "foreign-ca":
            raise
        return "closed-after-request" if sent else "reset"
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

identity_answer="" # set by identity_status
identity_primary="" # set by identity_mark_start: the primary pod of platform-db
identity_mark=""    # set by identity_mark_start: the database's clock before the 403
identity_mark_problem="" # set by identity_mark_start: why there is no mark

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

# expect_foreign_ca: the fifth line. The probe's answer for a certificate of
# another CA is "refused" (the TLS alert for an unknown CA) or "reset" (the
# connection ended with no alert before any request was sent, which is how
# uvicorn, the services' server, ends it); both pass, in words that tell them
# apart. "closed-after-request" (the connection ended after the request went
# out), a status or an error is a FAIL.
expect_foreign_ca() {
  local what="POST /v1/chat with a certificate of the Agent Runtime's own name from another CA"
  identity_status foreign-ca
  case "${identity_answer}" in
    refused) pass "service identity: ${what} -> refused (the TLS alert for an unknown CA)" ;;
    reset) pass "service identity: ${what} -> reset (the connection ended with no TLS alert, before any request was sent: uvicorn ends an unknown CA's connection so, and a gateway that died in that second would too)" ;;
    closed-after-request) fail "service identity: ${what}: the gateway took the request of a certificate from another CA and closed without answering: it may have accepted the certificate" ;;
    *) fail "service identity: ${what}: expected refused or reset, got ${identity_answer}" ;;
  esac
}

# identity_mark_start: before the request that causes the audit row below: the
# database's primary pod and its clock, in ${identity_primary} and
# ${identity_mark} (seconds since the epoch, with the fraction). A row recorded
# at or after the mark counts for this run. When either cannot be had, ${identity_mark}
# stays empty and ${identity_mark_problem} says why: the probes still run, and
# check_gateway_refusal_row prints the FAIL. The mark goes into SQL later, so it
# is kept only when it is a number.
identity_mark_start() {
  local err_file answer detail
  identity_primary=""
  identity_mark=""
  identity_mark_problem=""
  err_file="$(mktemp)"
  if ! identity_primary="$(kctl -n meridian get pod \
    -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary \
    -o jsonpath='{.items[0].metadata.name}' 2>"${err_file}")" || [[ -z "${identity_primary}" ]]; then
    detail="$(clean_lines "$(<"${err_file}")")"
    identity_primary=""
    identity_mark_problem="no primary pod found for platform-db to read the audit row of the 403${detail:+ (kubectl said: ${detail})}"
  elif ! answer="$(kctl -n meridian exec "${identity_primary}" -c postgres -- \
    env "PGOPTIONS=${PSQL_OPTIONS}" psql -d meridian -tAc "${IDENTITY_CLOCK_SQL}" 2>"${err_file}")"; then
    detail="$(clean_lines "$(<"${err_file}")")"
    identity_mark_problem="could not read the database's clock in ${identity_primary} before the probe${detail:+ (kubectl said: ${detail})}"
  elif ! [[ "$(clean_lines "${answer}")" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    identity_mark_problem="the database's clock in ${identity_primary} was not a number of seconds"
  else
    identity_mark="$(clean_lines "${answer}")"
  fi
  rm -f "${err_file}"
}

# check_gateway_refusal_row: the audit row of the 403 above, read in the
# database's primary pod the way check_cost_series reads the ledger. The reason
# the identity rule writes for a caller that names a tenant it may not
# (NAME_REFUSAL_REASON), the gateway as the service that wrote it (`service`),
# the calling service in `reference` (refuse_name in the gateway) and the tenant
# the probe named; `recorded_at` is the database's own clock (a timestamptz its
# insert trigger sets), so the age is the database's, not this laptop's. The
# gateway writes in a worker thread and at most one such row per reason, tenant
# and minute, so a second run inside that minute causes no row of its own. The
# query therefore asks for the newest row since ${identity_mark} less one
# throttle window, and says whether it was recorded at or after the mark (PASS,
# in those words: another run's 403 written after the mark is such a row too) or
# before it (the gateway wrote this minute's row for an earlier run: SKIP, not
# PASS); no row at all is a FAIL. It never asks for a count that
# went up, and runs again for about ten seconds while it finds no row of this
# run's, because the gateway writes in a worker thread. Every value in the SQL
# is a constant of this script, or the mark, which identity_mark_start kept
# only because it is a number; none came from a pod unchecked.
check_gateway_refusal_row() {
  local err_file detail answer="" attempt what
  what="a refusal for ${IDENTITY_AUDIT_REASON} by ${IDENTITY_CALLER} (tenant ${IDENTITY_FOREIGN_TENANT})"
  if [[ -z "${identity_mark}" ]]; then
    fail "service identity: ${identity_mark_problem}"
    return
  fi
  err_file="$(mktemp)"
  for ((attempt = 1; attempt <= IDENTITY_AUDIT_ATTEMPTS; attempt++)); do
    if ! answer="$(kctl -n meridian exec "${identity_primary}" -c postgres -- \
      env "PGOPTIONS=${PSQL_OPTIONS}" psql -d meridian -tAc "SELECT floor(extract(epoch FROM now() - recorded_at))::bigint, extract(epoch FROM recorded_at) >= ${identity_mark} FROM audit.events WHERE service = '${IDENTITY_GATEWAY_SERVICE}' AND event = 'model.call' AND outcome = 'refused' AND reason = '${IDENTITY_AUDIT_REASON}' AND reference = '${IDENTITY_CALLER}' AND tenant = '${IDENTITY_FOREIGN_TENANT}' AND recorded_at > to_timestamp(${identity_mark}) - interval '${IDENTITY_AUDIT_THROTTLE} seconds' ORDER BY recorded_at DESC LIMIT 1" \
      2>"${err_file}")"; then
      detail="$(clean_lines "$(<"${err_file}")")"
      rm -f "${err_file}"
      fail "service identity: could not read audit.events in ${identity_primary}${detail:+ (kubectl said: ${detail})}"
      return
    fi
    answer="$(clean_lines "${answer}")"
    if [[ -n "${answer}" && "${answer}" != *"|f" ]]; then break; fi
    ((attempt == IDENTITY_AUDIT_ATTEMPTS)) || sleep "${IDENTITY_AUDIT_INTERVAL}"
  done
  rm -f "${err_file}"
  if [[ -z "${answer}" ]]; then
    fail "service identity: the gateway's audit log has no row for ${what} since this run started or in the ${IDENTITY_AUDIT_THROTTLE} s before it, after ${IDENTITY_AUDIT_ATTEMPTS} tries: the 403 was not recorded, or was not the identity rule's"
  elif [[ "${answer}" =~ ^([0-9]+)\|t$ ]]; then
    pass "service identity: the gateway's audit log has ${what}, ${BASH_REMATCH[1]} s old, recorded at or after this run's mark"
  elif [[ "${answer}" =~ ^([0-9]+)\|f$ ]]; then
    skip "service identity: the gateway wrote this minute's refusal row for an earlier run (the newest row for ${what} is ${BASH_REMATCH[1]} s old, and none came after this run started), so this run's 403 left none of its own; run again in a minute"
  else
    fail "service identity: the audit query's answer was not a number of seconds and a flag"
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
  identity_mark_start # the database's clock, before the request that causes the row
  expect_identity_status foreign-tenant 403 "POST /v1/chat with the Agent Runtime's certificate, naming a tenant it may not name"
  check_gateway_refusal_row
  expect_foreign_ca
}
