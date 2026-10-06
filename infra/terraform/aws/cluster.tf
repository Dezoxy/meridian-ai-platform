# The two IAM roles EKS needs before the cluster and its nodes exist (ADR 6,
# "What a Terraform module needs on AWS").

# Every AWS-managed policy this module attaches is read by NAME and attached by
# the ARN that comes back, never by a typed ARN: two AWS pages disagree on the
# path of the EBS CSI policy's ARN (with and without service-role/), and a wrong
# one fails the apply with the cluster and the database already billing. A wrong
# name fails at plan, which is free. validate does not read a data source, so
# it still needs no account.
data "aws_iam_policy" "cluster" {
  name = "AmazonEKSClusterPolicy"
}

# The cluster role's trust is sts:AssumeRole alone, as the EKS page "Amazon EKS
# cluster IAM role" shows it. (sts:TagSession was here and had no reason: it is
# for a principal that passes session tags, which the EKS service does not.)
data "aws_iam_policy_document" "cluster_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${local.name}-cluster"
  assume_role_policy = data.aws_iam_policy_document.cluster_trust.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = data.aws_iam_policy.cluster.arn
}

data "aws_iam_policy_document" "node_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${local.name}-node"
  assume_role_policy = data.aws_iam_policy_document.node_trust.json
}

# AmazonEKSWorkerNodePolicy also carries the permission the Pod Identity Agent
# needs on the node (eks-auth:AssumeRoleForPodIdentity); the ECR pull-only
# policy lets the kubelet pull images, the built-in add-ons' included.
locals {
  node_policy_names = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEC2ContainerRegistryPullOnly",
    # Not in the ADR's list. The VPC CNI add-on runs as the node's identity
    # unless it is given a role of its own through IRSA or Pod Identity, and
    # then needs this policy on the node role (the EKS page "Amazon EKS node
    # IAM role"); without it the nodes stay NotReady, which validate cannot
    # see. AWS recommends a separate role for the CNI instead: production
    # does that and takes this line out.
    "AmazonEKS_CNI_Policy",
  ])
}

data "aws_iam_policy" "node" {
  for_each = local.node_policy_names

  name = each.value
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = local.node_policy_names

  role       = aws_iam_role.node.name
  policy_arn = data.aws_iam_policy.node[each.value].arn
}

resource "aws_eks_cluster" "main" {
  name     = local.name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  # Access entries only, no aws-auth ConfigMap. The creator is NOT made admin
  # implicitly: the entry for the applying principal is written out below, so
  # that who can reach the cluster is in the module and no entry collides with
  # a second one made by EKS.
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  # STANDARD: upgraded when standard support ends, never moved into extended
  # support, which costs six times as much (USD 0.60 an hour against 0.10).
  upgrade_policy {
    support_type = "STANDARD"
  }

  vpc_config {
    subnet_ids = aws_subnet.public[*].id

    # BOTH are on, and only the public side is restricted. Nodes sit in public
    # subnets and, with the private endpoint off, would reach the API server
    # through the public one and be refused by the address list below; with the
    # private endpoint on, they stay inside the VPC. The address list governs
    # the public side only, so it holds the applying machine and nothing else.
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = [var.api_access_cidr]
  }

  # Test environment: made to be removed. Production sets deletion_protection
  # to true.
  deletion_protection = false

  # A production cluster also turns on control-plane logging
  # (enabled_cluster_log_types) and envelope encryption of Secrets with a
  # customer-managed KMS key. Neither is here: the log group would outlive the
  # removal and bill, and a KMS key waits at least seven days to be removed.
  # The cluster API data and its Secrets are not used by anything in this
  # module.

  # The roles' permissions must exist before the cluster and outlive it, or EKS
  # cannot remove the security groups it makes (the provider's page).
  depends_on = [aws_iam_role_policy_attachment.cluster]
}

# The applying principal is the cluster's only administrator. A production
# cluster maps a role or group per team (and the pipeline's role) with
# narrower policies.
resource "aws_eks_access_entry" "admin" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = data.aws_iam_session_context.current.issuer_arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "admin" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_eks_access_entry.admin.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}

