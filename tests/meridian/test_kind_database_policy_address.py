"""The database pod reaches the API server's address alone (S063, N3, T-84).

The database's NetworkPolicy used to let its pod open TCP 6443 at any address,
because the API server is the node itself, at an address that changes with every
new cluster. Now `make up` reads the address (the `kubernetes` EndpointSlice in
`default`) and applies the policy with it: the manifest holds a placeholder that
is not a CIDR, so a plain `kubectl apply -f` of the file is refused by the API
server, and `up.sh` puts one `ipBlock` per address in its place. `deploy.sh` and
`make smoke` compare the policy's CIDRs with the endpoint's addresses and say
"the API server's address changed: run make up" when they differ.

Nothing here needs a cluster. The functions of ``common.sh``, ``up.sh``,
``deploy.sh`` and ``smoke.sh`` run in bash against a stub ``kctl`` that answers
the two reads (the slice and the policy) and keeps what ``kubectl apply`` was
given on stdin. Addresses are from the documentation range 192.0.2.0/24 (RFC
5737): no cluster's own goes into a test. What no test here proves: that the
network plugin enforces an `ipBlock` rule after the Service's address is
translated to the node's; the report of the change gives the commands to prove
it by hand once.
"""

import copy
import ipaddress
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from test_kind_manifests import (
    COMMON_SH,
    DB_POLICY_FILE,
    DEPLOY_SH,
    KIND_DIR,
    SMOKE_SH,
    UP_SH,
    function_body,
    function_definition,
    one_line_function,
    requires_jq,
)

pytestmark = requires_jq

NODE = "192.0.2.10"
OTHER_NODE = "192.0.2.11"
CHANGED = "the API server's address changed: run make up"
COMMON_FUNCTIONS = (
    "api_server_shown",
    "read_api_server_addresses",
    "read_database_policy_cidrs",
    "api_server_matches_policy",
)
STUB = r"""
kctl() {
  printf '%s\n' "${*//$'\n'/ }" >>"${ASKED}"
  case "$*" in
    *"get endpointslices"*)
      [[ "${SLICE_STATUS}" == 0 ]] || { echo "Error from server: boom" >&2; return 1; }
      printf '%s' "${SLICE}" ;;
    *"get networkpolicy platform-db"*)
      [[ "${POLICY_STATUS}" == 0 ]] || { echo "Error from server: boom" >&2; return 1; }
      printf '%s' "${POLICY}" ;;
    *"get database"*) printf true ;;
    "apply "*) cat >"${APPLIED}" ;;
  esac
}
"""


def slice_of(*addresses: str, address_type: str = "IPv4") -> dict[str, object]:
    return {
        "addressType": address_type,
        "endpoints": [{"addresses": [address]} for address in addresses],
        "ports": [{"name": "https", "port": 6443, "protocol": "TCP"}],
    }


def slices(*items: dict[str, object]) -> str:
    return json.dumps({"items": list(items)})


def one_slice(*addresses: str) -> str:
    return slices(slice_of(*addresses))


def policy_of(*cidrs: str) -> str:
    """The database's policy as the API server returns it. No CIDR: the rule as
    it was before this change, with ports and no `to`."""
    api_server: dict[str, object] = {"ports": [{"port": 6443, "protocol": "TCP"}]}
    if cidrs:
        api_server["to"] = [{"ipBlock": {"cidr": cidr}} for cidr in cidrs]
    dns = {"ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]}
    return json.dumps({"spec": {"egress": [dns, api_server, {"to": [{}]}]}})


def run_bash(
    tmp_path: Path,
    parts: list[str],
    *,
    slice_json: str = "",
    slice_status: int = 0,
    policy: str = "",
    policy_status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """The script made of ``parts`` in bash against ``STUB``. Returns the
    process, what ``kubectl apply`` was given on stdin ('' when it was not
    called) and what ``kctl`` was asked, one call per line."""
    asked, applied = tmp_path / "kctl-calls", tmp_path / "applied.yaml"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'die() { echo "error: $*" >&2; exit 1; }',
            'log() { echo "==> $*"; }',
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            *re.findall(r"^readonly API_SERVER_[A-Z_]+=.*$", COMMON_SH, re.M),
            *(function_definition(COMMON_SH, name) for name in COMMON_FUNCTIONS),
            STUB,
            *parts,
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "KIND_DIR": str(KIND_DIR),
            "ASKED": str(asked),
            "APPLIED": str(applied),
            "SLICE": slice_json,
            "SLICE_STATUS": str(slice_status),
            "POLICY": policy,
            "POLICY_STATUS": str(policy_status),
        },
        check=False,
    )
    text = applied.read_text() if applied.exists() else ""
    return done, text, asked.read_text()


