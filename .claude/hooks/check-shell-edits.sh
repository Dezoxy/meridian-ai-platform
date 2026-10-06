#!/usr/bin/env bash
# PostToolUse(Bash): advisory. Names the tracked files a shell command rewrote.
# Injects one line; never blocks and never asks.
#
# Why: the edit gate (GateGuard) and the advisory hooks on Edit and Write
# (check-py.sh, check-iac.sh, check-docs.sh, check-boundary.sh) read only what
# those two tools change. A `sed -i`, a heredoc over a tracked file or a
# `python -c` rewrite changes a file none of them sees, and in a subagent the
# edit gate never fires at all. Three of S055's five implementer runs did this,
# and S066 and S077 again. The implementer's instructions forbid it; this hook
# tells the session that lands the work when it happened anyway.
#
# How: with `bashEditDiffEnabled`, Claude Code puts the files a Bash command
# changed into the hook's input as tool_response.bashEditDiff.changedFiles
# (absolute paths, at most 200). Claude Code's documentation
# (https://code.claude.com/docs/en/hooks, read 2026-10-06) calls the list "best
# effort and in public beta", and says it is recorded for a command that is not
# run in the background and is not read-only; `shared` is set when another Bash
# call ran in the same repository at the same time, so some files may be that
# call's. A subagent's hook input carries agent_id and agent_type.
#
# Seen and not seen (Claude Code 2.1.289, 2026-10-06, `claude -p` in
# bypassPermissions mode, one Bash call that appended to a tracked file):
#   - SEEN in a scratch git project outside this repository: the list was
#     delivered with the setting passed as `--settings
#     '{"bashEditDiffEnabled":true}'` and with CLAUDE_CODE_BASH_EDIT_DIFF=1 in
#     the environment that starts Claude Code;
#   - SEEN NOT delivered, in the scratch project and again in this repository's
#     own worktree: with `bashEditDiffEnabled: true` in the project's
#     .claude/settings.json, and with CLAUDE_CODE_BASH_EDIT_DIFF=1 in that
#     file's `env` block. The binary reads the setting from the policy, flag
#     and user settings sources only; its own text says:
#     "Only user, flag or policy settings can turn it on outside auto and
#     bypassPermissions modes." Observed: in bypassPermissions mode no list
#     arrived without one of those three.
#     So this repository does not carry the setting: it must stand in the
#     user's ~/.claude/settings.json (or be passed with --settings, or
#     CLAUDE_CODE_BASH_EDIT_DIFF=1 be set where Claude Code starts). Without
#     it this hook never sees a list and stays silent;
#   - NOT SEEN: a real subagent's input in an isolation worktree. The
#     repository is taken from the input's `cwd` (the hook runs in the
#     subagent's working directory) and, as a second root, from
#     CLAUDE_PROJECT_DIR.
#
# Left out on purpose: uv.lock (a tool is meant to rewrite it), anything under
# .git/, files of another repository, and the files a named formatter rewrote
# (`ruff format` for Python files, `terraform fmt` for Terraform files; any
# other file the same command changed is still named). A command that names a
# formatter and also rewrites a Python file by hand is therefore not caught.
# Files a `make` target regenerates and git tracks (an evaluation baseline, a
# derived Mermaid block) are named too: the hook cannot tell them from a
# hand-made rewrite.
#
# A broken hook must never get in a session's way: no jq, no git, input that is
# not JSON, no list, an empty list or a list of things that are not paths all
# end in exit 0 with nothing printed.
set -uo pipefail

input="$(cat)"
command -v jq >/dev/null 2>&1 || exit 0
command -v git >/dev/null 2>&1 || exit 0

field() { printf '%s' "$input" | jq -r "$1" 2>/dev/null; }

mapfile -t files < <(field '
  [.tool_response? | objects | .bashEditDiff? | objects | .changedFiles? | arrays
   | .[] | strings | select(length > 0)] | .[]') || exit 0
[ "${#files[@]}" -gt 0 ] || exit 0

command_text="$(field '.tool_input? | objects | .command? | strings')"
cwd="$(field '.cwd? | strings')"
agent="$(field '.agent_type? | strings')"
shared="$(field '.tool_response? | objects | .bashEditDiff? | objects | .shared? | booleans')"

# The repositories a file may belong to: the one the command ran in, and the
# project's. A file anywhere else is not this hook's business.
roots=()
add_root() {
  local top
  [ -n "${1:-}" ] && [ -d "$1" ] || return 0
  top="$(git -C "$1" rev-parse --show-toplevel 2>/dev/null)" || return 0
  top="$(cd "$top" 2>/dev/null && pwd -P)" || return 0
  roots+=("$top")
}
add_root "$cwd"
add_root "${CLAUDE_PROJECT_DIR:-}"
[ "${#roots[@]}" -gt 0 ] || exit 0

ruff_format=0 terraform_fmt=0
word='(^|[^[:alnum:]_-])'
end='([^[:alnum:]_-]|$)'
[[ $command_text =~ ${word}ruff[[:space:]]+format${end} ]] && ruff_format=1
[[ $command_text =~ ${word}terraform([[:space:]]+-chdir=[^[:space:]]+)?[[:space:]]+fmt${end} ]] &&
  terraform_fmt=1

# The path of a changed file relative to the root it lies under, on stdout.
relative() {
  local root real
  for real in "$(realpath -ms -- "$1" 2>/dev/null)" "$(realpath -m -- "$1" 2>/dev/null)"; do
    for root in "${roots[@]}"; do
      case "$real" in "$root"/*)
        printf '%s\n%s\n' "$root" "${real#"$root"/}"
        return 0
        ;;
      esac
    done
  done
  return 1
}

declare -A seen=()
names=()
for file in "${files[@]}"; do
  pair="$(relative "$file")" || continue
  root="${pair%%$'\n'*}"
  rel="${pair#*$'\n'}"
  [ -z "${seen[$root/$rel]:-}" ] || continue
  seen[$root/$rel]=1
  case "/$rel" in */.git/*) continue ;; esac
  [ "${rel##*/}" = "uv.lock" ] && continue
  if [ "$ruff_format" = 1 ]; then
    case "$rel" in *.py | *.pyi) continue ;; esac
  fi
  if [ "$terraform_fmt" = 1 ]; then
    case "$rel" in *.tf | *.tfvars | *.tftest.hcl | *.tfmock.hcl) continue ;; esac
  fi
  git -C "$root" --literal-pathspecs ls-files --error-unmatch -- "$rel" >/dev/null 2>&1 ||
    continue
  names+=("${rel//[[:cntrl:]]/?}")
done
[ "${#names[@]}" -gt 0 ] || exit 0

limit=10
shown="$(printf '%s, ' "${names[@]:0:limit}")"
shown="${shown%, }"
[ "${#names[@]}" -gt "$limit" ] && shown="${shown} and $((${#names[@]} - limit)) more"

by=""
[ -n "$agent" ] && by=" (run by ${agent//[[:cntrl:]]/?})"
note=""
[ "$shared" = "true" ] &&
  note=" Some of these may come from another command that ran at the same time."
msg="shell edit check: this command${by} rewrote tracked files outside the Edit and Write tools, so the lint, boundary and docs hooks did not see them: ${shown}.${note} Check them by hand (make lint, make docs) or redo the change with Edit or Write."
jq -nc --arg c "$msg" \
  '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$c}}'
exit 0
