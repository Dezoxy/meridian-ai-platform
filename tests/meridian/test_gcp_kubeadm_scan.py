"""The twin's make targets, its accepted scan finding, and that nothing creates it.

``infra/terraform/gcp-kubeadm`` is checked by ``make gcp-kubeadm-validate`` and
scanned by ``make gcp-kubeadm-scan``, the twins of ``make aws-kubeadm-validate``
and ``make aws-kubeadm-scan`` (``test_aws_kubeadm_scan.py``: the test names below
say each twin) and of ``make gcp-scan`` (``test_gcp_scan.py``, whose helpers they
use). Its ``.trivyignore`` holds ONE entry, GCP-0031 on the control plane's one
external address, accepted by the main session on 2026-10-07 with the reason
above it; the three findings the README reports and does not accept (flow logs,
disk keys) are not in it. These tests hold the entry and the facts its reason
states: an entry that appears needs a change of the list below, with its reason,
in the same diff. The scan itself runs in a container and is not run here.

The module's own text is held to what a finding's reading depends on, since an
ignore entry applies to every resource of the directory: one subnet, one external
address, no module block, no inline ignore. And nothing in the repository plans,
applies or removes the module: the two targets check it, ``aws.sh`` takes its
name on ``validate`` only, and no workflow names it for a change.
"""

import re
import shutil
import subprocess
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
from test_gcp_scan import (
    ENTRY,
    MAKEFILE,
    help_line,
    malformed_entries,
    phony_names,
    recipe,
)

MODULE = REPO_ROOT / "infra" / "terraform" / "gcp-kubeadm"
TRIVYIGNORE = MODULE / ".trivyignore"
README = MODULE / "README.md"
ACCEPTED = ["GCP-0031"]
TARGETS = ("gcp-kubeadm-validate", "gcp-kubeadm-scan")
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


def test_the_ignore_file_has_a_reason_above_every_entry() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert text.startswith("#")
    assert entries(text) != []  # the reader found the entry
    assert entries_without_a_reason(text) == []
    assert malformed_entries(text) == []


def test_the_ignore_file_accepts_exactly_the_one_high_finding() -> None:
    """A second ID needs a change of this list, with its reason, in the same
    diff: a new ID with a reason of five words would otherwise pass the shape and
    the reason tests. The three reported-only findings are not in the file."""
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert entries(text) == ACCEPTED
    for reported_only in ("GCP-0029", "GCP-0076", "GCP-0033"):
        assert reported_only not in entries(text)


def test_the_entry_has_the_reason_the_decision_gave_with_what_production_does() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    (reason,) = re.findall(r"^# (GCP-0031 .+)$", text, re.MULTILINE)
    assert "(high)" in reason
    assert "Instance has a public IP allocated" in reason
    assert "one external address" in reason
    assert "owner's one address" in reason
    assert "the workers have none and leave through Cloud NAT" in reason
    assert "nothing but port 6443, from the owner's /32" in reason
    assert "never applied" in reason
    assert "Production:" in reason
    assert "Identity-Aware Proxy or an internal load balancer" in reason
    assert "designed, not built" in reason


def test_the_facts_the_reason_states_hold_in_the_modules_text() -> None:
    """One external address, on the control plane, and one rule that admits
    anything from outside the nodes' subnet: from the owner's /32 to 6443."""
    text = module_text()
    nodes = (MODULE / "nodes.tf").read_text(encoding="utf-8")
    control_plane = nodes.split('resource "google_compute_instance" "worker"')[0]
    workers = nodes.split('resource "google_compute_instance" "worker"')[1]

    assert len(re.findall(r"^\s+access_config \{", text, re.MULTILINE)) == 1
    assert "access_config {" in control_plane
    assert "access_config" not in re.sub(r"(?m)^\s*#.*$", "", workers)
    assert count_resources(text, "google_compute_address") == 1
    assert (
        count_resources(text, "google_compute_router_nat") == 1
    )  # the workers' way out
    outside = []
    for name, body in re.findall(
        r'resource "google_compute_firewall" "(\w+)" \{\n(.*?)^\}', text, re.M | re.S
    ):
        ranges = re.search(r"^\s*source_ranges\s*=\s*(.+)$", body, re.MULTILINE)[1]
        if (
            "local.nodes_cidr" not in ranges
            and "control_plane_internal_address" not in ranges
        ):
            outside.append((name, ranges, re.findall(r"ports\s*=\s*(\[.*?\])", body)))
    assert outside == [
        ("api_from_operator", "[var.api_access_cidr]", ["[tostring(local.api_port)]"])
    ]


