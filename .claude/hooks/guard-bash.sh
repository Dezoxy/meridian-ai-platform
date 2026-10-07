#!/usr/bin/env bash
# PreToolUse(Bash) guard. Emits a permission decision for dangerous commands.
# deny = hard block (destructive / secret exposure); ask = explicit confirmation.
#
# Heredoc bodies that are merely WRITTEN to a file (cat/tee) are stripped before
# matching, so documentation that mentions a dangerous command is not blocked
# for the mention. A heredoc whose body executes is kept: one fed to an
# interpreter (bash, sh, ksh, python3.12, ssh, env, awk, ... the EXECUTES list
# below, with or without a path in front), to `. file` or source, to >(sh), or
# written to a script file. The body of an unquoted delimiter is kept when it
# holds `$(`, a backtick or a backslash at a line's end, because the shell
# expands it. A << inside quotes (decided by counting the quote characters
# before it, a parity and not a parse), a comment or $(( opens no body, and
# neither does one whose delimiter the pass cannot read whole (END-OF-DATA,
# EOF.txt): what follows it stays read.
# Backslash-newline is deleted, as the shell deletes it, and the percent-decode
# of a --raw path asks when it runs out of rounds.
# Still open, one sentence each: a file written by a heredoc and then run by
# name (cat > run <<'EOF' ... then bash run) passes, as the Write tool and bash
# would; $(cat <<'EOF' ... ) used as a command passes, because its body is
# dropped as a file write though the substitution runs it; a heredoc piped into
# an interpreter that is not on the EXECUTES list (gawk, nodejs, deno, bun,
# Rscript, make -f -) is dropped as a file write; and quote kinds mixed in the
# head of a heredoc (cat '"' "<<EOF") fool the parity.
# This is a regex guard, not a sandbox: Claude Code's permission rules and the
# owner's approvals remain the real boundary. It stops a session from doing by
# reflex what it should stop and think about; it does not stop a session that
# means to get past it. It reads the text of the command, and these it does not
# see, each once (docs/operations/runbooks/secret-rotation.md, "What the
# command guard does not see", has the same list with the rules):
#   - quoting inside a word (e''nv, secr''ets, get-secret-"value")
#   - a variable that holds the verb or the path ($P, K=kind; $K get ...)
#   - brace expansion ({env,}, {secrets,})
#   - an alias for a tool (alias a=aws)
#   - a file written and then run (a heredoc or the Write tool, then bash it)
#   - an interpreter in a pod (python or awk reading what env would print)
#   - a value echoed by name (sh -c 'echo $DATABASE_URL')
#   - a proxy and curl (kubectl proxy, then curl on a path a variable builds)
#   - a script's inside, and anything that is not typed as a command
#   - this guard's own files: a session can edit this hook and
#     .claude/settings.json (the permission rules allow Edit and Write on
#     them), so a rule against that is the owner's decision, not built here;
#     nor does it see a hook file overwritten by a redirect (cat <<'EOF'
#     >.git/hooks/pre-commit): only rm, mv, chmod and truncate of one deny
#   - a secret-shaped variable whose name matches no word of printenv's list
#     (REDIS_PW, PW, GITHUB_PAT): the rule reads the name, not the value
#   - the AWS rules' own gaps (S036): git config --global (a write of
#     ~/.gitconfig through git), a pseudo-terminal made by a script file, a
#     file that names the wrapper's removal in a variable, the aws CLI through
#     python3 or a container that is not amazon/aws-cli, and every spelling above
#     of a path or verb. With credentials on the machine nothing here is the
#     barrier: where they are is (infra/terraform/aws/README.md)
#   - the heredoc openers above: quote kinds mixed in a head, and an
#     interpreter that is not on the EXECUTES list
set -euo pipefail

# A hook that runs past its timeout does not block the call: Claude Code lets
# it continue through the normal permission flow, so a hook that stalled would
# let a command through unread. The hook has ten seconds (.claude/settings.json)
# and the bounds below keep the commands it was measured on far under that, but
# a bound is a measurement of some shapes, not a guarantee for every shape. So
# the first thing the hook does, before it reads the command, is to start a
# watchdog: a helper that signals the hook after guard_watchdog_default seconds
# (5 of the 10, a margin of 5 for the one command that may be in flight, see
# below), and the hook then answers `ask`, whatever it was reading, saying that
# it ran out of time and did NOT read the command. tests/test_guard_bash.sh
# reads the timeout from settings.json and holds the two numbers together.
#
# What the watchdog cannot do: bash runs a trap between commands, and waits for
# a foreground command (one regex match, one of the python or sed passes) to
# end before it runs the trap. A command in flight is not interrupted, so the
# slowest single command decides how late the answer can be, and the bound on
# what the patterns read (guard_max_bytes, 8192, counted after the heredoc pass;
# a typed bound of 16384 comes first) is chosen from it. Measured on 2026-10-06 with the machine idle (load
# about 4), as the gap between two xtrace stamps, on the shapes that cost most
# (runs of `&(`, `(`, quotes and backticks after `kubectl `, and the same after
# `kubectl exec x -- `, which reach the Azure script rules and the pod rule):
#   16384 bytes: 0.97 s     8192 bytes: 0.24 s     4096 bytes: 0.06 s
# Later passes measured 0.23 to 0.42 s at 8192 bytes (the range of three
# measurements, on a machine at load 3 to 18); the margin below holds it. The
# AWS rules (S036) were measured the same way on 40 shapes of 8 KB (the name of
# a tool, a flag or an assignment repeated, before a verb or a path): the worst
# were 0.49 s (`make ` 1550 times before a near-miss target) and 0.44 s (a run
# of `>` before .awsX), at a load of 8. tests/test_guard_bash.sh holds those and
# the `aws ` run under one bound of 3 s of CPU for every CPU check (see there).
# The cost goes with the square of the length. The review of the first bounds
# saw this machine run 3.5 to 6.2 times slower than idle (3 s of CPU took 10.8
# and 19.3 s of wall with every core oversubscribed three times): 16384 bytes
# would leave 0.97 x 6.2 = 6 s in flight, more than the margin; 8192 bytes
# leave 0.24 x 6.2 = 1.5 s, and the margin holds a machine 20 times slower
# than idle. Beyond that the answer can come after the timeout, and the call
# then goes through unread: the one case this hook cannot close.
#
# GUARD_WATCHDOG_SECONDS may shorten the time (the tests do, to force the slow
# path) and never lengthens it: a value that is not a positive number under the
# default leaves the default.
guard_watchdog_default=5
guard_watchdog_seconds="$guard_watchdog_default"
if [[ "${GUARD_WATCHDOG_SECONDS:-}" =~ ^[0-9]+(\.[0-9]+)?$ ]] \
   && awk -v s="$GUARD_WATCHDOG_SECONDS" -v d="$guard_watchdog_default" 'BEGIN { exit !(s > 0 && s < d) }'; then
  guard_watchdog_seconds="$GUARD_WATCHDOG_SECONDS"
fi
guard_helper=""
guard_disarm() { # the helper is killed on every way out, and waited for
  [ -z "$guard_helper" ] && return 0
  kill "$guard_helper" 2>/dev/null || true
  wait "$guard_helper" 2>/dev/null || true
  guard_helper=""
}
decide() { # $1=decision $2=reason
  # Disarmed before it prints: a late alarm is ignored, so two answers are
  # never printed.
  trap '' ALRM
  guard_disarm
  jq -nc --arg d "$1" --arg r "$2" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:$d,permissionDecisionReason:$r}}'
  exit 0
}
trap guard_disarm EXIT
trap 'decide ask "The guard ran out of time reading this command, so it was NOT read to the end and may hold a form the guard would deny: confirm that it holds none, or write it to a script with the Write tool and run the file."' ALRM
# The helper: a subshell with its own sleep. Killing the subshell must kill the
# sleep too (or it would linger for the time left), so the subshell traps TERM,
# kills its sleep and leaves without signalling; the flag closes the window
# between starting the sleep and knowing its pid. Its output is closed so that
# it never holds the pipe the harness reads.
(
  guard_stopped=""
  guard_sleeper=""
  trap 'guard_stopped=1; [ -z "$guard_sleeper" ] || kill "$guard_sleeper" 2>/dev/null' TERM
  sleep "$guard_watchdog_seconds" &
  guard_sleeper=$!
  [ -z "$guard_stopped" ] || kill "$guard_sleeper" 2>/dev/null
  wait "$guard_sleeper" 2>/dev/null || true
  [ -n "$guard_stopped" ] || kill -ALRM "$$" 2>/dev/null
) >/dev/null 2>&1 </dev/null &
guard_helper=$!

input="$(cat)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"
[ -z "$cmd" ] && exit 0
# The working directory the harness passes with the command (the AWS rules read
# it as a cd into the module when it is the module's directory or under it).
# Whether it follows a cd made by an earlier Bash call is not verified.
hook_cwd="$(printf '%s' "$input" | jq -r '.cwd? | strings' 2>/dev/null || true)"
# The command as typed. The heredoc pass below drops lines the shell may still
# run, so the kubectl rule that lets a call through reads this copy.
raw_cmd="$cmd"

# Two byte bounds, because two things cost time. guard_max_typed_bytes bounds
# what was typed and is checked before anything reads it: the heredoc pass and
# the jq calls are linear (0.1 s of CPU for 1 MB, 0.4 s for 4 MB, measured) and
# a wc over the raw command costs nothing however long it is. guard_max_bytes
# (below, after the heredoc pass) bounds what the patterns read, and is the
# one the measurements above set: a heredoc written to a file of up to the
# typed bound passes (the pass drops its body), a command whose own text is
# longer than guard_max_bytes asks. Bytes, not characters: wc counts bytes and
# the arithmetic trims the padding some wc print.
guard_max_typed_bytes=16384
guard_max_bytes=8192
guard_typed_bytes=$(( $(printf '%s' "$cmd" | wc -c) ))
[ "$guard_typed_bytes" -le "$guard_max_typed_bytes" ] || \
  decide ask "This command is too long for the guard to read (${guard_typed_bytes} bytes, the limit is ${guard_max_typed_bytes}): it was NOT read and may hold a form the guard would deny; confirm that it holds none, or write it to a script with the Write tool and run the file."

if command -v python3 >/dev/null 2>&1; then
  # python3 -I: no working directory on sys.path, no PYTHON* variables, no user
  # site: a re.py (or any module this imports) lying in the directory the hook
  # runs from is not read in place of the standard library.
  # shellcheck disable=SC2016  # the Python source below is meant to stay literal
  guard_passed="$(printf '%s' "$cmd" | python3 -I -c '
import re, sys
# A here-string (<<<word) is not a heredoc: it opens no body. Group 1 is the
# delimiter quote: an unquoted delimiter has a body the shell expands. The
# delimiter is read whole: a blank, a separator, a bracket, a redirect or the
# end of the line must follow it (END-OF-DATA, EOF.txt and EOF-1 are whole
# delimiters, not END and EOF). A heredoc that does not parse opens no body, so
# what follows it stays read.
MARK = re.compile(r"(?<!<)<<-?\s*([\x27\"]?)([A-Za-z_][A-Za-z0-9_]*)\1(?=[\s;&|<>()]|$)")
WRITER = re.compile(r"(^|[;&|(]\s*)(cat|tee)\b")
# Interpreters count only as command tokens, not as substrings of a path
# such as guard-bash-cases.jsonl: after a space, a separator, a bracket or a
# slash (/bin/sh), before a space, the end or a closing bracket (>(sh)). The
# second branch is `. file`, the dot command.
EXECUTES = re.compile(r"(^|[\s;&|(/])(sudo\s+)?(bash|sh|zsh|dash|ksh|ash|fish|csh|tcsh|python[0-9.]*|node|perl|ruby|php|lua|awk|ssh|env|nsenter|xargs|eval|source|exec|chmod)(\s|$|[)])|(^|[;&|(])\s*\.\s")
# One character before the dot, not a run (\S+): a run is quadratic on a long
# line of dots.
SCRIPT_TARGET = re.compile(r"\S\.(sh|bash|zsh|py|rb|pl|js)\b")
def expands(body):
    # What the shell expands in the body of an unquoted delimiter: a command
    # substitution, a backtick, and a backslash-newline that can join the two
    # characters of a `$(` across lines.
    return any("$(" in l or "`" in l or l.endswith("\\") for l in body)
lines = sys.stdin.read().split("\n")
term = None
keep_subst = False
body = []
head_at = None
closed = False
out = []
i = 0
while i < len(lines):
    line = lines[i]
    i += 1
    if term is not None:
        if line.strip() == term:
            term = None
            closed = True
            # The body of an unquoted delimiter is expanded: when it holds a
            # command substitution the shell runs it, so the body is kept.
            if keep_subst and expands(body):
                out.extend(body)
        else:
            body.append(line)
        continue
    # Outside a body, the shell joins a line that ends in an odd number of
    # backslashes with the next one: <<E\<newline>OF is <<EOF.
    while i < len(lines) and (len(line) - len(line.rstrip("\\"))) % 2:
        line = line[:-1] + lines[i]
        i += 1
    # After a stripped body, a line that starts with the closing bracket of
    # the command substitution around the heredoc belongs to the command that
    # opened it: a flag written after the message is a flag of that commit.
    if closed and line.lstrip().startswith(")") and head_at is not None:
        out[head_at] += " " + line.lstrip()
        closed = False
        continue
    closed = False
    out.append(line)
    m = MARK.search(line)
    if not m:
        continue
    head, tail = line[:m.start()], line[m.end():]
    # A << inside quotes, a comment or $(( is not a heredoc. An odd number of
    # quotes in the head means it is inside a string, unless the head opens a
    # command substitution (the "$(cat <<EOF idiom, which runs outside the
    # quote). Such a line opens no body, so the lines after it stay.
    quoted = (head.count("\x27") % 2 or head.count("\"") % 2) and "$(" not in head and "`" not in head
    if quoted or "$((" in head or re.search(r"(^|\s)#", head):
        continue
    plain_write = (
        WRITER.search(head) is not None
        and EXECUTES.search(head) is None
        and EXECUTES.search(tail) is None
        and SCRIPT_TARGET.search(head + tail) is None
    )
    if plain_write:
        term = m.group(2)
        keep_subst = m.group(1) == ""
        body = []
        head_at = len(out) - 1
