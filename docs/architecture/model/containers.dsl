// The system in scope and its containers.
// Groups mark the planes and the internet-facing edge so views can show them.
// What is drawn without the tag "Designed" exists in the code and runs on the
// local kind cluster, in CI or from a laptop; the view register in ../README.md
// says which. "Designed" (always the last tag) marks what nothing implements yet.

meridian = softwareSystem "Meridian AI Platform" "Builds, runs and governs LLM agents for a fictional insurer, with a claims-triage reference workload." {

    group "Edge (internet-facing)" {
        ingress = container "Ingress" "Routes requests for the one public host to the Claims Triage App; every other host gets 404. TLS is designed." "Envoy Gateway (Gateway API) on kind; Azure Application Gateway WAF in the Azure design" "Layer Edge,Gateway,Internet-exposed"
    }

    group "Control plane" {
        gateway = container "Model Gateway" "Routes model calls by policy: a deployment by data class and residency label, fallback behind a circuit breaker, per-tenant quotas and budgets, cost metering, PII redaction, audit." "Python, FastAPI; rate windows in Redis on kind" "Layer Services"
        runtime = container "Agent Runtime" "Hosts workload graphs behind the agent contract: start, pause for approval, resume; checkpoints; call limits per run; MCP client with tool allowlists." "Python, LangGraph host" "Layer Services"
        policyMcp = container "Policy MCP Server" "Policy lookup and claim history as MCP tools." "Python, MCP SDK, Streamable HTTP" "Layer Services"
        knowledgeMcp = container "Knowledge MCP Server" "Hybrid search over policy wording with citations; its ingestion pipeline runs as a job." "Python, MCP SDK, Streamable HTTP, pgvector" "Layer Services"
        evals = container "Evaluation Harness" "Answers the golden set from a recorded model, grades with rules and an LLM judge, gates CI. Runs in tests and from the command line; not deployed." "Python, pytest, meridian eval CLI" "Layer Services"
        observability = container "Observability Stack" "Collects traces, metrics and logs; serves dashboards. Alerts are designed." "OpenTelemetry Collector, Prometheus, Grafana, Tempo, Loki" "Layer Services"
        registry = container "Platform Registry" "Declarative source of truth: models, providers, tools, agents, policies, tenants, services." "YAML in git with JSON Schema, loaded at startup" "Layer Data"
        platformDb = container "Platform Database" "Claims and decisions, policies, policy wording chunks, runs and graph checkpoints, the audit log and the usage ledger; one role per service." "PostgreSQL 17 with pgvector" "Layer Data,Database"
        keyVault = container "Key Vault" "On kind, the database roles' passwords as Kubernetes Secrets. Provider credentials and signing secrets in Azure Key Vault are designed." "Kubernetes Secrets on kind; Azure Key Vault in the Azure design" "Layer Data,Vault"
    }

    group "Workload plane" {
        claimsApp = container "Claims Triage App" "Claims API, adjuster queue UI and claimant pages for the reference workload; owns the claims-triage graph package and the scheduled sweep that refers overdue claims to an adjuster and ends abandoned runs." "Python, FastAPI, Jinja" "Layer Workload"
        claimsMcp = container "Claims MCP Server" "Claim notes, approval requests and the outcome recorded for a request as MCP tools. Adjuster decisions are recorded by the Claims Triage App, never through a tool." "Python, MCP SDK, Streamable HTTP" "Layer Workload"
    }
}

// Inbound
claimant -> meridian.ingress "Submits claims and checks their status through" "HTTP on kind; TLS designed" "Person"
adjuster -> meridian.ingress "Reviews triage proposals and records decisions through" "HTTP on kind; TLS designed" "Person"
agentDeveloper -> meridian.registry "Registers agents, tools, prompts and policies in" "Git pull request" "Person"
platformOperator -> meridian.observability "Watches cost and traces in" "HTTP, port-forward on kind" "Person"
meridian.ingress -> meridian.claimsApp "Routes claim and adjuster requests to" "HTTP (JSON, HTML pages)" "Layer Edge"

