# shellcheck shell=bash
#  11. alert rules and health dashboard: four lines, read-only (S062), run
#                 after the first ten (check 12 follows it, S072). Three
#                 lines read Prometheus' /api/v1/rules through
#                 Grafana's datasource proxy (the port-forward of check 4) for
#                 the PrometheusRule `meridian` that `make up` applies. The
#                 five groups of infra/kind/alerts/meridian.yaml are loaded and
#                 every rule of the loaded meridian.* groups has health ok (a
#                 FAIL names the rule, its health and Prometheus' lastError,
#                 cut to 120 printable ASCII characters). The loaded group and
#                 rule names equal the file's (the file's names are read with
#                 awk by their indentation, and a test keeps that equal to a
#                 YAML parser's reading; a cluster that runs an older file
#                 says which groups and rules differ), and so do each rule's
#                 expression and an alert's `for` (S073, K4: the same line, and
#                 the FAIL names the rule and which of the two differs, never
#                 an expression's text; it says to run make deploy or make up).
#                 Prometheus returns the parsed expression, not the file's
#                 text, so the two sides go through one filter (rules_changed)
#                 that collapses whitespace, writes every duration in
#                 milliseconds ([24h] as Prometheus prints it, [1d], is the
#                 same), removes all whitespace and sorts the matchers between
#                 a pair of braces. It is not a PromQL parser. What the
#                 comparison of expressions does not see: a change that only
#                 moves whitespace, including inside a string literal (a label
#                 value "a b" and "ab" are one); a label value or regular
#                 expression with a comma, a brace or a duration-like word in
#                 it (the matchers are split at every comma, and a word such as
#                 "1h" at the start of a word is read as a duration); an
#                 expression written in another way that is the same one (a
#                 quote that is not a double quote, or `sum(x) by (a)` for
#                 `sum by (a) (x)`, which Prometheus is expected to print in
#                 the second form: neither is in the file, nor was tried, and
#                 the check would call it a difference); the rule's labels and
#                 annotations and a `keep_firing_for` (not read); and that the
#                 expression is right. No alert of those
#                 groups is firing (a FAIL names it; a pending alert is not a
#                 failure, and the line names it). The three wait up to 120 s
#                 for the groups to load and be evaluated (a rule not yet
#                 evaluated has health unknown, which is not ok); one FAIL
#                 line replaces them when Prometheus does not answer with
#                 status success, or when the PrometheusRule is not there
#                 (`make up` applies it: the line says to run it), and so does
#                 any other failure to look for it. An answer of status success
#                 without a usable data.groups list fails each of the three
#                 lines (the dashboard line still runs), and with no meridian.*
#                 group loaded the third line fails too: there is nothing to be
#                 firing, so it cannot tell. The fourth line
#                 is check 5's dashboard check for the health dashboard (uid
#                 meridian-platform-health): the provisioned copy has the
#                 queries of dashboards/platform-health.json, and every query
#                 of it runs in Prometheus with a range of an hour. No query
#                 is left out: ${__range_s} becomes 3600, and a query that
#                 finds no series on a quiet cluster still answers with status
#                 success. What it does not prove: that the series a rule or a
#                 panel names exist (an absent series leaves a rule healthy
#                 and quiet, and a panel with no data is a success; that stays
#                 by hand, in docs/operations/README.md), that a threshold is
#                 right, or that anyone would be told (kind has no
#                 Alertmanager). It adds one request for the rules, one for
#                 the dashboard and its ten queries: a few seconds.

