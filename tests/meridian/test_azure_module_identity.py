"""The Azure platform module's identities, endpoints, budget and outputs (S020, Z4).

``identity.tf``, ``endpoints.tf``, ``budget.tf`` and ``outputs.tf`` hold the two
workload identities with their federated credentials and one role each, the two
private endpoints with their private DNS zones, the budget of the module's group
and what the second half and S022 read from the module. They are checked by
``terraform validate`` and by these tests, and they are never planned and never
applied: no sign-in is made or looked for, so nothing here has seen Azure. What a
test holds is what the text says, one decision to a test; what ``validate``
cannot check (that Azure takes a role at the scope of one secret, that an
endpoint to a resource of another group is approved by itself, that a zone name
is accepted) is the README's, not proved here.
"""

import re

import pytest
from azuremodulesupport import (
    FOUNDATION_DIR,
    MODULE_DIR,
    argument_names,
    attribute,
    comments_of,
    data_sources,
    data_sources_in,
    file_text,
    has_attribute,
    locals_text,
    module_text,
    nested_blocks,
    outputs,
    quoted_list_in,
    raw_text,
    resources,
    resources_in,
    role_assignments_everywhere,
    squeezed,
    variable_block,
)
from test_azure_module import ADDRESS, GUID, PRICE, hangs_on_the_pin

GROUP_NAME = "azurerm_resource_group.platform.name"
GROUP_LOCATION = "azurerm_resource_group.platform.location"
ISSUER = "azurerm_kubernetes_cluster.main.oidc_issuer_url"
AUDIENCE = '["api://AzureADTokenExchange"]'
GATEWAY = "azurerm_user_assigned_identity.gateway"
SECRETS = "azurerm_user_assigned_identity.secrets"
GATEWAY_CREDENTIAL = "azurerm_federated_identity_credential.gateway"
SECRETS_CREDENTIAL = "azurerm_federated_identity_credential.secrets"
GATEWAY_ROLE = "azurerm_role_assignment.gateway_openai_user"
SECRETS_ROLE = "azurerm_role_assignment.secrets_database_administrator"
ACCOUNT = "data.azurerm_cognitive_account.openai"
VAULT_ENTRY = "azurerm_key_vault_secret.database_administrator"
VAULT_ENDPOINT = "azurerm_private_endpoint.key_vault"
ACCOUNT_ENDPOINT = "azurerm_private_endpoint.openai"
VAULT_ZONE = "azurerm_private_dns_zone.key_vault"
ACCOUNT_ZONE = "azurerm_private_dns_zone.openai"
VAULT_LINK = "azurerm_private_dns_zone_virtual_network_link.key_vault"
ACCOUNT_LINK = "azurerm_private_dns_zone_virtual_network_link.openai"
BUDGET = "azurerm_consumption_budget_resource_group.platform"
ACTION_GROUP = "data.azurerm_monitor_action_group.budget"
CLUSTER = "azurerm_kubernetes_cluster.main"

IDENTITY_RESOURCES = [
    GATEWAY_CREDENTIAL,
    GATEWAY_ROLE,
    GATEWAY,
    SECRETS_CREDENTIAL,
    SECRETS_ROLE,
    SECRETS,
]
ENDPOINT_RESOURCES = [
    ACCOUNT_ENDPOINT,
    ACCOUNT_LINK,
    ACCOUNT_ZONE,
    VAULT_ENDPOINT,
    VAULT_LINK,
    VAULT_ZONE,
]
THE_FOUR_FILES = ["identity.tf", "endpoints.tf", "budget.tf", "outputs.tf"]

# The two role assignments of identity.tf: the role's name, the scope as written
# and the principal as written. The scope of each is ONE resource.
THE_TWO_ROLES = {
    GATEWAY_ROLE: (
        '"Cognitive Services OpenAI User"',
        f"{ACCOUNT}.id",
        f"{GATEWAY}.principal_id",
    ),
    SECRETS_ROLE: (
        '"Key Vault Secrets User"',
        f"{VAULT_ENTRY}.resource_versionless_id",
        f"{SECRETS}.principal_id",
    ),
}