# ── up.sh: the policy is applied with the address ────────────────────────────


# The line of up.sh that applies the database's policy: one function serves this
# file and cert-manager's (test_kind_cert_manager_policy_address.py), and each
# call names its file and whose policy it is.
DATABASE_CALL = 'apply_api_server_policy "${DATABASE_POLICY_FILE}" "the database\'s"'


def up_parts() -> list[str]:
    return [
        *re.findall(
            r"^readonly (?:DATABASE_POLICY_FILE|API_SERVER_PEERS_PLACEHOLDER)=.*$",
            UP_SH,
            re.M,
        ),
        function_definition(UP_SH, "api_server_policy_manifest"),
        function_definition(UP_SH, "apply_api_server_policy"),
        DATABASE_CALL,
    ]


def apply_policy(
    tmp_path: Path, slice_json: str, **kwargs: object
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    return run_bash(tmp_path, up_parts(), slice_json=slice_json, **kwargs)  # type: ignore[arg-type]


def api_server_rule(policy: dict) -> dict:
    (rule,) = [
        r
        for r in policy["spec"]["egress"]
        if any(p["port"] == 6443 for p in r.get("ports", []))
    ]
    return rule


def committed_policy() -> dict:
    return yaml.safe_load(DB_POLICY_FILE.read_text(encoding="utf-8"))


def test_up_applies_the_policy_with_the_one_address_of_the_api_server(
    tmp_path: Path,
) -> None:
    done, applied, asked = apply_policy(tmp_path, one_slice(NODE))

    assert done.returncode == 0, done.stderr
    policy = yaml.safe_load(applied)
    assert api_server_rule(policy)["to"] == [{"ipBlock": {"cidr": f"{NODE}/32"}}]
    assert "get endpointslices" in asked
    assert "-n default" in asked
    assert "kubernetes.io/service-name=kubernetes" in asked
    assert "apply --server-side --force-conflicts -f -" in asked
    assert NODE in done.stdout  # the log line says which address was read


def test_up_applies_the_policy_with_every_address_the_endpoint_holds(
    tmp_path: Path,
) -> None:
    both = slices(slice_of(OTHER_NODE, NODE), slice_of(NODE))

    done, applied, _ = apply_policy(tmp_path, both)

    assert done.returncode == 0, done.stderr
    assert api_server_rule(yaml.safe_load(applied))["to"] == [
        {"ipBlock": {"cidr": f"{NODE}/32"}},
        {"ipBlock": {"cidr": f"{OTHER_NODE}/32"}},
    ]


def test_up_changes_the_api_server_rule_and_nothing_else_of_the_policy(
    tmp_path: Path,
) -> None:
    _, applied, _ = apply_policy(tmp_path, one_slice(NODE))

    expected = copy.deepcopy(committed_policy())
    api_server_rule(expected)["to"] = [{"ipBlock": {"cidr": f"{NODE}/32"}}]
    assert yaml.safe_load(applied) == expected
    assert len(list(yaml.safe_load_all(applied))) == 1
    assert applied.endswith("\n")
    assert "API-SERVER" not in applied


def test_up_reads_only_ipv4_slices_and_ignores_an_ipv6_one(tmp_path: Path) -> None:
    both = slices(slice_of("2001:db8::1", address_type="IPv6"), slice_of(NODE))

    done, applied, _ = apply_policy(tmp_path, both)

    assert done.returncode == 0, done.stderr
    assert api_server_rule(yaml.safe_load(applied))["to"] == [
        {"ipBlock": {"cidr": f"{NODE}/32"}}
    ]


NOT_ADDRESSES = [
    "not-an-address",
    "192.0.2.256",
    "192.0.2",
    "192.0.2.10/32",
    "192.0.2.010",
    "2001:db8::1",
    "192.0.2.10}}, {ipBlock: {cidr: 0.0.0.0/0",
    "192.0.2.10\n- ports: []",
    " 192.0.2.10",
]


@pytest.mark.parametrize("answer", NOT_ADDRESSES)
def test_up_stops_when_the_endpoint_holds_something_that_is_not_an_ipv4_address(
    tmp_path: Path, answer: str
) -> None:
    done, applied, _ = apply_policy(tmp_path, one_slice(answer))

    assert done.returncode != 0
    assert applied == ""
    assert "not an IPv4 address" in done.stderr
    assert "run make up" not in done.stderr


def test_up_names_what_it_read_when_it_stops_and_cuts_it_short(
    tmp_path: Path,
) -> None:
    done, _, _ = apply_policy(tmp_path, one_slice("x" * 500 + "\x1b[31m"))

    assert done.returncode != 0
    assert "x" * 120 in done.stderr
    assert "x" * 121 not in done.stderr
    assert "\x1b" not in done.stderr


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps({"items": []}),
        slices(slice_of()),
        slices(slice_of("2001:db8::1", address_type="IPv6")),
    ],
    ids=["no slice", "no endpoint", "only an IPv6 slice"],
)
def test_up_stops_when_the_endpoint_holds_no_ipv4_address(
    tmp_path: Path, answer: str
) -> None:
    done, applied, _ = apply_policy(tmp_path, answer)

    assert done.returncode != 0
    assert applied == ""
    assert "no IPv4 address" in done.stderr


