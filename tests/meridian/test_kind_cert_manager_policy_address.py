"""cert-manager's pods reach the API server's address alone (S063, contract FB).

cert-manager's egress rule for TCP 6443 used to name the port and no address, as
the database's did until contract N3. Now the file holds the same placeholder
(not a CIDR, so a plain `kubectl apply -f` of it is refused by the API server)
on that one rule, and `up.sh` fills the API server's address in through the one
function that also serves the database's policy (``apply_api_server_policy``).
The three 10250 ingress rules (the webhooks) are not narrowed: the address the
API server's calls arrive from is not known to be the endpoint's.

Nothing here needs a cluster. The functions of ``common.sh`` and ``up.sh`` run
in bash against the stub ``kctl`` of ``test_kind_database_policy_address.py``,
and the addresses are from the documentation range (RFC 5737). What no test
proves: that the network plugin lets cert-manager's pods reach the API server
with this rule on a cluster that is made cold; ``make up`` is that test.
"""

import copy
import ipaddress
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from test_kind_database_policy_address import (
    NODE,
    NOT_ADDRESSES,
    OTHER_NODE,
    api_server_rule,
    one_slice,
    placeholder,
    run_bash,
    slice_of,
    slices,
)
from test_kind_manifests import UP_SH, function_body, function_definition, requires_jq
from test_kind_namespace_policies import CERT_MANAGER_FILE, header_of

pytestmark = requires_jq

CERT_MANAGER_CALL = (
    'apply_api_server_policy "${CERT_MANAGER_POLICY_FILE}" "cert-manager\'s"'
)


def parts(call: str = CERT_MANAGER_CALL) -> list[str]:
    return [
        *re.findall(
            r"^readonly (?:CERT_MANAGER_POLICY_FILE|API_SERVER_PEERS_PLACEHOLDER)=.*$",
            UP_SH,
            re.M,
        ),
        function_definition(UP_SH, "api_server_policy_manifest"),
        function_definition(UP_SH, "apply_api_server_policy"),
        call,
    ]


