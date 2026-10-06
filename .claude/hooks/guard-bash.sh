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
input="$(cat)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"
[ -z "$cmd" ] && exit 0

if command -v python3 >/dev/null 2>&1; then
  cmd="$(printf '%s' "$cmd" | python3 -c '
import re, sys
MARK = re.compile(r"<<-?\s*[\x27\"]?([A-Za-z_][A-Za-z0-9_]*)[\x27\"]?")
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
if [[ "$cmd" == *git* || "$cmd" == *secret* || "$cmd" == *psql* ]] && command -v python3 >/dev/null 2>&1; then
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

decide() { # $1=decision $2=reason
  jq -nc --arg d "$1" --arg r "$2" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:$d,permissionDecisionReason:$r}}'
  exit 0
}

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
bs_nl=$'\\\n'
while IFS= read -r seg; do
  [[ "$seg" =~ git[[:space:]]+push([[:space:]].*)?[[:space:]](--force|-[a-zA-Z]*f|[+]|--mirror|--prune) ]] && \
    decide deny "Destructive push (--force*, bundled -f, +refspec, --mirror/--prune) can rewrite shared refs. Run it yourself if you must."
  [[ "$seg" =~ (^|[[:space:]])(rm|mv|unlink|truncate)[[:space:]].*(\.git/hooks|\.githooks|\.husky)([/[:space:]]|$) || \
     "$seg" =~ chmod[[:space:]]+[ugoa]*-[rwx]*x.*(\.git/hooks|\.githooks|\.husky)([/[:space:]]|$) ]] && \
    decide deny "Removing or disabling a git hook file bypasses it. Run it yourself if intended."
  [[ "$seg" =~ (^|[[:space:]/])(cat|less|bat|more|head|tail|echo|printf|xxd|base64|strings)[[:space:]].*(\.env($|[^.a-zA-Z])|\.tfvars($|[^.])|\.pem($|[^a-zA-Z])|id_rsa|id_ed25519|kubeconfig|\.kube/config) ]] && \
    decide deny "That would print secret material to the transcript (.env/tfvars/keys/kubeconfig)."
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
[[ "$cmd" =~ kind[[:space:]]+delete ]] && \
  decide ask "Deleting the kind cluster loses local state; confirm."
# `make down` and infra/kind/down.sh wrap `kind delete`, so the rule above never
# sees them. `make` is matched as a whole word, any options may precede the
# target, and the target must end at a space, separator or the end of the line.
nl=$'\n'
[[ "$cmd" =~ (^|[^[:alnum:]_.-])make[[:space:]]+([^\;\&\|${nl}]*[[:space:]])?down([[:space:]]|$|[;\&\|\)]) ]] && \
  decide ask "make down deletes the kind cluster and its local state; confirm."
[[ "$cmd" =~ (^|[\;\&\|\(${nl}])[[:space:]]*((bash|sh|zsh)[[:space:]]+)?([^[:space:]]*infra/kind/|\./)down\.sh ]] && \
  decide ask "infra/kind/down.sh deletes the kind cluster and its local state; confirm."
# `make grafana-password` and the script behind it exist to print a password.
# A person runs them in a terminal of their own, which never meets this hook;
# in a session the output is the transcript, so the owner is asked first.
# Same anchoring as the down rules; `make grafana` and `grafana.sh forward`
# pass.
grafana_make_re="(^|[^[:alnum:]_.-])make[[:space:]]+([^\;\&\|${nl}]*[[:space:]])?grafana-password([[:space:]]|\$|[;\&\|\)])"
grafana_script_re="(^|[\;\&\|\(${nl}])[[:space:]]*((bash|sh|zsh)[[:space:]]+)?([^[:space:]]*infra/kind/|\./)grafana\.sh[[:space:]]+password([[:space:]]|\$|[;\&\|\)])"
if [[ "$cmd" =~ $grafana_make_re ]] || [[ "$cmd" =~ $grafana_script_re ]]; then
  decide ask "This prints the Grafana admin password into the transcript; confirm, or run it in a terminal of your own."
fi
# `make azure-state`, `make azure-apply` and the scripts behind them create
# Azure resources without the word terraform or az on the command line, so the
# rules above never see them. Same anchoring as the down rules, widened for the
# ways a command can be dressed: leading VAR=value assignments, `env`, `time`,
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
psql_exec_re="(^|[^[:alnum:]_-])exec[[:space:]].*[[:space:]]--[[:space:]](.*[[:space:]/\"';|&(])?psql([[:space:]\"';|&<>)]|\$)"
[[ "$hook_cmd" =~ $psql_exec_re ]] && \
  decide ask "psql through kubectl exec runs SQL inside the database pod, usually as its superuser; confirm the statement and the kube context."
[[ "$cmd" =~ helm[[:space:]]+(uninstall|delete|rollback) ]] && \
  decide ask "This changes a running Helm release; confirm the release and the kube context."
# A mutating kubectl call asks, because the current kube context may not be the
# one meant. A call that names the local kind cluster has answered that, so
# apply, scale and rollout restart pass when EVERY kubectl invocation in the
# command names it. Recognised, and nothing else:
# - an invocation that starts a segment (a segment is cut at newlines, ;, &, &&,
#   || and |; `do`, `then` and `else` may stand before it), with no second
#   `kubectl` in the segment, no --server, -s or --cluster, and every
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
# - Anything else asks as before: no cluster named, another context or
#   kubeconfig, sudo, env, time or a path before kubectl, kubectl inside
#   $( ), ( ), bash -c or xargs, a # that starts a comment anywhere in the
#   command (the quoting of a line is not parsed), a kind name only in an echo.
# kubectl delete asks on kind as well; the denies above it are not touched.
# This reads like the other rules, as a pattern on what a session types, not
# as a boundary: a flag inside a quoted string is skipped by quote parity only.
kind_path_re="^([^[:space:]\"${sq}\`;&|()<>]*/)?infra/kind/kubeconfig\$"
kind_dq_re='^"([^"]*)"$'
kind_sq_re="^${sq}([^${sq}]*)${sq}\$"
# shellcheck disable=SC2016  # a regex that names a literal $
kind_var_re='^\$(\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$'
kind_flag_re='(^|[[:space:]])--(kubeconfig|context)(=|[[:space:]]+)([^[:space:]]*)'
kind_other_re='(^|[[:space:]])(--(server|cluster)([=[:space:]]|$)|-s([^-]|$))'
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
  local kind_vars=" " chain_vars=" "
  [[ "$text" =~ (^|[[:space:]\;\&\|\(])# ]] && return 1
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
[[ "$cmd" =~ kubectl[[:space:]].*delete ]] && \
  decide ask "kubectl delete asks even on the local kind cluster; confirm the object and the kube context."
if [[ "$cmd" =~ kubectl[[:space:]].*(apply|scale|rollout[[:space:]]+restart) ]] && ! kind_only "$cmd"; then
  decide ask "Mutating kubectl call; confirm the current kube context is the intended one, or name the local cluster: --kubeconfig infra/kind/kubeconfig or --context kind-<name>."
fi
[[ "$cmd" =~ docker[[:space:]].*push ]] && \
  decide ask "Pushing an image to a registry; confirm tag and registry."
[[ "$cmd" =~ az[[:space:]].*[[:space:]](create|set|update|assign)([[:space:]]|$) ]] && \
  decide ask "Azure control-plane mutation; confirm subscription and resource."
[[ "$cmd" =~ gh[[:space:]]+(release|repo)[[:space:]]+(create|delete|edit) ]] && \
  decide ask "Outward-facing GitHub change; confirm."

exit 0
