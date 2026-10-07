"""The CloudNativePG operator is confined to `meridian` (S072, contract C).

The operator used to run in a namespace of its own with a ClusterRole that reads
and writes Secrets and ConfigMaps, and execs into pods, in every namespace. With
the chart's ``config.clusterWide=false`` and the release made in `meridian`, that
rule set is a Role in `meridian` only and the ClusterRole shrinks to nodes (read),
webhook configurations (get, patch) and image catalogs (read). The chart's
``default-deny`` (``make deploy``) selects every pod of `meridian`, the
operator's too, so kind's own policy for the operator is applied by ``make up``
before the release: it is an add-on of kind, not a part of the application.

Nothing here needs a cluster. The labels and ports come from ``helm template``
of chart 0.29.1 with ``config.clusterWide=false`` and ``--namespace meridian``
(2026-10-07); the function that fills in the node's address runs in bash against
the stub of ``test_kind_database_policy_address.py``, with an address from the
documentation range (RFC 5737). What no test proves: that a cold ``make up``
brings the database up under the confined operator (the fall-back in up.sh says
what to do when it does not).
"""

import ipaddress
import re
from pathlib import Path

import pytest
import yaml
from chartsupport import rules
from kindsupport import (
    DB_POLICY_FILE,
    KIND_DIR,
    UP_SH,
    function_definition,
    load_documents,
    requires_jq,
)
from test_kind_database_policy_address import NODE, one_slice, placeholder, run_bash
from test_kind_namespace_policies import (
    NAMESPACES_FILE,
    dns_rule,
    header_of,
    pods,
    policies_of,
    tcp,
)

MANIFESTS = KIND_DIR / "manifests"
OPERATOR_FILE = MANIFESTS / "cnpg-operator-networkpolicy.yaml"
OPERATOR_CALL = (
    'apply_api_server_policy "${CNPG_OPERATOR_POLICY_FILE}" '
    '"the CloudNativePG operator\'s"'
)
REPO_ROOT = KIND_DIR.parent.parent

# The operator's pod, as `helm template` renders chart 0.29.1 for the release
# `cnpg` (2026-10-07): the Deployment's selector, and the Service's. The second
# label is the release's name, which up.sh gives the install.
OPERATOR_LABELS = {
    "app.kubernetes.io/name": "cloudnative-pg",
    "app.kubernetes.io/instance": "cnpg",
}
DATABASE_LABELS = {"cnpg.io/cluster": "platform-db"}
# The instance manager's own HTTP port: what the operator asks each instance for
# its status (the database policy's header says what happens without the rule).
INSTANCE_MANAGER_PORT = 8000

# The namespace the operator used to live in, spelled so that this file does not
# name it. A line that names it must be history, and say so.
OLD_NAMESPACE = "cnpg-" + "system"
# Files that still name it: none. smoke.sh held two messages ("read the
# operator's log in <namespace>") until contract S of this batch said where the
# log is now, so the scan holds the whole tree.
STILL_NAMING: dict[str, int] = {}
SCANNED = (KIND_DIR, REPO_ROOT / "infra" / "helm", REPO_ROOT / "tests")
TEXT_SUFFIXES = {".py", ".sh", ".yaml", ".yml", ".md", ".env", ".json", ".jsonl", ""}


def operator_policy() -> dict:
    (policy,) = load_documents(OPERATOR_FILE)
    assert policy["kind"] == "NetworkPolicy"
    return policy


def database_rule_for_the_operator() -> dict:
    """The database policy's ingress rule that admits the operator: the one that
    names port 8000."""
    (rule,) = [
        rule
        for rule in rules(load_documents(DB_POLICY_FILE)[0], "ingress")
        if any(port["port"] == INSTANCE_MANAGER_PORT for port in rule.get("ports", []))
    ]
    return rule


# ── the release: in `meridian`, with a Role and not a ClusterRole ────────────