# Every scope any role assignment of the whole module may have: a subnet, the
# cluster, the registry, one OpenAI account, one secret. Never wider.
ALLOWED_SCOPES = {
    "azurerm_subnet.nodes.id",
    f"{CLUSTER}.id",
    "azurerm_container_registry.main.id",
    "data.azurerm_cognitive_account.openai.id",
    f"{VAULT_ENTRY}.resource_versionless_id",
}

# What every output of the module is, and nothing else: the name, then the
# expression it carries.
OUTPUTS = {
    "resource_group_name": GROUP_NAME,
    "cluster_name": f"{CLUSTER}.name",
    "cluster_oidc_issuer_url": ISSUER,
    "registry_login_server": "azurerm_container_registry.main.login_server",
    "database_server_fqdn": "azurerm_postgresql_flexible_server.main.fqdn",
    "database_administrator_login": (
        "azurerm_postgresql_flexible_server.main.administrator_login"
    ),
    "database_administrator_secret_name": f"{VAULT_ENTRY}.name",
    "gateway_identity_client_id": f"{GATEWAY}.client_id",
    "secrets_identity_client_id": f"{SECRETS}.client_id",
    "log_workspace_id": "azurerm_log_analytics_workspace.main.id",
}

CARRIER = re.compile(
    r"random_password|ephemeral|administrator_password|value_wo|\.result\b"
    r"|bcrypt_hash",
    re.IGNORECASE,
)


def identity_resource(name: str) -> str:
    return resources_in("identity.tf")[name]


def endpoint_resource(name: str) -> str:
    return resources_in("endpoints.tf")[name]


def budget() -> str:
    return resources_in("budget.tf")[BUDGET]


def output_blocks() -> dict[str, str]:
    return dict(outputs())


# ── what the four files declare ──────────────────────────────────────────────


def test_identity_declares_exactly_these_resources_and_one_data_source() -> None:
    assert sorted(resources_in("identity.tf")) == sorted(IDENTITY_RESOURCES)
    assert sorted(data_sources_in("identity.tf")) == [
        "azurerm_cognitive_account.openai"
    ]


def test_endpoints_declares_exactly_these_resources_and_no_data_source() -> None:
    assert sorted(resources_in("endpoints.tf")) == sorted(ENDPOINT_RESOURCES)
    assert data_sources_in("endpoints.tf") == {}


def test_budget_declares_exactly_two_resources_and_one_data_source() -> None:
    # Z6 added the second, on the cluster's node resource group.
    assert sorted(resources_in("budget.tf")) == sorted(
        [BUDGET, "azurerm_consumption_budget_resource_group.nodes"]
    )
    assert sorted(data_sources_in("budget.tf")) == [
        "azurerm_monitor_action_group.budget"
    ]


def test_outputs_declares_no_resource_and_no_data_source() -> None:
    assert resources_in("outputs.tf") == {}
    assert data_sources_in("outputs.tf") == {}


def test_the_module_reads_exactly_these_data_sources_from_the_foundation() -> None:
    assert sorted(data_sources()) == [
        "azurerm_client_config.current",
        "azurerm_cognitive_account.openai",
        "azurerm_key_vault.foundation",
        "azurerm_monitor_action_group.budget",
        "azurerm_resource_group.foundation",
    ]


# ── the two identities ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "label"), [(GATEWAY, "gateway"), (SECRETS, "secrets")]
)
def test_an_identity_is_a_named_user_assigned_one_in_the_module_group(
    name: str, label: str
) -> None:
    body = identity_resource(name)

    assert attribute(body, "name") == f'"id-${{local.name_prefix}}-{label}"'
    assert attribute(body, "location") == GROUP_LOCATION
    assert attribute(body, "resource_group_name") == GROUP_NAME
    assert attribute(body, "tags") == "local.tags"


