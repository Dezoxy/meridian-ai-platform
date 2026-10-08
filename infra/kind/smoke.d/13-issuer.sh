# shellcheck shell=bash
#  13. issuer: six lines when the sign-in issuer add-on is on (S021, Y2b), one SKIP
#                 line when it is off (MERIDIAN_IDENTITY empty, the default: Keycloak
#                 is an add-on that `make up` makes only with MERIDIAN_IDENTITY=
#                 keycloak; infra/kind/README.md, "The sign-in issuer"). On, in order:
#                 the pod in `identity` is Ready and runs the pinned image (the
#                 Deployment's image is KEYCLOAK_IMAGE of pins.env, the init
#                 container runs the same, the container has no environment, and
#                 the pod's imageID ends in the digest); the discovery document,
#                 through the edge, names the FRONT URL (http://id.meridian.
#                 localhost:8088/realms/meridian-staff) as its issuer; the key
#                 document, through the edge, holds at least one RS256 signing key;
#                 four paths through the edge are the edge's own 404 with an empty
#                 body (a 404 from Keycloak has a body): /admin/, /realms/master/
#                 and, under the staff realm, account/ and clients-registrations/
#                 openid-connect, because the route forwards what the sign-in flow
#                 needs and nothing else; four ways of climbing from the staff
#                 realm to the master realm (a plain .., %2e%2e and ..%2f, and a
#                 plain .. from inside .well-known/, sent as written with curl's
#                 --path-as-is) each pass when the answer is the edge's own empty
#                 404, or a 307 with an empty body to a path on this host that is
#                 under no forwarded prefix and is itself the edge's own empty 404
#                 (for an escaped slash the edge unescapes, normalises and
#                 redirects by itself: seen on kind, run KR1); a redirect into a
#                 forwarded prefix, a 200 or a 404 with a body fails and is named;
#                 and, from INSIDE the cluster, the
#                 Claims API's pod fetches the discovery document by the Service's
#                 name (keycloak.identity.svc:8080) and it names the FRONT URL as
#                 its issuer, which proves the policy that admits the Claims API,
#                 the path, and --hostname. That last line is skipped while the
#                 Claims API is not deployed. The requests are plain GETs of public
#                 documents: no line reads a Secret, signs anyone in or asks for a
#                 token. The lines say what they prove and not more: that a
#                 browser's sign-in works and that the pod keeps its keys are not
#                 checked here, and neither is a refusal by the network policy. Run
#                 on kind once (run KR1, 2026-10-08): five lines passed and the
#                 climbing line failed on its own expectation, now corrected. A
#                 pod that is not up yet fails the first
#                 line and the others run all the same; an add-on that was never
#                 made fails them all.

readonly ISSUER_FRONT_URL=http://id.meridian.localhost:8088
readonly ISSUER_REALM_URL="${ISSUER_FRONT_URL}/realms/meridian-staff"
# Paths that the route does not forward, so the edge answers with its own 404.
readonly ISSUER_CLOSED_PATHS="/admin/ /realms/master/ /realms/meridian-staff/account/ /realms/meridian-staff/clients-registrations/openid-connect"
# Ways of reaching the master realm from the staff realm, or from inside an allowed
# prefix, sent as written (--path-as-is): none may get through the edge.
readonly ISSUER_CLIMBING_PATHS="/realms/meridian-staff/../master/ /realms/meridian-staff/%2e%2e/master/ /realms/meridian-staff/..%2fmaster/ /realms/meridian-staff/.well-known/../../master/"
# The Service's name for the pods, and the probe the Claims API's pod runs (Python
# is in its image; curl is not): the issuer of the discovery document, one line.
readonly ISSUER_SERVICE_URL=http://keycloak.identity.svc:8080/realms/meridian-staff
readonly ISSUER_INSIDE_PROBE='import json, sys, urllib.request; print(json.load(urllib.request.urlopen(sys.argv[1], timeout=8)).get("issuer", ""))'
# The prefixes the route forwards (identity.yaml): a redirect that lands on one of
# them is a way in, not a way out.
readonly ISSUER_ALLOWED_PREFIXES="/realms/meridian-staff/.well-known/ /realms/meridian-staff/protocol/openid-connect/ /realms/meridian-staff/login-actions/ /resources/"
# What the last issuer_get left: the status, the body, the Location header (cleaned,
# at most 300 characters) and, when curl failed, why.
issuer_status=""
issuer_body=""
issuer_location=""
issuer_error=""