def cnpg_install_line() -> str:
    joined = re.sub(r"\\\n\s*", "", UP_SH)
    (line,) = [
        line for line in joined.splitlines() if line.startswith("install_release cnpg ")
    ]
    return line


def test_up_installs_the_operator_into_meridian_with_a_role_per_namespace() -> None:
    line = cnpg_install_line()

    assert line.startswith("install_release cnpg meridian ")
    assert OLD_NAMESPACE not in line
    # The chart's switch: the rules become a Role of the release's namespace.
    assert '--set "config.clusterWide=false"' in line
    # The pin and the chart stay as they were.
    assert '"${CNPG_OPERATOR_CHART}" "${CNPG_OPERATOR_VERSION}"' in line
    assert '"${CNPG_REPO}" cnpg.yaml' in line
    assert (
        '--set "image.tag=${CNPG_OPERATOR_IMAGE_TAG}@${CNPG_OPERATOR_IMAGE_DIGEST}"'
        in line
    )
    # Still ten installs, each through the one function.
    assert len(re.findall(r"^install_release ", UP_SH, re.MULTILINE)) == 10


def test_nothing_creates_the_operators_old_namespace() -> None:
    namespaces = [d["metadata"]["name"] for d in load_documents(NAMESPACES_FILE)]

    assert OLD_NAMESPACE not in namespaces
    assert "meridian" in namespaces
    assert "--create-namespace" not in UP_SH
    assert re.findall(rf"^\s*(?!#).*{OLD_NAMESPACE}", UP_SH, re.MULTILINE) == []


def test_the_operator_follows_the_namespaces_and_its_policy_and_precedes_the_db() -> (
    None
):
    lines = UP_SH.splitlines()
    (namespaces,) = [
        i for i, line in enumerate(lines) if "manifests/namespaces.yaml" in line
    ]
    (database_policy,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith('apply_api_server_policy "${DATABASE_POLICY_FILE}"')
    ]
    (applied,) = [i for i, line in enumerate(lines) if line == OPERATOR_CALL]
    (operator,) = [
        i for i, line in enumerate(lines) if line.startswith("install_release cnpg ")
    ]
    (database,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release platform-db")
    ]

    # Before any release, so that the pod never runs without the rules, and
    # next to the database's policy, which it belongs with.
    first_release = next(
        i for i, line in enumerate(lines) if line.startswith("install_release ")
    )
    assert namespaces < database_policy < applied < first_release
    assert applied < operator < database
    assert lines[applied - 1].startswith("log ")
    # A call at the top level, on every run, outside a condition.
    assert lines.index("create_cluster") < applied
    assert not lines[applied - 2].startswith(("if ", "  "))
    (path_lines,) = [
        line for line in lines if "manifests/cnpg-operator-networkpolicy.yaml" in line
    ]
    assert path_lines.startswith("readonly CNPG_OPERATOR_POLICY_FILE=")


def test_one_function_applies_the_five_files() -> None:
    calls = [
        line
        for line in UP_SH.splitlines()
        if line.startswith("apply_api_server_policy ")
    ]

    assert len(calls) == 5
    assert OPERATOR_CALL in calls
    assert UP_SH.count("apply_api_server_policy() {") == 1


def test_up_says_how_the_change_is_taken_out_and_what_a_failure_looks_like() -> None:
    comment = " ".join(
        line.removeprefix("#").strip()
        for line in UP_SH.splitlines()
        if line.startswith("#")
    )

    # The fall-back, where the reader of up.sh finds it.
    assert "tried, and what failed" in comment
    assert "namespace of its own" in comment and "cluster-wide" in comment
    # What "does not bring the database up" looks like: the wait that ends, the
    # Helm failure, and what the operator's log would say.
    assert "cluster/platform-db" in comment
    assert "helm release platform-db failed" in comment
    assert "Instance Status Extraction Error" in comment
    assert "operator's log" in comment
    # A cluster from before this change does not converge: Helm meets cluster-
    # scoped objects that the other namespace's release owns.
    assert "make down" in comment and "make up" in comment
    assert "from before" in comment