def test_the_gateways_credential_names_exactly_one_service_account() -> None:
    body = identity_resource(GATEWAY_CREDENTIAL)

    assert attribute(body, "subject") == (
        '"system:serviceaccount:${var.workload_namespace}'
        ':${var.gateway_service_account}"'
    )
    assert attribute(body, "user_assigned_identity_id") == f"{GATEWAY}.id"


def test_the_bootstraps_credential_names_exactly_one_service_account() -> None:
    body = identity_resource(SECRETS_CREDENTIAL)

    assert attribute(body, "subject") == (
        '"system:serviceaccount:${var.workload_namespace}'
        ':${var.secrets_service_account}"'
    )
    assert attribute(body, "user_assigned_identity_id") == f"{SECRETS}.id"


@pytest.mark.parametrize("name", [GATEWAY_CREDENTIAL, SECRETS_CREDENTIAL])
def test_each_credential_has_the_fixed_audience_and_the_clusters_issuer(
    name: str,
) -> None:
    body = identity_resource(name)

    assert attribute(body, "audience") == AUDIENCE
    assert attribute(body, "issuer") == ISSUER
    assert attribute(body, "name") is not None


def test_the_module_has_two_subjects_and_no_wildcard_in_either() -> None:
    subjects = [
        line.strip()
        for line in module_text().splitlines()
        if re.match(r"\s*subject\s*=", line)
    ]

    assert len(subjects) == 2
    for subject in subjects:
        assert "*" not in subject and "%" not in subject
        # Built from the two variables and the fixed prefix, and nothing else.
        parts = re.findall(r"\$\{([^}]*)\}", subject)
        assert parts[0] == "var.workload_namespace"
        assert parts[1].startswith("var.") and parts[1].endswith("_service_account")
        assert len(parts) == 2
        assert re.fullmatch(
            r'subject\s*=\s*"system:serviceaccount:\$\{[^}]+\}:\$\{[^}]+\}"', subject
        )


def test_the_two_service_account_names_are_not_the_same_variable() -> None:
    gateway = attribute(identity_resource(GATEWAY_CREDENTIAL), "subject")
    secrets = attribute(identity_resource(SECRETS_CREDENTIAL), "subject")

    assert gateway != secrets
    assert "var.gateway_service_account" in str(gateway)
    assert "var.secrets_service_account" in str(secrets)


def test_the_variables_these_subjects_use_are_dns_labels_with_no_wildcard() -> None:
    # A wildcard cannot reach a subject through a variable: each one is held to
    # a DNS label by its own validation (test_azure_module.py runs it).
    for name in (
        "workload_namespace",
        "gateway_service_account",
        "secrets_service_account",
    ):
        assert "[a-z0-9]([-a-z0-9]*[a-z0-9])?" in variable_block(name)


# ── the role assignments ─────────────────────────────────────────────────────


def test_identity_holds_exactly_two_role_assignments_with_these_terms() -> None:
    found = {
        name: (
            attribute(body, "role_definition_name"),
            attribute(body, "scope"),
            attribute(body, "principal_id"),
        )
        for name, body in resources_in("identity.tf").items()
        if name.startswith("azurerm_role_assignment.")
    }

    assert found == THE_TWO_ROLES


def test_the_gateways_role_is_on_the_one_account_of_the_modules_region() -> None:
    body = identity_resource(GATEWAY_ROLE)

    assert attribute(body, "scope") == f"{ACCOUNT}.id"
    assert attribute(body, "principal_type") == '"ServicePrincipal"'


def test_the_bootstraps_role_is_on_the_one_secret_and_not_the_vault() -> None:
    body = identity_resource(SECRETS_ROLE)

    assert attribute(body, "scope") == f"{VAULT_ENTRY}.resource_versionless_id"
    assert attribute(body, "principal_type") == '"ServicePrincipal"'
    assert "azurerm_key_vault." not in body
    assert "key_vault.foundation" not in body