# A body that never closes runs to the end of the input, expanded all the same.
if term is not None and keep_subst and expands(body):
    out.extend(body)
print("\n".join(out))
' 2>/dev/null || printf '%s' "$cmd")"
  # A pass that returned nothing for a command that was not empty did not run
  # (a python3 that is not python): the command is kept as typed, so that the
  # rules read all of it.
  if [ -n "$guard_passed" ]; then
    cmd="$guard_passed"
  fi
fi

# A backslash-newline is deleted by the shell outside single quotes (e\<newline>nv
# is env, --for\<newline>ce is --force), so it is deleted here, once, for every
# reader below: a deny or ask rule sees the word the shell will run. It comes
# after the heredoc pass on purpose: the pass reads a quoted body as the shell
# does (a line `a\` there joins nothing), and it joined the lines outside bodies
# itself. Inside single quotes the shell keeps the backslash and the newline;
# the hook deletes them there too, which can only join two words of a quoted
# text into one the rules know (a false deny on echo 'git push --for\<nl>ce'),
# and in a bash -c '...' body, where it matters, the inner shell joins them too.
# kind_scan below reads raw_cmd, the text as typed, and replaces the pair with a
# space: a word it cannot recognise then asks, the safe side for a rule that
# lets a call through.
cmd="${cmd//$'\\\n'/}"

# The second byte bound: what the patterns will read, after the heredoc pass,
# is at most guard_max_bytes. The rules below cost time in proportion to the
# length (some, to its square): see the figures above for the value. A heredoc
# that is only written to a file is gone by now, so a document of 15 KB passes;
# one that executes is kept in the text and counts. The rules that read the
# command as typed (raw_cmd) have bounds of their own (kind_scan_max).
guard_bytes=$(( $(printf '%s' "$cmd" | wc -c) ))
[ "$guard_bytes" -le "$guard_max_bytes" ] || \
  decide ask "This command is too long for the guard to read (${guard_bytes} bytes after the heredoc bodies written to files are dropped, the limit is ${guard_max_bytes}): it was NOT read and may hold a form the guard would deny; confirm that it holds none, or write it to a script with the Write tool and run the file."

# The rules below read a command one segment at a time (split on newlines, ;,
# &&, || and |, line continuations deleted), and the time they take follows the
# number of segments, not the bytes: 8192 one-word segments filled the former
# byte bound (16384) and took 3 s of CPU on an idle machine, 4096 (the present
# bound's worth) took 1 s, against 0.25 s for 1000. So the
# segments are counted once, by the same split the loops use (one sed pass over
# at most guard_max_bytes, cheap), before any loop runs, and a command with more
# than guard_max_segments asks. The bound is the round number at which the
# worst shape (one-word segments) stays under 0.5 s of CPU with margin
# (1500 took 0.37 s, 2000 0.5 s); the largest of the repository's documented
# command lines has 6 segments. Counted after the heredoc pass: a heredoc body
# that is merely written to a file is not a segment, one fed to an interpreter
# (or to kubectl apply) is, a line each.
# bs_nl is a backslash and a newline: kind_scan replaces it in raw_cmd.
bs_nl=$'\\\n'
guard_max_segments=1000
guard_segments=$(( $(printf '%s\n' "$cmd" | sed -E 's/(&&|\|\||;|\|)/\n/g' | wc -l) ))
[ "$guard_segments" -le "$guard_max_segments" ] || \
  decide ask "This command has ${guard_segments} parts; the guard reads at most ${guard_max_segments}: it was NOT read and may hold a form the guard would deny; confirm that it holds none, or write it to a script with the Write tool and run that."

# hook_cmd is what the git hook-bypass rules read, and the Kubernetes Secret
# and psql rules after them. A commit message or a pull
# request body is prose: it may name --no-verify or core.hooksPath, and it may
# hold a `;` that would cut the command in two before a real flag. So the
# quoted value of a prose option (-m, also bundled as in -am, --message,
# --body, --title, --notes) is emptied first.
#
# - The command is read one quoted string at a time, so a `-m` that ends an
#   earlier string never opens a value, and the inside of a quoted string is
#   read the same way: `bash -c "git commit -m x -n"` still shows its flag.
# - A value may be several quoted pieces set side by side, which is how a
#   shell writes an apostrophe inside single quotes.
# - A value that holds a command substitution is kept, because that part
#   executes; only the separators in it are blanked, so a flag after it
#   stays in the same segment.
# - A quoted flag is still a flag, so "-n" is read as -n.
#
# Without python3 the rules read the command as it is: prose that names a
# flag is denied again, and a flag after prose with a separator in it is
# missed again, as before this pass existed.
hook_cmd="$cmd"
# shellcheck disable=SC2016  # the Python source below is meant to stay literal
if [[ "$cmd" == *git* || "$cmd" == *secret* || "$cmd" == *psql* || "$cmd" == *pg_* \
      || "$cmd" == *kubectl* || "$cmd" == *helm* || "$cmd" == *aws* || "$cmd" == *cnpg* \
      || "$cmd" == *kind* || "$cmd" == *upkeep* || "$cmd" == *terraform* || "$cmd" == *tofu* ]] \
   && command -v python3 >/dev/null 2>&1; then
  hook_cmd="$(printf '%s' "$cmd" | python3 -I -c '
import re, sys
# A bundle that holds c is not a message option: in bash -cm and sh -ecm the c
# takes the quoted body as a command, which is read, not blanked.
PROSE = r"((?<![\w-])-(?![A-Za-z]*c)[A-Za-z]*m|--message|--body|--title|--notes)(\s+|=)?"
SINGLE = r"\x27[^\x27]*\x27"
DOUBLE = r"\"(?:[^\"\\]|\\.)*\""
QUOTED = "(?:" + SINGLE + "|" + DOUBLE + ")"
ATOM = re.compile(PROSE + "(" + QUOTED + "+)|" + QUOTED)
PIECE = re.compile(QUOTED)
def value_of(m):
    value = m.group(3)
    executes = any(
        p[0] == "\"" and ("$(" in p or "`" in p) for p in PIECE.findall(value)
    )
    if executes:
        value = re.sub(r"[;&|]", " ", value)
    else:
        value = "\"\""
    return m.group(1) + (m.group(2) or "") + value
def read(text):
    def atom(m):
        if m.group(3) is not None:
            return value_of(m)
        quoted = m.group(0)
        return quoted[0] + read(quoted[1:-1]) + quoted[-1]
    return ATOM.sub(atom, text)
text = read(sys.stdin.read())
text = re.sub(r"([\x27\"])(-[A-Za-z][A-Za-z-]*)\1", r"\2", text)
sys.stdout.write(text)
' 2>/dev/null || printf '%s' "$cmd")"
  # Nothing back for a command that was not empty: the pass did not run, the
  # rules read the command as it is (see the heredoc pass above).
  [ -n "$hook_cmd" ] || hook_cmd="$cmd"
fi

shopt -s nocasematch

# ---- hard denies ----
[[ "$cmd" =~ rm[[:space:]]+-[a-z]*r[a-z]*f?[[:space:]]+(/|~|\$HOME|\.\.($|/)) ]] && \
  decide deny "Recursive force-delete of a sensitive path. Run it yourself if truly intended."
[[ "$cmd" =~ (terraform|tofu)[[:space:]].*destroy ]] && \
  decide deny "terraform destroy is destructive; run it yourself after confirming the workspace."
# Destructive pushes and secret printing are judged per command segment (split
# on newlines, ;, &&, || and |), so a flag or path in one segment does not
# combine with a command in another: `git push -q; rm -f x` is not a force
# push, and `echo done && jq '.env' settings.json` prints no secret. Line
# continuations are deleted first (above), so `git push \<newline> --force`
# stays one segment.
#
# infra/kind/pins.env holds version pins and no secret, and its name ends in
# .env: a segment that names that exact path (as is, after ./, or after an
# absolute prefix) is read without it. Another .env in the same segment, and
# a path that only ends in these words (xinfra/kind/pins.env, pins.env/..),
# still deny. Meridian's rule: the base has no such file.
sq="'"
# What may stand before a reader of a secret file: the start, a space, a slash
# (/bin/cat), a quote, a backtick, a bracket or an equals sign, so that
# sh -c "cat .env", (cat .env), $(cat .env) and x=`cat .env` are read. A
# backslash too: \cat is cat (the shell skips an alias), and the deletion of
# a backslash-newline can leave one in front of a word (true \\<newline>cat).
reader_pre=$'(^|[[:space:]/(`"\'=\\])'
pins_env_re="(^|[[:space:]\"${sq}=])((/[^[:space:]\"${sq}]*/)?|\./)infra/kind/pins\.env([[:space:]\"${sq}]|\$)"
while IFS= read -r seg; do
  seg_env="$seg"
  while [[ "$seg_env" =~ $pins_env_re ]]; do
    seg_env="${seg_env/"${BASH_REMATCH[0]}"/${BASH_REMATCH[1]}${BASH_REMATCH[4]}}"
  done
  [[ "$seg" =~ git[[:space:]]+push([[:space:]].*)?[[:space:]](--force|-[a-zA-Z]*f|[+]|--mirror|--prune) ]] && \
    decide deny "Destructive push (--force*, bundled -f, +refspec, --mirror/--prune) can rewrite shared refs. Run it yourself if you must."
  [[ "$seg" =~ (^|[[:space:]])(rm|mv|unlink|truncate)[[:space:]].*(\.git/hooks|\.githooks|\.husky)([/[:space:]]|$) || \
     "$seg" =~ chmod[[:space:]]+[ugoa]*-[rwx]*x.*(\.git/hooks|\.githooks|\.husky)([/[:space:]]|$) ]] && \
    decide deny "Removing or disabling a git hook file bypasses it. Run it yourself if intended."
  [[ "$seg_env" =~ $reader_pre(cat|less|bat|more|head|tail|echo|printf|xxd|base64|strings)[[:space:]].*(\.env($|[^.a-zA-Z])|\.tfvars($|[^.])|\.pem($|[^a-zA-Z])|id_rsa|id_ed25519|kubeconfig|\.kube/config|admin\.conf) ]] && \
    decide deny "That would print secret material to the transcript (.env/tfvars/keys/kubeconfig, a node's admin.conf)."
