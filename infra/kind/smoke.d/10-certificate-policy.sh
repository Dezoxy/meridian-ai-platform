# shellcheck shell=bash
#  10. certificate policy: five lines (S056, S062, S073), run after the first
#                 nine and never skipped: its objects exist after `make up`, so
#                 a missing one is a FAIL. The first three lines read only: the
#                 five CertificateRequestPolicies
#                 (meridian-services, meridian-services-ca,
#                 meridian-deny-unlisted, telemetry-ca, otel-collector; S063)
#                 are Ready; the Deployment
#                 cert-manager-approver-policy in cert-manager has an available
#                 replica; and cert-manager's own approver is off, read two ways
#                 that must agree: the ClusterRole
#                 cert-manager-controller-approve:cert-manager-io, which the
#                 chart renders only with its approver on, does not exist
#                 (kubectl's NotFound is the pass; any other error is a FAIL),
#                 and the controller's arguments hold
#                 --controllers=-certificaterequests-approver. A FAIL says the
#                 issuer may be signing for every request again. Renovate's gate
#                 for the kind platform is `make up` and `make smoke`, and
#                 nothing else makes a certificate request on a cluster that has
#                 its certificates, so a cert-manager or approver-policy update
#                 that turned the approver back on, or left the policies or the
#                 add-on gone, would otherwise pass. The fourth line makes the
#                 one request that the issuer must refuse (S062), the one
#                 change this check makes: a CertificateRequest named
#                 meridian-smoke-refused-<pid>-<random> in the namespace
#                 default, for the issuer meridian-services, with a URI that
#                 policy lists (the Claims API's) and a duration and usages it
#                 allows, so that only its namespace refuses it: the
#                 namespace selector of meridian-services does not list
#                 default, and the policy meridian-deny-unlisted, which
#                 selects the issuer from every namespace, permits nothing. It passes when the request is
#                 Denied and the approver's whole message, judged before it
#                 is cut, names meridian-deny-unlisted as a policy that
#                 evaluated the request and does not name meridian-services
#                 as one (the line says the reason, cut to 60 characters, and
#                 the message, cut to 120). The form it matches is the one
#                 approver-policy v0.28.0 wrote on the cluster: "No policy
#                 approved this request: [meridian-deny-unlisted:
#                 [spec.allowed.uris: Invalid value: ...", a policy's name
#                 after "[", "]" or "," and before a colon, so that
#                 meridian-services-ca and the issuer's name do not count.
#                 The check depends on
#                 approver-policy's wording at the pinned version. A Denied
#                 request whose message names meridian-services as a policy
#                 is a FAIL (that policy selected a request from another
#                 namespace), and so is one whose message has neither form
#                 (the line says the approver's message is not in the form
#                 this check reads, and prints it cut). It fails when the
#                 request is Approved or carries a certificate (the issuer
#                 signed a request it must refuse; with both conditions true
#                 the request is Approved, whatever their order), and when
#                 neither condition is there
#                 after 30 s (the approver did not answer; a request nobody
#                 approves or denies would also be what a policy that can no
#                 longer be used by the requester leaves). The request is
#                 deleted as soon as it is read, by the EXIT trap when the run
#                 ends first (an error, a FAIL, an interrupt) and, for a run
#                 that was killed, at the start of the next one, which lists
#                 the requests with the label meridian-smoke=refused-request
#                 and deletes by name those older than 300 s (a request lives
#                 two seconds): the age is the creationTimestamp read with jq
#                 against this machine's clock, so a skewed clock only delays
#                 the sweep, and a younger request is another run's, which two
#                 runs at once leave to each other; a list that cannot be read
#                 is not an error. A failed delete, judged by kubectl's exit
#                 status and not by what it wrote, is a FAIL that names the
#                 request. A CertificateRequest makes no
#                 Secret: the certificate an issuer signed would be in the
#                 request's own status, which is deleted with it and never
#                 printed. openssl makes the key and writes it to /dev/null:
#                 it is in no file, no variable and no output. What it does
#                 not prove: the request is made by whoever runs smoke
#                 (kind's cluster-admin, which may use every policy), not by
#                 cert-manager's account, so the check shows what the
#                 namespace selector and the approver do with a request of
#                 another namespace, not the role bindings that let
#                 cert-manager use a policy (the plan's S056 section made
#                 those by hand); and a request for an issuer that is not
#                 Meridian's, which no policy answers, is not made. It adds
#                 a second or two when the approver is up, 30 s when it does
#                 not answer. The fifth line (S073, K5) is read-only and
#                 kind only (on Azure the database and its certificates are the
#                 provider's): CloudNativePG signs the database's server and
#                 replication client certificates with an authority of its
#                 own, cert-manager does not, and Prometheus holds no series
#                 for them, so nothing else says that a renewal did not
#                 happen. It reads the Cluster platform-db once and judges the
#                 earliest of the three expirations of
#                 .status.certificates.expirations, text in Go's default time
#                 format ("2027-01-04 18:05:31 +0000 UTC"): it passes with that
#                 date and the days left when more than half of the
#                 operator's renewal threshold remains (7 days by default, so
#                 84 hours), and fails when less remains, naming the
#                 certificate and the days, when one has ended, when the
#                 status holds no expiration (an operator that changed its
#                 status must not make it a pass), and when a date is not in
#                 exactly that form with a +0000 UTC zone (it cannot tell, and
#                 says so without repeating the text). The operator renews 7
#                 days before the end and its lifetime is in whole days, so a
#                 renewal cannot be seen inside one cluster run. What it does
#                 not prove: nothing alerts between two runs of smoke; it
#                 reads the operator's record of the dates, not the
#                 certificate files the instances serve. It was tested with a
#                 stand-in and the real jq. Seen on kind on 2026-10-07 (S073,
#                 run R4d): a pass on the real Cluster, naming platform-db-ca
#                 with 89 days left. Not seen: the line failing.