# ── the operator's policy ────────────────────────────────────────────────────


def test_the_operator_policy_selects_the_operators_pod_and_denies_both_ways() -> None:
    policy = operator_policy()

    assert policy["metadata"]["name"] == "cnpg-operator"
    assert policy["metadata"]["namespace"] == "meridian"
    assert policy["spec"]["podSelector"] == {"matchLabels": OPERATOR_LABELS}
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    # Ingress: no key, so no rule: the webhook's port and the metrics port are
    # reached by no pod (the API server calls from the node and needs no rule).
    assert "ingress" not in policy["spec"]
    assert rules(policy, "ingress") == []


def test_the_operator_may_reach_dns_the_api_server_and_the_database_pods_only() -> None:
    policy = operator_policy()

    assert rules(policy, "egress") == [
        dns_rule(),
        {"to": [{"ipBlock": {"cidr": "API-SERVER-ADDRESS/32"}}], "ports": tcp(6443)},
        {"to": [pods(DATABASE_LABELS)], "ports": tcp(INSTANCE_MANAGER_PORT)},
    ]


def test_no_rule_of_the_operator_policy_is_open_or_reaches_another_namespace() -> None:
    text = OPERATOR_FILE.read_text(encoding="utf-8")
    policy = operator_policy()

    assert "0.0.0.0/0" not in text and "::/0" not in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)  # no address in a file
    for rule in rules(policy, "egress"):
        assert rule.get("to") and rule.get("ports")
        for entry in rule["to"]:
            assert entry != {}
            if "ipBlock" in entry:
                assert entry["ipBlock"] == {"cidr": "API-SERVER-ADDRESS/32"}
                continue
            assert entry["podSelector"].get("matchLabels"), entry
            # A namespace is named only for the DNS pods, which live in kube-system.
            if "namespaceSelector" in entry:
                assert entry == pods({"k8s-app": "kube-dns"}, "kube-system")
    # Nothing towards the telemetry stack or anything else: the operator sends
    # none (the collector's rule admits `meridian`, and the chart's rules narrow
    # it, not this pod's: this file is what keeps the operator from it).
    assert "observability" not in yaml.safe_dump(policy)
    assert "4318" not in text and "4317" not in text


def test_the_operators_port_to_the_database_is_the_one_the_database_admits() -> None:
    (database,) = [r for r in rules(operator_policy(), "egress") if "to" in r][2:]
    rule = database_rule_for_the_operator()

    assert database["ports"] == rule["ports"] == tcp(INSTANCE_MANAGER_PORT)
    # The database's rule admits the operator by the labels of its pod, in the
    # policy's own namespace: the pod selector alone, no namespace selector.
    assert rule["from"] == [pods(OPERATOR_LABELS)]
    # 5432 is admitted to the Meridian pods alone: the operator reaches
    # PostgreSQL through the instance manager (values/platform-db.yaml: the
    # local socket), and no egress rule of the operator's names 5432.
    assert 5432 not in {
        p["port"] for r in rules(operator_policy(), "egress") for p in r["ports"]
    }


def test_the_database_policy_names_no_namespace_selector_at_all() -> None:
    policy = load_documents(DB_POLICY_FILE)[0]

    # The operator is in the database's namespace now: a namespace selector on
    # any ingress rule would be a rule written for the old place.
    for rule in rules(policy, "ingress"):
        for entry in rule["from"]:
            assert "namespaceSelector" not in entry, entry
    text = DB_POLICY_FILE.read_text(encoding="utf-8")
    assert "kubernetes.io/metadata.name: kube-system" in text  # egress, DNS only
    assert text.count("namespaceSelector") == 1