resource "aws_eks_node_group" "main" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "main"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.public[*].id

  instance_types = [var.node_instance_type]
  capacity_type  = "ON_DEMAND"
  # x86_64 to match the t3 family. An Arm instance type (t4g, m7g) needs
  # AL2023_ARM_64_STANDARD and an arm64 image.
  ami_type  = "AL2023_x86_64_STANDARD"
  disk_size = 20

  scaling_config {
    desired_size = var.node_count
    min_size     = var.node_count
    max_size     = var.node_count
  }

  update_config {
    max_unavailable = 1
  }

  # The node role's permissions must exist before the nodes and outlive them,
  # or EKS cannot remove the instances and network interfaces (the provider's
  # page).
  depends_on = [aws_iam_role_policy_attachment.node]
}

# Pod Identity: the agent runs on every node and hands a pod the credentials of
# the role its service account is associated with (identity.tf). No OIDC
# provider and no annotation on the service account are needed.
resource "aws_eks_addon" "pod_identity_agent" {
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "eks-pod-identity-agent"

  # The agent is a DaemonSet: with no node it never becomes active.
  depends_on = [aws_eks_node_group.main]
}

# The EBS CSI driver's own role is given through Pod Identity, the way the
# provider's aws_eks_addon page documents it (the pod_identity_association
# block: a role and a service account). The AWS page for the driver describes
# the IRSA way, which needs an OIDC provider and is not used here. The service
# account name, ebs-csi-controller-sa, is from that AWS page ("Amazon EBS CSI
# driver").
# The role trusts the Pod Identity service for ONE service account of ONE
# cluster. The EKS page "Create IAM role with trust policy required by EKS Pod
# Identity" says: "You can use these tags in the condition keys in the trust
# policy to restrict which service accounts, namespaces, and clusters can use
# this role", and shows the condition "aws:RequestTag/kubernetes-namespace" and
# "aws:RequestTag/kubernetes-service-account" under "StringEquals". The page's
# example has no cluster condition, but the list of session tags it points to
# ("Grant Pods access to AWS resources based on tags") includes
# eks-cluster-name, so the condition for the cluster's own name is added too:
# without it any cluster of the account that has a service account of that name
# in that namespace could take the role. It names the cluster by the literal
# name, not by an attribute of the cluster, so the role does not depend on the
# cluster. Whether Pod Identity then hands a pod these credentials is something
# only an apply shows: the second half checks it (README, the checklist).
# sts:TagSession stays: the page lists it with sts:AssumeRole in the trust
# policy, because Pod Identity assumes the role with session tags.
data "aws_iam_policy_document" "ebs_csi_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-namespace"
      values   = ["kube-system"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-service-account"
      values   = ["ebs-csi-controller-sa"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks-cluster-name"
      values   = [local.name]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${local.name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_trust.json
}

# The V2 managed policy is the one the AWS page names today; its predecessor,
# AmazonEBSCSIDriverPolicy, is being migrated away from. Read by name (see the
# top of this file).
data "aws_iam_policy" "ebs_csi" {
  name = "AmazonEBSCSIDriverPolicyV2"
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = data.aws_iam_policy.ebs_csi.arn
}

# Volumes for the chart's PersistentVolumeClaims (the database on kind is
# replaced by RDS here, but Loki, Tempo and the rest still want volumes). The
# module installs nothing into the cluster, so no volume exists through it:
# one appears only if something installed later asks the driver for it, and
# that volume is not in this state. When that happens, delete the claims before
# the removal (the README's removal section says in what order), or the volume
# is left behind and bills.
resource "aws_eks_addon" "ebs_csi" {
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "aws-ebs-csi-driver"

  pod_identity_association {
    role_arn        = aws_iam_role.ebs_csi.arn
    service_account = "ebs-csi-controller-sa"
  }

  # The controller needs a node to land on, and the association is only
  # honoured when the agent is running.
  depends_on = [
    aws_eks_node_group.main,
    aws_eks_addon.pod_identity_agent,
    aws_iam_role_policy_attachment.ebs_csi,
  ]
}
