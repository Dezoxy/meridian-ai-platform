---
name: feature-threat-model
description: Write a one-page threat-model note before building a security-relevant platform feature: anything that touches the Model Gateway, tool execution, approvals, identity, tenant data, provider routing or the registry. Produces T-IDs in docs/architecture/security/threat-model.md so risks surface before code.
---

# feature-threat-model

A lightweight, written threat model done *before* coding. One page. The
platform's trust boundaries are numbered in the architecture (edge, ingress to
APIs, runtime to gateway, gateway to providers, runtime to MCP servers,
untrusted content, agent proposal to human decision, git to cluster); name the
ones the feature crosses.

## Produce these sections

1. **Feature and data flow**: what it does; the path of any personal data,
   prompt content, credential or budget through the boundaries it crosses.
2. **Assets**: what is worth protecting here: prompts carrying personal data,
   provider and tool credentials, audit-log integrity, budgets and quotas,
   approval decisions, the registry.
3. **Threats (STRIDE-lite)**, per boundary crossed. Always consider: prompt
   injection through retrieved content or tool output; tool misuse or
   privilege escalation through an over-broad allowlist; exfiltration through
   a provider or a residency mismatch; budget abuse; approval bypass; audit
   gaps; secret leakage into logs or traces. Skip categories that do not apply.
4. **Invariant check** against the hard rules in `AGENTS.md`: every model call
   through the gateway; every tool allowlisted and audited; residency label on
   every deployment; no agent-framework import in platform packages; no
   secrets or real personal data in the repository. Note any tension.
5. **Decision and mitigations**: what you will do; which reviewer gates it
   (`security-reviewer`, `platform-boundary-reviewer`, `infra-reviewer`); which
   tests or evaluation cases (for example the injection suite) prove it.
6. **Residual risk**: what remains and why it is acceptable for this
   milestone.

## Output

- Add or refresh rows in `docs/architecture/security/threat-model.md`, one
  `T-NN` per threat, with boundary, threat, mitigation, status and evidence.
  If the file does not exist, create it with that register table, title it
  with `##`, and symlink it into `docs/architecture/overview/` as
  `23-threat-model.md` so it reaches the Documentation tab and the PDF.
- Summarise the must-fix mitigations in the reply.
- If the feature conflicts with an invariant (for example a tool that calls a
  provider directly), stop and surface the conflict for an explicit decision
  before coding.
