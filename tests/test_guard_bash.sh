#!/usr/bin/env bash
# Regression test for .claude/hooks/guard-bash.sh. Each line of
# guard-bash-cases.jsonl carries a sample command and the expected decision
# (deny, ask or none), and may carry the working directory the harness passes
# in its input ("cwd"; a line without one is as before) and a "note" that the
# runner does not read. Run: bash tests/test_guard_bash.sh
#
# The rows whose note says "false alarm by design" are searches (grep, rg and
# the like with a quoted word) and a pseudo-terminal word near the wrapper's
# name that the guard denies or asks about though they do not use what they
# name: the guard reads a quoted word as a use of it (a search, an echo, a
# commit message is the exception that is blanked). Search with the Grep tool.
# A try at emptying a search's quoted pattern (S036 T3b) was taken out because
# it hid a command substitution, a fake grep inside a literal and a file operand
# after -e or -f; the rows marked "finding N" hold those and the other cases the
# second security pass found.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
hook="$here/../.claude/hooks/guard-bash.sh"
fail=0
n=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  n=$((n + 1))
  expect="$(jq -r '.expect' <<<"$line")"
  got="$(jq -c '{cwd:(.cwd // null),tool_input:{command:.command}}' <<<"$line" | bash "$hook" \
    | jq -r '.hookSpecificOutput.permissionDecision // "none"')"
  [ -z "$got" ] && got=none
  if [ "$got" != "$expect" ]; then
    echo "FAIL case $n: expected $expect, got $got"
    fail=1
  else
    echo "ok   case $n: $expect"
  fi
done < "$here/guard-bash-cases.jsonl"

# The length bounds. A hook that runs past its timeout does not block the call,
# so a command too long to read in time must be answered at once. Two bounds:
# what the patterns read, after the heredoc pass (guard_max_bytes, 8192), and
# what was typed (guard_max_typed_bytes, 16384). The commands are built here,
# byte by byte, because a case line that long cannot be read in review.
# `ask_for` runs the hook on a command and compares the answer.
bound=8192
typed_bound=16384
ask_for() { # $1=name $2=expected decision $3=command
  local got
  got="$(jq -nc --arg c "$3" '{tool_input:{command:$c}}' | bash "$hook" \
    | jq -r '.hookSpecificOutput.permissionDecision // "none"')"
  [ -z "$got" ] && got=none
  if [ "$got" != "$2" ]; then
    echo "FAIL $1: expected $2, got $got"
    fail=1
  else
    echo "ok   $1: $2"
  fi
}
padding() { # $1=count, $2=the character: that many of it, all in one word
  head -c "$1" < /dev/zero | tr '\0' "$2"
}
prefix='echo '
ask_for "a command of the bound passes" none "${prefix}$(padding $((bound - ${#prefix})) a)"
ask_for "a command one byte over the bound asks" ask "${prefix}$(padding $((bound - ${#prefix} + 1)) a)"
# 4097 two-byte characters are 8194 bytes: the bound counts bytes, not characters.
ask_for "a command over the bound in bytes asks though it is under it in characters" ask \
  "${prefix}$(for _ in $(seq 4097); do printf '\303\251'; done)"
ask_for "a short command of two-byte characters passes" none "${prefix}$(for _ in $(seq 100); do printf '\303\251'; done)"
# Before any pattern: a long command that holds a denied form is asked, not read.
ask_for "a long command that holds a denied form asks before any pattern runs" ask \
  "terraform destroy ${prefix}$(padding "$bound" a)"
heredoc="$(for _ in $(seq 4000); do printf 'a: b\n'; done)"
long_heredoc="cat > x.yaml <<'EOF'
${heredoc}
EOF"
ask_for "a heredoc of 24 KB asks" ask "$long_heredoc"
long_heredoc="cat > x.yaml <<'EOF'
${heredoc}${heredoc}${heredoc}
EOF"
ask_for "a heredoc of 72 KB asks" ask "$long_heredoc"
# A heredoc only written to a file is dropped before the second bound reads the
# command, so it passes up to the typed bound, and asks one byte over it.
heredoc_head="cat > x.yaml <<'EOF'"$'\n'
heredoc_tail=$'\n'"EOF"
heredoc_room=$((typed_bound - ${#heredoc_head} - ${#heredoc_tail}))
ask_for "a heredoc written to a file, of exactly the typed bound, passes" none \
  "${heredoc_head}$(padding "$heredoc_room" a)${heredoc_tail}"
ask_for "a heredoc written to a file, one byte over the typed bound, asks" ask \
  "${heredoc_head}$(padding $((heredoc_room + 1)) a)${heredoc_tail}"
ask_for "a heredoc fed to bash, longer than the bound in what it holds, asks" ask \
  "bash <<'EOF'"$'\n'"$(padding $((bound + 1)) a)"$'\n'"EOF"
ask_for "a heredoc written to a file, with a denied command after it, is denied" deny \
  "${heredoc_head}$(padding 12000 a)${heredoc_tail}"$'\n'"git push --force"
# What the user reads when it asks.
reason="$(jq -nc --arg c "${prefix}$(padding "$bound" a)" '{tool_input:{command:$c}}' | bash "$hook" \
  | jq -r '.hookSpecificOutput.permissionDecisionReason // ""')"
case "$reason" in
  *"too long"*"Write tool"*) echo "ok   the length ask says to write a script with the Write tool" ;;
  *)
    echo "FAIL the length ask names neither the length nor the Write tool: $reason"
    fail=1
    ;;
esac
# The segment bound (guard_max_segments, 1000). The rules read a command one
# segment at a time (split on newlines, ;, &&, || and |), and the cost follows
# the number of segments, not the bytes: 8192 one-word segments fill 16384
# bytes and took 3 s of CPU with the machine idle. A command with more parts
# than the bound asks, before any rule runs.
segment_bound=1000
segments() { # $1=count, $2=the last segment: $1 segments in all, joined by ;
  local i out=""
  for ((i = 1; i < $1; i++)); do out+="a;"; done
  printf '%s%s' "$out" "$2"
}
ask_for "a command of exactly the segment bound passes" none "$(segments "$segment_bound" a)"
ask_for "a command of exactly the segment bound that holds a denied segment is denied" deny \
  "$(segments "$segment_bound" 'git push --force')"
ask_for "a command one segment over the bound asks" ask "$(segments $((segment_bound + 1)) a)"
ask_for "a command over the segment bound that holds a denied segment asks before any rule runs" ask \
  "$(segments $((segment_bound + 1)) 'git push --force')"
ask_for "a command of newline-separated parts over the bound asks" ask \
  "$(for _ in $(seq $((segment_bound + 1))); do printf 'a\n'; done)"
ask_for "a command of piped parts over the bound asks" ask \
  "$(for _ in $(seq "$segment_bound"); do printf 'a|'; done)a"
# 2500 lines of 5 bytes are 12500 bytes: written to a file the body is stripped
# and the command has one segment; fed to bash the body stays and every line is
# a segment.
heredoc_15k="$(for _ in $(seq 2500); do printf 'a: b\n'; done)"
ask_for "a heredoc of 12 KB written to a file passes" none "cat > x.yaml <<'EOF'
${heredoc_15k}
EOF"
# 1500 lines of 5 bytes are 7500 bytes, under the byte bound and over the
# segment bound: it is the segment bound that answers.
heredoc_7k="$(for _ in $(seq 1500); do printf 'a: b\n'; done)"
ask_for "a heredoc of 7 KB with more lines than the segment bound fed to bash asks" ask "bash <<'EOF'
${heredoc_7k}
EOF"
ask_for "a heredoc of 12 KB fed to bash asks, over the byte bound" ask "bash <<'EOF'
${heredoc_15k}
EOF"
ask_for "a script of 200 lines fed to bash passes" none "bash <<'EOF'
$(for _ in $(seq 200); do printf 'echo a\n'; done)
EOF"
ask_for "a command of 8 KB in one segment passes" none "${prefix}$(padding 8000 a)"
reason="$(jq -nc --arg c "$(segments $((segment_bound + 1)) a)" '{tool_input:{command:$c}}' | bash "$hook" \
  | jq -r '.hookSpecificOutput.permissionDecisionReason // ""')"
case "$reason" in
  *"$((segment_bound + 1)) parts"*"at most ${segment_bound}"*"Write tool"*)
    echo "ok   the segment ask gives the count, the bound and the Write tool"
    ;;
  *)
    echo "FAIL the segment ask does not give the count, the bound and the Write tool: $reason"
    fail=1
    ;;
