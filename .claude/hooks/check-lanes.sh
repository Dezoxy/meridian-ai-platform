#!/usr/bin/env bash
# Stop: names a lane of parallel work that sits idle, once, before the main
# session stops. The owner, 2026-10-06: "How can we aware of this? So you notice
# everytime when we dont have the enough steps in paralel".
#
# It reads one file and nothing else: .claude/lanes.md under the project
# directory ($CLAUDE_PROJECT_DIR, else the working one), a Markdown table the
# session keeps by hand, not tracked (.gitignore):
#
#   Target: 3                       (optional: how many lanes should run)
#   | Lane | Step | Out now | Idle because | Next |
#
# A row is RUNNING when "Out now" holds something (an agent is out, a run is
# going), IDLE WITH A REASON when it is empty and "Idle because" is not, and
# IDLE otherwise. An empty cell is empty, or "-", or a dash. The stop is
# blocked when a row is IDLE, or when fewer rows are RUNNING than Target; a
# row that is idle with a reason passes the first test and still does not
# count as running for the second. No file, no check. It runs no git and no
# kubectl and finishes in milliseconds.
#
# What it is not: a check of the board and not of the work. The board is what
# the session wrote. It catches a lane the session forgot to fill; it does not
# catch a board that says "out now" falsely, and a session can pass it by
# lowering Target.
#
# What Claude Code's documentation says (https://code.claude.com/docs/en/hooks,
# read 2026-10-06) and what this script relies on:
#   Stop: "Runs when the main Claude Code agent has finished responding."
#     A subagent finishing is another event, SubagentStop, so this hook should
#     not fire for one; the agent_id test below is a second line, not the first.
#   Input: "The `stop_hook_active` field is `true` when Claude Code is already
#     continuing as a result of a stop hook. Check this value or process the
#     transcript to avoid blocking on a condition that will never resolve."
#     So the hook blocks once and lets the next stop through. Claude Code also
#     applies "an 8-consecutive-continuation cap" whatever a hook says.
#   Subagent: "`agent_id` ... Present only when the hook fires inside a
#     subagent call."
#   Block: `decision` is "block" to prevent Claude from stopping ("Omit to
#     allow Claude to stop"); `reason` is "Required when `decision` is
#     `"block"`. Tells Claude why it should continue". "A hook that blocks by
#     exiting 2 routes the same way as `reason`"; this script prints the JSON
#     and exits 0, as guard-bash.sh does.
#   Async: "Async hooks can't block or control Claude's behavior", so this is
#     its own synchronous entry beside the async session-end one, with a short
#     timeout. A hook that reaches its timeout is cancelled and "renders no
#     decision": a slow board never traps a session.
# A broken board must not trap a session either: whatever it cannot read, it
# prints one line to standard error and exits 0.
set -uo pipefail

note() { printf 'check-lanes: %s\n' "$*" >&2; }

input="$(cat)"
board="${CLAUDE_PROJECT_DIR:-$PWD}/.claude/lanes.md"
[ -f "$board" ] || exit 0
if ! command -v jq >/dev/null 2>&1; then
  note "jq is not installed; the lane check is skipped"
  exit 0
fi
if [ ! -r "$board" ]; then
  note ".claude/lanes.md cannot be read; the lane check is skipped"
  exit 0
fi

# stop_hook_active true: it already blocked once. agent_id set: a subagent.
fields="$(printf '%s' "$input" |
  jq -r '[(.stop_hook_active // false | tostring), (.agent_id // "")] | @tsv' 2>/dev/null)" || {
  note "the hook input is not JSON; the lane check is skipped"
  exit 0
}
IFS=$'\t' read -r active agent <<<"$fields"
[ "$active" = "true" ] && exit 0
[ -n "${agent:-}" ] && exit 0

# One record a line, fields apart by the unit separator (an empty field must
# survive, which a tab would not): kind, lane, step.
parsed="$(LC_ALL=C awk '
function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
function empty(s) {
  return s == "" || s == "-" || s == "\342\200\224" || s == "\342\200\223"
}
function say(kind, a, b) { gsub(/[\t\037]/, " ", a); gsub(/[\t\037]/, " ", b)
  print kind "\037" a "\037" b }
{ sub(/\r$/, "") }
/^[ \t]*Target:/ {
  v = $0
  sub(/^[ \t]*Target:[ \t]*/, "", v)
  if (v !~ /^[0-9]+/) { say("bad", "Target is not a number"); exit }
  sub(/[^0-9].*$/, "", v)
  if (length(v) > 6) { say("bad", "Target is too large"); exit }
  say("target", v)
  next
}
/^[ \t]*\|/ {
  row = $0
  sub(/^[ \t]*\|/, "", row)
  sub(/\|[ \t]*$/, "", row)
  n = split(row, c, "|")
  delimiter = 1
  for (i = 1; i <= n; i++) {
    c[i] = trim(c[i])
    if (c[i] !~ /^:?-+:?$/) delimiter = 0
  }
  if (delimiter) next
  if (!header) {
    header = 1
    if (n != 5 || tolower(c[1]) != "lane" || tolower(c[2]) != "step" ||
        tolower(c[3]) != "out now" || tolower(c[4]) != "idle because" ||
        tolower(c[5]) != "next") {
      say("bad", "the first table is not Lane | Step | Out now | Idle because | Next")
      exit
    }
    next
  }
  if (n != 5) { say("bad", "a row has " n " cells, not 5"); exit }
  if (!empty(c[3])) say("running", c[1], c[2])
  else if (empty(c[4])) say("idle", c[1], c[2])
  else say("reasoned", c[1], c[2])
}
' "$board" 2>/dev/null)" || {
  note ".claude/lanes.md could not be read; the lane check is skipped"
  exit 0
}

target="" running=0 bad="" idle=()
while IFS=$'\037' read -r kind a b; do
  case "$kind" in
    bad) bad="$a"; break ;;
    target) target="$a" ;;
    running) running=$((running + 1)) ;;
    idle) idle+=("lane ${a:-?}${b:+ ($b)}") ;;
  esac
done <<<"$parsed"
if [ -n "$bad" ]; then
  note ".claude/lanes.md is not a board this hook reads ($bad); the lane check is skipped"
  exit 0
fi

problems=()
if [ "${#idle[@]}" -gt 0 ]; then
  names="$(printf '%s, ' "${idle[@]}")"
  names="${names%, }"
  verb="is"
  [ "${#idle[@]}" -gt 1 ] && verb="are"
  problems+=("$names $verb idle with nothing out and no reason given: put a contract out for it, or write why it waits in its 'Idle because' cell")
fi
if [ -n "$target" ] && [ "$running" -lt "$((10#$target))" ]; then
  problems+=("$running of $((10#$target)) lanes are running (Target: $((10#$target))): start another lane, or lower Target if fewer are wanted")
fi
[ "${#problems[@]}" -gt 0 ] || exit 0

reason="Lane check (.claude/lanes.md): "
for i in "${!problems[@]}"; do
  [ "$i" -gt 0 ] && reason+="; "
  reason+="${problems[$i]}"
done
reason+=". Fix the board and stop again: this check blocks once."
jq -nc --arg r "$reason" '{decision: "block", reason: $r}'
exit 0