@pytest.mark.parametrize("answer", ["", "not json"], ids=["empty", "not json"])
def test_up_stops_when_the_answer_is_not_a_list_of_slices(
    tmp_path: Path, answer: str
) -> None:
    done, applied, _ = apply_policy(tmp_path, answer)

    assert done.returncode != 0
    assert applied == ""
    assert "kubernetes" in done.stderr


def test_up_stops_when_the_endpoint_cannot_be_read(tmp_path: Path) -> None:
    done, applied, _ = apply_policy(tmp_path, one_slice(NODE), slice_status=1)

    assert done.returncode != 0
    assert applied == ""
    assert "Error from server: boom" in done.stderr
    assert "could not read" in done.stderr


# ── the manifest as committed ────────────────────────────────────────────────


def placeholder() -> str:
    (value,) = re.findall(
        r"^readonly API_SERVER_PEERS_PLACEHOLDER='(.*)'$", UP_SH, re.M
    )
    return value


def test_the_manifest_holds_the_placeholder_once_and_it_is_not_a_cidr() -> None:
    text = DB_POLICY_FILE.read_text(encoding="utf-8")
    rule = api_server_rule(committed_policy())

    assert text.count(placeholder()) == 1
    (peer,) = rule["to"]
    with pytest.raises(ValueError):
        ipaddress.ip_network(peer["ipBlock"]["cidr"])


def test_the_manifest_as_committed_names_no_address_and_every_rule_has_a_peer() -> None:
    text = DB_POLICY_FILE.read_text(encoding="utf-8")
    policy = committed_policy()

    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert all("to" in rule for rule in policy["spec"]["egress"])
    assert "0.0.0.0/0" not in text
    assert api_server_rule(policy)["ports"] == [{"port": 6443, "protocol": "TCP"}]


def test_the_header_of_the_manifest_says_what_is_true_and_what_is_not_proved() -> None:
    header = DB_POLICY_FILE.read_text(encoding="utf-8").split("apiVersion:")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in header.splitlines())

    assert "any address" not in flat
    assert "no address" not in flat
    assert "EndpointSlice" in flat
    assert "make up" in flat
    assert "a plain `kubectl apply -f`" in flat
    assert "not proved" in flat
    assert "kindnet" in flat
    assert "by hand" in flat


# ── up.sh: it runs on a running cluster too ──────────────────────────────────


def test_up_applies_the_database_policy_on_every_run_before_the_database() -> None:
    lines = UP_SH.splitlines()
    (namespaces,) = [
        i for i, line in enumerate(lines) if "manifests/namespaces.yaml" in line
    ]
    (applied,) = [i for i, line in enumerate(lines) if line == DATABASE_CALL]
    (operator,) = [
        i for i, line in enumerate(lines) if line.startswith("install_release cnpg ")
    ]
    (database,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release platform-db")
    ]

    assert namespaces < applied < operator < database
    assert lines[applied - 1].startswith("log ")
    # A call at the top level, after the cluster is created or found: a run on
    # a cluster that exists reaches it, and it is not inside a condition.
    assert lines.index("create_cluster") < applied
    assert not lines[applied - 2].startswith(("if ", "  "))
    # The file is applied through the function and nowhere else, from stdin.
    (path_lines,) = [
        line for line in lines if "manifests/platform-db-networkpolicy.yaml" in line
    ]
    assert path_lines.startswith("readonly DATABASE_POLICY_FILE=")
    body = function_body(UP_SH, "apply_api_server_policy")
    assert "apply --server-side --force-conflicts -f -" in body
    assert 'kctl apply --server-side --force-conflicts -f "' not in body