@pytest.mark.parametrize(
    "entry", ["GCP-0031 exp:2099-12-31", "GCP-0031 nodes.tf", "AVD-GCP-0031", "GCP-*"]
)
def test_an_entry_that_is_not_exactly_a_check_id_would_be_found(entry: str) -> None:
    assert ENTRY.fullmatch(entry) is None


def test_the_readme_lists_each_check_of_the_first_scan_with_its_severity() -> None:
    text = README.read_text(encoding="utf-8")

    rows = dict(re.findall(r"^\| (GCP-\d{4}) \| \**(\w+)\** \|", text, re.MULTILINE))

    assert rows == FIRST_SCAN


def test_the_readme_says_the_high_finding_is_accepted_and_the_others_are_not() -> None:
    text = " ".join(README.read_text(encoding="utf-8").replace("*", "").split())

    assert "Accepted in `.trivyignore`, with its reason" in text
    assert "decision of 2026-10-07" in text
    assert "designed, not built" in text  # no external address, IAP or a balancer
    assert "Identity-Aware Proxy or an internal load balancer" in text
    assert "not accepted and not in `.trivyignore`" in text
    assert "what an unfiltered run reports" in text
    assert text.count("Reported only.") == 2  # flow logs (the newer check says so)
    assert "The same finding, in the newer check" in text


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
        # A line that runs or names the program, not one that holds a path.
        if "gcp-kubeadm" in line and re.search(r"(?<![/.\w-])terraform\s", line):
            assert re.search(r"\b(fmt|validate|init)\b", line), line
            assert not re.search(CREATING, line), line


def test_no_script_or_workflow_of_the_repository_names_the_twin_for_a_change() -> None:
    """How this reads the wrapper's row for the twin: aws.sh names the word in
    comments, in its usage line, in `select_module` and in the one sentence that
    refuses it, and none of those is a path that creates anything, which the next
    test holds structurally. So a line of aws.sh is skipped here only if it is a
    comment or is that sentence; every other file is read whole."""
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
            # Not .json: Renovate's note is prose that says no step runs a plan.
            if path.suffix not in {".sh", ".yml", ".yaml"}:
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines()
            ):
                if path.name == "aws.sh" and (
                    line.lstrip().startswith("#")
                    or "never planned, applied or removed" in line
                ):
                    continue
                if "gcp-kubeadm" in line and re.search(CREATING, line):
                    found.append(f"{path.relative_to(REPO_ROOT)}:{number + 1}")

    assert found == []


def test_the_wrapper_takes_the_twins_name_on_validate_only_and_refuses_it_after() -> (
    None
):
    script = (REPO_ROOT / "infra" / "terraform" / "aws.sh").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )

    # The word selects a row in exactly one place, under `validate`...
    assert re.findall(
        r"^\s*gcp-kubeadm\) select_module gcp-kubeadm ;;$", code, re.M
    ) == ["      gcp-kubeadm) select_module gcp-kubeadm ;;"]
    validate_branch = code.split("  validate)\n", 1)[1].split("\n  *)\n", 1)[0]
    assert "gcp-kubeadm) select_module gcp-kubeadm ;;" in validate_branch
    # ...and on plan, apply and the removal it goes to the refusal.
    other_branch = code.split("\n  *)\n", 1)[1]
    assert "gcp | gcp-kubeadm) refuse_gcp ;;" in other_branch
    assert "select_module gcp-kubeadm" not in other_branch
    # The row has a directory and no plan, state, command or variable.
    row = code.split("    gcp-kubeadm)\n", 1)[1].split("      ;;", 1)[0]
    assert 'MODULE_DIR="${TF_DIR}/gcp-kubeadm"' in row
    for empty in (
        "PLAN_FILE",
        "PLAN_RECORD_FILE",
        "STATE_DIR_UNDER_HOME",
        "STATE_FILE_NAME",
        "MODULE_README",
        "CMD_PLAN",
        "CMD_APPLY",
        "CMD_DESTROY",
        "PLAN_REVIEW",
    ):
        assert f"{empty}=\n" in row, empty
    assert "MODULE_VARS=()" in row


