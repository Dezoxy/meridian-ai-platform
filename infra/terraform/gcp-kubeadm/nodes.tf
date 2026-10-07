# Implemented as code, never applied (S079). One control-plane instance and
# `worker_count` workers, and the boot scripts that make them a cluster
# (templates/).
#
# No SSH key and no port 22: `block-project-ssh-keys` keeps the project's keys
# off the instances, no instance carries a key of its own, and security.tf
# admits nothing on 22. `enable-oslogin` is set to TRUE on each instance, which
# makes the instance ignore every key in metadata (the scan's GCP-0036 asks for
# it, and it narrows access; it grants none). How a person then reaches a node is
# the owner's to turn on: the IAM role that lets a person log in with OS Login and
# a firewall rule for Identity-Aware Proxy's range are not made here (the README:
# designed, not built).
#
# The instances are Shielded VMs (Secure Boot, vTPM and integrity monitoring; the
# image family supports them, "Operating system details", read 2026-10-07). Whether
# the pinned Calico and the distribution's containerd run under Secure Boot was
# not seen: they load no module the distribution's kernel does not sign (from
# memory), and an apply shows it as a node that does not boot. A node's disk is
# encrypted at rest by Google's default and the module does not choose a key.
#
# The node's service account token is reachable from the node's metadata server,
# and so from any pod on the host network (calico-node, kube-proxy and any pod
# with hostNetwork: true share the node's network stack): what the token can do is
# in iam.tf (one secret, one verb on each side), and it works from outside the
# node until it expires. A pod with its OWN network namespace reaches the metadata
# server through the node as well: unlike the AWS module's hop limit of one, a
# Compute Engine instance has no setting that stops it (from memory: no page was
# read), so the limit of what a stolen token can do is IAM's alone, and that is
# why the accounts hold so little.

locals {
  # The text both boot scripts share (installing containerd and kubeadm, the
  # strict reading of a join command, and the calls to the metadata server and
  # Secret Manager). It is rendered once and handed to each script's template.
  node_common = templatefile("${path.module}/templates/node-common.sh.tftpl", {
    kubernetes_minor                  = var.kubernetes_version
    kubernetes_apt_signer_fingerprint = local.kubernetes_apt_signer_fingerprint
  })
}

resource "google_compute_instance" "control_plane" {
  name         = "${local.name}-control-plane"
  machine_type = var.node_machine_type
  zone         = local.zone
  labels       = local.labels

  can_ip_forward      = false
  deletion_protection = false

  boot_disk {
    auto_delete = true

    initialize_params {
      image = local.ubuntu_image
      type  = "pd-balanced"
      size  = 20
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.nodes.id
    network_ip = local.control_plane_internal_address

    # The one external address of the cluster: the reserved one (network.tf).
    access_config {
      nat_ip = google_compute_address.control_plane.address
    }
  }

  # cloud-platform: the token is limited by IAM and by nothing else (iam.tf).
  service_account {
    email  = google_service_account.control_plane.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  # `user-data` is the key cloud-init reads on a Compute Engine instance: the
  # cloud-init page for its GCE data source (read 2026-10-07) says "user-data and
  # user-data-encoding can be provided to cloud-init by setting those custom
  # meta-data keys for an instance". The text is a script (it starts with `#!`),
  # sent plain: a metadata value may be 256 KB and all of an instance's entries
  # 512 KB (the page "Set custom metadata", read 2026-10-07), and a test holds the
  # script far below both. It holds no secret (the join token is made on the node,
  # after boot): user data is metadata of the instance, readable by anyone who may
  # get the instance. NO `startup-script` key: the guest agent would run it on every
  # boot as well as cloud-init once.
  #
  # A change to the script is an in-place update of the metadata, and cloud-init
  # does not run it again on an instance that has run it: the AWS module's
  # user_data_replace_on_change has no counterpart on this resource, and this module
  # does not build one. A second apply is never done (README): remove, then apply.
  metadata = {
    user-data = templatefile("${path.module}/templates/control-plane.sh.tftpl", {
      common                 = local.node_common
      location               = var.region
      secret_id              = google_secret_manager_regional_secret.join_command.secret_id
      internal_address       = local.control_plane_internal_address
      public_address         = google_compute_address.control_plane.address
      api_port               = local.api_port
      pod_network_cidr       = local.pod_network_cidr
      calico_version         = var.calico_version
      calico_manifest_sha256 = var.calico_manifest_sha256
    })
    block-project-ssh-keys = "true"
    enable-oslogin         = "TRUE"
  }

  # The node needs its network's rules, its account's one permission and its
  # workers' way out before its boot script starts. A second apply never replaces
  # a node for a new image of the family: the family moves whenever Canonical
  # publishes a build, and a replaced control plane would leave the workers a join
  # command older than it.
  lifecycle {
    ignore_changes = [boot_disk[0].initialize_params[0].image]
  }

  depends_on = [
    terraform_data.project_pin,
    google_project_service.api,
    google_compute_router_nat.main,
    google_compute_firewall.api_from_operator,
    google_compute_firewall.api_from_nodes,
    google_compute_firewall.kubelet_from_control_plane,
    google_compute_firewall.bgp_between_nodes,
    google_compute_firewall.ipip_between_nodes,
    google_secret_manager_regional_secret_iam_member.control_plane_adds_versions,
  ]
}

resource "google_compute_instance" "worker" {
  count = var.worker_count

  name         = "${local.name}-worker-${count.index + 1}"
  machine_type = var.node_machine_type
  zone         = local.zone
  labels       = local.labels

  can_ip_forward      = false
  deletion_protection = false

  boot_disk {
    auto_delete = true

    initialize_params {
      image = local.ubuntu_image
      type  = "pd-balanced"
      size  = 20
    }
  }

  # No access configuration: a worker has no external address (network.tf).
  network_interface {
    subnetwork = google_compute_subnetwork.nodes.id
  }

  service_account {
    email  = google_service_account.worker.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  # As the control plane's (the comment there says why).
  metadata = {
    user-data = templatefile("${path.module}/templates/worker.sh.tftpl", {
      common                = local.node_common
      location              = var.region
      secret_id             = google_secret_manager_regional_secret.join_command.secret_id
      control_plane_address = local.control_plane_internal_address
      api_port              = local.api_port
    })
    block-project-ssh-keys = "true"
    enable-oslogin         = "TRUE"
  }

  lifecycle {
    ignore_changes = [boot_disk[0].initialize_params[0].image]
  }

  # The NAT is a worker's only way out, and the firewall rules and its account's
  # one permission are what the boot script needs. The workers do not wait for the
  # control plane: they poll for the join command, so booting first costs them
  # nothing.
  depends_on = [
    terraform_data.project_pin,
    google_project_service.api,
    google_compute_router_nat.main,
    google_compute_firewall.api_from_nodes,
    google_compute_firewall.kubelet_from_control_plane,
    google_compute_firewall.bgp_between_nodes,
    google_compute_firewall.ipip_between_nodes,
    google_secret_manager_regional_secret_iam_member.worker_reads_the_join_command,
  ]
}
