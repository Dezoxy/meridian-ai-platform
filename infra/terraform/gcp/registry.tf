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