def test_the_secrets_scope_is_the_arm_id_not_the_data_plane_address() -> None:
    # versionless_id is the vault's web address, which is no scope for a role;
    # resource_versionless_id is the secret's resource ID without a version.
    scope = attribute(identity_resource(SECRETS_ROLE), "scope")

    assert scope is not None and scope.endswith(".resource_versionless_id")


def test_the_roles_are_named_and_never_given_by_an_id() -> None:
    for name, body in resources_in("identity.tf").items():
        if name.startswith("azurerm_role_assignment."):
            assert not has_attribute(body, "role_definition_id"), name


def test_no_role_assignment_of_the_whole_module_has_a_wide_scope() -> None:
    found = role_assignments_everywhere()
    assert len(found) == 5  # Z2's three and these two: a sixth is a change

    for name, body in found.items():
        scope = attribute(body, "scope")
        assert scope is not None, name
        assert scope in ALLOWED_SCOPES, (name, scope)
        assert not re.search(r"resource_group|subscription", scope), (name, scope)
        assert not re.search(r"azurerm_key_vault\.|key_vault\.foundation", scope), name


def test_no_identity_of_this_module_has_a_role_on_the_vault_as_a_whole() -> None:
    for name in (GATEWAY_ROLE, SECRETS_ROLE):
        body = identity_resource(name)
        assert "data.azurerm_key_vault" not in body, name
        assert "data.azurerm_resource_group" not in body, name
        assert "data.azurerm_subscription" not in body, name


def test_the_secrets_role_comment_names_its_only_user() -> None:
    comment = squeezed(comments_of("identity.tf"))

    assert "second half's Job that creates the database roles" in comment
    assert "creates the database roles" in comment
    assert "only user" in comment


def test_a_second_region_is_said_to_need_its_own_role_and_endpoint() -> None:
    comment = squeezed(comments_of("identity.tf"))

    assert "a second region would need" in comment


# ── the OpenAI account it reads ──────────────────────────────────────────────


def test_the_account_is_read_by_the_foundations_name_in_the_foundations_group() -> None:
    body = data_sources_in("identity.tf")["azurerm_cognitive_account.openai"]

    assert attribute(body, "name") == (
        '"oai-meridian-${local.openai_location_keys[var.location]}-${local.suffix}"'
    )
    assert attribute(body, "resource_group_name") == (
        "data.azurerm_resource_group.foundation.name"
    )


def test_the_name_has_the_form_the_foundation_gives_its_accounts() -> None:
    foundation = raw_text("openai.tf", FOUNDATION_DIR)

    assert re.search(
        r'^\s*name\s*=\s*"oai-meridian-\$\{each\.key\}-\$\{local\.suffix\}"$',
        foundation,
        flags=re.MULTILINE,
    )


def locations_by_key() -> dict[str, str]:
    (block,) = re.findall(
        r"^locals \{\n(.*?)^\}$",
        file_text("identity.tf"),
        flags=re.MULTILINE | re.DOTALL,
    )
    (mapping,) = re.findall(
        r"openai_location_keys\s*=\s*\{(.*?)\}", block, flags=re.DOTALL
    )
    return {
        location: key for location, key in re.findall(r'(\w+)\s*=\s*"(\w+)"', mapping)
    }


def test_the_label_of_each_region_is_the_foundations_and_covers_the_closed_list() -> (
    None
):
    pairs = locations_by_key()
    foundation = raw_text("variables.tf", FOUNDATION_DIR)

    assert pairs == {"swedencentral": "sdc", "westeurope": "weu"}
    # Both labels are written in the foundation's own variable: sdc in the
    # default, weu in the comment that says when it returns.
    for location, key in pairs.items():
        assert re.search(rf'\b{key}\s*=\s*"{location}"', foundation), (key, location)
    assert sorted(pairs) == sorted(
        quoted_list_in(variable_block("location"), "var.location")
    )
    assert "openai_location_keys" in locals_text()


