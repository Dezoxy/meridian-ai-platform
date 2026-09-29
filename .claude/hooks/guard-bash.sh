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
  [[ "$seg" =~ (^|[[:space:]/])(cat|less|bat|more|head|tail|echo|printf|xxd|base64|strings)[[:space:]].*(\.env($|[^.a-zA-Z])|\.tfvars($|[^.])|\.pem($|[^a-zA-Z])|id_rsa|id_ed25519|kubeconfig|\.kube/config) ]] && \
    decide deny "That would print secret material to the transcript (.env/tfvars/keys/kubeconfig)."
done < <(printf '%s\n' "${cmd//"$bs_nl"/ }" | sed -E 's/(&&|\|\||;|\|)/\n/g')
[[ "$cmd" =~ az[[:space:]]+keyvault[[:space:]]+secret[[:space:]]+(show|set|download|backup|restore) ]] && \
  decide deny "Key Vault secret values never enter the transcript or the command line. Run it yourself; list secret names with 'az keyvault secret list'."
[[ "$cmd" =~ kubectl[[:space:]]+get[[:space:]]+secrets?[[:space:]].*-o[[:space:]]*(yaml|json) ]] && \
  decide deny "That would print Kubernetes secret values to the transcript."
[[ "$cmd" =~ az[[:space:]].*(group|keyvault|postgres|aks|cognitiveservices|acr)[[:space:]].*delete ]] && \
  decide deny "Azure resource delete is destructive; run it yourself after confirming subscription and resource."
[[ "$cmd" =~ kubectl[[:space:]].*delete[[:space:]].*(namespace|[[:space:]]ns[[:space:]]|pvc|persistentvolumeclaim|--all) ]] && \
  decide deny "Deleting namespaces, volumes or --all is destructive; run it yourself."

# ---- confirmations ----
[[ "$cmd" =~ terraform[[:space:]].*apply ]] && \
  decide ask "terraform apply mutates cloud infrastructure; confirm the plan and workspace first."
[[ "$cmd" =~ kind[[:space:]]+delete ]] && \
  decide ask "Deleting the kind cluster loses local state; confirm."
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
