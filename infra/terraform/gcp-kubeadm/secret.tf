# Implemented as code, never applied (S079). The one secret that carries the join
# command from the control plane to the workers. The control plane's boot script
# adds a version to it after `kubeadm init`; the workers' boot script reads the
# newest version. Terraform creates the secret, so the removal deletes it and its
# versions, and Terraform never holds a join command.
#
# NO VERSION, and so no placeholder and no write-only argument. The AWS module's
# parameter has to be created with a value (the provider refuses an empty one), and
# a refresh would then read the join command into the state, which is why that
# module writes its placeholder as a write-only argument. A Secret Manager secret
# is a container: it exists with no version, a read of its newest version fails
# until one is added, and the workers' poll treats a failed read as "not yet". This
# module has no resource that could hold a version, so the state cannot either.
#
# A regional secret, as the managed module's: its data stays in the one Region, at
# rest, in use and in transit (ADR 7, row 5), and the boot scripts use the
# Region's own endpoint (node-common.sh.tftpl). The Region list excludes the one
# EU Region that has none (variables.tf). A regional secret has no purge
# protection and no replication to choose.
resource "google_secret_manager_regional_secret" "join_command" {
  secret_id = local.join_secret_id
  location  = var.region
  labels    = local.labels

  depends_on = [terraform_data.project_pin, google_project_service.api]
}
