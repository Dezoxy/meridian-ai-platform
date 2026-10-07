# Implemented as code, never applied (S079). Two security groups, one for the
# control plane and one for the workers, and every rule is written out. There
# is no rule for port 22: nobody logs in over the network, a person reaches a
# node through Systems Manager Session Manager (iam.tf).
#
# What a port is for, and where it is read:
#
# * 6443/tcp, the API server. The Kubernetes page "Ports and Protocols" (read
#   2026-10-07, last modified 2024-09-18) lists it as "Kubernetes API server,
#   used by All". It is admitted from the applying machine's one address and
#   from the workers.
# * 10250/tcp, the kubelet API, "Used by: Self, Control plane": the API server
#   reaches each worker's kubelet (logs, exec). It is admitted on the workers
#   from the control plane. The control plane's own kubelet is reached from the
#   same machine, which needs no rule.
# * 179/tcp (BGP) and IP-in-IP (IP protocol number 4), between all nodes. The
#   Calico page "System requirements" (v3.32, read 2026-10-07) lists them under
#   "Network requirements" for "Calico networking (BGP)" and "Calico networking
#   with IP-in-IP enabled (default)". That the pinned manifest has the BGP
#   backend (calico_backend "bird") and CALICO_IPV4POOL_IPIP "Always" is the
#   documentation's default: the manifest's own lines were not read.
#
# What is NOT opened, and why: etcd (2379-2380/tcp), kube-scheduler (10259) and
# kube-controller-manager (10257) are "Used by: Self" on the same page, and
# with one control-plane node nothing else talks to them. NodePort services
# (30000-32767) are not used by anything this module installs, and the page
# lists them as "Used by: All": a person who later exposes one opens it
# knowingly. Typha (5473/tcp) is off in the manifest's defaults
# (typha_service_name "none"; not read in the manifest).

resource "aws_security_group" "control_plane" {
  name        = "${local.name}-control-plane"
  description = "The control plane: the API server from one address and the workers, and the network plugin between the nodes."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-control-plane" }
}

resource "aws_security_group" "worker" {
  name        = "${local.name}-worker"
  description = "The workers: the kubelet API from the control plane, and the network plugin between the nodes."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-worker" }
}

# ---- the API server ---------------------------------------------------------

resource "aws_vpc_security_group_ingress_rule" "api_from_operator" {
  security_group_id = aws_security_group.control_plane.id
  cidr_ipv4         = var.api_access_cidr
  ip_protocol       = "tcp"
  from_port         = local.api_port
  to_port           = local.api_port
  description       = "The API server from the one address in api_access_cidr."
}

resource "aws_vpc_security_group_ingress_rule" "api_from_workers" {
  security_group_id            = aws_security_group.control_plane.id
  referenced_security_group_id = aws_security_group.worker.id
  ip_protocol                  = "tcp"
  from_port                    = local.api_port
  to_port                      = local.api_port
  description                  = "The API server from the security group of the workers (their private addresses)."
}

# A hedge, not a rule the design needs: the control plane reaches its own API
# server through the Elastic IP (the public-address rules below admit that), and
# if AWS keeps the node's private source address on a packet sent to its own
# Elastic IP, only a reference to the node's own group would match. This opens
# nothing the public-address rules do not already admit (the same port, from the
# same node) and it holds in either case. An apply shows only whether the first
# boot got through, not which rule admitted the packet (there are no flow logs
# and no audit policy); learning that would cost another apply with a rule
# removed. The workers' group needs no twin of it: api_from_workers above is
# that reference for the workers.
resource "aws_vpc_security_group_ingress_rule" "api_from_control_plane_group" {
  security_group_id            = aws_security_group.control_plane.id
  referenced_security_group_id = aws_security_group.control_plane.id
  ip_protocol                  = "tcp"
  from_port                    = local.api_port
  to_port                      = local.api_port
  description                  = "The API server from the security group of the control plane itself, in case the private address of the node is the source (a hedge)."
}

# The API server's address in the certificates, the kubeconfigs and the join
# command is the Elastic IP. A worker that connects to it connects to a public
# address. The session's understanding, from general knowledge and NOT from an
# AWS page it read, is that a packet from one instance to another's public
# address goes out through the internet gateway with the sender's public
# address as its source, so a rule that names the workers' security group would
# not match it. Each worker's public address is therefore admitted as well. An
# apply shows whether the rule above would have been enough alone; this one
# costs nothing and opens the port to no one else. Terraform knows a worker's
# address once the instance exists.
resource "aws_vpc_security_group_ingress_rule" "api_from_worker_addresses" {
  count = var.worker_count

  security_group_id = aws_security_group.control_plane.id
  cidr_ipv4         = "${aws_instance.worker[count.index].public_ip}/32"
  ip_protocol       = "tcp"
  from_port         = local.api_port
  to_port           = local.api_port
  description       = "The API server from the public address of one worker, which is how a worker reaches the Elastic IP."
}

