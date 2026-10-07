"""The Google Cloud module's own text (S078).

``infra/terraform/gcp`` is a scaffold: it is checked by ``terraform validate``
and by these tests, and it is never planned and never applied, so nothing here
has seen a Google Cloud API. What a test holds is what the text says, which is
the only proof there is, and each test is named for the sentence it holds.

``terraform validate`` does not evaluate a precondition, so the project pin is
held on the text alone: its data source, its comparison, its message and the
``depends_on`` line of every resource. The variables' validations do run, in
``terraform console`` on a scratch copy of ``variables.tf`` with no provider and
no project, skipped where Terraform is not installed (the pipeline has none
yet). Every project number, billing account and address here is made up: twelve
identical digits, the documentation's own shape with zeros, ``example-project``
and a documentation address.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from servicesupport import REPO_ROOT

MODULE_DIR = REPO_ROOT / "infra" / "terraform" / "gcp"
VARIABLES = MODULE_DIR / "variables.tf"
PROJECT_NUMBER = "111111111111"
BILLING_ACCOUNT = "000000-000000-000000"
# The shape of a billing account ID, except the documentation's own, all zeros.
NOT_THE_ZERO_ACCOUNT = r"\b(?!0{6}-0{6}-0{6}\b)[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}\b"
PROJECT_ID = "example-project"
OPERATOR_CIDR = "203.0.113.7/32"

# The Regions of Google Cloud in EU member states, with the country of each as
# Google's page "Regions and zones" prints it (read 2026-10-07,
# https://docs.cloud.google.com/compute/docs/regions-zones). The list is written
# from that page: the repository had none (ADR 7 names four of them and the two
# that are not).
EU_REGIONS = {
    "europe-central2": "Poland",
    "europe-north1": "Finland",
    "europe-north2": "Sweden",
    "europe-southwest1": "Spain",
    "europe-west1": "Belgium",
    "europe-west3": "Germany",
    "europe-west4": "Netherlands",
    "europe-west8": "Italy",
    "europe-west9": "France",
    "europe-west10": "Germany",
    "europe-west12": "Italy",
}
EU_MEMBER_STATES = {
    "Austria",
    "Belgium",
    "Bulgaria",
    "Croatia",
    "Cyprus",
    "Czechia",
    "Denmark",
    "Estonia",
    "Finland",
    "France",
    "Germany",
    "Greece",
    "Hungary",
    "Ireland",
    "Italy",
    "Latvia",
    "Lithuania",
    "Luxembourg",
    "Malta",
    "Netherlands",
    "Poland",
    "Portugal",
    "Romania",
    "Slovakia",
    "Slovenia",
    "Spain",
    "Sweden",
}
# In Google's "Europe" and not in the EU (ADR 7): London and Zurich.
NOT_IN_THE_EU = {"europe-west2": "England", "europe-west6": "Switzerland"}


def tf_files() -> list[Path]:
    return sorted(MODULE_DIR.glob("*.tf"))


def without_comments(text: str) -> str:
    return "\n".join(re.sub(r"\s#.*$|^\s*#.*$", "", line) for line in text.splitlines())


def module_text() -> str:
    """Every .tf file of the module, comments removed: a word in a comment is
    not a setting."""
    return without_comments(
        "\n".join(path.read_text(encoding="utf-8") for path in tf_files())
    )


def file_text(name: str) -> str:
    return without_comments((MODULE_DIR / name).read_text(encoding="utf-8"))


def top_level_blocks(text: str, kind: str) -> dict[str, str]:
    """The bodies of the top-level blocks of one kind (``resource``, ``data``,
    ``output``...), keyed by their labels joined with a dot. ``terraform fmt``
    puts a block's closing brace at the start of a line, and the module is held
    to it by the validate gate."""
    found = re.findall(
        rf'^{kind} ((?:"[^"]+"\s*)+)\{{\n(.*?)^\}}$',
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return {".".join(re.findall(r'"([^"]+)"', labels)): body for labels, body in found}


def resources() -> dict[str, str]:
    return top_level_blocks(module_text(), "resource")


def resources_of(resource_type: str) -> dict[str, str]:
    return {k: v for k, v in resources().items() if k.startswith(f"{resource_type}.")}


def one_resource(resource_type: str) -> str:
    (body,) = resources_of(resource_type).values()
    return body


def variable_blocks() -> dict[str, str]:
    return top_level_blocks(
        (MODULE_DIR / "variables.tf").read_text(encoding="utf-8"), "variable"
    )


def variable_block(name: str) -> str:
    return variable_blocks()[name]


def output_blocks() -> dict[str, str]:
    return top_level_blocks(module_text(), "output")


def quoted_list_in(text: str, anchor: str) -> list[str]:
    """The quoted strings of the list that ``contains([...], <anchor>)`` tests."""
    (items,) = re.findall(
        rf"contains\(\[(.*?)\],\s*{re.escape(anchor)}\)", text, flags=re.DOTALL
    )
    return re.findall(r'"([^"]+)"', items)


# ── the project pin ──────────────────────────────────────────────────────────


def test_the_pin_compares_the_projects_number_with_the_expected_variable() -> None:
    text = module_text()

    assert 'data "google_project" "current" {}' in text
    pin = top_level_blocks(text, "resource").get("terraform_data.project_pin")
    assert pin is not None
    (condition,) = re.findall(r"^\s*condition\s*=\s*(.+)$", pin, flags=re.MULTILINE)
    assert condition.strip() == (
        "data.google_project.current.number == var.expected_project_number"
    )
    assert re.search(r"^\s*precondition \{$", pin, flags=re.MULTILINE)


def test_the_pins_message_names_neither_the_project_nor_the_number_it_expected() -> (
    None
):
    pin = resources()["terraform_data.project_pin"]

    (message,) = re.findall(r"^\s*error_message\s*=\s*(.+)$", pin, flags=re.MULTILINE)

    assert "${" not in message
    assert "var." not in message
    assert "data." not in message
    assert not re.search(r"\d{6,}", message)


def test_every_resource_but_the_pin_names_the_pin_in_its_depends_on() -> None:
    found = resources()
    assert len(found) >= 15  # the whole module was read, not a part of it

    without = [
        name
        for name, body in found.items()
        if name != "terraform_data.project_pin"
        and not re.search(
            r"^\s*depends_on\s*=\s*\[[^\]]*\bterraform_data\.project_pin\b[^\]]*\]",
            body,
            flags=re.MULTILINE,
        )
    ]

    assert without == []


def test_the_pin_waits_for_nothing_and_the_data_source_is_read_with_no_arguments() -> (
    None
):
    text = module_text()

    assert "depends_on" not in resources()["terraform_data.project_pin"]
    # An empty block: the project is the provider's own, and nothing it waits on
    # could make the read come after the resources it guards.
    assert len(re.findall(r'^data "', text, flags=re.MULTILINE)) == 1
    assert 'data "google_project" "current" {}' in text


def test_the_expected_number_is_a_sensitive_variable_without_a_default() -> None:
    block = variable_block("expected_project_number")

    assert re.search(r"^\s*sensitive\s*=\s*true$", block, flags=re.MULTILINE)
    assert not re.search(r"^\s*default\s*=", block, flags=re.MULTILINE)


# ── variables ────────────────────────────────────────────────────────────────


def test_every_variable_has_a_description_and_a_validation() -> None:
    blocks = variable_blocks()
    assert len(blocks) >= 10

    for name, body in blocks.items():
        assert re.search(r"^\s*description\s*=", body, flags=re.MULTILINE), name
        assert re.search(r"^\s*validation \{$", body, flags=re.MULTILINE), name


@pytest.mark.parametrize(
    "name",
    ["project_id", "expected_project_number", "billing_account", "api_access_cidr"],
)
def test_what_names_the_owners_project_account_or_address_is_sensitive_with_no_default(
    name: str,
) -> None:
    block = variable_block(name)

    assert re.search(r"^\s*sensitive\s*=\s*true$", block, flags=re.MULTILINE)
    assert not re.search(r"^\s*default\s*=", block, flags=re.MULTILINE)


def test_the_sensitive_variables_and_those_with_defaults_are_these_and_no_more() -> (
    None
):
    blocks = variable_blocks()

    sensitive = {
        n
        for n, b in blocks.items()
        if re.search(r"^\s*sensitive\s*=\s*true$", b, flags=re.MULTILINE)
    }
    with_default = {
        n
        for n, b in blocks.items()
        if re.search(r"^\s*default\s*=", b, flags=re.MULTILINE)
    }

    assert sensitive == {
        "project_id",
        "expected_project_number",
        "billing_account",
        "api_access_cidr",
    }
    assert with_default == {
        "region",
        "node_machine_type",
        "node_count",
        "database_tier",
        "workload_namespace",
        "workload_service_account",
        "budget_monthly_limit",
    }


def test_the_regions_the_module_allows_are_the_eu_ones_google_lists() -> None:
    allowed = quoted_list_in(variable_block("region"), "var.region")

    assert sorted(allowed) == sorted(EU_REGIONS)
    assert set(EU_REGIONS.values()) <= EU_MEMBER_STATES


@pytest.mark.parametrize("region", sorted(NOT_IN_THE_EU))
def test_london_and_zurich_are_not_in_the_list_of_regions(region: str) -> None:
    block = variable_block("region")

    assert NOT_IN_THE_EU[region] not in EU_MEMBER_STATES
    assert region not in quoted_list_in(block, "var.region")
    # Named in the message, as the AWS module names the two it excludes.
    (message,) = re.findall(r"error_message\s*=\s*(.+)", block)
    assert region in message


def test_the_default_region_is_frankfurt_and_is_in_the_list() -> None:
    block = variable_block("region")
    (default,) = re.findall(r'^\s*default\s*=\s*"([^"]+)"$', block, re.MULTILINE)

    assert default == "europe-west3"
    assert default in quoted_list_in(block, "var.region")


def test_the_machine_types_and_the_database_tiers_are_closed_lists_of_two() -> None:
    machines = quoted_list_in(
        variable_block("node_machine_type"), "var.node_machine_type"
    )
    tiers = quoted_list_in(variable_block("database_tier"), "var.database_tier")

    assert machines == ["e2-standard-2", "e2-standard-4"]
    assert tiers == ["db-f1-micro", "db-g1-small"]
    for name in ("node_machine_type", "database_tier"):
        block = variable_block(name)
        (default,) = re.findall(r'^\s*default\s*=\s*"([^"]+)"$', block, re.MULTILINE)
        assert default in quoted_list_in(block, f"var.{name}")
        assert "cost ceiling" in block


def test_the_node_count_is_a_whole_number_from_one_to_five() -> None:
    block = variable_block("node_count")

    assert "var.node_count >= 1" in block
    assert "var.node_count <= 5" in block
    assert "floor(var.node_count) == var.node_count" in block


def test_the_budgets_amount_is_above_zero_and_at_most_five_hundred() -> None:
    block = variable_block("budget_monthly_limit")

    assert "var.budget_monthly_limit > 0" in block
    assert "var.budget_monthly_limit <= 500" in block


def test_no_variable_holds_a_real_looking_project_number_account_or_address() -> None:
    text = (MODULE_DIR / "variables.tf").read_text(encoding="utf-8")

    assert not re.search(r"\b\d{10,}\b", re.sub(r"\{\d+,?\d*\}", "", text))
    assert not re.search(NOT_THE_ZERO_ACCOUNT, text)
    assert not re.search(r"[\w.]+@[\w.]+\.\w+", text)


# ── provider and versions ────────────────────────────────────────────────────


def test_terraform_and_the_google_provider_are_pinned_and_the_state_stays_out() -> None:
    versions = file_text("versions.tf")

    assert 'required_version = "~> 1.16"' in versions
    assert 'source  = "hashicorp/google"' in versions
    assert re.search(r'version = "~> 8\.\d+"', versions)
    assert re.search(r'^  backend "local" \{\}$', versions, re.MULTILINE)
    assert "path" not in versions
    assert "hashicorp/random" not in versions


def test_the_provider_takes_project_and_region_from_variables_and_bills_it() -> None:
    (provider,) = top_level_blocks(module_text(), "provider").values()

    assert re.search(r"^\s*project\s*=\s*var\.project_id$", provider, re.MULTILINE)
    assert re.search(r"^\s*region\s*=\s*var\.region$", provider, re.MULTILINE)
    assert re.search(r"^\s*user_project_override\s*=\s*true$", provider, re.MULTILINE)
    assert re.search(
        r"^\s*billing_project\s*=\s*var\.project_id$", provider, re.MULTILINE
    )


def test_the_provider_is_given_no_credential_and_no_identity_to_impersonate() -> None:
    (provider,) = top_level_blocks(module_text(), "provider").values()

    for word in ("credentials", "access_token", "impersonate", "_endpoint"):
        assert word not in provider


def test_the_lock_file_names_the_google_provider_at_the_minor_the_module_pins() -> None:
    lock = (MODULE_DIR / ".terraform.lock.hcl").read_text(encoding="utf-8")
    constraint = re.search(r'version = "~> (8\.\d+)"', file_text("versions.tf"))
    assert constraint is not None

    assert 'provider "registry.terraform.io/hashicorp/google"' in lock
    assert f'constraints = "~> {constraint.group(1)}"' in lock
    assert re.search(rf'version\s+= "{re.escape(constraint.group(1))}\.\d+"', lock)
    assert "hashicorp/aws" not in lock


def test_the_lock_file_holds_the_two_platform_hashes_the_aws_lock_has() -> None:
    # `terraform providers lock -platform=linux_amd64 -platform=darwin_arm64`
    # writes one h1: hash for each platform and the zh: hashes of the archives;
    # the platform is not in the hash, so the count is what can be held.
    lock = (MODULE_DIR / ".terraform.lock.hcl").read_text(encoding="utf-8")
    aws_lock = (MODULE_DIR.parent / "aws" / ".terraform.lock.hcl").read_text(
        encoding="utf-8"
    )

    assert len(re.findall(r'"h1:', lock)) == len(re.findall(r'"h1:', aws_lock)) == 2
    assert len(re.findall(r'"zh:', lock)) >= 2


def test_no_variable_file_override_file_or_state_sits_in_the_modules_directory() -> (
    None
):
    names = {path.name for path in MODULE_DIR.iterdir()}

    for name in names:
        assert not name.endswith((".tfvars", ".tfvars.json", ".tfstate", ".tfplan"))
        assert not name.endswith("_override.tf")
        assert name != "override.tf"
        assert not name.startswith("terraform.tfstate")


def test_the_module_calls_no_module_and_uses_no_kubernetes_or_helm_provider() -> None:
    text = module_text()

    assert not re.search(r'^\s*module\s+("[^"]+"|\w+)', text, re.MULTILINE)
    assert not re.search(r'provider\s+"(kubernetes|helm|kubectl)"', text)
    assert "hashicorp/kubernetes" not in text
    assert "hashicorp/helm" not in text


# ── the APIs ─────────────────────────────────────────────────────────────────

APIS_BY_RESOURCE_PREFIX = {
    "google_compute_": "compute.googleapis.com",
    "google_container_": "container.googleapis.com",
    "google_sql_": "sqladmin.googleapis.com",
    "google_artifact_registry_": "artifactregistry.googleapis.com",
    "google_secret_manager_": "secretmanager.googleapis.com",
    "google_service_account": "iam.googleapis.com",
    "google_project_iam_": "cloudresourcemanager.googleapis.com",
    "google_billing_budget": "billingbudgets.googleapis.com",
}


def enabled_apis() -> list[str]:
    body = one_resource("google_project_service")
    (items,) = re.findall(r"for_each\s*=\s*toset\(\[(.*?)\]\)", body, flags=re.DOTALL)
    return re.findall(r'"([^"]+\.googleapis\.com)"', items)


def test_each_api_a_resource_needs_is_enabled_and_no_other() -> None:
    needed = {
        api
        for name in resources()
        for prefix, api in APIS_BY_RESOURCE_PREFIX.items()
        if name.startswith(prefix)
    }

    assert set(enabled_apis()) == needed
    assert len(enabled_apis()) == len(set(enabled_apis()))


def test_no_api_is_switched_off_when_the_module_is_removed() -> None:
    body = one_resource("google_project_service")

    assert re.search(r"^\s*disable_on_destroy\s*=\s*false$", body, flags=re.MULTILINE)


def test_the_apis_that_private_service_connect_made_needless_are_not_enabled() -> None:
    apis = enabled_apis()

    assert "servicenetworking.googleapis.com" not in apis  # PSC: no peering
    assert "aiplatform.googleapis.com" not in apis  # model access is no resource


# ── network ──────────────────────────────────────────────────────────────────


def test_the_network_has_no_automatic_subnets_and_one_subnet_with_the_two_ranges() -> (
    None
):
    network = one_resource("google_compute_network")
    subnet = one_resource("google_compute_subnetwork")

    assert re.search(r"auto_create_subnetworks\s*=\s*false", network)
    assert re.search(r"private_ip_google_access\s*=\s*true", subnet)
    assert len(re.findall(r"^\s*secondary_ip_range \{$", subnet, re.MULTILINE)) == 2
    assert re.search(r'range_name\s*=\s*"pods"', subnet)
    assert re.search(r'range_name\s*=\s*"services"', subnet)
    assert re.search(r"region\s*=\s*var\.region", subnet)


def test_nodes_have_no_external_address_and_reach_the_internet_through_cloud_nat() -> (
    None
):
    cluster = one_resource("google_container_cluster")

    assert re.search(r"enable_private_nodes\s*=\s*true", cluster)
    assert len(resources_of("google_compute_router")) == 1
    assert len(resources_of("google_compute_router_nat")) == 1
    assert "external_ip" not in module_text()


def test_the_module_writes_no_firewall_rule_of_its_own() -> None:
    assert [n for n in resources() if n.startswith("google_compute_firewall")] == []


# ── cluster ──────────────────────────────────────────────────────────────────


def test_there_is_one_zonal_cluster_one_node_pool_and_one_subnet() -> None:
    assert len(resources_of("google_container_cluster")) == 1
    assert len(resources_of("google_container_node_pool")) == 1
    assert len(resources_of("google_compute_subnetwork")) == 1
    cluster = one_resource("google_container_cluster")
    assert re.search(r"^\s*location\s*=\s*local\.zone$", cluster, re.MULTILINE)
    assert "node_locations" not in cluster
    (zone,) = re.findall(r"^\s*zone\s*=\s*(.+)$", file_text("main.tf"), re.MULTILINE)
    assert zone.strip() == '"${var.region}-a"'


def test_the_cluster_chooses_dataplane_v2_workload_identity_and_the_gateway_api() -> (
    None
):
    cluster = one_resource("google_container_cluster")

    assert re.search(r'datapath_provider\s*=\s*"ADVANCED_DATAPATH"', cluster)
    assert re.search(
        r'workload_pool\s*=\s*"\$\{var\.project_id\}\.svc\.id\.goog"', cluster
    )
    gateway = re.search(r"gateway_api_config \{\n(.*?)\n\s*\}", cluster, re.DOTALL)
    assert gateway is not None
    assert 'channel = "CHANNEL_STANDARD"' in gateway.group(1)
    assert "network_policy" not in cluster  # Dataplane V2 enforces the chart's


def test_the_node_pool_serves_workload_identity_from_the_metadata_server() -> None:
    pool = one_resource("google_container_node_pool")

    assert re.search(r'mode\s*=\s*"GKE_METADATA"', pool)
    assert re.search(r"node_count\s*=\s*var\.node_count", pool)
    assert re.search(r"machine_type\s*=\s*var\.node_machine_type", pool)
    assert re.search(r"service_account\s*=\s*google_service_account\.node\.email", pool)


def test_the_control_plane_is_reachable_from_one_variable_range_only() -> None:
    cluster = one_resource("google_container_cluster")

    (blocks,) = re.findall(r"cidr_blocks \{\n(.*?)\n\s*\}", cluster, re.DOTALL)
    # A variable, never a literal: the scanner's check for a public range fires
    # on an address written in the file, and the owner's address is not in it.
    assert re.search(r"cidr_block\s*=\s*var\.api_access_cidr$", blocks, re.MULTILINE)
    assert len(re.findall(r"^\s*cidr_block\s*=", cluster, re.MULTILINE)) == 1
    assert re.search(r"gcp_public_cidrs_access_enabled\s*=\s*false", cluster)


def test_the_node_service_account_has_the_one_role_adr_7_names() -> None:
    members = resources_of("google_project_iam_member")
    (body,) = members.values()

    assert re.search(
        r'^\s*role\s*=\s*"roles/container\.defaultNodeServiceAccount"$',
        body,
        flags=re.MULTILINE,
    )
    assert "google_service_account.node.member" in body
    assert len(resources_of("google_service_account")) == 1
    assert not re.search(r"google_project_iam_(binding|policy)", module_text())
    assert not re.search(r"roles/(owner|editor|viewer)", module_text())


@pytest.mark.parametrize("resource_type", ["google_container_cluster"])
def test_deletion_protection_is_written_false_on_the_cluster(
    resource_type: str,
) -> None:
    assert re.search(
        r"^\s*deletion_protection\s*=\s*false$",
        one_resource(resource_type),
        re.MULTILINE,
    )


# ── registry ─────────────────────────────────────────────────────────────────


def test_the_registry_is_one_docker_repository_in_the_region_with_immutable_tags() -> (
    None
):
    repository = one_resource("google_artifact_registry_repository")

    assert re.search(r'format\s*=\s*"DOCKER"', repository)
    assert re.search(r"location\s*=\s*var\.region", repository)
    assert re.search(r"immutable_tags\s*=\s*true", repository)
    assert "allUsers" not in module_text()
    assert "allAuthenticatedUsers" not in module_text()


# ── database ─────────────────────────────────────────────────────────────────


def test_the_database_is_postgresql_17_in_the_enterprise_edition_written_out() -> None:
    database = one_resource("google_sql_database_instance")

    assert re.search(r'database_version\s*=\s*"POSTGRES_17"', database)
    assert re.search(r'^\s*edition\s*=\s*"ENTERPRISE"$', database, re.MULTILINE)
    assert re.search(r"tier\s*=\s*var\.database_tier", database)
    assert re.search(r"region\s*=\s*var\.region", database)


def test_backups_and_point_in_time_recovery_are_written_out_not_left_to_a_default() -> (
    None
):
    database = one_resource("google_sql_database_instance")

    (backup,) = re.findall(
        r"backup_configuration \{\n(.*?)\n    \}", database, re.DOTALL
    )
    assert re.search(r"^\s*enabled\s*=\s*true$", backup, re.MULTILINE)
    assert re.search(
        r"^\s*point_in_time_recovery_enabled\s*=\s*true$", backup, re.MULTILINE
    )


def test_the_database_has_no_public_address_and_requires_tls() -> None:
    database = one_resource("google_sql_database_instance")

    assert re.search(r"^\s*ipv4_enabled\s*=\s*false$", database, re.MULTILINE)
    assert "authorized_networks" not in database
    assert "private_network" not in database
    assert re.search(r'ssl_mode\s*=\s*"ENCRYPTED_ONLY"', database)


def test_the_database_is_reached_by_private_service_connect_and_not_by_peering() -> (
    None
):
    database = one_resource("google_sql_database_instance")
    address = one_resource("google_compute_address")
    rule = one_resource("google_compute_forwarding_rule")

    assert re.search(r"psc_enabled\s*=\s*true", database)
    assert re.search(r"allowed_consumer_projects\s*=\s*\[var\.project_id\]", database)
    assert re.search(r'address_type\s*=\s*"INTERNAL"', address)
    assert re.search(
        r"target\s*=\s*google_sql_database_instance\.main\.psc_service_attachment_link",
        rule,
    )
    assert re.search(r"ip_address\s*=\s*google_compute_address\.", rule)
    assert re.search(r'load_balancing_scheme\s*=\s*""', rule)
    text = module_text()
    assert "google_service_networking_connection" not in text
    assert "google_compute_global_address" not in text


def test_the_instance_name_comes_from_the_provider_as_a_name_is_not_reusable() -> None:
    database = one_resource("google_sql_database_instance")

    assert not re.search(r"^\s*name\s*=", database, re.MULTILINE)


def test_deletion_protection_is_written_false_on_the_instance_in_both_places() -> None:
    database = one_resource("google_sql_database_instance")

    assert re.search(r"^\s*deletion_protection\s*=\s*false$", database, re.MULTILINE)
    assert re.search(
        r"^\s*deletion_protection_enabled\s*=\s*false$", database, re.MULTILINE
    )


def test_the_module_makes_no_database_user_and_holds_no_password() -> None:
    text = module_text()

    assert "google_sql_user" not in text
    assert 'google_sql_database"' not in text
    assert not re.search(r"password", text, re.IGNORECASE)
    assert "random_password" not in text


# ── identity ─────────────────────────────────────────────────────────────────


def test_the_secret_is_regional_and_has_no_version() -> None:
    text = module_text()
    secret = one_resource("google_secret_manager_regional_secret")

    assert re.search(r"location\s*=\s*var\.region", secret)
    assert 'resource "google_secret_manager_secret"' not in text
    assert "secret_version" not in text
    assert "replication" not in text


def test_one_binding_on_the_secret_names_one_namespace_and_one_service_account() -> (
    None
):
    text = module_text()
    bindings = [n for n in resources() if n.startswith("google_secret_manager_")]
    member = one_resource("google_secret_manager_regional_secret_iam_member")

    # The _iam_member form adds one member and removes nothing: the _iam_binding
    # form owns a whole role and the _iam_policy form owns the whole policy.
    assert len(bindings) == 2  # the secret and its one binding
    assert "_iam_binding" not in text
    assert "_iam_policy" not in text
    assert re.search(r'role\s*=\s*"roles/secretmanager\.secretAccessor"', member)
    assert (
        "/subject/ns/${var.workload_namespace}/sa/${var.workload_service_account}"
        in member
    )
    assert (
        "principal://iam.googleapis.com/projects/${var.expected_project_number}"
        in member
    )
    assert "principalSet" not in member  # one ServiceAccount, not a set of them
    assert "*" not in member


def test_the_workload_names_default_to_the_ones_the_aws_module_uses() -> None:
    for name, default in (
        ("workload_namespace", "meridian"),
        ("workload_service_account", "model-gateway"),
    ):
        assert f'default     = "{default}"' in variable_block(name)


# ── budget ───────────────────────────────────────────────────────────────────


def test_one_budget_for_this_project_alerts_at_three_thresholds() -> None:
    budget = one_resource("google_billing_budget")

    assert re.search(r"billing_account\s*=\s*var\.billing_account", budget)
    assert 'projects = ["projects/${var.expected_project_number}"]' in budget
    assert re.search(r"toset\(\[0\.5, 0\.8, 1(\.0)?\]\)", budget)
    assert re.search(r'spend_basis\s*=\s*"CURRENT_SPEND"', budget)
    assert "FORECASTED_SPEND" not in budget
    assert re.search(r"units\s*=\s*tostring\(var\.budget_monthly_limit\)", budget)


def test_the_budget_names_no_currency_and_no_recipient_of_its_own() -> None:
    # The provider's page: currency_code is optional and, if given, must match
    # the billing account's. The module cannot know it, so it does not say.
    budget = one_resource("google_billing_budget")

    assert "currency_code" not in budget
    assert "all_updates_rule" not in budget
    assert "notification" not in budget
    assert "@" not in budget


# ── outputs ──────────────────────────────────────────────────────────────────


def test_the_outputs_are_the_region_cluster_registry_database_and_secret() -> None:
    assert sorted(output_blocks()) == [
        "cluster_endpoint",
        "cluster_name",
        "database_address",
        "region",
        "repository_name",
        "workload_secret_name",
    ]


def test_no_output_is_sensitive_or_holds_the_project_number_or_account() -> None:
    assert len(output_blocks()) == 6
    for name, body in output_blocks().items():
        assert "sensitive" not in body, name
        for private in (
            "var.project_id",
            "var.expected_project_number",
            "var.billing_account",
            "var.api_access_cidr",
            "data.google_project",
            "connection_name",
            "self_link",
            "member",
        ):
            assert private not in body, f"{name}: {private}"


def test_no_file_of_the_module_holds_a_project_number_an_account_or_an_address() -> (
    None
):
    for path in [*tf_files(), MODULE_DIR / "README.md"]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(NOT_THE_ZERO_ACCOUNT, text), path.name
        assert not re.search(r"[\w.+-]+@(?!example\.)[\w-]+\.\w+", text), path.name
        assert not re.search(r"\b(?<![.\d])\d{11,13}\b(?![.\d])", text), path.name


def test_the_directory_has_a_readme_that_says_what_it_is() -> None:
    text = (MODULE_DIR / "README.md").read_text(encoding="utf-8")

    assert "never planned" in text
    assert "never applied" in text


# ── the validations, run by terraform console ────────────────────────────────

needs_terraform = pytest.mark.skipif(
    shutil.which("terraform") is None, reason="terraform is not installed"
)
VALID = {
    "project_id": PROJECT_ID,
    "expected_project_number": PROJECT_NUMBER,
    "billing_account": BILLING_ACCOUNT,
    "api_access_cidr": OPERATOR_CIDR,
}
REFUSED = "Invalid value for variable"


def evaluate(tmp_path: Path, name: str, value: str) -> str:
    """Everything `terraform console` prints for var.<name> set to the value,
    the other required variables valid. The scratch copy holds variables.tf and
    nothing else: no provider, no project, no credential."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    shutil.copy2(VARIABLES, scratch / "variables.tf")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        **{f"TF_VAR_{k}": v for k, v in {**VALID, name: value}.items()},
    }
    done = subprocess.run(
        ["terraform", f"-chdir={scratch}", "console", "-no-color"],
        input=f"var.{name}\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    return done.stdout + done.stderr


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("project_id", "example-project"),
        ("project_id", "a23456"),
        ("project_id", "a" + "b" * 28 + "c"),
        ("expected_project_number", "111111111111"),
        ("expected_project_number", "1234567890"),
        ("billing_account", "000000-000000-000000"),
        ("billing_account", "01A2B3-C4D5E6-F70819"),
        *[("region", region) for region in sorted(EU_REGIONS)],
        ("api_access_cidr", "203.0.113.7/32"),
        ("api_access_cidr", "8.8.8.8/32"),
        ("node_machine_type", "e2-standard-2"),
        ("node_machine_type", "e2-standard-4"),
        ("database_tier", "db-f1-micro"),
        ("database_tier", "db-g1-small"),
        ("node_count", "1"),
        ("node_count", "5"),
        ("workload_namespace", "meridian"),
        ("workload_service_account", "model-gateway"),
        ("budget_monthly_limit", "1"),
        ("budget_monthly_limit", "25"),
        ("budget_monthly_limit", "500"),
    ],
)
def test_the_values_the_module_allows_are_accepted(
    tmp_path: Path, name: str, value: str
) -> None:
    assert REFUSED not in evaluate(tmp_path, name, value)


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("project_id", ""),
        ("project_id", "Example-Project"),
        ("project_id", "example_project"),
        ("project_id", "1example"),
        ("project_id", "short"),
        ("project_id", "example-"),
        ("project_id", "a" + "b" * 29 + "c"),
        ("expected_project_number", ""),
        ("expected_project_number", "12345"),
        ("expected_project_number", "11111111111a"),
        ("expected_project_number", "example-project"),
        ("billing_account", ""),
        ("billing_account", "000000-000000"),
        ("billing_account", "000000000000000000"),
        ("billing_account", "00000g-000000-000000"),
        ("billing_account", "01a2b3-c4d5e6-f70819"),
        ("region", "europe-west2"),
        ("region", "europe-west6"),
        ("region", "us-central1"),
        ("region", "europe-west3-a"),
        ("region", "europe"),
        ("region", "eu"),
        ("region", ""),
        ("node_machine_type", "n2-standard-64"),
        ("node_machine_type", "e2-medium"),
        ("node_machine_type", ""),
        ("database_tier", "db-custom-96-368640"),
        ("database_tier", "db-n1-standard-1"),
        ("database_tier", ""),
        ("node_count", "0"),
        ("node_count", "6"),
        ("node_count", "2.5"),
        ("node_count", "-1"),
        ("workload_namespace", "Meridian"),
        ("workload_namespace", "-meridian"),
        ("workload_namespace", "a" * 64),
        ("workload_namespace", ""),
        ("workload_service_account", "Model_Gateway"),
        ("workload_service_account", "model-gateway-"),
        ("workload_service_account", ""),
        ("budget_monthly_limit", "0"),
        ("budget_monthly_limit", "-5"),
        ("budget_monthly_limit", "501"),
        ("budget_monthly_limit", "25.5"),
    ],
)
def test_a_value_the_module_does_not_allow_is_refused_with_its_own_sentence(
    tmp_path: Path, name: str, value: str
) -> None:
    printed = evaluate(tmp_path, name, value)

    assert REFUSED in printed
    assert f"{name} must" in printed


