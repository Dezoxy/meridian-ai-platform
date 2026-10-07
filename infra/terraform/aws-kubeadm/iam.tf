# Implemented as code, never applied (S079). Two instance roles: both may use
# Session Manager (the only way into a node: no key pair, no port 22); the
# control plane's may WRITE the one join parameter, the workers' may READ it with
# decryption; nothing else.

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