# The certificate policy check (10): what approves the services' certificates
# (S056). The policies are the ones manifests/certificate-policy.yaml applies
# (a test keeps this list equal to deploy.sh's); the add-on and the controller
# are Deployments in cert-manager. With cert-manager's approver off
# (disableAutoApproval in values/cert-manager.yaml) the chart renders neither
# the ClusterRole below nor the controller without the argument that follows.
readonly POLICY_NAMES=(meridian-services meridian-services-ca meridian-deny-unlisted telemetry-ca otel-collector)
readonly POLICY_NAMESPACE=cert-manager
readonly POLICY_ADDON=cert-manager-approver-policy
readonly POLICY_CONTROLLER=cert-manager
readonly POLICY_BUILTIN_ROLE=cert-manager-controller-approve:cert-manager-io
readonly POLICY_BUILTIN_OFF_ARG=--controllers=-certificaterequests-approver
# The database's own certificates (check 10's fifth line, S073). CloudNativePG
# signs the server's and the replication client's, with an authority of its own,
# and writes the three expirations into the Cluster's status as text in Go's
# default time format ("2027-01-04 18:05:31 +0000 UTC", not RFC 3339). The
# operator renews a certificate when it is closer to its end than
# EXPIRING_CHECK_THRESHOLD, in whole days, default 7 (the operator's
# documentation, "Operator configuration" and "Certificates", at the pinned
# version 1.30.1; the operator's ConfigMap on kind has no keys, so it is the
# default). The line fails inside half of that, so it stays silent while the
# operator still has time and is red when the operator has plainly missed.
# CERTIFICATE_DURATION, the lifetime (default 90), is in whole days too: the
# shortest is one day, so a renewal cannot be seen inside one cluster run.
readonly DATABASE_CERTIFICATE_RENEWAL_DAYS=7
readonly DATABASE_CERTIFICATE_MARGIN_SECONDS=$((DATABASE_CERTIFICATE_RENEWAL_DAYS * 86400 / 2))
# The request the issuer must refuse (check 10's fourth line): a
# CertificateRequest in REFUSED_NAMESPACE for the issuer REFUSED_ISSUER, with a
# URI that meridian-services lists (the Claims API's own: since S072 the policy
# lists each URI, so an unlisted one would be refused in `meridian` too) and a
# duration and usages it allows, so nothing about the request's shape refuses
# it, only its namespace (a test keeps these equal to certificate-policy.yaml's
# lists). The name is
# REFUSED_NAME_PREFIX and a suffix, the label is what the next run finds a
# leftover by (only one older than REFUSED_LEFTOVER_AGE seconds is deleted), and
# the answer is read for up to REFUSED_ATTEMPTS tries, REFUSED_INTERVAL seconds
# apart (the approver answers in about a second). The reason is cut to
# REFUSED_REASON_LENGTH characters and the message to REFUSED_MESSAGE_LENGTH for
# the line; a Denied request passes when the whole message names
# REFUSED_DENYING_POLICY and not REFUSED_SELECTING_POLICY as a policy (a test
# keeps both equal to certificate-policy.yaml's names).
readonly REFUSED_NAMESPACE=default
readonly REFUSED_ISSUER=meridian-services
readonly REFUSED_LABEL=meridian-smoke=refused-request
readonly REFUSED_NAME_PREFIX=meridian-smoke-refused-
readonly REFUSED_URI=spiffe://meridian.kind/ns/meridian/sa/claims-api
readonly REFUSED_DURATION=1h0m0s
readonly REFUSED_ATTEMPTS=15
readonly REFUSED_INTERVAL=2
readonly REFUSED_MESSAGE_LENGTH=120
readonly REFUSED_REASON_LENGTH=60
readonly REFUSED_LEFTOVER_AGE=300
readonly REFUSED_SELECTING_POLICY=meridian-services
readonly REFUSED_DENYING_POLICY=meridian-deny-unlisted
refused_state=""   # set by refused_read: "<verdict>|<issued>|<reason>|<message>"
refused_problem="" # set by refused_read: why it could not read the request
refused_verdict="" # set by refused_wait: Approved, Denied or Issued, or empty
refused_reason=""  # set by refused_wait: the verdict's reason, cleaned
refused_message="" # set by refused_wait: the verdict's message, cleaned and cut
refused_message_full="" # set by refused_wait: the same message, cleaned, not cut