done < <(printf '%s\n' "$cmd" | sed -E 's/(&&|\|\||;|\|)/\n/g')
# Git hook bypasses. A repository's hooks are its guard rails (a pre-push
# hook may be the only thing stopping a push to main), so an agent never
# skips them: every form is denied, and a human runs it if truly needed.
# These rules read hook_cmd, so prose that names a flag passes and a flag
# after prose that holds a separator is still seen. What they cannot see is
# a git alias that carries the flag: this catches a habit, not an adversary.
#
# Git's own options may stand between `git` and the subcommand: any word
# that starts with a dash, and the value of the few that take one. A word
# may hold quoted parts with spaces in them, as in -C "my dir".
sq="'"
word="([^[:space:]\"${sq}]|\"[^\"]*\"|${sq}[^${sq}]*${sq})+"
git_globals="([[:space:]]+(-[cC][[:space:]]+${word}|--(git-dir|work-tree|namespace|config-env)[[:space:]]+${word}|-${word}))*"
# Only `commit` reads -n as --no-verify. The flag ends at a space, at the
# end, or at the bracket that closes `$(...)`. A closing quote ends it only
# inside a string that an interpreter runs (bash -c "..."), which is judged
# on the whole command below; elsewhere a quoted `git commit -n` is prose,
# as in grep or echo.
commit_n="git${git_globals}[[:space:]]+commit([[:space:]].*)?[[:space:]]-[a-zA-Z]*n[a-zA-Z]*"
commit_n_re="${commit_n}([[:space:]]|\$|[)])"
commit_n_wrapped_re="(^|[[:space:]])(bash|sh|zsh|dash|eval|ssh|su|sudo|xargs)[[:space:]].*[\"${sq}].*${commit_n}[\"${sq}]"
# Repointing or unsetting core.hooksPath disables every hook at once. Only
# the ways of setting it count: `git config`, `-c` and `--config-env`. Reading
# it, or pointing it at the repository's own .githooks, is the activation
# step and passes; a command that merely names the setting (git grep, a log
# search, grep in a README) passes too.
hookspath_config_re="git${git_globals}[[:space:]]+config[[:space:]].*core\.hookspath"
hookspath_read_re="git${git_globals}[[:space:]]+config[[:space:]]+((--local|--get)[[:space:]]+)*core\.hookspath([[:space:]]+(\./)?\.githooks/?)?[[:space:]]*\$"
hookspath_inline_re="git[[:space:]].*(-c[[:space:]]*|--config-env[=[:space:]]+)[\"${sq}]?core\.hookspath"
# The git subcommands that run a hook SKIP could leave out.
hooked="(commit|push|merge|rebase|pull|am|cherry-pick|revert)"
# Printing a Kubernetes Secret. Two questions are asked of what follows the
# word `get` in a segment: is a Secret among the resources (secret, secrets,
# secret/name, pod,secret, "secret"), and is there an output format that can
# carry a value? Whatever stands before `get` does not matter, so global flags
# (-n, --kubeconfig, --context), a variable ($K get ...), a shell function
# or an alias are all read the same way. Every format but `name` and `wide`
# counts, jsonpath and go-template on metadata included: the last-applied
# annotation of an applied Secret holds its values. `-o` may be bundled
# (-Ao yaml). A namespace or an object that is itself named `secrets` is
# denied too.
#
# Two segments can do together what neither does alone: one lists the
# Secrets, the other prints whatever a variable, `{}` or xargs hands it
# (for s in $(... get secrets -o name); do ... get $s -o yaml; done). So a
# get of Secrets and such a get in one command are denied as a pair.
#
# These rules read hook_cmd, so a commit message or a pull request body that
# names the command passes; a grep or an echo that names it is denied, as
# before.
k8s_get_re="(^|[^[:alnum:]_-])get[[:space:]]+(.*)\$"
k8s_secret_re="(^|[[:space:],\"'])secrets?([[:space:],/.)\"']|\$)"
k8s_output_re="(^|[[:space:]])(-[AwR]*o[=[:space:]]*|--output[=[:space:]]+)[\"']?([^nw=[:space:]\"']|n[^a]|w[^i])"
k8s_template_re="(^|[[:space:]])--template([=[:space:]]|\$)"
# Verbosity 8 and up (-v=8, -v 9, --v=10, -v9) makes kubectl log the API
# response body, and a response to a get of Secrets holds their values, so it
# counts as an output format that prints them. The default output and -o name
# pass. Known from kubectl's documented behaviour (--v=8 "display HTTP request
# contents", 9 "...without truncation"): the reviewer who found it and this rule
# did not run it against a cluster. It is read on the whole segment, since the
# flag may stand before `get` (kubectl -v=8 get secret x).
k8s_verbose_re="(^|[[:space:]])--?v[=[:space:]]*([89]|[1-9][0-9]+)([[:space:]]|\$)"
k8s_handed_re="[\$]|[{][}]"
k8s_xargs_re="(^|[^[:alnum:]_-])xargs[[:space:]]"
k8s_secret_seen=""
k8s_handed_seen=""
while IFS= read -r seg; do
  # Git accepts abbreviated long options (--no-veri), and only `commit` reads
  # -n as --no-verify; `git push -n` is a dry run and `git log -n` a count.
  [[ "$seg" =~ git[[:space:]].*--no-veri ]] && \
    decide deny "Skipping git hooks (--no-verify) is not allowed. Fix what the hook reports, or run it yourself."
  [[ "$seg" =~ $commit_n_re ]] && \
    decide deny "git commit -n skips the commit hooks. Fix what the hook reports, or run it yourself."
  if [[ "$seg" =~ $hookspath_inline_re ]] || \
     { [[ "$seg" =~ $hookspath_config_re ]] && ! [[ "$seg" =~ $hookspath_read_re ]]; }; then
    decide deny "Changing core.hooksPath disables the repository's git hooks. Run it yourself if intended."
  fi
  if [[ "$seg" =~ $k8s_get_re ]]; then
    k8s_rest="${BASH_REMATCH[2]}"
    k8s_values=""
    if [[ "$k8s_rest" =~ $k8s_output_re ]] || [[ "$k8s_rest" =~ $k8s_template_re ]] \
       || [[ "$seg" =~ $k8s_verbose_re ]]; then
      k8s_values=1
    fi
    if [[ "$k8s_rest" =~ $k8s_secret_re ]]; then
      [ -n "$k8s_values" ] && \
        decide deny "That would print Kubernetes Secret values to the transcript. -o name lists Secrets and kubectl describe secret shows keys and sizes; run it yourself for a value."
      k8s_secret_seen=1
    elif [ -n "$k8s_values" ]; then
      if [[ "$k8s_rest" =~ $k8s_handed_re ]] || [[ "$seg" =~ $k8s_xargs_re ]]; then
        k8s_handed_seen=1
      fi
    fi
  fi
done < <(printf '%s\n' "$hook_cmd" | sed -E 's/(&&|\|\||;|\|)/\n/g')
[[ -n "$k8s_secret_seen" && -n "$k8s_handed_seen" ]] && \
  decide deny "A get of Secrets, and a get that prints whatever a variable or xargs hands it, in one command: that can print Secret values to the transcript. Run it yourself."
# Other ways to a Secret's value. The get rule above reads `kubectl get secret`;
# these read what a pod mounts or holds in its environment, what the API prints
# for a path that names Secrets, a kubeconfig's credentials, a service
# account's token, and what a cloud's secret store hands back. They read
# hook_cmd (a commit message that names one passes), whose line continuations
# are already deleted, and the ones that look at a pod's command stop at a separator, a
# newline included, so `kubectl exec x -- ls; env FOO=1 make` is not an env
# print. Inside `sh -c "..."` a separator belongs to the pod's command: the
# body is read as far as its closing quote and no further, so a command that
# follows the quoted body (`sh -c 'ls' | grep env`) is not part of it. Neutral
# rules, none names a Meridian path.
eol=$'\n'
joined="$hook_cmd"
# What follows `exec ... --` (or `debug`: its --target shares the process
# namespace and its command is read the same way): the pod's own command,
# after a wrapper word and its options, a quote and a directory, if there are
# any. `kubectl run` is not read as exec is: a pod it starts holds none of the
# workload's own environment, and the same words follow `docker run` and `uv
# run` (the psql ask below does read `run`). But that is true only of a run
# without --overrides or --env, which can put a Secret into the new pod; those
# ask (kube_run_re below).
exec_wrap="((busybox|command|sudo|exec|nohup|nice|timeout|time|stdbuf|setsid)[[:space:]]+(-[ugC][[:space:]]+[^-[:space:]][^[:space:]]*[[:space:]]+|-[^[:space:]]*[[:space:]]+|[0-9][0-9.]*[smhd]?[[:space:]]+)*)*"
exec_head="(^|[^[:alnum:]_-])(exec|debug)[[:space:]]+[^;&|${eol}]*[[:space:]]--[[:space:]]+${exec_wrap}[\"${sq}]?([^[:space:];&|\"${sq}]*/)?"
# What ends a command: a separator, a redirect (`2>&1` included), a comment, a
# backtick, a newline, or a quote that closes a string (one that is followed
# by the end or a separator, or a run of quotes that closes a nested string
# (sh -c 'sh -c "env"' ends in "'"), not the quote that opens an argument of
# `env "PGOPTIONS=x" psql`).
exec_end="([;&|)<>#\`${eol}]|[0-9]+[<>]|\\\\[\"${sq}]|[\"${sq}]+[[:space:]]*(\$|[;&|)]))"
# env and printenv print the environment; env with options and assignments only
# does too, and so does `env -u X` (-u, -C and -S take a value). `env VAR=x
# some-command` runs the command and passes. printenv prints one variable when
# it is given one name, and that is no harm unless the name looks like it holds
# a secret (secret_name: a password, token, key, credential, URL or URI, DSN,
# the database, a certificate: DATABASE_URL holds a password). So printenv is
# read as a print of the environment when it has no name (options only), when
# it has a name of that shape anywhere, when it has two names or more, or when
# a name is a $ or backtick that the pod's shell or the session fills in;
# `printenv HOME` and `printenv PATH` pass. A name outside the list that holds
# a secret passes: this reads the name, not the value.
secret_name="(secret|passw|pass|token|key|cred|auth|uri|url|dsn|conn|database|private|cert|sign|salt)"
printenv_re="printenv(([[:space:]]+-[^[:space:]]*)*[[:space:]]*(\$|${exec_end})|[[:space:]]+[^;&|${eol}]*${secret_name}|[[:space:]]+[^[:space:];&|${eol}]+[[:space:]]+[A-Za-z_\$\"${sq}\`]|[[:space:]]+[^;&|${eol}]*[\$\`])"
env_tail="(${printenv_re}|env([[:space:]]+(-[uCS][[:space:]]+[^[:space:]]+|-[^[:space:]]*|[^[:space:]=;&|\"${sq}]+=[^[:space:];&|\"${sq}]*))*[[:space:]]*(\$|${exec_end}))"
# The shell builtins that print every variable: set, export and declare with
# nothing but -p after them (`set -e` and `export FOO=1` pass).
set_tail="(set|(export|declare|typeset)([[:space:]]+-p)?)[[:space:]]*(\$|${exec_end})"
# The files a Secret or a token lives in: a path that holds one of these words
# (token and creds as words, so tokenizer.json and token_usage.log pass), and
# the directories a pod mounts them in, which need no word.
secret_path="(/var/run/secrets/|/run/secrets/|/etc/secrets|secret|tokens?([^[:alnum:]_]|\$)|credential|creds?([^[:alnum:]_]|\$)|password|\.key|/proc/[^[:space:]]*/environ)"
# mount_path also names the directories this repository's own chart mounts a
# Secret at: /etc/meridian (the services' TLS key at /etc/meridian/tls, the
# database CA at /etc/meridian/db-ca) and /etc/redis-acl (the rate store's ACL
# file, which holds a password hash). The parent /etc/meridian is named, not
# the two children, so that `tar cf - /etc/meridian` is read too; it also takes
# /etc/meridian/telemetry-ca, a ConfigMap of public certificates (a read of it
# in a pod is denied all the same: the accepted false deny). tests/
# test_guard_bash_mounts.py reads the chart and fails when a Secret is mounted
# somewhere this does not name. Meridian's rule: the base has no such chart.
mount_path="(/var/run/secrets|/run/secrets|/etc/secrets|/etc/meridian|/etc/redis-acl|/proc/[^[:space:]]*/environ)"
reader="(cat|head|tail|less|base64|xxd|strings)"
# The pod's command as typed after `--`: env, a reader and a secret path in the
# same command, or /proc/<pid>/environ read by anything.
exec_direct="(${env_tail}|${reader}[[:space:]]+[^;&|${eol}]*${secret_path}|[^;&|${eol}]*/proc/[^[:space:]]*/environ)"
# `sh -c` and its relatives, then a body in double quotes, single quotes or
# bare. A quoted body is read up to its closing quote. A command in it starts
# at the opening quote, or after a separator, with assignments and wrapper
# words before it. Read in the body: env and printenv, set and export -p, a
# reader beside a secret path, /proc/<pid>/environ whatever reads it, and a
# reader and a mounted path anywhere in the same body, in either order.
#
# The shell's options may stand before -c: a short one (-x), a long one
# (--norc) and -o with its word (-o pipefail). The body ends at its closing
# quote, but a backslash-escaped quote in a double-quoted body is no end, and a
# closing quote that touches the next character ('x'";env") is no end either:
# the shell joins the pieces into one word. A command in the body also starts
# after the words of a compound command (do, then, else, if, while, !), and a
# shell started in the body is read the same way (sh -c 'sh -c env').
assign="([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*"
exec_sh_open="(ba|da|k|z|a)?sh[[:space:]]+(([-+][oO][[:space:]]+[A-Za-z]+|-[-a-z]+)[[:space:]]+)*"
exec_sh_nested="(([^[:space:];&|\"${sq}]*/)?${exec_sh_open}[\\\\\"${sq}]*){0,3}"
exec_sh_kw="((do|then|else|elif|if|while|until|!)[[:space:]]+)*"
exec_sh_body() { # $1=the quote that opens the body; sets exec_sh_alts
  local q="$1" in nonword at
  if [ "$q" = '"' ]; then
    in="([^\"\\\\]|\\\\.|\"[^[:space:];&|)<>])*"
  else
    in="([^${q}]|${q}[^[:space:];&|)<>])*"
  fi
  nonword="[^[:alnum:]_${q}-]"
  at="(${in}[;&|(\`{${eol}])?[[:space:]]*${exec_sh_kw}${assign}${exec_wrap}([^[:space:];&|\"${sq}]*/)?"
  exec_sh_alts="${at}${exec_sh_nested}(${env_tail}|${set_tail})"
  exec_sh_alts+="|(${in}${nonword})?${reader}[[:space:]]+[^;&|${q}]*${secret_path}"
  exec_sh_alts+="|${in}/proc/[^[:space:]${q}]*/environ"
  exec_sh_alts+="|(${in}${nonword})?${reader}[[:space:]]${in}${mount_path}"
  exec_sh_alts+="|${in}${mount_path}${in}${nonword}${reader}([[:space:]]|\$)"
}
exec_sh_body '"'
exec_sh_dq="${exec_sh_alts}"
exec_sh_body "$sq"
exec_sh_sq="${exec_sh_alts}"
exec_sh_bare="${assign}${exec_wrap}([^[:space:];&|\"${sq}]*/)?(${env_tail}|${set_tail})"
exec_pod_re="${exec_head}(${exec_direct}|${exec_sh_open}(\"(${exec_sh_dq})|${sq}(${exec_sh_sq})|${exec_sh_bare}))"
exec_msg="That would print a pod's environment or a mounted Secret to the transcript. kubectl describe pod shows which Secrets it uses; run it yourself for a value."
# Every shape this reads has a ` -- ` and exec or debug in it: a command
# without them is not run through the pattern at all.
if [[ "$joined" == *" -- "* && ( "$joined" == *exec* || "$joined" == *debug* ) && "$joined" =~ $exec_pod_re ]]; then
  decide deny "$exec_msg"
