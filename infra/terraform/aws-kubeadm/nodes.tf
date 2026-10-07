# Implemented as code, never applied (S079). One control-plane instance and
# `worker_count` workers, an Elastic IP for the control plane, and the boot
# scripts that make them a cluster (templates/).
#
# No key pair and no port 22: the only way into a node is Session Manager. The
# metadata service is at version 2 with a hop limit of 1, so a pod with its OWN
# network namespace (one more hop beyond the node) cannot reach it. A pod on the
# host network can: calico-node, kube-proxy and any pod with hostNetwork: true
# share the node's network stack and so can fetch the node role's credentials,
# and those work from outside the node until they expire (nothing in a policy
# ties them to the VPC or an address). iam.tf says what the role can do with
# them. Root volumes are encrypted and go with the instance.

# The image id comes from Canonical's public parameter (main.tf says which and
# why it is a moving input). The value is an image id and no secret, so the
# data source's non-sensitive attribute is used.
data "aws_ssm_parameter" "ubuntu_image" {
  name = local.ubuntu_image_parameter
}

locals {
  # The text both boot scripts share (installing containerd and kubeadm). It is
  # rendered once and handed to each script's template.
  node_common = templatefile("${path.module}/templates/node-common.sh.tftpl", {
    kubernetes_minor               = var.kubernetes_version
    kubernetes_apt_key_fingerprint = local.kubernetes_apt_key_fingerprint
  })
}

# The control plane's address is made first and attached after the instance
# starts, so the certificates can name it before the node boots, and the
# removal releases it. The internet gateway has to exist first, which the
# provider's page for aws_eip asks for.
resource "aws_eip" "control_plane" {
  domain = "vpc"

  tags = { Name = "${local.name}-control-plane" }

  depends_on = [aws_internet_gateway.main]
}

resource "aws_instance" "control_plane" {
  ami                         = data.aws_ssm_parameter.ubuntu_image.insecure_value
  instance_type               = var.node_instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.control_plane.id]
  iam_instance_profile        = aws_iam_instance_profile.control_plane.name
  associate_public_ip_address = true

  # The scripts run once per instance, so a change to them replaces the
  # instance. The text holds no secret (the join token is made on the node).
  #
  # The text goes out gzip-compressed, because the control plane's script is
  # close to the 16 KB EC2 allows for user data ("in raw form, before it is
  # base64-encoded", so the compressed bytes are the ones counted: the reading
  # of the module's authors, no API has been asked). Two pages, both read
  # 2026-10-07, say the rest. Cloud-init's "Gzip compressed content":
  # https://cloudinit.readthedocs.io/en/latest/explanation/format/gzip.html
  # says content found to be gzip compressed is uncompressed and then used as
  # if it were not compressed. Its "Headers and content types":
  # https://cloudinit.readthedocs.io/en/latest/reference/config-format-headers.html
  # says the gzip format has no header text and is identified by its magic
  # bytes, and that a user-data script is recognized by `#!` (both templates
  # start with it, so the decompressed text is read as a script). The
  # aws_instance page of the provider (documentation of v6.67.0, read the same
  # day) says gzip-encoded user data must be passed as base64 in
  # `user_data_base64` and not in `user_data`, "to avoid corruption", and that
  # `user_data_replace_on_change` applies to either.
  user_data_replace_on_change = true
  user_data_base64 = base64gzip(templatefile("${path.module}/templates/control-plane.sh.tftpl", {
    common                 = local.node_common
    region                 = var.region
    public_address         = aws_eip.control_plane.public_ip
    api_port               = local.api_port
    pod_network_cidr       = local.pod_network_cidr
    join_parameter_name    = aws_ssm_parameter.join_command.name
    calico_version         = var.calico_version
    calico_manifest_sha256 = var.calico_manifest_sha256
  }))

  # `standard` stops the t3 family's default (unlimited) from billing surplus
  # CPU credits on top of the hourly price; these nodes are idle most of the
  # hour. The provider page says T3 instances are launched as unlimited by
  # default.
  credit_specification {
    cpu_credits = "standard"
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 20
    encrypted             = true
    delete_on_termination = true
  }

  tags = { Name = "${local.name}-control-plane" }

  # A second apply is never done (README): `ami` is read from a parameter that
  # moves whenever the publisher releases a build, and a changed `ami` would
  # replace all three nodes, leaving the workers a join command older than the
  # new control plane. Ignoring it makes a second apply leave the nodes alone.
  lifecycle {
    ignore_changes = [ami]
  }

  # The node needs its route to the internet, its role's permissions and its
  # security group's rules before its boot script starts: the two egress rules
  # (nothing leaves a node without one), and the rules that admit the nodes to
  # the API server on the control plane's group, by group (the workers, and the
  # control plane's own group), by the Elastic IP (the node reaching itself) and
  # by the public address of each worker. That last rule reads the workers'
  # public addresses, so it exists only once the workers do: the control plane
  # waits for it, which makes it start after the workers are created, and a
  # worker cannot wait for it (it would wait for itself). The workers poll for
  # the join command, so they tolerate booting first, and a worker cannot join
  # before the control plane has written the command, which is after this rule.
  depends_on = [
    aws_route_table_association.public,
    aws_iam_role_policy_attachment.control_plane_session_manager,
    aws_iam_role_policy.control_plane_write_join_command,
    aws_vpc_security_group_egress_rule.control_plane_all,
    aws_vpc_security_group_egress_rule.worker_all,
    aws_vpc_security_group_ingress_rule.api_from_workers,
    aws_vpc_security_group_ingress_rule.api_from_control_plane_group,
    aws_vpc_security_group_ingress_rule.api_from_control_plane_address,
    aws_vpc_security_group_ingress_rule.api_from_worker_addresses,
  ]
}

resource "aws_eip_association" "control_plane" {
  instance_id   = aws_instance.control_plane.id
  allocation_id = aws_eip.control_plane.id
}

resource "aws_instance" "worker" {
  count = var.worker_count

  ami                         = data.aws_ssm_parameter.ubuntu_image.insecure_value
  instance_type               = var.node_instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.worker.id]
  iam_instance_profile        = aws_iam_instance_profile.worker.name
  associate_public_ip_address = true

  # Compressed, like the control plane's (the comment there says why).
  user_data_replace_on_change = true
  user_data_base64 = base64gzip(templatefile("${path.module}/templates/worker.sh.tftpl", {
    common                = local.node_common
    region                = var.region
    control_plane_address = aws_eip.control_plane.public_ip
    api_port              = local.api_port
    join_parameter_name   = aws_ssm_parameter.join_command.name
  }))

  credit_specification {
    cpu_credits = "standard"
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 20
    encrypted             = true
    delete_on_termination = true
  }

  tags = { Name = "${local.name}-worker-${count.index + 1}" }

  # As the control plane's (the comment there says why), except the rule for
  # the workers' own public addresses: it reads them from these instances.
  lifecycle {
    ignore_changes = [ami]
  }

  depends_on = [
    aws_route_table_association.public,
    aws_iam_role_policy_attachment.worker_session_manager,
    aws_iam_role_policy.worker_read_join_command,
    aws_vpc_security_group_egress_rule.control_plane_all,
    aws_vpc_security_group_egress_rule.worker_all,
    aws_vpc_security_group_ingress_rule.api_from_workers,
    aws_vpc_security_group_ingress_rule.api_from_control_plane_group,
    aws_vpc_security_group_ingress_rule.api_from_control_plane_address,
  ]
}
