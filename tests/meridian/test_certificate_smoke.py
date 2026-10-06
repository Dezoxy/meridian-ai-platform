"""What `make smoke` does about the certificates (S056).

``smoke.sh``'s identity check (9) reads the audit row of the gateway's 403 and
presents a certificate of another CA; its check (10) reads the certificate
policy. Neither touches a cluster here: the functions run in bash against a stub
``kctl`` (the harness is ``run_identity_check``'s in test_helm_identity.py). The
probe's ``foreign-ca`` mode runs for real in ``test_certificate_probe.py``;
``deploy.sh`` is in ``test_certificate_deploy.py``.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from certscriptsupport import (
    CALLER,
    KIND_DIR,
    POLICIES,
    POLICY_STATES,
    SECONDS,
)
from servicesupport import REGISTRY_DIR
from test_helm_identity import (
    DEPLOY_SH,
    GOOD,
    PROBE_RUNS,
    SMOKE_SH,
    run_identity_check,
    script_function,
)

from meridian.platform.common.identity import NAME_REFUSAL_REASON
from meridian.platform.common.throttle import REFUSAL_AUDIT_SECONDS
from meridian.platform.gateway.app import SERVICE_NAME as GATEWAY_SERVICE_NAME
from meridian.platform.registry import load_registry

# ── smoke.sh: the audit row ──────────────────────────────────────────────────


def constant(name: str) -> str:
    (value,) = re.findall(rf"^readonly {name}=(\S+)$", SMOKE_SH, re.MULTILINE)
    return value


def verdicts(lines: list[str]) -> list[str]:
    return [line.split("  ")[0] for line in lines]


MARK = "1759752000.123456"  # what the stub database's clock prints


def psql_calls(asked: str) -> list[str]:
    return [line for line in asked.splitlines() if "psql -d meridian -tAc" in line]


def audit_queries(asked: str) -> list[str]:
    return [call for call in psql_calls(asked) if "audit.events" in call]


def clock_queries(asked: str) -> list[str]:
    return [call for call in psql_calls(asked) if "audit.events" not in call]


def test_the_identity_check_passes_with_this_runs_audit_row_and_says_what_it_found(
    tmp_path: Path,
) -> None:
    lines, asked = run_identity_check(tmp_path, answers=GOOD, audit="6|t")

    assert verdicts(lines) == ["PASS"] * 5
    assert NAME_REFUSAL_REASON in lines[3]
    assert CALLER in lines[3]
    assert "6 s" in lines[3]
    # One read of the clock and one query of the table, both in the database's
    # primary pod.
    assert len(clock_queries(asked)) == 1
    assert len(audit_queries(asked)) == 1
    assert "exec platform-db-1 -c postgres -- env PGOPTIONS=" in asked
    # The other lines are the probe's, from the runtime's pod.
    assert asked.count("exec deploy/agent-runtime") == PROBE_RUNS


def test_the_clock_and_the_audit_row_are_read_with_the_statement_and_lock_timeouts(
    tmp_path: Path,
) -> None:
    _, asked = run_identity_check(tmp_path, answers=GOOD, audit="6|t")
    (options,) = re.findall(r"^readonly PSQL_OPTIONS='(.*)'$", SMOKE_SH, re.MULTILINE)

    reads = psql_calls(asked)
    assert len(reads) == 2
    for call in reads:
        assert f" -- env PGOPTIONS={options} psql -d meridian -tAc " in call


def test_the_clock_is_read_before_the_probe_that_causes_the_row(
    tmp_path: Path,
) -> None:
    _, asked = run_identity_check(tmp_path, answers=GOOD)
    calls = asked.splitlines()

    (clock,) = [i for i, call in enumerate(calls) if call in clock_queries(asked)]
    (tenant,) = [i for i, call in enumerate(calls) if " foreign-tenant " in call]
    (audit,) = [i for i, call in enumerate(calls) if call in audit_queries(asked)]
    # Before the 403 that causes the row, and the table is read after it.
    assert clock < tenant < audit


def test_the_audit_line_tries_again_and_then_fails_naming_what_was_missing(
    tmp_path: Path,
) -> None:
    lines, asked = run_identity_check(tmp_path, answers=GOOD, audit="")

    assert verdicts(lines) == ["PASS", "PASS", "PASS", "FAIL", "PASS"]
    assert NAME_REFUSAL_REASON in lines[3]
    assert CALLER in lines[3]
    # No row since this run started or in the throttle window before it: that
    # window is what the line names.
    assert constant("IDENTITY_AUDIT_THROTTLE") in lines[3]
    # About ten seconds of tries: the gateway writes in a worker thread.
    attempts = int(constant("IDENTITY_AUDIT_ATTEMPTS"))
    interval = int(constant("IDENTITY_AUDIT_INTERVAL"))
    assert len(audit_queries(asked)) == attempts
    assert 8 <= (attempts - 1) * interval <= 12


def test_an_older_row_only_is_a_skip_that_says_it_was_written_for_an_earlier_run(
    tmp_path: Path,
) -> None:
    # The gateway writes one refusal row per reason, tenant and minute: a second
    # run inside the minute causes none, and must not pass on the first's.
    lines, asked = run_identity_check(tmp_path, answers=GOOD, audit="40|f")

    assert verdicts(lines) == ["PASS", "PASS", "PASS", "SKIP", "PASS"]
    assert "wrote this minute's refusal row for an earlier run" in lines[3]
    assert "run again in a minute" in lines[3]
    assert NAME_REFUSAL_REASON in lines[3]
    assert "PASS" not in lines[3]
    # It still waited for a row of this run: the worker thread may be late.
    assert len(audit_queries(asked)) == int(constant("IDENTITY_AUDIT_ATTEMPTS"))


def test_the_audit_line_fails_when_the_query_fails_and_does_not_try_again(
    tmp_path: Path,
) -> None:
    lines, asked = run_identity_check(tmp_path, answers=GOOD, audit="FAIL")

    assert verdicts(lines) == ["PASS", "PASS", "PASS", "FAIL", "PASS"]
    assert "could not connect" in lines[3]  # psql's own message
    assert len(audit_queries(asked)) == 1


def test_the_audit_line_fails_without_a_primary_pod(tmp_path: Path) -> None:
    lines, asked = run_identity_check(tmp_path, answers=GOOD, primary="")

    assert verdicts(lines) == ["PASS", "PASS", "PASS", "FAIL", "PASS"]
    assert "platform-db" in lines[3]
    assert psql_calls(asked) == []


@pytest.mark.parametrize("clock", ["FAIL", "", "abc", "1759752000;1", "-5"])
def test_the_audit_line_fails_when_the_databases_clock_cannot_be_read_as_a_number(
    tmp_path: Path, clock: str
) -> None:
    lines, asked = run_identity_check(tmp_path, answers=GOOD, clock=clock)

    # The probes still run; only the audit line cannot judge, and it never
    # puts what the pod printed into its SQL.
    assert verdicts(lines) == ["PASS", "PASS", "PASS", "FAIL", "PASS"]
    assert "clock" in lines[3]
    assert audit_queries(asked) == []


@pytest.mark.parametrize(
    "answer", ["abc|t", "-3|t", "6;7|t", "6.5|t", "6", "6|x", "|t"]
)
def test_the_audit_line_reads_only_a_whole_number_of_seconds_and_a_flag(
    tmp_path: Path, answer: str
) -> None:
    lines, _ = run_identity_check(tmp_path, answers=GOOD, audit=answer)

    assert verdicts(lines)[3] == "FAIL"


def test_the_audit_line_waits_for_a_row_that_comes_late(tmp_path: Path) -> None:
    # The first two queries find nothing, the third finds the row.
    lines, asked = run_identity_check(
        tmp_path, answers=GOOD, audit="2|t", audit_after=2
    )

    assert verdicts(lines) == ["PASS"] * 5
    assert len(audit_queries(asked)) == 3


def test_the_foreign_ca_line_fails_on_a_status_and_on_a_traceback(
    tmp_path: Path,
) -> None:
    answered, _ = run_identity_check(
        tmp_path,
        answers="health=200 anonymous=401 foreign-tenant=403 foreign-ca=200",
    )
    raised, _ = run_identity_check(
        tmp_path,
        answers="health=200 anonymous=401 foreign-tenant=403 foreign-ca=FAIL",
    )

    assert verdicts(answered) == ["PASS", "PASS", "PASS", "PASS", "FAIL"]
    assert "expected refused or reset, got 200" in answered[4]
    assert verdicts(raised) == ["PASS", "PASS", "PASS", "PASS", "FAIL"]
    assert "Traceback" in raised[4]


def test_the_foreign_ca_line_passes_on_the_alert_and_on_a_reset_in_different_words(
    tmp_path: Path,
) -> None:
    alert, _ = run_identity_check(
        tmp_path,
        answers="health=200 anonymous=401 foreign-tenant=403 foreign-ca=refused",
    )
    reset, _ = run_identity_check(
        tmp_path,
        answers="health=200 anonymous=401 foreign-tenant=403 foreign-ca=reset",
    )

    assert verdicts(alert) == verdicts(reset) == ["PASS"] * 5
    # The alert names the unknown CA; the reset says no alert came, and that a
    # gateway that died would end the connection the same way.
    assert "TLS alert for an unknown CA" in alert[4]
    assert "no TLS alert" in reset[4]
    assert "no TLS alert" not in alert[4]
    assert "died" in reset[4]


def test_the_foreign_ca_line_fails_when_the_connection_ended_after_the_request(
    tmp_path: Path,
) -> None:
    lines, _ = run_identity_check(
        tmp_path,
        answers=(
            "health=200 anonymous=401 foreign-tenant=403 "
            "foreign-ca=closed-after-request"
        ),
    )

    # The certificate may have been accepted: not a refusal, not a PASS.
    assert verdicts(lines) == ["PASS", "PASS", "PASS", "PASS", "FAIL"]
    assert "took the request of a certificate from another CA" in lines[4]
    assert "closed without answering" in lines[4]
    assert "it may have accepted the certificate" in lines[4]
    assert "PASS" not in lines[4]


def test_a_reset_says_that_the_connection_ended_before_any_request_was_sent(
    tmp_path: Path,
) -> None:
    lines, _ = run_identity_check(
        tmp_path,
        answers="health=200 anonymous=401 foreign-tenant=403 foreign-ca=reset",
    )

    assert verdicts(lines)[4] == "PASS"
    assert "before any request was sent" in lines[4]


def test_the_audit_line_says_what_it_knows_a_row_at_or_after_this_runs_mark(
    tmp_path: Path,
) -> None:
    lines, _ = run_identity_check(tmp_path, answers=GOOD, audit="6|t")

    assert verdicts(lines)[3] == "PASS"
    assert "recorded at or after this run's mark" in lines[3]
    # Another run's 403, written after the mark, would pass here too.
    assert "recorded after this run started" not in lines[3]


def test_the_audit_query_asks_for_the_gateways_refusal_of_the_runtime_by_the_dbs_clock(
    tmp_path: Path,
) -> None:
    _, asked = run_identity_check(tmp_path, answers=GOOD)
    (query,) = audit_queries(asked)
    (clock,) = clock_queries(asked)

    # Every value in the SQL is one of the script's own constants.
    assert f"service = '{GATEWAY_SERVICE_NAME}'" in query
    assert "event = 'model.call'" in query
    assert "outcome = 'refused'" in query
    assert f"reason = '{NAME_REFUSAL_REASON}'" in query
    assert f"reference = '{CALLER}'" in query
    assert "tenant = 'evaluation'" in query
    # The run's start is the database's own clock, read before the probes; a
    # row at or after it is this run's, one in the throttle window before it
    # is why this run wrote none.
    assert "SELECT extract(epoch FROM now())" in clock
    assert f"extract(epoch FROM recorded_at) >= {MARK}" in query
    assert f"recorded_at > to_timestamp({MARK}) - interval '60 seconds'" in query
    assert "ORDER BY recorded_at DESC LIMIT 1" in query
    # A row that exists: no count, no comparison with an earlier one.
    assert "count(" not in query


def test_the_audit_query_holds_nothing_that_came_from_a_pod_but_the_checked_clock() -> (
    None
):
    body = re.search(
        r"^check_gateway_refusal_row\(\) \{\n(.*?)^\}", SMOKE_SH, re.M | re.S
    )
    start = re.search(r"^identity_mark_start\(\) \{\n(.*?)^\}", SMOKE_SH, re.M | re.S)
    assert body
    assert start
    (statement,) = re.findall(r'-tAc "(SELECT .*?)"', body.group(1), re.S)

    # Only the script's own IDENTITY_ constants are expanded in the SQL, and the
    # one value read from the database: the clock, kept only when it is a number.
    assert set(re.findall(r"\$\{(\w+)\}", statement)) <= {
        name
        for name in re.findall(r"^readonly (IDENTITY_\w+)=", SMOKE_SH, re.MULTILINE)
    } | {"identity_mark"}
    assert "$(" not in statement
    assert "`" not in statement
    assert "${primary}" not in statement
    assert "${identity_answer}" not in statement
    assert "^[0-9]+(\\.[0-9]+)?$" in start.group(1)


def test_the_reason_and_the_calling_service_are_the_ones_the_code_writes() -> None:
    registry = load_registry(REGISTRY_DIR)
    caller = registry.service(constant("IDENTITY_CALLER"))

    assert constant("IDENTITY_AUDIT_REASON") == NAME_REFUSAL_REASON
    # The gateway writes one refusal row per reason, tenant and window: the
    # window before the run's start is the one that could have taken its row.
    assert float(constant("IDENTITY_AUDIT_THROTTLE")) == REFUSAL_AUDIT_SECONDS
    assert constant("IDENTITY_GATEWAY_SERVICE") == GATEWAY_SERVICE_NAME
    # The probe runs in deploy/agent-runtime, and its certificate names that
    # registry service: the gateway writes its ID in `reference`.
    assert caller is not None
    assert caller.id == "agent-runtime"
    assert GATEWAY_SERVICE_NAME in caller.calls
    assert "kctl -n meridian exec deploy/agent-runtime" in SMOKE_SH
    assert constant("IDENTITY_FOREIGN_TENANT") == "evaluation"


# ── smoke.sh: check 10, the certificate policy ───────────────────────────────

BUILTIN_OFF_ARG = "--controllers=-certificaterequests-approver"
CONTROLLER_ARGS = (
    f"--v=2 --cluster-resource-namespace=$(POD_NAMESPACE) {BUILTIN_OFF_ARG}"
)
BUILTIN_ROLE = "cert-manager-controller-approve:cert-manager-io"
POLICY_LINES = 3
READ_STATES = {
    "ready": "printf True",
    "not-ready": "printf False",
    "no-condition": "exit 0",
    "missing": POLICY_STATES["missing"],
    "unknown-kind": POLICY_STATES["unknown-kind"],
}
ROLE_STATES = {
    "absent": 'echo "Error from server (NotFound): clusterroles.rbac.authorization.'
    f'k8s.io \\"{BUILTIN_ROLE}\\" not found" >&2; return 1',
    "present": f"printf 'clusterrole.rbac.authorization.k8s.io/{BUILTIN_ROLE}'",
    "forbidden": 'echo "Error from server (Forbidden): clusterroles.rbac.'
    'authorization.k8s.io is forbidden" >&2; return 1',
    "unreachable": 'echo "The connection to the server was refused" >&2; return 1',
}


def run_policy_check(
    tmp_path: Path,
    *,
    policies: dict[str, str] | None = None,
    available: str = "1",
    role: str = "absent",
    args: str | None = CONTROLLER_ARGS,
) -> tuple[list[str], str]:
    """The three read-only lines of ``check_certificate_policy`` from smoke.sh
    in bash against a stub ``kctl``. ``policies`` maps a policy name to a state
    of ``READ_STATES`` (the others are Ready); ``available`` is what the
    add-on's ``availableReplicas`` prints (``MISSING``: the Deployment is not
    found); ``role`` is a state of ``ROLE_STATES``; ``args`` is what the
    controller's arguments print (``None``: the read fails). Returns the
    output lines and what ``kctl`` was asked."""
    asked = tmp_path / "kctl-calls"
    asked.touch()
    states = {name: "ready" for name in POLICIES} | (policies or {})
    cases = [
        f'    *"get certificaterequestpolicy {name} "*) {READ_STATES[state]} ;;'
        for name, state in states.items()
    ]
    cases.append(
        '    *"get deployment cert-manager-approver-policy "*) '
        + (
            POLICY_STATES["missing"]
            if available == "MISSING"
            else f"printf '%s' '{available}'"
        )
        + " ;;"
    )
    cases.append(
        '    *"get deployment cert-manager "*) '
        + (
            f"printf '%s' '{args}'"
            if args is not None
            else 'echo "error: connection refused" >&2; exit 1'
        )
        + " ;;"
    )
    cases.append(f'    *"get clusterrole {BUILTIN_ROLE}"*) {ROLE_STATES[role]} ;;')
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            *re.findall(r"^readonly POLICY_\w+=.*$", SMOKE_SH, re.MULTILINE),
            script_function(SMOKE_SH, "clean_lines"),
            "kctl() {",
            f'  echo "$*" >>"{asked}"',
            '  case "$*" in',
            *cases,
            '    *) echo "stub kctl: unexpected $*" >&2; return 99 ;;',
            "  esac",
            "}",
            *(
                script_function(SMOKE_SH, name)
                for name in (
                    "check_policies_ready",
                    "check_approver_addon",
                    "check_builtin_approver_off",
                )
            ),
            # The three read-only lines of check 10; its fourth, the request the
            # issuer must refuse, has its own harness below.
            "check_policies_ready",
            "check_approver_addon",
            "check_builtin_approver_off",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"]},
        timeout=SECONDS,
    )
    return done.stdout.splitlines(), asked.read_text(encoding="utf-8")


def test_the_policy_check_prints_three_pass_lines_when_all_is_as_make_up_leaves_it(
    tmp_path: Path,
) -> None:
    lines, asked = run_policy_check(tmp_path)

    assert verdicts(lines) == ["PASS"] * POLICY_LINES
    for name in POLICIES:
        assert name in lines[0]
    assert "cert-manager-approver-policy" in lines[1]
    assert "approver" in lines[2]
    # Read-only, and nothing but the objects it studies.
    assert all("get" in call.split() for call in asked.splitlines())
    assert asked.count("get certificaterequestpolicy") == len(POLICIES)


@pytest.mark.parametrize(
    "state", ["not-ready", "no-condition", "missing", "unknown-kind"]
)
@pytest.mark.parametrize("policy", POLICIES)
def test_the_policy_check_fails_for_a_policy_that_is_not_ready_or_not_there(
    tmp_path: Path, policy: str, state: str
) -> None:
    lines, _ = run_policy_check(tmp_path, policies={policy: state})

    assert verdicts(lines) == ["FAIL", "PASS", "PASS"]
    assert policy in lines[0]
    assert "make up" in lines[0]
    for other in set(POLICIES) - {policy}:
        assert other not in lines[0].replace("meridian-services-ca", "")


@pytest.mark.parametrize("available", ["", "0", "MISSING"])
def test_the_policy_check_fails_when_the_add_on_has_no_available_replica(
    tmp_path: Path, available: str
) -> None:
    lines, _ = run_policy_check(tmp_path, available=available)

    assert verdicts(lines) == ["PASS", "FAIL", "PASS"]
    assert "cert-manager-approver-policy" in lines[1]


@pytest.mark.parametrize("available", ["2", "10"])
def test_the_policy_check_accepts_any_positive_number_of_available_replicas(
    tmp_path: Path, available: str
) -> None:
    lines, _ = run_policy_check(tmp_path, available=available)

    assert verdicts(lines) == ["PASS"] * POLICY_LINES


def test_the_policy_check_fails_when_the_built_in_approvers_clusterrole_exists(
    tmp_path: Path,
) -> None:
    lines, _ = run_policy_check(tmp_path, role="present")

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    assert BUILTIN_ROLE in lines[2]
    assert "signing for every request" in lines[2]


def test_the_policy_check_fails_when_the_controllers_arguments_lack_the_switch(
    tmp_path: Path,
) -> None:
    lines, _ = run_policy_check(tmp_path, args="--v=2 --controllers=*")

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    assert BUILTIN_OFF_ARG in lines[2]
    assert "signing for every request" in lines[2]


def test_the_policy_check_reads_the_switch_as_a_whole_argument(tmp_path: Path) -> None:
    near = f"--v=2 {BUILTIN_OFF_ARG}-not --x{BUILTIN_OFF_ARG}"
    lines, _ = run_policy_check(tmp_path, args=near)

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]


def test_the_policy_check_needs_both_readings_to_agree(tmp_path: Path) -> None:
    lines, _ = run_policy_check(tmp_path, role="present", args="")

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    # One line, and it says both things.
    assert BUILTIN_ROLE in lines[2]
    assert BUILTIN_OFF_ARG in lines[2]


@pytest.mark.parametrize("role", ["forbidden", "unreachable"])
def test_a_kubectl_error_other_than_not_found_is_a_fail_not_a_pass_for_the_clusterrole(
    tmp_path: Path, role: str
) -> None:
    lines, _ = run_policy_check(tmp_path, role=role)

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    assert "could not" in lines[2]


def test_a_failure_to_read_the_controllers_arguments_is_a_fail(tmp_path: Path) -> None:
    lines, _ = run_policy_check(tmp_path, args=None)

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    assert "could not" in lines[2]


def test_the_policy_check_never_skips(tmp_path: Path) -> None:
    # Its objects exist after `make up`, with or without `make deploy`: a
    # missing one is a FAIL, never a SKIP.
    lines, _ = run_policy_check(
        tmp_path,
        policies={name: "missing" for name in POLICIES},
        available="MISSING",
        role="present",
        args=None,
    )

    assert verdicts(lines) == ["FAIL"] * POLICY_LINES
    assert "SKIP" not in "".join(lines)


def test_the_policy_check_is_the_tenth_and_is_documented() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]

    # The alert rules check (S062, test_smoke_alert_rules.py) runs after it.
    assert calls[8:10] == ["check_service_identity", "check_certificate_policy"]
    assert calls[10] == "check_alert_rules"
    assert calls[11].startswith("if ((failures")
    # Four lines since S062: the three above and the request that must be
    # refused (test_certificate_refused_request.py).
    assert "10. certificate policy: four lines" in SMOKE_SH


def test_the_policies_smoke_reads_are_the_ones_deploy_reads() -> None:
    (smoke,) = re.findall(r"^readonly POLICY_NAMES=\((.*)\)$", SMOKE_SH, re.M)
    (deploy,) = re.findall(r"^readonly CERTIFICATE_POLICIES=\((.*)\)$", DEPLOY_SH, re.M)

    assert smoke.split() == deploy.split() == list(POLICIES)
    assert constant("POLICY_BUILTIN_ROLE") == BUILTIN_ROLE
    assert constant("POLICY_BUILTIN_OFF_ARG") == BUILTIN_OFF_ARG


def test_the_comments_say_what_two_lines_of_the_identity_check_do_not_prove() -> None:
    header = " ".join(
        line.removeprefix("#").strip()
        for line in SMOKE_SH.split("set -euo pipefail")[0].splitlines()
    )
    probe_comment = " ".join(
        line.removeprefix("#").strip()
        for line in SMOKE_SH.split("readonly IDENTITY_HOST")[0].splitlines()
    )

    # The audit line is this run's row, or a SKIP that says why it cannot be;
    # `reset` is wider than the alert for an unknown CA, and says so.
    assert "recorded at or after this run's start" in header
    assert "two runs that overlap can share one row" in header
    assert "a SKIP, not a PASS" in header
    assert "the gateway wrote this minute's refusal row for an earlier run" in header
    assert "an earlier run wrote in the last 120 seconds" not in header
    assert "not that it refused this run's 403" not in header
    assert "uvicorn" in header
    assert "without delivering the alert" in header
    assert "a gateway that died in that second" in header
    assert "TLSV1_ALERT_UNKNOWN_CA" in header
    assert "any TLS error or reset after the server's certificate verified" not in (
        header
    )
    assert "TLSV1_ALERT_UNKNOWN_CA" in probe_comment
    assert "`reset`" in probe_comment
    assert "any TLS error or reset after the server's certificate verified" not in (
        probe_comment
    )
    # `reset` is a connection that ended before any request was sent; one that
    # ended after it is a third answer, a FAIL, and its price is said.
    for words in (
        "before any request was sent",
        "closed-after-request",
        "fails closed",
    ):
        assert words in header, words
        assert words in probe_comment, words
    # The deploy header says what approves the Certificates.
    deploy_header = " ".join(
        line.removeprefix("#").strip()
        for line in DEPLOY_SH.split("set -euo pipefail")[0].splitlines()
    )
    assert "five CertificateRequestPolicies" in deploy_header
    assert "approver-policy running" in deploy_header


def test_the_switch_smoke_looks_for_is_the_one_the_charts_flag_makes() -> None:
    values = yaml.safe_load(
        (KIND_DIR / "values" / "cert-manager.yaml").read_text(encoding="utf-8")
    )

    # kind's values turn the built-in approver off with the chart's own flag;
    # the flag renders the argument and removes the ClusterRole (chart v1.21.2).
    assert values["disableAutoApproval"] is True
