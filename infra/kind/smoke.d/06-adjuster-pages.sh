# shellcheck shell=bash
#   6. adjuster pages: three lines, through the edge as demo.sh reaches the Claims
#                 API. The queue page answers 200 with the Content-Security-
#                 Policy (frame-ancestors 'none', default-src 'none') and the
#                 synthetic-data line; a decision posted with a foreign Origin is
#                 refused with 403 before any claim is looked up; the claimant's
#                 start page answers 200 with the same policy and the banner's
#                 fictional-data sentence (T-04), and changes no claim. Skipped
#                 while the Meridian services are not deployed (`make deploy`).
#                 With MERIDIAN_SIGNIN=staff (S021, Y4b) the queue's line is
#                 another: with no session the queue answers 303 to the app's
#                 /auth/start, which answers 303 to the issuer's authorization
#                 address (the redirect is followed once, and neither query is
#                 printed), and two unsigned posts of a decision are refused by
#                 the guard itself, still in that one line: the page's form with
#                 the page's own Origin answers 401, and the JSON route's answers
#                 401 with WWW-Authenticate: Bearer (CLM-9999 need not exist);
#                 the other two lines are the same.

readonly ADJUSTER_QUEUE_URL=http://claims.meridian.localhost:8088/adjuster/claims
readonly ADJUSTER_DECISION_URL=http://claims.meridian.localhost:8088/adjuster/claims/CLM-9999/decision
# The first sentence of the banner every page carries (templates/base.html).
readonly ADJUSTER_BANNER="Synthetic data only."
readonly CLAIMANT_START_URL=http://claims.meridian.localhost:8088/claimant/claims
# The second sentence of the claimant banner (templates/claimant_base.html).
readonly CLAIMANT_BANNER="Every name, address and description you enter must be fictional: never a real person's."

# With the staff sign-in on: where the queue sends a person with no session (the
# app's own start, on the queue's origin) and where that sends them (the realm's
# authorization address on the front URL, values/signin.yaml's authorizationUrl; a
# test holds the two equal).
readonly ADJUSTER_SIGNIN_START_PATH=/auth/start
# The JSON route's decision, which the guard refuses with a 401 and a Bearer
# challenge (S021 L5): the same host, CLM-9999 not a claim.
readonly ADJUSTER_SIGNIN_JSON_URL=http://claims.meridian.localhost:8088/claims/CLM-9999/decision
readonly ADJUSTER_SIGNIN_AUTHORIZATION=http://id.meridian.localhost:8088/realms/meridian-staff/protocol/openid-connect/auth

# adjuster_location HEADERS_FILE: the value of the Location header that curl -D
# wrote, on stdout, printable ASCII only (no carriage return); nothing when there
# is none.
adjuster_location() {
  { grep -i '^location:' "$1" || true; } | head -n 1 | sed -E 's/^[^:]*:[[:space:]]*//' |
    LC_ALL=C tr -cd '[:print:]'
}

