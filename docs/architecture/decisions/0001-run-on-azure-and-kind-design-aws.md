# 1. Run on Azure and kind, design AWS

Date: 2026-09-29

## Status

Accepted

## Context

The platform is a portfolio reference. Its intended audience works mainly on
Azure (Entra ID, Azure OpenAI with EU data zones, AKS); a smaller part works
on AWS, where edge services such as CloudFront, Route 53 and WAF matter. The
earlier proposal was a primary Azure deployment plus a smaller AWS reference
deployment with EKS, Route 53, CloudFront, WAF and an application load
balancer.

One person builds and operates everything (C-01), Azure spend is capped
(C-04), the only cloud account with billing is the existing pay-as-you-go
Azure subscription (C-05), and the whole demo must run on a laptop (C-06).

## Decision drivers

- C-01: a second deployed cloud doubles infrastructure code, identity,
  networking, cost and operations for one maintainer.
- C-04 and C-05: an always-on environment would consume the monthly ceiling
  by itself, and AWS has no billing account.
- C-06: the everyday environment has to be local anyway.
- Evidence value: the AWS-specific skills are edge and networking services
  that an agent platform barely exercises; the rest (Kubernetes,
  infrastructure as code, CI/CD, SLOs, incident handling, technical
  leadership) is cloud-neutral and shown on Azure.

## Considered options

1. Azure primary plus a deployed AWS reference environment.
2. Azure only, created and destroyed per demo day, with kind as the everyday
   environment; AWS as a designed mapping: a deployment view, a service
   mapping ADR and, later, a Terraform module that is validated but never
   applied.
3. AWS only.
4. Homelab only (Proxmox, no Kubernetes).

## Decision

Option 2. Terraform creates the Azure environment (resource group, virtual
network, AKS, container registry, Key Vault, PostgreSQL Flexible Server with
pgvector, Azure OpenAI in Sweden Central, Entra application registration,
Workload Identity) and destroys it after a demo. The same Helm charts run on
kind. AWS is documented as a mapping of every Azure service to its AWS
equivalent, with a deployment view, written alongside the Azure deployment
view in milestone M2. A validate-only AWS Terraform module is an optional
milestone M4 item.

Amended on 2026-10-06 (S036): a Terraform module for AWS now exists under
`infra/terraform/aws/`. It is checked (`terraform validate` and a policy scan,
run without an account) and not applied, and nothing about it runs in CI. The
owner's word of that day: "add the aws template too and we will test it in a
real aws enviroment and you should go until azure step where we have to spend
some money on it". That changes the sentence above: the module is no longer
only validated, because the second half of S036 applies it once in the
owner's AWS account, after the cost of an hour of it is stated and the owner
says yes. Until that run the decision stands as written: Azure is the
deployed cloud, AWS has no billing account (C-05) and no deployment on AWS
is claimed.

## Consequences

Positive:

- Half the infrastructure work and no second identity model.
- Recreating the environment in minutes is itself evidence of infrastructure
  maturity, and keeps spend within C-04.
- The platform packaging stays cloud-neutral (Helm, OpenTelemetry,
  PostgreSQL), which is what the AWS mapping relies on.

Negative / accepted trade-offs:

- No hands-on AWS deployment to show; the mapping ADR and,
  optionally, a Claude adapter on AWS Bedrock in an EU region are the
  substitutes.
- Ephemeral environments cannot show long-running operational history;
  dashboards and incident records come from game days and are labelled
  simulated.

Rejected options:

- Option 1: too much work for one person and for the evidence gained.
- Option 3: contradicts three of the four roles and has no billing account.
- Option 4: no Kubernetes at home; it would not evidence a cloud platform.

## Risks

- The risk register does not exist yet; the AWS evidence gap becomes its
  first entry when it is created.

## Related

- Requirements: C-01, C-04, C-05, C-06
- Architecture views: SystemContext, Containers
- Other ADRs: [3. Build a thin model gateway](0003-build-a-thin-model-gateway.md)