def test_the_label_map_says_where_it_came_from() -> None:
    comment = squeezed(comments_of("identity.tf"))

    assert "the foundation's" in comment
    assert "openai_locations" in comment


# ── the endpoints ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "subresource", "target"),
    [
        (VAULT_ENDPOINT, '"vault"', "data.azurerm_key_vault.foundation.id"),
        (ACCOUNT_ENDPOINT, '"account"', f"{ACCOUNT}.id"),
    ],
)
def test_each_endpoint_sits_in_the_endpoints_subnet_and_connects_to_one_resource(
    name: str, subresource: str, target: str
) -> None:
    body = endpoint_resource(name)
    (connection,) = nested_blocks(body, "private_service_connection")

    assert attribute(body, "subnet_id") == "azurerm_subnet.endpoints.id"
    assert attribute(body, "resource_group_name") == GROUP_NAME
    assert attribute(body, "location") == GROUP_LOCATION
    assert attribute(body, "tags") == "local.tags"
    assert attribute(connection, "private_connection_resource_id") == target
    assert attribute(connection, "subresource_names") == f"[{subresource}]"
    assert attribute(connection, "is_manual_connection") == "false"


@pytest.mark.parametrize(
    ("endpoint", "zone"),
    [(VAULT_ENDPOINT, VAULT_ZONE), (ACCOUNT_ENDPOINT, ACCOUNT_ZONE)],
)
def test_each_endpoint_has_a_zone_group_with_its_own_zone_and_no_other(
    endpoint: str, zone: str
) -> None:
    (group,) = nested_blocks(endpoint_resource(endpoint), "private_dns_zone_group")

    assert attribute(group, "private_dns_zone_ids") == f"[{zone}.id]"
    assert attribute(group, "name") is not None


def test_there_are_exactly_two_private_endpoints_in_the_module() -> None:
    found = sorted(n for n in resources() if n.startswith("azurerm_private_endpoint."))

    assert found == sorted([ACCOUNT_ENDPOINT, VAULT_ENDPOINT])


@pytest.mark.parametrize(
    ("zone", "dns_name"),
    [
        (VAULT_ZONE, '"privatelink.vaultcore.azure.net"'),
        (ACCOUNT_ZONE, '"privatelink.openai.azure.com"'),
    ],
)
def test_each_zone_has_the_name_of_its_endpoint_suffix_in_the_module_group(
    zone: str, dns_name: str
) -> None:
    body = endpoint_resource(zone)

    assert attribute(body, "name") == dns_name
    assert attribute(body, "resource_group_name") == GROUP_NAME
    assert attribute(body, "tags") == "local.tags"


@pytest.mark.parametrize(
    ("link", "zone"), [(VAULT_LINK, VAULT_ZONE), (ACCOUNT_LINK, ACCOUNT_ZONE)]
)
def test_each_zone_is_linked_to_the_modules_network_with_no_registration(
    link: str, zone: str
) -> None:
    body = endpoint_resource(link)

    assert attribute(body, "private_dns_zone_id") == f"{zone}.id"
    assert attribute(body, "virtual_network_id") == "azurerm_virtual_network.main.id"
    assert attribute(body, "registration_enabled") == "false"
    assert attribute(body, "tags") == "local.tags"


def test_only_the_account_zone_the_gateways_host_name_needs_is_written() -> None:
    # The gateway accepts the host suffix .openai.azure.com and no other; the
    # Cognitive Services scope is a token's scope, not a host.
    comment = squeezed(comments_of("endpoints.tf"))

    assert ".openai.azure.com" in comment
    assert "the gateway accepts no other host" in comment
    assert "cognitiveservices.azure.com" in comment
    assert "privatelink.cognitiveservices.azure.com" not in module_text()
    assert "privatelink.services.ai.azure.com" not in module_text()


