# Implemented as code, never applied (S079). Names, labels, the constants that
# the nodes' boot scripts are given, and the project pin. Written to the pattern
# of infra/terraform/gcp and of infra/terraform/aws-kubeadm, and like them never
# planned and never applied.
locals {
  # One name for the cluster and the things named after it. It is not the
  # managed module's name (meridian-gcp-test): service account IDs and secret
  # IDs are unique in a project, and a leftover of one module must not make the
  # other fail. A test environment, one at a time.
  name = "meridian-gcp-kubeadm"

  # One zone, as the managed module has: the nodes, and so the instances, are
  # zonal. Google's page "Regions and zones" (read 2026-10-07,
  # https://docs.cloud.google.com/compute/docs/regions-zones) lists b, c and d for
  # europe-west1 (St. Ghislain) and a, b and c for each of the other ten. The map
  # holds the first zone the page lists for each Region of the list in
  # variables.tf, which is the managed module's list without europe-north1 (the
  # variable's comment says why), and it is the managed module's map without that
  # entry: a test holds the two equal. There is no fallback: a Region added to the
  # list without an entry here fails the plan at the index below (and a test),
  # where a zone built from the Region's name would fail an apply at the first
  # instance.
  zones = {
    "europe-central2"   = "europe-central2-a"
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

  # As the managed module's labels, with this module's environment. Instances,
  # the reserved address and the secret carry them; a network, a subnetwork, a
  # firewall rule and a service account take no labels in Google Cloud.
  labels = {
    project     = "meridian"
    environment = "gcp-kubeadm"
    managed-by  = "terraform"
  }

  # The operating system: the Ubuntu 24.04 LTS (noble) image family for x86 of
  # Canonical's own project. Google's page "Operating system details" (read
  # 2026-10-07, updated 2026-10-05) lists ubuntu-2404-lts-amd64 in the project
  # ubuntu-os-cloud, with Shielded VM support, and no Arm needs here. No image
  # identifier is in the repository: a family resolves to the newest image of the
  # family when an instance is created, so the image is a MOVING INPUT, as the AWS
  # module's is. The release is the fixed part.
  ubuntu_image = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"

  # The one secret that carries the join command from the control plane to the
  # workers (secret.tf).
  join_secret_id = "${local.name}-join-command"

  # The network. The nodes' range, and the pod network's range, which kubeadm is
  # given and the network plugin uses. The pod range is the plugin's own default
  # (192.168.0.0/16), so the pinned manifest is applied without a change, and it
  # does not overlap the nodes' range.
  nodes_cidr       = "10.10.0.0/24"
  pod_network_cidr = "192.168.0.0/16"

  # The control plane's INTERNAL address: the API server's address for every node
  # (the workers join there; the control plane's own kubeconfig names it). It is
  # chosen here, inside the nodes' range, and given to the instance as its own, so
  # the workers' boot scripts can be rendered with it before any instance exists.
  # The first addresses of a subnet's range are reserved by Google Cloud for the
  # network; the tenth is not among them (the page "VPC networks", from memory: no
  # page was read for the list).
  control_plane_internal_address = cidrhost(local.nodes_cidr, 10)

  # The signing key of the Kubernetes project's package repositories, the AWS
  # module's pin and its reasons (infra/terraform/aws-kubeadm/main.tf): the same
  # key for all minors, expiring on 2026-12-29. After that date an expired key
  # still has the same fingerprint, so the pin check still PASSES, and what fails
  # is `apt-get update` with apt's own signature error. The apply must come before
  # 2026-12-29, or the project's current key is read again first and put here, and
  # in the AWS module, in a committed change.
  kubernetes_apt_key_fingerprint = "DE15B14486CD377B9E876E1A234654DA9A296436"

  # 6443 is kubeadm's default API server port; the Kubernetes page "Ports and
  # Protocols" (read 2026-10-07) lists it.
  api_port = 6443
}

# The project the provider is configured for, read from Google Cloud. What a plan
# or an apply prints of it is the managed module's list (infra/terraform/gcp/
# main.tf): no variable value is printed (the variables that name the project are
# sensitive), but Google's own resource IDs name the project, Terraform prints the
# data source's own line (data.google_project.current: Read complete, with the
# project in its id), and a failed precondition prints the number of the project the
# provider reached. All of that is shown, and nothing redacts it: there is no
# wrapper here.
#
# Reading it needs the Cloud Resource Manager API, and the Service Usage API
# (which google_project_service itself needs), enabled on the project by hand
# before the first init: the resource that enables an API hangs on the pin, the
# pin on the data source, and the data source on that API.
data "google_project" "current" {}

# The APIs the module's resources need, one by one, as the managed module does.
#   compute                Compute Engine: the network, subnet, router, NAT,
#                          address, firewall rules and instances.
#   iam                    IAM: the two node service accounts.
#   secretmanager          Secret Manager: the regional secret and its bindings.
#   cloudresourcemanager   the project data source.
# disable_on_destroy is false: the project is not this module's, and switching an
# API off suspends every resource of it.
resource "google_project_service" "api" {
  for_each = toset([
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "secretmanager.googleapis.com",
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
