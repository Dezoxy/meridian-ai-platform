# shellcheck shell=bash
#  13. issuer: four lines when the sign-in issuer add-on is on (S021, Y2b), one SKIP
#                 line when it is off (MERIDIAN_IDENTITY empty, the default: Keycloak
#                 is an add-on that `make up` makes only with MERIDIAN_IDENTITY=
#                 keycloak; infra/kind/README.md, "The sign-in issuer"). On, in order:
#                 the pod in `identity` is Ready and runs the pinned image (the
#                 Deployment's image is KEYCLOAK_IMAGE of pins.env, and the pod's
#                 imageID ends in its digest); the discovery document, through the
#                 edge, names the FRONT URL (http://id.meridian.localhost:8088/
#                 realms/meridian-staff) as its issuer; the key document, through the
#                 edge, holds at least one RS256 signing key; and /admin/ and
#                 /realms/master/ through the edge are 404, because the route
#                 forwards two prefixes of the staff realm and nothing else. The
#                 requests are plain GETs of public documents: no line reads a
#                 Secret, signs anyone in or asks for a token. The lines say what
#                 they prove and not more: that a browser's sign-in works, that
#                 the Claims API reaches the key URL and that the pod keeps its
#                 keys are not checked here. Not run on the cluster yet.
#                 A pod that is not up yet fails the first line and the others
#                 run all the same; an add-on that was never made fails them all.

readonly ISSUER_FRONT_URL=http://id.meridian.localhost:8088
readonly ISSUER_REALM_URL="${ISSUER_FRONT_URL}/realms/meridian-staff"
# What the last issuer_get left: the status, the body and, when curl failed, why.
issuer_status=""
issuer_body=""
issuer_error=""

# ── 13. issuer ───────────────────────────────────────────────────────────────
# issuer_get URL: GET URL through the edge, as the other checks reach the Claims
# API. Returns 1 when curl failed (issuer_error holds why); otherwise leaves the
# status in ${issuer_status} and the body in ${issuer_body}. Nothing is followed.
issuer_get() {
  local out status
  out="$(mktemp)"
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -o "${out}" -w '%{http_code}' "$1" 2>&1)"; then
    issuer_error="$(clean_lines "${status}")"
    rm -f "${out}"
    return 1
  fi
  issuer_status="$(clean_lines "${status}")"
  issuer_body="$(<"${out}")"
  rm -f "${out}"
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
        | if $ready != 1 then "the pod is not Ready"
          elif $spec != $image then "the pod runs another image than the pin in pins.env"
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

check_issuer_closed() {
  local path wrong=""
  for path in /admin/ /realms/master/; do
    if ! issuer_get "${ISSUER_FRONT_URL}${path}"; then
      fail "issuer: ${ISSUER_FRONT_URL}${path} did not answer: ${issuer_error}"
      return
    fi
    [[ "${issuer_status}" == 404 ]] || wrong+="${wrong:+, }${path} -> ${issuer_status}"
  done
  if [[ -z "${wrong}" ]]; then
    pass "issuer: /admin/ and /realms/master/ through the edge are 404 (the route forwards the staff realm's two prefixes and nothing else)"
  else
    fail "issuer: expected 404 from the edge, got ${wrong}: the route forwards more than the staff realm's two prefixes"
  fi
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
}