def test_endpoints_says_public_access_stays_on_and_what_the_endpoints_give() -> None:
    comment = squeezed(comments_of("endpoints.tf"))

    assert "public access stays ON" in comment
    assert "does not close the vault or the account to the internet" in comment
    assert "a private path" in comment
    assert "one subnet" in comment
    # What the module makes is not a closed door: neither resource is changed.
    assert "public_network_access" not in file_text("endpoints.tf")
    assert "network_acls" not in file_text("endpoints.tf")


def test_the_endpoint_comment_comes_first_in_the_file() -> None:
    first = raw_text("endpoints.tf").lstrip().splitlines()[0]

    assert first.startswith("#")


# ── the budget ───────────────────────────────────────────────────────────────


def test_the_budget_is_on_the_modules_group_and_its_amount_is_the_variable() -> None:
    body = budget()

    assert attribute(body, "resource_group_id") == "azurerm_resource_group.platform.id"
    assert attribute(body, "amount") == "var.budget_amount_eur"
    assert attribute(body, "time_grain") == '"Monthly"'
    assert attribute(body, "name") is not None
    assert not nested_blocks(body, "filter")


def test_the_budget_has_three_notifications_at_50_80_and_100_actual_spend() -> None:
    notifications = nested_blocks(budget(), "notification")

    assert sorted(float(attribute(n, "threshold") or "nan") for n in notifications) == [
        50.0,
        80.0,
        100.0,
    ]
    assert not nested_blocks(budget(), "dynamic")
    for body in notifications:
        assert attribute(body, "threshold_type") == '"Actual"'
        assert attribute(body, "operator") == '"GreaterThanOrEqualTo"'
        assert attribute(body, "enabled") == "true"


def test_every_notification_goes_to_the_foundations_action_group_and_nobody_else() -> (
    None
):
    for body in nested_blocks(budget(), "notification"):
        assert attribute(body, "contact_groups") == f"[{ACTION_GROUP}.id]"
        assert not has_attribute(body, "contact_emails")
        assert not has_attribute(body, "contact_roles")


def test_the_action_group_is_read_by_the_foundations_name() -> None:
    body = data_sources_in("budget.tf")["azurerm_monitor_action_group.budget"]

    assert attribute(body, "name") == '"ag-meridian-budget"'
    assert attribute(body, "resource_group_name") == (
        "data.azurerm_resource_group.foundation.name"
    )
    foundation = raw_text("main.tf", FOUNDATION_DIR)
    assert 'name                = "ag-meridian-budget"' in foundation


def test_the_start_date_is_the_foundations_form_and_is_left_alone_after_creation() -> (
    None
):
    (period,) = nested_blocks(budget(), "time_period")
    foundation = raw_text("main.tf", FOUNDATION_DIR)

    assert attribute(period, "start_date") == (
        """formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())"""
    )
    assert """formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())""" in foundation
    (lifecycle,) = nested_blocks(budget(), "lifecycle")
    assert attribute(lifecycle, "ignore_changes") == "[time_period]"
    assert not has_attribute(period, "end_date")


def test_the_budget_says_it_alerts_and_does_not_stop_spend() -> None:
    comment = squeezed(comments_of("budget.tf"))

    assert "alerts" in comment and "does not stop spend" in comment
    assert "T-15" in comment


# ── the outputs ──────────────────────────────────────────────────────────────


def test_outputs_are_exactly_these_ten_and_each_carries_its_expression() -> None:
    found = output_blocks()

    assert sorted(found) == sorted(OUTPUTS)
    for name, expression in OUTPUTS.items():
        assert attribute(found[name], "value") == expression, name