# ── 10. certificate policy ───────────────────────────────────────────────────
# Read-only, and never skipped: `make up` makes every object it reads, with or
# without `make deploy`, so a missing one is a FAIL. Nothing else in `make up`
# or `make smoke` makes a certificate request on a cluster that has its
# certificates, and the issuer is Ready with the policies or the add-on gone, so
# these three lines are what catch a cert-manager or approver-policy update that
# leaves nothing to approve a request, or turns the built-in approver back on.
check_policies_ready() {
  local policy ready wrong=""
  for policy in "${POLICY_NAMES[@]}"; do
    ready="$(kctl get certificaterequestpolicy "${policy}" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" || ready=""
    [[ "${ready}" == True ]] || wrong+=" ${policy}"
  done
  if [[ -n "${wrong}" ]]; then
    fail "certificate policy: CertificateRequestPolicy${wrong} missing or not Ready: with cert-manager's own approver off, nothing would approve the chart's Certificates (make up)"
    return
  fi
  pass "certificate policy: the CertificateRequestPolicies ${POLICY_NAMES[*]} are Ready"
}

check_approver_addon() {
  local available
  available="$(kctl -n "${POLICY_NAMESPACE}" get deployment "${POLICY_ADDON}" \
    -o jsonpath='{.status.availableReplicas}' 2>/dev/null)" || available=""
  if [[ "${available}" =~ ^[0-9]+$ ]] && ((10#${available} > 0)); then
    pass "certificate policy: deployment/${POLICY_ADDON} in ${POLICY_NAMESPACE} has ${available} available replica(s)"
  else
    fail "certificate policy: deployment/${POLICY_ADDON} in ${POLICY_NAMESPACE} is missing or has no available replica: nothing would approve the chart's Certificates (make up)"
  fi
}

# cert-manager's built-in approver is off, read two ways that must agree: the
# ClusterRole it needs to approve exists only when the chart renders it with the
# approver on (kubectl's NotFound is the pass; any other error is not), and the
# controller's arguments hold the switch that turns the approver controller off,
# as a whole argument.
check_builtin_approver_off() {
  local err_file args problems=""
  err_file="$(mktemp)"
  if kctl get clusterrole "${POLICY_BUILTIN_ROLE}" -o name >/dev/null 2>"${err_file}"; then
    problems+="; the ClusterRole ${POLICY_BUILTIN_ROLE} exists"
  elif ! grep -q '(NotFound)' "${err_file}"; then
    problems+="; could not look for the ClusterRole ${POLICY_BUILTIN_ROLE} (kubectl said: $(clean_lines "$(<"${err_file}")"))"
  fi
  if args="$(kctl -n "${POLICY_NAMESPACE}" get deployment "${POLICY_CONTROLLER}" \
    -o jsonpath='{.spec.template.spec.containers[*].args[*]}' 2>"${err_file}")"; then
    [[ " ${args} " == *" ${POLICY_BUILTIN_OFF_ARG} "* ]] ||
      problems+="; the arguments of deployment/${POLICY_CONTROLLER} do not hold ${POLICY_BUILTIN_OFF_ARG}"
  else
    problems+="; could not read the arguments of deployment/${POLICY_CONTROLLER} (kubectl said: $(clean_lines "$(<"${err_file}")"))"
  fi
  rm -f "${err_file}"
  if [[ -n "${problems}" ]]; then
    fail "certificate policy: cert-manager's own approver may be on, so the issuer may be signing for every request again${problems}"
    return
  fi
  pass "certificate policy: cert-manager's own approver is off (no ClusterRole ${POLICY_BUILTIN_ROLE}; the controller runs with ${POLICY_BUILTIN_OFF_ARG})"
}

# The fourth line, and the one place where smoke changes the cluster on purpose:
# a request that the issuer must refuse. It is a CertificateRequest, not a
# Certificate, so that the key is made here, with openssl, and never leaves this
# machine (a Certificate has cert-manager make it, in a Secret in the cluster).
# The request is for the issuer meridian-services with a URI that policy lists
# (the Claims API's, and no DNS name, as that service's own Certificate), so
# that the policy would approve it in `meridian`; it is made in
# REFUSED_NAMESPACE, which no policy but the denying one selects, so it must be
# Denied. Approved, or carrying a certificate, is a
# FAIL: the issuer signed a request it must refuse. The request is deleted
# right after it is read, by the EXIT trap when the run ends first, and, when a
# run was killed, at the start of the next one by name, from a list of the
# requests with its label that are older than REFUSED_LEFTOVER_AGE.

# refused_make_csr: a signing request in PEM on stdout. openssl makes the key
# and writes it to /dev/null: this script never wants the certificate, so the key
# is in no file, no variable and no output.
refused_make_csr() {
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout /dev/null -subj / -addext "subjectAltName=URI:${REFUSED_URI}"
}

# refused_manifest NAME CSR: the CertificateRequest as JSON on stdout.
refused_manifest() {
  jq -n --arg name "$1" --arg csr "$2" --arg namespace "${REFUSED_NAMESPACE}" \
    --arg issuer "${REFUSED_ISSUER}" --arg duration "${REFUSED_DURATION}" \
    --arg label_key "${REFUSED_LABEL%%=*}" --arg label_value "${REFUSED_LABEL#*=}" '{
      apiVersion: "cert-manager.io/v1",
      kind: "CertificateRequest",
      metadata: {name: $name, namespace: $namespace, labels: {($label_key): $label_value}},
      spec: {
        request: ($csr + "\n" | @base64),
        duration: $duration,
        usages: ["digital signature", "client auth", "server auth"],
        issuerRef: {name: $issuer, kind: "ClusterIssuer", group: "cert-manager.io"}
      }
    }'
}

# refused_read: the request's verdict in ${refused_state} as
# "<Approved|Denied|empty>|<true|false: it carries a certificate>|<reason>|<message>",
# the message on one line. Returns 1 with ${refused_problem} when it cannot read it.
# The request, and a certificate in it, are read by jq and never printed.
refused_read() {
  local json
  if ! json="$(kctl -n "${REFUSED_NAMESPACE}" get certificaterequest "${refused_request}" \
    -o json 2>"${refused_err_file}")"; then
    refused_problem="could not read the request ${refused_request} in ${REFUSED_NAMESPACE} (kubectl said: $(clean_lines "$(<"${refused_err_file}")"))"
    return 1
  fi
  if ! refused_state="$(jq -r '
    [(.status.conditions // [])[] | select(.status == "True")] as $held
    | (([$held[] | select(.type == "Approved")] + [$held[] | select(.type == "Denied")]) | .[0] // {}) as $verdict
    | [$verdict.type // "", ((.status.certificate // "") != "" | tostring),
       ($verdict.reason // "" | gsub("[|\r\n]"; " ")), ($verdict.message // "" | gsub("[\r\n]+"; " "))]
    | join("|")' <<<"${json}" 2>/dev/null)"; then
    refused_problem="could not read the conditions of the request ${refused_request} in ${REFUSED_NAMESPACE}"
    return 1
  fi
}

# refused_wait: read the request until it holds a verdict, up to REFUSED_ATTEMPTS
# times. Sets ${refused_verdict} (Approved, Denied, or Issued for a request that
# carries a certificate whatever its conditions say), ${refused_reason} and
# ${refused_message} (cleaned and cut). Returns 1 when a read failed and 2 when
# the attempts ran out with no verdict.
refused_wait() {
  local attempt issued
  for ((attempt = 1; attempt <= REFUSED_ATTEMPTS; attempt++)); do
    refused_read || return 1
    IFS='|' read -r refused_verdict issued refused_reason refused_message <<<"${refused_state}"
    refused_reason="$(clean_lines "${refused_reason}")"
    refused_reason="${refused_reason:0:REFUSED_REASON_LENGTH}"
    refused_message_full="$(clean_lines "${refused_message}")"
    refused_message="${refused_message_full:0:REFUSED_MESSAGE_LENGTH}"
    [[ "${issued}" != true ]] || refused_verdict=Issued
    [[ -z "${refused_verdict}" ]] || return 0
    ((attempt == REFUSED_ATTEMPTS)) || sleep "${REFUSED_INTERVAL}"
  done
  return 2
}

# refused_names_policy NAME: succeeds when the approver's whole message
# (${refused_message_full}) names the policy NAME as one that evaluated the
# request. The form it matches is the one approver-policy v0.28.0 wrote on the
# cluster (2026-10-06): "No policy approved this request: [<policy>: [<errors>"
# with the next policy, if any, after a "]" or a ",". A policy's name is the
# word after "[", "]" or "," (and at most one space) and before ":", so that
# meridian-services-ca, the issuer's name after "name: ", a URI that ends in
# the name and a longer name do not count. It depends on that wording at the
# pinned version.
refused_names_policy() {
  local before='(\[|\]|,) ?'
  [[ "${refused_message_full}" =~ ${before}$1: ]]
}

# refused_denial_cause: why the Denied request was denied, from the approver's
# whole message (before it is cut for the line), one word on stdout: "namespace"
# when it names the denying policy and not the one that selects `meridian` (the
# claim of the line: the namespace refuses), "selected" when it names that one,
# and "unreadable" when it names neither as a policy.
refused_denial_cause() {
  if refused_names_policy "${REFUSED_SELECTING_POLICY}"; then
    echo selected
  elif refused_names_policy "${REFUSED_DENYING_POLICY}"; then
    echo namespace
  else
    echo unreadable
  fi
}

# refused_report_denied WHERE LEFT: the line of a Denied request, for the
# request WHERE names; LEFT is what is left of it when it could not be deleted.
refused_report_denied() {
  local where=$1 left=$2 verdict="was Denied (${refused_reason}: ${refused_message})"
  case "$(refused_denial_cause)" in
    selected)
      fail "certificate policy: the policy ${REFUSED_SELECTING_POLICY} selected a request from another namespace: ${where} ${verdict}, and the message names that policy; its namespace selector must not list ${REFUSED_NAMESPACE}${left}" ;;
    unreadable)
      fail "certificate policy: ${where} ${verdict}, but the approver's message is not in the form this check reads (the policy ${REFUSED_DENYING_POLICY}, named as a policy of the request's evaluation); approver-policy's wording may have changed${left}" ;;
    *)
      if [[ -n "${left}" ]]; then
        fail "certificate policy: ${where} ${verdict}${left}"
      else
        pass "certificate policy: a request for the issuer ${REFUSED_ISSUER} from the namespace ${REFUSED_NAMESPACE}, with a URI under the Meridian prefix, ${verdict}, and deleted"
      fi ;;
  esac
}

# refused_report NAME WAIT_STATUS DELETE_PROBLEM: the one line of the fourth
# check, for the request NAME. DELETE_PROBLEM is what kubectl said when the
# request could not be deleted, else empty: the line then says what is left.
refused_report() {
  local name=$1 waited=$2 delete_problem=$3 left="" where
  where="the request ${name} in ${REFUSED_NAMESPACE}"
  [[ -z "${delete_problem}" ]] ||
    left="; it could not be deleted (kubectl said: ${delete_problem}): kubectl -n ${REFUSED_NAMESPACE} delete certificaterequest ${name}"
  case "${waited}:${refused_verdict}" in
    0:Denied) refused_report_denied "${where}" "${left}" ;;
    0:*)
      fail "certificate policy: the issuer signed a request it must refuse: ${where} was ${refused_verdict} (${refused_reason}: ${refused_message})${left:-; smoke deleted it}; check cert-manager's own approver and the selectors of the policies (make up)" ;;
    1:*) fail "certificate policy: ${refused_problem}${left}" ;;
    *) fail "certificate policy: the approver did not answer: ${where} had neither condition Approved nor Denied after $((REFUSED_ATTEMPTS * REFUSED_INTERVAL)) s (is deployment/${POLICY_ADDON} running, and may the user running smoke use the policy meridian-deny-unlisted?)${left}" ;;
  esac
}

