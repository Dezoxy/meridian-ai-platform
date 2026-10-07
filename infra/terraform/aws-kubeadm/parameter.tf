# Implemented as code, never applied (S079). The one parameter that carries the
# join command from the control plane to the workers. The control plane's boot
# script overwrites it after `kubeadm init`; the workers' boot script reads it.
# Terraform creates it, so the removal deletes it, and Terraform never holds a
# join command.
#
# The provider refuses an empty value, so the parameter is created with a fixed
# placeholder, which is not a join command (the workers keep polling until the
# value is one).
#
# value_wo, not value. The design said `value` with ignore_changes = [value].
# The provider's source at v6.67.0 (internal/service/ssm/parameter.go) shows why
# that does not keep the join command out of the state: ignore_changes only
# hides the difference from the plan, while every refresh reads the parameter
# WITH decryption (resourceParameterRead calls findParameterByName with
# withDecryption true) and, when value_wo is not set, writes what it read into
# the state's `value`. A plan, an apply or a removal refreshes first, so the
# state would hold the join command, a token that lives one hour, in clear, and
# so would the state's backup file. A write-only argument is "never stored to
# state" (the provider's page for aws_ssm_parameter), and with it Read sets
# `value` to null. The placeholder is sent at create, and
# value_wo_version never changes, so nothing is sent again.
resource "aws_ssm_parameter" "join_command" {
  name        = local.join_parameter_name
  description = "The kubeadm join command of this cluster: a placeholder until the control plane writes it, valid for one hour after that."
  type        = "SecureString"

  value_wo         = "not-yet-written"
  value_wo_version = 1
}
