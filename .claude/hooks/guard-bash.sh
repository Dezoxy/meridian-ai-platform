#!/usr/bin/env bash
# PreToolUse(Bash) guard. Emits a permission decision for dangerous commands.
# deny = hard block (destructive / secret exposure); ask = explicit confirmation.
#
# Heredoc bodies that are merely WRITTEN to a file (cat/tee) are stripped before
# matching, so documentation that mentions a dangerous command is not blocked
# for the mention. A heredoc fed to an interpreter (bash, python, ...), piped
# onward, or written to a script file is kept, because its body executes.
# This is a regex guard, not a sandbox: Claude Code's permission rules and the
# owner's approvals remain the real boundary.
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
# slowest single command decides how late the answer can be, and the byte bound
# below is chosen from it. Measured on 2026-10-06 with the machine idle (load
# about 4), as the gap between two xtrace stamps, on the shapes that cost most
# (runs of `&(`, `(`, quotes and backticks after `kubectl `, and the same after
# `kubectl exec x -- `, which reach the Azure script rules and the pod rule):
#   16384 bytes: 0.97 s     8192 bytes: 0.24 s     4096 bytes: 0.06 s
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
# The command as typed. The heredoc pass below drops lines the shell may still
# run, so the kubectl rule that lets a call through reads this copy.
raw_cmd="$cmd"

# The byte bound: a command over guard_max_bytes asks, before the heredoc pass
# and before any pattern runs, and it says that the command was not read. The
# rules below cost time in proportion to the length (some, to its square): see
# the figures above for the value. The check sits before the heredoc pass: a wc
# over the raw command costs nothing however long it is. Moving it after the
# pass would let a long heredoc that is only written to a file through (the
# pass is linear: 0.1 s of CPU for 1 MB, 0.4 s for 4 MB, measured), and would
# bound what the patterns read instead of what was typed; it was not moved,
# because it changes which commands pass, and the owner has not asked. Bytes,
# not characters: wc counts bytes and the arithmetic trims the padding some wc
# print.
guard_max_bytes=8192
guard_bytes=$(( $(printf '%s' "$cmd" | wc -c) ))
[ "$guard_bytes" -le "$guard_max_bytes" ] || \
  decide ask "This command is too long for the guard to read (${guard_bytes} bytes, the limit is ${guard_max_bytes}): it was NOT read and may hold a form the guard would deny; confirm that it holds none, or write it to a script with the Write tool and run the file."

if command -v python3 >/dev/null 2>&1; then
  cmd="$(printf '%s' "$cmd" | python3 -c '
import re, sys
# A here-string (<<<word) is not a heredoc: it opens no body.
MARK = re.compile(r"(?<!<)<<-?\s*[\x27\"]?([A-Za-z_][A-Za-z0-9_]*)[\x27\"]?")
WRITER = re.compile(r"(^|[;&|(]\s*)(cat|tee)\b")
# Interpreters count only as command tokens, not as substrings of a path
# such as guard-bash-cases.jsonl.
EXECUTES = re.compile(r"(^|[\s;&|(])(sudo\s+)?(bash|sh|zsh|dash|python3?|node|perl|ruby|xargs|eval|source|exec|chmod)(\s|$)")
SCRIPT_TARGET = re.compile(r"\S+\.(sh|bash|zsh|py|rb|pl|js)\b")
term = None
closed = False
out = []
for line in sys.stdin.read().split("\n"):
    if term is not None:
        if line.strip() == term:
            term = None
            closed = True
        continue
    # After a stripped body, a line that starts with the closing bracket of
    # the command substitution around the heredoc belongs to the command that
    # opened it: a flag written after the message is a flag of that commit.
    if closed and line.lstrip().startswith(")") and out:
        out[-1] += " " + line.lstrip()
        closed = False
        continue
    closed = False
    out.append(line)
    m = MARK.search(line)
    if not m:
        continue
    head, tail = line[:m.start()], line[m.end():]
    plain_write = (
        WRITER.search(head) is not None
        and EXECUTES.search(head) is None
        and EXECUTES.search(tail) is None
        and SCRIPT_TARGET.search(head + tail) is None
    )
    if plain_write:
        term = m.group(1)
