# Names and the project pin. Written to the pattern of infra/terraform/aws, and
# like it, never planned and never applied.
locals {
  # One name for the cluster and the things named after it. Service account IDs
  # are unique in a project, so a second copy of this module in the same project
  # would collide: it is a test environment, one at a time.
  name = "meridian-gcp-test"

  # A zonal cluster needs one zone, and a Region's zones are not always a, b and
  # c: Google's page "Regions and zones" (read 2026-10-07,
  # https://docs.cloud.google.com/compute/docs/regions-zones) lists b, c and d
  # for europe-west1 (St. Ghislain) and a, b and c for each of the other ten.
  # The map holds the first zone the page lists for each Region of the list in
  # variables.tf, and the machine types the module allows (E2) are offered in
  # every zone of every one of them. There is no fallback: a Region added to the
  # list without an entry here fails the plan at the index below (and a test),
  # where a zone built from the Region's name would fail an apply at the cluster,
  # after the database was made and was billing.
  zones = {
    "europe-central2"   = "europe-central2-a"
    "europe-north1"     = "europe-north1-a"
    "europe-north2"     = "europe-north2-a"
    "europe-southwest1" = "europe-southwest1-a"
    "europe-west1"      = "europe-west1-b"
    "europe-west3"      = "europe-west3-a"
    "europe-west4"      = "europe-west4-a"
    "europe-west8"      = "europe-west8-a"
    "europe-west9"      = "europe-west9-a"
    "europe-west10"     = "europe-west10-a"
    "europe-west12"     = "europe-west12-a"
  }

  zone = local.zones[var.region]

  labels = {
    project     = "meridian"
    environment = "gcp-test"
    managed-by  = "terraform"
  }
}

# The project the provider is configured for, read from Google Cloud. What a
# plan or an apply prints of it: no variable value is printed (the variables
# that name the project are sensitive), but Google's own resource IDs name the
# project (projects/<ID>/...), Terraform prints the data source's own line
# (data.google_project.current: Read complete, with the project in its id), and
# a failed precondition below prints the number of the project the provider
# reached, because that value is not sensitive. All of that is shown, and
# nothing redacts it: there is no wrapper here, as there is for the AWS module.
#
# Reading it needs the Cloud Resource Manager API, and the Service Usage API
# (which google_project_service itself needs), enabled on the project by hand
# before the first init: gcloud services enable or the console, from the
# operator's own session. The module cannot do it for itself: the resource that
# enables an API hangs on the pin, the pin on the data source, and the data
# source on that API. On a project that has neither, the first plan stops at
# this data source and proposes nothing.
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
