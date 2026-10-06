// The AWS deployment environment: a mapping of the Azure design (S025),
// designed and nothing else. Nothing here is built, applied or priced; no AWS
// account exists (C-05). Every node and instance carries the tag "Designed",
// always last, so that it renders dotted and faded (constraint C-07).
// The environment's name is one token: the view register check reads the
// deployment view's definition with a pattern that stops at the first space.
//
// Only what the cloud changes is placed: the edge, the Model Gateway (the one
// service that calls the model provider and the secret store), the Platform
// Database, the secret store and the model provider. The other
// services run in the same cluster from the same chart as on kind and reach
// nothing of the cloud's; the Containers view shows them. Six more instances
// would not fit the view's budget and would not be legible (probe of 2026-10-06:
// 14 boxes and 15 arrows, 3680 x 5936 pixels).
//
// An instance's arrows are the container relationships between the instances
// present; they cannot be written again. Only an infrastructure node takes an
// arrow written here.
//
// The container registry (Amazon ECR) is not placed. The true arrow is the
// cluster's nodes pulling images from it, and Structurizr validates an arrow
// whose source is a deployment node, but the renderer then collapses the whole
// view into one corner (exported twice, left to right and top to bottom). An
// arrow from one container instance would be false (every pod's image comes
// from it) and a registry with no arrow fails the disconnected-element check.

aws = deploymentEnvironment "AwsDesigned" {

    region = deploymentNode "AWS Region eu-central-1 (Frankfurt)" "The Region for the test of the mapping; the mapping ADR says why. Designed." "AWS Region" "Designed" {

        edge = infrastructureNode "Load balancer" "Takes the public host's traffic into the cluster. An Application Load Balancer with AWS WAF, or a Network Load Balancer in front of Envoy Gateway: not chosen (the mapping ADR says what each changes). Designed." "Elastic Load Balancing" "Designed"

        cluster = deploymentNode "Managed Kubernetes" "Would run the Meridian chart as on kind. Besides the two instances drawn, the Agent Runtime, the three tool servers, the Claims Triage App and the Observability Stack would run here unchanged, reach nothing of AWS's, and are not drawn (the Containers view shows them). Designed." "Amazon EKS" "Designed" {
            gatewayInstance = containerInstance meridian.gateway "" "Designed"
            ingressInstance = containerInstance meridian.ingress "" "Designed"
        }

        database = deploymentNode "Managed PostgreSQL" "Would hold the Platform Database with the vector extension, reached inside the network. Designed." "Amazon RDS for PostgreSQL with pgvector" "Designed" {
            platformDbInstance = containerInstance meridian.platformDb "" "Designed"
        }

        secrets = deploymentNode "Secret store" "Would hold the runtime secrets that Key Vault holds in the Azure design. A Bedrock call needs no stored provider credential, so it holds fewer. Designed." "AWS Secrets Manager" "Designed" {
            keyVaultInstance = containerInstance meridian.keyVault "" "Designed"
        }

        provider = infrastructureNode "Model provider" "Serves the models. From Frankfurt a Claude model is reached through the eu. inference profile (label eu-zone), not in-Region (eu-region). Designed." "Amazon Bedrock" "Designed"

        edge -> cluster.ingressInstance "Forwards the public host's traffic to" "HTTP, or TLS passthrough: not chosen" "Designed"
        cluster.gatewayInstance -> provider "Sends redacted prompts to" "HTTPS, IAM-signed requests" "Designed"
    }
}