def test_the_placeholder_is_held_once_and_is_not_a_cidr() -> None:
    text = OPERATOR_FILE.read_text(encoding="utf-8")
    (rule,) = [
        r
        for r in rules(operator_policy(), "egress")
        if 6443 in {p["port"] for p in r["ports"]}
    ]

    assert text.count(placeholder()) == 1
    (peer,) = rule["to"]
    with pytest.raises(ValueError):
        ipaddress.ip_network(peer["ipBlock"]["cidr"])
    assert len(list(yaml.safe_load_all(text))) == 1


def test_the_policy_selects_the_pod_the_chart_labels_for_release_cnpg() -> None:
    # The second label is the release's name: a rename of the release would
    # leave the pod with no policy of its own and no way out of the chart's
    # default-deny, so the two are tied here.
    pins = (KIND_DIR / "pins.env").read_text(encoding="utf-8")
    release = cnpg_install_line().split()[1]

    assert OPERATOR_LABELS["app.kubernetes.io/instance"] == release == "cnpg"
    # The first label is the chart's name, which the pins hold.
    chart = OPERATOR_LABELS["app.kubernetes.io/name"]
    assert f"\nCNPG_OPERATOR_CHART={chart}\n" in pins
    # Both are the labels of the database policy's rule for the operator.
    assert database_rule_for_the_operator()["from"] == [pods(OPERATOR_LABELS)]


# ── the stand-in run of `make up`'s step ─────────────────────────────────────


def operator_parts() -> list[str]:
    return [
        *re.findall(
            r"^readonly (?:CNPG_OPERATOR_POLICY_FILE|API_SERVER_PEERS_PLACEHOLDER)=.*$",
            UP_SH,
            re.M,
        ),
        function_definition(UP_SH, "api_server_policy_manifest"),
        function_definition(UP_SH, "apply_api_server_policy"),
        OPERATOR_CALL,
    ]


@requires_jq
def test_up_applies_the_policy_with_the_address_and_changes_nothing_else(
    tmp_path: Path,
) -> None:
    done, applied, asked = run_bash(
        tmp_path, operator_parts(), slice_json=one_slice(NODE)
    )

    assert done.returncode == 0, done.stderr
    expected = operator_policy()
    expected["spec"]["egress"][1]["to"] = [{"ipBlock": {"cidr": f"{NODE}/32"}}]
    assert yaml.safe_load(applied) == expected
    assert "API-SERVER" not in applied
    assert "apply --server-side --force-conflicts -f -" in asked
    assert NODE in done.stdout
    assert "the CloudNativePG operator's pods may reach TCP 6443" in done.stdout


@requires_jq
def test_up_stops_before_changing_the_policy_when_the_address_is_unreadable(
    tmp_path: Path,
) -> None:
    done, applied, _ = run_bash(
        tmp_path, operator_parts(), slice_json="", slice_status=1
    )

    assert done.returncode != 0
    assert applied == ""
    assert "the CloudNativePG operator's NetworkPolicy was not changed" in done.stderr


# ── the header says what is true, and what is not proved ─────────────────────


def test_the_header_says_who_calls_the_operators_ports_and_what_was_not_seen() -> None:
    header = header_of(OPERATOR_FILE)

    # Why `make up` applies it and not the chart; why not the chart's.
    assert "kind's platform, not the Meridian chart's" in header
    assert "default-deny" in header and "make deploy" in header
    assert "installed another way" in header  # on AKS
    # Who calls 9443 and 8080, and why no rule admits the API server.
    assert "9443" in header and "8080" in header
    assert "the API server alone" in header
    assert "from the node" in header
    assert "seen on kind 2026-10-07" in header
    assert "not seen separately" in header
    assert "one ipBlock peer" in header
    assert "the node's pod-network address" in header
    # Why 8000 and not 5432.
    assert "8000" in header and "5432" in header
    assert "Instance Status Extraction Error" in header
    # What is claimed and what is not, and the way back.
    assert "not seen on kind" in header
    assert "make down" in header and "make up" in header
    assert "tried, and what failed" in header
    assert "Role" in header and "meridian" in header


