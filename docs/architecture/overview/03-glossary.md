## Glossary

| Term | Meaning |
|---|---|
| Agent contract | The small interface between the Agent Runtime and a workload: start a run, resume a paused run, read status, a checkpoint store, a tool client. It does not name a framework. |
| Control plane | Shared platform services: gateway, runtime, MCP servers, evaluation, registry, observability, database, secrets. |
| Workload plane | Use-case code that runs on the platform: the claims-triage app, its graph and its MCP server. |
| Data class | Label on a tenant or request: `synthetic`, `internal`, `personal`, `special`. Decides which model deployments may be used. |
| Residency label | Label on a model deployment: `eu-region` (one EU region), `eu-zone` (EU data zone), `global` (may process outside the EU). Maps to the Azure SKUs `Standard`, `DataZoneStandard` and `GlobalStandard`. |
| FNOL | First notice of loss: the initial report of a claim. |
| Golden set | Synthetic claims with labelled expected outcomes; the evaluation baseline. |
| Guardrail | A check before or after a model call: redaction, injection detection, tool allowlist, output schema, budget. |
| Interrupt | A LangGraph pause with durable state; how human approval is implemented. |
| Idempotency key | Client-supplied key that makes a repeated mutating tool call return the original result. |
| MCP | Model Context Protocol: the interface between the runtime and tool servers. |
| Tenant | A team or use case with its own quotas, budgets, data class and policies. |
| Trajectory | The sequence of model and tool calls in one run, visible as a trace. |
| SLO, SLI | Service level objective and indicator; targets until measured in milestone M3. |
| Implemented, simulated, designed | Capability labels: runs on the platform; demonstrated with synthetic data or a game day; documented only. |
