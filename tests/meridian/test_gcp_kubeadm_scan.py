"""The twin's accepted scan findings, and that nothing creates it (S079).

``infra/terraform/gcp-kubeadm`` is scanned by hand with the command of ``make
gcp-scan`` (this contract adds no target: the targets come with the wrapper's word
for the module). Its ``.trivyignore`` is a header with no entry: the first scan of
the module reported one HIGH finding, GCP-0031 on the control plane's one external
address, which is neither removed nor accepted, and these tests hold that state:
an entry that appears needs a change of the list below, with its reason, in the
same diff. The scan itself runs in a container and is not run here.

The module's own text is held to what a finding's reading depends on, since an
ignore entry applies to every resource of the directory: one subnet, one external
address, no module block, no inline ignore. And nothing in the repository that
plans, applies or removes the module exists: no target, no script line, no
workflow.
"""

import re
from pathlib import Path

import pytest
from servicesupport import REPO_ROOT
from test_aws_scan import (
    count_resources,
    entries,
    entries_without_a_reason,
    inline_ignores,
    module_blocks,
    scanner_files,
)
from test_gcp_scan import ENTRY, MAKEFILE, malformed_entries, phony_names

MODULE = REPO_ROOT / "infra" / "terraform" / "gcp-kubeadm"
TRIVYIGNORE = MODULE / ".trivyignore"
README = MODULE / "README.md"
# Every check the first scan of the module reported, at every severity, and the
# severity it gave (the README's table). A finding that appears or goes is a change
# of this table in the same diff as the change that made it.
FIRST_SCAN = {
    "GCP-0031": "HIGH",
    "GCP-0029": "LOW",
    "GCP-0076": "MEDIUM",
    "GCP-0033": "LOW",
}
# What a by-hand run in the module's directory leaves, and what a wrapper would
# leave of a plan: a name nobody makes yet, skipped by the scan's command all the
# same.
SKIPPED_FILES = {"gcp-kubeadm.tfplan", "terraform.tfstate", "terraform.tfstate.backup"}


def module_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MODULE.glob("*.tf"))
    )


def test_the_ignore_file_is_a_header_and_accepts_no_finding() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert text.startswith("#")
    assert entries(text) == []


def test_the_ignore_file_says_the_high_finding_is_neither_removed_nor_accepted() -> (
    None
):
    text = " ".join(
        re.sub(r"^#\s?", "", line)
        for line in TRIVYIGNORE.read_text(encoding="utf-8").splitlines()
    )

    assert "GCP-0031" in text
    assert "not removed and not accepted" in text
    assert "NONE" in text
    assert "a decision left open" in text


def test_the_committed_ignore_file_would_hold_a_reason_above_every_entry() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert entries_without_a_reason(text) == []
    assert malformed_entries(text) == []


@pytest.mark.parametrize(
    "entry", ["GCP-0031 exp:2099-12-31", "GCP-0031 nodes.tf", "AVD-GCP-0031", "GCP-*"]
)
def test_an_entry_that_is_not_exactly_a_check_id_would_be_found(entry: str) -> None:
    assert ENTRY.fullmatch(entry) is None


def test_the_readme_lists_each_check_of_the_first_scan_with_its_severity() -> None:
    text = README.read_text(encoding="utf-8")

    rows = dict(re.findall(r"^\| (GCP-\d{4}) \| \**(\w+)\** \|", text, re.MULTILINE))

    assert rows == FIRST_SCAN


def test_the_readme_says_the_high_finding_is_not_accepted_and_not_removed() -> None:
    text = " ".join(README.read_text(encoding="utf-8").replace("*", "").split())

    assert "Not accepted, not removed." in text
    assert "the finding the contract said to stop on" in text
    assert "`.trivyignore` holds no entry" in text


def test_the_two_findings_the_first_run_had_and_the_design_removed_are_named() -> None:
    text = " ".join(README.read_text(encoding="utf-8").split())

    assert "GCP-0027 (CRITICAL" in text
    assert "GCP-0036 (MEDIUM" in text
    assert "so the rules name their sources by address" in text


