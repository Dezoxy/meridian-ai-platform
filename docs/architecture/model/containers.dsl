// The system in scope and its containers.
// Groups mark the planes and the internet-facing edge so views can show them.

meridian = softwareSystem "Meridian AI Platform" "Builds, runs and governs LLM agents for a fictional insurer, with a claims-triage reference workload." {

    group "Edge (internet-facing)" {
        ingress = container "Ingress" "Terminates TLS and routes requests to the platform." "Envoy Gateway (Gateway API) on kind; Azure Application Gateway WAF in the Azure design" "Layer Edge,Gateway,Internet-exposed"
    }

    group "Control plane" {
        gateway = container "Model Gateway" "Routes model calls by policy: provider and region per data class, fallback behind a circuit breaker, per-tenant quotas and budgets, cost metering, PII redaction, audit." "Python, FastAPI" "Layer Services"
        runtime = container "Agent Runtime" "Hosts workload graphs behind the agent contract: start, pause for approval, resume; checkpoints; guardrails; MCP client." "Python, LangGraph host" "Layer Services"
        policyMcp = container "Policy MCP Server" "Policy lookup and claim history as MCP tools." "Python, MCP SDK, Streamable HTTP" "Layer Services"
        knowledgeMcp = container "Knowledge MCP Server" "Hybrid search over policy wording with citations, and the ingestion pipeline behind it." "Python, MCP SDK, pgvector" "Layer Services"
        evals = container "Evaluation Harness" "Replays the golden set, grades with rules and an LLM judge, gates CI." "Python, pytest" "Layer Services"
        observability = container "Observability Stack" "Collects traces, metrics and logs; serves dashboards and alerts." "OpenTelemetry Collector, Prometheus, Grafana, Tempo, Loki" "Layer Services"
        registry = container "Platform Registry" "Declarative source of truth: models, providers, tools, agents, policies, tenants." "YAML in git with JSON Schema, loaded at startup" "Layer Data"
        platformDb = container "Platform Database" "Claims, policy wording chunks, graph checkpoints, audit log, usage and evaluation results." "PostgreSQL 17 with pgvector" "Layer Data,Database"
        keyVault = container "Key Vault" "Provider credentials and signing secrets." "Azure Key Vault; Kubernetes Secrets on kind" "Layer Data,Vault"
    }

    group "Workload plane" {
        claimsApp = container "Claims Triage App" "Claims API and adjuster queue UI for the reference workload; owns the claims-triage graph package." "Python, FastAPI, Jinja, HTMX" "Layer Workload"
        claimsMcp = container "Claims MCP Server" "Claim notes and approval requests as MCP tools. Adjuster decisions are recorded by the Claims Triage App, never through a tool." "Python, MCP SDK" "Layer Workload"
    }
}

// Inbound
claimant -> meridian.ingress "Submits claims and checks their status through" "HTTPS" "Person"
adjuster -> meridian.ingress "Reviews triage proposals and records decisions through" "HTTPS" "Person"
agentDeveloper -> meridian.registry "Registers agents, tools, prompts and policies in" "Git pull request" "Person"
platformOperator -> meridian.observability "Watches SLOs, cost and traces in" "HTTPS" "Person"
meridian.ingress -> meridian.claimsApp "Routes claim and adjuster requests to" "HTTPS/JSON" "Layer Edge"

// Workload plane
meridian.claimsApp -> identityProvider "Validates user tokens against" "OIDC/JWKS" "Layer Workload"
meridian.claimsApp -> meridian.runtime "Starts and resumes triage runs on" "HTTPS/JSON, service identity" "Layer Workload"
meridian.claimsApp -> meridian.platformDb "Stores claims and adjuster decisions in" "PostgreSQL" "Layer Workload"
meridian.claimsMcp -> meridian.platformDb "Binds each call to its run and claim in, and writes notes, approval requests and audit events to" "PostgreSQL" "Layer Workload"
meridian.claimsMcp -> meridian.registry "Loads its tools, their schemas and the agent allowlists from" "File read at startup" "Layer Workload"

// Agent runtime
meridian.runtime -> meridian.registry "Loads agent definitions and tool allowlists from" "File read at startup" "Layer Services"
meridian.runtime -> meridian.gateway "Requests completions through" "HTTPS/JSON, tenant and agent headers" "Layer Services"
meridian.runtime -> meridian.policyMcp "Calls policy tools on" "MCP, Streamable HTTP" "Layer Services"
meridian.runtime -> meridian.knowledgeMcp "Calls retrieval tools on" "MCP, Streamable HTTP" "Layer Services"
meridian.runtime -> meridian.claimsMcp "Calls claim tools on" "MCP, Streamable HTTP" "Layer Services"
meridian.runtime -> meridian.platformDb "Checkpoints graph state and writes audit events to" "PostgreSQL" "Layer Services"

// Model gateway
meridian.gateway -> meridian.registry "Loads model, provider, policy and tenant definitions from" "File read at startup" "Layer Services"
meridian.gateway -> meridian.keyVault "Reads provider credentials from" "HTTPS, workload identity" "Layer Services"
meridian.gateway -> azureOpenAI "Sends redacted prompts to EU deployments of" "HTTPS/JSON" "Layer Services"
meridian.gateway -> mistralFoundry "Routes EU-resident requests and fallbacks to" "HTTPS/JSON" "Layer Services"
meridian.gateway -> meridian.platformDb "Records usage, cost and policy decisions in" "PostgreSQL" "Layer Services"

// Tools and knowledge
meridian.policyMcp -> meridian.platformDb "Binds each call to its run and claim in, reads policies and claim history from, and writes audit events to" "PostgreSQL" "Layer Services"
meridian.policyMcp -> meridian.registry "Loads its tools, their schemas and the agent allowlists from" "File read at startup" "Layer Services"
meridian.knowledgeMcp -> meridian.platformDb "Searches policy wording chunks in" "PostgreSQL, pgvector" "Layer Services"
meridian.knowledgeMcp -> meridian.gateway "Requests embeddings through" "HTTPS/JSON" "Layer Services"

// Evaluation
meridian.evals -> meridian.runtime "Executes golden-set claims on" "HTTPS/JSON" "Layer Services"
meridian.evals -> meridian.platformDb "Stores evaluation results in" "PostgreSQL" "Layer Services"

// Telemetry, one arrow per emitting service
meridian.claimsApp -> meridian.observability "Exports traces, metrics and logs to" "OTLP" "Layer Workload"
meridian.runtime -> meridian.observability "Exports traces, metrics and logs to" "OTLP" "Layer Services"
meridian.gateway -> meridian.observability "Exports traces, metrics and logs to" "OTLP" "Layer Services"
meridian.policyMcp -> meridian.observability "Exports traces and logs to" "OTLP" "Layer Services"
meridian.claimsMcp -> meridian.observability "Exports traces and logs to" "OTLP" "Layer Workload"