# The health dashboard (infra/kind/dashboards/platform-health.json), checked as
# the cost dashboard is, by check_dashboard (check 11).
readonly HEALTH_DASHBOARD_UID=meridian-platform-health
readonly HEALTH_DASHBOARD_FILE="${KIND_DIR}/dashboards/platform-health.json"
# The alert rules check (11): the PrometheusRule `meridian` that up.sh applies
# from ALERT_RULES_FILE; Prometheus names a rule's group by the file's group
# name, and every group of the file starts with ALERT_GROUP_PREFIX (a test keeps
# it so). A rule's lastError is cut to ALERT_ERROR_LENGTH characters.
readonly ALERT_RULES_NAMESPACE=observability
readonly ALERT_RULES_OBJECT=meridian
readonly ALERT_RULES_FILE="${KIND_DIR}/alerts/meridian.yaml"
readonly ALERT_GROUP_PREFIX="meridian."
readonly ALERT_ERROR_LENGTH=120
readonly ALERT_RULES_PATH=/api/datasources/proxy/uid/prometheus/api/v1/rules
rules_body=""      # set by fetch_rules

# ── 11. alert rules and health dashboard ─────────────────────────────────────
# What the file holds, read with awk by the file's indentation (the group
# names at four spaces, the rules at eight; a test compares both with a YAML
# parser's reading): tree_groups prints the group names, tree_rules prints
# "<group><TAB><rule>" per alert and recording rule, both sorted bytewise.
tree_groups() {
  awk '/^    - name: / { print $3 }' "${ALERT_RULES_FILE}" | LC_ALL=C sort
}

tree_rules() {
  awk '/^    - name: / { group = $3 } /^        - (alert|record): / { print group "\t" $3 }' \
    "${ALERT_RULES_FILE}" | LC_ALL=C sort
}

# cluster_groups BODY and cluster_rules BODY: the same two lists for the
# meridian.* groups in Prometheus' /api/v1/rules answer BODY.
cluster_groups() {
  jq -r --arg prefix "${ALERT_GROUP_PREFIX}" \
    '.data.groups[].name | select(startswith($prefix))' <<<"$1" | LC_ALL=C sort
}

cluster_rules() {
  jq -r --arg prefix "${ALERT_GROUP_PREFIX}" \
    '.data.groups[] | select(.name | startswith($prefix)) | .name as $group
      | .rules[] | $group + "\t" + .name' <<<"$1" | LC_ALL=C sort
}

# tree_exprs: what the file holds for the comparison of expressions, read by the
# same indentation: "<group><TAB><rule><TAB><for><TAB><expression>" per alert and
# recording rule, in the file's order, the `for` as written ("" when there is
# none) and the expression (a literal block, `expr: |`) on one line, its lines
# joined by a space.
tree_exprs() {
  awk '
    function flush() { if (rule != "") printf "%s\t%s\t%s\t%s\n", group, rule, hold, expr; rule = "" }
    /^    - name: / { flush(); group = $3; next }
    /^        - (alert|record): / { flush(); rule = $3; hold = ""; expr = ""; in_expr = 0; next }
    /^          expr: [|]$/ { in_expr = 1; next }
    /^          for: / { in_expr = 0; hold = $2; next }
    in_expr && /^            / { line = $0; sub(/^ +/, "", line); expr = expr (expr == "" ? "" : " ") line; next }
    in_expr && /^$/ { next }
    { in_expr = 0 }
    END { flush() }
  ' "${ALERT_RULES_FILE}"
}

