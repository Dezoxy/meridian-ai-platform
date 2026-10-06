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
# A cell may not hold a pipe, not even an escaped one or one in backticks: a
# row that splits into more or fewer than five cells makes the board one this
# hook cannot read. So do a second table of another shape, a first table with
# other columns, a "Target:" with no number (or a number of more than six
# digits) and a row before the header. A table of the same five columns may
# follow: its header row is skipped. A board the hook cannot read is said to
# the session in the same once-only block as an idle lane, never skipped in
# silence: exit 0 shows the session nothing of standard error.
#
# What it is not: a check of the board and not of the work. The board is what
# the session wrote. It catches a lane the session forgot to fill; it does not
# catch a board that says "out now" falsely, and a session can pass it by
# lowering Target or by stopping a second time.
#
# What it covers: Claude Code only (.codex/hooks.json has no Stop entry, so
# Codex never runs it), once per user turn (stop_hook_active stays true for the
# rest of a continuation, so a second stop in the same turn passes, and a new
# turn checks again), and a checkout that has a board: the board is untracked,
# so a session started in a fresh worktree has none and no check.
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
# A broken board must not trap a session either: it blocks once and the next
# stop passes. What is not the board's fault exits 0 with nothing on standard
# output (one line on standard error): no jq, a file that cannot be read, input
# that is not a JSON object.
set -uo pipefail

# The most idle lanes a reason names, and the most bytes of a lane's name and
# of its step it quotes: a huge board must not make a reason that jq cannot
# take as an argument.
MAX_NAMED=10
MAX_LANE=40
MAX_STEP=60

note() { printf 'check-lanes: %s\n' "$*" >&2; }

block() {
  jq -nc --arg r "$1" '{decision: "block", reason: $r}'
  exit 0
}

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
# Empty input and null are not an event either: jq reads both without error.
fields="$(printf '%s' "$input" |
  jq -r 'if type == "object"
    then [(.stop_hook_active // false | tostring), (.agent_id // "")] | @tsv
    else error("not an object") end' 2>/dev/null)" || fields=""
if [ -z "$fields" ]; then
  note "the hook input is not a JSON object; the lane check is skipped"
  exit 0
fi
IFS=$'\t' read -r active agent <<<"$fields"
[ "$active" = "true" ] && exit 0
[ -n "${agent:-}" ] && exit 0

# One record a line, fields apart by the unit separator (an empty field must
# survive, which a tab would not): kind, lane, step.
parsed="$(LC_ALL=C awk -v max_lane="$MAX_LANE" -v max_step="$MAX_STEP" '
function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
function empty(s) {
  return s == "" || s == "-" || s == "\342\200\224" || s == "\342\200\223"
}
function say(kind, a, b) {
  a = substr(a, 1, max_lane); b = substr(b, 1, max_step)
  gsub(/[\t\037]/, " ", a); gsub(/[\t\037]/, " ", b)
  print kind "\037" a "\037" b
}
{ sub(/\r$/, "") }
# "Target: 3", "**Target:** 3", "**Target**: 3", "__Target:__ 3".
/^[ \t]*[*_]*Target[*_]*:/ {
  pos = 0
  v = $0
  sub(/^[ \t]*[*_]*Target[*_]*:[*_ \t]*/, "", v)
  if (v !~ /^[0-9]+/) { say("bad", "Target is not a number"); exit }
  sub(/[^0-9].*$/, "", v)
  if (length(v) > 6) { say("bad", "Target is too large"); exit }
  say("target", v)
  next
}
/^[ \t]*\|/ {
  pos++            # which row of its table this is; any other line ends a table
  row = $0
  sub(/^[ \t]*\|/, "", row)
  sub(/\|[ \t]*$/, "", row)
  n = split(row, c, "|")
  delimiter = 1
  for (i = 1; i <= n; i++) {
    c[i] = trim(c[i])
    if (c[i] !~ /^:?-+:?$/) delimiter = 0
  }
  # A delimiter row has no letters and is the second row of its table: a row
  # of dashes anywhere else is a lane with nothing in it.
  if (delimiter && pos == 2) next
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
  # The header again, at the head of a second table of the same columns.
  if (tolower(c[1]) == "lane" && tolower(c[2]) == "step" &&
      tolower(c[3]) == "out now" && tolower(c[4]) == "idle because" &&
      tolower(c[5]) == "next") next
  if (!empty(c[3])) say("running", c[1], c[2])
  else if (empty(c[4])) say("idle", c[1], c[2])
  else say("reasoned", c[1], c[2])
  next
}
{ pos = 0 }
' "$board" 2>/dev/null)" || {
  note ".claude/lanes.md could not be read; the lane check is skipped"
  exit 0
}

target="" running=0 bad="" idle_count=0 names=""
while IFS=$'\037' read -r kind a b; do
  case "$kind" in
    bad) bad="$a"; break ;;
    target) target="$a" ;;
    running) running=$((running + 1)) ;;
    idle)
      idle_count=$((idle_count + 1))
      if [ "$idle_count" -le "$MAX_NAMED" ]; then
        names+="${names:+, }lane ${a:-?}${b:+ ($b)}"
      fi
      ;;
  esac
done <<<"$parsed"
if [ -n "$bad" ]; then
  block "Lane check (.claude/lanes.md): this is not a board the hook can read ($bad), so no lane was checked. A cell may not hold '|', not even escaped or in backticks; a second table must have the same five columns. Fix the board and stop again: this check blocks once."
fi

problems=()
if [ "$idle_count" -gt 0 ]; then
  [ "$idle_count" -gt "$MAX_NAMED" ] && names+=" and $((idle_count - MAX_NAMED)) more"
  verb="is"
  [ "$idle_count" -gt 1 ] && verb="are"
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
block "$reason"
