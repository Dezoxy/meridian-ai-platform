"""The Azure platform module's fixes after its two reviews (S020, Z6).

An infrastructure review and a security review read ``infra/terraform/azure`` at
the end of Z1 to Z5, and a second facts sheet read what the first review only
recalled. Each test below pins one fix to the argument that makes it, read from
the comment-stripped text: nothing is planned and nothing is applied, so what
``terraform validate`` cannot check (that Azure accepts a value, what an apply
makes of it) is the README's list, not proved here. The places where the facts
sheet said NOT ESTABLISHED are pinned too, as the conservative form and the
comment that says so.
"""

import re

import pytest
from azuremodulesupport import (
    MODULE_DIR,
    attribute,
    comments_of,
    data_sources_in,
    file_text,
    has_attribute,
    module_text,
    nested_blocks,
    own_text,
    resources,
    resources_in,
    squeezed,
    top_level_blocks,
    variable_block,
)
from test_azure_module import ADDRESS, GUID, PLATFORM_GROUP, PRICE

SERVER = "azurerm_postgresql_flexible_server.main"
WORKSPACE = "azurerm_log_analytics_workspace.main"
CLUSTER = "azurerm_kubernetes_cluster.main"
VAULT = "data.azurerm_key_vault.foundation"
ACTION_GROUP = "data.azurerm_monitor_action_group.budget"
NODES_CIDR = "local.nodes_subnet_cidr"
CONFIGURATION = "azurerm_postgresql_flexible_server_configuration"
SUBNET_NSG = "azurerm_subnet_network_security_group_association"


def provider() -> str:
    (body,) = top_level_blocks(file_text("providers.tf"), "provider").values()
    return body


def depends_on_list(body: str) -> list[str]:
    """The names a resource's ``depends_on`` lists, one line or several."""
    found = re.findall(r"^  depends_on\s*=\s*\[(.*?)\]", body, re.M | re.S)
    return sorted(re.findall(r"[a-z_]+\.[a-z_]+(?:\.[a-z_]+)?", " ".join(found)))


# ── the workspace (infrastructure HIGH-1, security H1) ───────────────────────


def test_the_provider_purges_a_removed_workspace_and_the_comment_says_why() -> None:
    assert re.search(
        r"^\s*log_analytics_workspace \{\n\s*permanently_delete_on_destroy\s*=\s*true\n"
        r"\s*\}$",
        provider(),
        flags=re.MULTILINE,
    )
    comment = squeezed(comments_of("providers.tf"))

    assert "14 days" in comment
    assert "the OLD one given back" in comment
    assert "so every demo day starts empty" in comment


def test_the_workspace_takes_no_shared_key_and_settings_go_by_resource_id() -> None:
    body = resources_in("logs.tf")[WORKSPACE]
    comment = squeezed(comments_of("logs.tf"))

    assert attribute(body, "local_authentication_enabled") == "false"
    assert "Shared-key sign-in is off" in comment
    assert "send by the workspace's resource id" in comment
    assert "provider writes both into the Terraform state" in comment
    # Nothing reads a key: the two computed attributes are named nowhere.
    assert "primary_shared_key" not in module_text()
    assert "secondary_shared_key" not in module_text()


# ── the vault's and the server's logs (security M4, M5) ──────────────────────


def test_the_foundation_vault_sends_audit_events_to_this_modules_workspace() -> None:
    body = resources_in("logs.tf")["azurerm_monitor_diagnostic_setting.vault"]
    (log,) = nested_blocks(body, "enabled_log")

    assert attribute(body, "target_resource_id") == f"{VAULT}.id"
    assert attribute(body, "log_analytics_workspace_id") == f"{WORKSPACE}.id"
    assert attribute(log, "category") == '"AuditEvent"'
    assert nested_blocks(body, "enabled_metric") == []
    assert attribute(body, "depends_on") == f"[{PLATFORM_GROUP}]"
    comment = squeezed(comments_of("logs.tf"))
    assert "nothing recorded who read it" in comment
    assert "the vault itself is not changed" in comment