fi
# A Secret's mount read by whatever reader. The rule above knows a few readers;
# there are as many more (grep, tar, find -exec, awk, sed, od, dd, python), so
# this one reads the path and not the reader: a command after the `--` of an
# exec or debug that names a mounted path (mount_path: the service account
# directory, /run/secrets, /etc/secrets, the chart's own mounts,
# /proc/<pid>/environ) is denied unless
# its verb is ls, stat or test, which show a name or a mode and no content (or
# redis-cli, see exec_look_re).
# ERE has no negative look-ahead, so the text is cut off after each ` -- ` of
# each `exec ...` call in turn (not only the last: `-- awk 1 /etc/secrets/db
# -- x` has two, and `ls -- /etc/secrets` is denied for the second, a known
# false deny) and the verb tested on what follows. `sh -c '...'` has the verb sh: a body
# that is one ls, stat or test with no separator, redirect or substitution in
# it (the repository's tests use sh -c "ls /etc/secrets") passes like the bare
# verb, and any other body that names a mount is denied, whatever it does with
# it. The text after the
# `--` is read to its end: a mounted path in a later command of the line
# counts too, which is the safe side. A line with more than eight such calls
# is not read to the end and asks.
exec_word_re="(^|[^[:alnum:]_-])(exec|debug)[[:space:]]+"
# redis-cli is the fourth harmless verb: the rate store's probe (docs/operations/
# runbooks/rate-store.md) names its certificate and key files as arguments, uses
# them to connect and prints none of their content.
exec_look_re="^${exec_wrap}[\"${sq}]?([^[:space:];&|\"${sq}]*/)?(ls|stat|test|redis-cli)([[:space:]]|\$|[;&|)\"${sq}])"
exec_look_sh_re="^${exec_wrap}${exec_sh_open}(\"(ls|stat|test)[[:space:]][^\"\\\\;&|<>\$\`]*\"|${sq}(ls|stat|test)[[:space:]][^${sq};&|<>\$\`]*${sq})[[:space:]]*(\$|[;&|)])"
if [[ "$joined" == *" -- "* && ( "$joined" == *exec* || "$joined" == *debug* ) && "$joined" =~ $mount_path ]]; then
  exec_rest="$joined"
  exec_calls=0
  while [[ "$exec_rest" =~ $exec_word_re ]]; do
    exec_rest="${exec_rest#*"${BASH_REMATCH[0]}"}"
    # Every ` -- ` of the call is a place its command may start: the run up to
    # the next separator is walked, and the text after each is tested (the
    # last alone would miss `-- awk 1 /etc/secrets/db -- x`).
    exec_run=" ${exec_rest%%[;&|$'\n']*}"
    exec_after=" ${exec_rest}"
    while [[ "$exec_run" == *" -- "* ]]; do
      exec_run="${exec_run#*" -- "}"
      exec_after="${exec_after#*" -- "}"
      exec_after="${exec_after#"${exec_after%%[![:space:]]*}"}"
      [[ "$exec_after" =~ $mount_path ]] || break 2
      [ $(( ++exec_calls )) -le 8 ] || \
        decide ask "This command runs more than eight exec or debug calls and names a Secret mount: the guard did NOT read the rest of them, and one may read a mounted Secret it would deny; confirm that none does, or split the command."
      [[ "$exec_after" =~ $exec_look_re || "$exec_after" =~ $exec_look_sh_re ]] || \
        decide deny "That names a mounted Secret or a pod's environment file in a command run in a pod, and the guard knows no list of harmless readers: only ls, stat, test and redis-cli of such a path pass. kubectl describe pod shows which Secrets it uses; run it yourself for a value."
    done
  done
fi
# A path under /secrets in the API prints Secret values without the word get
# secret, in either order of the flag and the path and with the path
# percent-encoded (the API server decodes it: secre%74s); so does a
# kubeconfig's raw view (client keys, tokens) and a minted service account
# token (flags may stand between create and token; `create secret generic
# token` is another command and passes).
raw_secrets_re="(^|[^[:alnum:]_-])get[[:space:]]+([^;&|${eol}]*[[:space:]])?--raw([=[:space:]][^;&|${eol}]*)?/secrets|(^|[^[:alnum:]_-])get[[:space:]]+[^;&|${eol}]*/secrets[^;&|${eol}]*[[:space:]]--raw"
config_view_re="(^|[^[:alnum:]_-])config[[:space:]]+([^;&|${eol}]*[[:space:]])?view[[:space:]]+([^;&|${eol}]*[[:space:]])?--(raw|flatten)([=[:space:]]|\$)"
create_token_re="(^|[^[:alnum:]_-])create[[:space:]]+(-[^[:space:]]+([[:space:]]+[^-[:space:]][^[:space:]]*)?[[:space:]]+)*token([[:space:]]|\$)"
cp_secret_re="(^|[^[:alnum:]_-])cp[[:space:]]+([^;&|${eol}]*[[:space:]])?[^[:space:]:]+:[^[:space:]]*${secret_path}"
# `--help` (or -h) after a reader of secrets prints its usage and no value
# (aws secretsmanager get-secret-value --help, helm get values -h). `unhelped`
# succeeds when the regex matches somewhere that no whole-word --help or -h
# follows in the same segment (up to a separator or a newline); a command that
# reads once with --help and once without still counts. It looks at the first
# twenty matches and counts the rest as not helped: the safe side.
#
# The --help is looked for in what the shell passes to the command, and not in
# a quoted value or a comment, which pass nothing of the kind: `--query 'a
# --help b'` and a trailing `# --help` do not let a real read through. The
# quoted strings of the rest are blanked (at most twenty; more are not helped)
# and then the rest is cut at a # that follows a blank. `-h` is the short form
# of --help for the tools of this file, except gh, where it is --hostname:
# a match that holds the word gh is read for --help alone. A match that ends in
# a separator, a bracket, a quote or a backtick (the end class of the printers
# below) has no arguments left, so nothing follows it.
help_re="(^|[[:space:]])(--help|-h)([[:space:])\"${sq}\`]|\$)"
help_long_re="(^|[[:space:]])--help([[:space:])\"${sq}\`]|\$)"
gh_word_re="(^|[^[:alnum:]_.-])gh[[:space:]]"
quoted_re="'[^']*'|\"[^\"]*\""
unhelped() { # $1=text $2=regex
  local text="$1" re="$2" m rest helper tries=0 quotes=0
  while [[ "$text" =~ $re ]]; do
    m="${BASH_REMATCH[0]}"
    # Cutting the text after the match costs time in proportion to the length
    # of the text times that of the match (a glob over a literal): 6 s of CPU
    # for 2000 repetitions of `aws ` before a verb, at 8192 bytes. A match of
    # more than 256 bytes is no command a person types, and is counted as not
    # helped, the safe side.
    [ "${#m}" -le 256 ] || return 0
    text="${text#*"$m"}"
    [ $(( ++tries )) -lt 20 ] || return 0
    case "${m: -1}" in
      [\;\&\|\)\"\'\`]) return 0 ;;
    esac
    rest="${text%%[;&|$'\n']*}"
    while [[ "$rest" =~ $quoted_re ]]; do
      [ $(( ++quotes )) -le 20 ] || return 0
      rest="${rest/"${BASH_REMATCH[0]}"/ }"
    done
    rest=" ${rest}"
    rest="${rest%%[[:space:]]#*}"
    helper="$help_re"
    if [[ "$m" =~ $gh_word_re ]]; then
      helper="$help_long_re"
    fi
    [[ "$rest" =~ $helper ]] || return 0
  done
  return 1
}
raw_secrets_msg="That reads Secret values through the API to the transcript. -o name lists Secrets and kubectl describe secret shows keys and sizes; run it yourself for a value."
if [[ "$joined" == *--raw* ]]; then
  [[ "$joined" =~ $raw_secrets_re ]] && decide deny "$raw_secrets_msg"
  # One pass over each distinct %XX (twenty at most): enough for the words
  # that matter, bounded for a command that is nothing but percent signs.
  raw_decoded="$joined"
  for _ in {1..20}; do
    [[ "$raw_decoded" =~ %([0-9A-Fa-f][0-9A-Fa-f]) ]] || break
    raw_hex="${BASH_REMATCH[1]}"
    raw_escape="${BASH_REMATCH[0]}"
    # An encoded separator (%26 &, %3B ;, %7C |, %0A newline) is the API
    # server's data and no end of the command: decoded as such it would cut the
    # rule's reading short (a=%26 --raw .../secre%74s). It becomes a plain
    # character; the rule then reads on.
    case "$raw_hex" in
      26 | 3[Bb] | 7[Cc] | 0[Aa]) raw_char="_" ;;
      *) printf -v raw_char '%b' "\\x${raw_hex}" ;;
    esac
    raw_decoded="${raw_decoded//"$raw_escape"/"$raw_char"}"
  done
  [[ "$raw_decoded" =~ $raw_secrets_re ]] && decide deny "$raw_secrets_msg"
  # An escape left after twenty rounds was not decoded: the text may spell
  # secrets, and the loop cannot say. A --raw path with more than twenty
  # distinct escapes is not ordinary work, so the price of asking is small.
  [[ "$raw_decoded" =~ %[0-9A-Fa-f][0-9A-Fa-f] ]] && \
    decide ask "This --raw path holds more than twenty distinct percent-escapes: the guard did not decode all of them and so did NOT read what it spells, which may be a Secret read it would deny; confirm that it names none, or write the path out."
fi
[[ "$joined" =~ $config_view_re ]] && \
  decide deny "kubectl config view --raw (and --flatten) prints the kubeconfig's keys and tokens to the transcript. Run it yourself."
unhelped "$joined" "$create_token_re" && \
  decide deny "kubectl create token puts a service account token into the transcript. Run it yourself."
[[ "$joined" =~ $cp_secret_re ]] && \
  decide deny "kubectl cp out of a Secret mount or a token file copies secret material off the pod. Run it yourself."
# A cloud's secret store, read for a value. `az keyvault secret show` is denied
# below with the Key Vault rules; these are the same on the other stores.
# aws ssm get-parameter returns a SecureString's ciphertext without
# --with-decryption, so only the decrypting form is denied.
cloud_cli="(^|[^[:alnum:]_.-])"
az_secret_re="${cloud_cli}az[[:space:]]+([^;&|${eol}]*[[:space:]])?containerapp[[:space:]]+([^;&|${eol}]*[[:space:]])?secret[[:space:]]+show([[:space:]]|\$)"
az_secret_list_re="${cloud_cli}az[[:space:]]+([^;&|${eol}]*[[:space:]])?containerapp[[:space:]]+([^;&|${eol}]*[[:space:]])?secret[[:space:]]+list[[:space:]]+([^;&|${eol}]*[[:space:]])?--show-values([[:space:]=]|\$)"
gcloud_secret_re="${cloud_cli}gcloud[[:space:]]+([^;&|${eol}]*[[:space:]])?secrets[[:space:]]+versions[[:space:]]+access([[:space:]]|\$)"
aws_secret_re="${cloud_cli}aws[[:space:]]+([^;&|${eol}]*[[:space:]])?secretsmanager[[:space:]]+(batch-)?get-secret-value([[:space:]]|\$)"
aws_ssm_re="${cloud_cli}aws[[:space:]]+([^;&|${eol}]*[[:space:]])?ssm[[:space:]]+get-parameters?(-by-path|-history)?[[:space:]]+([^;&|${eol}]*[[:space:]])?--with-decryption([[:space:]=]|\$)"
for cloud_re in "$az_secret_re" "$az_secret_list_re" "$gcloud_secret_re" "$aws_secret_re" "$aws_ssm_re"; do
  unhelped "$joined" "$cloud_re" && \
    decide deny "A cloud secret store's value never enters the transcript or the command line. Run it yourself; list the names instead."
done
# The kubectl plugin view-secret prints a Secret's decoded values (krew installs
# it as kubectl-view_secret).
view_secret_re="(^|[^[:alnum:]_.])view[-_]secret([[:space:]]|\$)"
unhelped "$hook_cmd" "$view_secret_re" && \
  decide deny "kubectl view-secret prints Secret values to the transcript. kubectl describe secret shows keys and sizes; run it yourself for a value."
[[ "$hook_cmd" =~ $commit_n_wrapped_re ]] && \
  decide deny "git commit -n skips the commit hooks. Fix what the hook reports, or run it yourself."
# Environment switches are checked across the whole command, because
# `export SKIP=ruff && git commit` splits them from the git call. SKIP names
# the hooks pre-commit leaves out; other tools read a variable of that name,
# so it counts only when the command also runs a git subcommand that has
# hooks. The rest have no use other than turning hooks off.
skip_re="(^|[^[:alnum:]_])SKIP=[^[:space:];&|]+"
hooked_git_re="(^|[^[:alnum:]_.-])git${git_globals}[[:space:]]+${hooked}([[:space:]]|\$)"
[[ "$hook_cmd" =~ $skip_re && "$hook_cmd" =~ $hooked_git_re ]] && \
  decide deny "SKIP= bypasses pre-commit hooks. Fix what the hook reports, or run it yourself."
hookspath_env_re="(^|[^[:alnum:]_])(GIT_CONFIG_KEY_[0-9]+|GIT_CONFIG_PARAMETERS)=[\"${sq}]*core\.hookspath"
[[ "$hook_cmd" =~ $hookspath_env_re ]] && \
  decide deny "Changing core.hooksPath disables the repository's git hooks. Run it yourself if intended."
[[ "$hook_cmd" =~ (^|[^[:alnum:]_])(HUSKY=0|HUSKY_SKIP_HOOKS=1|LEFTHOOK=0) ]] && \
  decide deny "Disabling the hook manager (HUSKY=0, LEFTHOOK=0) bypasses git hooks. Fix what the hook reports, or run it yourself."
unhelped "$cmd" "az[[:space:]]+keyvault[[:space:]]+secret[[:space:]]+(show|set|download|backup|restore)" && \
  decide deny "Key Vault secret values never enter the transcript or the command line. Run it yourself; list secret names with 'az keyvault secret list'."