esac
# CPU time of the hook's process on a 70 KB command, not the wall clock: the
# wall clock also counts the time the process waited for a CPU. The answer is
# there before any pattern runs, so it takes a few hundredths of a second; the
# limit is far above that and far below a ten-second timeout.
big_input="$(mktemp)"
trap 'rm -f "$big_input"' EXIT
# One bound for every CPU check below: 3 s of CPU, whatever the shape. The
# shapes take 0.05 to 0.85 s of CPU on the development machine, and one of the
# older bounds (1.5 s for the worst shape under the byte bound) took 1.736 s on
# the hosted CI runner once, where the same shape took 0.74 s here: a machine
# can be twice as slow, and a fixed small bound then fails for the machine and
# not for the hook. A hook that has come to cost more than the bytes it reads
# should (a pattern gone quadratic) lands near 10 s, so 3 s still catches it.
# The watchdog (5 of the hook's 10 s) carries the protection; these are
# regression guards. A ratio against a baseline shape was the other form and
# is not used: its baseline is itself a measurement of the loaded machine.
cpu_bound=3
# The command is nine thousand segments, not a heredoc: a heredoc written to a
# file is stripped before any pattern, and cheap to read even without the bound.
jq -nc --arg c "$(for _ in $(seq 9000); do printf 'echo a; '; done)" \
  '{tool_input:{command:$c}}' > "$big_input"
TIMEFORMAT='%U %S'
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
  echo "ok   a 70 KB command takes ${cpu_seconds} s of CPU, under ${cpu_bound}"
else
  echo "FAIL a 70 KB command takes ${cpu_seconds} s of CPU, not under ${cpu_bound}"
  fail=1
fi
# The worst shape under the byte bound: 8192 one-word segments are 16384 bytes
# and took 3 s of CPU before the segment bound; they are answered at once now.
jq -nc --arg c "$(segments 8192 a)" '{tool_input:{command:$c}}' > "$big_input"
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
  echo "ok   16384 bytes of one-word segments take ${cpu_seconds} s of CPU, under ${cpu_bound}"
else
  echo "FAIL 16384 bytes of one-word segments take ${cpu_seconds} s of CPU, not under ${cpu_bound}"
  fail=1
fi

# The parts are counted the way the loops split them: && and || count, a CRLF
# line is a part, and a continuation joins lines into one part (unless a ; or
# an && stands before it).
parts() { # $1=count, $2=the separator: that many one-word parts
  local i out=""
  for ((i = 1; i <= $1; i++)); do
    out+="a"
    [ "$i" -ge "$1" ] || out+="$2"
  done
  printf '%s' "$out"
}
ask_for "a command of exactly the segment bound joined by && passes" none "$(parts "$segment_bound" '&&')"
ask_for "a command one part over the bound joined by && asks" ask "$(parts $((segment_bound + 1)) '&&')"
ask_for "a command of exactly the segment bound joined by || passes" none "$(parts "$segment_bound" '||')"
ask_for "a command one part over the bound joined by || asks" ask "$(parts $((segment_bound + 1)) '||')"
ask_for "a command of exactly the segment bound of CRLF lines passes" none "$(parts "$segment_bound" $'\r\n')"
ask_for "a command one part over the bound of CRLF lines asks" ask "$(parts $((segment_bound + 1)) $'\r\n')"
ask_for "lines joined by a continuation are one part and pass" none "$(parts $((segment_bound + 500)) $' \\\n')"
ask_for "a ; before a continuation still cuts the part, so the count asks" ask "$(parts $((segment_bound + 1)) $';\\\n')"

