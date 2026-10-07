# One VPC with one regional subnet, and Cloud NAT for the nodes. Declared, never
# planned, never applied (README.md).
#
# Every resource names terraform_data.project_pin in depends_on (main.tf) so
# that a plan against the wrong project stops before it proposes anything. The
# network also waits for the APIs (main.tf); the rest of this file hangs on the
# network, and so on them.
resource "google_compute_network" "main" {
  name                    = local.name
  auto_create_subnetworks = false

  depends_on = [terraform_data.project_pin, google_project_service.api]
}

# The nodes' range, and the two secondary ranges a VPC-native cluster takes its
# Pod and Service addresses from. Private Google Access lets the nodes, which
# have no external address, reach Google's APIs without Cloud NAT.
resource "google_compute_subnetwork" "nodes" {
  name                     = "${local.name}-nodes"
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = "10.10.0.0/24"
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.20.0.0/16"
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.30.0.0/20"
  }

  depends_on = [terraform_data.project_pin]
}

# The nodes are private, so Cloud NAT is how they reach the internet (images
# from other registries, Helm charts): ADR 7's cost sketch assumes the same, at
# about USD 0.008 an hour. Two external addresses would cost a little more per
# hour and have no per-GiB charge, and would give every node an address.
resource "google_compute_router" "main" {
  name    = local.name
  region  = var.region
  network = google_compute_network.main.id

  depends_on = [terraform_data.project_pin]
}

resource "google_compute_router_nat" "main" {
  name                               = local.name
  region                             = var.region
  router                             = google_compute_router.main.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  depends_on = [terraform_data.project_pin]
}