// Workload plane
meridian.claimsApp -> identityProvider "Validates user tokens against" "OIDC/JWKS" "Layer Workload,Designed"
meridian.claimsApp -> meridian.runtime "Starts and resumes triage runs on" "HTTP/JSON over mutual TLS" "Layer Workload"
meridian.claimsApp -> meridian.platformDb "Stores claims and adjuster decisions in; its scheduled sweep, under a role of its own, moves overdue and stranded claims and removes abandoned runs' checkpoints in" "PostgreSQL" "Layer Workload"
meridian.claimsMcp -> meridian.platformDb "Binds each call to its run and claim in, and writes notes, approval requests and audit events to" "PostgreSQL" "Layer Workload"
meridian.claimsMcp -> meridian.registry "Loads its tools, their schemas and the agent allowlists from" "File read at startup" "Layer Workload"

// Agent runtime
meridian.runtime -> meridian.registry "Loads agent definitions and tool allowlists from" "File read at startup" "Layer Services"
meridian.runtime -> meridian.gateway "Requests completions through" "HTTP/JSON over mutual TLS, tenant, agent and run headers" "Layer Services"
meridian.runtime -> meridian.policyMcp "Calls policy tools on" "MCP, Streamable HTTP over mutual TLS" "Layer Services"
meridian.runtime -> meridian.knowledgeMcp "Calls retrieval tools on" "MCP, Streamable HTTP over mutual TLS" "Layer Services"
meridian.runtime -> meridian.claimsMcp "Calls claim tools on" "MCP, Streamable HTTP over mutual TLS" "Layer Services"
meridian.runtime -> meridian.platformDb "Checkpoints graph state and writes audit events to" "PostgreSQL" "Layer Services"

// Model gateway
meridian.gateway -> meridian.registry "Loads model, provider, policy and tenant definitions from" "File read at startup" "Layer Services"
meridian.gateway -> meridian.keyVault "Reads provider credentials from" "HTTPS, workload identity" "Layer Services,Designed"
meridian.gateway -> azureOpenAI "Sends redacted prompts to EU deployments of" "HTTPS/JSON" "Layer Services"
meridian.gateway -> mistralFoundry "Routes EU-resident requests and fallbacks to" "HTTPS/JSON" "Layer Services,Designed"
meridian.gateway -> meridian.platformDb "Records usage, cost and policy decisions in" "PostgreSQL" "Layer Services"

// Tools and knowledge
meridian.policyMcp -> meridian.platformDb "Binds each call to its run and claim in, reads policies and claim history from, and writes audit events to" "PostgreSQL" "Layer Services"
meridian.policyMcp -> meridian.registry "Loads its tools, their schemas and the agent allowlists from" "File read at startup" "Layer Services"
meridian.knowledgeMcp -> meridian.platformDb "Binds each call to its run, claim and policy in, searches policy wording chunks in, and writes audit events to" "PostgreSQL, pgvector" "Layer Services"
meridian.knowledgeMcp -> meridian.registry "Loads its tools, their schemas and the agent allowlists from" "File read at startup" "Layer Services"
meridian.knowledgeMcp -> meridian.gateway "Requests embeddings through" "HTTP/JSON over mutual TLS, tenant, agent and run headers" "Layer Services"

// Evaluation
meridian.evals -> meridian.claimsApp "Submits golden-set claims to and reads their proposals from" "HTTP/JSON; in process in CI" "Layer Services"
meridian.evals -> meridian.platformDb "Reads each run's proposals and the gateway's usage ledger from" "PostgreSQL, CI's own instance" "Layer Services"
meridian.evals -> meridian.gateway "Asks the judge's question through" "HTTP/JSON, tenant, agent and run headers; in process in CI" "Layer Services"

// Telemetry, one arrow per emitting service. No service exports its logs yet:
// they stay in the pods' output.
meridian.claimsApp -> meridian.observability "Exports traces to" "OTLP" "Layer Workload"
meridian.runtime -> meridian.observability "Exports traces to" "OTLP" "Layer Services"
meridian.gateway -> meridian.observability "Exports traces and metrics to" "OTLP" "Layer Services"
meridian.policyMcp -> meridian.observability "Exports traces to" "OTLP" "Layer Services"
meridian.knowledgeMcp -> meridian.observability "Exports traces to" "OTLP" "Layer Services"
meridian.claimsMcp -> meridian.observability "Exports traces to" "OTLP" "Layer Workload"
