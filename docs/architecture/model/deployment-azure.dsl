// The Azure deployment environment: the design of the compute environment of
// S020, written as Terraform (infra/terraform/azure/), validated and scanned
// without an account and NEVER applied. Nothing here runs in Azure: no plan was
// made against an account, no sign-in was used, no price was paid, and the
// foundation (a vault, an OpenAI account, a state store) is the only part of
// Azure that exists (applied 2026-09-30, S007). Every node and instance carries
// the tag "Designed", always last, so that it renders dotted and faded
// (constraint C-07). The environment's name is one token: the view register
// check reads the deployment view's definition with a pattern that stops at the
// first space.
//
// The shape is the AWS and the Google Cloud views' shape (the same probe of
// 2026-10-06 sizes it): only what the cloud changes is placed, the edge, the
// Model Gateway (the one service that calls the model provider and the secret
// store), the Platform Database, the secret store and the model provider. The
// other services run in the same cluster from the same chart as on kind and
// reach nothing of Azure's; the Containers view shows them.
//
// An instance's arrows are the container relationships between the instances
// present; they cannot be written again. Only an infrastructure node takes an
// arrow written here. The model provider is an infrastructure node with a
// written, Designed arrow and not an instance of the Azure OpenAI software
// system, because an instance's inherited arrow would draw solid in an
// environment where nothing runs.
//
// The container registry (Azure Container Registry) is not placed, for the same
// reason as on the other clouds: its true arrow is the cluster's nodes pulling
// images, and Structurizr validates an arrow whose source is a deployment node,
// but the renderer then collapses the whole view into one corner. The network,
// the identities, the budgets and the workspace for the cluster's audit log are
// in the module's README (infra/terraform/azure/README.md) and not drawn.

azure = deploymentEnvironment "AzureDesigned" {

    region = deploymentNode "Azure region Sweden Central" "The module's default region, with West Europe the one other the module allows (hard rule 3: EU member states only). Designed." "Azure region" "Designed" {

        edge = infrastructureNode "Load balancer" "The cluster's standard load balancer in front of Envoy Gateway, as on kind: AKS makes it and the module does not. A web application firewall in front of it is a written design (ADR 11), not built. Designed." "Azure Load Balancer, Standard SKU" "Designed"

        cluster = deploymentNode "Managed Kubernetes" "Would run the Meridian chart as on kind: two Standard_D2s_v5 nodes on the Free control-plane tier, Azure CNI Overlay with the Cilium data plane (the engine that enforces the chart's default-deny policies), a public API server open to the operator's address only, Microsoft Entra sign-in with Azure roles, workload identity on. Besides the two instances drawn, the Agent Runtime, the three tool servers, the Claims Triage App and the Observability Stack would run here unchanged, reach nothing of Azure's, and are not drawn (the Containers view shows them). Written as Terraform and never applied. Designed." "Azure Kubernetes Service (AKS)" "Designed" {
            gatewayInstance = containerInstance meridian.gateway "" "Designed"
            ingressInstance = containerInstance meridian.ingress "" "Designed"
        }

        database = deploymentNode "Managed PostgreSQL" "Would hold the Platform Database with the vector extension, reached by private access only: a delegated subnet and a private DNS zone, no public endpoint. Burstable size, backups kept 7 days, no geo-redundancy, one server. Written as Terraform and never applied. Designed." "Azure Database for PostgreSQL flexible server, version 17, with pgvector" "Designed" {
            platformDbInstance = containerInstance meridian.platformDb "" "Designed"
        }

        secrets = deploymentNode "Secret store" "The foundation's vault, applied on 2026-09-30 and not made by the platform module: it holds no secret that a service reads yet. The module would write the database administrator's password into it, and a private endpoint for the cluster's path to it is written as Terraform and never applied. Public network access stays on, and a firewall is an undecided change to the foundation. The gateway's read of it is designed. Designed." "Azure Key Vault" "Designed" {
            keyVaultInstance = containerInstance meridian.keyVault "" "Designed"
        }

        provider = infrastructureNode "Model provider" "The foundation's Azure OpenAI account in Sweden Central (applied 2026-09-30, called from a laptop so far; label eu-region). The cluster's private path to it is written as Terraform, never applied. Designed." "Azure OpenAI Service" "Designed"

        edge -> cluster.ingressInstance "Forwards the public host's traffic to" "TCP; the certificate is cert-manager's, as on kind" "Designed"
        cluster.gatewayInstance -> provider "Sends redacted prompts to" "HTTPS, workload identity" "Designed"
    }
}