[[ "$cmd" =~ az[[:space:]].*(group|keyvault|postgres|aks|cognitiveservices|acr)[[:space:]]+(.*[[:space:]])?delete([[:space:]]|$) ]] && \
  decide deny "Azure resource delete is destructive; run it yourself after confirming subscription and resource."
[[ "$cmd" =~ kubectl[[:space:]].*delete[[:space:]].*(namespace|[[:space:]]ns[[:space:]]|pvc|persistentvolumeclaim|--all) ]] && \
  decide deny "Deleting namespaces, volumes or --all is destructive; run it yourself."

# ---- the AWS environment (S036) ----
# What a session should not do by reflex on AWS: remove the environment, run
# the wrapper (infra/terraform/aws.sh) or its make targets in a way that hides
# what it prints or changes what it runs, drive Terraform by hand against the
# module's directory, read what the wrapper keeps closed (the local file, the
# state, the plan, the AWS configuration), or call the aws CLI for a credential
# or a delete. The asks for the same family are in the next section, after the
# anchors it shares with the Azure rules. This is a guard for habits and not a
# barrier: with credentials on the machine a session reaches the same calls by
# a variable, a quote in the middle of a word, a script file, python3, uv or a
# container (the runbook's "What the command guard does not see" lists them),
# so the barrier for the second half of S036 is WHERE the credentials are
# (infra/terraform/aws/README.md, "What stops a session, and what does not").
# These rules read hook_cmd, so a commit message or a pull request body that
# names a command passes, and so does a search whose quoted pattern names one
# (aws_cmd, below); an echo that names one is denied, as for the Azure and
# Kubernetes rules above. Order matters: the denies of Terraform
# against the module's directory come before the generic asks for apply and
# state surgery below, which they would otherwise answer first.
aws_end="([[:space:]]|\$|[;&|)\"${sq}])"
aws_make_pre="(^|[^[:alnum:]_.-])(g|gnu)?make[[:space:]]+([^;&|${eol}]*[[:space:]])?[\"${sq}]?"
aws_script_pre="aws\.sh[[:space:]]+([^;&|${eol}]*[[:space:]])?[\"${sq}]?"
aws_destroy_re="${aws_make_pre}aws-destroy${aws_end}|${aws_script_pre}destroy${aws_end}"
# A pseudo-terminal tool or the shell's tracing in a command that names the
# wrapper or a target of it. The terminal check in aws.sh is `[[ -t 0 ]]` and a
# pseudo-terminal satisfies it; a trace prints what the script keeps out of its
# output; BASH_ENV, ENV and the rest run code before the script's first line.
aws_pty_named_re="aws\.sh|aws-(plan|apply|destroy)"
# The tool is read where a command starts (after VAR=value, sudo, env, time and
# the like, a separator, a bracket or a quote), so a search for the word in the
# wrapper (grep -n script infra/terraform/aws.sh) is no use of it; python3 -c
# that names pty is the other form.
aws_cmd_start="(^|[;&|(\`\"${sq}]|${eol})[[:space:]]*((sudo|time|nohup|exec|command|env|xargs|[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*)[[:space:]]+)*"
aws_pty_re="${aws_cmd_start}(script|unbuffer|expect|socat|setsid|pty)([^[:alnum:]_-]|\$)"
aws_pty_re+="|(^|[^[:alnum:]_.-])python[0-9.]*[[:space:]]+([^;&|${eol}]*[[:space:]])?-c[[:space:]].*(^|[^[:alnum:]_.-])pty([^[:alnum:]_-]|\$)"
aws_named_re="aws\.sh|aws-(validate|scan|plan|apply|destroy)"
aws_sh_word="(^|[^[:alnum:]_.-])((ba|da|k|z|a)?sh|set)"
aws_trace_flag="(-[a-zA-Z]*[xv][a-zA-Z]*|--(xtrace|verbose))"
aws_trace_re="${aws_sh_word}[[:space:]]+(-[^[:space:]]*[[:space:]]+)*${aws_trace_flag}${aws_end}"
aws_trace_re+="|${aws_sh_word}[[:space:]]+([^;&|${eol}]*[[:space:]])?[-+]o[[:space:]]+(xtrace|verbose)${aws_end}"
aws_trace_re+="|(^|[^[:alnum:]_])(SHELLOPTS|BASH_XTRACEFD|BASH_ENV|ENV|PS4)="
# Terraform's and the AWS SDK's own settings, assigned in front of a command
# that reads them: a log level, extra arguments, a workspace, a data directory,
# a configuration file, a variable, an endpoint. aws.sh drops them for its own
# children; a direct call does not.
aws_tf_assign_re="(^|[^[:alnum:]_])(TF_[A-Za-z0-9_]+|AWS_ENDPOINT_URL[A-Za-z0-9_]*)="
aws_tf_target_re="(^|[^[:alnum:]_./-])(terraform|tofu|aws)[[:space:]]|aws\.sh|(g|gnu)?make[[:space:]]+([^;&|${eol}]*[[:space:]])?[\"${sq}]?aws-"
# Terraform by hand where the module is: a -chdir or a cd into the directory,
# or the state's directory, or the plan or state file's name. The verbs that
# change the account, the state or the workspace are denied (apply and destroy
# are the wrapper's, which pins the account and the plan); the verbs that print
# the state or run unattended ask in the next section.
aws_dir_re="terraform/aws([/[:space:]\"${sq};&|)]|\$)"
aws_dir_re+="|-chdir[=[:space:]]+[\"${sq}]?([^[:space:]\"${sq}]*/)?aws([/\"${sq}[:space:]]|\$)"
aws_dir_re+="|(^|[^[:alnum:]_.-])cd[[:space:]]+[\"${sq}]?([^[:space:];&|\"${sq}]*/)?aws([/\"${sq};&|[:space:]]|\$)"
aws_dir_re+="|meridian-aws|aws\.tf(plan|state)"
aws_tf_cmd="(^|[^[:alnum:]_.-])(terraform|tofu)[[:space:]]+([^;&|${eol}]*[[:space:]])?"
aws_tf_deny_re="${aws_tf_cmd}(apply|destroy|import|force-unlock|state[[:space:]]+(mv|rm|push)|workspace[[:space:]]+(new|delete|select)|plan[[:space:]]+([^;&|${eol}]*[[:space:]])?-out)([[:space:]=]|\$|[;&|)\"${sq}])"
# The one workspace command a session may be asked about: the README's way back
# to the default workspace (`workspace select default`, which asks). It is taken
# out of the text the deny reads, so a `workspace select` of another name, a
# `new` or a `delete` beside it is still denied.
aws_ws_default="workspace select default"
# The working directory the harness passed counts as a cd into the module when
# it is the module's directory or under it.
aws_cwd_re='(^|/)terraform/aws(/|$)'
aws_in_module=""
[[ "$hook_cwd" =~ $aws_cwd_re ]] && aws_in_module=1
# What the AWS rules read: hook_cmd, with the quoted pattern of a search tool
# (grep, egrep, fgrep, rg, ag, git grep: the first word after its options, when
# it is quoted) emptied. A search for a word is not a use of it, and the files
# after the pattern are still read (grep "x" ~/.aws/credentials is denied).
aws_cmd="$hook_cmd"
if [[ "$hook_cmd" == *grep* || "$hook_cmd" == *rg* || "$hook_cmd" == *ag* ]]; then
  aws_cmd="$(printf '%s' "$hook_cmd" | sed -E "s/(^|[;&|(])([[:space:]]*(git[[:space:]]+)?(grep|egrep|fgrep|rg|ag)([[:space:]]+-[^[:space:]]*)*[[:space:]]+)(\"[^\"]*\"|'[^']*')/\1\2\"\"/g")"
  [ -n "$aws_cmd" ] || aws_cmd="$hook_cmd"
fi
# The aws CLI: a call that prints a new credential, and a delete. The same
# cloud_cli and aws_head read `aws`, the image of the CLI (amazon/aws-cli) and
# `uvx --from awscli aws` (the last is read by the word aws that ends it).
# S075's rules above already deny a secret store's
# value and ask for a session, a role, a registry or cluster token; these are
# the credential printers they leave and the deletes.
aws_head="(${cloud_cli}aws|(amazon|aws-cli)/aws-cli([:@][^[:space:];&|)\"${sq}]*)?)[[:space:]]+([^;&|${eol}]*[[:space:]])?"
aws_deny_re="${aws_head}(iam[[:space:]]+(create-access-key|create-service-specific-credential|reset-service-specific-credential)|rds[[:space:]]+generate-db-auth-token|kms[[:space:]]+decrypt|sso[[:space:]]+get-role-credentials"
aws_deny_re+="|(batch-|force-)?(delete|terminate|purge)-[a-z0-9-]+|s3[[:space:]]+(rm|rb)|--(skip-final-snapshot|force-delete-without-recovery))${aws_end}"
# What the wrapper keeps closed, read by a reader, and what steers Terraform,
# git or the aws CLI from the caller's home, written. The reader list is wide
# (it is not the list of the .env rule above, which would deny `jq '.env'`) and
# the paths are narrow. A path that only names a template (.tfvars.example)
# passes.
aws_closed_path="(local\.env|\.tfstate|\.tfplan|meridian-aws|\.aws(/|[[:space:]\"${sq}]|\$)|\.terraformrc|\.terraform\.d|\.tfvars(\.json)?(\$|[^.a-zA-Z]))"
aws_reader_re="${reader_pre}(cat|tac|nl|less|more|bat|head|tail|grep|egrep|fgrep|rg|ag|sed|awk|gawk|cut|od|hexdump|xxd|strings|base64|diff|cmp|jq|yq|sort|uniq|cp|tar|zip|python3?|perl|ruby|node|source|echo|printf|xargs|dd|rsync|scp|curl)[[:space:]].*${aws_closed_path}"
aws_steer_path="(\.terraformrc|\.gitconfig|\.config/git/|\.aws(/|[[:space:]\"${sq}]|\$)|\.terraform/environment|aws\.tfplan)"
aws_writer_re=">>?[[:space:]]*[\"${sq}]?[^[:space:]\"${sq};&|]*${aws_steer_path}"
aws_writer_re+="|${reader_pre}(tee|cp|mv|install|ln|dd|rsync|truncate)[[:space:]].*${aws_steer_path}"
aws_writer_re+="|${reader_pre}sed[[:space:]]+([^;&|${eol}]*[[:space:]])?-[a-zA-Z]*i[^;&|${eol}]*${aws_steer_path}"
if [[ "$aws_cmd" == *aws* || -n "$aws_in_module" ]]; then
  [[ "$aws_cmd" =~ $aws_destroy_re ]] && \
    decide deny "make aws-destroy and aws.sh destroy remove the AWS environment and are the owner's to run (hard rule 8): in a terminal, from a machine or user where no session holds the credentials (infra/terraform/aws/README.md, \"Removal\")."
  [[ "$aws_cmd" =~ $aws_pty_named_re && "$aws_cmd" =~ $aws_pty_re ]] && \
    decide deny "aws.sh and make aws-plan, aws-apply and aws-destroy are not run under a pseudo-terminal tool (script, unbuffer, expect, socat, setsid, pty): the wrapper's terminal check is there to stop an accident, and this is how it is passed."
  [[ "$aws_cmd" =~ $aws_named_re && "$aws_cmd" =~ $aws_trace_re ]] && \
    decide deny "aws.sh and make aws-* are not run traced (bash -x, set -x, SHELLOPTS, BASH_XTRACEFD, PS4) or with a start-up file (BASH_ENV, ENV): a trace prints the account, the address and the e-mail the script keeps out of its output, and a start-up file runs code before its first line."
  aws_tf_text="${aws_cmd//"$aws_ws_default"/workspace-keep-default}"
  [[ ( "$aws_cmd" =~ $aws_dir_re || -n "$aws_in_module" ) && "$aws_tf_text" =~ $aws_tf_deny_re ]] && \
    decide deny "Terraform by hand against infra/terraform/aws (apply, destroy, plan -out, import, state mv|rm|push, force-unlock, workspace new|delete|select of another name) skips the wrapper's account pin, plan record and state path. Use make aws-plan and make aws-apply (the owner runs them); validate, fmt and init -backend=false pass."
  unhelped "$aws_cmd" "$aws_deny_re" && \
    decide deny "That deletes AWS resources, or prints a new credential, a database login token, a decrypted value or a role's credentials to the transcript. Run it yourself; the owner's removal is make aws-destroy."
fi
if [[ "$aws_cmd" == *TF_* || "$aws_cmd" == *AWS_ENDPOINT_URL* ]]; then
  [[ "$aws_cmd" =~ $aws_tf_assign_re && "$aws_cmd" =~ $aws_tf_target_re ]] && \
    decide deny "A TF_* or AWS_ENDPOINT_URL* assignment in front of terraform, tofu, aws, aws.sh or make aws-* changes what they run (a log level, extra arguments, a variable, a workspace, a configuration file, an endpoint). Set the value in the module or the local file instead."
fi
if [[ "$aws_cmd" == *local.env* || "$aws_cmd" == *tfstate* || "$aws_cmd" == *tfplan* \
      || "$aws_cmd" == *meridian-aws* || "$aws_cmd" == *.aws* || "$aws_cmd" == *terraformrc* \
      || "$aws_cmd" == *terraform.d* || "$aws_cmd" == *tfvars* || "$aws_cmd" == *.gitconfig* \
      || "$aws_cmd" == *config/git/* || "$aws_cmd" == *.terraform/environment* ]]; then
  while IFS= read -r seg; do
    [[ "$seg" =~ $aws_reader_re ]] && \
      decide deny "That would print what the AWS wrapper keeps closed (the local file with the account and address, the state, the plan and its record, a variable file, the AWS configuration and sign-in cache, Terraform's own configuration) to the transcript. Run it yourself."
    [[ "$seg" =~ $aws_writer_re ]] && \
      decide deny "That writes a file that steers Terraform, git or the aws CLI from the caller's home (~/.terraformrc, ~/.gitconfig, ~/.config/git, ~/.aws), the workspace file or the saved plan. Run it yourself if intended."
  done < <(printf '%s\n' "$aws_cmd" | sed -E 's/(&&|\|\||;|\|)/\n/g')
fi

# ---- confirmations ----
[[ "$cmd" =~ (terraform|tofu)[[:space:]].*apply ]] && \
  decide ask "terraform apply mutates cloud infrastructure; confirm the plan and workspace first."
# Deleting the local kind cluster does not ask: `make down` and
# infra/kind/down.sh pass, and so does a `kind delete cluster` that names the
# cluster. The cluster is disposable on the development machine, `make up`
# makes it again from the repository, and a test may need it made again (the
# owner, 2026-10-06; AGENTS.md, hard rule 8). That word covers the local
# cluster only. down.sh and make down refuse any other name themselves; a raw
# `kind delete` does not (kind's default name is `kind`, and `clusters --all`
# deletes every cluster on the machine), so it asks unless the segment is
#   kind [global flags] delete cluster ... --name meridian ...
# with exactly one --name, spelled --name meridian or --name=meridian (quotes
# allowed), no --all and no command substitution. meridian is CLUSTER_NAME in
# infra/kind/pins.env, which infra/kind/common.sh reads. A segment is cut at
# newlines, ;, &, && and |, so each `kind delete` of a command is judged alone.
# Anything else, sudo or a path before kind included, asks.
nl=$'\n'
# The longest command, in characters, that the kind rules below read segment by
# segment: they cost time in proportion to the length, and a hook has ten
# seconds. A longer command that holds such a call asks without being read.
# The text the rules read (cmd, after the heredoc pass) is already held to this
# many bytes, and characters are never more than bytes. The command as typed
# (raw_cmd) may be longer, up to guard_max_typed_bytes, when a heredoc that is
# written to a file was dropped: the check on raw_cmd below is live for that.
kind_scan_max=8192
kind_delete_re="(^|[^[:alnum:]_.-])kind([[:space:]]+-[^[:space:]]*([[:space:]]+[0-9]+)?)*[[:space:]]+delete([[:space:]]|\$)"
kind_delete_local_re="^[[:space:]]*kind([[:space:]]+-[^[:space:]]*([[:space:]]+[0-9]+)?)*[[:space:]]+delete[[:space:]]+cluster[[:space:]]"
kind_delete_name_re="(^|[[:space:]])--name(=|[[:space:]]+)(meridian|\"meridian\"|'meridian')([[:space:]]|\$)"
if [[ "$cmd" =~ $kind_delete_re ]]; then
  [ "${#cmd}" -le "$kind_scan_max" ] || \
    decide ask "kind delete in a command too long to check (over ${kind_scan_max} characters); use make down, or split the command."
  while IFS= read -r seg; do
    [[ "$seg" =~ $kind_delete_re ]] || continue
    without="${seg//--name/}"
    if [[ "$seg" =~ $kind_delete_local_re && "$seg" =~ $kind_delete_name_re \
          && $(( ${#seg} - ${#without} )) -eq 6 \
          && "$seg" != *--all* && "$seg" != *\$\(* && "$seg" != *'`'* ]]; then
      continue
    fi
    decide ask "kind delete outside the local cluster; confirm the cluster, or use make down, or name it: kind delete cluster --name meridian."
  done < <(printf '%s\n' "$cmd" | sed -E 's/(&&|\|\||;|\||&)/\n/g')
fi
# `make grafana-password` and the script behind it exist to print a password.
# A person runs them in a terminal of their own, which never meets this hook;
# in a session the output is the transcript, so the owner is asked first.
# `make` is matched as a whole word, any options may precede the target, and
# the target must end at a space, separator or the end of the line; a script
# needs its path or an interpreter. `make grafana` and `grafana.sh forward`
# pass.
grafana_make_re="(^|[^[:alnum:]_.-])make[[:space:]]+([^\;\&\|${nl}]*[[:space:]])?grafana-password([[:space:]]|\$|[;\&\|\)])"
grafana_script_re="(^|[\;\&\|\(${nl}])[[:space:]]*((bash|sh|zsh)[[:space:]]+)?([^[:space:]]*infra/kind/|\./)grafana\.sh[[:space:]]+password([[:space:]]|\$|[;\&\|\)])"
if [[ "$cmd" =~ $grafana_make_re ]] || [[ "$cmd" =~ $grafana_script_re ]]; then
  decide ask "This prints the Grafana admin password into the transcript; confirm, or run it in a terminal of your own."
fi
# `make azure-state`, `make azure-apply` and the scripts behind them create
# Azure resources without the word terraform or az on the command line, so the
# rules above never see them. Same anchoring as the Grafana rules, widened for
# the ways a command can be dressed: leading VAR=value assignments, `env`, `time`,
# quoted targets, and a body inside `bash -c "..."` or `sh -c '...'` (a quote
# may start a command). `azure-plan`, `azure-smoke`, `foundation.sh plan|smoke`
# and `shellcheck .../state.sh` pass.
sq="'"
cmd_start="(^|[;&|(\`\"${sq}]|${nl})[[:space:]]*((time|nohup|exec|command)[[:space:]]+)?"
assignment="([A-Za-z_][A-Za-z0-9_]*=(\"[^\"]*\"|${sq}[^${sq}]*${sq}|[^[:space:]]*)[[:space:]]+)*"
runner="${cmd_start}${assignment}(env[[:space:]]+${assignment})?"
script_end="([[:space:]]|\$|[;\&\|\)\"${sq}])"
azure_make_re="(^|[^[:alnum:]_.-])make[[:space:]]+([^\;\&\|${nl}]*[[:space:]])?[\"${sq}]?azure-(state|apply)[\"${sq}]?${script_end}"
[[ "$cmd" =~ $azure_make_re ]] && \
  decide ask "make azure-state and make azure-apply create or change Azure resources; confirm the plan and subscription first."
# An interpreter may be followed by flags and a bare script name (after a cd);
# without an interpreter the script needs a path (infra/terraform/ or ./).
script_path="((bash|sh|zsh)[[:space:]]+(-[a-z]+[[:space:]]+)*[\"${sq}]?([^[:space:]]*/)?|[\"${sq}]?([^[:space:]]*infra/terraform/|\./))"
state_re="${runner}${script_path}state\.sh${script_end}"
[[ "$cmd" =~ $state_re ]] && \
  decide ask "infra/terraform/state.sh creates Azure resources; confirm the plan and subscription first."
foundation_apply_re="${runner}${script_path}foundation\.sh[[:space:]]+[\"${sq}]?apply${script_end}"
[[ "$cmd" =~ $foundation_apply_re ]] && \
  decide ask "foundation.sh apply creates or changes Azure resources; confirm the plan and subscription first."
# The asks for the AWS environment (S036); the denies are above, with the
# reasoning. `make aws-validate` and `make aws-scan` change nothing in AWS and
# pass, as `make azure-plan` does. They read hook_cmd (a commit message that
# names a target passes).
#   - make aws-apply and aws.sh apply cost money; make aws-plan and aws.sh plan
#     sign in with the owner's credentials, which no session should hold.
#   - make TRIVY_IMAGE= and PROMTOOL_IMAGE= replace a pinned image (the
#     Makefile's := is overridden by a command-line assignment, not by the
#     environment).
#   - Terraform against the module's directory with a verb that prints the
#     state or an output, or evaluates (show, output, console, refresh, state
#     list|show|pull), and -auto-approve anywhere. Foundation's `terraform
#     output` and `state list` pass, as before.
#   - any aws call whose operation is not on the read list: describe-*, list-*,
#     sts get-caller-identity, help, --version, sso login, configure list. The
#     call is read where a command starts (aws is a word of a command, not a
#     path component: terraform -chdir=infra/terraform/aws state list is not
#     one), in the image of the CLI, and after uvx --from awscli; --help passes.
#   - python3 or uv run that imports boto3, botocore or awscli.
aws_apply_re="${aws_make_pre}aws-apply${aws_end}|${aws_script_pre}apply${aws_end}"
aws_plan_re="${aws_make_pre}aws-plan${aws_end}|${aws_script_pre}plan${aws_end}"
aws_image_re="(^|[^[:alnum:]_.-])(g|gnu)?make[[:space:]]+([^;&|${eol}]*[[:space:]])?[\"${sq}]?(TRIVY|PROMTOOL)_IMAGE="
aws_tf_ask_re="${aws_tf_cmd}(plan|show|output|console|refresh|state[[:space:]]+(list|show|pull)|workspace[[:space:]]+select[[:space:]]+default)${aws_end}"
aws_auto_re="(^|[^[:alnum:]_.-])(terraform|tofu|terragrunt)[[:space:]]+([^;&|${eol}]*[[:space:]])?-auto-approve([[:space:]=]|\$)"
aws_call_end="([[:space:]]|\$|[;&|])"
aws_call_re="(${runner}((sudo|nice|xargs|exec|command|time|nohup)[[:space:]]+)*((do|then|else|elif|if|while|until|!)[[:space:]]+)*(/[^[:space:];&|\"${sq}]*/)?aws${aws_call_end})"
aws_call_re+="|((amazon|aws-cli)/aws-cli([:@][^[:space:];&|)\"${sq}]*)?${aws_call_end})"
aws_call_re+="|(--(from|with)[[:space:]=]+awscli[^[:space:];&|]*[[:space:]]+aws${aws_call_end})"
aws_boto_re="(^|[^[:alnum:]_.-])(python[0-9.]*|uv[[:space:]]+run)[[:space:]]+([^;&|${eol}]*[^[:alnum:]_.-])?(boto3|botocore|awscli)([^[:alnum:]_.-]|\$)"
aws_call_listed() { # $1=the text after `aws`; succeeds when the call is on the read list
  local rest="${1%%[;&|)\`$'\n']*}" tok skip="" service="" op="" n=0
  local -a words=()
  read -ra words <<<"$rest" || true
  for tok in "${words[@]}"; do
    [ $(( ++n )) -le 400 ] || return 1 # a longer call is not read: it asks
    if [ -n "$skip" ]; then
      skip=""
      continue
    fi
    case "$tok" in
      --help | -h) return 0 ;;
      --version) [ -n "$service" ] || return 0 ;;
      --region | --profile | --output | --query | --endpoint-url | --ca-bundle | --color \
        | --cli-read-timeout | --cli-connect-timeout | --cli-binary-format) skip=1 ;;
      -*) ;;
      *)
        # A quote is no part of the name: aws "ec2" run-instances and bash -c
        # "aws sts get-caller-identity" read as ec2 and get-caller-identity. A
        # token that is nothing but quotes names nothing, and asks (it would
        # leave the slot empty for a later value to fill).
        tok="${tok#[\"\']}"
        tok="${tok%%[\"\']*}"
        [ -n "$tok" ] || return 1
        if [ -z "$service" ]; then
          service="$tok"
        elif [ -z "$op" ]; then
          op="$tok"
        fi
        ;;
    esac
  done
  [ -n "$service" ] || return 1
  [ "$service" != help ] || return 0
  # The review's read list, and the reads S075's cases already pass: s3 ls,
  # configure get (a secret key or token asks above) and the ssm get-parameter
  # family (--with-decryption is denied above, so only ciphertext is returned).
  case "${service}/${op}" in
    */describe-* | */list-* | */help | sts/get-caller-identity | sso/login | sso/logout | s3/ls \
      | configure/list | configure/get | ssm/get-parameter | ssm/get-parameters \
      | ssm/get-parameters-by-path | ssm/get-parameter-history) return 0 ;;
  esac
  return 1
}
if [[ "$aws_cmd" == *aws* || "$aws_cmd" == *terraform* || "$aws_cmd" == *tofu* || "$aws_cmd" == *_IMAGE=* ]]; then
  [[ "$aws_cmd" =~ $aws_apply_re ]] && \
    decide ask "make aws-apply and aws.sh apply create the AWS environment (EKS, RDS, ECR, the network, a budget) and COST MONEY: it bills by the hour until make aws-destroy. The owner runs it, after reading the plan, from a machine or user where no session holds the credentials (infra/terraform/aws/README.md); confirm only if that is where this runs."
  [[ "$aws_cmd" =~ $aws_plan_re ]] && \
    decide ask "make aws-plan and aws.sh plan sign in to AWS with the owner's credentials and read the account; they need those credentials, and no session should hold them (infra/terraform/aws/README.md). Confirm that this is the owner's own session."
  [[ "$aws_cmd" =~ $aws_image_re ]] && \
    decide ask "TRIVY_IMAGE= and PROMTOOL_IMAGE= replace an image the Makefile pins by digest; confirm the image and why the pin is not used."
  [[ "$aws_cmd" =~ $aws_auto_re ]] && \
    decide ask "-auto-approve runs Terraform without its own question; confirm the workspace and the plan."
  [[ ( "$aws_cmd" =~ $aws_dir_re || -n "$aws_in_module" ) && "$aws_cmd" =~ $aws_tf_ask_re ]] && \
    decide ask "terraform plan (it signs in with the owner's credentials), show, output, console, refresh, state list|show|pull (they print the state or its outputs: the database's secret ARN, the cluster's endpoint) and workspace select default, against infra/terraform/aws; confirm that this is the owner's own session and that the transcript may hold them, or run it in a terminal of your own."
