"""The self-managed cluster's accepted scan findings (S079).

The managed module's ``make aws-scan`` and the rules for its ``.trivyignore``
are tested in ``test_aws_scan.py``; this module has the same kind of file with
its own findings. A finding the owner accepts for an environment that lives an
hour goes in ``infra/terraform/aws-kubeadm/.trivyignore`` with its reason on the
line above, and these tests hold the file to exactly the findings the first
scan of the module reported (the scan itself runs in a container and is not run
here; no Makefile target runs it for this module yet). The helpers are the
managed module's.
"""

import re

import pytest
from servicesupport import REPO_ROOT
from test_aws_scan import (
    count_resources,
    entries,
    entries_without_a_reason,
    inline_ignores,
    malformed_entries,
    scanner_files,
)

MODULE = REPO_ROOT / "infra" / "terraform" / "aws-kubeadm"
TRIVYIGNORE = MODULE / ".trivyignore"
ACCEPTED = ["AWS-0104", "AWS-0164", "AWS-0178"]
# What the module's own tests must refuse in place of the ignore file's reading:
# an inline ignore of either of the pinned scanner's spellings.
INLINE_PLANT = "  #trivy:ignore:AWS-0104\n"


def module_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MODULE.glob("*.tf"))
    )


def test_the_modules_ignore_file_has_a_reason_above_every_entry() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert text.startswith("#")
    assert entries(text) != []  # the reader found them
    assert entries_without_a_reason(text) == []


def test_the_modules_entries_are_each_exactly_a_check_id() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert malformed_entries(text) == []


def test_the_accepted_findings_are_the_three_the_scan_reports_unfiltered() -> None:
    """The scan, run with every severity, reports one HIGH (AWS-0164, the
    subnet), two CRITICAL (AWS-0104, the two egress rules) and one MEDIUM
    (AWS-0178, the VPC's missing flow logs), and nothing else. A fourth ID needs
    a change of this list, with its reason, in the same diff."""
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert sorted(entries(text)) == ACCEPTED
    assert len(entries(text)) == len(set(entries(text)))


def test_each_reason_names_its_check_and_says_what_production_does() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    for check in ACCEPTED:
        (reason,) = re.findall(rf"^# ({check} .+)$", text, re.MULTILINE)
        assert "Production:" in reason, check
    (egress,) = re.findall(r"^# (AWS-0104 .+)$", text, re.MULTILINE)
    # The reason is the one security.tf gives: no fixed addresses, ports could
    # be narrowed and are not, and nothing of value on the nodes.
    assert "fixed address" in egress
    assert "limited by port and is not" in egress
    assert "nothing of value" in egress
    (subnet,) = re.findall(r"^# (AWS-0164 .+)$", text, re.MULTILINE)
    assert "NAT gateway" in subnet
    # The flow logs: why a network of an hour that holds no data goes without,
    # and what a production network sets instead.
    (flow_logs,) = re.findall(r"^# (AWS-0178 .+)$", text, re.MULTILINE)
    assert "flow logs" in flow_logs
    assert "lives an hour" in flow_logs
    assert "holds no data" in flow_logs
    assert "(medium)" in flow_logs
    assert "Production: flow logs" in flow_logs


def test_no_terraform_file_of_the_module_carries_an_inline_ignore_comment() -> None:
    found = {
        path.name: inline_ignores(path.read_text(encoding="utf-8"))
        for path in sorted(MODULE.glob("*.tf"))
    }

    assert {name: lines for name, lines in found.items() if lines} == {}


def test_an_inline_ignore_planted_in_the_modules_text_would_be_found() -> None:
    planted = INLINE_PLANT + (MODULE / "security.tf").read_text(encoding="utf-8")

    assert inline_ignores(planted) == [INLINE_PLANT.rstrip("\n")]


def test_the_module_directory_holds_no_scanner_configuration_or_yaml_ignore() -> None:
    assert scanner_files(MODULE) == []


def test_the_vpc_subnet_and_egress_rules_are_what_the_ignore_reasons_describe() -> None:
    """An entry applies to every resource of the directory: a second VPC, a
    second subnet or a third egress rule would be accepted unseen."""
    text = module_text()

    vpcs = count_resources(text, "aws_vpc")
    subnets = count_resources(text, "aws_subnet")
    egress = count_resources(text, "aws_vpc_security_group_egress_rule")

    reasons = (
        "infra/terraform/aws-kubeadm/.trivyignore accepts AWS-0178 for the one "
        "VPC, AWS-0164 for the one subnet and AWS-0104 for the two egress rules "
        "its reasons describe, and an ignore entry applies to every resource of "
        "the directory: a new VPC, subnet or egress rule is accepted silently. "
        "Decide again in that file, in the reasons, before changing these numbers."
    )
    assert vpcs == 1, reasons
    assert subnets == 1, reasons
    assert egress == 2, reasons


def test_the_module_makes_no_other_kind_of_egress_rule() -> None:
    text = module_text()

    # AWS-0104 reads these too, and each would be accepted by the same entry.
    assert count_resources(text, "aws_security_group_rule") == 0
    assert count_resources(text, "aws_network_acl_rule") == 0


@pytest.mark.parametrize(
    "line",
    [
        "  #trivy:ignore:AWS-0104",
        "// tfsec:ignore:AWS-0164",
        "/* trivy:ignore:* */",
    ],
)
def test_an_inline_ignore_of_either_spelling_is_found(line: str) -> None:
    assert inline_ignores(f'resource "x" "y" {{\n{line}\n}}\n') == [line]


def test_the_module_calls_no_module_that_could_hide_a_subnet_or_a_rule() -> None:
    found = [
        line
        for path in sorted(MODULE.glob("*.tf"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.match(r'\s*module\s+("[^"]+"|\w+)', line)
    ]

    assert found == []
    assert sorted(path.name for path in MODULE.glob("*.tf.json")) == []