# adjuster_absolute LOCATION: the address a Location names, on stdout: a path (one
# slash, not two) is taken under the queue's origin, anything else as it is.
adjuster_absolute() {
  local origin=${ADJUSTER_QUEUE_URL%/adjuster/claims}
  if [[ "$1" == /* && "$1" != //* ]]; then
    printf '%s%s' "${origin}" "$1"
  else
    printf '%s' "$1"
  fi
}

# adjuster_without_query TEXT: TEXT on one clean line with every query string
# (from a ? to the next blank) removed: what a redirect carries is the sign-in's
# state and is never printed.
adjuster_without_query() {
  clean_lines "$(printf '%s' "$1" | sed -E 's/\?[^[:space:]]*//g')"
}

# adjuster_queue_signed_out HEADERS_FILE: the queue's line with the staff sign-in
# on, and no session: 303 to the app's start; that, followed once, 303 to the
# issuer's authorization address. It prints one PASS or FAIL line, and no query.
adjuster_queue_signed_out() {
  local headers_file=$1 status location start origin=${ADJUSTER_QUEUE_URL%/adjuster/claims}
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -D "${headers_file}" -o /dev/null \
    -w '%{http_code}' "${ADJUSTER_QUEUE_URL}" 2>&1)"; then
    fail "adjuster pages: ${ADJUSTER_QUEUE_URL} did not answer: $(adjuster_without_query "${status}")"
    return
  fi
  status="$(clean_lines "${status}")"
  location="$(adjuster_location "${headers_file}")"
  if [[ "${status}" != 303 ]]; then
    fail "adjuster pages: ${ADJUSTER_QUEUE_URL} with the staff sign-in on and no session expected 303 to ${ADJUSTER_SIGNIN_START_PATH}, got ${status}"
    return
  fi
  if [[ -z "${location}" ]]; then
    fail "adjuster pages: ${ADJUSTER_QUEUE_URL} answered 303 and has no Location header"
    return
  fi
  start="$(adjuster_absolute "${location}")"
  if [[ "${start%%\?*}" != "${origin}${ADJUSTER_SIGNIN_START_PATH}" ]]; then
    fail "adjuster pages: ${ADJUSTER_QUEUE_URL} answered 303 to $(adjuster_without_query "${start}"), not to the app's ${ADJUSTER_SIGNIN_START_PATH}"
    return
  fi
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -D "${headers_file}" -o /dev/null \
    -w '%{http_code}' "${start}" 2>&1)"; then
    fail "adjuster pages: ${origin}${ADJUSTER_SIGNIN_START_PATH} did not answer: $(adjuster_without_query "${status}")"
    return
  fi
  status="$(clean_lines "${status}")"
  location="$(adjuster_location "${headers_file}")"
  if [[ "${status}" != 303 ]]; then
    fail "adjuster pages: ${origin}${ADJUSTER_SIGNIN_START_PATH} expected 303 to the issuer, got ${status}"
    return
  fi
  if [[ -z "${location}" ]]; then
    fail "adjuster pages: ${origin}${ADJUSTER_SIGNIN_START_PATH} answered 303 and has no Location header"
    return
  fi
  if [[ "${location}" != "${ADJUSTER_SIGNIN_AUTHORIZATION}" && "${location}" != "${ADJUSTER_SIGNIN_AUTHORIZATION}?"* ]]; then
    fail "adjuster pages: ${origin}${ADJUSTER_SIGNIN_START_PATH} answered 303 to $(adjuster_without_query "${location}"), not to the issuer's authorization address ${ADJUSTER_SIGNIN_AUTHORIZATION}"
    return
  fi
  adjuster_guard_refuses_posts "${headers_file}" || return 0
  pass "adjuster pages: ${ADJUSTER_QUEUE_URL} with no session -> 303 to ${ADJUSTER_SIGNIN_START_PATH}, which answers 303 to ${ADJUSTER_SIGNIN_AUTHORIZATION}; a form post of a decision with the page's own Origin -> 401, and a JSON post of one to ${ADJUSTER_SIGNIN_JSON_URL} -> 401 with WWW-Authenticate: Bearer (the staff sign-in is on; neither redirect's query is printed, and no post changes a claim)"
}

# adjuster_guard_refuses_posts HEADERS_FILE: with the sign-in on, a post of a
# decision that carries no session is refused by the guard itself. The GET above
# proves the redirect only: a cross-site post is refused by the origin check before
# the guard runs, so it proves nothing about the guard. Two posts, neither of which
# changes a claim (CLM-9999 need not exist: the guard refuses before any lookup):
# the page's own form with the page's own Origin, which passes the origin check and
# must be 401; and the JSON route's, which must be 401 with `WWW-Authenticate:
# Bearer`. Prints one FAIL line naming which post and returns 1 when one is not
# refused; returns 0 and prints nothing otherwise.
adjuster_guard_refuses_posts() {
  local headers_file=$1 origin=${ADJUSTER_QUEUE_URL%/adjuster/claims} status
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -o /dev/null -w '%{http_code}' \
    -H "Origin: ${origin}" --data 'decision=approve&run=' "${ADJUSTER_DECISION_URL}" 2>&1)"; then
    fail "adjuster pages: the form post of a decision to ${ADJUSTER_DECISION_URL} did not answer: $(adjuster_without_query "${status}")"
    return 1
  fi
  status="$(clean_lines "${status}")"
  if [[ "${status}" != 401 ]]; then
    fail "adjuster pages: the form post of a decision to ${ADJUSTER_DECISION_URL} with the page's own Origin expected 401 from the guard, got ${status}"
    return 1
  fi
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -D "${headers_file}" -o /dev/null \
    -w '%{http_code}' -H 'Content-Type: application/json' --data '{"decision": "approve"}' \
    "${ADJUSTER_SIGNIN_JSON_URL}" 2>&1)"; then
    fail "adjuster pages: the JSON post of a decision to ${ADJUSTER_SIGNIN_JSON_URL} did not answer: $(adjuster_without_query "${status}")"
    return 1
  fi
  status="$(clean_lines "${status}")"
  if [[ "${status}" != 401 ]]; then
    fail "adjuster pages: the JSON post of a decision to ${ADJUSTER_SIGNIN_JSON_URL} expected 401 from the guard, got ${status}"
    return 1
  fi
  if ! grep -qiE '^www-authenticate:[[:space:]]*Bearer([^[:alnum:]]|$)' "${headers_file}"; then
    fail "adjuster pages: the JSON post of a decision to ${ADJUSTER_SIGNIN_JSON_URL} answered 401 without the header WWW-Authenticate: Bearer"
    return 1
  fi
}

