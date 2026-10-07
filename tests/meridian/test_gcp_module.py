"""The Google Cloud module's own text (S078).

``infra/terraform/gcp`` is a scaffold: it is checked by ``terraform validate``
and by these tests, and it is never planned and never applied, so nothing here
has seen a Google Cloud API. What a test holds is what the text says, which is
the only proof there is, and each test is named for the sentence it holds.

``terraform validate`` does not evaluate a precondition, so the project pin is
held on the text alone: its data source, its comparison, its message and the
``depends_on`` line of every resource. The variables' validations do run, in
``terraform console`` on a scratch copy of ``variables.tf`` with no provider and
no project, skipped where Terraform is not installed and failed under
``GITHUB_ACTIONS=true`` (the python workflow installs it). Every project
number, billing account and address here is made up: twelve identical digits,
the documentation's own shape with zeros, ``example-project`` and a
documentation address.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from servicesupport import REPO_ROOT
from terraformsupport import needs_terraform

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

# The zones each of those Regions has, in the order Google's page "Regions and
# zones" lists them (read 2026-10-07). St. Ghislain has no zone "a": it has b, c
# and d, which a zone built as "<region>-a" would not find.
PAGE_ZONES = {
    "europe-central2": ["a", "b", "c"],
    "europe-north1": ["a", "b", "c"],
    "europe-north2": ["a", "b", "c"],
    "europe-southwest1": ["a", "b", "c"],
    "europe-west1": ["b", "c", "d"],
    "europe-west3": ["a", "b", "c"],
    "europe-west4": ["a", "b", "c"],
    "europe-west8": ["a", "b", "c"],
    "europe-west9": ["a", "b", "c"],
    "europe-west10": ["a", "b", "c"],
    "europe-west12": ["a", "b", "c"],
}

# Every resource address the module declares: a resource added without the pin,
# or one that vanishes, changes this set and fails the test that holds it.
RESOURCE_ADDRESSES = [
    "google_artifact_registry_repository.main",
    "google_artifact_registry_repository_iam_member.nodes_pull",
    "google_billing_budget.monthly",
    "google_compute_address.database",
    "google_compute_forwarding_rule.database",
    "google_compute_network.main",
    "google_compute_router.main",
    "google_compute_router_nat.main",
    "google_compute_subnetwork.nodes",
    "google_container_cluster.main",
    "google_container_node_pool.main",
    "google_project_iam_member.node",
    "google_project_service.api",
    "google_secret_manager_regional_secret.workload",
    "google_secret_manager_regional_secret_iam_member.workload",
    "google_service_account.node",
    "google_sql_database_instance.main",
    "terraform_data.project_pin",
]


def tf_files() -> list[Path]:
    return sorted(MODULE_DIR.glob("*.tf"))


def without_comments(text: str) -> str:
    """The text with each ``#`` comment removed, line by line. A ``#`` inside a
    double-quoted string (a URL fragment, a message) is part of the string and
    stays; a backslash keeps the quote after it from ending the string."""
    return "\n".join(_without_comment(line) for line in text.splitlines())


def _without_comment(line: str) -> str:
    in_string = False
    escaped = False
    for index, char in enumerate(line):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "#":
            return line[:index].rstrip()
    return line


def module_text() -> str:
    """Every .tf file of the module, comments removed: a word in a comment is
    not a setting."""
    return without_comments(
        "\n".join(path.read_text(encoding="utf-8") for path in tf_files())
    )


def file_text(name: str) -> str:
    return without_comments((MODULE_DIR / name).read_text(encoding="utf-8"))


def raw_text(name: str) -> str:
    """A file as written, comments and all: for what a comment must say."""
    return (MODULE_DIR / name).read_text(encoding="utf-8")


def squeezed(text: str) -> str:
    """A comment wraps where it likes: the words, joined by single spaces and
    without the comment markers."""
    return " ".join(re.sub(r"^\s*#", "", text, flags=re.MULTILINE).split())


def comment_lines_of(name: str) -> str:
    """The lines of a file that are a comment and nothing else, squeezed: what a
    comment must say, with no code line to satisfy a word the code also holds."""
    lines = raw_text(name).splitlines()
    return squeezed("\n".join(line for line in lines if line.lstrip().startswith("#")))


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


# ── the helper that strips comments ──────────────────────────────────────────


def test_a_hash_inside_a_quoted_string_is_not_a_comment_and_is_kept() -> None:
    text = 'url = "https://example.com/#frag"  # a real comment\nname = "a # b"\n'

    stripped = without_comments(text)

    assert 'url = "https://example.com/#frag"' in stripped
    assert 'name = "a # b"' in stripped
    assert "real comment" not in stripped


def test_a_whole_line_comment_and_a_trailing_comment_are_removed() -> None:
    text = "# a whole line\n  # an indented one\nsize = 1 # trailing\nnext = 2\n"

    stripped = without_comments(text)

    assert "whole line" not in stripped
    assert "indented" not in stripped
    assert "trailing" not in stripped
    assert "size = 1" in stripped
    assert "next = 2" in stripped


def test_an_escaped_quote_does_not_end_the_string_the_hash_sits_in() -> None:
    text = 'a = "say \\"hi\\" # still inside"  # outside\n'

    stripped = without_comments(text)

    assert "still inside" in stripped
    assert "outside" not in stripped


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


def test_the_module_declares_exactly_these_resources() -> None:
    assert sorted(resources()) == RESOURCE_ADDRESSES


def test_no_resource_is_written_where_the_block_reader_cannot_see_it() -> None:
    # The reader needs "{", a newline and a closing brace at the start of a line.
    # `resource "terraform_data" "sneak" { input = "x" }` on one line matches none
    # of that, and `terraform fmt` leaves it alone: only a count of the lines that
    # START a resource sees it, and the reader's own count must equal it.
    started = re.findall(r'^resource "', module_text(), flags=re.MULTILINE)

    assert len(started) == len(RESOURCE_ADDRESSES)
    assert len(started) == len(resources())


def test_the_one_data_source_is_the_projects_and_no_other_is_read() -> None:
    # `data "google_project" "current" {}` is itself written on one line, so the
    # block reader cannot list it; the labels of the lines that start a data
    # source are what is counted.
    started = re.findall(
        r'^data ("[^"]+"\s+"[^"]+")', module_text(), flags=re.MULTILINE
    )

    assert started == ['"google_project" "current"']


def test_every_resource_but_the_pin_names_the_pin_in_its_depends_on() -> None:
    found = resources()
    assert sorted(found) == RESOURCE_ADDRESSES  # the whole module was read

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


def test_no_comment_says_a_plan_prints_no_project_id_or_number() -> None:
    overstated = re.compile(
        r"plan (does not print|never prints|prints (no|none|neither|nothing))",
        re.IGNORECASE,
    )

    for path in tf_files():
        assert not overstated.search(squeezed(path.read_text(encoding="utf-8"))), (
            path.name
        )


def test_the_comments_say_what_a_plan_does_print_and_that_nothing_redacts_it() -> None:
    main = squeezed(raw_text("main.tf"))
    variables = squeezed(raw_text("variables.tf"))

    # What is true: no VARIABLE's value is printed; Google's own resource IDs, the
    # data source's read line and a failed precondition name the project, and
    # there is no wrapper that redacts them.
    for comment in (main, variables):
        assert "no variable value is printed" in comment
        assert "nothing redacts" in comment
    assert "Read complete" in main
    assert "precondition" in main


def test_the_comment_on_the_data_source_says_which_apis_to_enable_by_hand_and_why() -> (
    None
):
    comment = squeezed(raw_text("main.tf"))

    assert "(README.md)" not in comment  # the sentence it pointed at did not exist
    assert "Cloud Resource Manager" in comment
    assert "Service Usage" in comment
    assert "by hand" in comment
    assert "before the first init" in comment
    # Why the module cannot do it: the enabling resource hangs on the pin, the pin
    # on the data source, and the data source on that API.
    assert "hangs on the pin" in comment
    assert "the pin on the data source" in comment
    assert "the data source on that API" in comment


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
    #
    # What this test can tell: the lock holds two h1: hashes and they are not the
    # same string. What it cannot tell: which platform each is for, or that a
    # hash is the provider's (a made-up h1: string passes, which was tried). A
    # hash is not pinned here on purpose: Renovate rewrites the lock, and a test
    # that held a value would fail on its first pull request.
    lock = (MODULE_DIR / ".terraform.lock.hcl").read_text(encoding="utf-8")
    aws_lock = (MODULE_DIR.parent / "aws" / ".terraform.lock.hcl").read_text(
        encoding="utf-8"
    )

    hashes = re.findall(r'"(h1:[^"]+)"', lock)
    assert len(hashes) == len(re.findall(r'"h1:', aws_lock)) == 2
    assert len(set(hashes)) == 2  # two platforms, not one hash written twice
    assert len(re.findall(r'"zh:', lock)) >= 2


def test_the_state_comment_says_a_bare_init_writes_the_state_in_this_directory() -> (
    None
):
    header = squeezed(raw_text("versions.tf"))

    # The block has no path, so a call that passes none writes terraform.tfstate
    # next to the files, in a worktree a session deletes (the failure aws.sh
    # exists to prevent). A by-hand apply starts with a path outside every
    # checkout.
    assert "terraform.tfstate" in header
    assert "in this directory" in header
    assert "init -backend-config=path=" in header
    assert "outside every checkout" in header
    assert "NOT one in this directory" not in header


def test_the_provider_constraint_comment_says_what_it_allows_and_what_runs() -> None:
    header = squeezed(raw_text("versions.tf"))

    # "~> 8.6" is the form the AWS module uses ("~> 6.67"): it allows any 8.x from
    # 8.6 and never 9; the lock holds the version a validate runs.
    assert 'version = "~> 8.6"' in raw_text("versions.tf")
    assert 'version = "~> 6.67"' in raw_text("../aws/versions.tf")
    assert "any 8.x from 8.6" in header
    assert "never 9" in header
    assert "-lockfile=readonly" in header
    assert "pinned to the minor" not in header


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
    assert zone.strip() == "local.zones[var.region]"


def module_zones() -> dict[str, str]:
    (body,) = re.findall(
        r"^\s*zones\s*=\s*\{\n(.*?)^\s*\}$",
        file_text("main.tf"),
        re.MULTILINE | re.DOTALL,
    )
    return dict(re.findall(r'"([^"]+)"\s*=\s*"([^"]+)"', body))


def test_the_zone_map_has_one_entry_for_every_region_of_the_list_and_no_other() -> None:
    allowed = quoted_list_in(variable_block("region"), "var.region")

    zones = module_zones()

    assert sorted(zones) == sorted(allowed) == sorted(EU_REGIONS)
    assert len(zones) == 11


def test_every_zone_of_the_map_begins_with_its_region_and_names_one_letter() -> None:
    for region, zone in module_zones().items():
        assert zone.startswith(f"{region}-"), region
        assert re.fullmatch(r"[a-z]", zone.removeprefix(f"{region}-")), region


def test_each_zone_is_the_first_one_the_page_lists_for_its_region() -> None:
    assert sorted(PAGE_ZONES) == sorted(EU_REGIONS)

    for region, zone in module_zones().items():
        assert zone == f"{region}-{PAGE_ZONES[region][0]}", region


def test_st_ghislain_is_not_given_a_zone_it_does_not_have() -> None:
    zones = module_zones()

    assert zones["europe-west1"] == "europe-west1-b"
    assert zones["europe-west1"] != "europe-west1-a"


def test_the_node_pool_and_the_cluster_are_both_in_the_one_zone_the_map_gives() -> None:
    # The first review's HIGH was a zone built as "<region>-a": the cluster's
    # location was fixed and the node pool's could have gone back unseen. Each of
    # the two `location` lines is held on its own, as the one plain local.zone.
    for resource_type in ("google_container_cluster", "google_container_node_pool"):
        body = one_resource(resource_type)

        locations = re.findall(r"^\s*location\s*=\s*(.+)$", body, flags=re.MULTILINE)

        assert [value.strip() for value in locations] == ["local.zone"], resource_type
    assert len(re.findall(r"\blocal\.zone\b", module_text())) == 2


def test_the_zone_is_looked_up_with_no_fallback_for_a_region_without_an_entry() -> None:
    # The positive claim is the one that holds: `local.zone` is the one plain
    # index into the map, and the two `location` lines above are `local.zone`.
    text = module_text()
    assert re.findall(r"^\s*zone\s*=\s*(.+)$", text, flags=re.MULTILINE) == [
        "local.zones[var.region]"
    ]
    assert len(re.findall(r"\blocal\.zones\b", text)) == 1  # the index, nowhere else

    # What follows is a denylist of the ways a zone has been, or could be, built
    # from the Region's name instead. It is a LIST and cannot be complete: HCL has
    # more string functions than are named here (`regex(`, `trimprefix(`,
    # `cidrsubnet(`...), and a zone could pass through a local or a variable. A
    # name that is not here is not therefore safe; the assertions above are.
    # Every file but variables.tf is read: that file's validations use `try(` and
    # `regex(` on other variables, and nothing there makes a zone.
    code = "\n".join(
        file_text(path.name) for path in tf_files() if path.name != "variables.tf"
    )
    fallbacks = (
        "lookup(",  # a map lookup with a default
        "try(",  # the other two ways to write a fallback
        "coalesce(",
        "${var.region",  # interpolation, "${var.region}-a"
        "format(",  # format("%s-a", var.region)
        "join(",  # join("-", [var.region, "a"])
        "replace(",
        "substr(",
        "concat(",
    )
    for fallback in fallbacks:
        assert fallback not in code, fallback


def test_the_zone_comment_says_what_was_read_and_when() -> None:
    comment = squeezed(raw_text("main.tf"))

    assert "Regions and zones" in comment
    assert "2026-10-07" in comment
    assert "europe-west1" in comment
    assert "b, c and d" in comment
    assert 'has a zone "a"' not in comment


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


def test_the_cluster_ships_system_components_logs_only_and_says_why() -> None:
    cluster = one_resource("google_container_cluster")

    (logging,) = re.findall(r"logging_config \{\n(.*?)\n\s*\}", cluster, re.DOTALL)
    assert re.search(
        r'^\s*enable_components\s*=\s*\["SYSTEM_COMPONENTS"\]$', logging, re.MULTILINE
    )
    assert "WORKLOADS" not in cluster
    # What the pages said: the _Default bucket is in the global location and
    # cannot be moved, and the platform's chart ships workload logs itself.
    # The comment lines only: the code line holds SYSTEM_COMPONENTS too, so the
    # word would be found with the comment deleted.
    comment = comment_lines_of("cluster.tf")
    assert "_Default" in comment
    assert "global" in comment
    assert "SYSTEM_COMPONENTS" in comment
    assert "chart ships workload logs" in comment


def test_the_default_node_pool_is_removed_and_has_no_node_config_of_its_own() -> None:
    cluster = one_resource("google_container_cluster")

    assert re.search(r"^\s*remove_default_node_pool\s*=\s*true$", cluster, re.MULTILINE)
    assert re.search(r"^\s*initial_node_count\s*=\s*1$", cluster, re.MULTILINE)
    # The provider's page: the cluster's node_config manages the default pool and
    # "should not be used at the same time as a google_container_node_pool". The
    # minutes the default pool lives it runs as Compute Engine's default service
    # account; the comment says so, and what fails if that account has no role.
    assert "node_config" not in cluster
    comment = squeezed(raw_text("cluster.tf"))
    assert "Compute Engine default service account" in comment
    assert "for a few minutes" in comment
    assert "fails" in comment


@pytest.mark.parametrize(
    ("resource_type", "setting"),
    [
        ("google_container_cluster", r'^\s*channel\s*=\s*"REGULAR"$'),
        ("google_container_cluster", r"^\s*enable_shielded_nodes\s*=\s*true$"),
        ("google_container_node_pool", r"^\s*auto_upgrade\s*=\s*true$"),
        ("google_container_node_pool", r"^\s*auto_repair\s*=\s*true$"),
        ("google_container_node_pool", r"^\s*enable_secure_boot\s*=\s*true$"),
        ("google_container_node_pool", r"^\s*enable_integrity_monitoring\s*=\s*true$"),
        ("google_sql_database_instance", r'^\s*availability_type\s*=\s*"ZONAL"$'),
        (
            "google_billing_budget",
            r'^\s*credit_types_treatment\s*=\s*"EXCLUDE_ALL_CREDITS"$',
        ),
    ],
    ids=[
        "release channel",
        "shielded nodes",
        "automatic upgrade",
        "automatic repair",
        "secure boot",
        "integrity monitoring",
        "zonal availability",
        "credits excluded",
    ],
)
def test_the_settings_the_review_found_untested_are_written_out(
    resource_type: str, setting: str
) -> None:
    assert re.search(setting, one_resource(resource_type), re.MULTILINE)


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


def test_the_nodes_may_read_the_modules_own_repository_and_nothing_else_in_it() -> None:
    members = resources_of("google_artifact_registry_repository_iam_member")
    (body,) = members.values()

    # The node role covers logging and metrics, not Artifact Registry: Google's
    # page says a user-provided node service account must be granted access on
    # the repository. ONE member, ON the repository (not the project), the reader
    # role, in the form that removes nothing granted elsewhere.
    assert re.search(r'^\s*role\s*=\s*"roles/artifactregistry\.reader"$', body, re.M)
    assert re.search(
        r"^\s*member\s*=\s*google_service_account\.node\.member$", body, re.M
    )
    assert re.search(
        r"^\s*repository\s*=\s*google_artifact_registry_repository\.main\.name$",
        body,
        re.M,
    )
    assert re.search(
        r"^\s*location\s*=\s*google_artifact_registry_repository\.main\.location$",
        body,
        re.M,
    )
    # No `project`: the member's project is the provider's own, and an argument
    # could aim the grant at another project's repository of the same name,
    # outside the pin. (The body does name terraform_data.project_pin, so the
    # word alone is not what is refused: an argument called project is.)
    assert not re.search(r"^\s*project\s*=", body, re.M)
    assert not re.search(
        r"artifact_registry_repository_iam_(binding|policy)", module_text()
    )
    assert "roles/artifactregistry.writer" not in module_text()
    assert "roles/artifactregistry.admin" not in module_text()
    assert "roles/artifactregistry.repoAdmin" not in module_text()
    assert "artifactregistry" not in one_resource("google_project_iam_member")


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
    assert re.search(r"backup_retention_settings \{", backup)
    assert re.search(r"^\s*retained_backups\s*=\s*3$", backup, re.MULTILINE)


def test_no_final_backup_is_kept_when_the_instance_is_removed() -> None:
    database = one_resource("google_sql_database_instance")

    assert re.search(r"final_backup_config \{\n\s*enabled\s*=\s*false\n\s*\}", database)


def test_the_backups_are_kept_in_the_instances_own_region_and_why() -> None:
    database = one_resource("google_sql_database_instance")

    # Google's page offers the "eu" multi-region (data centres in the EU, the
    # default) or a Region, and says a backup in the instance's own Region always
    # succeeds whatever the organization policy. The Region is the tighter claim.
    (backup,) = re.findall(
        r"backup_configuration \{\n(.*?)\n    \}", database, re.DOTALL
    )
    assert re.search(r"^\s*location\s*=\s*var\.region$", backup, re.MULTILINE)
    comment = squeezed(raw_text("database.tf"))
    assert "multi-region" in comment
    assert "always succeeds" in comment


def test_the_databases_storage_grows_to_a_closed_ceiling_and_no_disk_size_is_set() -> (
    None
):
    database = one_resource("google_sql_database_instance")

    # Autoresize is on by default and its limit 0 means no limit (a shared-core
    # instance may reach 3054 GB). disk_size stays unset: the provider's page says
    # a disk_size beside autoresize makes a later apply try to delete the
    # instance after a resize.
    assert re.search(r"^\s*disk_autoresize\s*=\s*true$", database, re.MULTILINE)
    (limit,) = re.findall(r"^\s*disk_autoresize_limit\s*=\s*(\d+)$", database, re.M)
    # Exactly the number the README states, not a range that accepts 1: a change
    # of the limit changes the README's sentence in the same commit.
    assert int(limit) == 20
    assert "ceiling of 20 GB" in squeezed(raw_text("README.md"))
    assert "disk_size" not in database
    comment = squeezed(raw_text("database.tf"))
    assert "disk_size" in comment
    assert "no limit" in comment


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
        ("workload_service_account", "gateway2"),
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
        # A dot is a valid Kubernetes name and not a valid Secret Manager ID, and
        # the secret's ID is built from this name.
        ("workload_service_account", "model.gateway"),
        ("workload_service_account", "a.b"),
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