def test_every_output_has_a_description_and_none_is_marked_sensitive() -> None:
    for name, body in output_blocks().items():
        description = attribute(body, "description")
        assert description is not None and len(description) > 10, name
        assert not has_attribute(body, "sensitive"), name
        assert not has_attribute(body, "ephemeral"), name


def test_no_output_is_or_derives_from_the_password() -> None:
    for name, body in output_blocks().items():
        assert not CARRIER.search(body), name
        value = attribute(body, "value") or ""
        assert "password" not in value.lower(), name
    assert not CARRIER.search(locals_text())


def test_the_password_check_can_see_what_it_looks_for() -> None:
    # A check that matches nothing proves nothing: each shape, once.
    for shape in (
        "ephemeral.random_password.database_administrator.result",
        "azurerm_postgresql_flexible_server.main.administrator_password_wo",
        "value_wo",
        "x.result",
    ):
        assert CARRIER.search(f"value = {shape}"), shape
    assert not CARRIER.search("value = azurerm_postgresql_flexible_server.main.fqdn")


def test_the_one_output_that_names_the_secret_gives_its_name_and_not_its_value() -> (
    None
):
    body = output_blocks()["database_administrator_secret_name"]

    assert attribute(body, "value") == f"{VAULT_ENTRY}.name"
    assert "is not an output" in (attribute(body, "description") or "")


# ── no credential of its own anywhere in the module ──────────────────────────

# The three names that hold the word password and are not a credential: the
# server's write-only argument and its version, and the switch that keeps
# password sign-in on (database.tf holds each).
PASSWORD_ARGUMENTS = {
    "administrator_password_wo",
    "administrator_password_wo_version",
    "password_auth_enabled",
}


def test_no_resource_sets_a_client_secret_a_password_or_a_certificate() -> None:
    for name, body in resources().items():
        for argument in argument_names(body):
            assert "client_secret" not in argument, (name, argument)
            assert "certificate" not in argument, (name, argument)
            assert "service_principal" not in argument, (name, argument)
            assert "shared_key" not in argument, (name, argument)
            if "password" in argument:
                assert argument in PASSWORD_ARGUMENTS, (name, argument)
    # No identity of Entra's own is made by Terraform: no application, no
    # service principal, no password for either (the provider is not used).
    assert not [n for n in resources() if n.startswith(("azuread_", "azurerm_aad_"))]


def test_the_four_files_name_no_password_secret_value_or_credential() -> None:
    for file_name in THE_FOUR_FILES:
        text = file_text(file_name)
        assert not re.search(r"password|client_secret|certificate", text), file_name
        assert not re.search(r"\bvalue_wo\b|\bvalue\s*=\s*\"", text), file_name


# ── the pin, the facts, the cluster ──────────────────────────────────────────


def test_every_resource_of_the_four_files_hangs_on_the_pin() -> None:
    found = resources()
    names = [name for file_name in THE_FOUR_FILES for name in resources_in(file_name)]

    assert len(names) == 14
    assert [n for n in names if not hangs_on_the_pin(n, found, frozenset())] == []


def test_the_four_files_state_no_guid_no_address_and_no_price() -> None:
    for file_name in THE_FOUR_FILES:
        text = (MODULE_DIR / file_name).read_text(encoding="utf-8")
        assert not GUID.search(text), file_name
        assert not PRICE.search(text), file_name
        assert not ADDRESS.search(text), file_name


def test_only_the_database_file_names_a_vault_by_id() -> None:
    # Z3's own test reads every file for the argument; the four say it in no
    # word either, comments included.
    for file_name in THE_FOUR_FILES:
        assert "key_vault_id" not in raw_text(file_name), file_name


def test_run_command_is_off_and_the_comment_says_why() -> None:
    body = resources_in("cluster.tf")[CLUSTER]
    comment = squeezed(comments_of("cluster.tf"))

    assert attribute(body, "run_command_enabled") == "false"
    assert "Run command is off" in comment
    assert "management API" in comment
    assert "does not use it" in comment
