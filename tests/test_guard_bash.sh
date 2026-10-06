#!/usr/bin/env bash
# Regression test for .claude/hooks/guard-bash.sh. Each line of
# guard-bash-cases.jsonl carries a sample command and the expected decision
# (deny, ask or none). Run: bash tests/test_guard_bash.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
hook="$here/../.claude/hooks/guard-bash.sh"
fail=0
n=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  n=$((n + 1))
  expect="$(jq -r '.expect' <<<"$line")"
  got="$(jq -c '{tool_input:{command:.command}}' <<<"$line" | bash "$hook" \
    | jq -r '.hookSpecificOutput.permissionDecision // "none"')"
  [ -z "$got" ] && got=none
  if [ "$got" != "$expect" ]; then
    echo "FAIL case $n: expected $expect, got $got"
    fail=1
  else
    echo "ok   case $n: $expect"
  fi
done < "$here/guard-bash-cases.jsonl"

# The length bound (guard_max_bytes, 16384). A hook that runs past its timeout
# does not block the call, so a command too long to read in time must be
# answered at once. The commands are built here, byte by byte, because a case
# line that long cannot be read in review. `ask_for` runs the hook on a command
# and compares the answer.
bound=16384
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
# 8193 two-byte characters are 16386 bytes: the bound counts bytes, not characters.
ask_for "a command over the bound in bytes asks though it is under it in characters" ask \
  "${prefix}$(for _ in $(seq 8193); do printf '\303\251'; done)"
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
# 2500 lines of 6 bytes are 15000 bytes: written to a file the body is stripped
# and the command has one segment; fed to bash the body stays and every line is
# a segment.
heredoc_15k="$(for _ in $(seq 2500); do printf 'a: b\n'; done)"
ask_for "a heredoc of 15 KB written to a file passes" none "cat > x.yaml <<'EOF'
${heredoc_15k}
EOF"
ask_for "a heredoc of 15 KB with more lines than the segment bound fed to bash asks" ask "bash <<'EOF'
${heredoc_15k}
EOF"
ask_for "a script of 200 lines fed to bash passes" none "bash <<'EOF'
$(for _ in $(seq 200); do printf 'echo a\n'; done)
EOF"
ask_for "a command of 15 KB in one segment passes" none "${prefix}$(padding 15000 a)"
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
# The command is nine thousand segments, not a heredoc: a heredoc written to a
# file is stripped before any pattern, and cheap to read even without the bound.
jq -nc --arg c "$(for _ in $(seq 9000); do printf 'echo a; '; done)" \
  '{tool_input:{command:$c}}' > "$big_input"
TIMEFORMAT='%U %S'
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" 'BEGIN { exit !(s < 0.5) }'; then
  echo "ok   a 70 KB command takes ${cpu_seconds} s of CPU, under 0.5"
else
  echo "FAIL a 70 KB command takes ${cpu_seconds} s of CPU, not under 0.5"
  fail=1
fi
# The worst shape under the byte bound: 8192 one-word segments are 16384 bytes
# and took 3 s of CPU before the segment bound; they are answered at once now.
jq -nc --arg c "$(segments 8192 a)" '{tool_input:{command:$c}}' > "$big_input"
cpu="$( { time bash "$hook" < "$big_input" > /dev/null; } 2>&1 )"
cpu_seconds="$(awk '{ print $1 + $2 }' <<<"$cpu")"
if awk -v s="$cpu_seconds" 'BEGIN { exit !(s < 0.5) }'; then
  echo "ok   16384 bytes of one-word segments take ${cpu_seconds} s of CPU, under 0.5"
else
  echo "FAIL 16384 bytes of one-word segments take ${cpu_seconds} s of CPU, not under 0.5"
  fail=1
fi
exit "$fail"