def test_the_server_sends_its_log_to_the_same_workspace_by_category_group() -> None:
    body = resources_in("database.tf")["azurerm_monitor_diagnostic_setting.database"]
    (log,) = nested_blocks(body, "enabled_log")

    assert attribute(body, "target_resource_id") == f"{SERVER}.id"
    assert attribute(body, "log_analytics_workspace_id") == f"{WORKSPACE}.id"
    assert attribute(log, "category_group") == '"allLogs"'
    assert not has_attribute(log, "category")
    # NOT ESTABLISHED by any page or by the schema: the comment says so, and says
    # what the cost of taking every category is.
    comment = squeezed(comments_of("database.tf"))
    assert "NOT ESTABLISHED" in comment
    assert "takes the group for a flexible server is not read" in comment
    assert "name the one that holds the connection log and no other" in comment


@pytest.mark.parametrize("name", ["log_connections", "log_checkpoints"])
def test_a_logging_parameter_is_on_and_waits_for_the_log_destination(
    name: str,
) -> None:
    body = resources_in("database.tf")[f"{CONFIGURATION}.{name}"]

    assert attribute(body, "name") == f'"{name}"'
    assert attribute(body, "server_id") == f"{SERVER}.id"
    assert attribute(body, "value") == '"on"'
    assert depends_on_list(body) == ["azurerm_monitor_diagnostic_setting.database"]


def test_connection_throttling_is_on_under_the_flexible_servers_own_name() -> None:
    body = resources_in("database.tf")[f"{CONFIGURATION}.connection_throttle"]

    assert attribute(body, "name") == '"connection_throttle.enable"'
    assert attribute(body, "value") == '"on"'
    assert '"connection_throttling"' not in module_text()
    comment = squeezed(comments_of("database.tf"))
    assert "this one is OFF by default" in comment
    assert "this DOES change what the server does" in comment


def test_the_database_waits_for_every_configuration_of_the_server() -> None:
    found = resources_in("database.tf")
    configurations = sorted(name for name in found if name.startswith(CONFIGURATION))

    assert len(configurations) == 4
    body = found["azurerm_postgresql_flexible_server_database.meridian"]
    assert depends_on_list(body) == configurations


# ── the server (infrastructure M2, security M1) ──────────────────────────────


def test_the_server_ignores_the_zone_azure_picks_and_names_none() -> None:
    body = resources_in("database.tf")[SERVER]
    (lifecycle,) = nested_blocks(body, "lifecycle")

    assert attribute(lifecycle, "ignore_changes") == "[zone]"
    assert not has_attribute(own_text(body), "zone")
    comment = squeezed(comments_of("database.tf"))
    assert "neither computed nor a replacement trigger" in comment
    assert "offers ignore_changes for the zone" in comment


def test_the_server_keeps_its_wait_for_the_zones_link_beside_the_lifecycle() -> None:
    body = resources_in("database.tf")[SERVER]

    assert depends_on_list(body) == [
        "azurerm_private_dns_zone_virtual_network_link.postgres"
    ]


def test_the_password_comment_names_the_two_cases_and_ends_in_the_rule() -> None:
    comment = squeezed(comments_of("database.tf"))

    # The sentence that was false.
    assert "Nothing else ever changes the password" not in comment
    assert "A server that is replaced" in comment
    assert "fails at the secret, run again" in comment
    assert "the Job's login fails at first use" in comment
    assert comment.count("raise the version in the same change") == 1
    assert "in the same change as any replacement of the server" in comment


# ── the registry (infrastructure M6, security L2) ────────────────────────────


def test_the_registry_names_the_mode_under_which_acrpull_grants_a_pull() -> None:
    body = resources_in("registry.tf")["azurerm_container_registry.main"]

    assert attribute(body, "role_assignment_mode") == '"LegacyRegistryPermissions"'
    assert "AbacRepositoryPermissions" not in module_text()
    comment = squeezed(comments_of("registry.tf"))
    assert "the provider always sends it" in comment
    assert "is not honoured at all in the ABAC mode" in comment
    assert "AcrPull" in comment