fi
if [[ "$aws_cmd" == *aws* || "$aws_cmd" == *boto* ]]; then
  aws_text="$aws_cmd"
  aws_calls=0
  # The CLI is spelled in lower case: AWS at the start of a table cell or a
  # sentence is no call, so the pattern is read with case on.
  shopt -u nocasematch
  while [[ "$aws_text" =~ $aws_call_re ]]; do
    aws_seen="${BASH_REMATCH[0]}"
    [ "${#aws_seen}" -le 256 ] || \
      decide ask "An aws call after more than 256 bytes of what stands before it: the guard did NOT read its operation; confirm that it is a read (describe-*, list-*), or split the command."
    [ $(( ++aws_calls )) -le 8 ] || \
      decide ask "This command holds more than eight aws calls: the guard did NOT read the rest of them, and one may create, change or delete; confirm that none does, or split the command."
    aws_text="${aws_text#*"$aws_seen"}"
    aws_call_listed "$aws_text" || \
      decide ask "An aws call whose operation is not on the read list (describe-*, list-*, sts get-caller-identity, help, --version, sso login and logout, configure list and get, s3 ls, ssm get-parameter*): it can create, change or delete in the account. Confirm the account and the call, or run it in a terminal of your own. --help passes."
  done
  shopt -s nocasematch
  [[ "$aws_cmd" =~ $aws_boto_re ]] && \
    decide ask "python or uv run with boto3, botocore or awscli reaches AWS without the aws CLI's rules; confirm the account and the call."
