"""The number of lines ``make smoke`` prints after ``make deploy``, from the script.

The documents say how many PASS lines a healthy ``make smoke`` prints, and
``test_smoke_alert_rules.py`` pins that number (``SMOKE_LINES_AFTER_DEPLOY``) in
them. Here the number is tied to ``infra/kind/smoke.sh``: the sum of what each
check prints when everything is as it should be. Of the eleven checks, eight have
at least one harness of their own that runs the function in bash against stubs:
the database (its stores, and its policy's address line), telemetry (the round
trip, and the two TLS lines that open it), the cost panel, the sweep (the Job's
line, and the findings' apart), the network
policy (four lines, and the collector's and the rate store's apart), service
identity, the certificate
policy (the policy, and the refused request apart) and the alert rules. Three
have none (the edge, the tools, the adjuster pages), and the pgvector lines of
the database check have none either: their count is the number of ``pass`` calls
in the function's source (times the databases the loop names). There is no
harness for the whole script, which needs a cluster.
"""

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from kindharness import (
    SWEEP_FINISHED,
    run_cost_panel,
    run_sweep_check,
    sweep_job,
)
from kindsupport import SMOKE_SH, function_body, requires_jq
from test_certificate_refused_request import run_check as run_refused_request_check
from test_certificate_smoke import run_policy_check
from test_helm_identity import GOOD, run_identity_check
from test_kind_database_policy_address import run_database_policy_check
from test_smoke_alert_rules import SMOKE_LINES_AFTER_DEPLOY, run_alert_rules
from test_smoke_log_agent import healthy_log_agent_lines
from test_smoke_log_agent_shape import (
    healthy_log_agent_pod_lines,
    healthy_log_agent_streams_lines,
)
from test_smoke_network_collector import run_collector_check
from test_smoke_network_policy import run_network_policy_check
from test_smoke_network_rate_store import run_rate_store_check
from test_smoke_stores import run_stores_check
from test_smoke_sweep_findings import healthy_sweep_findings_lines
from test_smoke_telemetry import run_telemetry_check
from test_smoke_telemetry_tls import healthy_ca_lines, healthy_clear_text_lines

pytestmark = requires_jq


def pass_sites(function: str) -> int:
    """How many ``pass`` calls the function's source holds."""
    return len(re.findall(r'^\s*pass "', function_body(SMOKE_SH, function), re.M))


def pgvector_lines() -> int:
    """The database check's own lines: one ``pass`` in a loop over databases."""
    body = function_body(SMOKE_SH, "check_database")
    (databases,) = re.findall(r"^\s*for database in ([a-z ]+); do$", body, re.M)
    return pass_sites("check_database") * len(databases.split())


def smoke_calls() -> list[str]:
    """The ``check_*`` calls at the end of the script, in order."""
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]
    return calls[: next(i for i, c in enumerate(calls) if c.startswith("if (("))]


def all_pass(lines: list[str]) -> int:
    """The number of lines, which must all be PASS."""
    assert lines, "a check printed nothing"
    assert [line.split()[0] for line in lines] == ["PASS"] * len(lines), lines
    return len(lines)


@pytest.fixture(scope="module")
def printed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, int]:
    """What each check of the script prints when all is well, by function."""

    def fresh() -> Path:
        return tmp_path_factory.mktemp("check")

    def lines_of(run: Callable[[Path], tuple[list[str], str]]) -> list[str]:
        return run(fresh())[0]

    healthy_sweep = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]
    return {
        "check_edge": pass_sites("check_edge"),
        # The pgvector lines, the three of the stores and, since S063, the
        # policy's address line: the last two have harnesses apart.
        "check_database": pgvector_lines()
        + all_pass(lines_of(run_stores_check))
        + all_pass(lines_of(run_database_policy_check)),
        "check_tools": pass_sites("check_tools"),
        # Four lines, since S063 the two TLS lines that open the check and, since
        # S064, the line that finds the Claims API's own record in Loki and, since
        # G1, the one that reads the agent's pod before it and the one that asks
        # Loki for the streams that must not be there after it: their harnesses
        # are apart.
        "check_telemetry": all_pass(run_telemetry_check(fresh()))
        + all_pass(healthy_ca_lines(fresh()))
        + all_pass(healthy_clear_text_lines(fresh()))
        + all_pass(healthy_log_agent_pod_lines(fresh()))
        + all_pass(healthy_log_agent_lines(fresh()))
        + all_pass(healthy_log_agent_streams_lines(fresh())),
        "check_cost_panel": all_pass(lines_of(run_cost_panel)),
        "check_adjuster_pages": pass_sites("check_adjuster_pages"),
        # Two lines since S064 (C3): the Job's and, apart harness, the findings'.
        "check_sweep": all_pass(
            lines_of(lambda path: run_sweep_check(path, jobs=healthy_sweep))
        )
        + all_pass(healthy_sweep_findings_lines(fresh())),
        # Four lines and, since S063, the collector's and, since S066, the rate
        # store's: their harnesses are apart.
        "check_network_policy": all_pass(lines_of(run_network_policy_check))
        + all_pass(lines_of(run_collector_check))
        + all_pass(lines_of(run_rate_store_check)),
        "check_service_identity": all_pass(
            lines_of(lambda path: run_identity_check(path, answers=GOOD))
        ),
        "check_certificate_policy": all_pass(lines_of(run_policy_check))
        + all_pass(run_refused_request_check(fresh()).lines),
        "check_alert_rules": all_pass(run_alert_rules(fresh())),
    }


def test_every_check_the_script_runs_is_counted_here_and_none_that_it_does_not(
    printed: dict[str, int],
) -> None:
    assert smoke_calls() == list(printed)


def test_the_lines_the_checks_print_add_up_to_the_count_the_documents_state(
    printed: dict[str, int],
) -> None:
    assert sum(printed.values()) == SMOKE_LINES_AFTER_DEPLOY, printed


def test_the_counts_by_print_site_are_the_ones_the_checks_visibly_have() -> None:
    assert pass_sites("check_edge") == 1
    assert pass_sites("check_tools") == 1
    assert pass_sites("check_adjuster_pages") == 3
    assert pgvector_lines() == 2