# ── the privatelink links (infrastructure M3) ────────────────────────────────


@pytest.mark.parametrize("name", ["key_vault", "openai"])
def test_a_privatelink_zones_link_redirects_a_name_the_zone_lacks(name: str) -> None:
    body = resources_in("endpoints.tf")[
        f"azurerm_private_dns_zone_virtual_network_link.{name}"
    ]

    assert attribute(body, "resolution_policy") == '"NxDomainRedirect"'


def test_the_postgres_zones_link_has_no_resolution_policy() -> None:
    # The policy exists only for Private Link zones; the database's zone is not one.
    link = resources_in("database.tf")[
        "azurerm_private_dns_zone_virtual_network_link.postgres"
    ]

    assert not has_attribute(link, "resolution_policy")
    assert module_text().count("resolution_policy") == 2
    assert "is not a Private Link zone" in squeezed(comments_of("endpoints.tf"))


# ── the second budget (infrastructure HIGH-2) ────────────────────────────────


def test_a_second_budget_watches_the_cluster_node_resource_group() -> None:
    found = resources_in("budget.tf")
    first = found["azurerm_consumption_budget_resource_group.platform"]
    second = found["azurerm_consumption_budget_resource_group.nodes"]

    assert attribute(second, "resource_group_id") == f"{CLUSTER}.node_resource_group_id"
    assert attribute(second, "amount") == "var.budget_amount_eur"
    assert attribute(second, "time_grain") == '"Monthly"'
    assert attribute(second, "name") == '"budget-${local.name_prefix}-nodes"'
    # The same notifications as the first, word for word, to the same group.
    assert sorted(nested_blocks(second, "notification")) == sorted(
        nested_blocks(first, "notification")
    )
    assert len(nested_blocks(second, "notification")) == 3
    assert nested_blocks(second, "time_period") == nested_blocks(first, "time_period")
    assert nested_blocks(second, "lifecycle") == nested_blocks(first, "lifecycle")
    assert not nested_blocks(second, "filter")


def test_the_first_budget_says_it_does_not_count_the_nodes() -> None:
    comment = squeezed(comments_of("budget.tf"))

    assert "It does NOT count the cluster's nodes" in comment
    assert "billed in the node resource group" in comment


def test_the_second_budget_says_no_page_establishes_that_azure_accepts_it() -> None:
    comment = squeezed(comments_of("budget.tf"))

    assert "NOT established by any page" in comment
    assert "that Azure accepts a budget on a managed group" in comment
    assert "no lockdown is set" in comment
    # The comment's claim holds: the cluster names no node group and no lockdown.
    assert "node_resource_group_name" not in file_text("cluster.tf")
    assert "node_resource_group_lockdown" not in file_text("cluster.tf")


def test_no_daily_cap_alert_is_built_and_no_action_group_is_made() -> None:
    # Z6 point 9 was conditional and its condition did not hold: the resources
    # exist, and the module already reads the foundation's action group, but the
    # signal of the cap (a query or an operation name) is on no page the facts
    # sheets read. A row for Z8; building one now would be a guess in a query.
    kinds = {name.split(".")[0] for name in resources()}

    assert not {k for k in kinds if "scheduled_query" in k or "metric_alert" in k}
    assert "azurerm_monitor_action_group" not in kinds
    assert list(data_sources_in("budget.tf")) == [ACTION_GROUP.removeprefix("data.")]


# ── the network (infrastructure HIGH-3, L3; both reviews on security groups) ─


def subnet(name: str) -> str:
    return resources_in("network.tf")[f"azurerm_subnet.{name}"]


def test_the_postgres_subnet_declares_the_storage_endpoint_azure_adds_to_it() -> None:
    (endpoint,) = nested_blocks(subnet("postgres"), "service_endpoint")

    assert attribute(endpoint, "service") == '"Microsoft.Storage"'
    # 5.x has the block and not the 4.x list.
    assert "service_endpoints" not in module_text()
    for name in ("nodes", "endpoints"):
        assert nested_blocks(subnet(name), "service_endpoint") == []
    comment = squeezed(comments_of("network.tf"))
    assert "adds it to a delegated subnet itself" in comment
    assert "is not computed" in comment
    assert "the 4.x list is gone" in comment