def apply_policy(
    tmp_path: Path, slice_json: str, **kwargs: object
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    return run_bash(tmp_path, parts(), slice_json=slice_json, **kwargs)  # type: ignore[arg-type]


def committed() -> list[dict]:
    return list(yaml.safe_load_all(CERT_MANAGER_FILE.read_text(encoding="utf-8")))


def egress_policy(documents: list[dict]) -> dict:
    (policy,) = [d for d in documents if d["metadata"]["name"] == "egress"]
    return policy


# ── up.sh: the policy is applied with the address ────────────────────────────


def test_up_applies_cert_managers_policies_with_the_one_address_of_the_api_server(
    tmp_path: Path,
) -> None:
    done, applied, asked = apply_policy(tmp_path, one_slice(NODE))

    assert done.returncode == 0, done.stderr
    egress = egress_policy(list(yaml.safe_load_all(applied)))
    assert api_server_rule(egress)["to"] == [{"ipBlock": {"cidr": f"{NODE}/32"}}]
    assert "get endpointslices" in asked
    assert "kubernetes.io/service-name=kubernetes" in asked
    assert "apply --server-side --force-conflicts -f -" in asked
    assert NODE in done.stdout  # the log line says which address was read
    assert "cert-manager" in done.stdout


def test_up_applies_cert_managers_policy_with_every_address_the_endpoint_holds(
    tmp_path: Path,
) -> None:
    both = slices(slice_of(OTHER_NODE, NODE), slice_of(NODE))

    done, applied, _ = apply_policy(tmp_path, both)

    assert done.returncode == 0, done.stderr
    egress = egress_policy(list(yaml.safe_load_all(applied)))
    assert api_server_rule(egress)["to"] == [
        {"ipBlock": {"cidr": f"{NODE}/32"}},
        {"ipBlock": {"cidr": f"{OTHER_NODE}/32"}},
    ]


def test_up_changes_the_6443_rule_of_the_egress_policy_and_nothing_else(
    tmp_path: Path,
) -> None:
    _, applied, _ = apply_policy(tmp_path, one_slice(NODE))

    expected = copy.deepcopy(committed())
    api_server_rule(egress_policy(expected))["to"] = [
        {"ipBlock": {"cidr": f"{NODE}/32"}}
    ]
    assert list(yaml.safe_load_all(applied)) == expected
    assert len(expected) == 5  # all five policies of the file, applied together
    assert applied.endswith("\n")
    assert "API-SERVER" not in applied


def test_up_narrows_no_webhook_rule_and_the_only_ipblock_is_the_api_servers(
    tmp_path: Path,
) -> None:
    _, applied, _ = apply_policy(tmp_path, one_slice(NODE))

    documents = list(yaml.safe_load_all(applied))
    webhooks = [d for d in documents if d["metadata"]["name"].endswith("webhook")]
    assert len(webhooks) == 2
    for policy in webhooks:
        assert policy["spec"]["ingress"] == [
            {"ports": [{"port": 10250, "protocol": "TCP"}]}
        ]
    assert json.dumps(documents).count("ipBlock") == 1


@pytest.mark.parametrize("answer", NOT_ADDRESSES)
def test_up_applies_nothing_of_cert_managers_when_the_endpoint_is_not_an_address(
    tmp_path: Path, answer: str
) -> None:
    done, applied, _ = apply_policy(tmp_path, one_slice(answer))

    assert done.returncode != 0
    assert applied == ""
    assert "not an IPv4 address" in done.stderr
    assert "cert-manager's NetworkPolicy was not changed" in done.stderr


@pytest.mark.parametrize(
    "answer",
    [
        slices(),
        slices(slice_of()),
        slices(slice_of("2001:db8::1", address_type="IPv6")),
        "not json",
    ],
    ids=["no slice", "no endpoint", "only an IPv6 slice", "not json"],
)
def test_up_applies_nothing_of_cert_managers_when_the_endpoint_holds_no_address(
    tmp_path: Path, answer: str
) -> None:
    done, applied, _ = apply_policy(tmp_path, answer)

    assert done.returncode != 0
    assert applied == ""
    assert "cert-manager's NetworkPolicy was not changed" in done.stderr


def test_up_applies_nothing_of_cert_managers_when_the_endpoint_cannot_be_read(
    tmp_path: Path,
) -> None:
    done, applied, _ = apply_policy(tmp_path, one_slice(NODE), slice_status=1)

    assert done.returncode != 0
    assert applied == ""
    assert "could not read" in done.stderr


# ── the file must hold the placeholder exactly once ──────────────────────────


def run_on_copy(
    tmp_path: Path, text: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    copy_of_file = tmp_path / "cert-manager-networkpolicy.yaml"
    copy_of_file.write_text(text, encoding="utf-8")
    call = f'apply_api_server_policy "{copy_of_file}" "cert-manager\'s"'
    done, applied, _ = run_bash(
        tmp_path, parts(call), slice_json=one_slice(NODE), policy=""
    )
    return done, applied


def test_up_refuses_a_file_that_lost_the_placeholder_and_applies_nothing(
    tmp_path: Path,
) -> None:
    text = CERT_MANAGER_FILE.read_text(encoding="utf-8")
    without = text.replace(placeholder(), "to: [{ipBlock: {cidr: 192.0.2.99/32}}]")
    assert without != text

    done, applied = run_on_copy(tmp_path, without)

    assert done.returncode != 0
    assert applied == ""
    assert "does not hold the placeholder" in done.stderr


def test_up_refuses_a_file_that_holds_the_placeholder_twice_and_applies_nothing(
    tmp_path: Path,
) -> None:
    text = CERT_MANAGER_FILE.read_text(encoding="utf-8")

    done, applied = run_on_copy(tmp_path, f"{text}\n# {placeholder()}\n")

    assert done.returncode != 0
    assert applied == ""
    assert "more than once" in done.stderr


def test_the_control_of_the_two_refusals_is_the_file_as_committed(
    tmp_path: Path,
) -> None:
    text = CERT_MANAGER_FILE.read_text(encoding="utf-8")

    done, applied = run_on_copy(tmp_path, text)

    assert done.returncode == 0, done.stderr
    assert NODE in applied


# ── one function serves both files ───────────────────────────────────────────


def test_one_function_applies_both_files_and_the_file_is_an_argument() -> None:
    lines = UP_SH.splitlines()
    calls = [line for line in lines if line.startswith("apply_api_server_policy ")]

    assert len(calls) == 2
    assert 'apply_api_server_policy "${DATABASE_POLICY_FILE}" ' in calls[0]
    assert calls[1] == CERT_MANAGER_CALL
    assert UP_SH.count("apply_api_server_policy() {") == 1
    assert UP_SH.count("api_server_policy_manifest() {") == 1
    body = function_body(UP_SH, "apply_api_server_policy")
    assert "manifests/" not in body
    # The two files are applied through the function and nowhere else.
    (path_lines,) = [
        line for line in lines if "manifests/cert-manager-networkpolicy.yaml" in line
    ]
    assert path_lines.startswith("readonly CERT_MANAGER_POLICY_FILE=")


# ── the manifest as committed ────────────────────────────────────────────────


def test_the_manifest_holds_the_placeholder_once_and_it_is_not_a_cidr() -> None:
    text = CERT_MANAGER_FILE.read_text(encoding="utf-8")
    rule = api_server_rule(egress_policy(committed()))

    assert text.count(placeholder()) == 1
    (peer,) = rule["to"]
    with pytest.raises(ValueError):
        ipaddress.ip_network(peer["ipBlock"]["cidr"])
    assert rule["ports"] == [{"port": 6443, "protocol": "TCP"}]


def test_the_manifest_names_no_address_and_every_egress_rule_has_a_peer() -> None:
    text = CERT_MANAGER_FILE.read_text(encoding="utf-8")

    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert "0.0.0.0/0" not in text
    egress = egress_policy(committed())["spec"]["egress"]
    assert all("to" in rule for rule in egress)


def test_the_header_says_what_make_up_fills_and_what_a_stale_address_shows() -> None:
    header = header_of(CERT_MANAGER_FILE)

    # The egress rule is the database's case: a placeholder, filled by `make up`.
    assert "EndpointSlice" in header and "make up" in header
    assert "a plain `kubectl apply -f`" in header
    assert "TCP 6443 to any address" not in header
    # The webhook rules are not: the address of the API server's calls is not
    # known to be the endpoint's, nobody measured it, and the rule stays open.
    assert "not known" in header and "measured" in header
    assert "the same case" not in header
    # What the open ports allow, said plainly (any pod, forged reviews, a flood).
    assert "forged" in header and "fail closed" in header
    assert "stall" in header
    # deploy.sh and smoke.sh do not compare this policy with the endpoint, and
    # how a stale address shows itself.
    assert "do not compare" in header
    assert "Certificates stop being issued" in header
    assert "Not proved" in header and "kindnet" in header


def test_the_header_of_up_says_both_policies_get_the_address() -> None:
    header = UP_SH.split("set -euo pipefail")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in header.splitlines())

    assert "the database's and cert-manager's" in flat
    assert "EndpointSlice" in flat