# rules_changed BODY: "<group><TAB><rule> (expression)", "(for)" or "(expression
# and for)" for each rule that is in both the file and the meridian.* groups of
# Prometheus' answer BODY and differs in its expression or, for an alert, its
# `for`; the names only. Prometheus returns the parsed expression, not the file's
# text: on one line, the matchers of a selector sorted by label name, a duration
# in its largest units ([24h] as [1d]). So both sides go through the same
# canon: whitespace collapsed (so a duration is told from the end of a word),
# every duration literal at the start of a word as milliseconds, all whitespace
# removed, and the matchers between each pair of braces sorted. A rule absent
# on one side is the names' business (check_rules_names), not named here. A
# rule of the file with no `for` is 0, as the API says.
rules_changed() {
  local tree
  tree="$(tree_exprs | jq -Rsc 'split("\n") | map(select(. != "") | split("\t")
    | {group: .[0], rule: .[1], hold: .[2], expr: .[3]})')" || return 1
  jq -r --arg prefix "${ALERT_GROUP_PREFIX}" --slurpfile tree <(printf '%s' "${tree}") '
    def canon:
      gsub("\\s+"; " ")
      | gsub("(?<![\\w.])(?<d>(?:[0-9]+(?:ms|[smhdwy]))+)(?![\\w])";
          (.d | [scan("([0-9]+)(ms|[smhdwy])")]
            | map((.[0] | tonumber) * ({"ms": 1, "s": 1000, "m": 60000, "h": 3600000,
                "d": 86400000, "w": 604800000, "y": 31536000000}[.[1]]))
            | add | tostring) + "ms")
      | gsub("\\s+"; "")
      | gsub("\\{(?<m>[^{}]*)\\}"; "{" + (.m | split(",") | sort | join(",")) + "}");
    [.data.groups[] | select(.name | startswith($prefix)) | .name as $group | .rules[]
      | {key: ($group + "\t" + .name), query: (.query // ""), alerting: (.type == "alerting"),
         seconds: (.duration // 0)}] as $cluster
    | $tree[0][] | . as $file
    | ($cluster[] | select(.key == ($file.group + "\t" + $file.rule))) as $loaded
    | [(if ($file.expr | canon) != ($loaded.query | canon) then "expression" else empty end),
       (if $loaded.alerting
          and (($file.hold | if . == "" then "0s" else . end | canon) != (($loaded.seconds * 1000 | tostring) + "ms"))
        then "for" else empty end)] as $differs
    | select($differs != [])
    | "\($file.group)\t\($file.rule) (\($differs | join(" and ")))"' <<<"$1"
}

# name_list: the lines of stdin as one line of names ("group/rule" for a tab),
# separated by ", ", without any byte that is not printable ASCII.
name_list() {
  tr '\t' '/' | LC_ALL=C tr -cd '[:print:]\n' | paste -sd ',' - | sed 's/,/, /g'
}

# check_rules_object: 0 when the PrometheusRule is there. Otherwise one FAIL line
# and 1: for kubectl's NotFound (`make up` applies the object, so on any cluster
# this repository makes it is missing for a reason, and the line says what to
# run) and for any other error.
check_rules_object() {
  local err_file
  err_file="$(mktemp)"
  if kctl -n "${ALERT_RULES_NAMESPACE}" get prometheusrule "${ALERT_RULES_OBJECT}" \
    -o name >/dev/null 2>"${err_file}"; then
    rm -f "${err_file}"
    return 0
  fi
  if grep -q '(NotFound)' "${err_file}"; then
    fail "alert rules: the PrometheusRule ${ALERT_RULES_OBJECT} is not in ${ALERT_RULES_NAMESPACE}, and make up applies it from infra/kind/alerts/meridian.yaml: run make up"
  else
    fail "alert rules: could not look for the PrometheusRule ${ALERT_RULES_OBJECT} in ${ALERT_RULES_NAMESPACE} (kubectl said: $(clean_lines "$(<"${err_file}")"))"
  fi
  rm -f "${err_file}"
  return 1
}

# fetch_rules: Prometheus' /api/v1/rules, through Grafana's datasource proxy, left
# in ${rules_body}. It polls (up to POLL_TIMEOUT) until every group of the file
# is loaded and none of the loaded rules is still unevaluated (a rule's health is
# "unknown" until its group's first evaluation), then takes that answer. When that
# does not happen the answer of one more request is judged as it is. Returns 1
# after one FAIL line when Prometheus does not answer with status success. An
# answer of status success that has no list of groups in the form the later
# lines read (data.groups: objects with a string name and a list of rules, which
# are objects) leaves ${rules_body} empty and returns 0: each line that needed
# the groups then prints its own FAIL (rules_missing_groups), and the dashboard
# line after them still runs.
fetch_rules() {
  local wanted status
  wanted="$(tree_groups | jq -R . | jq -sc .)" || wanted="[]"
  if [[ "${wanted}" == "[]" ]]; then
    fail "alert rules: found no group in ${ALERT_RULES_FILE}"
    return 1
  fi
  # shellcheck disable=SC2154  # grafana_url is set by open_grafana, poll_result by poll (shared.sh)
  if poll "select(.status == \"success\")
    | [.data.groups[] | select(.name | startswith(\"${ALERT_GROUP_PREFIX}\"))] as \$loaded
    | select((${wanted} - [\$loaded[].name] | length) == 0
      and ([\$loaded[].rules[].health] | all(. != \"unknown\")))
    | tojson" "${grafana_url}${ALERT_RULES_PATH}"; then
    rules_body="${poll_result}"
    return 0
  fi
  rules_body="$(gcurl "${grafana_url}${ALERT_RULES_PATH}" 2>/dev/null || true)"
  status="$(jq -r '.status // empty' <<<"${rules_body}" 2>/dev/null || true)"
  if [[ "${status}" != success ]]; then
    # shellcheck disable=SC2154  # poll_error is set by poll (shared.sh)
    fail "alert rules: Prometheus did not answer ${ALERT_RULES_PATH} with status success after ${POLL_TIMEOUT}s (last answer: ${poll_error})"
    return 1
  fi
  if ! jq -e '(.data.groups | type == "array")
    and all(.data.groups[]; type == "object" and (.name | type == "string")
      and (.rules | type == "array") and all(.rules[]; type == "object"))' \
    <<<"${rules_body}" >/dev/null 2>&1; then
    rules_body=""
  fi
}

# rules_missing_groups BODY WHAT: 0 after one FAIL line for WHAT when BODY is
# empty (fetch_rules found no usable data.groups in the answer); 1, silently,
# when there is a body to read.
rules_missing_groups() {
  [[ -z "$1" ]] || return 1
  fail "alert rules: ${2} cannot be checked: Prometheus' answer to ${ALERT_RULES_PATH} has no data.groups list of groups with names and rules"
}

# check_rules_loaded BODY: the file's groups are loaded and every rule of the
# loaded meridian.* groups has health ok. A rule that is not is named with its
# health and its lastError, newlines and tabs as spaces and cut to
# ALERT_ERROR_LENGTH characters.
check_rules_loaded() {
  local body=$1 missing unhealthy problems="" total
  if rules_missing_groups "${body}" "whether the groups are loaded and the rules healthy"; then
    return
  fi
  missing="$(LC_ALL=C comm -23 <(tree_groups) <(cluster_groups "${body}") | name_list)"
  unhealthy="$(jq -r --arg prefix "${ALERT_GROUP_PREFIX}" --argjson cut "${ALERT_ERROR_LENGTH}" \
    '.data.groups[] | select(.name | startswith($prefix)) | .rules[] | select(.health != "ok")
      | .name + " (health " + (.health // "unknown")
        + (if (.lastError // "") != "" then ": " + (.lastError | gsub("[\\r\\n\\t]"; " ") | .[0:$cut]) else "" end)
        + ")"' <<<"${body}" | name_list)"
  [[ -z "${missing}" ]] || problems="group(s) ${missing} of infra/kind/alerts/meridian.yaml not loaded in Prometheus (run make up)"
  [[ -z "${unhealthy}" ]] || problems+="${problems:+; }rules not healthy: ${unhealthy}"
  if [[ -n "${problems}" ]]; then
    fail "alert rules: ${problems}"
    return
  fi
  total="$(cluster_rules "${body}" | wc -l)"
  pass "alert rules: the $(tree_groups | wc -l | tr -d ' ') groups of infra/kind/alerts/meridian.yaml are loaded in Prometheus and all ${total//[[:space:]]/} rules in them are healthy"
}

# check_rules_names BODY: the loaded meridian.* groups and their rules are the
# file's, by name, in both directions, and each rule's expression and `for` are
# the file's (rules_changed says how they are compared). A cluster that runs
# another rule file says which groups and rules differ, by name.
check_rules_names() {
  local body=$1 not_loaded not_in_file changed differences="" total
  if rules_missing_groups "${body}" "whether the loaded rules are the file's"; then
    return
  fi
  not_loaded="$({
    LC_ALL=C comm -23 <(tree_groups) <(cluster_groups "${body}")
    LC_ALL=C comm -23 <(tree_rules) <(cluster_rules "${body}")
  } | name_list)"
  not_in_file="$({
    LC_ALL=C comm -13 <(tree_groups) <(cluster_groups "${body}")
    LC_ALL=C comm -13 <(tree_rules) <(cluster_rules "${body}")
  } | name_list)"
  changed="$(rules_changed "${body}" | name_list)" || {
    fail "alert rules: could not compare the expressions and the for of the loaded rules with infra/kind/alerts/meridian.yaml (jq could not read one side)"
    return
  }
  [[ -z "${not_loaded}" ]] || differences="not loaded: ${not_loaded}"
  [[ -z "${not_in_file}" ]] || differences+="${differences:+; }not in the file: ${not_in_file}"
  [[ -z "${changed}" ]] || differences+="${differences:+; }expression or for differs: ${changed}"
  if [[ -n "${differences}" ]]; then
    fail "alert rules: the rules Prometheus runs are not infra/kind/alerts/meridian.yaml's (a cluster that runs an older file: run make deploy or make up): ${differences}"
    return
  fi
  total="$(tree_rules | wc -l)"
  pass "alert rules: the loaded rules are the file's: the same $(tree_groups | wc -l | tr -d ' ') groups and ${total//[[:space:]]/} rule names, each with its expression and its for"
}

# alerts_in_state STATE BODY: the names of the rules of the meridian.* groups
# that have an alert in STATE (firing or pending), sorted, one per line.
alerts_in_state() {
  jq -r --arg prefix "${ALERT_GROUP_PREFIX}" --arg state "$1" \
    '.data.groups[] | select(.name | startswith($prefix)) | .rules[]
      | select(any(.alerts[]?; .state == $state)) | .name' <<<"$2" | LC_ALL=C sort -u
}

# check_rules_firing BODY: no alert of the meridian.* groups is firing. A pending
# alert has not yet lasted its `for`: it is not a failure, and the line names it.
# With no meridian.* group loaded there is nothing to be firing, so the line
# would pass for nothing: it is a FAIL that says it cannot tell.
check_rules_firing() {
  local body=$1 firing pending count
  if rules_missing_groups "${body}" "whether a Meridian alert is firing"; then
    return
  fi
  if [[ -z "$(cluster_groups "${body}")" ]]; then
    fail "alert rules: cannot tell whether a Meridian alert is firing: no group of ${ALERT_GROUP_PREFIX}* is loaded in Prometheus (run make up)"
    return
  fi
  firing="$(alerts_in_state firing "${body}")"
  pending="$(alerts_in_state pending "${body}")"
  if [[ -n "${firing}" ]]; then
    fail "alert rules: Meridian alert(s) firing: $(name_list <<<"${firing}") (the health dashboard's table and Prometheus' Alerts page show them)"
    return
  fi
  if [[ -z "${pending}" ]]; then
    pass "alert rules: no Meridian alert is firing (none pending)"
    return
  fi
  count="$(wc -l <<<"${pending}")"
  pass "alert rules: no Meridian alert is firing (${count//[[:space:]]/} pending, not a failure: $(name_list <<<"${pending}"))"
}

# The rules, then the health dashboard as check 5 reads the cost dashboard.
check_alert_rules() {
  open_grafana || return 0 # otherwise it printed the one FAIL line
  if check_rules_object && fetch_rules; then
    check_rules_loaded "${rules_body}"
    check_rules_names "${rules_body}"
    check_rules_firing "${rules_body}"
  fi
  check_dashboard "${HEALTH_DASHBOARD_UID}" "${HEALTH_DASHBOARD_FILE}"
}
