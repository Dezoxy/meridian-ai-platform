"""The Azure platform module's cluster, registry and control-plane log (S020, Z2).

``cluster.tf``, ``registry.tf`` and ``logs.tf`` hold the cluster and its identity,
the registry and the workspace that takes the cluster's audit log. They are
checked by ``terraform validate`` and by these tests, and they are never planned
and never applied: no sign-in is made or looked for, so nothing here has seen
Azure. What a test holds is what the text says, one decision to a test; a
setting that ``validate`` cannot check (a value's spelling, whether Azure accepts
it) is the README's, not proved here.
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
    nested_blocks,
    own_text,
    raw_text,
    resources,
    resources_in,
    squeezed,
)
from test_azure_module import ADDRESS, GUID, PIN, PLATFORM_GROUP, PRICE

CLUSTER = "azurerm_kubernetes_cluster.main"
IDENTITY = "azurerm_user_assigned_identity.cluster"
REGISTRY = "azurerm_container_registry.main"
WORKSPACE = "azurerm_log_analytics_workspace.main"
DIAGNOSTIC = "azurerm_monitor_diagnostic_setting.cluster"
NODES_SUBNET = "azurerm_subnet.nodes"
GROUP_LOCATION = "azurerm_resource_group.platform.location"
GROUP_NAME = "azurerm_resource_group.platform.name"

CLUSTER_RESOURCES = [
    CLUSTER,
    IDENTITY,
    "azurerm_role_assignment.cluster_network_contributor",
    "azurerm_role_assignment.operator_cluster_admin",
]
REGISTRY_RESOURCES = [REGISTRY, "azurerm_role_assignment.kubelet_acr_pull"]
LOGS_RESOURCES = [DIAGNOSTIC, WORKSPACE, "azurerm_monitor_diagnostic_setting.vault"]

# The only three role assignments the three files may hold: the role's name, the
# scope as written and the principal as written.
THE_THREE_ROLES = {
    "azurerm_role_assignment.cluster_network_contributor": (
        '"Network Contributor"',
        f"{NODES_SUBNET}.id",
        f"{IDENTITY}.principal_id",
    ),
    "azurerm_role_assignment.operator_cluster_admin": (
        '"Azure Kubernetes Service RBAC Cluster Admin"',
        f"{CLUSTER}.id",
        "data.azurerm_client_config.current.object_id",
    ),
    "azurerm_role_assignment.kubelet_acr_pull": (
        '"AcrPull"',
        f"{REGISTRY}.id",
        f"{CLUSTER}.kubelet_identity[0].object_id",
    ),
}
THE_THREE_FILES = ["cluster.tf", "registry.tf", "logs.tf"]


def cluster() -> str:
    return resources_in("cluster.tf")[CLUSTER]


def cluster_own() -> str:
    return own_text(cluster())


def only_block(body: str, name: str) -> str:
    (found,) = nested_blocks(body, name)
    return found


def role_assignments() -> dict[str, str]:
    return {
        name: body
        for file_name in THE_THREE_FILES
        for name, body in resources_in(file_name).items()
        if name.startswith("azurerm_role_assignment.")
    }


# ── what the three files declare ─────────────────────────────────────────────


def test_cluster_declares_exactly_these_resources_and_no_data_source() -> None:
    assert sorted(resources_in("cluster.tf")) == sorted(CLUSTER_RESOURCES)
    assert data_sources_in("cluster.tf") == {}


def test_registry_declares_exactly_these_resources_and_no_data_source() -> None:
    assert sorted(resources_in("registry.tf")) == sorted(REGISTRY_RESOURCES)
    assert data_sources_in("registry.tf") == {}


def test_logs_declares_exactly_these_resources_and_no_data_source() -> None:
    assert sorted(resources_in("logs.tf")) == sorted(LOGS_RESOURCES)
    assert data_sources_in("logs.tf") == {}


# ── the role assignments ─────────────────────────────────────────────────────


def test_the_three_files_hold_exactly_three_role_assignments_with_these_terms() -> None:
    found = {
        name: (
            attribute(body, "role_definition_name"),
            attribute(body, "scope"),
            attribute(body, "principal_id"),
        )
        for name, body in role_assignments().items()
    }

    assert found == THE_THREE_ROLES


def test_no_role_assignment_is_scoped_to_a_resource_group_or_a_subscription() -> None:
    for name, body in role_assignments().items():
        scope = attribute(body, "scope")
        assert scope is not None, name
        assert not re.search(r"resource_group|subscription", scope), (name, scope)
        assert scope.endswith(".id"), (name, scope)
        # The role is named, not given by an ID that could be another one.
        assert not has_attribute(body, "role_definition_id"), name


def test_no_other_role_assignment_exists_in_the_module_yet_than_these_three() -> None:
    # Z3 and Z4 add their own, and their tests hold them; this holds that the
    # three files of this contract add nothing but the three above.
    in_these_files = {
        name
        for file_name in THE_THREE_FILES
        for name in resources_in(file_name)
        if name.startswith("azurerm_role_assignment.")
    }

    assert in_these_files == set(THE_THREE_ROLES)


def test_the_control_planes_role_is_on_the_nodes_subnet_and_its_comment_says_why() -> (
    None
):
    comment = comments_of("cluster.tf")

    assert "nowhere wider" in comment
    assert "never granted to any identity of this module" in comment
    assert "customer manages" in comment


def test_the_cluster_waits_for_the_control_planes_role_on_the_subnet() -> None:
    depends = attribute(cluster(), "depends_on")

    assert depends == "[azurerm_role_assignment.cluster_network_contributor]"
    assert "role must be there first" in squeezed(comments_of("cluster.tf"))


# ── the identity of the control plane ────────────────────────────────────────


def test_the_control_planes_identity_is_a_named_user_assigned_one_in_the_group() -> (
    None
):
    body = resources_in("cluster.tf")[IDENTITY]

    assert attribute(body, "name") == '"id-${local.name_prefix}-aks"'
    assert attribute(body, "location") == GROUP_LOCATION
    assert attribute(body, "resource_group_name") == GROUP_NAME
    assert attribute(body, "tags") == "local.tags"


def test_the_cluster_runs_as_that_identity_and_no_other() -> None:
    identity = only_block(cluster(), "identity")

    assert attribute(identity, "type") == '"UserAssigned"'
    assert attribute(identity, "identity_ids") == f"[{IDENTITY}.id]"
    assert len(nested_blocks(cluster(), "identity")) == 1


# ── the cluster ──────────────────────────────────────────────────────────────


def test_the_cluster_is_named_and_placed_in_the_module_group_and_region() -> None:
    own = cluster_own()

    assert attribute(own, "name") == '"aks-${local.name_prefix}"'
    assert attribute(own, "location") == GROUP_LOCATION
    assert attribute(own, "resource_group_name") == GROUP_NAME
    assert attribute(own, "tags") == "local.tags"


def test_the_kubernetes_version_is_the_variable() -> None:
    assert attribute(cluster_own(), "kubernetes_version") == "var.kubernetes_version"


def test_the_control_plane_tier_is_free() -> None:
    assert attribute(cluster_own(), "sku_tier") == '"Free"'


def test_local_accounts_are_off() -> None:
    assert attribute(cluster_own(), "local_account_disabled") == "true"


def test_the_oidc_issuer_is_on() -> None:
    assert attribute(cluster_own(), "oidc_issuer_enabled") == "true"


def test_workload_identity_is_on() -> None:
    assert attribute(cluster_own(), "workload_identity_enabled") == "true"


def test_kubernetes_rbac_is_on_with_entra_and_azure_roles_for_kubernetes() -> None:
    entra = only_block(cluster(), "azure_active_directory_role_based_access_control")

    assert attribute(cluster_own(), "role_based_access_control_enabled") == "true"
    assert attribute(entra, "azure_rbac_enabled") == "true"
    assert attribute(entra, "tenant_id") == (
        "data.azurerm_client_config.current.tenant_id"
    )


def test_no_entra_group_is_named_as_an_administrator() -> None:
    entra = only_block(cluster(), "azure_active_directory_role_based_access_control")

    assert not has_attribute(entra, "admin_group_object_ids")
    assert "admin_group" not in file_text("cluster.tf")


def test_the_operator_is_a_cluster_admin_by_an_azure_role_on_the_cluster_only() -> None:
    body = resources_in("cluster.tf")["azurerm_role_assignment.operator_cluster_admin"]

    assert attribute(body, "principal_id") == (
        "data.azurerm_client_config.current.object_id"
    )
    assert attribute(body, "scope") == f"{CLUSTER}.id"


def test_the_cluster_has_no_automatic_upgrade_channel_and_the_node_images_is_none() -> (
    None
):
    own = cluster_own()

    # The provider's list for the cluster's channel has no "off" value: it is left
    # unset. The node image's has one, and the provider's default for it is not it.
    assert not has_attribute(own, "automatic_upgrade_channel")
    assert attribute(own, "node_os_upgrade_channel") == '"None"'


def test_the_upgrade_comment_says_a_day_a_surge_node_and_the_quota() -> None:
    comment = squeezed(comments_of("cluster.tf"))

    assert "The environment lives a day" in comment
    assert "adds a surge node while it runs" in comment
    assert "virtual CPU quota that a free trial subscription may not have" in comment


def test_the_azure_policy_add_on_is_off_and_one_sentence_says_why() -> None:
    assert attribute(cluster_own(), "azure_policy_enabled") == "false"
    assert "The Azure Policy add-on is off: it adds an admission webhook" in squeezed(
        comments_of("cluster.tf")
    )


def test_the_api_server_is_public_and_takes_its_ranges_from_the_variable() -> None:
    profile = only_block(cluster(), "api_server_access_profile")

    assert attribute(cluster_own(), "private_cluster_enabled") == "false"
    assert attribute(profile, "authorized_ip_ranges") == (
        "var.api_server_authorized_ip_ranges"
    )
    # Nothing else is a source of a range: the argument has no other value, no
    # address is written in the module's text, and the variable is read once.
    assert re.findall(r"authorized_ip_ranges\s*=", file_text("cluster.tf")) == [
        "authorized_ip_ranges ="
    ]
    assert not ADDRESS.search(profile)
    for file_name in THE_THREE_FILES:
        assert "/32" not in file_text(file_name)
    assert (
        sum(
            body.count("var.api_server_authorized_ip_ranges")
            for body in resources().values()
        )
        == 1
    )


def test_the_default_pool_is_the_only_pool_and_it_is_named_system() -> None:
    pool = only_block(cluster(), "default_node_pool")

    assert attribute(pool, "name") == '"system"'
    assert "azurerm_kubernetes_cluster_node_pool" not in module_text_of_the_three()


def test_the_pool_takes_its_size_and_count_from_the_variables() -> None:
    pool = only_block(cluster(), "default_node_pool")

    assert attribute(pool, "vm_size") == "var.node_vm_size"
    assert attribute(pool, "node_count") == "var.node_count"


def test_the_pool_sits_in_the_nodes_subnet() -> None:
    pool = only_block(cluster(), "default_node_pool")

    assert attribute(pool, "vnet_subnet_id") == f"{NODES_SUBNET}.id"


def test_the_pool_allows_fifty_pods_a_node_and_the_comment_has_the_arithmetic() -> None:
    pool = only_block(cluster(), "default_node_pool")
    comment = squeezed(comments_of("cluster.tf"))

    assert attribute(pool, "max_pods") == "50"
    assert "max_pods is 50" in comment
    assert "20 MB a pod plus 50 MB" in comment
    assert "1050 MB" in comment
    assert "a quarter of the node's memory" in comment
    assert "about 7 GiB" in comment


def test_workloads_are_allowed_on_the_pool_and_it_has_a_temporary_name() -> None:
    pool = only_block(cluster(), "default_node_pool")

    assert attribute(pool, "only_critical_addons_enabled") == "false"
    assert attribute(pool, "temporary_name_for_rotation") == '"systemtmp"'


def test_the_pool_keeps_the_provider_as_the_only_source_of_nodes() -> None:
    profile = only_block(cluster(), "node_provisioning_profile")

    assert attribute(profile, "mode") == '"Manual"'
    assert not has_attribute(profile, "default_node_pools")
    pool = only_block(cluster(), "default_node_pool")
    assert not has_attribute(pool, "auto_scaling_enabled")
    assert not has_attribute(pool, "min_count") and not has_attribute(pool, "max_count")


def test_the_network_is_azure_cni_overlay_with_cilium_as_data_plane_and_policy() -> (
    None
):
    profile = only_block(cluster(), "network_profile")

    assert attribute(profile, "network_plugin") == '"azure"'
    assert attribute(profile, "network_plugin_mode") == '"overlay"'
    assert attribute(profile, "network_data_plane") == '"cilium"'
    assert attribute(profile, "network_policy") == '"cilium"'


def test_the_three_ranges_come_from_the_address_plans_locals() -> None:
    profile = only_block(cluster(), "network_profile")

    assert attribute(profile, "pod_cidr") == "local.pod_cidr"
    assert attribute(profile, "service_cidr") == "local.service_cidr"
    assert attribute(profile, "dns_service_ip") == "local.dns_service_ip"


def test_the_load_balancer_is_standard_and_carries_the_outbound_traffic() -> None:
    profile = only_block(cluster(), "network_profile")

    assert attribute(profile, "load_balancer_sku") == '"standard"'
    assert attribute(profile, "outbound_type") == '"loadBalancer"'
    assert "load_balancer_profile" not in profile


def test_the_network_profile_holds_exactly_these_nine_arguments_and_no_block() -> None:
    profile = only_block(cluster(), "network_profile")
    names = re.findall(r"^    (\w+)\s*=", profile, flags=re.MULTILINE)

    assert sorted(names) == sorted(
        [
            "network_plugin",
            "network_plugin_mode",
            "network_data_plane",
            "network_policy",
            "pod_cidr",
            "service_cidr",
            "dns_service_ip",
            "load_balancer_sku",
            "outbound_type",
        ]
    )
    assert not re.search(r"^    \w+ \{", profile, flags=re.MULTILINE)


def test_no_advanced_networking_block_is_written() -> None:
    assert nested_blocks(cluster(), "advanced_networking") == []
    assert "advanced_networking" not in file_text("cluster.tf")
    assert "price is not established" in squeezed(comments_of("cluster.tf"))


@pytest.mark.parametrize(
    "left_out",
    [
        "oms_agent",
        "key_vault_secrets_provider",
        "http_application_routing_enabled",
        "ingress_application_gateway",
        "web_app_routing",
        "microsoft_defender",
        "service_mesh_profile",
        "azurerm_public_ip",
    ],
)
def test_the_cluster_leaves_out_what_a_later_contract_decides_or_nobody_asked_for(
    left_out: str,
) -> None:
    assert left_out not in file_text("cluster.tf")


# ── the dependency on the pin ────────────────────────────────────────────────


def module_text_of_the_three() -> str:
    return "\n".join(file_text(name) for name in THE_THREE_FILES)


def references_of(body: str) -> set[str]:
    return {
        f"{kind}.{name}"
        for kind, name in re.findall(r"\b([a-z_]+)\.([a-z_]+)\b", body)
        if f"{kind}.{name}" in resources()
    }


def reaches_the_pin(name: str, seen: frozenset[str] = frozenset()) -> bool:
    """Whether a resource names the pin or the group that waits for it, or names
    a resource that does, however long the chain."""
    if name in (PIN, PLATFORM_GROUP):
        return True
    return any(
        reaches_the_pin(other, seen | {name})
        for other in references_of(resources()[name]) - seen - {name}
    )


@pytest.mark.parametrize(
    "name", CLUSTER_RESOURCES + REGISTRY_RESOURCES + LOGS_RESOURCES
)
def test_every_resource_of_the_three_files_hangs_on_the_pin_through_the_group(
    name: str,
) -> None:
    assert reaches_the_pin(name)


@pytest.mark.parametrize(
    "name",
    [
        "azurerm_role_assignment.cluster_network_contributor",
        "azurerm_role_assignment.operator_cluster_admin",
        "azurerm_role_assignment.kubelet_acr_pull",
        DIAGNOSTIC,
    ],
)
def test_a_resource_that_names_no_group_says_in_the_text_that_it_waits_for_it(
    name: str,
) -> None:
    body = resources()[name]

    assert attribute(body, "depends_on") == f"[{PLATFORM_GROUP}]"
    assert "depends_on on it" in squeezed(comments_of("cluster.tf"))


def test_the_chain_walk_can_tell_a_resource_that_hangs_from_one_that_does_not() -> None:
    # A walk that reaches everything proves nothing: the pin and the group do
    # reach it, and a name that is not in the module reaches nothing.
    assert reaches_the_pin(PIN) and reaches_the_pin(PLATFORM_GROUP)
    assert references_of("scope = azurerm_subnet.nodes.id") == {NODES_SUBNET}
    assert references_of("scope = data.azurerm_client_config.current.id") == set()


# ── the registry ─────────────────────────────────────────────────────────────


def test_the_registry_is_named_with_the_suffix_and_the_comment_says_why() -> None:
    body = resources_in("registry.tf")[REGISTRY]
    name = attribute(body, "name")

    assert name == '"crmeridian${local.suffix}"'
    assert re.fullmatch(r'"[a-z0-9]+\$\{local\.suffix\}"', name)
    comment = squeezed(comments_of("registry.tf"))
    assert "global, letters and digits only" in comment
    assert "keep the name free of a clash" in comment


def test_the_registry_is_in_the_module_group_and_region() -> None:
    body = resources_in("registry.tf")[REGISTRY]

    assert attribute(body, "location") == GROUP_LOCATION
    assert attribute(body, "resource_group_name") == GROUP_NAME
    assert attribute(body, "tags") == "local.tags"


def test_the_registry_is_basic_with_the_admin_user_off() -> None:
    body = resources_in("registry.tf")[REGISTRY]

    assert attribute(body, "sku") == '"Basic"'
    assert attribute(body, "admin_enabled") == "false"


def test_the_registry_sets_no_anonymous_pull_and_says_basic_has_none() -> None:
    # Z6 wrote it false and Z6b took it out again: Basic has no anonymous pull and
    # an explicit false on Basic is not established. The fixes file holds the
    # argument's absence; this holds the sentence that says why there is none.
    body = resources_in("registry.tf")[REGISTRY]

    assert not has_attribute(body, "anonymous_pull_enabled")
    assert "Basic has no anonymous pull" in squeezed(comments_of("registry.tf"))


def test_the_registry_says_basic_has_no_private_endpoint_so_it_is_public() -> None:
    comment = squeezed(comments_of("registry.tf"))

    assert "Basic has no private endpoint either" in comment
    assert "the registry's endpoint is public" in comment
    assert "needs Entra authentication" in comment
    assert not has_attribute(
        resources_in("registry.tf")[REGISTRY], "public_network_access_enabled"
    )


def test_the_nodes_pull_with_the_kubelet_identity_and_the_role_is_acrpull() -> None:
    body = resources_in("registry.tf")["azurerm_role_assignment.kubelet_acr_pull"]

    assert attribute(body, "role_definition_name") == '"AcrPull"'
    assert attribute(body, "principal_id") == (
        f"{CLUSTER}.kubelet_identity[0].object_id"
    )
    assert attribute(body, "scope") == f"{REGISTRY}.id"


# ── the control-plane log ────────────────────────────────────────────────────


def test_the_workspace_is_per_gigabyte_for_thirty_days_in_the_module_group() -> None:
    body = resources_in("logs.tf")[WORKSPACE]

    assert attribute(body, "name") == '"log-${local.name_prefix}"'
    assert attribute(body, "sku") == '"PerGB2018"'
    assert attribute(body, "retention_in_days") == "30"
    assert attribute(body, "location") == GROUP_LOCATION
    assert attribute(body, "resource_group_name") == GROUP_NAME
    assert attribute(body, "tags") == "local.tags"


def test_the_workspaces_daily_cap_is_the_variable_and_nothing_else() -> None:
    body = resources_in("logs.tf")[WORKSPACE]

    assert attribute(body, "daily_quota_gb") == "var.log_daily_quota_gb"
    assert sum(b.count("var.log_daily_quota_gb") for b in resources().values()) == 1


def log_categories() -> list[str]:
    body = resources_in("logs.tf")[DIAGNOSTIC]
    return [
        attribute(found, "category") or ""
        for found in nested_blocks(body, "enabled_log")
    ]


def test_the_diagnostic_setting_sends_the_audit_admin_and_guard_categories() -> None:
    body = resources_in("logs.tf")[DIAGNOSTIC]

    assert attribute(body, "target_resource_id") == f"{CLUSTER}.id"
    assert attribute(body, "log_analytics_workspace_id") == f"{WORKSPACE}.id"
    assert sorted(log_categories()) == ['"guard"', '"kube-audit-admin"']


def test_kube_audit_is_not_sent_and_the_comment_says_why() -> None:
    assert '"kube-audit"' not in log_categories()
    comment = squeezed(comments_of("logs.tf"))
    assert "Microsoft's own cost note" in comment
    assert "substantial cost" in comment
    assert "kube-audit holds every one" in comment


def test_nothing_else_is_sent_and_no_other_destination_is_named() -> None:
    body = resources_in("logs.tf")[DIAGNOSTIC]

    assert len(nested_blocks(body, "enabled_log")) == 2
    assert nested_blocks(body, "enabled_metric") == []
    for argument in (
        "storage_account_id",
        "eventhub_authorization_rule_id",
        "eventhub_name",
        "partner_solution_id",
        "category_group",
    ):
        assert argument not in file_text("logs.tf")


# ── the variable's comment, and what no file may hold ────────────────────────


def test_the_size_variables_comment_says_what_is_known_and_what_needs_a_sign_in() -> (
    None
):
    above = raw_text("variables.tf").split('variable "node_vm_size"')[0].splitlines()
    block: list[str] = []
    for line in reversed(above):
        if not line.lstrip().startswith("#"):
            break
        block.insert(0, line)
    comment = squeezed("\n".join(block))

    assert "FACTS:" not in comment
    assert "priced Linux sizes in both regions" in comment
    assert "read 2026-10-07" in comment
    assert "NOT KNOWN from any page" in comment
    assert "whether a subscription is offered them" in comment
    assert "its vCPU quota" in comment
    assert "read with a sign-in before an apply" in comment


def test_the_three_files_state_no_guid_no_public_address_and_no_price() -> None:
    for file_name in THE_THREE_FILES:
        text = (MODULE_DIR / file_name).read_text(encoding="utf-8")
        assert not GUID.search(text), file_name
        assert not PRICE.search(text), file_name
        # No address at all: the ranges are the address plan's, in main.tf.
        assert not ADDRESS.search(text), file_name


def test_the_three_files_name_no_secret_and_no_credential() -> None:
    for file_name in THE_THREE_FILES:
        text = file_text(file_name)
        assert not re.search(r"password|client_secret|shared_key|access_key", text)
        assert "admin_enabled = true" not in text