# ── deploy.sh: require_database ──────────────────────────────────────────────


def run_require_database(
    tmp_path: Path, *, slice_json: str, policy: str, **kwargs: int
) -> tuple[subprocess.CompletedProcess[str], str]:
    parts = [
        "NAMESPACE=meridian; DATABASE_ROLES=()",
        "database_roles_reconciled() { return 0; }",
        function_definition(DEPLOY_SH, "require_database"),
        "require_database",
        "echo passed",
    ]
    done, _, asked = run_bash(
        tmp_path, parts, slice_json=slice_json, policy=policy, **kwargs
    )
    return done, asked


def test_deploy_goes_on_when_the_policy_names_the_address_of_the_endpoint(
    tmp_path: Path,
) -> None:
    done, _ = run_require_database(
        tmp_path, slice_json=one_slice(NODE), policy=policy_of(f"{NODE}/32")
    )

    assert done.returncode == 0, done.stderr
    assert "passed" in done.stdout


def test_deploy_compares_every_address_not_the_first_only(tmp_path: Path) -> None:
    done, _ = run_require_database(
        tmp_path,
        slice_json=one_slice(NODE, OTHER_NODE),
        policy=policy_of(f"{OTHER_NODE}/32", f"{NODE}/32"),
    )

    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    ("addresses", "cidrs"),
    [
        ((OTHER_NODE,), (f"{NODE}/32",)),
        ((NODE, OTHER_NODE), (f"{NODE}/32",)),
        ((NODE,), (f"{NODE}/32", f"{OTHER_NODE}/32")),
        ((NODE,), (f"{NODE}/24",)),
        ((NODE,), ()),
    ],
    ids=["moved", "one more address", "one more cidr", "wider block", "no peer"],
)
def test_deploy_dies_with_make_up_when_the_policy_names_another_address(
    tmp_path: Path, addresses: tuple[str, ...], cidrs: tuple[str, ...]
) -> None:
    done, _ = run_require_database(
        tmp_path, slice_json=one_slice(*addresses), policy=policy_of(*cidrs)
    )

    assert done.returncode != 0
    assert CHANGED in done.stderr
    assert "passed" not in done.stdout


def test_deploy_says_it_could_not_read_the_endpoint_and_does_not_say_it_changed(
    tmp_path: Path,
) -> None:
    done, _ = run_require_database(
        tmp_path,
        slice_json=one_slice(NODE),
        policy=policy_of(f"{NODE}/32"),
        slice_status=1,
    )

    assert done.returncode != 0
    assert "could not read" in done.stderr
    assert CHANGED not in done.stderr


def test_deploy_dies_without_saying_changed_when_the_policy_cannot_be_read(
    tmp_path: Path,
) -> None:
    done, _ = run_require_database(
        tmp_path,
        slice_json=one_slice(NODE),
        policy=policy_of(f"{NODE}/32"),
        policy_status=1,
    )

    assert done.returncode != 0
    assert "passed" not in done.stdout
    assert CHANGED not in done.stderr


def test_deploy_reads_the_policy_and_the_endpoint_and_changes_nothing(
    tmp_path: Path,
) -> None:
    _, asked = run_require_database(
        tmp_path, slice_json=one_slice(NODE), policy=policy_of(f"{NODE}/32")
    )

    assert "get endpointslices" in asked
    assert not re.search(r"\b(apply|create|delete|patch|label|exec)\b", asked)


# ── smoke.sh: the line of the database check ─────────────────────────────────


