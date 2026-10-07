#!/usr/bin/env bash
# Ask cert-manager to issue one Certificate of the namespace `meridian` again,
# now: `make cert-renew CERT=<name>` (S073). After a denied or failed request
# cert-manager waits before it asks again (an hour after the first failure,
# doubling), so a repaired policy does not help a deploy for an hour. `cmctl
# renew` is the tool that asks at once; it is not installed here and none is
# added. What it does is one write, which this script makes with kubectl: the
# Certificate's `Issuing` condition set to True, reason `ManuallyTriggered`,
# through the status subresource (renewCertificate in cmctl v2.6.1,
# pkg/renew/renew.go lines 216 to 218). cert-manager's controllers see the
# condition and make a new request; the failed one of the earlier attempt is
# recognised by its failure time being before the condition's
# lastTransitionTime (certificates-issuing, issuing_controller.go in v1.21.2),
# which is why the time is set to now when the status changes.
#   1. CERT, checked before the cluster is asked anything: it must be set and a
#      DNS label (lowercase letters, digits and '-', 1 to 63 characters, a
#      letter or digit at each end). A value that is not is not repeated.
#   2. who holds the cluster (S075, common.sh): another holder stops this unless
#      TAKE_CLUSTER=1. The record is written once, with state `ok`, right after
#      the write succeeded: the one write is all this command changes, so a
#      refusal, a write that failed and a command that stopped before it leave
#      the record as it was (not `changing`, which says a run began to change
#      the cluster and did not end well). A write that timed out may have been
#      made; running the command again finds the Issuing condition True and
#      writes nothing
#   3. the Certificates of the namespace, one read: CERT must be one of them, or
#      the script lists the names it found. A Certificate whose Issuing
#      condition is already True is being issued: nothing is written
#   4. the write: a JSON merge patch (a custom resource takes no strategic one),
#      which replaces the whole list `status.conditions`, so the list is sent
#      with every other condition as read, and with the resourceVersion that was
#      read, so a Certificate that cert-manager changed in between is refused
#      (409) and the command is run again. The Issuing entry is replaced in its
#      place, or added; observedGeneration is the Certificate's generation, as
#      cmctl sets it. lastFailureTime and failedIssuanceAttempts are left alone:
#      cert-manager clears them when the issuance succeeds
# CERT=rate-store costs a restart: the store reads its certificate once, at its
# start, and its liveness check restarts the server when the file on the volume
# is more than two seconds newer than the server. The script says so (one line,
# before the write): every model call answers 503 for one to three minutes (worked
# out from the probes' numbers, not measured) and the tenants' rate windows are
# lost. The one case it does not restart is a
# renewal typed within two seconds after the store started: the store then keeps
# the certificate it loaded until the next renewal or restart (the template's
# comment on the rule says what that costs). The six services do not restart on
# a renewal by hand.
# No Secret is read, and nothing but the one status is written. Exit code 0 when
# the condition was written or was already True; 1 otherwise.
# Tested against a stub kubectl. Seen on kind on 2026-10-07: a refusal for each
# bad name and one renewal of a healthy Certificate (revision 1 to 2, a new
# request Approved and Ready). Not seen: a Certificate whose request was denied,
# a 409 in the window between the read and the write, and the rate store's
# renewal with the line below (it was added after that run).
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly NAMESPACE=meridian
readonly NAME_PATTERN='^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'
readonly USAGE='make cert-renew CERT=<name of a Certificate in the namespace meridian>'
readonly TRIGGER_REASON=ManuallyTriggered
readonly TRIGGER_MESSAGE='Certificate re-issuance manually triggered'
readonly SHOWN_NAMES=300

cert="${CERT:-}"

# check_name: ${cert} is set and a DNS label, or the script stops. The check is
# for the letters of the C locale, whatever the operator's locale says.
check_name() {
  local LC_ALL=C
  [[ -n "${cert}" ]] || die "CERT is missing; run: ${USAGE}"
  [[ "${cert}" =~ ${NAME_PATTERN} ]] ||
    die "CERT is not a DNS label (lowercase letters, digits and '-', 1 to 63 characters, starting and ending with a letter or digit); run: ${USAGE}"
}

