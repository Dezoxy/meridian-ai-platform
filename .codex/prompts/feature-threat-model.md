Write a one-page threat-model note BEFORE coding a security-relevant platform
feature (gateway, tool execution, approvals, identity, tenant data, provider
routing, registry). Record it as T-NN rows in
docs/architecture/security/threat-model.md (create the register if missing and
symlink it into docs/architecture/overview/ as 23-threat-model.md).

Sections:
1. Feature and data flow: trace personal data, prompt content, credentials and
   budgets through the numbered trust boundaries the feature crosses.
2. Assets: prompts with personal data, provider and tool credentials, audit-log
   integrity, budgets, approval decisions, the registry.
3. Threats (STRIDE-lite) per boundary: prompt injection via retrieved content or
   tool output, tool misuse, exfiltration or residency mismatch, budget abuse,
   approval bypass, audit gaps, secrets in logs.
4. Invariant check against the hard rules in AGENTS.md; note any tension.
5. Decision and mitigations: which reviewer gates it and which tests or eval
   cases prove it.
6. Residual risk: what remains and why it is acceptable now.

If the feature conflicts with an invariant, STOP and surface the conflict for a
decision before coding.