# The watchdog (H1). The byte and segment bounds keep the hook short for the
# shapes they were measured on; the watchdog answers `ask` whatever the shape,
# so that a hook past its timeout (which Claude Code lets through unread) is
# not the way past the guard. Its time and the timeout in settings.json are
# held together here: the watchdog must leave guard_watchdog_margin seconds
# for the one command in flight that its trap waits for.
settings_timeout="$(jq -r '[.hooks.PreToolUse[].hooks[] | select(.command | contains("guard-bash.sh")) | .timeout] | first' \
  "$here/../.claude/settings.json")"
watchdog_default="$(sed -n 's/^guard_watchdog_default=\([0-9][0-9]*\)$/\1/p' "$hook")"
guard_watchdog_margin=4
if [[ "$settings_timeout" =~ ^[0-9]+$ && "$watchdog_default" =~ ^[0-9]+$ ]] \
  && [ "$watchdog_default" -ge 1 ] \
  && [ $((watchdog_default + guard_watchdog_margin)) -le "$settings_timeout" ]; then
  echo "ok   the watchdog's ${watchdog_default} s and a margin of ${guard_watchdog_margin} s fit the hook's timeout of ${settings_timeout} s"
else
  echo "FAIL the watchdog (${watchdog_default:-unset} s) plus ${guard_watchdog_margin} s does not fit the hook's timeout (${settings_timeout:-unset} s)"
  fail=1
fi
# The worst shape the review found: `kubectl ` and then `&(` to just under the
# byte bound is one segment, and the patterns that read it are quadratic.
amp_shape() { # $1=the bytes: kubectl, then &( up to just under that
  printf 'kubectl '
  printf '&(%.0s' $(seq $((($1 - 8) / 2)))
}
amp_command="$(amp_shape "$bound")"
jq -nc --arg c "$amp_command" '{tool_input:{command:$c}}' > "$big_input"
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
  echo "ok   the worst shape under the byte bound takes ${cpu_seconds} s of CPU, under ${cpu_bound}"
else
  echo "FAIL the worst shape under the byte bound takes ${cpu_seconds} s of CPU, not under ${cpu_bound}"
  fail=1
fi
ask_for "the worst shape followed by a denied segment is denied, not skipped" deny \
  "$(amp_shape $((bound - 32)))"'; git push --force'
# A slow path forced: the watchdog is set to a tenth of a second (it can only be
# shortened, never lengthened, through GUARD_WATCHDOG_SECONDS) and the worst
# shape cannot be read that fast. A distinctive value lets pgrep find the
# helper's sleep afterwards.
slow="0.1$$"
slow_out="$(GUARD_WATCHDOG_SECONDS="$slow" bash "$hook" < "$big_input")"
slow_decision="$(jq -r '.hookSpecificOutput.permissionDecision // "none"' <<<"$slow_out")"
slow_reason="$(jq -r '.hookSpecificOutput.permissionDecisionReason // ""' <<<"$slow_out")"
if [ "$slow_decision" = ask ]; then
  echo "ok   a command the hook cannot read in time asks: $slow_decision"
else
  echo "FAIL a command the hook cannot read in time: expected ask, got $slow_decision"
  fail=1
fi
case "$slow_reason" in
  *"ran out of time"*"NOT read"*"Write tool"*) echo "ok   the watchdog's ask says the guard ran out of time and the command was not read" ;;
  *)
    echo "FAIL the watchdog's ask does not say it ran out of time, that the command was NOT read, and to use the Write tool: $slow_reason"
    fail=1
    ;;
esac
if [ "$(wc -l <<<"$slow_out")" -eq 1 ]; then
  echo "ok   the watchdog's answer is one JSON object"
else
  echo "FAIL the watchdog's answer is not one line: $slow_out"
  fail=1
fi
# Nothing of the hook is left: the helper's sleep is gone, after an answer by
# the watchdog and after an ordinary one. pgrep is given a second to see it go.
no_sleep_left() { # $1=the sleep's argument
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f "sleep $1" > /dev/null || return 0
    sleep 0.1
  done
  return 1
}
if no_sleep_left "$slow"; then
  echo "ok   no helper is left after the watchdog answered"
else
  echo "FAIL a helper process is left after the watchdog answered"
  fail=1
fi
quiet="5.1$$"
jq -nc --arg c "ls -la" '{tool_input:{command:$c}}' | GUARD_WATCHDOG_SECONDS="$quiet" bash "$hook" > /dev/null
if no_sleep_left "$quiet"; then
  echo "ok   no helper is left after an ordinary answer"
else
  echo "FAIL a helper process is left after an ordinary answer"
  fail=1
fi
jq -nc --arg c "git push --force" '{tool_input:{command:$c}}' | GUARD_WATCHDOG_SECONDS="$quiet" bash "$hook" > /dev/null
if no_sleep_left "$quiet"; then
  echo "ok   no helper is left after a decision"
else
  echo "FAIL a helper process is left after a decision"
  fail=1
fi
# The time can be shortened through the variable and not lengthened: a value
# over the default, or one that is not a positive number, leaves the default.
# The hook is started on a pipe that stays open, so that it is waiting for its
# command: the helper is then armed and its sleep can be read from ps. That the
# helper is armed before the hook reads anything is the same check.
fifo_dir="$(mktemp -d)"
mkfifo "$fifo_dir/in"
armed_sleep() { # $1=the value of GUARD_WATCHDOG_SECONDS: the sleep's argument
  local hook_pid
  GUARD_WATCHDOG_SECONDS="$1" bash "$hook" < "$fifo_dir/in" > /dev/null &
  hook_pid=$!
  exec 3> "$fifo_dir/in"
  sleep 0.3
  ps -eo pid=,ppid=,args= | awk -v root="$hook_pid" '
    { parent[$1] = $2; line[$1] = $0 }
    END {
      for (p in parent) {
        if (parent[parent[p]] == root && line[p] ~ / sleep /) {
          sub(/^ *[0-9]+ +[0-9]+ +(sleep )?/, "", line[p]);
          print line[p]
        }
      }
    }'
  exec 3>&-
  wait "$hook_pid" || true
}
for value_and_want in "|${watchdog_default}" "0.9$$|0.9$$" "$((watchdog_default + 1)).5$$|${watchdog_default}" "abc|${watchdog_default}" "0|${watchdog_default}" "-1|${watchdog_default}"; do
  got="$(armed_sleep "${value_and_want%%|*}")"
  if [ "$got" = "${value_and_want#*|}" ]; then
    echo "ok   GUARD_WATCHDOG_SECONDS='${value_and_want%%|*}' arms a sleep of ${got}, before the command is read"
  else
    echo "FAIL GUARD_WATCHDOG_SECONDS='${value_and_want%%|*}': expected a sleep of ${value_and_want#*|}, found '${got}'"
    fail=1
  fi