# The same applies to the control plane itself: its admin kubeconfig, its own
# kubelet and `kubectl apply` of the network plugin all name the control-plane
# endpoint, which is the Elastic IP, so the node reaches its own API server
# through its public address. The session's understanding is that kubeadm writes
# the endpoint it was given into every kubeconfig; the Kubernetes page "kubeadm
# init" (read 2026-10-07) describes the flag only as "a stable IP address or
# DNS name for the control plane". Without this rule the first apply would
# stop at the network plugin.
resource "aws_vpc_security_group_ingress_rule" "api_from_control_plane_address" {
  security_group_id = aws_security_group.control_plane.id
  cidr_ipv4         = "${aws_eip.control_plane.public_ip}/32"
  ip_protocol       = "tcp"
  from_port         = local.api_port
  to_port           = local.api_port
  description       = "The API server from the Elastic IP of the control plane itself, which is how the node reaches itself."
}

# ---- the kubelet ------------------------------------------------------------

resource "aws_vpc_security_group_ingress_rule" "kubelet_from_control_plane" {
  security_group_id            = aws_security_group.worker.id
  referenced_security_group_id = aws_security_group.control_plane.id
  ip_protocol                  = "tcp"
  from_port                    = 10250
  to_port                      = 10250
  description                  = "The kubelet API from the control plane."
}

# ---- the network plugin, in both directions between every pair of nodes -----

resource "aws_vpc_security_group_ingress_rule" "bgp_control_plane_from_workers" {
  security_group_id            = aws_security_group.control_plane.id
  referenced_security_group_id = aws_security_group.worker.id
  ip_protocol                  = "tcp"
  from_port                    = 179
  to_port                      = 179
  description                  = "Calico BGP from the workers."
}

resource "aws_vpc_security_group_ingress_rule" "bgp_workers_from_control_plane" {
  security_group_id            = aws_security_group.worker.id
  referenced_security_group_id = aws_security_group.control_plane.id
  ip_protocol                  = "tcp"
  from_port                    = 179
  to_port                      = 179
  description                  = "Calico BGP from the control plane."
}

resource "aws_vpc_security_group_ingress_rule" "bgp_workers_from_workers" {
  security_group_id            = aws_security_group.worker.id
  referenced_security_group_id = aws_security_group.worker.id
  ip_protocol                  = "tcp"
  from_port                    = 179
  to_port                      = 179
  description                  = "Calico BGP between the workers."
}

# IP protocol number 4 is IP-in-IP: no port. The provider takes the number as a
# string.
resource "aws_vpc_security_group_ingress_rule" "ipip_control_plane_from_workers" {
  security_group_id            = aws_security_group.control_plane.id
  referenced_security_group_id = aws_security_group.worker.id
  ip_protocol                  = "4"
  description                  = "Calico IP-in-IP from the workers."
}

resource "aws_vpc_security_group_ingress_rule" "ipip_workers_from_control_plane" {
  security_group_id            = aws_security_group.worker.id
  referenced_security_group_id = aws_security_group.control_plane.id
  ip_protocol                  = "4"
  description                  = "Calico IP-in-IP from the control plane."
}

resource "aws_vpc_security_group_ingress_rule" "ipip_workers_from_workers" {
  security_group_id            = aws_security_group.worker.id
  referenced_security_group_id = aws_security_group.worker.id
  ip_protocol                  = "4"
  description                  = "Calico IP-in-IP between the workers."
}

# ---- egress -----------------------------------------------------------------

# Everything out, every port and protocol. The reason, as far as it is true:
# the nodes reach the Ubuntu and Kubernetes package repositories, GitHub for the
# plugin's manifest, the plugin's and the Kubernetes images' registries, the
# snap store and the Systems Manager and Parameter Store endpoints, and NONE of
# them has a fixed address the module can name, so egress cannot be limited by
# destination. It could be limited by port and protocol (tcp 80 and 443, DNS,
# and the node-to-node rules above) and is NOT: a port closed by mistake stops
# a boot that is only seen at the one paid apply, and costs more than an hour of
# open egress from nodes that hold nothing of value for that hour (the cluster's
# own CA and admin certificates and the role credentials of iam.tf). What open
# egress exposes is data sent out, or a command channel in, from a node or a pod
# that someone has taken over. The scan's AWS-0104 fires on any 0.0.0.0/0 egress
# whatever the ports, so narrowing them would not remove it. The managed module
# has no security group of its own for its nodes (EKS makes one), so this
# decision is this module's. Production: private subnets, a NAT gateway or VPC
# endpoints, and egress limited to those.
resource "aws_vpc_security_group_egress_rule" "control_plane_all" {
  security_group_id = aws_security_group.control_plane.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "Everything out: packages, manifests, registries and the AWS APIs."
}

resource "aws_vpc_security_group_egress_rule" "worker_all" {
  security_group_id = aws_security_group.worker.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "Everything out: packages, registries, the AWS APIs and the control plane."
}