# refused_sweep_leftovers: at the start of a run, delete by name the requests
# with REFUSED_LABEL that are older than REFUSED_LEFTOVER_AGE seconds: what a
# killed run left (a request lives two seconds). A younger one is another run's
# that is under way (two sessions on one machine do happen), and is its own to
# delete. The age is this machine's clock against the request's
# creationTimestamp (the API server's), so a skewed clock only delays the
# sweep: the request stays, and a later run deletes it. A list that cannot be
# read is not an error: the check goes on, and the leftover waits.
refused_sweep_leftovers() {
  local json names leftover
  json="$(kctl -n "${REFUSED_NAMESPACE}" get certificaterequest -l "${REFUSED_LABEL}" \
    -o json 2>/dev/null)" || return 0
  names="$(jq -r --argjson age "${REFUSED_LEFTOVER_AGE}" '
    .items[] | select(now - (.metadata.creationTimestamp | fromdateiso8601) > $age)
    | .metadata.name' <<<"${json}" 2>/dev/null)" || return 0
  while IFS= read -r leftover; do
    [[ -n "${leftover}" ]] || continue
    kctl -n "${REFUSED_NAMESPACE}" delete certificaterequest "${leftover}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  done <<<"${names}"
}

refused_check() {
  local csr manifest detail name waited=0 delete_problem=""
  refused_sweep_leftovers
  if ! csr="$(refused_make_csr 2>"${refused_err_file}")"; then
    fail "certificate policy: openssl could not make the request that the issuer must refuse: $(clean_lines "$(<"${refused_err_file}")")"
    return
  fi
  name="${REFUSED_NAME_PREFIX}$$-${RANDOM}"
  refused_request="${name}"
  if ! manifest="$(refused_manifest "${name}" "${csr}" 2>"${refused_err_file}")" ||
    ! kctl -n "${REFUSED_NAMESPACE}" create -f - <<<"${manifest}" >/dev/null 2>"${refused_err_file}"; then
    detail="$(clean_lines "$(<"${refused_err_file}")")"
    refused_delete_request || true # the create may have been half done
    fail "certificate policy: could not create the request that the issuer must refuse in ${REFUSED_NAMESPACE} (said: ${detail})"
    return
  fi
  refused_wait || waited=$?
  if ! refused_delete_request "${refused_err_file}"; then # the exit status decides
    delete_problem="$(clean_lines "$(<"${refused_err_file}")")"
    delete_problem="${delete_problem:-kubectl exited non-zero with no message}"
  fi
  refused_report "${name}" "${waited}" "${delete_problem}"
}

