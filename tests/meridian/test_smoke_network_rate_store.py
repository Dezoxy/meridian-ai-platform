"""The sixth line of smoke's network policy check: the rate store (S066, T-45).

``check_network_rate_store`` in ``infra/kind/smoke.sh`` opens a TCP connection from
the Claims API's pod to the rate store's Service, which only the Model Gateway's
pods may reach: it must time out. It is one ``network_expect`` of the check's own
probe (``test_smoke_network_policy.py``), the control of which, the Claims API to
the Agent Runtime, has already passed when this line runs, and it runs inside
``check_network_policy`` after the collector's line, so it is skipped with the
others while the Meridian services are not deployed. The harness is that of the
network policy check (its probe definitions and stub ``kctl``). The line count
(``test_smoke_line_count.py``) adds this harness's line to the other five.

What the line does not prove is in its header: a timeout from the Claims API is
the sender's egress (the Claims API's policy has no rule to the store) as much as
the store's ingress, which no pod of the chart can be made to show alone, because
no pod but the gateway's has an egress rule to the store. The chart's tests pin
the store's ingress; the line shows that the path is closed from where a caller
of the platform would stand, that the name resolves (a store that is not there is
a name that does not resolve, which fails the line) and that nothing answers.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import test_smoke_network_policy as network
from chartsupport import (
    NAMESPACE,
    RATE_STORE,
    allowed_services,
    network_policies,
    rendered_chart,
)
from test_kind_manifests import (
    SMOKE_SH,
    function_body,
    function_definition,
    one_line_function,
    requires_jq,
)

pytestmark = requires_jq

TARGET = f"{RATE_STORE}.{NAMESPACE}.svc:6379"
HOST, PORT = TARGET.split(":")
FUNCTIONS = ("network_probe", "network_expect", "check_network_rate_store")
PASS = (
    "PASS  network policy: the Claims API cannot reach the rate store "
    f"({TARGET}), which only the Model Gateway's pods may"
)
STUB = r"""
kctl() {
  printf '%s\n' "${*//$'\n'/ }" >>"${ASKED}"
  case "$*" in
    *" exec "*)
      [[ -z "${ERROR}" ]] || echo "${ERROR}" >&2
      printf "%s" "${ANSWER}"
      return "${STATUS}" ;;
    *) echo "unexpected kctl $*" >&2; return 1 ;;
  esac
}
"""


def run_rate_store_check(
    tmp_path: Path, *, answer: str = "blocked\n", status: int = 0, error: str = ""
) -> tuple[list[str], str]:
    """``check_network_rate_store`` of smoke.sh in bash against a stub ``kctl``;
    ``answer``, ``status`` and ``error`` are what the probe's exec prints, exits
    with and writes to stderr. Returns the output lines and what ``kctl`` was
    asked, one call per line."""
    asked = tmp_path / "kctl-calls"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            *network.PROBE_DEFINITIONS,
            one_line_function(SMOKE_SH, "clean_lines"),
            STUB,
            *(function_definition(SMOKE_SH, name) for name in FUNCTIONS),
            "check_network_rate_store",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "ASKED": str(asked),
            "ANSWER": answer,
            "STATUS": str(status),
            "ERROR": error,
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text()


def test_a_store_nobody_but_the_gateway_reaches_is_one_pass_line(
    tmp_path: Path,
) -> None:
    lines, asked = run_rate_store_check(tmp_path)

    assert lines == [PASS]
    (call,) = asked.splitlines()
    assert call.startswith("-n meridian exec deploy/claims-api -- python -c ")
    assert call.endswith(f" {HOST} {PORT}")


def test_a_store_the_claims_api_reaches_fails_and_says_what_may_be_wrong(
    tmp_path: Path,
) -> None:
    lines, _ = run_rate_store_check(tmp_path, answer="reached\n")

    (line,) = lines
    assert line.startswith(
        f"FAIL  network policy: the Claims API reached the rate store ({TARGET})"
    )
    # The three ways it happens: too wide, missing, not enforced.
    assert "admits more than the Model Gateway's pods" in line
    assert "or is missing" in line
    assert "does not enforce" in line
    # And what it means: the pod could read or reset every tenant's window.
    assert "window" in line


@pytest.mark.parametrize(
    ("answer", "status", "error"),
    [
        ("", 1, "error: unable to upgrade connection: container not found"),
        ("", 1, "Traceback: socket.gaierror: Name or service not known"),
        ("", 1, "Traceback: ConnectionRefusedError: [Errno 111] Connection refused"),
        ("maybe\n", 0, ""),
        ("blocked\n", 1, "command terminated with exit code 1"),
        ("\x1b[31mblocked\nreached\n", 0, "\x1b]0;title\x07"),
    ],
    ids=[
        "exec-failed",
        "no-dns-a-store-that-is-not-there",
        "refused",
        "other-word",
        "blocked-but-failed",
        "escapes",
    ],
)
def test_any_answer_but_a_timeout_fails_with_the_probes_words_and_clean_output(
    tmp_path: Path, answer: str, status: int, error: str
) -> None:
    lines, _ = run_rate_store_check(tmp_path, answer=answer, status=status, error=error)

    (line,) = lines
    assert line.startswith(
        f"FAIL  network policy: the probe in deploy/claims-api to {TARGET} "
        "gave no answer of reached or blocked"
    )
    assert "\x1b" not in line and "\x07" not in line


def test_the_check_changes_nothing_and_only_probes(tmp_path: Path) -> None:
    source = function_body(SMOKE_SH, "check_network_rate_store")
    _, asked = run_rate_store_check(tmp_path)

    assert not re.search(r"kctl[^\n]*\b(apply|patch|replace|create|delete)\b", source)
    assert {call.split()[2] for call in asked.splitlines()} == {"exec"}


# ── what the line stands on ──────────────────────────────────────────────────


def test_the_target_is_the_stores_service_and_the_port_its_policy_admits() -> None:
    (target,) = re.findall(r"^readonly NETWORK_RATE_STORE=(\S+)$", SMOKE_SH, re.M)
    documents = list(rendered_chart())
    (service,) = [
        d
        for d in documents
        if d["kind"] == "Service" and d["metadata"]["name"] == RATE_STORE
    ]
    (certificate,) = [
        d
        for d in documents
        if d["kind"] == "Certificate" and d["metadata"]["name"] == RATE_STORE
    ]

    assert target == TARGET
    assert certificate["spec"]["dnsNames"] == [HOST]
    assert [p["port"] for p in service["spec"]["ports"]] == [int(PORT)]


def test_only_the_gateway_has_the_store_in_its_ingress_or_its_egress() -> None:
    policies = network_policies(list(rendered_chart()))

    # The store admits the gateway's pods and nobody else.
    assert allowed_services(policies[RATE_STORE], "ingress") == {"model-gateway"}
    assert "claims-api" not in allowed_services(policies[RATE_STORE], "ingress")
    # And the only policy with an egress rule to it is the gateway's.
    assert [
        name
        for name, policy in policies.items()
        if RATE_STORE in allowed_services(policy, "egress")
    ] == ["model-gateway"]


def test_check_eight_runs_the_line_last_after_the_control_has_passed() -> None:
    body = function_body(SMOKE_SH, "check_network_policy").strip().splitlines()

    assert [line.strip() for line in body[-3:]] == [
        "check_network_database",
        "check_network_collector",
        "check_network_rate_store",
    ]
    # The control returns before any of them when it fails: the line never
    # prints a "blocked" that a broken probe could have made.
    assert body.index("    return 0") < body.index("  check_network_rate_store")


def flat_header_of_check_eight() -> str:
    header = SMOKE_SH.split("set -euo pipefail")[0]
    eighth = header.split("8. network policy: six lines")[1].split("9. service")[0]
    return " ".join(line.removeprefix("#").strip() for line in eighth.splitlines())


def test_the_header_says_six_lines_and_what_the_sixth_does_not_prove() -> None:
    flat = flat_header_of_check_eight()

    assert "the rate store (S066)" in flat
    assert "rate-store.meridian.svc:6379" in flat
    assert "What the sixth line does not prove" in flat
    # Its blind spot is named: the Claims API's own egress would block it too.
    assert "egress" in flat.split("What the sixth line does not prove")[1]
    assert "the store's ingress" in flat
    # And where the store's other states are read.
    assert "check 5" in flat
    assert "a name that does not resolve" in flat


def test_check_fives_header_says_its_series_means_the_store_answered() -> None:
    header = SMOKE_SH.split("set -euo pipefail")[0]
    fifth = header.split("5. cost panel:")[1].split("6. adjuster pages")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in fifth.splitlines())

    assert "the rate store answered" in flat
    # The reason: the gateway refuses every call it cannot count.
    assert "refuses every" in flat
    # And what no check does: read the store, which would need its credential.
    assert "No line reads the store" in flat
    assert "credential" in flat
