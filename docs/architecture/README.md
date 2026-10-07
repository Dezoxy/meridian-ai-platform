# Architecture

## System

Meridian AI Platform

## Purpose

Builds, runs and governs LLM agents for Meridian Insurance, a fictional
insurer, with a claims-triage reference workload.

## Status

As built at the end of milestone M1 (2026-10-04, S018): the model was
compared with the code on `main`, element by element and arrow by arrow.
What a view draws solid exists in the code and runs on the local kind
cluster, in CI or from a laptop; nothing runs in Azure. What it draws
dotted and faded carries the tag `Designed` and is not built: the sign-in
against Entra ID, the read of provider credentials from Key Vault, and
Mistral. On kind the model is simulated (replay mode); network policies say
which pod may call which (S019), and since S055 the Agent Runtime, the Model
Gateway and the three tool servers serve mutual TLS and tell their callers
apart by certificate (ADR 4). The edge reaches the Claims Triage App over
plain HTTP and has no TLS, and there is no sign-in for people, so a tenant is
the calling service's word, bounded by its entry in the registry: TLS at the
edge and the sign-in (S020, S021) are designed. Since S037 (2026-10-06) the
Agent Runtime hosts a second agent framework, Microsoft Agent Framework, behind
the same `Host` protocol as LangGraph, for a second workload, the claim brief
([ADR 9](decisions/0009-run-a-second-agent-framework-behind-the-same-host-protocol.md)):
implemented, tested, and seen on kind once on 2026-10-06 under replay (no model
was called: a brief started, read, approved and filed for one claim and rejected
for another), so the Containers view's label for the runtime names a host that
has run there once, and not in Azure. The
register below says per view what was compared and what is omitted. Labels
used everywhere: implemented, simulated, designed.

## Architecture model

- [workspace.dsl](workspace.dsl): entry point, fragments in [model/](model/)
- Viewing and validating: `make view`, `make check` (see the root README)

## Reading paths

- Stakeholder: SystemContext, ClaimsTriage, the scope page.
- Engineer: Containers, ClaimsTriage, ClaimsApproval, the ADRs (ADR 9 for the
  second agent framework and the runtime's two hosts; ADR 10 for the designed
  split into services, a database each and their own releases).
- Architect: Containers, Governance, DeploymentAws, DeploymentGcp and
  DeploymentAzure (all designed), the ADRs (ADR 10 for where the platform is
  meant to go next, and what it would cost; ADR 11 for the Azure environment's
  five choices: written as Terraform, never applied).
- Operator: Governance, DeploymentAws, DeploymentGcp and DeploymentAzure
  (designed: nothing runs on AWS, on Google Cloud or in Azure beyond the
  foundation). The deployment view of kind and the observability views arrive
  with milestone M2.

- To see it run: the [demo script](../demo.md).

## View register