def run_database_policy_check(
    tmp_path: Path,
    *,
    slice_json: str | None = None,
    policy: str | None = None,
    **kwargs: int,
) -> tuple[list[str], str]:
    """``check_database_api_server`` of smoke.sh. The defaults are a cluster
    where the policy names the endpoint's one address. Returns the output lines
    and what ``kctl`` was asked."""
    parts = [
        one_line_function(SMOKE_SH, "clean_lines"),
        function_definition(SMOKE_SH, "check_database_api_server"),
        "check_database_api_server",
    ]
    done, _, asked = run_bash(
        tmp_path,
        parts,
        slice_json=one_slice(NODE) if slice_json is None else slice_json,
        policy=policy_of(f"{NODE}/32") if policy is None else policy,
        **kwargs,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines(), asked


def test_smoke_passes_one_line_when_the_policy_names_the_endpoints_address(
    tmp_path: Path,
) -> None:
    lines, _ = run_database_policy_check(tmp_path)

    assert len(lines) == 1
    assert lines[0].startswith("PASS  database: ")
    assert "platform-db" in lines[0]
    assert "6443" in lines[0]


def test_smoke_passes_when_there_are_two_addresses_and_the_policy_names_both(
    tmp_path: Path,
) -> None:
    lines, _ = run_database_policy_check(
        tmp_path,
        slice_json=one_slice(NODE, OTHER_NODE),
        policy=policy_of(f"{OTHER_NODE}/32", f"{NODE}/32"),
    )

    assert [line.split()[0] for line in lines] == ["PASS"]


@pytest.mark.parametrize(
    "policy",
    [
        policy_of(f"{OTHER_NODE}/32"),
        policy_of(f"{NODE}/32", f"{OTHER_NODE}/32"),
        policy_of(),
    ],
    ids=["another address", "one more cidr", "the rule before this change"],
)
def test_smoke_fails_with_make_up_when_the_policy_and_the_endpoint_differ(
    tmp_path: Path, policy: str
) -> None:
    lines, _ = run_database_policy_check(tmp_path, policy=policy)

    assert len(lines) == 1
    assert lines[0].startswith(f"FAIL  database: {CHANGED}")


def test_smoke_fails_without_saying_changed_when_the_endpoint_cannot_be_read(
    tmp_path: Path,
) -> None:
    lines, _ = run_database_policy_check(tmp_path, slice_status=1)

    assert [line.split()[0] for line in lines] == ["FAIL"]
    assert "could not read" in lines[0]
    assert CHANGED not in lines[0]


def test_smoke_fails_without_saying_changed_when_the_policy_cannot_be_read(
    tmp_path: Path,
) -> None:
    lines, _ = run_database_policy_check(tmp_path, policy_status=1)

    assert [line.split()[0] for line in lines] == ["FAIL"]
    assert "could not read" in lines[0]
    assert CHANGED not in lines[0]


def test_smoke_fails_when_the_policy_is_missing_and_says_make_up(
    tmp_path: Path,
) -> None:
    lines, _ = run_database_policy_check(tmp_path, policy="")

    assert [line.split()[0] for line in lines] == ["FAIL"]
    assert "platform-db" in lines[0]
    assert "run make up" in lines[0]


def test_smoke_fails_when_the_endpoint_holds_no_address(tmp_path: Path) -> None:
    lines, _ = run_database_policy_check(tmp_path, slice_json=slices())

    assert [line.split()[0] for line in lines] == ["FAIL"]
    assert "no IPv4 address" in lines[0]


def test_smoke_reads_and_changes_nothing_and_prints_nothing_but_the_one_line(
    tmp_path: Path,
) -> None:
    lines, asked = run_database_policy_check(tmp_path)

    assert len(lines) == 1
    assert not re.search(r"\b(apply|create|delete|patch|label|exec)\b", asked)
    assert "--request-timeout" in asked


def test_smoke_prints_nothing_a_hostile_endpoint_could_inject(tmp_path: Path) -> None:
    lines, _ = run_database_policy_check(
        tmp_path, slice_json=one_slice("1.2.3.4\x1b]0;x\x07\nPASS  forged")
    )

    assert len(lines) == 1
    assert lines[0].startswith("FAIL")
    assert "\x1b" not in lines[0] and "\x07" not in lines[0]


def test_the_database_check_calls_the_line_before_it_looks_for_the_primary() -> None:
    body = function_body(SMOKE_SH, "check_database")

    assert body.index("check_database_api_server") < body.index("primary=")


def test_the_header_of_smoke_says_what_the_line_does_not_prove() -> None:
    header = SMOKE_SH.split("set -euo pipefail")[0]
    database = header.split("2. database:")[1].split("3. tools:")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in database.splitlines())

    assert "API server" in flat
    assert "EndpointSlice" in flat
    assert "does not prove" in flat or "not prove" in flat
    assert "path is closed" in flat or "closed" in flat
    assert "by hand" in flat