# find_certificate: the Certificate ${cert} of the namespace, as one line of JSON
# in ${certificate}, or a refusal that lists the names the namespace holds.
certificate=""
find_certificate() {
  local list names
  list="$(kctl -n "${NAMESPACE}" get certificates -o json)" ||
    die "could not list the Certificates of the namespace ${NAMESPACE} (kubectl's error is above): a cluster made before S055 does not know the kind; run 'make up' first"
  certificate="$(jq -c --arg name "${cert}" '[.items[]? | select(.metadata.name == $name)] | first // empty' <<<"${list}")" ||
    die "could not read the list of Certificates: the answer is not a list"
  [[ -z "${certificate}" ]] || return 0
  names="$(jq -r '[.items[]?.metadata.name] | join(", ")' <<<"${list}" 2>/dev/null | printable_ascii | cut -c "1-${SHOWN_NAMES}")" || names=""
  if [[ -z "${names}" ]]; then
    die "the namespace ${NAMESPACE} holds no Certificate (is the release installed? run 'make deploy')"
  fi
  die "that name is not a Certificate in the namespace ${NAMESPACE}; it holds: ${names}"
}

# trigger_patch: the merge patch on stdout, from ${certificate}. The conditions
# are sent whole (a merge patch replaces a list); the Issuing entry is replaced
# where it stands, or added at the end; the version read is named, so a change
# in between is refused.
trigger_patch() {
  local now
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  jq -c --arg now "${now}" --arg reason "${TRIGGER_REASON}" --arg message "${TRIGGER_MESSAGE}" '
    ({type: "Issuing", status: "True", reason: $reason, message: $message, lastTransitionTime: $now}
      + (if (.metadata.generation | type) == "number" then {observedGeneration: .metadata.generation} else {} end)) as $issuing
    | (.status.conditions // []) as $conditions
    | {status: {conditions: (if any($conditions[]; .type == "Issuing")
        then [$conditions[] | if .type == "Issuing" then $issuing else . end]
        else $conditions + [$issuing] end)}}
      + (if (.metadata.resourceVersion | type) == "string" then {metadata: {resourceVersion: .metadata.resourceVersion}} else {} end)
  ' <<<"${certificate}"
}

# is_issuing: true when the Certificate already has Issuing True.
is_issuing() {
  jq -e 'any((.status.conditions // [])[]; .type == "Issuing" and .status == "True")' <<<"${certificate}" >/dev/null
}

watch_commands() {
  printf '    kubectl --kubeconfig %s --context %s -n %s get certificate %s\n' "${KUBECONFIG_FILE}" "${KUBE_CONTEXT}" "${NAMESPACE}" "${cert}"
  printf '    kubectl --kubeconfig %s --context %s -n %s get certificaterequest\n' "${KUBECONFIG_FILE}" "${KUBE_CONTEXT}" "${NAMESPACE}"
}

check_name
need_tools kubectl jq
need_cluster
check_cluster_holder "make cert-renew CERT=${cert}"
find_certificate
if is_issuing; then
  log "the Certificate ${cert} is already being issued (its Issuing condition is True): nothing was written. Watch it:"
  watch_commands
  exit 0
fi
patch="$(trigger_patch)"
if [[ "${cert}" == rate-store ]]; then
  # "One to three minutes" is derived from the probes' numbers (the kubelet's
  # sync of the Secret, then ten seconds a probe and six failures), not measured.
  log "renewing the rate store's certificate restarts the store when it sees the new file: for about one to three minutes (worked out from the probes' numbers, not measured) every model call answers 503 and the tenants' rate windows are lost"
fi
kctl -n "${NAMESPACE}" patch certificate "${cert}" --subresource=status --type=merge -p "${patch}" >/dev/null ||
  die "kubectl could not write the status of the Certificate ${cert} (its error is above); if it says the object has been modified, cert-manager wrote to it meanwhile: run the command again"
log "asked cert-manager to issue the Certificate ${cert} again: its Issuing condition is now True (reason ${TRIGGER_REASON})"
log "watch it (a new CertificateRequest appears in seconds; one that is Denied again says the policy is still wrong, and its Approved or Denied condition says why):"
watch_commands
record_cluster_holder ok