| Key | Audience | Question | Scope and selection | Omitted on purpose | Evidence | Update trigger | Visually verified |
|---|---|---|---|---|---|---|---|
| SystemContext | Everyone | What does the platform provide, who uses it, which external systems matter? | System, people, external systems | Internal structure | Compared with the code on 2026-10-04 (S018). Implemented, laptop only: the claimant's and adjuster's pages, the registry by pull request, Grafana by port-forward, Azure OpenAI called from a laptop and replayed (simulated) on kind. Designed, drawn dotted: every arrow to Entra ID (S021) and Mistral (S023) | Users, scope or providers change | Yes, PNG export of 2026-10-04 |
| Containers | Engineers, architects | What are the building blocks and how do they connect? | All containers except governance-only ones | Observability, evaluation, registry, Key Vault, second provider; the migration, seed and ingestion jobs, which run as the database's owner at deploy time; the Kubernetes Secrets each pod reads its database role from; since 2026-10-06 (S066), the gateway's rate store, a Redis on kind that only the gateway reaches (private to it, so no relationship changes; the view to split by plane would draw it) | Compared with the code and the kind manifests on 2026-10-04 (S018; since S019 the manifests are a Helm chart that renders the same objects): six services, each under a database role of its own, and the sweep as a CronJob of the Claims Triage App under a seventh role; every arrow's protocol is what kind runs (plain HTTP from the edge; since S055, mutual TLS from the Claims Triage App to the runtime, from the runtime to the gateway and the three tool servers, and from the Knowledge MCP Server to the gateway; TLS at the edge later). Designed, drawn dotted: token validation against Entra ID (S021). Since S037 (ADR 9) the Agent Runtime's label names two hosts, and no container or arrow was added; the second host and the claim brief are implemented, tested, and seen on kind once on 2026-10-06 under replay (a brief approved and filed, another rejected; the run meter split by agent; the migrate Job applied 0023 and 0024); not seen: a failed brief run, a changed-workflow refusal, the sweep closing a brief, a live model, a second replica, the brief's trace. | An interface or boundary changes | Yes, PNG export of 2026-10-04 |
| Governance | CTO, operators, security | How are models, policies, budgets and evidence governed? | Gateway, registry, Key Vault, database, evaluation, observability, providers, runtime | Workload containers; the tool servers' registry reads and audit writes (the Containers view has them) | Compared with the code on 2026-10-04 (S018). Implemented: the registry loaded at startup, the usage ledger and audit rows, traces from every service and metrics from the gateway only (no service exports logs yet), the cost dashboard on kind. Since S064 (2026-10-06, on kind): the runtime, the tool servers and the Claims Triage App export metrics too, and an agent on the node ships the services' output to Loki; no service exports its logs itself. The Evaluation Harness runs in tests, CI and from the command line, on CI's own database, and is not deployed. Designed, drawn dotted: the gateway's read of Key Vault (on kind the Secrets hold database passwords only, and Azure OpenAI is called without a key) and Mistral (S023); alerts and SLOs (S024) | Policy, provider or evidence flow changes | Yes, PNG export of 2026-10-04 |
| ClaimsTriage | Stakeholders, engineers | What happens from claim submission to a paused proposal? | One runtime scenario, eight steps | Fraud rules tool, guardrail internals; the Claims Triage App storing the claim, its proposal and each state change; the embedding of every search query through the gateway; the approval request the run records before it pauses | Compared with the triage graph and the Claims API on 2026-10-04 (S018), in tests and on kind (laptop only, `make demo`): the policy, retrieval and model steps match the graph of S014 and the pause matches S015; the prompt is redacted and screened since S047. Step 6 happens only when the rules need the model's answer, and step 8's pause only for a claim referred to an adjuster. Step 7 is simulated on kind (the replay provider answers; nothing leaves the cluster) and was run against Azure from a laptop. Compared again on 2026-10-06 (S031, ADR 5): the graph is a supervisor and four workers, and the view's eight steps stand, because they name the containers a run calls and not the graph's nodes; steps 4 and 5 are the `intake` and `terms` workers, step 6 the `assessor` and the rules, step 8 the `approvals` worker's request and the supervisor's pause | Graph steps or failure handling change | Yes, PNG export of 2026-10-04 |
| ClaimsApproval | Stakeholders, engineers | What happens when an adjuster decides? | One runtime scenario, six steps | Notification of the claimant; the runtime marking the run resumed before it loads the checkpoint | Compared with the code on 2026-10-04 (S018), in tests and on kind (laptop only, `make demo`): the Claims Triage App records the decision and its audit event in one transaction, and the run reads the recorded decision from the claims tool server instead of being given it (S015). Designed: the adjuster's identity on the decision (S021); no decision row names a person yet | Approval semantics change | Yes, PNG export of 2026-10-04 |
| DeploymentAws | Architects, operators | Where would the platform run on AWS, and which infrastructure would it share? | One designed deployment environment (`AwsDesigned`) in the Region `eu-central-1` (Frankfurt): a managed Kubernetes node (Amazon EKS) holding the Ingress and the Model Gateway, a managed PostgreSQL node (Amazon RDS for PostgreSQL with pgvector) holding the Platform Database, a secret store node (AWS Secrets Manager) holding the Key Vault container, and two infrastructure nodes: the load balancer in front of the Ingress and the model provider (Amazon Bedrock). Only what the cloud changes is placed | The Agent Runtime, the three tool servers, the Claims Triage App and the Observability Stack, which would run in the same cluster from the same chart as on kind and reach nothing of AWS's (the Containers view shows them); the Platform Registry and Evaluation Harness containers; the container registry (Amazon ECR), whose true arrow, the cluster's nodes pulling images from it, validates but makes the renderer collapse the view, so the registry is not in the model; the network's subnets and zones, IAM roles, the private endpoints, the state bucket, the second provider, instance counts and failure domains (the mapping ADR's text covers the network and the roles); the Application Load Balancer with AWS WAF, which the mapping ADR names as the managed alternative to the Network Load Balancer drawn and does not take | Designed only (S025): no AWS account exists (C-05) and nothing is deployed. Since S036 a Terraform module for most of this environment (the network, the cluster, the registry, the database and a secret with its role, but not the Ingress, the load balancer or the model provider) exists as code, checked without an account and never applied; the view is not changed by it and stays designed. Every node and instance is tagged `Designed` and drawn dotted, and the title says DESIGNED. The Ingress keeps the red border of its `Internet-exposed` tag, which wins over the faded look of `Designed` (it is still dotted). The arrows between instances are the container relationships the instances inherit, so the gateway's arrow to the database is solid because kind runs it, and its arrow to the secret store is dotted because that read is designed; the two arrows written in the view (load balancer to Ingress, gateway to the model provider) are tagged `Designed`. The Ingress is Envoy Gateway on kind, and since ADR 11 (S020) its technology string no longer names an Azure edge. The load balancer is a Network Load Balancer in front of Envoy Gateway, as on kind, so the certificate is cert-manager's and AWS WAF, which does not protect a Network Load Balancer, has no place in front of it: T-02's firewall has no counterpart in this view. The equivalents, their sources and the date each was read are in the ADR "Map the Azure platform to AWS" | The mapping ADR "Map the Azure platform to AWS" is amended by S036 or S020; S036 builds the environment; a container the view places changes | Yes, PNG export of 2026-10-06, exported again after the load balancer's text changed, top to bottom (10 boxes and 4 arrows; read at full size, every label legible, the load balancer's text fits its box, no arrow through an element; two flaws, both still there: the gateway's arrow to the secret store crosses the border of the managed PostgreSQL node, and the load balancer's arrow crosses the gateway's arrow to the model provider) |
| DeploymentGcp | Operators, architects | Where would the platform run on Google Cloud, and what does the cloud change? | One designed environment: the region europe-west3, the load balancer, the managed Kubernetes cluster with the Ingress and the Model Gateway, the managed PostgreSQL with the Platform Database, the secret store with Key Vault, the model provider. Only what the cloud changes is placed | The five other services (Agent Runtime, the three tool servers, Claims Triage App) and the Observability Stack, which would run in the same cluster from the same chart and reach nothing of Google Cloud's (the Containers view shows them); the container registry (Artifact Registry), because the renderer cannot lay out an arrow whose source is a deployment node and one instance's arrow would be false; the network's ranges, IAM and workload identity bindings, instance counts and failure domains (the cluster is one zonal cluster); the model provider's second location (its node says the endpoint is the `eu` multi-region, not Frankfurt) | Designed only (S077): no Google Cloud project or billing account exists and nothing is built, applied or priced (C-05). Every node, instance and written arrow carries `Designed`; the arrows between instances are the container relationships, so the gateway's arrow to the database is solid because kind runs it, and the Ingress keeps the red border of `Internet-exposed`. The technology strings and the model provider's residency label come from the vendor's pages read on 2026-10-06, cited in the mapping ADR of S077; one instance shows the container's own text (Key Vault still names Azure; the Ingress no longer does, since ADR 11) | The mapping ADR changes, S078 builds or falsifies it, or a container changes | Yes, PNG export of 2026-10-06, read at full size: every label legible, the boundaries nest as meant, no arrow crosses a box's text. Two flaws: the load balancer's arrow crosses the gateway's arrow to the model provider, and the gateway's arrow to the secret store clips the top right corner of the managed PostgreSQL box and of the database cylinder inside it. Top to bottom was kept; left to right put the same arrow through the middle of that box and its label |
| DeploymentAzure | Operators, architects | Where would the platform run on Azure, and what does the cloud change? | One designed environment (`AzureDesigned`) in the region Sweden Central: the load balancer in front of Envoy Gateway, the managed Kubernetes cluster (AKS) holding the Ingress and the Model Gateway, the managed PostgreSQL (flexible server, private access) holding the Platform Database, the foundation's Key Vault as the secret store, and the foundation's Azure OpenAI account as the model provider. Only what the cloud changes is placed | The five other services and the Observability Stack, which would run in the same cluster from the same chart (the Containers view shows them); the container registry (Azure Container Registry), for the AWS views' reason (its true arrow validates and makes the renderer collapse the view); the network, the identities, the budgets, the workspace for the cluster's audit log and the private endpoints, which the module's README describes; the model provider's second region; the web application firewall, which ADR 11 writes as a design | Designed (S020): the module `infra/terraform/azure/` is written, validated and scanned without an account and NEVER applied, and nothing of the platform runs in Azure. The vault and the OpenAI account exist (the foundation, applied 2026-09-30, S007) and are drawn dotted with the rest, because what the view shows is the platform's place in them. Every node and instance carries `Designed`; the Ingress keeps the red border of `Internet-exposed`; the arrows between instances are the container relationships (the gateway's arrow to the database is solid because kind runs it, the one to the vault is dotted because that read is designed) and the two written arrows are `Designed`. The provider is an infrastructure node with a written arrow, not an instance of the Azure OpenAI system, so that no solid arrow is inherited in an environment where nothing runs. The Ingress is Envoy Gateway, as on kind (owner's decision, 2026-10-07, ADR 11) | The module is applied or its design changes (ADR 11 is amended), or a container the view places changes | Yes, PNG export of 2026-10-07, read at full size: 10 boxes and 4 arrows, every label legible, every box's text fits, the boundaries nest as meant. Two flaws, the AWS view's: the load balancer's arrow crosses the gateway's arrow to the model provider, and the gateway's arrow to the secret store clips the top right corner of the managed PostgreSQL box. Top to bottom, as the others |

Sizes on 2026-10-04, as boxes and arrows: SystemContext 8 and 9, Containers
12 and 18, Governance 11 and 14, ClaimsTriage 9 and 8, ClaimsApproval 6 and
6. The first four are over the view skill's starting budgets (7 and 8 for
an overview, 10 and 12 for a structural view, 7 participants and 8
interactions for a scenario). None was split: each PNG export was read at
full size on that day, every label was legible, and the one flaw found is
the long label of the Claims Triage App's database arrow, which lies across
the workload plane's border in the Containers view. Containers is the view
to split first, by plane, when a container is added. Each view's key is a
separate file in the export (`<Key>-key.png`); it shows the dotted, faded
style of `Designed`. DeploymentAws, added on 2026-10-06 as a designed
environment, is 10 boxes and 4 arrows, inside the deployment budget of 12
boxes and 8 arrows, boundary boxes counted. It places only what the cloud
changes: a probe the same day found that placing all twelve containers made
14 boxes and 15 arrows and a picture of 3680 by 5936 pixels that was not
readable, so the other services are named under what the view omits.