done
rm -f "$fifo_dir/in"
rmdir "$fifo_dir"

# What the user reads when a command is not read (M10): the length asks and the
# watchdog's ask all say that the command was not read and may hold a form the
# guard would deny, so that nobody approves one unread.
reason_of() { # $1=the command
  jq -nc --arg c "$1" '{tool_input:{command:$c}}' | bash "$hook" \
    | jq -r '.hookSpecificOutput.permissionDecisionReason // ""'
}
for name_and_command in "byte|${prefix}$(padding "$((bound + 1))" a)" "segment|$(segments $((segment_bound + 1)) a)"; do
  reason="$(reason_of "${name_and_command#*|}")"
  case "$reason" in
    *"NOT read"*"deny"*) echo "ok   the ${name_and_command%%|*} ask says that the command was NOT read and may hold a denied form" ;;
    *)
      echo "FAIL the ${name_and_command%%|*} ask does not say the command was NOT read and may hold a denied form: $reason"
      fail=1
      ;;
  esac
done
case "$slow_reason" in
  *"deny"*) echo "ok   the watchdog's ask says the unread command may hold a denied form" ;;
  *)
    echo "FAIL the watchdog's ask does not say the unread command may hold a denied form: $slow_reason"
    fail=1
    ;;
esac

# The hook's Python is not replaced from the working directory (N3). A re.py
# that exits quietly, lying where the hook runs, would make the heredoc pass
# return nothing and the rules read an empty command: python3 -I leaves the
# directory off sys.path. A python3 that does nothing and prints nothing (first
# on the PATH) is the other way the pass returns nothing, and the hook then
# keeps the command as typed.
stand_in="$(mktemp -d)"
printf 'import sys\nsys.exit(0)\n' > "$stand_in/re.py"
mkdir "$stand_in/bin"
printf '#!/bin/sh\nexit 0\n' > "$stand_in/bin/python3"
chmod +x "$stand_in/bin/python3"
decision_with() { # $1=name $2=expected $3=a directory to run in $4=a PATH prefix $5=command
  local got
  got="$(jq -nc --arg c "$5" '{tool_input:{command:$c}}' | (cd "$3" && PATH="$4:$PATH" bash "$hook") \
    | jq -r '.hookSpecificOutput.permissionDecision // "none"')"
  [ -z "$got" ] && got=none
  if [ "$got" != "$2" ]; then
    echo "FAIL $1: expected $2, got $got"
    fail=1
  else
    echo "ok   $1: $2"
  fi
}
decision_with "a re.py in the working directory does not disable a force push deny" deny "$stand_in" "$stand_in/nothing" \
  "git push --force"
decision_with "a re.py in the working directory does not disable terraform destroy" deny "$stand_in" "$stand_in/nothing" \
  "terraform destroy"
decision_with "a re.py in the working directory does not disable the Secret read deny" deny "$stand_in" "$stand_in/nothing" \
  "kubectl get secret x -o yaml"
decision_with "a re.py in the working directory leaves an ordinary command alone" none "$stand_in" "$stand_in/nothing" \
  "ls -la"
decision_with "a python3 that prints nothing does not disable a force push deny" deny "$here" "$stand_in/bin" \
  "git push --force"
decision_with "a python3 that prints nothing does not disable the Secret read deny" deny "$here" "$stand_in/bin" \
  "kubectl get secret x -o yaml"
decision_with "a python3 that prints nothing leaves an ordinary command alone" none "$here" "$stand_in/bin" \
  "ls -la"
rm -f "$stand_in/re.py" "$stand_in/bin/python3"
rmdir "$stand_in/bin" "$stand_in"

# The heredoc pass has no quadratic shape (N5): `tee <<EOF .` and a long run of
# dots, under the typed bound, cost 1.5 s of CPU with a pattern that took a run
# of non-blanks before the dot of a script name; they take a few hundredths now.
jq -nc --arg c "tee <<EOF .$(padding 16000 .)" '{tool_input:{command:$c}}' > "$big_input"
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
  echo "ok   16 KB of dots after a heredoc marker take ${cpu_seconds} s of CPU, under ${cpu_bound}"
else
  echo "FAIL 16 KB of dots after a heredoc marker take ${cpu_seconds} s of CPU, not under ${cpu_bound}"
  fail=1
fi

# A tool's name repeated thousands of times before a verb it reads: the cut of
# the text after a long match (a glob over a literal of that length) took 17 to
# 24 s of CPU at 8 KB, past the hook's timeout; a match over 256 bytes now counts
# as not helped and the time is a few tenths. Bytes of arguments up to the cap
# still let --help through.
ask_for "a printer with 100 bytes of flags before its verb passes with --help" none \
  "aws $(for _ in $(seq 50); do printf 'x '; done)sts get-session-token --help"
ask_for "a printer with 300 bytes of flags before its verb asks, --help or not" ask \
  "aws $(for _ in $(seq 150); do printf 'x '; done)sts get-session-token --help"
ask_for "a reader of a secret store with 300 bytes of flags before its verb is denied" deny \
  "aws $(for _ in $(seq 150); do printf 'x '; done)secretsmanager get-secret-value --help"
jq -nc --arg c "$(for _ in $(seq 2000); do printf 'aws '; done)secretsmanager get-secret-value" \
  '{tool_input:{command:$c}}' > "$big_input"
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
  echo "ok   2000 repetitions of a tool name before its verb take ${cpu_seconds} s of CPU, under ${cpu_bound}"
