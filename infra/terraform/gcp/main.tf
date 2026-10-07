# Names and the project pin. Written to the pattern of infra/terraform/aws, and
# like it, never planned and never applied.
locals {
  # One name for the cluster and the things named after it. Service account IDs
  # are unique in a project, so a second copy of this module in the same project
  # would collide: it is a test environment, one at a time.
  name = "meridian-gcp-test"

  # A zonal cluster needs one zone. Every Region the module accepts has a zone
  # "a" on Google's page "Regions and zones" (read 2026-10-07), and the machine
  # types the module allows (E2) are offered in each of them.
  zone = "${var.region}-a"

  labels = {
    project     = "meridian"
    environment = "gcp-test"
    managed-by  = "terraform"
  }
}

# The project the provider is configured for, read from Google Cloud. Nothing in
# the module prints its number or writes it down.
#
# Reading it needs the Cloud Resource Manager API on the project already: the
# data source is read before any google_project_service can enable it, so it is
# the one API this module cannot switch on for itself (README.md).
data "google_project" "current" {}

# The APIs the module's resources need, one by one (ADR 7, row 13 and "What a
# Terraform module needs": "to be confirmed by a plan", and never confirmed).
#   compute                Compute Engine: the network, subnet, router, NAT,
#                          address and forwarding rule.
#   container              Kubernetes Engine: the cluster and its node pool.
#   sqladmin               Cloud SQL Admin: the database instance.
#   artifactregistry       Artifact Registry: the repository.
#   secretmanager          Secret Manager: the regional secret and its binding.
#   iam                    IAM: the node service account.
#   cloudresourcemanager   the project's IAM policy (the node role) and the
#                          project data source.
#   billingbudgets         Cloud Billing Budget API: the budget.
# ADR 7 lists Service Networking and Agent Platform as well. Neither is here:
# Private Service Connect needs no peering, and a model is no resource (item 10).
# disable_on_destroy is false: the project is not this module's, and switching
# the Kubernetes API off suspends every cluster in it.
resource "google_project_service" "api" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "iam.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
  ])

  service            = each.value
  disable_on_destroy = false

  depends_on = [terraform_data.project_pin]
}

# The project pin. The Google provider has no list of allowed projects, so the
# module checks itself: every resource below names this one in depends_on, and a
# plan against any project but the expected one stops here, before it proposes
# anything. The messages name neither number.
#
# Written, and never seen to refuse: terraform validate does not evaluate a
# precondition, and nothing was planned. terraform_data is built into Terraform
# and needs no provider.
resource "terraform_data" "project_pin" {
  lifecycle {
    precondition {
      condition     = data.google_project.current.number == var.expected_project_number
      error_message = "The project the provider is configured for is not the project this module was written for (expected_project_number). Nothing is proposed. Check project_id and the credentials in use."
    }
  }
}