DeploymentGcp, on 2026-10-06 and designed only: 10 boxes and 4 arrows, inside
the deployment budget of 12 and 8 with the region, the cluster and the two
managed-service boxes counted. Two of the four arrows are written in the
model (load balancer to Ingress, gateway to model provider) and two are the
container relationships between the instances present (gateway to database
and to Key Vault). Four of the twelve containers are placed (the Ingress, the
Model Gateway, the Platform Database and Key Vault): the AWS step's probe
that placed the six services came out at 14 boxes and 15 arrows and 3680 by
5936 pixels, which could not be read, so only what the cloud changes is
drawn.

DeploymentAzure, on 2026-10-07 and designed only: 10 boxes and 4 arrows, the
same shape and the same budget as the other two (10 boxes and 4 arrows, inside
12 and 8). Two of the four arrows are written in the model (load balancer to
Ingress, gateway to model provider) and two are the container relationships
between the instances present (gateway to database and to Key Vault). The
environment's Terraform exists and has never been applied; the view is not
changed by that and stays designed.

## Key decisions

- [0001 Run on Azure and kind, design AWS](decisions/0001-run-on-azure-and-kind-design-aws.md)
- [0002 Run LangGraph behind a framework-agnostic platform contract](decisions/0002-langgraph-behind-a-framework-agnostic-contract.md)
- [0003 Build a thin model gateway instead of adopting LiteLLM](decisions/0003-build-a-thin-model-gateway.md)
- [0004 Prove a service's identity with mutual TLS and cert-manager](decisions/0004-prove-service-identity-with-mutual-tls.md)
- [0005 Split an agent into workers routed by code, with their own tool lists](decisions/0005-split-an-agent-into-workers-routed-by-code.md)
- [0006 Map the Azure platform to AWS](decisions/0006-map-the-azure-platform-to-aws.md)
- [0007 Map the Azure platform to Google Cloud](decisions/0007-map-the-azure-platform-to-google-cloud.md)
- [0008 Share the gateway's rate windows in Redis](decisions/0008-share-the-gateways-rate-windows-in-redis.md)
- [0009 Run a second agent framework behind the same host protocol](decisions/0009-run-a-second-agent-framework-behind-the-same-host-protocol.md)
- [0010 Split the platform into services with a database each and their own releases](decisions/0010-split-the-platform-into-services-with-a-database-each-and-their-own-releases.md)
- [0011 Run the Azure platform per demo day on AKS, Envoy at the edge](decisions/0011-run-the-azure-platform-per-demo-day-on-aks-with-envoy-at-the-edge.md)


New ADR: copy [templates/adr.md](templates/adr.md) to
`decisions/NNNN-short-title.md`. Keep this index current; there is
deliberately no README inside `decisions/`, because the ADR importer parses
every `.md` file in that folder.

## Written documentation

| Area | Documents |
|---|---|
| Narrative (opens the tab and the PDF) | [01 overview](overview/01-meridian-ai-platform.md) · [02 scope](overview/02-scope.md) · [03 glossary](overview/03-glossary.md) |
| Requirements | [constraints](requirements/constraints.md) · [quality attributes](requirements/quality-attributes.md) |
| Security | [threat model](security/threat-model.md) · [data classification](security/data-classification.md) |
| Deployment | [Azure platform](deployment/azure-platform.md): every Azure service the platform uses or designs, the residency rule in words no cloud owns, and, in its last section, a comparison of a managed and a self-managed cluster (not a plan) |
| Code | [import layering](code/import-layering.md): which Python package may import which, as the six import-linter contracts enforce it, with one Mermaid diagram of the layers. Not a component view: none exists yet |

Only `overview/` is imported into the model by `!docs`. Registers reach it by
symlink (`overview/10-constraints.md`, `11-quality-attributes.md`,
`22-data-classification.md`, `23-threat-model.md`, `30-azure-platform.md`,
`40-import-layering.md`).
The security and quality registers came before the code they govern, so later
steps cite their IDs instead of inventing them. Add other concern documents
when there is something true to say: reliability when something runs, and
the deployment of each cloud when there is something to place.

## Diagrams

The Structurizr model owns structure; Mermaid owns behaviour and logic (state
machines, decision logic, branching procedures) that the model has no element
for, and only where a list or table cannot show it. Never a C4 diagram in
Mermaid. The rule is "Model or Mermaid" in
`.agents/skills/architecture-views/SKILL.md`.

- **Derived blocks**: a view exported as Mermaid, allowed only in Markdown the
  Documentation tab does not import (READMEs, not `overview/` or `decisions/`).
  The line above its fence is `<!-- mermaid-view: Key -->`. Run
  `make mermaid-views` to regenerate it; `make docs` fails a stale one. Never
  edit the body by hand.
- **Documentation tab**: renders Mermaid through public mermaid.ink, enabled
  in [workspace.dsl](workspace.dsl). Every page view sends the diagram source
  to that server, acceptable because this repository is public and
  synthetic. Some DNS blocklists, such as HaGeZi Ultimate, block
  mermaid.ink; there the diagrams show as broken images until it is
  allowlisted. Comment out both parts in `workspace.dsl` to show the source
  instead.
- **PDF**: `make pdf` renders every Mermaid block locally with the pinned
  mermaid-cli image (`MERMAID_IMAGE` in the Makefile); nothing leaves the
  machine.
- `make mermaid` regenerates the derived blocks and renders every fence, which
  catches syntax errors that GitHub would show as an error box.
  The `derived diagrams` job of the `docs` workflow runs both halves on
  every pull request, so a fence that does not parse fails a required
  check. Under rootless Docker `make mermaid-render` and `make pdf` cannot
  write their PNGs (the container's user is not the folder's owner); there
  the pull request's job is the proof.

## Not documented here

API contracts (`api/`), Terraform and Helm (`infra/`), application code
(`src/`), service level objectives, alert rules and runbooks
([`docs/operations/`](../operations/README.md)). This folder links to them;
it does not copy them.