def test_no_terraform_file_of_the_module_carries_an_inline_ignore_comment() -> None:
    found = {
        path.name: inline_ignores(path.read_text(encoding="utf-8"))
        for path in sorted(MODULE.glob("*.tf"))
    }

    assert {name: lines for name, lines in found.items() if lines} == {}


def test_the_module_directory_holds_no_scanner_configuration_or_yaml_ignore_file() -> (
    None
):
    assert scanner_files(MODULE) == []


def test_the_module_calls_no_module_that_could_hide_an_address_or_a_subnet() -> None:
    found = {
        path.name: module_blocks(path.read_text(encoding="utf-8"))
        for path in sorted(MODULE.glob("*.tf"))
    }

    assert {name: lines for name, lines in found.items() if lines} == {}
    assert sorted(path.name for path in MODULE.glob("*.tf.json")) == []


def test_the_module_has_one_subnet_one_external_address_and_one_instance_kind() -> None:
    """The findings the README lists are about these: a second subnet, a second
    external address or an instance made another way (a template, a group) would
    be judged by what was decided for the first, unseen."""
    text = module_text()

    reasons = (
        "infra/terraform/gcp-kubeadm scans one subnet and one external address "
        "(the control plane's); an ignore entry applies to every resource of the "
        "directory. Decide again, in .trivyignore and the README, before "
        "changing these numbers."
    )
    assert count_resources(text, "google_compute_subnetwork") == 1, reasons
    assert count_resources(text, "google_compute_address") == 1, reasons
    assert len(re.findall(r"^\s+access_config \{", text, re.MULTILINE)) == 1, reasons
    for kind in (
        "google_compute_instance_template",
        "google_compute_instance_from_template",
        "google_compute_region_instance_template",
        "google_compute_instance_group",
        "google_compute_global_address",
    ):
        assert count_resources(text, kind) == 0, kind


def test_the_module_writes_no_firewall_rule_of_the_kinds_the_scan_reads_as_open() -> (
    None
):
    text = module_text()

    assert count_resources(text, "google_compute_firewall") == 5
    assert count_resources(text, "google_compute_network_firewall_policy_rule") == 0
    assert count_resources(text, "google_compute_firewall_policy_rule") == 0
    assert "source_service_accounts" not in re.sub(r"(?m)^\s*#.*$", "", text)


# ── nothing creates the module ───────────────────────────────────────────────

CREATING = r"(plan|apply|destroy|import|taint)"


def test_no_make_target_plans_applies_or_removes_the_google_cloud_twin() -> None:
    names = re.findall(r"^(gcp-kubeadm[\w-]*):", MAKEFILE, re.MULTILINE)

    assert [name for name in names if re.search(CREATING, name)] == []
    assert not [
        n
        for n in phony_names()
        if n.startswith("gcp-kubeadm") and re.search(CREATING, n)
    ]


def test_no_recipe_of_the_makefile_runs_terraform_in_the_twins_directory() -> None:
    for line in MAKEFILE.splitlines():
        if "gcp-kubeadm" in line and "terraform" in line:
            assert re.search(r"\b(fmt|validate|init)\b", line), line
            assert not re.search(CREATING, line), line


def test_no_script_or_workflow_of_the_repository_names_the_twin_for_a_change() -> None:
    roots = [REPO_ROOT / ".github", REPO_ROOT / "infra" / "terraform"]
    found = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if (
                not path.is_file()
                or MODULE in path.parents
                or ".terraform" in path.parts
            ):
                continue
            if path.suffix not in {".sh", ".yml", ".yaml", ".json"}:
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines()
            ):
                if "gcp-kubeadm" in line and re.search(CREATING, line):
                    found.append(f"{path.relative_to(REPO_ROOT)}:{number + 1}")

    assert found == []


def test_nothing_in_the_module_runs_a_command_and_no_state_or_plan_file_is_there() -> (
    None
):
    names = {path.name for path in Path(MODULE).iterdir()}

    assert not (names & SKIPPED_FILES)
    assert not [n for n in names if n.endswith((".tfplan", ".tfstate"))]
    for script in MODULE.rglob("*.sh"):
        pytest.fail(f"a shell script in the module's directory: {script.name}")
