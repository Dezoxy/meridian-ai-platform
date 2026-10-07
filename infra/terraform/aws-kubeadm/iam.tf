# Implemented as code, never applied (S079), and no run has seen what either role
# can do. Two instance roles, each with the managed policy
# AmazonSSMManagedInstanceCore (below) and one inline policy of this module.
#
# What each role can and cannot do, AFTER the managed policy is counted:
#
# * Both: use Session Manager (the only way into a node: no key pair, no port
#   22) and the rest of what that managed policy allows (the list is below).
# * The control plane: WRITE the one join parameter. It can read NO parameter:
#   an explicit Deny of the four read actions on every resource beats the
#   managed policy's Allow.
# * A worker: READ the one join parameter with decryption (its own Allow is
#   redundant with the managed policy's, which the page below says allows
#   ssm:GetParameter on every resource, and stays as the statement of intent).
#   It can read NO other parameter: an explicit Deny of the four read actions on
#   every resource but the join parameter. It cannot write one.
#
# What the page says, and what only the account settles: the "AWS managed
# policy reference" page for AmazonSSMManagedInstanceCore, read 2026-10-07 at
# https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonSSMManagedInstanceCore.html
# shows version v2 as the default, edited 2019-05-23, with ssm:GetParameter and
# ssm:GetParameters on Resource "*". It lists neither ssm:GetParametersByPath
# nor ssm:GetParameterHistory: those two are denied as well in case a later
# version adds them. What the policy says TODAY in the account is settled by
# `aws iam get-policy-version` there, and that was not run. The Deny statements
# are right whatever it says.
#
# What that managed policy also allows and this module does not need (the same
# page): ssm:DescribeAssociation, ssm:ListAssociations,
# ssm:ListInstanceAssociations, ssm:UpdateAssociationStatus,
# ssm:UpdateInstanceAssociationStatus, ssm:GetDocument, ssm:DescribeDocument,
# ssm:GetManifest, ssm:GetDeployablePatchSnapshotForInstance, ssm:PutInventory,
# ssm:PutComplianceItems and ssm:PutConfigurePackageResult, all on every
# resource. The agent's own messaging (ssmmessages:* for Session Manager,
# ec2messages:* and ssm:UpdateInstanceInformation) is what a node needs to be
# reachable. It has no SendCommand, StartSession or Put*Parameter. Nothing here
# narrows the first list: a customer-managed policy with only the messaging
# actions would, and is not built.
#
# Not seen: whether the Deny breaks the SSM agent, if the agent reads a
# parameter of its own with these credentials (the page does not say why the
# managed policy grants the read). An apply shows it as a node that never
# registers with Systems Manager.

# The managed policy is read by NAME and attached by the ARN that comes back,
# never by a typed ARN (the managed module's cluster.tf says why). The name is
# the one the AWS Systems Manager page "Configure instance permissions required
# for Systems Manager" gives (read 2026-10-07). validate does not read a data
# source; a wrong name fails at plan, which is free.
data "aws_iam_policy" "session_manager" {
  name = "AmazonSSMManagedInstanceCore"
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

# The join parameter's value is encrypted under the account's AWS managed key
# (aws/ssm), because the parameter names no key. The page "AWS KMS encryption
# for AWS Systems Manager Parameter Store SecureString parameters" (read
# 2026-10-07) says kms:Encrypt and kms:Decrypt are needed and also that "you
# cannot establish access control policies for the default aws/ssm KMS key".
# These roles carry no kms: statement, on the reading that
# the AWS managed key serves any principal of the account that Parameter Store
# lets through. An apply is what shows whether that reading is right: if the
# control plane or a worker is refused, the fix is a statement or a
# customer-managed key, and a key bills and waits seven days to be removed.

resource "aws_iam_role" "control_plane" {
  name               = "${local.name}-control-plane"
  assume_role_policy = data.aws_iam_policy_document.node_trust.json
}

resource "aws_iam_role_policy_attachment" "control_plane_session_manager" {
  role       = aws_iam_role.control_plane.name
  policy_arn = data.aws_iam_policy.session_manager.arn
}

data "aws_iam_policy_document" "control_plane_write_join_command" {
  statement {
    effect    = "Allow"
    actions   = ["ssm:PutParameter"]
    resources = [aws_ssm_parameter.join_command.arn]
  }

  # It writes the join parameter and reads none (iam.tf's header says why).
  statement {
    effect = "Deny"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
      "ssm:GetParameterHistory",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "control_plane_write_join_command" {
  name   = "write-the-join-command"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.control_plane_write_join_command.json
}

resource "aws_iam_instance_profile" "control_plane" {
  name = "${local.name}-control-plane"
  role = aws_iam_role.control_plane.name
}

resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.node_trust.json
}

resource "aws_iam_role_policy_attachment" "worker_session_manager" {
  role       = aws_iam_role.worker.name
  policy_arn = data.aws_iam_policy.session_manager.arn
}

data "aws_iam_policy_document" "worker_read_join_command" {
  statement {
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.join_command.arn]
  }

  # A worker reads the join parameter and no other (iam.tf's header says why).
  statement {
    effect = "Deny"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
      "ssm:GetParameterHistory",
    ]
    not_resources = [aws_ssm_parameter.join_command.arn]
  }
}

resource "aws_iam_role_policy" "worker_read_join_command" {
  name   = "read-the-join-command"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_read_join_command.json
}

resource "aws_iam_instance_profile" "worker" {
  name = "${local.name}-worker"
  role = aws_iam_role.worker.name
}
