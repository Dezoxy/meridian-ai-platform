# The two identities a workload in the cluster may take, each bound to exactly
# one Kubernetes service account and holding exactly one role on exactly one
# resource (S020). Declared, never planned, never applied (README.md).
#
# Workload identity: a pod presents a token that the cluster's issuer signed
# (cluster.tf turns the issuer and workload identity on), and Azure exchanges it
# for the identity's own token. No client secret, no certificate and no service
# principal credential exists anywhere in this module. No identity here gets a
# role on a resource group, on the vault as a whole or on the subscription.
#
# Every resource here hangs on the subscription pin (main.tf) through the module's
# resource group: the identities name it, and the credentials and the role
# assignments name an identity.

locals {
  # The foundation names its OpenAI accounts oai-meridian-<key>-<suffix>, where
  # <key> is the label of a location in the foundation's openai_locations
  # variable (infra/terraform/foundation/variables.tf): sdc is its default entry,
  # and the comment above that variable names weu for West Europe. This module
  # has the region and not the label, so the labels are written here once, one
  # for each region variables.tf allows. A test holds both pairs against the
  # foundation's text.
  openai_location_keys = {
    swedencentral = "sdc"
    westeurope    = "weu"
  }
}

# The foundation's account in the module's region, read by the name the
# foundation gave it. A region in which the foundation has no account fails at
# this read, before anything is proposed. Only the account of var.location is
# read: if the foundation ever holds accounts in more than one location, the role
# below is on the one in var.location only, and a second region would need its own
# data source, its own role assignment for the gateway's identity and its own
# private endpoint (endpoints.tf), each written for that account.
data "azurerm_cognitive_account" "openai" {
  name                = "oai-meridian-${local.openai_location_keys[var.location]}-${local.suffix}"
  resource_group_name = data.azurerm_resource_group.foundation.name
}

# The Model Gateway's identity, federated to ONE service account: the one that
# var.workload_namespace and var.gateway_service_account name, and no other. The
# subject is built from those two variables and nothing else (each is held to a
# DNS label by its validation, so no wildcard can come in through it). The
# audience is the one Azure's token exchange fixes. The issuer is the cluster's:
# the credential follows the cluster if it is made again, and an issuer from
# another cluster is refused by the reference alone.
resource "azurerm_user_assigned_identity" "gateway" {
  name                = "id-${local.name_prefix}-gateway"
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "gateway" {
  name                      = "fic-${local.name_prefix}-gateway"
  user_assigned_identity_id = azurerm_user_assigned_identity.gateway.id
  issuer                    = azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject                   = "system:serviceaccount:${var.workload_namespace}:${var.gateway_service_account}"
  audience                  = ["api://AzureADTokenExchange"]
}

# The one role of the gateway's identity: the data-plane role that lets it call
# the models, on the one account it calls (owner roles are control-plane and do
# not let a caller use a model).
resource "azurerm_role_assignment" "gateway_openai_user" {
  scope                = data.azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.gateway.principal_id
  principal_type       = "ServicePrincipal"
}

# The bootstrap's identity, federated to ONE service account the same way
# (var.secrets_service_account; the second half of S020 confirms the name).
resource "azurerm_user_assigned_identity" "secrets" {
  name                = "id-${local.name_prefix}-secrets"
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "secrets" {
  name                      = "fic-${local.name_prefix}-secrets"
  user_assigned_identity_id = azurerm_user_assigned_identity.secrets.id
  issuer                    = azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject                   = "system:serviceaccount:${var.workload_namespace}:${var.secrets_service_account}"
  audience                  = ["api://AzureADTokenExchange"]
}

# The one role of the bootstrap's identity: Key Vault Secrets User on the ONE
# secret this module writes, the database administrator's, and not on the vault.
# The scope is the secret's own resource ID without a version, so a new version
# of the secret (database.tf raises it to rotate the credential) needs no new
# assignment. The second half's Job that creates the database roles is its only
# user. A role at this scope reads that secret and no other secret of the vault.
resource "azurerm_role_assignment" "secrets_database_administrator" {
  scope                = azurerm_key_vault_secret.database_administrator.resource_versionless_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.secrets.principal_id
  principal_type       = "ServicePrincipal"
}
