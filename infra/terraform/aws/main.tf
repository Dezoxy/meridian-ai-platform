locals {
  # One name for the cluster and the things named after it. IAM role names
  # are unique in an account, so a second copy of this module in the same
  # account would collide: it is a test environment, one at a time.
  name = "meridian-aws-test"

  tags = {
    project     = "meridian"
    environment = "aws-test"
    managed-by  = "terraform"
  }
}

# The caller's principal, read from the credentials in use. Nothing in the
# module prints it or writes it down. An assumed-role session ARN cannot be an
# access-entry principal, so cluster.tf takes the role behind the session from
# aws_iam_session_context (for an IAM user the ARN passes through unchanged).
data "aws_caller_identity" "current" {}

data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}
