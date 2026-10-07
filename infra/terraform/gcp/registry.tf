# One Docker repository for the one image the chart runs (ADR 7, row 19). A
# repository is a regional resource of the project, so the Region is part of the
# image reference the chart would be given. Declared, never planned, never
# applied.
resource "google_artifact_registry_repository" "main" {
  repository_id = "meridian"
  location      = var.region
  format        = "DOCKER"
  description   = "The image the Meridian chart runs. Made by the module in infra/terraform/gcp."
  labels        = local.labels

  # The chart pulls by digest, so an immutable tag costs nothing and keeps a tag
  # from being moved under a running deployment.
  docker_config {
    immutable_tags = true
  }

  depends_on = [terraform_data.project_pin, google_project_service.api]
}

# The nodes pull the chart's image with the module's own node service account,
# and its project role holds no Artifact Registry permission (cluster.tf). One
# member, the reader role, ON this one repository and not the project: nothing
# else in the project's repositories is readable by the nodes, and nothing may
# push. _iam_member adds one member to one role and removes nothing granted
# elsewhere (ADR 7, row 6). Read 2026-10-07: the provider's page for
# google_artifact_registry_repository_iam_member, and Google's page "Access
# control with IAM" (roles/artifactregistry.reader: view and get artifacts, view
# repository metadata).
resource "google_artifact_registry_repository_iam_member" "nodes_pull" {
  location   = google_artifact_registry_repository.main.location
  repository = google_artifact_registry_repository.main.name
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.node.member

  depends_on = [terraform_data.project_pin]
}