# ── 6. adjuster pages ────────────────────────────────────────────────────────
# The pages are served by the Claims API (S016), through the edge, in the form
# demo.sh reaches it. No line changes a claim: the second is refused by the
# origin check before any lookup, so CLM-9999 need not exist, and the third is a
# GET of the claimant's start page (S049).
check_adjuster_pages() {
  local found headers_file body_file status csp missing=""
  # Skipped only when no Meridian Deployment exists, as in check_tools.
  if ! found="$(deployed_services)"; then
    fail "adjuster pages: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "adjuster pages: the Meridian services are not deployed (make deploy)"
    return
  fi

  headers_file="$(mktemp)"
  body_file="$(mktemp)"
  if signin_on; then
    adjuster_queue_signed_out "${headers_file}"
  elif ! status="$(curl -q --noproxy '*' -sS -m 10 -D "${headers_file}" -o "${body_file}" \
    -w '%{http_code}' "${ADJUSTER_QUEUE_URL}" 2>&1)"; then
    fail "adjuster pages: ${ADJUSTER_QUEUE_URL} did not answer: $(clean_lines "${status}")"
  else
    status="$(clean_lines "${status}")"
    # Header names are case-insensitive; the lines end in a carriage return.
    csp="$(grep -i '^content-security-policy:' "${headers_file}" | LC_ALL=C tr -cd '[:print:]' || true)"
    [[ "${status}" == 200 ]] || missing="status 200 (got ${status})"
    [[ "${csp}" == *"frame-ancestors 'none'"* ]] ||
      missing="${missing:+${missing}, }header Content-Security-Policy with frame-ancestors 'none'"
    [[ "${csp}" == *"default-src 'none'"* ]] ||
      missing="${missing:+${missing}, }header Content-Security-Policy with default-src 'none'"
    grep -qF "${ADJUSTER_BANNER}" "${body_file}" ||
      missing="${missing:+${missing}, }text \"${ADJUSTER_BANNER}\""
    if [[ -z "${missing}" ]]; then
      pass "adjuster pages: ${ADJUSTER_QUEUE_URL} -> 200 with a Content-Security-Policy of frame-ancestors 'none' and default-src 'none', and the synthetic-data line"
    else
      fail "adjuster pages: ${ADJUSTER_QUEUE_URL} lacks: ${missing}"
    fi
  fi
  rm -f "${headers_file}" "${body_file}"

  if ! status="$(curl -q --noproxy '*' -sS -m 10 -o /dev/null -w '%{http_code}' \
    -H 'Origin: http://attacker.example' --data 'decision=approve' \
    "${ADJUSTER_DECISION_URL}" 2>&1)"; then
    fail "adjuster pages: ${ADJUSTER_DECISION_URL} did not answer: $(clean_lines "${status}")"
  else
    status="$(clean_lines "${status}")"
    if [[ "${status}" == 403 ]]; then
      pass "adjuster pages: a decision posted with Origin http://attacker.example -> 403, refused before any lookup"
    else
      fail "adjuster pages: a decision posted with Origin http://attacker.example expected 403, got ${status}"
    fi
  fi

  # The claimant's start page (S049), served by the same app with the same
  # headers; its banner asks for fictional data (T-04). A GET, so no claim.
  headers_file="$(mktemp)"
  body_file="$(mktemp)"
  missing=""
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -D "${headers_file}" -o "${body_file}" \
    -w '%{http_code}' "${CLAIMANT_START_URL}" 2>&1)"; then
    fail "adjuster pages: ${CLAIMANT_START_URL} did not answer: $(clean_lines "${status}")"
  else
    status="$(clean_lines "${status}")"
    csp="$(grep -i '^content-security-policy:' "${headers_file}" | LC_ALL=C tr -cd '[:print:]' || true)"
    [[ "${status}" == 200 ]] || missing="status 200 (got ${status})"
    [[ "${csp}" == *"frame-ancestors 'none'"* ]] ||
      missing="${missing:+${missing}, }header Content-Security-Policy with frame-ancestors 'none'"
    [[ "${csp}" == *"default-src 'none'"* ]] ||
      missing="${missing:+${missing}, }header Content-Security-Policy with default-src 'none'"
    grep -qF "${CLAIMANT_BANNER}" "${body_file}" ||
      missing="${missing:+${missing}, }text \"${CLAIMANT_BANNER}\""
    if [[ -z "${missing}" ]]; then
      pass "adjuster pages: ${CLAIMANT_START_URL} -> 200 with a Content-Security-Policy of frame-ancestors 'none' and default-src 'none', and the fictional-data line (T-04)"
    else
      fail "adjuster pages: ${CLAIMANT_START_URL} lacks: ${missing}"
    fi
  fi
  rm -f "${headers_file}" "${body_file}"
}