# ── 13. issuer ───────────────────────────────────────────────────────────────
# issuer_get URL: GET URL through the edge, as the other checks reach the Claims
# API. Returns 1 when curl failed (issuer_error holds why); otherwise leaves the
# status in ${issuer_status} and the body in ${issuer_body}. Nothing is followed, and
# the path goes as written (--path-as-is: curl would otherwise remove the dots).
issuer_get() {
  local out headers status
  out="$(mktemp)"
  headers="$(mktemp)"
  issuer_location=""
  if ! status="$(curl -q --noproxy '*' -sS --path-as-is -m 10 -D "${headers}" -o "${out}" -w '%{http_code}' "$1" 2>&1)"; then
    issuer_error="$(clean_lines "${status}")"
    rm -f "${out}" "${headers}"
    return 1
  fi
  issuer_status="$(clean_lines "${status}")"
  issuer_body="$(<"${out}")"
  issuer_location="$(awk 'tolower($1) == "location:" { print $2; exit }' "${headers}" | LC_ALL=C tr -cd '[:print:]' | cut -c 1-300)"
  rm -f "${out}" "${headers}"
}

check_issuer_pod() {
  local pods verdict
  if ! pods="$(kctl -n identity get pod -l app.kubernetes.io/name=keycloak -o json)"; then
    fail "issuer: could not read the Keycloak pods in identity (kubectl's error is above); if the add-on was never made here, MERIDIAN_IDENTITY=keycloak make up makes it"
    return
  fi
  verdict="$(jq -r --arg image "${KEYCLOAK_IMAGE}" --arg digest "${KEYCLOAK_IMAGE##*@}" '
    [.items[]?] as $pods
    | if ($pods | length) != 1 then "expected one Keycloak pod, found \($pods | length)"
      else $pods[0] as $pod
        | ([$pod.status.conditions[]? | select(.type == "Ready" and .status == "True")] | length) as $ready
        | ([$pod.spec.containers[]? | select(.name == "keycloak") | .image][0] // "") as $spec
        | ([$pod.status.containerStatuses[]? | select(.name == "keycloak") | .imageID][0] // "") as $id
        | ([$pod.spec.initContainers[]? | select(.image != $image)] | length) as $other
        | ([$pod.spec.containers[]? | select(has("env") or has("envFrom"))] | length) as $environment
        | if $ready != 1 then "the pod is not Ready"
          elif $spec != $image then "the pod runs another image than the pin in pins.env"
          elif $other != 0 then "an init container runs another image than the pin in pins.env"
          elif $environment != 0 then "the container has an environment, and none is written"
          elif ($id | endswith($digest) | not) then "the pod reports an imageID that does not end in the pinned digest"
          else "ok" end
      end' <<<"${pods}" 2>/dev/null)" || verdict=""
  if [[ "${verdict}" == ok ]]; then
    pass "issuer: the Keycloak pod in identity is Ready and runs the pinned image (sha256:${KEYCLOAK_IMAGE##*@sha256:})"
  elif [[ -z "${verdict}" ]]; then
    fail "issuer: the answer for the Keycloak pods in identity is not a list of pods"
  else
    fail "issuer: ${verdict} (kubectl -n identity get pods; describe the pod)"
  fi
}

check_issuer_discovery() {
  local url="${ISSUER_REALM_URL}/.well-known/openid-configuration" issuer
  if ! issuer_get "${url}"; then
    fail "issuer: ${url} did not answer: ${issuer_error}"
    return
  fi
  if [[ "${issuer_status}" != 200 ]]; then
    fail "issuer: ${url} -> ${issuer_status}, expected 200: the route, the pod or the realm is not there"
    return
  fi
  issuer="$(jq -r '(.issuer // empty) | strings' <<<"${issuer_body}" 2>/dev/null)" || issuer=""
  if [[ "${issuer}" == "${ISSUER_REALM_URL}" ]]; then
    pass "issuer: the discovery document through the edge names ${ISSUER_REALM_URL}, the front URL, as its issuer"
  else
    fail "issuer: the discovery document names '$(clean_lines "${issuer}")' as its issuer, not ${ISSUER_REALM_URL}: a token's iss would not match what the Claims API pins"
  fi
}

check_issuer_keys() {
  local url="${ISSUER_REALM_URL}/protocol/openid-connect/certs" count
  if ! issuer_get "${url}"; then
    fail "issuer: ${url} did not answer: ${issuer_error}"
    return
  fi
  if [[ "${issuer_status}" != 200 ]]; then
    fail "issuer: ${url} -> ${issuer_status}, expected 200"
    return
  fi
  count="$(jq -r '[.keys[]? | select(.use == "sig" and .alg == "RS256")] | length' <<<"${issuer_body}" 2>/dev/null)" || count=""
  if [[ "${count}" =~ ^[0-9]+$ ]] && ((count >= 1)); then
    pass "issuer: the key document through the edge holds ${count} RS256 signing key(s)"
  else
    fail "issuer: the key document through the edge holds no RS256 signing key (or is not a key document)"
  fi
}

# issuer_edge_404 WHAT PATH...: one line. Each PATH through the edge must be the
# EDGE's own 404: the status 404 and an empty body (a 404 that Keycloak answers has
# a body, so a route that forwards the path would not pass as one that does not).
issuer_edge_404() {
  local what=$1 path wrong=""
  shift
  for path in "$@"; do
    if ! issuer_get "${ISSUER_FRONT_URL}${path}"; then
      fail "issuer: ${ISSUER_FRONT_URL}${path} did not answer: ${issuer_error}"
      return
    fi
    if [[ "${issuer_status}" != 404 ]]; then
      wrong+="${wrong:+, }${path} -> ${issuer_status}"
    elif [[ -n "${issuer_body}" ]]; then
      wrong+="${wrong:+, }${path} -> 404 with a body, which is Keycloak's and not the edge's"
    fi
  done
  if [[ -z "${wrong}" ]]; then
    pass "issuer: ${what} through the edge are the edge's own 404, with an empty body: $*"
  else
    fail "issuer: expected the edge's own 404 for ${what}, got ${wrong}: the route forwards more than the sign-in flow needs"
  fi
}

check_issuer_closed() {
  local paths
  read -r -a paths <<<"${ISSUER_CLOSED_PATHS}"
  issuer_edge_404 "the admin console, the master realm, the staff realm's account console and its client registration" "${paths[@]}"
}

# issuer_local_path LOCATION: the path (with any query) of a redirect that stays on
# this host, on stdout: LOCATION when it starts with one slash, or the front URL's
# path when it starts with the front URL. Nothing for any other host or scheme.
issuer_local_path() {
  case "$1" in
    //*) ;;
    /*) printf '%s' "$1" ;;
    "${ISSUER_FRONT_URL}"/*) printf '%s' "${1#"${ISSUER_FRONT_URL}"}" ;;
  esac
}

# issuer_in_allowed_prefix PATH: 0 when PATH, without its query, is under a prefix
# the route forwards (a path equal to a prefix without its last slash counts).
issuer_in_allowed_prefix() {
  local path="${1%%\?*}" prefix prefixes
  read -r -a prefixes <<<"${ISSUER_ALLOWED_PREFIXES}"
  for prefix in "${prefixes[@]}"; do
    [[ "${path}/" == "${prefix}"* ]] && return 0
  done
  return 1
}

# check_issuer_climbing: a way of climbing to the master realm passes when the
# answer through the edge is EITHER the edge's own empty 404, OR a 307 with an
# empty body whose Location is a path on this host that is under no prefix the
# route forwards and that, fetched as written, is the edge's own empty 404 (seen
# on kind, run KR1: for an escaped slash the edge unescapes, normalises and
# redirects by itself, and nothing reaches Keycloak). Anything else fails and
# names the form and what came back: a 200, a 30x to an allowed prefix or to
# another host, a redirect whose target answers anything but the edge's 404, a 404
# with a body, a 400.
check_issuer_climbing() {
  local paths path target gone="" redirected="" wrong=""
  read -r -a paths <<<"${ISSUER_CLIMBING_PATHS}"
  for path in "${paths[@]}"; do
    if ! issuer_get "${ISSUER_FRONT_URL}${path}"; then
      fail "issuer: ${ISSUER_FRONT_URL}${path} did not answer: ${issuer_error}"
      return
    fi
    if [[ "${issuer_status}" == 404 && -z "${issuer_body}" ]]; then
      gone+="${gone:+ }${path}"
    elif [[ "${issuer_status}" == 404 ]]; then
      wrong+="${wrong:+, }${path} -> 404 with a body, which is Keycloak's and not the edge's"
    elif [[ "${issuer_status}" == 307 && -z "${issuer_body}" ]]; then
      target="$(issuer_local_path "${issuer_location}")"
      if [[ -z "${target}" ]]; then
        wrong+="${wrong:+, }${path} -> 307 to '${issuer_location}', which is not a path on this host"
      elif issuer_in_allowed_prefix "${target}"; then
        wrong+="${wrong:+, }${path} -> 307 to ${target}, a prefix the route forwards"
      elif ! issuer_get "${ISSUER_FRONT_URL}${target}"; then
        wrong+="${wrong:+, }${path} -> 307 to ${target}, which did not answer: ${issuer_error}"
      elif [[ "${issuer_status}" != 404 ]]; then
        wrong+="${wrong:+, }${path} -> 307 to ${target}, which answers ${issuer_status}"
      elif [[ -n "${issuer_body}" ]]; then
        wrong+="${wrong:+, }${path} -> 307 to ${target}, which answers 404 with a body, Keycloak's and not the edge's"
      else
        redirected+="${redirected:+, }${path} -> ${target}"
      fi
    else
      wrong+="${wrong:+, }${path} -> ${issuer_status}$([[ -z "${issuer_body}" ]] || printf ' with a body')"
    fi
  done
  if [[ -n "${wrong}" ]]; then
    fail "issuer: a way of climbing to the master realm, sent as written, is not the edge's 404 or a redirect to one: ${wrong}"
  else
    pass "issuer: the ways of climbing to the master realm, sent as written, reach nothing: the edge's own empty 404 for [${gone:-none}]; a 307 with an empty body to a path that is itself the edge's own empty 404 for [${redirected:-none}]"
  fi
}

# check_issuer_inside: from the Claims API's pod, the discovery document by the
# Service's name. The edge's lines cannot tell a missing --hostname (the Host header
# they send gives the front name all the same); a pod asking at the Service's name
# can, and it also proves the policy that admits the Claims API, and the path.
check_issuer_inside() {
  local found answer err_file
  if ! found="$(deployed_services)"; then
    fail "issuer: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if ! grep -qx '[^/]*/claims-api' <<<"${found}"; then
    skip "issuer: the Claims API is not deployed (make deploy), so the issuer is not asked from inside the cluster"
    return
  fi
  err_file="$(mktemp)"
  if answer="$(kctl -n meridian exec deploy/claims-api -- python -c "${ISSUER_INSIDE_PROBE}" \
    "${ISSUER_SERVICE_URL}/.well-known/openid-configuration" 2>"${err_file}")"; then
    answer="$(clean_lines "${answer}")"
    if [[ "${answer}" == "${ISSUER_REALM_URL}" ]]; then
      pass "issuer: from the Claims API's pod, the discovery document fetched as ${ISSUER_SERVICE_URL} names the front URL ${ISSUER_REALM_URL} as its issuer (the policy, the path and --hostname hold)"
    else
      fail "issuer: from the Claims API's pod the discovery document names '${answer}' as its issuer, not ${ISSUER_REALM_URL}: a token's iss would not match what the Claims API pins"
    fi
  else
    fail "issuer: the Claims API's pod could not fetch the discovery document as ${ISSUER_SERVICE_URL}: $(clean_lines "${answer}") $(clean_lines "$(<"${err_file}")" | cut -c 1-300)"
  fi
  rm -f "${err_file}"
}

check_issuer() {
  if ! identity_on; then
    skip "issuer: the sign-in issuer (Keycloak) is an add-on and it is off; MERIDIAN_IDENTITY=keycloak make up switches it on"
    return
  fi
  check_issuer_pod
  check_issuer_discovery
  check_issuer_keys
  check_issuer_closed
  check_issuer_climbing
  check_issuer_inside
}