fi
# Deletes and purges, state surgery, and a bearer token in the transcript.
# (Deletes of resource groups, vaults, databases, clusters, registries and
# Cognitive Services accounts are denied above; this asks for the rest, such as
# locks, storage accounts and role assignments.)
[[ "$cmd" =~ (^|[^[:alnum:]_.-])az[[:space:]]+([^\;\&\|${nl}]*[[:space:]])?(delete|purge)([[:space:]]|$) ]] && \
  decide ask "Azure delete or purge is destructive; confirm the subscription and resource (hard rule 8)."
[[ "$cmd" =~ (^|[^[:alnum:]_.-])terraform[[:space:]]+([^\;\&\|${nl}]*[[:space:]])?(state[[:space:]]+(rm|push|mv|pull)|force-unlock|import|taint|untaint)([[:space:]]|$) ]] && \
  decide ask "terraform state surgery, import, taint or force-unlock can orphan or corrupt resources, and state pull prints resource secrets; confirm the workspace and the reason."
[[ "$cmd" =~ (^|[^[:alnum:]_.-])az[[:space:]]+([^\;\&\|${nl}]*[[:space:]])?account[[:space:]]+get-access-token ]] && \
  decide ask "That would put a bearer token into the transcript; run it yourself, or use a script that passes it on stdin."
# make gateway-upkeep (infra/kind/upkeep.sh): credit and close change a tenant's
# budget ledger, and expire --confirm deletes months of it; reservations, and an
# expire without --confirm, only read. ARGS is a make variable and an
# environment variable, so it may stand before or after the target, and the
# segment is judged as a whole. It fails closed: a segment that names the target
# asks unless it shows exactly one ARGS= whose first word is reservations or
# expire, with no --confirm. A variable, any other verb, no ARGS at all
# (export ARGS=...; make ...) and a bare `make gateway-upkeep` (a no-op the
# script refuses) therefore ask. The script splits ARGS on blanks into words of
# letters, digits, . _ = and -, and its first word is the subcommand, so the
# first word is what is read. It is a segment-by-segment loop of its own, after
# every deny above, so that a deny in another segment of the same command
# still wins. Reads hook_cmd: a commit message that names the target passes.
# make is read as make, gmake and gnumake; an ARGS that holds a $ or a
# backtick asks, since the guard cannot read what the shell or make will
# expand it to (the rule fails closed on it, in single quotes too).
upkeep_make_re="(^|[^[:alnum:]_.-])(g|gnu)?make[[:space:]]+([^;&|]*[[:space:]])?[\"${sq}]?gateway-upkeep[\"${sq}]?([[:space:]]|\$|[;&|)])"
# The script, run: at the start of a segment or of a bracket (a subshell, a
# $( )) or of the body of a shell's -c, behind VAR=value words, env, time (and
# nohup, exec, command, sudo), an interpreter with flags but not -n (bash -n
# only checks the syntax) or source and the dot; a bare mention of its name
# (cat, shellcheck, git diff, grep) is not a run and passes.
upkeep_script_re="((^|[(\`])[[:space:]]*|(^|[^[:alnum:]_.-])(bash|sh|zsh)[[:space:]]+(-[a-z]+[[:space:]]+)*-[a-z]*c[[:space:]]+[\"${sq}][[:space:]]*)((time|nohup|exec|command|sudo)[[:space:]]+)?${assignment}(env[[:space:]]+${assignment})?((bash|sh|zsh)[[:space:]]+(-[a-mo-z]+[[:space:]]+)*|(source|\.)[[:space:]]+)?[\"${sq}]?([^[:space:]]*/)?upkeep\.sh([[:space:]]|\$|[;&|)\"${sq}])"
upkeep_target_re="${upkeep_make_re}|${upkeep_script_re}"
upkeep_args_pre="(^|[[:space:]\"${sq}])"
upkeep_twice_re="${upkeep_args_pre}ARGS=.*[[:space:]\"${sq}]ARGS="
upkeep_read_re="${upkeep_args_pre}ARGS=[\"${sq}]?[[:space:]]*(reservations|expire)([[:space:]\"${sq}]|\$)"
upkeep_confirm_re="(^|[[:space:]=\"${sq}])--confirm([[:space:]=\"${sq}]|\$)"
# make expands $( ) and ${ } in ARGS even inside single quotes, and the shell
# expands $X and a backtick in double quotes: the guard cannot read what
# the word will become (--conf$(EMPTY)irm), so an ARGS that holds either asks.
upkeep_expand_re="${upkeep_args_pre}ARGS=.*[\$\`]"
if [[ "$hook_cmd" == *upkeep* ]]; then
  while IFS= read -r seg; do
    [[ "$seg" =~ $upkeep_target_re ]] || continue
    if ! [[ "$seg" =~ $upkeep_read_re ]] || [[ "$seg" =~ $upkeep_confirm_re || "$seg" =~ $upkeep_twice_re \
         || "$seg" =~ $upkeep_expand_re ]]; then
      decide ask "make gateway-upkeep credit, close and expire --confirm change a tenant's budget ledger on the local cluster; confirm the tenant, the amount and the reason, or run it in a terminal of your own. reservations and expire without --confirm only read."
    fi
  done < <(printf '%s\n' "$hook_cmd" | sed -E 's/(&&|\|\||;|\|)/\n/g')
fi
# kubectl run with --overrides or --env. The pod it starts holds none of the
# workload's own environment, but --overrides is a pod spec of the caller's
# making (an env entry with a secretKeyRef, envFrom, a Secret volume) and
# --env sets variables: either can put a Secret into the new pod, and the
# command after its `--` then prints it. So these ask; `kubectl run` without
# them passes, as do `docker run --env`, `uv run` and `gh run`, which are not
# kubectl. The command after the `--` is not read as exec's is.
kube_run_re="(^|[^[:alnum:]_.-])kubectl[[:space:]]+([^;&|${eol}]*[[:space:]])?run[[:space:]]+([^;&|${eol}]*[[:space:]])?--(overrides|env)([=[:space:]]|\$)"
[[ "$hook_cmd" =~ $kube_run_re ]] && \
  decide ask "kubectl run with --overrides or --env can put a Secret into the new pod (a secretKeyRef, envFrom or a Secret volume), and its command can print it; confirm what the pod carries."
# Other printers of a credential, which ask as `az account get-access-token`
# does: a service account token minted through the API (create --raw ...
# /serviceaccounts/NAME/token; `create token` is denied above), the aws
# commands that print a session, a role's credentials, a registry password, a
# cluster token, a registry authorization token, a federation token or the
# stored secret key, gh auth token and gh auth status --show-token, gcloud's
# access and identity tokens, the az commands that print a registry credential,
# a storage key, a login token or a new service principal's secret (created or
# reset), helm's hook manifests
# (in the helm rule below), crictl inspect (a container's environment, secret
# values injected into it included: on a kind node it is reached with docker
# exec), and the path of the Secrets API anywhere in a command (kubectl proxy
# and curl, after the --raw deny above). Each reads hook_cmd and passes with
# --help. Not asked, and why: aws configure get of a name that holds no
# secret, gh auth status, gcloud auth list, and the az and aws reads that
# list or describe.
cred_cli="${cloud_cli}"
# What may follow the last word of a printer: a blank, the end, a separator, a
# closing bracket, a quote or a backtick, so that $(gh auth token), `gh auth
# token`, TOKEN=$(gcloud auth print-access-token), "$(gh auth token)" and
# gh auth token|wc -c are read as the spaced form is.
cred_end="([[:space:]]|\$|[;&|)\"${sq}\`])"
cred_ask_res=(
  "(^|[^[:alnum:]_-])create[[:space:]]+[^;&|${eol}]*(--raw[^;&|${eol}]*/serviceaccounts/[^;&|${eol}]*/token|/serviceaccounts/[^;&|${eol}]*/token[^;&|${eol}]*--raw)"
  "${cred_cli}aws[[:space:]]+([^;&|${eol}]*[[:space:]])?(sts[[:space:]]+(get-session-token|get-federation-token|assume-role(-with-saml|-with-web-identity)?)|configure[[:space:]]+export-credentials|ecr[[:space:]]+(get-login-password|get-authorization-token)|eks[[:space:]]+get-token)${cred_end}"
  "${cred_cli}aws[[:space:]]+([^;&|${eol}]*[[:space:]])?configure[[:space:]]+get[[:space:]]+[\"${sq}]?aws_(secret_access_key|session_token)${cred_end}"
  "${cred_cli}gh[[:space:]]+auth[[:space:]]+token${cred_end}"
  "${cred_cli}gh[[:space:]]+auth[[:space:]]+status[[:space:]]+([^;&|${eol}]*[[:space:]])?(--show-token|-[A-Za-z]*t[A-Za-z]*)${cred_end}"
  "${cred_cli}gcloud[[:space:]]+([^;&|${eol}]*[[:space:]])?auth[[:space:]]+(application-default[[:space:]]+)?print-(access|identity)-token${cred_end}"
  "${cred_cli}az[[:space:]]+([^;&|${eol}]*[[:space:]])?(acr[[:space:]]+credential[[:space:]]+show|storage[[:space:]]+account[[:space:]]+keys[[:space:]]+list|ad[[:space:]]+sp[[:space:]]+(create-for-rbac|credential[[:space:]]+reset))${cred_end}"
  "${cred_cli}az[[:space:]]+([^;&|${eol}]*[[:space:]])?acr[[:space:]]+login[[:space:]]+([^;&|${eol}]*[[:space:]])?--expose-token([[:space:]=]|\$|[;&|)\"${sq}\`])"
  "${cred_cli}crictl[[:space:]]+([^;&|${eol}]*[[:space:]])?inspect${cred_end}"
  "/api/v1/(namespaces/[^/[:space:]]+/)?secrets([/?[:space:]\"${sq}]|\$)"
)
if [[ "$hook_cmd" == *token* || "$hook_cmd" == *credential* || "$hook_cmd" == *login-password* \
      || "$hook_cmd" == *get-session* || "$hook_cmd" == *assume-role* || "$hook_cmd" == *crictl* \
      || "$hook_cmd" == *rbac* || "$hook_cmd" == *keys* || "$hook_cmd" == */secrets* \
      || "$hook_cmd" == *configure* || "$hook_cmd" == *"auth status"* ]]; then
  for cred_re in "${cred_ask_res[@]}"; do
    unhelped "$hook_cmd" "$cred_re" && \
      decide ask "That prints a token, a key or a Secret's value to the transcript (a credential printer, or a path of the Secrets API); run it yourself, or use a script that passes the value on stdin. With --help it passes."
  done
fi
# psql through `kubectl exec` runs SQL inside the database pod, where the
# default login is usually the superuser. The `--` that kubectl exec puts
# before the pod's command tells it from a local psql, from `docker exec` and
# from a search for the words. Read on the whole command, because the SQL or
# a shell wrapper after `--` may hold a separator; so any psql after the `--`
# asks, a `which psql` and a local psql in a later command included. It
# reads hook_cmd: a commit message that names the command passes.
#
# The same goes for the dumps (pg_dump, pg_dumpall, pg_restore: a dump holds
# every row, a restore writes them), and for the pod that `kubectl run` or
# `kubectl debug` starts for the purpose: the command after its `--` is read
# the same way. `kubectl cnpg psql` is the plugin's own way to the same
# prompt. `docker exec <container> psql` is not read: the tests' own database
# is reached that way, and the runbook lists it as not covered.
psql_exec_re="(^|[^[:alnum:]_-])(exec|run|debug)[[:space:]].*[[:space:]]--[[:space:]](.*[[:space:]/\"';|&(])?(psql|pg_dump|pg_dumpall|pg_restore)([[:space:]\"';|&<>)]|\$)"
[[ "$hook_cmd" =~ $psql_exec_re ]] && \
  decide ask "psql, pg_dump or pg_restore through kubectl exec, run or debug reaches the database as its superuser or dumps it; confirm the statement and the kube context."
cnpg_psql_re="(^|[^[:alnum:]_.])cnpg[[:blank:]]+([^;&|${nl}]*[[:blank:]])?psql([[:space:]]|\$)"
[[ "$hook_cmd" =~ $cnpg_psql_re ]] && \
  decide ask "kubectl cnpg psql runs SQL in the database pod, usually as its superuser; confirm the statement and the kube context."
