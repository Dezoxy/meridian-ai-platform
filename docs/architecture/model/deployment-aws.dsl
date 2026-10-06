// The AWS deployment environment: a mapping of the Azure design (S025),
// designed and nothing else. Nothing here is built, applied or priced; no AWS
// account exists (C-05). Every node and instance carries the tag "Designed",
// always last, so that it renders dotted and faded (constraint C-07).
// The environment's name is one token: the view register check reads the
// deployment view's definition with a pattern that stops at the first space.

aws = deploymentEnvironment "AwsDesigned" {

    region = deploymentNode "EU region (to be chosen)" "A region in the EU for a test of the mapping. Designed; the mapping ADR chooses it." "AWS region" "Designed" {

        cluster = deploymentNode "Managed Kubernetes" "Would run the Meridian chart's services. Designed." "Amazon EKS" "Designed" {
            gatewayInstance = containerInstance meridian.gateway "" "Designed"
        }

        database = deploymentNode "Managed PostgreSQL" "Would hold the Platform Database. Designed." "Amazon RDS for PostgreSQL" "Designed" {
            platformDbInstance = containerInstance meridian.platformDb "" "Designed"
        }
    }
}
