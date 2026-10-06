# Workload identity to a secret store: the AWS counterpart of the Azure Key
# Vault and the workload identity that may read it (ADR 6, the rows for the
# vault and for AKS Workload Identity). The module writes no secret value:
# the secret exists, empty, so that the role and the policy have something to
# name, and nothing in the repository reads it yet.

resource "aws_secretsmanager_secret" "workload" {
  name        = "${local.name}/${var.workload_service_account}"
  description = "Empty secret that the ${var.workload_namespace}/${var.workload_service_account} service account may read. The module writes no value."

  # Zero: the secret is deleted at once, not scheduled. The default is 30 days
  # (7 at the least), during which the name is still taken and the secret still
  # bills; a removal that is meant to be complete cannot wait. Production keeps
  # the default so that a wrongly deleted secret can be recovered.
  recovery_window_in_days = 0
}

# Trusted for the one service account the association below names, in this
# cluster, and no other (the conditions and the AWS page they come from are
# explained above the EBS CSI role's trust document in cluster.tf).
data "aws_iam_policy_document" "workload_trust" {
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
      values   = [var.workload_namespace]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-service-account"
      values   = [var.workload_service_account]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks-cluster-name"
      values   = [local.name]
    }
  }
}

resource "aws_iam_role" "workload" {
  name               = "${local.name}-workload"
  assume_role_policy = data.aws_iam_policy_document.workload_trust.json
}

# Read this one secret and nothing else. DescribeSecret is a read of the same
# secret's metadata, which the usual clients (the Secrets Store CSI driver,
# External Secrets) call first.
data "aws_iam_policy_document" "workload_read_secret" {
  statement {
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_secretsmanager_secret.workload.arn]
  }
}

resource "aws_iam_role_policy" "workload_read_secret" {
  name   = "read-the-workload-secret"
  role   = aws_iam_role.workload.id
  policy = data.aws_iam_policy_document.workload_read_secret.json
}

# One service account in one namespace gets the role's credentials from the
# Pod Identity Agent. No OIDC provider, no annotation on the service account.
# Nothing in this module creates the namespace or the service account (nothing
# is installed into the cluster); the association can exist before them.
resource "aws_eks_pod_identity_association" "workload" {
  cluster_name    = aws_eks_cluster.main.name
  namespace       = var.workload_namespace
  service_account = var.workload_service_account
  role_arn        = aws_iam_role.workload.arn

  depends_on = [aws_eks_addon.pod_identity_agent]
}
