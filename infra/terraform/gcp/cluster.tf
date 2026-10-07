# One GKE Standard cluster, zonal, with one node pool. Declared, never planned,
# never applied, so none of what follows has met GKE. ADR 7 chose Standard over
# Autopilot (the chart's NetworkPolicies need the dataplane chosen at creation,
# the node log agent and cert-manager run as on kind) and a zonal control plane
# (the free-tier credit covers a zonal cluster, not a regional one).

# The nodes' own service account. By default GKE uses the Compute Engine default
# service account, which is broad; Google's best practice is a custom one with
# at least roles/container.defaultNodeServiceAccount (ADR 7, item 2), and that
# is the one role granted. The role that pulls from Artifact Registry is not
# verified (ADR 7, question 16): whether this one covers it, only an apply shows.
resource "google_service_account" "node" {
  account_id   = "${local.name}-node"
  display_name = "Meridian GKE nodes (${local.name})"

  depends_on = [terraform_data.project_pin, google_project_service.api]
}

# _iam_member adds one member to one role and removes nothing that is granted
# elsewhere (ADR 7, row 6: _iam_policy replaces the whole policy and
# _iam_binding owns the whole role).
resource "google_project_iam_member" "node" {
  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount"
  member  = google_service_account.node.member

  depends_on = [terraform_data.project_pin]
}

resource "google_container_cluster" "main" {
  name     = local.name
  location = local.zone

  network    = google_compute_network.main.id
  subnetwork = google_compute_subnetwork.nodes.id

  # The node pool below is the cluster's only one: the default pool GKE makes
  # with the cluster is removed at once.
  remove_default_node_pool = true
  initial_node_count       = 1

  # Fixed at creation (ADR 7): Dataplane V2, which enforces the chart's
  # NetworkPolicies, workload identity and the Gateway API's configuration. A
  # change to any of them is a new cluster. network_policy is not set beside
  # Dataplane V2: whether GKE accepts both is not seen.
  datapath_provider = "ADVANCED_DATAPATH"

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  gateway_api_config {
    channel = "CHANNEL_STANDARD"
  }

  # ADR 7 names no release channel and no version. REGULAR is Google's default
  # channel; no minimum version is set, so the cluster starts on what the
  # channel offers on the day of an apply.
  release_channel {
    channel = "REGULAR"
  }

  enable_shielded_nodes = true

  resource_labels = local.labels

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  # Private nodes (no external address); the control plane keeps its public
  # endpoint, which only the one range below may reach.
  private_cluster_config {
    enable_private_nodes = true
  }

  # The control plane is reachable from one address range, the owner's, and from
  # no Google Cloud address: gcp_public_cidrs_access_enabled is the setting
  # that lets any Compute Engine public address in. The range is a variable,
  # sensitive, with no default.
  master_authorized_networks_config {
    gcp_public_cidrs_access_enabled = false

    cidr_blocks {
      cidr_block   = var.api_access_cidr
      display_name = "operator"
    }
  }

  # A test environment made to be removed: Terraform may delete the cluster.
  # Production sets true. The provider's flag protects against deletion by
  # Terraform only (ADR 7, row 12).
  deletion_protection = false

  depends_on = [terraform_data.project_pin, google_project_service.api]
}

resource "google_container_node_pool" "main" {
  name       = "main"
  cluster    = google_container_cluster.main.id
  location   = local.zone
  node_count = var.node_count

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.node_machine_type
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.node.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    # The GKE metadata server on every node exchanges a Pod's Kubernetes token
    # for a Google credential: workload identity (ADR 7, row 16).
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }

  # The node service account has its role before a node starts.
  depends_on = [terraform_data.project_pin, google_project_iam_member.node]
}