else
  echo "FAIL 2000 repetitions of a tool name before its verb take ${cpu_seconds} s of CPU, not under ${cpu_bound}"
  fail=1
fi

# The AWS rules (S036). The worst shape of them: a command that is nothing but
# `aws ` and then a read, so that it reaches the loop over aws calls with
# nothing denied before it (0.3 s measured on 2026-10-06 at 8 KB, load about 3),
# and one that is `make ` and then the plan target, which reaches the rules of
# the make targets last. Both are asked, not skipped.
aws_shape="$(for _ in $(seq 1950); do printf 'aws '; done)ec2 describe-instances"
jq -nc --arg c "$aws_shape" '{tool_input:{command:$c}}' > "$big_input"
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
  echo "ok   2000 repetitions of aws before a read take ${cpu_seconds} s of CPU, under ${cpu_bound}"
else
  echo "FAIL 2000 repetitions of aws before a read take ${cpu_seconds} s of CPU, not under ${cpu_bound}"
  fail=1
fi
make_shape="$(for _ in $(seq 1550); do printf 'make '; done)aws-plan"
jq -nc --arg c "$make_shape" '{tool_input:{command:$c}}' > "$big_input"
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
  echo "ok   1600 repetitions of make before aws-plan take ${cpu_seconds} s of CPU, under ${cpu_bound}"
else
  echo "FAIL 1600 repetitions of make before aws-plan take ${cpu_seconds} s of CPU, not under ${cpu_bound}"
  fail=1
fi
# The security pass found two shapes slower than these: `make ` 1550 times and
# then a target that only starts like the one the rules look for (0.5 s), and a
# run of `>` and then a dot-directory name that only starts like the AWS one
# (0.5 s). Neither matches, so both are read to the end.
near_miss_shape="$(for _ in $(seq 1550); do printf 'make '; done)aws-valid"
jq -nc --arg c "$near_miss_shape" '{tool_input:{command:$c}}' > "$big_input"
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
  echo "ok   1550 repetitions of make before a near-miss target take ${cpu_seconds} s of CPU, under ${cpu_bound}"
else
  echo "FAIL 1550 repetitions of make before a near-miss target take ${cpu_seconds} s of CPU, not under ${cpu_bound}"
  fail=1
fi
redirect_shape="echo $(padding 7900 '>').awsX"
jq -nc --arg c "$redirect_shape" '{tool_input:{command:$c}}' > "$big_input"
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
  echo "ok   a run of > before a near-miss dot-directory takes ${cpu_seconds} s of CPU, under ${cpu_bound}"
else
  echo "FAIL a run of > before a near-miss dot-directory takes ${cpu_seconds} s of CPU, not under ${cpu_bound}"
  fail=1
fi
ask_for "a command of 1950 aws words before a read asks and is read" ask "$aws_shape"
ask_for "the same, with a deletion in another part, is denied and not skipped" deny \
  "$(for _ in $(seq 1700); do printf 'aws '; done)ec2 describe-instances; aws eks delete-cluster --name stand-in"

# What the user reads (S036): the apply says it costs money and that the owner
# runs it where no session holds credentials, and the removal says it is the
# owner's.
reason="$(reason_of 'make aws-apply')"
case "$reason" in
  *"COST MONEY"*"owner"*"no session holds the credentials"*) echo "ok   the ask for make aws-apply says it costs money and who runs it" ;;
  *)
    echo "FAIL the ask for make aws-apply does not say it costs money, that the owner runs it and where: $reason"
    fail=1
    ;;
esac
reason="$(reason_of 'make aws-destroy')"
case "$reason" in
  *"owner's"*"terminal"*"no session holds the credentials"*) echo "ok   the deny for make aws-destroy says it is the owner's, in a terminal" ;;
  *)
    echo "FAIL the deny for make aws-destroy does not say it is the owner's, in a terminal: $reason"
    fail=1
    ;;
esac
# The second module's lines (S079, K7, the review's L3): the reason names the
# module's own directory or target, and keeps what the first module's says.
reason="$(reason_of 'make aws-kubeadm-apply')"
case "$reason" in
  *"aws-kubeadm-apply"*"COST MONEY"*"owner"*"no session holds the credentials"*) echo "ok   the ask for make aws-kubeadm-apply names its target, says it costs money and who runs it" ;;
  *)
    echo "FAIL the ask for make aws-kubeadm-apply does not name its target, or does not say it costs money and who runs it: $reason"
    fail=1
    ;;
esac
reason="$(reason_of 'make aws-kubeadm-destroy')"
case "$reason" in
  *"aws-kubeadm-destroy"*"owner's"*"terminal"*"no session holds the credentials"*) echo "ok   the deny for make aws-kubeadm-destroy names its target and says it is the owner's, in a terminal" ;;
  *)
    echo "FAIL the deny for make aws-kubeadm-destroy does not name its target, or does not say it is the owner's, in a terminal: $reason"
    fail=1
    ;;
esac
reason="$(reason_of 'make aws-kubeadm-plan')"
case "$reason" in
  *"aws-kubeadm-plan"*"owner"*) echo "ok   the ask for make aws-kubeadm-plan names its target" ;;
  *)
    echo "FAIL the ask for make aws-kubeadm-plan does not name its target: $reason"
    fail=1
    ;;
esac
reason="$(reason_of 'terraform -chdir=infra/terraform/aws-kubeadm workspace new x')"
case "$reason" in
  *"infra/terraform/aws-kubeadm"*"wrapper"*) echo "ok   the deny for Terraform by hand in the second module's directory names that directory" ;;
  *)
    echo "FAIL the deny for Terraform by hand in the second module's directory does not name that directory: $reason"
    fail=1
    ;;
esac
reason="$(reason_of 'terraform -chdir=infra/terraform/aws-kubeadm show')"
case "$reason" in
  *"infra/terraform/aws-kubeadm"*"owner's own session"*) echo "ok   the ask for terraform show in the second module's directory names that directory" ;;
  *)
    echo "FAIL the ask for terraform show in the second module's directory does not name that directory: $reason"
    fail=1
    ;;