print("\n".join(out))
' 2>/dev/null || printf '%s' "$cmd")"
fi

# The rules below read a command one segment at a time (split on newlines, ;,
# &&, || and |, line continuations joined), and the time they take follows the
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
bs_nl=$'\\\n'
guard_max_segments=1000
guard_segments=$(( $(printf '%s\n' "${cmd//"$bs_nl"/ }" | sed -E 's/(&&|\|\||;|\|)/\n/g' | wc -l) ))
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
      || "$cmd" == *kind* ]] \
   && command -v python3 >/dev/null 2>&1; then
  hook_cmd="$(printf '%s' "$cmd" | python3 -c '
import re, sys
PROSE = r"((?<![\w-])-[A-Za-z]*m|--message|--body|--title|--notes)(\s+|=)?"
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
fi

shopt -s nocasematch

# ---- hard denies ----
[[ "$cmd" =~ rm[[:space:]]+-[a-z]*r[a-z]*f?[[:space:]]+(/|~|\$HOME|\.\.($|/)) ]] && \
  decide deny "Recursive force-delete of a sensitive path. Run it yourself if truly intended."
[[ "$cmd" =~ terraform[[:space:]].*destroy ]] && \
  decide deny "terraform destroy is destructive; run it yourself after confirming the workspace."
# Destructive pushes and secret printing are judged per command segment (split
# on newlines, ;, &&, || and |), so a flag or path in one segment does not
# combine with a command in another: `git push -q; rm -f x` is not a force
# push, and `echo done && jq '.env' settings.json` prints no secret. Line
# continuations are joined first, so `git push \<newline> --force` stays one
# segment.
#
# infra/kind/pins.env holds version pins and no secret, and its name ends in
# .env: a segment that names that exact path (as is, after ./, or after an
# absolute prefix) is read without it. Another .env in the same segment, and
# a path that only ends in these words (xinfra/kind/pins.env, pins.env/..),
# still deny. Meridian's rule: the base has no such file.
sq="'"
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
  [[ "$seg_env" =~ (^|[[:space:]/])(cat|less|bat|more|head|tail|echo|printf|xxd|base64|strings)[[:space:]].*(\.env($|[^.a-zA-Z])|\.tfvars($|[^.])|\.pem($|[^a-zA-Z])|id_rsa|id_ed25519|kubeconfig|\.kube/config|admin\.conf) ]] && \
    decide deny "That would print secret material to the transcript (.env/tfvars/keys/kubeconfig, a node's admin.conf)."
done < <(printf '%s\n' "${cmd//"$bs_nl"/ }" | sed -E 's/(&&|\|\||;|\|)/\n/g')
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
    if [[ "$k8s_rest" =~ $k8s_output_re ]] || [[ "$k8s_rest" =~ $k8s_template_re ]]; then
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
done < <(printf '%s\n' "${hook_cmd//"$bs_nl"/ }" | sed -E 's/(&&|\|\||;|\|)/\n/g')
[[ -n "$k8s_secret_seen" && -n "$k8s_handed_seen" ]] && \
  decide deny "A get of Secrets, and a get that prints whatever a variable or xargs hands it, in one command: that can print Secret values to the transcript. Run it yourself."
