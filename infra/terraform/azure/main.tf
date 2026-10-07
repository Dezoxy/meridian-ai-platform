# The Azure platform module (S020): the environment of one demo day, made to be
# created and removed with its own state, beside the persistent foundation
# (infra/terraform/foundation), which this module reads and never changes. Written
# so far: the names, the pin, the resource group and the network. It has never
# been planned and never applied (README.md).
#
# The foundation is read by data source, by its fixed names, and never through
# its remote state: a reader of a state can read every output and secret in it.
data "azurerm_client_config" "current" {}

locals {
  name_prefix = "meridian"

  # The regions the module accepts: the foundation's list, which variables.tf
  # holds a second time in the validation of location. The pin below compares the
  # foundation's own region with it. A test holds all three equal.
  allowed_locations = ["swedencentral", "westeurope"]

  # Six hex characters of the subscription ID's SHA-1 make globally unique names
  # (the foundation's Key Vault) stable and free of the ID itself. The foundation
  # computes the same value, and the scripts derive it too: common.sh name_suffix.
  suffix = substr(sha1(data.azurerm_client_config.current.subscription_id), 0, 6)

  tags = {
    project     = "meridian"
    environment = "demo"
    managed-by  = "terraform"
  }

  # The group's tags are the three above and, only when var.expires_on is set,
  # expires-on with that date. No other resource carries it.
  group_tags = merge(local.tags, var.expires_on == null ? {} : { "expires-on" = var.expires_on })

  # The address plan. The three subnets sit inside the virtual network. The pod
  # and the service ranges of the cluster's overlay network are not subnets: they
  # are kept apart from every other range, and the cluster (a later change) takes
  # them from here. The DNS address is the cluster's DNS service, inside the
  # service range. A test holds the five ranges apart and every one private.
  vnet_cidr             = "10.40.0.0/16"
  nodes_subnet_cidr     = "10.40.0.0/22"
  postgres_subnet_cidr  = "10.40.4.0/24"
  endpoints_subnet_cidr = "10.40.5.0/24"
  pod_cidr              = "10.244.0.0/16"
  service_cidr          = "10.41.0.0/16"
  dns_service_ip        = "10.41.0.10"
}

# What the foundation holds, read by the names it was made with. The group has a
# fixed name and no suffix; the vault carries the suffix of the subscription the
# module is pointed at. A subscription with no foundation of this project has no
# such vault, and a plan stops at this read, before it proposes anything.
data "azurerm_resource_group" "foundation" {
  name = "rg-meridian-foundation"
}

data "azurerm_key_vault" "foundation" {
  name                = "kv-meridian-${local.suffix}"
  resource_group_name = data.azurerm_resource_group.foundation.name
}

# The subscription pin. The subscription is not in code (ARM_SUBSCRIPTION_ID, as
# the foundation's), so the module checks what it was pointed at: the vault it
# found must belong to the tenant the provider is signed in to, and the
# foundation's group must be in a region the module allows. So every resource of
# the module depends on this resource, directly or through the resource group
# below, and a plan against another subscription stops here before it proposes
# anything.
# The messages name neither a subscription nor a tenant; what a failed read of
# the vault above prints is the vault's name and the group's, which are derived
# from a hash of the subscription and are not secret.
#
# Written, and never seen to refuse: terraform validate does not evaluate a
# precondition, and nothing was planned. terraform_data is built into Terraform
# and needs no provider.
resource "terraform_data" "subscription_pin" {
  lifecycle {
    precondition {
      condition     = data.azurerm_key_vault.foundation.tenant_id == data.azurerm_client_config.current.tenant_id
      error_message = "The foundation's Key Vault belongs to another Entra tenant than the one the provider is signed in to. Nothing is proposed. Check ARM_SUBSCRIPTION_ID and the sign-in."
    }

    precondition {
      condition     = contains(local.allowed_locations, data.azurerm_resource_group.foundation.location)
      error_message = "The foundation's resource group is in a region this module does not allow (hard rule 3: EU residency). Nothing is proposed. Check ARM_SUBSCRIPTION_ID."
    }
  }
}

# The module's own group, deleted with everything in it when the environment is
# removed (providers.tf says what that asks of the provider). It waits for the pin,
# and every other resource of the module names this group, so each one depends on
# the pin through it.
resource "azurerm_resource_group" "platform" {
  name     = "rg-${local.name_prefix}-platform"
  location = var.location
  tags     = local.group_tags

  depends_on = [terraform_data.subscription_pin]
}