esac
reason="$(reason_of 'tee ~/.local/state/meridian-aws-kubeadm/aws-kubeadm.tfstate')"
case "$reason" in
  *"saved plan"*"state file"*"meridian-aws-kubeadm"*) echo "ok   the deny for a write into the state names the saved plan, a state file and both modules' directories" ;;
  *)
    echo "FAIL the deny for a write into the state does not name the saved plan, a state file and the second module's directory: $reason"
    fail=1
    ;;
esac

# The settings (S036): a hook decision comes first, and the settings are what a
# session meets when it uses a tool the hook does not read. The lists are held
# here so that an edit that drops one fails: Read, Edit and Write are denied
# for what the AWS wrapper keeps closed, git's and Terraform's configuration in
# the caller's home is not written, plan reaches Terraform only where the
# module is not (the settings cannot see the directory a `cd` chose, so the
# bare `terraform plan` asks), and make aws-plan and aws-apply ask. make * stays
# allowed for aws-validate and aws-scan, which cost nothing.
settings_file="$here/../.claude/settings.json"
in_list() { # $1=allow, ask or deny  $2=the entry
  jq -e --arg e "$2" --arg l "$1" '.permissions[$l] | index($e) != null' "$settings_file" > /dev/null
}
for entry in 'Bash(make *)' 'Bash(terraform -chdir=* plan*)' 'Bash(terraform -chdir=* validate*)'; do
  if in_list allow "$entry"; then
    echo "ok   allow keeps ${entry}"
  else
    echo "FAIL allow lost ${entry}"
    fail=1
  fi
done
if in_list allow 'Bash(terraform plan*)'; then
  echo "FAIL allow still holds the bare terraform plan"
  fail=1
else
  echo "ok   allow no longer holds the bare terraform plan"
fi
for entry in 'Bash(terraform plan*)' 'Bash(terraform -chdir=*aws* plan*)' 'Bash(make aws-plan*)' 'Bash(make aws-apply*)' \
  'Bash(make aws-kubeadm-plan*)' 'Bash(make aws-kubeadm-apply*)' \
  'Bash(make eval-record*)' 'Bash(make eval-injection-record*)' 'Bash(make gateway-live*)' 'Bash(az rest*)' \
  'Bash(terraform apply*)' 'Bash(terraform -chdir=* apply*)'; do
  if in_list ask "$entry"; then
    echo "ok   ask holds ${entry}"
  else
    echo "FAIL ask lacks ${entry}"
    fail=1
  fi
done
# shellcheck disable=SC2088  # the entries are the literal text of the settings
closed_paths=(
  './**/local.env*' './**/*.tfstate*' './**/*.tfplan*' './**/*.tfvars.json' './**/*.auto.tfvars*'
  './infra/terraform/aws/.*tfvars*' './**/terraform.tfstate.d/**' './**/*override*.tf' './**/.terraform/**'
  '~/.local/state/meridian-aws/**' '~/.aws/**' '~/.terraformrc' '~/.terraform.d/**'
  './infra/terraform/aws-kubeadm/.*tfvars*' '~/.local/state/meridian-aws-kubeadm/**'
)
for path in "${closed_paths[@]}"; do
  for tool in Read Edit Write; do
    if in_list deny "${tool}(${path})"; then
      echo "ok   deny holds ${tool}(${path})"
    else
      echo "FAIL deny lacks ${tool}(${path})"
      fail=1
    fi
  done
done
# shellcheck disable=SC2088  # the entries are the literal text of the settings
for path in '~/.gitconfig' '~/.config/git/**'; do
  for tool in Edit Write; do
    if in_list deny "${tool}(${path})"; then
      echo "ok   deny holds ${tool}(${path})"
    else
      echo "FAIL deny lacks ${tool}(${path})"
      fail=1
    fi
  done
done
# The old denies stay.
# The removal has a second layer (the hook is the first): a hook that timed out
# or crashed would let make aws-destroy through, as Bash(make *) is allowed.
for entry in 'Bash(make aws-destroy*)' 'Bash(make aws-kubeadm-destroy*)' \
  'Bash(infra/terraform/aws.sh destroy*)' 'Bash(./infra/terraform/aws.sh destroy*)'; do
  if in_list deny "$entry"; then
    echo "ok   deny holds ${entry}"
  else
    echo "FAIL deny lacks ${entry}"
    fail=1
  fi
done
for entry in 'Read(./**/*.tfvars)' 'Read(./**/.env)' 'Edit(./**/.env)' 'Bash(terraform destroy*)' 'Bash(terraform -chdir=* destroy*)'; do
  if in_list deny "$entry"; then
    echo "ok   deny keeps ${entry}"
  else
    echo "FAIL deny lost ${entry}"
    fail=1
  fi
done
# Every entry that names the first module's state directory, its directory or
# its make targets has a twin for the second module (S079, K6): a deny that
# names one module and not the other is how the review found the gap. The twin
# is built from the entry, so an entry added for the first module without one
# fails; the count keeps the loop from passing empty. A module that is a third
# directory is caught by the scan after it, which reads the directories.
twin_count=0
while IFS=$'\t' read -r list entry; do
  twin="${entry//meridian-aws\//meridian-aws-kubeadm/}"
  twin="${twin//infra\/terraform\/aws\//infra/terraform/aws-kubeadm/}"
  twin="${twin//make aws-/make aws-kubeadm-}"
  twin_count=$((twin_count + 1))
  if in_list "$list" "$twin"; then
    echo "ok   ${list} holds ${twin}, the twin of ${entry}"
  else
    echo "FAIL ${list} lacks ${twin}, the twin of ${entry}"
    fail=1
  fi