@needs_terraform
@pytest.mark.parametrize(
    "value",
    [
        "203.0.113.7/32",
        "100.63.255.255/32",
        "126.255.255.255/32",
        "169.253.1.1/32",
        "172.15.255.255/32",
        "172.32.0.1/32",
        "192.167.1.1/32",
        "223.255.255.255/32",
    ],
)
def test_a_public_ipv4_address_as_a_slash_32_is_accepted_for_the_control_plane(
    tmp_path: Path, value: str
) -> None:
    assert REFUSED not in evaluate(tmp_path, "api_access_cidr", value)


@needs_terraform
@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1/32",
        "10.0.0.1/32",
        "100.64.0.1/32",
        "169.254.169.254/32",
        "172.16.0.1/32",
        "192.168.1.1/32",
        "0.0.0.0/32",
        "224.0.0.1/32",
        "203.0.113.0/24",
        "0.0.0.0/0",
        "203.0.113.7",
        "999.1.1.1/32",
        "010.1.1.1/32",
        "203.0.113.07/32",
        "2001:db8::1/32",
        "not-an-address",
        "",
    ],
)
def test_an_address_that_would_lock_the_owner_out_or_open_the_api_is_refused(
    tmp_path: Path, value: str
) -> None:
    printed = evaluate(tmp_path, "api_access_cidr", value)

    assert REFUSED in printed
    assert "api_access_cidr must be one public IPv4 address" in printed
    assert "Error: Invalid condition" not in printed  # the message is ours


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("project_id", "Example-Project"),
        ("expected_project_number", "11111111111a"),
        ("billing_account", "000000-000000"),
        ("api_access_cidr", "10.1.2.3/32"),
    ],
)
def test_a_refused_value_of_a_sensitive_variable_is_not_printed_back(
    tmp_path: Path, name: str, value: str
) -> None:
    printed = evaluate(tmp_path, name, value)

    assert REFUSED in printed
    assert value not in printed