# ── the targets (twins of test_aws_kubeadm_scan.py's, and of test_gcp_scan.py's) ─


def without_the_module(text: str) -> str:
    """A scan recipe with what is the module's own made the same: the skipped
    file names, and the word that names the module (the directory, the target in
    the refusal's message). The longer names come first in the pattern."""
    text = re.sub(r"--skip-files \S+", "--skip-files SKIPPED", text.strip())
    return re.sub(r"\b(gcp-kubeadm|aws-kubeadm|aws|gcp)\b", "MODULE", text)


@pytest.mark.parametrize("target", TARGETS)
def test_each_gcp_kubeadm_target_is_phony_and_has_a_help_line(target: str) -> None:
    # twin of: test_each_aws_kubeadm_target_is_phony_and_has_a_help_line
    assert target in phony_names()
    assert re.search(rf"^{target}:", MAKEFILE, re.MULTILINE)
    assert help_line(target)


def test_the_validate_target_runs_the_script_with_the_module_name_alone() -> None:
    # twin of: test_the_validate_target_runs_the_script_with_the_module_name_alone
    assert (
        recipe("gcp-kubeadm-validate").strip()
        == "infra/terraform/aws.sh validate gcp-kubeadm"
    )


def test_the_two_targets_change_nothing_in_google_cloud_and_need_no_project() -> None:
    # twin of: test_the_two_targets_say_they_change_nothing_in_aws_and_need_no_account
    for target in TARGETS:
        line = help_line(target)
        assert "changes nothing in Google Cloud" in line
        assert "no project and no credential" in line
    assert "offline" in help_line("gcp-kubeadm-scan")


def test_the_only_gcp_kubeadm_targets_are_the_two_checks() -> None:
    # twin of: test_no_target_plans_applies_or_removes_the_self_managed_module_yet,
    # which names three targets that do not exist; here no such name will ever.
    declared = set(re.findall(r"^(gcp-kubeadm-[\w-]+):", MAKEFILE, re.MULTILINE))

    assert declared == set(TARGETS)
    assert not {"gcp-kubeadm-plan", "gcp-kubeadm-apply", "gcp-kubeadm-destroy"} & set(
        phony_names()
    )


def test_the_scan_uses_the_one_pinned_image_and_no_second_pin() -> None:
    # twin of: test_the_scan_uses_the_one_pinned_image_and_no_second_pin
    assert "$(TRIVY_IMAGE)" in recipe("gcp-kubeadm-scan")
    assert len(re.findall(r"^\w*TRIVY\w*\s*:=", MAKEFILE, re.MULTILINE)) == 1
    assert "aquasecurity/trivy" not in recipe("gcp-kubeadm-scan")


def test_the_scan_runs_with_no_network_and_no_rights_on_this_directory_alone() -> None:
    # twin of: test_the_scan_runs_with_no_network_and_no_rights_on_this_directory_alone
    text = recipe("gcp-kubeadm-scan")

    for flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
    ):
        assert flag in text
    assert re.search(r"--mount type=bind,[^ ]*readonly", text)
    assert 'source="$(CURDIR)/infra/terraform/gcp-kubeadm"' in text
    assert 'infra/terraform/gcp"' not in text
    assert "infra/terraform/aws" not in text