done < <(jq -r '.permissions | to_entries[] | .key as $l | .value[]
  | select(contains("meridian-aws/") or contains("infra/terraform/aws/") or test("make aws-(plan|apply|destroy)"))
  | "\($l)\t\(.)"' "$settings_file")
if [ "$twin_count" -ge 9 ]; then
  echo "ok   the twin check read ${twin_count} entries of the first module"
else
  echo "FAIL the twin check read ${twin_count} entries of the first module, fewer than 9"
  fail=1
fi
# A module of the AWS family is a directory infra/terraform/aws* that holds
# Terraform files (the wrapper aws.sh is a file, not a directory). Each one has
# the settings entries the first two have: the hidden variable files, the state
# directory under home (the wrapper names it meridian-<directory>), and the make
# targets' second layer (the removal denied, the plan and the apply asked). A
# third module fails here the day its directory lands, until its entries are in
# the settings and its names are in the guard's patterns.
module_count=0
for dir in "$here"/../infra/terraform/aws*/; do
  [ -d "$dir" ] || continue
  module="$(basename "$dir")"
  module_count=$((module_count + 1))
  for tool in Read Edit Write; do
    for entry in "${tool}(./infra/terraform/${module}/.*tfvars*)" "${tool}(~/.local/state/meridian-${module}/**)"; do
      if in_list deny "$entry"; then
        echo "ok   deny holds ${entry}, for the directory ${module}"
      else
        echo "FAIL deny lacks ${entry}, for the directory ${module}"
        fail=1
      fi
    done
  done
  if in_list deny "Bash(make ${module}-destroy*)"; then
    echo "ok   deny holds Bash(make ${module}-destroy*), for the directory ${module}"
  else
    echo "FAIL deny lacks Bash(make ${module}-destroy*), for the directory ${module}"
    fail=1
  fi
  for verb in plan apply; do
    if in_list ask "Bash(make ${module}-${verb}*)"; then
      echo "ok   ask holds Bash(make ${module}-${verb}*), for the directory ${module}"
    else
      echo "FAIL ask lacks Bash(make ${module}-${verb}*), for the directory ${module}"
      fail=1
    fi
  done
  # The guard as well as the settings (S079, K7, the review's L2): one by-hand
  # show in the directory asks, one new workspace and one write into the saved
  # plan are denied, and the removal target is denied, so a module whose name
  # the patterns do not read fails here with the settings' entries.
  ask_for "the guard asks for a show in infra/terraform/${module}" ask \
    "terraform -chdir=infra/terraform/${module} show"
  ask_for "the guard denies a new workspace in infra/terraform/${module}" deny \
    "terraform -chdir=infra/terraform/${module} workspace new x"
  ask_for "the guard denies a write into ${module}.tfplan" deny \
    "tee infra/terraform/${module}/${module}.tfplan"
  ask_for "the guard denies make ${module}-destroy" deny "make ${module}-destroy"
done
if [ "$module_count" -ge 2 ]; then
  echo "ok   the directory scan found ${module_count} modules of the AWS family"
else
  echo "FAIL the directory scan found ${module_count} modules of the AWS family, fewer than 2"
  fail=1
fi

# The paid model calls (S071, G1). The ask says that the command calls a live
# model and spends money, and that the owner's yes to a stated cost comes first;
# the settings keep the free targets out of the ask list, so that the replay of
# the recording and the smoke check (three calls, under a cent) never ask.
for command_text in 'make eval-record' 'make eval-injection-record' 'make gateway-live' \
  'infra/terraform/foundation.sh eval-record' 'MERIDIAN_LIVE_AZURE=1 pytest'; do
  reason="$(reason_of "$command_text")"
  case "$reason" in
    *"live model"*"spend money"*"owner's yes"*"stated cost"*) echo "ok   the ask for ${command_text} says it calls a live model, spends money and needs the owner's yes to a stated cost" ;;
    *)
      echo "FAIL the ask for ${command_text} does not say it calls a live model, spends money and needs the owner's yes to a stated cost: $reason"
      fail=1
      ;;
  esac
done
for entry in 'Bash(make azure-smoke*)' 'Bash(make eval*)' 'Bash(make eval-baseline*)' 'Bash(make eval-compare*)' 'Bash(make eval)'; do
  if in_list ask "$entry" || in_list deny "$entry"; then
    echo "FAIL ask or deny holds ${entry}, a free target"
    fail=1
  else
    echo "ok   neither ask nor deny holds ${entry}, a free target"
  fi
done
# The worst shapes of the new rules under the byte bound: `make ` repeated and
# then a target that only starts like a paid one (the make rule backtracks over
# every blank), and an opt-in name repeated (the loop is bounded at sixteen
# reads and then asks). Both are answered well inside the CPU bound.
paid_shape="$(for _ in $(seq 1550); do printf 'make '; done)eval-recorder"
jq -nc --arg c "$paid_shape" '{tool_input:{command:$c}}' > "$big_input"
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
  echo "ok   1550 repetitions of make before a near-miss paid target take ${cpu_seconds} s of CPU, under ${cpu_bound}"
else
  echo "FAIL 1550 repetitions of make before a near-miss paid target take ${cpu_seconds} s of CPU, not under ${cpu_bound}"
  fail=1
fi
ask_for "1550 repetitions of make before a paid target ask" ask \
  "$(for _ in $(seq 1550); do printf 'make '; done)eval-record"
paid_env_shape="$(for _ in $(seq 360); do printf 'MERIDIAN_EVAL_RECORD=0 '; done)pytest"
jq -nc --arg c "$paid_env_shape" '{tool_input:{command:$c}}' > "$big_input"
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
  echo "ok   360 opt-in assignments set to 0 take ${cpu_seconds} s of CPU, under ${cpu_bound}"
else
  echo "FAIL 360 opt-in assignments set to 0 take ${cpu_seconds} s of CPU, not under ${cpu_bound}"
  fail=1
fi
ask_for "360 opt-in assignments set to 0 ask, the loop reads sixteen" ask "$paid_env_shape"

# The paid call itself and the additions of S071 (G2, G4). The asks say that the
# command reaches a live model without the gateway and that the gateway's
# targets are the only door; a paid target found by co-occurrence and one given
# through MAKEFLAGS say what the other paid asks say.
for command_text in 'az rest --url https://x' 'python3 -c "import azure.identity"'; do
  reason="$(reason_of "$command_text")"
  case "$reason" in
    *"live"*"spends money"*"gateway's ceiling"*"only door"*"stated cost"*) echo "ok   the ask for ${command_text} says it reaches a live model without the gateway and that the gateway's targets are the only door" ;;
    *)
      echo "FAIL the ask for ${command_text} does not say it reaches a live model without the gateway and that its targets are the only door: $reason"
      fail=1
      ;;
  esac
