// The Google Cloud deployment environment: a mapping of the Azure design
// (S077), designed and nothing else. Nothing here is built, applied or priced;
// no Google Cloud project exists and no billing account was opened (C-05).
// Every node and instance carries the tag "Designed", always last, so that it
// renders dotted and faded (constraint C-07). The environment's name is one
// token: the view register check reads the deployment view's definition with a
// pattern that stops at the first space.
//
// Only what the cloud changes is placed: the edge, the Model Gateway (the one
// service that calls the model provider and the secret store), the Platform
// Database, the secret store and the model provider. The other services run in
// the same cluster from the same chart as on kind and reach nothing of the
// cloud's; the Containers view shows them. Six more instances would not fit
// the view's budget and would not be legible (the AWS step's probe of
// 2026-10-06 placed all of them: 14 boxes and 15 arrows, 3680 x 5936 pixels).
//
// An instance's arrows are the container relationships between the instances
// present; they cannot be written again. Only an infrastructure node takes an
// arrow written here.
//
// The container registry (Artifact Registry) is not placed. The true arrow is
// the cluster's nodes pulling images from it, and Structurizr validates an
// arrow whose source is a deployment node, but the renderer then collapses the
// whole view into one corner (the AWS step exported it twice, left to right and
// top to bottom). An arrow from one container instance would be false (every
// pod's image comes from it) and a registry with no arrow fails the
// disconnected-element check. Not tried again here.

gcp = deploymentEnvironment "GcpDesigned" {

    region = deploymentNode "Google Cloud region europe-west3 (Frankfurt)" "The region for the test of the mapping, for the cluster and the database; the mapping ADR says why. The model provider is a second location (its node says which). Designed." "Google Cloud region" "Designed" {

        edge = infrastructureNode "Load balancer" "A passthrough Network Load Balancer in front of Envoy Gateway, as on kind. The GKE Gateway controller is the managed alternative: not built (the mapping ADR says what each changes). Designed." "Cloud Load Balancing, passthrough Network Load Balancer" "Designed"

        cluster = deploymentNode "Managed Kubernetes" "Would run the Meridian chart as on kind, in one zonal cluster of Standard mode. Network policies are enforced only if Dataplane V2 (or the Calico add-on) is chosen, and workload identity is optional on Standard: both would be set. Besides the two instances drawn, the Agent Runtime, the three tool servers, the Claims Triage App and the Observability Stack would run here unchanged, reach nothing of Google Cloud's, and are not drawn (the Containers view shows them). Designed." "Google Kubernetes Engine, Standard mode, one zonal cluster" "Designed" {
            gatewayInstance = containerInstance meridian.gateway "" "Designed"
            ingressInstance = containerInstance meridian.ingress "" "Designed"
        }

        database = deploymentNode "Managed PostgreSQL" "Would hold the Platform Database with the vector extension, reached inside the network by private services access. Cloud SQL for PostgreSQL 16 or later defaults to the Enterprise Plus edition, so the cheaper edition would be named. Designed." "Cloud SQL for PostgreSQL with the vector extension" "Designed" {
            platformDbInstance = containerInstance meridian.platformDb "" "Designed"
        }

        secrets = deploymentNode "Secret store" "Would hold the runtime secrets that Key Vault holds in the Azure design, as regional secrets so that the data stays in one location (a global secret with automatic replication carries no EU guarantee). Designed." "Secret Manager" "Designed" {
            keyVaultInstance = containerInstance meridian.keyVault "" "Designed"
        }

        provider = infrastructureNode "Model provider" "Gemini on pay-as-you-go is called at the eu multi-region endpoint (label eu-zone), not in europe-west3 (label eu-region: Provisioned Throughput only). Drawn in the region box for layout: its location is the EU. Designed." "Gemini Enterprise Agent Platform (Vertex AI)" "Designed"

        edge -> cluster.ingressInstance "Forwards the public host's traffic to" "HTTP, or TLS passthrough: not chosen" "Designed"
        cluster.gatewayInstance -> provider "Sends redacted prompts to" "HTTPS, application default credentials" "Designed"
    }
}
