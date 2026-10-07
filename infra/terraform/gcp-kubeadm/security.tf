# Implemented as code, never applied (S079). Five firewall rules, every one an
# INGRESS rule, and every one TARGETED at the nodes by their service accounts
# (iam.tf), not by network tags: a tag can be set by anyone who may edit an
# instance, a service account is the identity the instance runs as. Who the rule
# admits is named by ADDRESS: the owner's /32, the control plane's own internal
# address (the kubelet rule) or the nodes' subnet (the rest). The rules could
# name their sources by service account as well (source_service_accounts), and
# the scan reads such a rule as open to every address (GCP-0027, critical, on the
# first run of the module: it takes a rule with no source range for one that
# admits 0.0.0.0/0, whatever its service accounts), which it is not. The subnet is
# this module's own, in a network of its own, and holds the cluster's nodes and
# nothing else: an instance somebody adds to it would be admitted to the nodes by
# the rules that name the subnet, which a source by service account would not do.
# There is no rule for port 22: nobody logs in over the network, and how a person
# reaches a node is the owner's to turn on (README: OS Login or Identity-Aware
# Proxy, designed, not built).
#
# What the network does with no rule: Google's page "VPC firewall rules" (read
# 2026-10-07, updated 2026-10-05) says a VPC has an implied rule that denies all
# ingress and one that allows all egress, and the network of this module
# (network.tf) is its own, not the project's default network whose pre-populated
# rules allow SSH and RDP from anywhere. So the five rules below are the whole of
# what can reach a node, and EGRESS IS OPEN by the implied rule: there is no egress
# rule in this module to read, and so none for the scan to flag. The reason is the
# AWS module's (infra/terraform/aws-kubeadm/security.tf): none of the package
# repositories, GitHub, the registries or Google's APIs has an address the module
# can name, and a port closed by mistake stops a boot that is first seen at the one
# paid apply, which costs more than an hour of open egress from nodes that hold
# nothing of value for that hour (the cluster's own CA and admin certificates and
# the node service accounts' tokens). What open egress exposes is data sent out, or
# a command channel in, from a node or a pod that someone has taken over.
# Production: a deny-all egress rule and allow rules for the destinations.
#
# What a port is for, and where it is read (the AWS module's list, the same pages,
# read 2026-10-07):
#
# * 6443/tcp, the API server ("Ports and Protocols" lists it as "Kubernetes API
#   server, used by All"). From the applying machine's one address, and from the
#   nodes (the workers join at it).
# * 10250/tcp, the kubelet API, "Used by: Self, Control plane": the API server
#   reaches each worker's kubelet. Admitted on the workers from the control plane.
# * 179/tcp (BGP) and IP-in-IP (IP protocol number 4), between all nodes (the
#   Calico page "System requirements" for v3.32 lists them for "Calico networking
#   (BGP)" and "Calico networking with IP-in-IP enabled (default)"; that the pinned
#   manifest has those defaults was not read in the manifest). The provider's page
#   for google_compute_firewall (v8.6.0, read 2026-10-07) lists `ipip` among the
#   protocol strings and says the IP protocol number is accepted; the number is
#   written here, as the AWS module writes it. Whether the network passes
#   IP-in-IP between the instances is not read: an apply shows it.
#
# NOT opened, and why: etcd (2379-2380), kube-scheduler (10259) and kube-controller-
# manager (10257) are "Used by: Self" and with one control-plane node nothing else
# talks to them; NodePort services (30000-32767) are used by nothing this module
# installs. A node reaching its OWN address needs no rule (the packet does not leave
# the machine), which is why the control plane's own kubeconfig, at its internal
# address, needs none.

resource "google_compute_firewall" "api_from_operator" {
  name        = "${local.name}-api-from-operator"
  network     = google_compute_network.main.name
  direction   = "INGRESS"
  description = "The API server from the one address in api_access_cidr."

  source_ranges           = [var.api_access_cidr]
  target_service_accounts = [google_service_account.control_plane.email]

  allow {
    protocol = "tcp"
    ports    = [tostring(local.api_port)]
  }

  depends_on = [terraform_data.project_pin]
}

resource "google_compute_firewall" "api_from_nodes" {
  name        = "${local.name}-api-from-nodes"
  network     = google_compute_network.main.name
  direction   = "INGRESS"
  description = "The API server from the nodes: the workers join and report at it."

  source_ranges           = [local.nodes_cidr]
  target_service_accounts = [google_service_account.control_plane.email]

  allow {
    protocol = "tcp"
    ports    = [tostring(local.api_port)]
  }

  depends_on = [terraform_data.project_pin]
}

resource "google_compute_firewall" "kubelet_from_control_plane" {
  name        = "${local.name}-kubelet-from-control-plane"
  network     = google_compute_network.main.name
  direction   = "INGRESS"
  description = "The kubelet API of a worker from the control plane."

  source_ranges           = ["${local.control_plane_internal_address}/32"]
  target_service_accounts = [google_service_account.worker.email]

  allow {
    protocol = "tcp"
    ports    = ["10250"]
  }

  depends_on = [terraform_data.project_pin]
}

# The network plugin, between every pair of nodes: one rule for each protocol, the
# nodes on both sides (a rule admits traffic from a source to a target, and here the
# two sets are the same).
resource "google_compute_firewall" "bgp_between_nodes" {
  name        = "${local.name}-bgp-between-nodes"
  network     = google_compute_network.main.name
  direction   = "INGRESS"
  description = "Calico BGP between the nodes."

  source_ranges           = [local.nodes_cidr]
  target_service_accounts = [google_service_account.control_plane.email, google_service_account.worker.email]

  allow {
    protocol = "tcp"
    ports    = ["179"]
  }

  depends_on = [terraform_data.project_pin]
}

# IP protocol number 4 is IP-in-IP: no port.
resource "google_compute_firewall" "ipip_between_nodes" {
  name        = "${local.name}-ipip-between-nodes"
  network     = google_compute_network.main.name
  direction   = "INGRESS"
  description = "Calico IP-in-IP between the nodes."

  source_ranges           = [local.nodes_cidr]
  target_service_accounts = [google_service_account.control_plane.email, google_service_account.worker.email]

  allow {
    protocol = "4"
  }

  depends_on = [terraform_data.project_pin]
}