# The file kubectl's and openssl's messages go to while the request exists; the
# EXIT trap removes it too, so an interrupted run leaves no temporary file.
check_refused_request() {
  refused_err_file="$(mktemp)"
  refused_check
  rm -f "${refused_err_file}"
  refused_err_file=""
}

# database_certificates_verdict CLUSTER_JSON NOW: one line, fields separated by
# "|", from the Cluster's answer and the clock (NOW, epoch seconds; the caller
# gives it, nothing here reads a clock). The cluster's answer goes in as a file
# (a process substitution), never as an argument. Every expiration of
# .status.certificates.expirations must be exactly "YYYY-MM-DD hh:mm:ss +0000
# UTC" (Go's default format for a UTC instant); the seconds are then read as
# UTC from the first 19 characters. The earliest of them is judged.
#   none                              no expiration in the status
#   shape|NAME                        NAME's expiration is not in that form
#                                     (another zone, RFC 3339, a fraction, not
#                                     text): nothing is judged
#   date|NAME                         NAME's expiration has that form and is no
#                                     date (month 13, hour 25): nothing is judged
#   ended|NAME|DAYS|DATE|COUNT        the earliest ended DAYS whole days ago
#   close|NAME|DAYS|DATE|COUNT        DAYS whole days left, margin or less
#   far|NAME|DAYS|DATE|COUNT          more than the margin left
database_certificates_verdict() {
  jq -nr --slurpfile status <(printf '%s' "$1") --argjson now "$2" \
    --argjson margin "${DATABASE_CERTIFICATE_MARGIN_SECONDS}" '
    def shaped: type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} \\+0000 UTC$");
    def epoch: .[0:19] | strptime("%Y-%m-%d %H:%M:%S") | mktime;
    (try $status[0].status.certificates.expirations catch null) as $raw
    | (if ($raw | type) == "object" then $raw | to_entries else [] end) as $found
    | if ($found | length) == 0 then "none"
      elif any($found[]; (.value | shaped) | not) then
        "shape|\(first($found[] | select((.value | shaped) | not)).key)"
      elif any($found[]; (.value | try epoch catch null) == null) then
        "date|\(first($found[] | select((.value | try epoch catch null) == null)).key)"
      else
        ($found | map({name: .key, date: .value, at: (.value | epoch)}) | sort_by([.at, .name]) | first) as $first
        | ($first.at - $now) as $left
        | (if $left <= 0 then "ended|\($first.name)|\((0 - $left) / 86400 | floor)"
           else "\(if $left <= $margin then "close" else "far" end)|\($first.name)|\($left / 86400 | floor)" end)
          + "|\($first.date)|\($found | length)"
      end'
}