done
# shellcheck disable=SC2016  # the commands are samples for the hook, not for this shell
for command_text in 'MAKEFLAGS="-- eval-record" make' 'T=eval-record; make $T' 'make -C $(cd x; pwd) gateway-live'; do
  reason="$(reason_of "$command_text")"
  case "$reason" in
    *"live model"*"spend money"*"owner's yes"*"stated cost"*) echo "ok   the ask for ${command_text} says it calls a live model and spends money" ;;
    *)
      echo "FAIL the ask for ${command_text} does not say it calls a live model and spends money: $reason"
      fail=1
      ;;
  esac
done
reason="$(reason_of 'make -m "aws-destroy"')"
case "$reason" in
  *"owner's"*"terminal"*"no session holds the credentials"*) echo "ok   the deny for a quoted removal target behind a make option says it is the owner's, in a terminal" ;;
  *)
    echo "FAIL the deny for a quoted removal target behind a make option does not say it is the owner's, in a terminal: $reason"
    fail=1
    ;;
esac
reason="$(reason_of 'make -m "aws-apply"')"
case "$reason" in
  *"COST MONEY"*"owner"*"no session holds the credentials"*) echo "ok   the ask for a quoted apply target behind a make option says it costs money and who runs it" ;;
  *)
    echo "FAIL the ask for a quoted apply target behind a make option does not say it costs money and who runs it: $reason"
    fail=1
    ;;
esac
# The worst shape of the az rule (it backtracks over every blank, like the make
# rule): `az ` repeated and then a word that only starts like rest.
az_shape="$(for _ in $(seq 2000); do printf 'az '; done)restx"
jq -nc --arg c "$az_shape" '{tool_input:{command:$c}}' > "$big_input"
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
  echo "ok   2000 repetitions of az before a near-miss word take ${cpu_seconds} s of CPU, under ${cpu_bound}"
else
  echo "FAIL 2000 repetitions of az before a near-miss word take ${cpu_seconds} s of CPU, not under ${cpu_bound}"
  fail=1
fi
ask_for "2000 repetitions of az before rest ask" ask "$(for _ in $(seq 2000); do printf 'az '; done)rest"

# The passes S071 adds (G4): a copy of the command with the separators inside
# quoted pieces blanked, a pass of its own that empties message values, and the
# pass that finds a quoted AWS target behind a make option. G2's pass over the
# older copy ran 14 to 56 s on a line of unmatched escaped quotes and message
# flags (the hook's timeout is 10 s, and a hook past it does not block); the new
# passes mask the quoted pieces once and scan the text a few times. Each shape
# stays under the one CPU bound (they take 0.1 to 1.4 s here). The first four
# are the first review's, the worst first; the rest are the next-worst shapes
# found by trying every flag the passes read, a make in the segment, nested
# quotes, long runs of separators and every trigger word of the passes at once
# (the second review's worst, which starts four Python passes).
repeat() { # $1=text $2=count: the text, that many times
  local i out=""
  for ((i = 0; i < $2; i++)); do out+="$1"; done
  printf '%s' "$out"
}
cpu_shape() { # $1=what the shape is $2=the command
  jq -nc --arg c "$2" '{tool_input:{command:$c}}' > "$big_input"
  cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
  cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
  if awk -v s="$cpu_seconds" -v b="$cpu_bound" 'BEGIN { exit !(s < b) }'; then
    echo "ok   ${1} takes ${cpu_seconds} s of CPU, under ${cpu_bound}"
  else
    echo "FAIL ${1} takes ${cpu_seconds} s of CPU, not under ${cpu_bound}"
    fail=1
  fi
}
esc_quote='\"'
cpu_shape "1500 escaped quotes and 700 empty -m values" "git $(repeat "$esc_quote" 1500)$(repeat " -m ''" 700)"
cpu_shape "2000 escaped quotes and 500 empty -m values" "git $(repeat "$esc_quote" 2000)$(repeat " -m ''" 500)"
cpu_shape "500 escaped quotes and 1100 empty -m values" "git $(repeat "$esc_quote" 500)$(repeat " -m ''" 1100)"
cpu_shape "3000 escaped quotes and 150 empty -m values" "git $(repeat "$esc_quote" 3000)$(repeat " -m ''" 150)"
cpu_shape "1500 escaped quotes and 500 empty --body values" "git $(repeat "$esc_quote" 1500)$(repeat " --body ''" 500)"
cpu_shape "1500 escaped quotes and 600 empty -am values" "git $(repeat "$esc_quote" 1500)$(repeat " -am ''" 600)"
cpu_shape "make, 1500 escaped quotes and 700 empty -m values" "git make $(repeat "$esc_quote" 1500)$(repeat " -m ''" 700)"
cpu_shape "aws, 1500 escaped quotes and 400 make -m values" "aws $(repeat "$esc_quote" 1500)$(repeat " make -m 'x'" 400)"
cpu_shape "a long run of quoted values after -m" "git -m $(repeat "'x'" 2500)"
cpu_shape "3000 separators and 800 empty -m values" "git $(repeat ';' 3000)$(repeat " -m ''" 800)"
cpu_shape "every trigger word, 3900 escaped quotes and 300 separators" \
  "git aws rest record azure- credential MERIDIAN_ python3 $(repeat "$esc_quote" 3900)$(repeat '&' 300)"
# A line that reaches the passes with padding is still read to its end: the
# denied part after the padding is denied, not skipped.
ask_for "the first review's worst shape followed by a denied part is denied" deny \
  "git $(repeat "$esc_quote" 1500)$(repeat " -m ''" 700); git push --force"
ask_for "every trigger word and padding followed by a denied part is denied" deny \
  "git aws rest record azure- credential MERIDIAN_ python3 $(repeat "$esc_quote" 3900)$(repeat '&' 100); git push --force"
exit "$fail"