# Other ways to a Secret's value. The get rule above reads `kubectl get secret`;
# these read what a pod mounts or holds in its environment, what the API prints
# for a path that names Secrets, a kubeconfig's credentials, a service
# account's token, and what a cloud's secret store hands back. They read
# hook_cmd (a commit message that names one passes) with line continuations
# joined, and the ones that look at a pod's command stop at a separator, a
# newline included, so `kubectl exec x -- ls; env FOO=1 make` is not an env
# print. Inside `sh -c "..."` a separator belongs to the pod's command: the
# body is read as far as its closing quote and no further, so a command that
# follows the quoted body (`sh -c 'ls' | grep env`) is not part of it. Neutral
# rules, none names a Meridian path.
eol=$'\n'
joined="${hook_cmd//"$bs_nl"/ }"
# What follows `exec ... --` (or `debug`: its --target shares the process
# namespace and its command is read the same way): the pod's own command,
# after a wrapper word and its options, a quote and a directory, if there are
# any. `kubectl run` is not read: a pod it starts holds none of the workload's
# environment, and the same words follow `docker run` and `uv run` (the psql
# ask below does read `run`).
exec_wrap="((busybox|command|sudo|exec|nohup|nice|timeout|time|stdbuf|setsid)[[:space:]]+(-[^[:space:]]*[[:space:]]+|[0-9][0-9.]*[smhd]?[[:space:]]+)*)*"
exec_head="(^|[^[:alnum:]_-])(exec|debug)[[:space:]]+[^;&|${eol}]*[[:space:]]--[[:space:]]+${exec_wrap}[\"${sq}]?([^[:space:];&|\"${sq}]*/)?"
# What ends a command: a separator, a redirect (`2>&1` included), a comment, a
# backtick, a newline, or a quote that closes a string (one that is followed
# by the end or a separator, not the quote that opens an argument of
# `env "PGOPTIONS=x" psql`).
exec_end="([;&|)<>#\`${eol}]|[0-9]+[<>]|[\"${sq}][[:space:]]*(\$|[;&|)]))"
# env and printenv print the environment; env with options and assignments only
# does too, and so does `env -u X` (-u, -C and -S take a value). `env VAR=x
# some-command` runs the command and passes.
env_tail="(printenv([[:space:]]|\$|${exec_end})|env([[:space:]]+(-[uCS][[:space:]]+[^[:space:]]+|-[^[:space:]]*|[^[:space:]=;&|\"${sq}]+=[^[:space:];&|\"${sq}]*))*[[:space:]]*(\$|${exec_end}))"
# The shell builtins that print every variable: set, export and declare with
# nothing but -p after them (`set -e` and `export FOO=1` pass).
set_tail="(set|(export|declare|typeset)([[:space:]]+-p)?)[[:space:]]*(\$|${exec_end})"
# The files a Secret or a token lives in: a path that holds one of these words
# (token and creds as words, so tokenizer.json and token_usage.log pass), and
# the directories a pod mounts them in, which need no word.
secret_path="(/var/run/secrets/|/run/secrets/|/etc/secrets|secret|tokens?([^[:alnum:]_]|\$)|credential|creds?([^[:alnum:]_]|\$)|password|\.key|/proc/[^[:space:]]*/environ)"
mount_path="(/var/run/secrets/|/run/secrets/|/etc/secrets|/proc/[^[:space:]]*/environ)"
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
assign="([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*"
exec_sh_open="(ba|da|k|z|a)?sh[[:space:]]+(-[a-z]+[[:space:]]+)*"
exec_sh_body() { # $1=the quote that opens the body; sets exec_sh_alts
  local q="$1" in nonword at
  in="[^${q}]*"
  nonword="[^[:alnum:]_${q}-]"
  at="(${in}[;&|(\`{${eol}])?[[:space:]]*${assign}${exec_wrap}([^[:space:];&|\"${sq}]*/)?"
  exec_sh_alts="${at}(${env_tail}|${set_tail})"
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
raw_secrets_msg="That reads Secret values through the API to the transcript. -o name lists Secrets and kubectl describe secret shows keys and sizes; run it yourself for a value."
if [[ "$joined" == *--raw* ]]; then
  [[ "$joined" =~ $raw_secrets_re ]] && decide deny "$raw_secrets_msg"
  # One pass over each distinct %XX (twenty at most): enough for the words
  # that matter, bounded for a command that is nothing but percent signs.
  raw_decoded="$joined"
  for _ in {1..20}; do
    [[ "$raw_decoded" =~ %([0-9A-Fa-f][0-9A-Fa-f]) ]] || break
    printf -v raw_char "\\x${BASH_REMATCH[1]}"
    raw_decoded="${raw_decoded//"${BASH_REMATCH[0]}"/"$raw_char"}"
  done
  [[ "$raw_decoded" =~ $raw_secrets_re ]] && decide deny "$raw_secrets_msg"
fi
[[ "$joined" =~ $config_view_re ]] && \
  decide deny "kubectl config view --raw (and --flatten) prints the kubeconfig's keys and tokens to the transcript. Run it yourself."
[[ "$joined" =~ $create_token_re ]] && \
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
if [[ "$joined" =~ $az_secret_re || "$joined" =~ $az_secret_list_re || "$joined" =~ $gcloud_secret_re \
      || "$joined" =~ $aws_secret_re || "$joined" =~ $aws_ssm_re ]]; then
  decide deny "A cloud secret store's value never enters the transcript or the command line. Run it yourself; list the names instead."
fi
# The kubectl plugin view-secret prints a Secret's decoded values (krew installs
# it as kubectl-view_secret).
view_secret_re="(^|[^[:alnum:]_.])view[-_]secret([[:space:]]|\$)"
[[ "$hook_cmd" =~ $view_secret_re ]] && \
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
[[ "$cmd" =~ az[[:space:]]+keyvault[[:space:]]+secret[[:space:]]+(show|set|download|backup|restore) ]] && \
  decide deny "Key Vault secret values never enter the transcript or the command line. Run it yourself; list secret names with 'az keyvault secret list'."
[[ "$cmd" =~ az[[:space:]].*(group|keyvault|postgres|aks|cognitiveservices|acr)[[:space:]]+(.*[[:space:]])?delete([[:space:]]|$) ]] && \
  decide deny "Azure resource delete is destructive; run it yourself after confirming subscription and resource."
[[ "$cmd" =~ kubectl[[:space:]].*delete[[:space:]].*(namespace|[[:space:]]ns[[:space:]]|pvc|persistentvolumeclaim|--all) ]] && \
  decide deny "Deleting namespaces, volumes or --all is destructive; run it yourself."

# ---- confirmations ----
[[ "$cmd" =~ terraform[[:space:]].*apply ]] && \
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
# The byte bound at the top is now this value, and characters are never more
# than bytes, so no command reaches the checks that use this: they stay as a
# second line, for the day the byte bound is raised.
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
  done < <(printf '%s\n' "${cmd//"$bs_nl"/ }" | sed -E 's/(&&|\|\||;|\||&)/\n/g')
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
# script file is not a typed command and is not seen.
helm_get_re="(^|[^[:alnum:]_.-])helm[[:blank:]]+([^;&|${nl}]*[[:blank:]])?get[[:blank:]]+([^;&|${nl}]*[[:blank:]])?(manifest|values|all)([[:space:]]|\$)"
[[ "$hook_cmd" =~ $helm_get_re ]] && \
  decide ask "helm get manifest, values and all print a release's rendered manifests and values, which can hold Secret data; confirm that this release has none, or run it yourself."
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
# hook_cmd (a commit message that names the flag passes), with line
# continuations joined; --admin=value and a quoted "--admin" count, and so does
# a global flag between the words (gh -R owner/repo pr merge). A command that
# holds both words in one line asks, even across a separator: fail-closed. The
# pattern costs time in proportion to the square of the length: it runs only
# when --admin is there and the command is within kind_scan_max characters;
# a longer one that holds --admin, merge and gh asks without being read.
gh_admin_re="(^|[^[:alnum:]_.-])gh[[:space:]]+([^${nl}]*[[:space:]])?pr[[:space:]]+([^${nl}]*[[:space:]])?merge([[:space:]][^${nl}]*)?[[:space:]][\"${sq}]?--admin([^[:alnum:]_-]|\$)"
if [[ "$hook_cmd" == *--admin* ]]; then
  gh_text="${hook_cmd//"$bs_nl"/ }"
  if [ "${#gh_text}" -gt "$kind_scan_max" ]; then
    [[ "$gh_text" == *merge* && "$gh_text" == *gh* ]] && \
      decide ask "A long command that holds gh, merge and --admin; confirm that no gh pr merge --admin is in it, or split the command."
  elif [[ "$gh_text" =~ $gh_admin_re ]]; then
    decide ask "gh pr merge --admin merges past failing required checks; confirm the checks, or drop --admin."
  fi
fi

exit 0
