#!/usr/bin/env bash
# PostToolUse(Edit|Write): advisory platform-boundary checks on Python files.
# The hard gates are import-linter and the reviewers; this catches the obvious
# early. Injects findings; does not block.
set -euo pipefail
input="$(cat)"
f="$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_response.filePath // empty' 2>/dev/null || true)"
case "$f" in *.py) ;; *) exit 0 ;; esac
[ -f "$f" ] || exit 0
findings=""
case "$f" in
  */src/meridian/platform/*)
    if grep -nE '^[[:space:]]*(from|import)[[:space:]]+(langgraph|langchain)' "$f" >/dev/null 2>&1; then
      findings="${findings}- Platform package imports the agent framework. Platform code never imports langgraph or langchain (ADR 2).\n"
    fi
  ;;
esac
case "$f" in
  */src/meridian/platform/gateway/*) ;;
  */src/*)
    if grep -nE '^[[:space:]]*(from|import)[[:space:]]+(openai|anthropic|mistralai|boto3|litellm)' "$f" >/dev/null 2>&1; then
      findings="${findings}- Provider SDK imported outside src/meridian/platform/gateway. Every model call goes through the Model Gateway (ADR 3).\n"
    fi
  ;;
esac
if grep -nEi '(log(ger)?\.(info|warning|error|debug|exception)|print)\(.*(prompt|completion|claimant|policy_holder|email|phone|address|api_key|token|secret)' "$f" >/dev/null 2>&1; then
  findings="${findings}- Possible logging of prompt content, personal data or secrets. Logs carry identifiers and metadata only.\n"
fi
[ -z "$findings" ] && exit 0
msg="platform boundary check on ${f}:\n${findings}Route this through the platform-boundary-reviewer before committing."
jq -nc --arg c "$(printf '%b' "$msg")" \
  '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$c}}'
exit 0
