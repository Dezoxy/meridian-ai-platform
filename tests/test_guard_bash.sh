#!/usr/bin/env bash
# Regression test for .claude/hooks/guard-bash.sh. Each line of
# guard-bash-cases.jsonl carries a sample command and the expected decision
# (deny, ask or none), and may carry the working directory the harness passes
# in its input ("cwd"; a line without one is as before). Run: bash tests/test_guard_bash.sh
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
for entry in 'Bash(make aws-destroy*)' 'Bash(infra/terraform/aws.sh destroy*)' 'Bash(./infra/terraform/aws.sh destroy*)'; do
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
exit "$fail"