def test_the_scan_stays_offline_and_fails_on_high_and_critical() -> None:
    # twin of: test_the_scan_stays_offline_and_fails_on_high_and_critical
    text = recipe("gcp-kubeadm-scan")

    assert " config " in text
    assert "--severity HIGH,CRITICAL" in text
    assert "--exit-code 1" in text
    for flag in ("--skip-check-update", "--skip-version-check", "--disable-telemetry"):
        assert flag in text


def test_the_scan_reads_the_ignore_file_that_sits_in_the_modules_directory() -> None:
    # twin of: test_the_scan_reads_the_ignore_file_that_sits_in_the_modules_directory
    text = recipe("gcp-kubeadm-scan")

    assert "-w /work" in text  # Trivy's default .trivyignore is the working dir's
    assert "--ignorefile" not in text
    (source,) = re.findall(r'source="\$\(CURDIR\)/([^"]+)"', text)
    mounted = REPO_ROOT / source
    assert mounted == MODULE
    assert entries((mounted / ".trivyignore").read_text(encoding="utf-8")) == ACCEPTED


def test_the_scan_leaves_out_the_terraform_directory_and_the_files_a_run_leaves() -> (
    None
):
    # twin of: test_the_scan_leaves_out_the_terraform_directory_and_the_files_a_run
    text = recipe("gcp-kubeadm-scan")

    assert "--skip-dirs .terraform " in text
    (skipped,) = re.findall(r"--skip-files (\S+)", text)
    assert set(skipped.split(",")) == SKIPPED_FILES


def test_the_scan_refuses_a_directory_with_no_terraform_in_it_before_docker_runs(
    tmp_path: Path,
) -> None:
    # twin of: test_the_scan_refuses_a_directory_with_no_terraform_in_it_...
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    called = tmp_path / "docker-was-called"
    stub = bin_dir / "docker"
    stub.write_text(f"#!/bin/sh\ntouch {called}\n", encoding="utf-8")
    stub.chmod(0o755)
    empty = tmp_path / "empty"
    empty.mkdir()

    result = subprocess.run(
        [make, "-C", str(empty), "-f", str(REPO_ROOT / "Makefile"), "gcp-kubeadm-scan"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )

    assert result.returncode != 0
    assert (
        "gcp-kubeadm-scan: no .tf file in infra/terraform/gcp-kubeadm" in result.stderr
    )
    assert not called.exists()


def test_the_two_scan_recipes_are_equal_but_for_directory_and_skipped_names() -> None:
    # twin of: test_the_two_scan_recipes_are_equal_but_for_directory_and_skipped_names
    twin = without_the_module(recipe("gcp-kubeadm-scan"))
    scaffold = without_the_module(recipe("gcp-scan"))

    assert twin == scaffold
    assert "infra/terraform/gcp-kubeadm" in recipe("gcp-kubeadm-scan")
    assert "infra/terraform/gcp/" in recipe("gcp-scan")
    assert "--skip-files" in twin


def test_a_flag_in_one_recipe_only_makes_the_recipes_differ() -> None:
    # twin of: test_a_flag_in_one_recipe_only_makes_the_recipes_differ
    scaffold = recipe("gcp-scan")
    twin = recipe("gcp-kubeadm-scan")
    for flag in ("--network none", "--cap-drop ALL", "--exit-code 1"):
        assert flag in twin
        assert without_the_module(twin.replace(flag, "")) != without_the_module(
            scaffold
        )
    extra = twin.replace(" config ", " config --scanners misconfig ")
    assert without_the_module(extra) != without_the_module(scaffold)


def test_nothing_in_the_module_runs_a_command_and_no_state_or_plan_file_is_there() -> (
    None
):
    names = {path.name for path in Path(MODULE).iterdir()}

    assert not (names & SKIPPED_FILES)
    assert not [n for n in names if n.endswith((".tfplan", ".tfstate"))]
    for script in MODULE.rglob("*.sh"):
        pytest.fail(f"a shell script in the module's directory: {script.name}")
