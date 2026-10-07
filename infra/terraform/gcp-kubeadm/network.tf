# Implemented as code, never applied (S079). One VPC with ONE subnet, Cloud NAT
# for the workers, and the control plane's one reserved public address. The
# firewall rules are in security.tf.
#
# Every resource names terraform_data.project_pin in depends_on (main.tf) so that
# a plan against the wrong project stops before it proposes anything. The network
# also waits for the APIs (main.tf); the rest of this file hangs on the network,
# and so on them.
#
# WHAT THE AWS MODULE DOES AND WHAT THIS ONE DOES. The AWS module has no NAT: every
# node has a public address (it bills by the hour, and a NAT gateway survives a
# removal that stops half way), the control plane's is an Elastic IP attached after
# the instance starts, and the workers reach the API server through that public
# address, which makes the security-group rules for the nodes' own public
# addresses necessary. Here only the control plane has an external address, and
# it is the one the design needs: the one address the firewall admits (the
# owner's) reaches the API server on it. It is a reserved address, attached
# when the instance is created, so its value is known before any node boots and
# goes into the API server's certificate. The workers have NO external address:
# nothing from the internet can address them at all, which a firewall rule alone
# only promises, and they reach the package repositories, GitHub and the image
# registries through Cloud NAT. The workers join at the control plane's INTERNAL
# address, which is chosen in main.tf and given to the instance as its own, so no
# node ever reaches another through a public address: there is no hairpin to hedge
# and no rule for a node's own public address. The cost of that choice is the
# router and the NAT (two more resources that bill by the hour and that the removal
# deletes with the rest); the cheaper way, an address on every node, was not taken
# because it gives three machines an address the design does not need.
#
# A production cluster puts every node in private subnets, reaches the API server
# through a load balancer or Identity-Aware Proxy, and sends flow logs somewhere.

resource "google_compute_network" "main" {
  name                    = local.name
  auto_create_subnetworks = false

  depends_on = [terraform_data.project_pin, google_project_service.api]
}

# The nodes' range. Private Google Access lets a node with no external address
# reach Google's APIs (Secret Manager, and the metadata server is link-local) and
# not only through the NAT. There are no secondary ranges: the pod network is the
# plugin's own overlay (main.tf), not a VPC-native one.
resource "google_compute_subnetwork" "nodes" {
  name                     = "${local.name}-nodes"
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = local.nodes_cidr
  private_ip_google_access = true

  depends_on = [terraform_data.project_pin]
}

# The workers' way to the internet. The control plane has an external address of
# its own and does not use it.
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

# The API server's public address, reserved before the instance so that the
# certificate can name it, and released by the removal. An EXTERNAL regional
# address; the instance's access configuration names it (nodes.tf).
resource "google_compute_address" "control_plane" {
  name         = "${local.name}-control-plane"
  region       = var.region
  address_type = "EXTERNAL"
  labels       = local.labels

  depends_on = [terraform_data.project_pin, google_project_service.api]
}