def test_only_the_endpoints_subnet_is_private_and_has_the_policy_for_its_group() -> (
    None
):
    endpoints = subnet("endpoints")

    assert attribute(endpoints, "default_outbound_access_enabled") == "false"
    assert attribute(endpoints, "private_endpoint_network_policies") == (
        '"NetworkSecurityGroupEnabled"'
    )
    for name in ("nodes", "postgres"):
        assert not has_attribute(subnet(name), "default_outbound_access_enabled")
        assert not has_attribute(subnet(name), "private_endpoint_network_policies")
    comment = squeezed(comments_of("network.tf"))
    assert "off here only" in comment
    assert "read from the value's name, not from a page" in comment


GROUP_NAMES = {
    "postgres": "nsg-${local.name_prefix}-postgres",
    "endpoints": "nsg-${local.name_prefix}-endpoints",
}


@pytest.mark.parametrize("name", ["postgres", "endpoints"])
def test_each_guarded_subnet_has_a_group_in_the_module_group_and_region(
    name: str,
) -> None:
    body = resources_in("network.tf")[f"azurerm_network_security_group.{name}"]

    assert attribute(body, "name") == f'"{GROUP_NAMES[name]}"'
    assert attribute(body, "resource_group_name") == f"{PLATFORM_GROUP}.name"
    assert attribute(body, "location") == f"{PLATFORM_GROUP}.location"
    assert attribute(body, "tags") == "local.tags"
    # The rules are resources of their own, never inline blocks of the group.
    assert not has_attribute(body, "security_rule")
    assert nested_blocks(body, "security_rule") == []


@pytest.mark.parametrize(
    ("name", "rule", "port", "destination"),
    [
        ("postgres", "postgres_allow_nodes", '"5432"', "local.postgres_subnet_cidr"),
        ("endpoints", "endpoints_allow_nodes", '"443"', "local.endpoints_subnet_cidr"),
    ],
)
def test_a_group_lets_the_nodes_range_reach_one_tcp_port_of_its_subnet(
    name: str, rule: str, port: str, destination: str
) -> None:
    body = resources_in("network.tf")[f"azurerm_network_security_rule.{rule}"]

    assert attribute(body, "network_security_group_name") == (
        f"azurerm_network_security_group.{name}.name"
    )
    assert attribute(body, "resource_group_name") == f"{PLATFORM_GROUP}.name"
    assert attribute(body, "priority") == "100"
    assert attribute(body, "direction") == '"Inbound"'
    assert attribute(body, "access") == '"Allow"'
    assert attribute(body, "protocol") == '"Tcp"'
    assert attribute(body, "source_address_prefix") == NODES_CIDR
    assert attribute(body, "destination_address_prefix") == destination
    assert attribute(body, "destination_port_range") == port
    assert attribute(body, "source_port_range") == '"*"'


@pytest.mark.parametrize("name", ["postgres", "endpoints"])
def test_a_group_denies_all_other_inbound_after_its_allow_and_writes_no_outbound_rule(
    name: str,
) -> None:
    found = resources_in("network.tf")
    deny = found[f"azurerm_network_security_rule.{name}_deny_other_inbound"]
    allow = found[f"azurerm_network_security_rule.{name}_allow_nodes"]

    assert attribute(deny, "network_security_group_name") == (
        f"azurerm_network_security_group.{name}.name"
    )
    assert attribute(deny, "direction") == '"Inbound"'
    assert attribute(deny, "access") == '"Deny"'
    assert attribute(deny, "protocol") == '"*"'
    for argument in (
        "source_port_range",
        "destination_port_range",
        "source_address_prefix",
        "destination_address_prefix",
    ):
        assert attribute(deny, argument) == '"*"', argument
    # Azure's default rules, at 65000 and above, allow the whole virtual network
    # in: the deny must come after the allow and before them.
    assert int(attribute(allow, "priority") or "0") < int(
        attribute(deny, "priority") or "0"
    )
    assert 100 < int(attribute(deny, "priority") or "0") < 65000


