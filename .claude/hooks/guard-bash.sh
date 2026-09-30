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
out = []
for line in sys.stdin.read().split("\n"):
    if term is not None:
        if line.strip() == term:
            term = None
        continue
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
  # Git hook bypasses. A repository's hooks are its guard rails (a pre-push
  # hook may be the only thing stopping a push to main), so an agent never
  # skips them: every form is denied, and a human runs it if truly needed.
  # Git accepts abbreviated long options (--no-veri), and only `commit` reads
  # -n as --no-verify; `git push -n` is a dry run and `git log -n` a count.
  [[ "$seg" =~ git[[:space:]].*--no-veri ]] && \
    decide deny "Skipping git hooks (--no-verify) is not allowed. Fix what the hook reports, or run it yourself."
  [[ "$seg" =~ git[[:space:]]+(-[cC][[:space:]]+[^[:space:]]+[[:space:]]+)*commit([[:space:]].*)?[[:space:]]-[a-zA-Z]*n[a-zA-Z]*([[:space:]]|$) ]] && \
    decide deny "git commit -n skips the commit hooks. Fix what the hook reports, or run it yourself."
  # Repointing or unsetting core.hooksPath (git config, -c, --config-env or
  # GIT_CONFIG_KEY_n) disables every hook at once. Reading it, or pointing it
  # at the repository's own .githooks, is the activation step and passes.
  if [[ "$seg" =~ (^|[[:space:]])git[[:space:]] && "$seg" =~ core\.hookspath ]] && \
     ! [[ "$seg" =~ git[[:space:]]+config[[:space:]]+((--local|--get)[[:space:]]+)*core\.hookspath([[:space:]]+(\./)?\.githooks/?)?[[:space:]]*$ ]]; then
    decide deny "Changing core.hooksPath disables the repository's git hooks. Run it yourself if intended."
  fi
  [[ "$seg" =~ (^|[[:space:]])SKIP=[^[:space:]]+[[:space:]]+(.*[[:space:]])?git[[:space:]] ]] && \
    decide deny "SKIP= bypasses pre-commit hooks. Fix what the hook reports, or run it yourself."
  [[ "$seg" =~ (^|[[:space:]])(rm|mv|unlink|truncate)[[:space:]].*(\.git/hooks/|\.githooks/|\.husky/) || \
     "$seg" =~ chmod[[:space:]]+[ugoa]*-[rwx]*x.*(\.git/hooks/|\.githooks/|\.husky/) ]] && \
    decide deny "Removing or disabling a git hook file bypasses it. Run it yourself if intended."
  [[ "$seg" =~ (^|[[:space:]/])(cat|less|bat|more|head|tail|echo|printf|xxd|base64|strings)[[:space:]].*(\.env($|[^.a-zA-Z])|\.tfvars($|[^.])|\.pem($|[^a-zA-Z])|id_rsa|id_ed25519|kubeconfig|\.kube/config) ]] && \
    decide deny "That would print secret material to the transcript (.env/tfvars/keys/kubeconfig)."
done < <(printf '%s\n' "${cmd//"$bs_nl"/ }" | sed -E 's/(&&|\|\||;|\|)/\n/g')
# Hook-manager switches, checked across the whole command because
# `export HUSKY=0 && git push` splits them from the git call. They have no use
# other than turning hooks off.
[[ "$cmd" =~ (^|[^[:alnum:]_])(HUSKY=0|HUSKY_SKIP_HOOKS=1|LEFTHOOK=0) ]] && \
  decide deny "Disabling the hook manager (HUSKY=0, LEFTHOOK=0) bypasses git hooks. Fix what the hook reports, or run it yourself."
[[ "$cmd" =~ az[[:space:]]+keyvault[[:space:]]+secret[[:space:]]+(show|set|download|backup|restore) ]] && \
  decide deny "Key Vault secret values never enter the transcript or the command line. Run it yourself; list secret names with 'az keyvault secret list'."
[[ "$cmd" =~ kubectl[[:space:]]+get[[:space:]]+secrets?[[:space:]].*-o[[:space:]]*(yaml|json) ]] && \
  decide deny "That would print Kubernetes secret values to the transcript."
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
[[ "$cmd" =~ helm[[:space:]]+(uninstall|delete|rollback) ]] && \
  decide ask "This changes a running Helm release; confirm the release and the kube context."
[[ "$cmd" =~ kubectl[[:space:]].*(apply|delete|scale|rollout[[:space:]]+restart) ]] && \
  decide ask "Mutating kubectl call; confirm the current kube context is the intended one."
[[ "$cmd" =~ docker[[:space:]].*push ]] && \
  decide ask "Pushing an image to a registry; confirm tag and registry."
[[ "$cmd" =~ az[[:space:]].*[[:space:]](create|set|update|assign)([[:space:]]|$) ]] && \
  decide ask "Azure control-plane mutation; confirm subscription and resource."
[[ "$cmd" =~ gh[[:space:]]+(release|repo)[[:space:]]+(create|delete|edit) ]] && \
  decide ask "Outward-facing GitHub change; confirm."

exit 0