def test_the_namespaces_file_labels_meridian_for_the_operators_pod() -> None:
    header = header_of(NAMESPACES_FILE)
    namespaces = {d["metadata"]["name"]: d for d in load_documents(NAMESPACES_FILE)}

    assert namespaces["meridian"]["metadata"]["labels"] == {
        "pod-security.kubernetes.io/warn": "restricted",
        "pod-security.kubernetes.io/audit": "restricted",
    }
    # The operator's pod is read in a render and meets `restricted` there; the
    # API server has not said so.
    assert "operator's pod" in header
    assert "not confirmed by the API server" in header
    assert "never enforce" in header


# ── nothing names the old namespace but history ──────────────────────────────


def scanned_files() -> list[Path]:
    found = []
    for root in SCANNED:
        for path in sorted(root.rglob("*")):
            if (
                path.is_file()
                and path.suffix in TEXT_SUFFIXES
                and "__pycache__" not in path.parts
                and path != Path(__file__).resolve()
            ):
                found.append(path)
    found.append(REPO_ROOT / "Makefile")
    return found


def paragraphs_naming_the_old_namespace(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [block for block in re.split(r"\n\s*\n", text) if OLD_NAMESPACE in block]


def test_nothing_names_the_old_namespace_but_as_history() -> None:
    unmarked, counted = [], {}
    for path in scanned_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        lines = [
            line
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if OLD_NAMESPACE in line
        ]
        if relative in STILL_NAMING:
            counted[relative] = len(lines)
            continue
        for block in paragraphs_naming_the_old_namespace(path):
            if "history" not in block.lower():
                unmarked.append(relative)
    assert unmarked == []
    assert counted == STILL_NAMING


def test_the_readme_says_where_the_operator_runs_and_what_that_costs() -> None:
    kind = " ".join((KIND_DIR / "README.md").read_text("utf-8").split())

    row = "| CloudNativePG operator | `cloudnative-pg` | 0.29.1 (operator 1.30.1) |"
    assert f"{row} `meridian` |" in kind
    assert "cnpg-operator-networkpolicy.yaml" in kind
    assert "`config.clusterWide=false`" in kind
    assert "a Role in `meridian`" in kind
    assert (
        "cluster made before this change needs `make down` and then `make up`" in kind
    )
    assert "tried, and what failed" in kind
    # The Pod Security table has no row for the namespace that is gone. The
    # namespace that was left bare after it has its own policy now (contract N).
    assert f"| `{OLD_NAMESPACE}` |" not in kind
    assert "envoy-gateway-networkpolicy.yaml" in kind
    # The operator no longer reads the CA's private key by a cluster-wide rule.
    assert "CloudNativePG operator reads Secrets in `meridian` only" in kind


def test_the_collector_is_kept_from_the_operator_by_the_operators_own_policy() -> None:
    kind = " ".join((KIND_DIR / "README.md").read_text("utf-8").split())
    observability = header_of(MANIFESTS / "observability-networkpolicy.yaml")

    # The operator's pod is in `meridian` and the collector's rule admits the
    # namespace: kind's policy for the operator, not the chart's, keeps it out.
    assert "the operator's own policy" in observability
    assert "the operator's own policy" in kind
    # One policy in the file, and it has no rule towards the collector's port.
    assert policies_of(OPERATOR_FILE) == {"cnpg-operator": operator_policy()}
    ports = {p["port"] for r in rules(operator_policy(), "egress") for p in r["ports"]}
    assert 4318 not in ports and 4317 not in ports


def test_smoke_says_where_the_operators_log_is_now() -> None:
    smoke = (KIND_DIR / "smoke.sh").read_text(encoding="utf-8")
    # Check 10's verdict on the database's certificates: both messages send the
    # reader to the Deployment in `meridian`, which the scan above cannot ask for.
    where = "in meridian (the Deployment cnpg-cloudnative-pg)"

    assert f"read its log {where}" in smoke
    assert f"read the operator's log {where}" in smoke
    assert OLD_NAMESPACE not in smoke
