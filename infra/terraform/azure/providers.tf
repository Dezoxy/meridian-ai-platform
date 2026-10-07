# The subscription is not in code: it comes from ARM_SUBSCRIPTION_ID. main.tf
# then refuses a subscription that holds no foundation of this project.
provider "azurerm" {
  # state.sh registers the resource providers the foundation uses, and this
  # module registers none of its own, so a plan changes nothing in Azure on its
  # own account. A resource provider this module needs and the subscription lacks
  # fails at the first apply, and no plan can see it.
  resource_provider_registrations = "none"

  features {
    key_vault {
      # The module writes a secret into the foundation's vault (the database
      # administrator's, in a later change), and that vault has purge protection
      # on: a purge on removal would fail, and the removal with it. A secret that
      # is soft-deleted under the same name is recovered instead of colliding.
      purge_soft_deleted_secrets_on_destroy = false
      recover_soft_deleted_secrets          = true
    }

    log_analytics_workspace {
      # The workspace is removed with the group after a demo day. By default the
      # provider only soft-deletes it: Azure keeps it, its data and its name for
      # 14 days, and a workspace created under the same name, group and region
      # inside that time is the OLD one given back, the previous day's audit log
      # in it. True purges it on removal, so every demo day starts empty and the
      # name is free at once. The price is that the previous day's control-plane
      # audit log is gone when the environment is.
      permanently_delete_on_destroy = true
    }

    resource_group {
      # Unlike the foundation's group, this module's group IS deleted, with
      # everything in it, when the environment is removed after a demo day. True
      # is the safer value: the provider then refuses to delete a group that
      # still holds a resource Terraform does not manage (one that Azure or a
      # person added by hand), and a removal that stops there leaves that
      # resource, which may bill, in front of a person, where false would delete
      # it unseen. Terraform removes every resource it manages before the group,
      # so true blocks nothing else; the cost is a removal that has to be
      # finished by hand when something unmanaged is left.
      prevent_deletion_if_contains_resources = true
    }
  }
}
