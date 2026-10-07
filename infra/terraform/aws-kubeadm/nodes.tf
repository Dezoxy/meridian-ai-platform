# Implemented as code, never applied (S079). One control-plane instance and
# `worker_count` workers, an Elastic IP for the control plane, and the boot
# scripts that make them a cluster (templates/).
#
# No key pair and no port 22: the only way into a node is Session Manager. The
# metadata service is at version 2 with a hop limit of 1, so a pod (one network
# hop beyond the node) cannot use the node's role. Root volumes are encrypted
# and go with the instance.

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
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/templates/control-plane.sh.tftpl", {
    common                 = local.node_common
    region                 = var.region
    public_address         = aws_eip.control_plane.public_ip
    api_port               = local.api_port
    pod_network_cidr       = local.pod_network_cidr
    join_parameter_name    = aws_ssm_parameter.join_command.name
    calico_version         = var.calico_version
    calico_manifest_sha256 = var.calico_manifest_sha256
  })

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

  # The node needs its route to the internet and its role's permissions before
  # its boot script starts.
  depends_on = [
    aws_route_table_association.public,
    aws_iam_role_policy_attachment.control_plane_session_manager,
    aws_iam_role_policy.control_plane_write_join_command,
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

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/templates/worker.sh.tftpl", {
    common                = local.node_common
    region                = var.region
    control_plane_address = aws_eip.control_plane.public_ip
    api_port              = local.api_port
    join_parameter_name   = aws_ssm_parameter.join_command.name
  })

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

  depends_on = [
    aws_route_table_association.public,
    aws_iam_role_policy_attachment.worker_session_manager,
    aws_iam_role_policy.worker_read_join_command,
  ]
}