# kind get kubeconfig prints the cluster's admin credentials. It asks, it does
# not deny: writing it to a file is the legitimate use (kind export kubeconfig
# does that and passes; so do the repository's scripts, which this hook never
# reads). Reading a node's admin.conf is denied with the other readers above.
kind_kubeconfig_re="(^|[^[:alnum:]_.-])kind[[:blank:]]+([^;&|${nl}]*[[:blank:]])?get[[:blank:]]+kubeconfig([[:space:]]|\$)"
[[ "$hook_cmd" =~ $kind_kubeconfig_re ]] && \
  decide ask "kind get kubeconfig prints the cluster's admin credentials to the transcript; write it to a file instead (kind export kubeconfig --kubeconfig <file>), or confirm."
# helm get manifest, values and all print a release's rendered manifests and
# values, which can hold Secret data. They ask, they do not deny: reading a
# release's values is routine (the rollback runbook does). Options may stand
# between get and the verb, and the verb does not span a line. A call inside a
# script file is not a typed command and is not seen. hooks prints the hook
# manifests (this chart's Jobs: their environment and Secret references). notes
# and metadata are left out: metadata prints the chart's name, version and
# status, and notes prints the chart's NOTES.txt, which this chart does not have
# (a chart that renders a value into its notes would print it: that is a fact
# about this chart, not about helm).
helm_get_re="(^|[^[:alnum:]_.-])helm[[:blank:]]+([^;&|${nl}]*[[:blank:]])?get[[:blank:]]+([^;&|${nl}]*[[:blank:]])?(manifest|values|all|hooks)([[:space:]]|\$)"
unhelped "$hook_cmd" "$helm_get_re" && \
  decide ask "helm get manifest, values, hooks and all print a release's rendered manifests, hook manifests and values, which can hold Secret data; confirm that this release has none, or run it yourself."
[[ "$cmd" =~ helm[[:space:]]+(uninstall|delete|rollback) ]] && \
  decide ask "This changes a running Helm release; confirm the release and the kube context."
# A mutating kubectl call asks, because the current kube context may not be the
# one meant. A call that names the local kind cluster has answered that, so
# apply, scale and rollout restart pass when EVERY kubectl invocation in the
# command names it. The scan reads the command as typed (raw_cmd), not the text
# the heredoc pass left, because that pass also drops lines the shell still runs
# (after a here-string, after a quoted "<<X", after a heredoc piped to
# something that executes it): a heredoc that mentions kubectl, followed by a
# kind call, therefore asks. Recognised, and nothing else:
# - an invocation that starts a segment (a segment is cut at newlines, ;, &, &&,
#   || and |; `do`, `then` and `else` may stand before it), with no second
#   `kubectl` in the segment (in any case, $( ) and backticks included), no
#   --server, --cluster or -s (alone or bundled: -Rs https://x), and every
#   --context and --kubeconfig it carries (a space or = before the value)
#   outside quotes and of these forms, at least one of them present:
#   --context kind-<name>; --kubeconfig <path> where the path is
#   infra/kind/kubeconfig or ends in /infra/kind/kubeconfig, or is a shell
#   variable ($K, ${K}, quoted or not) that an earlier segment of the same
#   command assigned such a path (K=path or export K=path alone in its
#   segment, after a newline, ; or at the start, or after && when no other
#   separator comes before the use). A later assignment of something else,
#   a for or read of that name, or an assignment after || or | withdraws it,
#   and so does a & or | right after it (a subshell or a background job).
#   Where the command mentions KUBECONFIG (kind_env_re), --context names
#   nothing: it only picks a context inside the file KUBECONFIG selects, and
#   only --kubeconfig counts.
# - Anything else asks as before: no cluster named, another context or
#   kubeconfig, sudo, env, time or a path before kubectl, kubectl inside
#   $( ), ( ), bash -c or xargs, a # that starts a comment anywhere in the
#   command (the quoting of a line is not parsed), a kind name only in an echo.
# kubectl delete asks on kind as well, and so do apply --prune and --force;
# the denies above it are not touched.
# This reads like the other rules, as a pattern on what a session types, not
# as a boundary: a flag inside a quoted string is skipped by quote parity only.
kind_path_re="^([^[:space:]\"${sq}\`;&|()<>]*/)?infra/kind/kubeconfig\$"
kind_dq_re='^"([^"]*)"$'
kind_sq_re="^${sq}([^${sq}]*)${sq}\$"
# shellcheck disable=SC2016  # a regex that names a literal $
kind_var_re='^\$(\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$'
kind_flag_re='(^|[[:space:]])--(kubeconfig|context)(=|[[:space:]]+)([^[:space:]]*)'
# -s may be bundled with other short flags (-Rs https://x, -Rshttps://x).
kind_other_re='(^|[[:space:]])(--(server|cluster)([=[:space:]]|$)|-[A-Za-z]*s([^-]|$))'
# KUBECONFIG, as a whole word, anywhere in the command (an assignment, an
# export, a read, a $KUBECONFIG): kubectl then reads another file than the
# default, and --context only picks a context inside it.
kind_env_re='(^|[^[:alnum:]_])KUBECONFIG([^[:alnum:]_]|$)'
kind_word='[Kk][Uu][Bb][Ee][Cc][Tt][Ll]' # in any case, as the rule above reads it
kind_unquote() { # sets kind_value, and kind_single when it was single-quoted
  kind_value="$1"
  kind_single=""
  if [[ "$1" =~ $kind_dq_re ]]; then
    kind_value="${BASH_REMATCH[1]}"
  elif [[ "$1" =~ $kind_sq_re ]]; then
    kind_value="${BASH_REMATCH[1]}"
    kind_single=1
  fi
}
kind_invocation() { # $1=segment, $2=names usable as a kubeconfig path
  local seg="$1" vars="$2" rest named="" flag name pre quotes without
  [[ "$seg" =~ ^((do|then|else)[[:space:]]+)?kubectl[[:space:]] ]] || return 1
  without="${seg//$kind_word/}"
  [ $(( ${#seg} - ${#without} )) -eq 7 ] || return 1
  [[ "$seg" =~ $kind_other_re ]] && return 1
  rest="$seg"
  while [[ "$rest" =~ $kind_flag_re ]]; do
    flag="${BASH_REMATCH[2]}"
    pre="${rest%%"${BASH_REMATCH[0]}"*}"
    rest="${rest#*"${BASH_REMATCH[0]}"}"
    kind_unquote "${BASH_REMATCH[4]}"
    quotes="${pre//[^$sq]/}"
    [ $(( ${#quotes} % 2 )) -eq 0 ] || return 1
    quotes="${pre//[^\"]/}"
    [ $(( ${#quotes} % 2 )) -eq 0 ] || return 1
    [[ "$pre" != *\\* && "$pre" != *"\`"* ]] || return 1
    if [ "$flag" = context ]; then
      [[ "$kind_value" =~ ^kind-[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || return 1
      [ -z "$kind_env" ] || continue # a context of the file KUBECONFIG selects
    elif [ -z "$kind_single" ] && [[ "$kind_value" =~ $kind_var_re ]]; then
      name="${BASH_REMATCH[2]}${BASH_REMATCH[3]}"
      [[ "$vars" == *" $name "* ]] || return 1
    else
      [[ "$kind_value" =~ $kind_path_re ]] || return 1
    fi
    named=1
  done
  [ -n "$named" ]
}
kind_scan() { # $1=the whole command; succeeds when every kubectl names kind
  local text="${1//"$bs_nl"/ }" seg trimmed prev_sep="" name seen="" value just_set=""
  local kind_vars=" " chain_vars=" " kind_env=""
  [[ "$text" =~ (^|[[:space:]\;\&\|\(])# ]] && return 1
  [[ "$text" =~ $kind_env_re ]] && kind_env=1
  while IFS= read -r seg; do
    case "$seg" in
      '&&' | '||' | ';' | '|' | '&')
        # an assignment that a & or a | follows runs in a subshell and is lost
        if [ -n "$just_set" ] && [[ "$seg" == '&' || "$seg" == '|' ]]; then
          kind_vars="${kind_vars// $just_set / }"
          chain_vars="${chain_vars// $just_set / }"
        fi
        just_set=""
        prev_sep="$seg"
        continue
        ;;
    esac
    trimmed="${seg#"${seg%%[![:space:]]*}"}"
    [ -z "$trimmed" ] && continue
    just_set=""
    # a name assigned after && holds only while && is the one separator
    [ "$prev_sep" = "&&" ] || chain_vars=" "
    if [[ "$trimmed" =~ $kind_word ]]; then
      seen=1
      kind_invocation "$trimmed" "$kind_vars$chain_vars" || return 1
    elif [[ "$trimmed" =~ ^(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      name="${BASH_REMATCH[2]}"
      value="${BASH_REMATCH[3]}"
      value="${value%"${value##*[![:space:]]}"}"
      kind_vars="${kind_vars// $name / }"
      chain_vars="${chain_vars// $name / }"
      kind_unquote "$value"
      if [[ "$kind_value" =~ $kind_path_re ]]; then
        case "$prev_sep" in
          '' | ';') kind_vars="$kind_vars$name " ;;
          '&&') chain_vars="$chain_vars$name " ;;
        esac
        just_set="$name"
      fi
    else
      # any other mention of a trusted name (for K in, read K, unset K) ends it
      for name in $kind_vars $chain_vars; do
        if [[ "$trimmed" =~ (^|[^[:alnum:]_\$\{])${name}([^[:alnum:]_]|$) ]]; then
          kind_vars="${kind_vars// $name / }"
          chain_vars="${chain_vars// $name / }"
        fi
      done
    fi
    prev_sep=""
  done < <(printf '%s\n' "$text" | sed -E 's/(&&|\|\||;|\||&)/\n&\n/g')
  [ -n "$seen" ]
}
kind_only() {
  local rc=0
  shopt -u nocasematch
  kind_scan "$1" || rc=1
  shopt -s nocasematch
  return "$rc"
}
# Over kind_scan_max characters (set above) the patterns below are not run on a
# command that holds a mutating call: it asks, whatever it names. They cost
# time in proportion to the square of the length, and so does the scan; this
# check comes first so that a long command pays for none of them.
kubectl_mutates=""
[[ "$cmd" =~ kubectl[[:space:]].*(apply|scale|rollout[[:space:]]+restart) ]] && kubectl_mutates=1
[ -z "$kubectl_mutates" ] || [ "${#raw_cmd}" -le "$kind_scan_max" ] || \
  decide ask "Mutating kubectl call in a command too long to check (over ${kind_scan_max} characters); confirm the kube context, or split the command."
[[ "$cmd" =~ kubectl[[:space:]].*delete ]] && \
  decide ask "kubectl delete asks even on the local kind cluster; confirm the object and the kube context."
# apply --prune deletes what the manifests no longer hold, and apply --force
# deletes and recreates what it cannot patch: the delete ask needs the word
# delete, so these ask as well, kind or not. The flag is a whole word:
# --force-conflicts deletes nothing and passes.
apply_destroys_re="kubectl[[:space:]].*apply[[:space:]].*--(prune|force)([[:space:]=]|\$)"
if [ -n "$kubectl_mutates" ]; then
  [[ "$cmd" =~ $apply_destroys_re ]] && \
    decide ask "kubectl apply --prune and --force delete objects, even on the local kind cluster; confirm the objects and the kube context."
  kind_only "$raw_cmd" || \
    decide ask "Mutating kubectl call; confirm the current kube context is the intended one, or name the local cluster: --kubeconfig infra/kind/kubeconfig or --context kind-<name>."
fi
[[ "$cmd" =~ docker[[:space:]].*push ]] && \
  decide ask "Pushing an image to a registry; confirm tag and registry."
[[ "$cmd" =~ az[[:space:]].*[[:space:]](create|set|update|assign)([[:space:]]|$) ]] && \
  decide ask "Azure control-plane mutation; confirm subscription and resource."
[[ "$cmd" =~ gh[[:space:]]+(release|repo)[[:space:]]+(create|delete|edit) ]] && \
  decide ask "Outward-facing GitHub change; confirm."
# `gh pr merge` on green checks is the session's standing instruction and does
# not ask. --admin merges past failing required checks, so it does. Read on
# hook_cmd (a commit message that names the flag passes), line
# continuations deleted; --admin=value and a quoted "--admin" count, and so does
# a global flag between the words (gh -R owner/repo pr merge). A command that
# holds both words in one line asks, even across a separator: fail-closed. The
# pattern costs time in proportion to the square of the length: it runs only
# when --admin is there and the command is within kind_scan_max characters;
# a longer one that holds --admin, merge and gh asks without being read.
gh_admin_re="(^|[^[:alnum:]_.-])gh[[:space:]]+([^${nl}]*[[:space:]])?pr[[:space:]]+([^${nl}]*[[:space:]])?merge([[:space:]][^${nl}]*)?[[:space:]][\"${sq}]?--admin([^[:alnum:]_-]|\$)"
if [[ "$hook_cmd" == *--admin* ]]; then
  gh_text="$hook_cmd"
  if [ "${#gh_text}" -gt "$kind_scan_max" ]; then
    [[ "$gh_text" == *merge* && "$gh_text" == *gh* ]] && \
      decide ask "A long command that holds gh, merge and --admin; confirm that no gh pr merge --admin is in it, or split the command."
  elif [[ "$gh_text" =~ $gh_admin_re ]]; then
    decide ask "gh pr merge --admin merges past failing required checks; confirm the checks, or drop --admin."
  fi
fi

exit 0
