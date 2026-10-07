# Read from the declared resources, never applied. None is a secret and none
# holds the project's ID or number: the instance's connection name is
# project:region:instance, so it is not an output, and neither is a resource's
# self link (a URL that begins with the project) or the workload principal. The
# repository's URL, REGION-docker.pkg.dev/PROJECT/meridian, names the project:
# the repository's name is an output, and the URL is built from the project and
# the Region in the operator's session.

output "region" {
  description = "Region of the module's regional resources. Holds no project identifier."
  value       = google_compute_subnetwork.nodes.region
}

output "cluster_name" {
  description = "Name of the GKE cluster. Holds no project identifier."
  value       = google_container_cluster.main.name
}

output "cluster_endpoint" {
  description = "Address of the cluster's control plane (its public endpoint). Reachable from the range in api_access_cidr only."
  value       = google_container_cluster.main.endpoint
}

output "repository_name" {
  description = "Name of the Artifact Registry repository. Its URL names the project and is not printed: build it from the project and the Region, in the operator's session."
  value       = google_artifact_registry_repository.main.repository_id
}

output "database_address" {
  description = "Internal address of the Private Service Connect endpoint that reaches the PostgreSQL instance (from the cluster's network only). Holds no project identifier and no secret: the module makes no database user."
  value       = google_compute_address.database.address
}

output "workload_secret_name" {
  description = "Name of the empty regional secret that the workload's service account may read. The name, not the resource's path: the path holds the project."
  value       = google_secret_manager_regional_secret.workload.secret_id
}