def test_no_rule_of_the_module_is_outbound_and_no_rule_allows_from_anywhere() -> None:
    assert '"Outbound"' not in module_text()
    allows = {
        name: body
        for name, body in resources().items()
        if name.startswith("azurerm_network_security_rule.")
        and attribute(body, "access") == '"Allow"'
    }

    assert len(allows) == 2
    for name, body in allows.items():
        assert attribute(body, "source_address_prefix") == NODES_CIDR, name


@pytest.mark.parametrize("name", ["postgres", "endpoints"])
def test_each_group_is_attached_to_its_subnet_and_waits_for_the_module_group(
    name: str,
) -> None:
    body = resources_in("network.tf")[f"{SUBNET_NSG}.{name}"]

    assert attribute(body, "subnet_id") == f"azurerm_subnet.{name}.id"
    assert attribute(body, "network_security_group_id") == (
        f"azurerm_network_security_group.{name}.id"
    )
    assert attribute(body, "depends_on") == f"[{PLATFORM_GROUP}]"


def test_the_nodes_subnet_has_no_group_and_nothing_names_one_for_it() -> None:
    text = module_text()

    assert "azurerm_network_security_group.nodes" not in text
    assert f"{SUBNET_NSG}.nodes" not in text
    assert "network_security_group_id" not in subnet("nodes")
    # The only association of the module are the two above, so none has the
    # nodes' subnet as its subnet_id.
    associated = [
        attribute(body, "subnet_id")
        for name, body in resources().items()
        if name.startswith(f"{SUBNET_NSG}.")
    ]
    assert "azurerm_subnet.nodes.id" not in associated
    assert len(associated) == 2


def test_network_names_no_address_and_only_the_plans_locals_for_a_range() -> None:
    raw = (MODULE_DIR / "network.tf").read_text(encoding="utf-8")

    assert not ADDRESS.search(raw)
    assert not GUID.search(raw)
    assert not PRICE.search(raw)
    for found in re.findall(r"(?:source|destination)_address_prefix\s*=\s*(\S+)", raw):
        assert found == '"*"' or found.startswith("local."), found


def test_the_network_comment_says_what_the_group_does_not_know() -> None:
    comment = squeezed(comments_of("network.tf"))

    assert "NOT read: what Microsoft requires of a group on a flexible server's" in (
        comment
    )
    assert "overlay pods are translated to their node's address" in comment
    assert "the explicit deny at the end of the list is what makes" in comment


# ── the tag that says when the environment should be gone (security L4) ──────


def test_expires_on_has_no_default_value_and_the_group_alone_carries_the_tag() -> None:
    body = variable_block("expires_on")

    assert attribute(body, "type") == "string"
    assert attribute(body, "default") == "null"
    assert attribute(body, "sensitive") is None
    assert re.search(r"^\s*validation \{$", body, flags=re.MULTILINE)
    group = resources_in("main.tf")[PLATFORM_GROUP]
    assert attribute(group, "tags") == "local.group_tags"
    assert re.search(
        r"^\s*group_tags\s*=\s*merge\(local\.tags, var\.expires_on == null \? {} : "
        r'\{ "expires-on" = var\.expires_on \}\)$',
        file_text("main.tf"),
        flags=re.MULTILINE,
    )
    # No other resource gets it, and the others keep the three tags as they were.
    assert len(re.findall(r'"expires-on"\s*=', module_text())) == 1
    for name, other in resources().items():
        if name != PLATFORM_GROUP and has_attribute(other, "tags"):
            assert attribute(other, "tags") == "local.tags", name


def test_expires_on_says_it_is_a_label_and_nothing_is_removed_on_the_date() -> None:
    above = squeezed(
        comments_of("variables.tf").split("The day the environment is meant")[1]
    )

    assert "It is a label: nothing deletes anything on that date" in above
    assert "Absent (the default) means no tag" in above