# check_database_certificates [NOW]: the fifth line of check 10, kind only (on
# Azure the database and its certificates are the provider's). It reads the
# Cluster platform-db once and fails when the earliest of the operator's three
# expirations is DATABASE_CERTIFICATE_MARGIN_SECONDS or less away, when one
# cannot be read as a UTC date and when there is none: an operator that changed
# its status must not turn the line into a pass. NOW defaults to this machine's
# clock (kind's node shares it, and the margin is days); a test gives its own.
# What it does not do: it tells nothing between two runs of smoke.
check_database_certificates() {
  local now="${1:-$(date +%s)}" cluster_status verdict
  local what result name days ends_at unit count
  what="certificate policy: the database's certificates (CloudNativePG's own)"
  if ! cluster_status="$(kctl -n meridian get clusters.postgresql.cnpg.io platform-db -o json 2>/dev/null)"; then
    fail "${what}: could not read the Cluster platform-db (kubectl failed)"
    return
  fi
  if ! verdict="$(database_certificates_verdict "${cluster_status}" "${now}" 2>/dev/null)"; then
    fail "${what}: could not read the Cluster platform-db's answer as JSON"
    return
  fi
  IFS='|' read -r result name days ends_at count <<<"$(clean_lines "${verdict}")"
  unit=days
  if [[ "${days}" == 1 ]]; then unit=day; fi
  case "${result}" in
    far) pass "${what}: the earliest of ${count} is ${name}, with ${days} ${unit} left (ends ${ends_at}); the operator renews at ${DATABASE_CERTIFICATE_RENEWAL_DAYS} days, this line fails inside $((DATABASE_CERTIFICATE_MARGIN_SECONDS / 3600)) hours" ;;
    close) fail "${what}: ${name} has ${days} ${unit} left (ends ${ends_at}), inside the margin of $((DATABASE_CERTIFICATE_MARGIN_SECONDS / 3600)) hours: the operator renews at ${DATABASE_CERTIFICATE_RENEWAL_DAYS} days and has not; read its log in meridian (the Deployment cnpg-cloudnative-pg)" ;;
    ended) fail "${what}: ${name} has ended ${days} ${unit} ago (${ends_at}) and was not renewed; read the operator's log in meridian (the Deployment cnpg-cloudnative-pg)" ;;
    none) fail "${what}: the status of the Cluster platform-db holds no expiration (.status.certificates.expirations), so nothing is judged: the operator may have changed its status" ;;
    shape) fail "${what}: cannot tell when ${name} ends: its expiration is not in the form 'YYYY-MM-DD hh:mm:ss +0000 UTC' (another zone or another shape), so nothing is judged: has the operator changed its status?" ;;
    date) fail "${what}: cannot tell when ${name} ends: its expiration has the form 'YYYY-MM-DD hh:mm:ss +0000 UTC' but is not a date, so nothing is judged: has the operator changed its status?" ;;
    *) fail "${what}: cannot tell: the verdict was not in a form this line reads" ;;
  esac
}

check_certificate_policy() {
  check_policies_ready
  check_approver_addon
  check_builtin_approver_off
  check_refused_request
  check_database_certificates
}
