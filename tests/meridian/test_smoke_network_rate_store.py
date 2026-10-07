"""The sixth line of smoke's network policy check: the rate store (S066, T-45).

``check_network_rate_store`` in ``infra/kind/smoke.sh`` proves the store's INGRESS
rule, and not the sender's egress rule. The first version opened the connection
from the Claims API's pod, which has no egress rule to the store, so its packets
were dropped at the sender whatever the store's ingress said: the line passed with
the store's policy deleted. Now the probe pod of the database's check (the
sweep's name label, and smoke's own) gets an egress rule to the store from a
kind-owned policy (``manifests/smoke-rate-store-networkpolicy.yaml``, which `make
up` applies), so the only rule left between it and the store is the store's
ingress, which admits the Model Gateway's pods alone: the connection must time
out. The control, as the database's check does it: the same pod, given the
gateway's name label, must reach the port (a TCP connection that is then reset or
answered is "reached": the probe holds no certificate and no password, so it can
do nothing there). It is one PASS line when both hold, so the count of lines does
not move (``test_smoke_line_count.py``).

The harness is that of the network policy check (its probe definitions); the stub
``kctl`` is this file's, because the check starts a pod and relabels it.
"""

import copy
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import test_smoke_network_policy as network
import yaml
from chartsupport import (
    NAMESPACE,
    RATE_STORE,
    allowed_services,
    network_policies,
    rendered_chart,
)
from kindsupport import (
    KIND_DIR,
    SMOKE_SH,
    function_body,
    function_definition,
    one_line_function,
    requires_jq,
)

pytestmark = requires_jq

TARGET = f"{RATE_STORE}.{NAMESPACE}.svc:6379"
HOST, PORT = TARGET.split(":")
POLICY_NAME = "meridian-smoke-rate-store"
POLICY_FILE = KIND_DIR / "manifests" / "smoke-rate-store-networkpolicy.yaml"
GATEWAY_LABEL = "app.kubernetes.io/name=model-gateway"
SWEEP_LABEL = "app.kubernetes.io/name=meridian-sweep"
LABEL_ATTEMPTS = network.LABEL_ATTEMPTS
FUNCTIONS = (
    "network_probe",
    "network_pod_spec",
    "network_start_pod",
    "network_delete_pod",
    "network_rate_store_lines",
    "check_network_rate_store",
)
PASS = (
    "PASS  network policy: a pod that is not the Model Gateway's cannot reach the "
    f"rate store ({TARGET}) though its egress is open to it, which only the store's "
    "ingress rule can cause, and with the Model Gateway's name label the same pod can"
)
STUB = r"""
kctl() {
  printf '%s\n' "${*//$'\n'/ }" >>"${ASKED}"
  case "$*" in
    *" get networkpolicy "*)
      [[ "${POLICY_LOOKUP}" != FAIL ]] || { echo "Error" >&2; return 1; }
      printf "%s" "${POLICY_LOOKUP}" ;;
    *"get deployment claims-api -o json"*) cat "${STATE}/deployment.json" ;;
    *" exec "*)
      if [[ -e "${STATE}/labelled" ]]; then
        n=$(( $(<"${STATE}/after-count") + 1 )); echo "${n}" >"${STATE}/after-count"
        file="${STATE}/answer-after-${n}"
        [[ -e "${file}" ]] || file="${STATE}/answer-after-last"
        key=after
      else
        file="${STATE}/answer-before"
        key=before
      fi
      [[ ! -e "${STATE}/error-${key}" ]] || cat "${STATE}/error-${key}" >&2
      cat "${file}"
      [[ ! -e "${STATE}/status-${key}" ]] || return "$(<"${STATE}/status-${key}")" ;;
    *" create "*)
      cat >"${STATE}/created.json"
      [[ "${CREATE_STATUS}" == 0 ]] || { echo "Error: create" >&2; return 1; } ;;
    *" wait "*)
      [[ "${WAIT_STATUS}" == 0 ]] || { echo "error: timed out" >&2; return 1; } ;;
    *" label "*)
      [[ "${LABEL_STATUS}" == 0 ]] || { echo "Error: label" >&2; return 1; }
      case "$*" in *"model-gateway"*) touch "${STATE}/labelled" ;; esac ;;
    *" delete "*) ;;
  esac
}
"""


def run_rate_store_check(
    tmp_path: Path,
    *,
    policy: str | None = None,
    before: str = "blocked\n",
    after: tuple[str, ...] = ("reached\n",),
    failing: dict[str, tuple[int, str]] | None = None,
    create_status: int = 0,
    wait_status: int = 0,
    label_status: int = 0,
) -> tuple[list[str], str]:
    """``check_network_rate_store`` of smoke.sh in bash against a stub ``kctl``.
    ``policy`` is what the lookup of kind's own policy prints (its JSON, which is
    kind's manifest as the API server would return it unless given; empty:
    absent; ``FAIL``: the lookup fails). ``before`` is what the probe pod prints
    before it is given the gateway's label and ``after`` what it prints on each
    try after (the last repeats). ``failing`` maps ``before`` or ``after`` to the
    exit status and stderr of the probe. Returns the output lines and what ``kctl``
    was asked, one call per line."""
    if policy is None:
        policy = json.dumps(kind_policy())
    state = tmp_path / "state"
    state.mkdir()
    asked = tmp_path / "kctl-calls"
    asked.touch()
    (state / "answer-before").write_text(before)
    for number, text in enumerate(after, start=1):
        (state / f"answer-after-{number}").write_text(text)
    (state / "answer-after-last").write_text(after[-1])
    (state / "after-count").write_text("0")
    for key, (status, error) in (failing or {}).items():
        (state / f"status-{key}").write_text(str(status))
        (state / f"error-{key}").write_text(error)
    (state / "deployment.json").write_text(json.dumps(network.DEPLOYMENT))
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            "sleep() { :; }",
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
            "STATE": str(state),
            "POLICY_LOOKUP": policy,
            "CREATE_STATUS": str(create_status),
            "WAIT_STATUS": str(wait_status),
            "LABEL_STATUS": str(label_status),
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text()


def verbs(asked: str) -> list[str]:
    return network.verbs(asked)


def test_a_store_only_the_gateways_pods_reach_is_one_pass_line(tmp_path: Path) -> None:
    lines, _ = run_rate_store_check(tmp_path)

    assert lines == [PASS]


def test_the_probe_pod_times_out_first_then_reaches_with_the_gateways_label(
    tmp_path: Path,
) -> None:
    _, asked = run_rate_store_check(tmp_path)

    calls = asked.splitlines()
    name = json.loads((tmp_path / "state" / "created.json").read_text())["metadata"][
        "name"
    ]
    labels = [c for c in calls if " label " in c]
    execs = [c for c in calls if " exec " in c]
    assert len(execs) == 2
    for call in execs:
        # In the probe pod, never in the Claims API's: its egress would block the
        # packets whatever the store's ingress says.
        assert call.startswith(f"-n meridian exec {name} -- python -c ")
        assert call.endswith(f" {HOST} {PORT}")
    assert labels == [
        f"-n meridian label pod {name} {GATEWAY_LABEL} --overwrite",
        f"-n meridian label pod {name} {SWEEP_LABEL} --overwrite",
    ]
    # The order: probe, the gateway's label, probe, the sweep's label back.
    kinds = [
        "probe" if " exec " in c else "label" if " label " in c else "other"
        for c in calls
    ]
    assert [k for k in kinds if k != "other"] == ["probe", "label", "probe", "label"]


def test_the_pod_is_the_sweeps_and_is_labelled_back_before_it_is_deleted(
    tmp_path: Path,
) -> None:
    _, asked = run_rate_store_check(tmp_path)

    calls = asked.splitlines()
    pod = json.loads((tmp_path / "state" / "created.json").read_text())
    # It starts as the sweep's pod and smoke's own, which is what the kind policy
    # selects by; the gateway's name is on it only while the control runs, and the
    # model-gateway Service selects by that name alone, so the pod (no readiness
    # probe) would take a gateway's calls for as long as it carries it.
    assert pod["metadata"]["labels"] == {
        "app.kubernetes.io/name": "meridian-sweep",
        "meridian-smoke": "network-probe",
    }
    last_label = max(i for i, c in enumerate(calls) if " label " in c)
    assert SWEEP_LABEL in calls[last_label]
    assert last_label < max(i for i, c in enumerate(calls) if " delete " in c)
    assert calls[-1].startswith("-n meridian delete pod smoke-network-")


def test_a_pod_that_reaches_the_store_without_the_label_fails_and_is_never_labelled(
    tmp_path: Path,
) -> None:
    lines, asked = run_rate_store_check(tmp_path, before="reached\n")

    (line,) = lines
    assert line.startswith(
        f"FAIL  network policy: a pod that is not the Model Gateway's reached the "
        f"rate store ({TARGET})"
    )
    # The three ways: too wide, missing, not enforced; and what it would mean.
    assert "admits more than the Model Gateway's pods" in line
    assert "or is missing" in line
    assert "does not enforce" in line
    assert "window" in line
    assert "label" not in verbs(asked)


def test_a_control_that_does_not_reach_fails_and_says_the_line_proves_nothing(
    tmp_path: Path,
) -> None:
    lines, asked = run_rate_store_check(tmp_path, after=("blocked\n",))

    (line,) = lines
    assert line.startswith(
        f"FAIL  network policy: the same pod still cannot reach the rate store "
        f"({TARGET}) after the label {GATEWAY_LABEL} was added, in "
        f"{LABEL_ATTEMPTS} tries"
    )
    assert "proves nothing" in line
    assert "PASS" not in "".join(lines)
    probes = [c for c in asked.splitlines() if " exec " in c]
    assert len(probes) == 1 + LABEL_ATTEMPTS


def test_the_label_may_take_a_moment_to_reach_the_network_plugin(
    tmp_path: Path,
) -> None:
    assert LABEL_ATTEMPTS >= 3

    lines, asked = run_rate_store_check(
        tmp_path, after=("blocked\n", "blocked\n", "reached\n")
    )

    assert lines == [PASS]
    assert len([c for c in asked.splitlines() if " exec " in c]) == 1 + 3


@pytest.mark.parametrize(
    ("failing", "after"),
    [
        (
            {"before": (1, "Traceback: socket.gaierror: Name or service not known")},
            None,
        ),
        ({"before": (1, "error: unable to upgrade connection")}, None),
        ({"after": (1, "Traceback: ConnectionRefusedError")}, ("",)),
        (None, ("maybe\n",)),
    ],
    ids=["no-dns", "exec-failed", "refused-after-label", "other-word-after-label"],
)
def test_any_answer_but_reached_or_blocked_fails_with_the_probes_words(
    tmp_path: Path, failing: dict | None, after: tuple[str, ...] | None
) -> None:
    lines, _ = run_rate_store_check(
        tmp_path, failing=failing, after=after or ("reached\n",)
    )

    (line,) = lines
    assert line.startswith("FAIL  network policy: the probe in smoke-network-")
    assert f"to {TARGET} gave no answer of reached or blocked" in line


def test_an_escape_in_the_probes_answer_never_reaches_the_terminal(
    tmp_path: Path,
) -> None:
    lines, _ = run_rate_store_check(
        tmp_path,
        before="\x1b[31mblocked\nreached\n",
        failing={"before": (0, "\x1b]0;title\x07")},
    )

    assert all("\x1b" not in line and "\x07" not in line for line in lines)


@pytest.mark.parametrize(
    ("options", "pattern"),
    [
        ({"create_status": 1}, r"FAIL  network policy: could not start the probe pod"),
        (
            {"wait_status": 1},
            r"FAIL  network policy: the probe pod smoke-network-\d+ did not become "
            r"Ready within \d+s",
        ),
        ({"label_status": 1}, r"FAIL  network policy: could not give the probe pod"),
    ],
    ids=["create", "wait", "label"],
)
def test_a_pod_that_cannot_be_used_is_one_failed_line_and_is_deleted(
    tmp_path: Path, options: dict, pattern: str
) -> None:
    lines, asked = run_rate_store_check(tmp_path, **options)

    (line,) = lines
    assert re.match(pattern, line), line
    assert asked.splitlines()[-1].startswith("-n meridian delete pod smoke-network-")


def test_a_missing_kind_policy_fails_before_any_pod_starts_and_says_make_up(
    tmp_path: Path,
) -> None:
    lines, asked = run_rate_store_check(tmp_path, policy="")

    (line,) = lines
    assert line.startswith(f"FAIL  network policy: networkpolicy/{POLICY_NAME}")
    # Without it the probe pod has no egress to the store, a timeout would be the
    # sender's, and the control (the gateway's own egress rule) would still reach:
    # the line would pass with the store wide open.
    assert "no egress to the rate store" in line
    assert "make up" in line
    assert "smoke-rate-store-networkpolicy.yaml" in line
    assert verbs(asked) == ["get"]


def test_a_policy_lookup_that_fails_is_a_failed_line_and_no_pod_starts(
    tmp_path: Path,
) -> None:
    lines, asked = run_rate_store_check(tmp_path, policy="FAIL")

    (line,) = lines
    assert line.startswith("FAIL  network policy: could not look for")
    assert verbs(asked) == ["get"]


def mutated(change) -> str:
    """Kind's own policy, as the API server returns it, after ``change``."""
    policy = copy.deepcopy(kind_policy())
    change(policy)
    return json.dumps(policy)


def to_peer(policy: dict) -> dict:
    return policy["spec"]["egress"][0]["to"][0]


WRONG_SHAPES = {
    "the-wrong-port": lambda p: p["spec"]["egress"][0].update(
        ports=[{"port": 6380, "protocol": "TCP"}]
    ),
    "the-wrong-protocol": lambda p: p["spec"]["egress"][0].update(
        ports=[{"port": int(PORT), "protocol": "UDP"}]
    ),
    "other-ports-only": lambda p: p["spec"]["egress"][0].update(
        ports=[{"port": 5432, "protocol": "TCP"}, {"port": 8000, "protocol": "TCP"}]
    ),
    "another-peer": lambda p: to_peer(p)["podSelector"]["matchLabels"].update(
        {"app.kubernetes.io/name": "model-gateway"}
    ),
    "no-peer-label": lambda p: to_peer(p).update(podSelector={}),
    "a-peer-in-another-namespace": lambda p: to_peer(p).update(
        namespaceSelector={"matchLabels": {"kubernetes.io/metadata.name": "default"}}
    ),
    "an-address-block-not-the-pods": lambda p: p["spec"]["egress"][0].update(
        to=[{"ipBlock": {"cidr": "10.0.0.0/8"}}]
    ),
    "no-peer-at-all": lambda p: p["spec"]["egress"][0].update(to=[]),
    "no-egress-rule": lambda p: p["spec"].update(egress=[]),
    "egress-rules-absent": lambda p: p["spec"].pop("egress"),
    "egress-not-a-policy-type": lambda p: p["spec"].update(policyTypes=["Ingress"]),
    "no-policy-types": lambda p: p["spec"].pop("policyTypes"),
    "another-pod-selected": lambda p: p["spec"].update(
        podSelector={"matchLabels": {"app.kubernetes.io/name": "meridian-sweep"}}
    ),
    "no-pod-selected-by-label": lambda p: p["spec"].update(podSelector={}),
}


@pytest.mark.parametrize("shape", list(WRONG_SHAPES))
def test_a_kind_policy_of_the_wrong_shape_fails_the_line_before_any_pod_starts(
    tmp_path: Path, shape: str
) -> None:
    lines, asked = run_rate_store_check(tmp_path, policy=mutated(WRONG_SHAPES[shape]))

    # Without egress to the store's pods on TCP 6379 the first attempt would time
    # out at the sender whatever the store's ingress says, the control would reach
    # through the gateway's own egress rule, and the line would PASS with the
    # store's ingress unproven: so it fails, with a sentence of its own.
    (line,) = lines
    assert line.startswith(f"FAIL  network policy: networkpolicy/{POLICY_NAME} exists")
    assert "does not give the probe pod" in line
    assert "meridian-smoke=network-probe" in line
    assert "app.kubernetes.io/name=rate-store" in line
    assert f"TCP {PORT}" in line
    assert "unproven" in line
    assert "smoke-rate-store-networkpolicy.yaml" in line
    assert verbs(asked) == ["get"]


def test_a_lookup_that_returns_something_that_is_not_json_fails_the_line(
    tmp_path: Path,
) -> None:
    lines, asked = run_rate_store_check(tmp_path, policy="not json at all")

    (line,) = lines
    assert line.startswith(f"FAIL  network policy: networkpolicy/{POLICY_NAME} exists")
    assert verbs(asked) == ["get"]


@pytest.mark.parametrize(
    "variant",
    {
        "the-manifest-as-written": lambda p: None,
        "the-api-server-defaults-the-protocol-away": lambda p: p["spec"]["egress"][
            0
        ].update(ports=[{"port": int(PORT)}]),
        "no-ports-which-is-every-port": lambda p: p["spec"]["egress"][0].pop("ports"),
        "another-rule-beside-it": lambda p: p["spec"]["egress"].insert(
            0,
            {
                "to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}],
                "ports": [{"port": 53, "protocol": "UDP"}],
            },
        ),
        "the-port-beside-another": lambda p: p["spec"]["egress"][0].update(
            ports=[
                {"port": 53, "protocol": "UDP"},
                {"port": int(PORT), "protocol": "TCP"},
            ]
        ),
        "metadata-the-server-adds": lambda p: p["metadata"].update(
            uid="0", resourceVersion="1", creationTimestamp="2026-10-06T00:00:00Z"
        ),
    }.items(),
    ids=lambda item: item[0],
)
def test_the_shapes_the_line_needs_pass_it(tmp_path: Path, variant) -> None:
    _, change = variant

    lines, _ = run_rate_store_check(tmp_path, policy=mutated(change))

    assert lines == [PASS]


def test_the_lookup_reads_the_policys_json_and_not_only_its_name(
    tmp_path: Path,
) -> None:
    _, asked = run_rate_store_check(tmp_path)

    (lookup,) = [c for c in asked.splitlines() if " get networkpolicy " in c]
    assert lookup == (
        f"-n meridian get networkpolicy {POLICY_NAME} -o json --ignore-not-found"
    )


def test_the_check_changes_nothing_but_its_own_pod(tmp_path: Path) -> None:
    source = " ".join(function_body(SMOKE_SH, name) for name in FUNCTIONS)
    _, asked = run_rate_store_check(tmp_path)

    assert not re.search(r"kctl[^\n]*\b(apply|patch|replace)\b", source)
    assert set(verbs(asked)) == {"get", "create", "wait", "exec", "label", "delete"}
    name = json.loads((tmp_path / "state" / "created.json").read_text())["metadata"][
        "name"
    ]
    for call in asked.splitlines():
        if call.split()[2] in {"wait", "label", "delete"}:
            assert name in call.replace("pod/", "").split(), call


def test_the_leftover_sweep_and_the_exit_trap_find_the_pod_by_the_variable() -> None:
    body = function_body(SMOKE_SH, "check_network_rate_store")

    # The pod is `${network_pod}`, which the EXIT trap and the next run's sweep
    # already delete, so an interrupted run leaves nothing new behind.
    assert "network_start_pod" in body
    assert "network_delete_pod" in body


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
    # And the only policy of the CHART with an egress rule to it is the gateway's:
    # the probe pod's egress is kind's own (below).
    assert [
        name
        for name, policy in policies.items()
        if RATE_STORE in allowed_services(policy, "egress")
    ] == ["model-gateway"]


def kind_policy() -> dict:
    return yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8"))


def test_kinds_own_policy_opens_the_probe_pods_egress_to_the_store_alone() -> None:
    policy = kind_policy()

    assert policy["kind"] == "NetworkPolicy"
    assert policy["metadata"] == {"name": POLICY_NAME, "namespace": "meridian"}
    # The probe pods, by smoke's own label (the one no policy of the chart selects),
    # and not by the sweep's name label: that would reach the sweep's pods.
    assert policy["spec"]["podSelector"] == {
        "matchLabels": {"meridian-smoke": "network-probe"}
    }
    # Egress only: it grants nothing in, and it changes no ingress rule.
    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert "ingress" not in policy["spec"]
    # To the store's pods, on its port, TCP: no namespace selector (the store is
    # in this namespace), no other peer, no other port, no "any".
    assert policy["spec"]["egress"] == [
        {
            "to": [
                {
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/name": "rate-store"}
                    }
                }
            ],
            "ports": [{"port": int(PORT), "protocol": "TCP"}],
        }
    ]


def test_the_policys_name_is_the_one_the_check_looks_for() -> None:
    (name,) = re.findall(r"^readonly NETWORK_RATE_STORE_POLICY=(\S+)$", SMOKE_SH, re.M)

    assert name == POLICY_NAME == kind_policy()["metadata"]["name"]


def test_make_up_applies_the_policy_with_the_others_before_the_releases() -> None:
    lines = (KIND_DIR / "up.sh").read_text(encoding="utf-8").splitlines()
    (applied,) = [
        i
        for i, line in enumerate(lines)
        if "smoke-rate-store-networkpolicy.yaml" in line
    ]
    (telemetrygen,) = [
        i
        for i, line in enumerate(lines)
        if "manifests/smoke-networkpolicy.yaml" in line
    ]
    first_release = min(
        i for i, line in enumerate(lines) if line.startswith("install_release ")
    )

    assert lines[applied].startswith("kctl apply --server-side --force-conflicts -f ")
    assert lines[applied - 1].startswith("log ")
    assert telemetrygen < applied < first_release


def selecting(policies: dict[str, dict], labels: dict[str, str]) -> list[str]:
    """The chart's policies whose pod selector the labels satisfy."""
    return sorted(
        name
        for name, policy in policies.items()
        if policy["spec"]["podSelector"].get("matchLabels", {}).items()
        <= labels.items()
    )


def test_the_chart_gives_the_probe_pod_no_path_to_the_store_and_kind_gives_egress() -> (
    None
):
    policies = network_policies(list(rendered_chart()))
    probe = {
        "app.kubernetes.io/name": "meridian-sweep",
        "meridian-smoke": "network-probe",
    }

    # The policies of the chart that select the probe pod are default-deny and the
    # sweep's, and neither lets it send to the store: its egress to the store is
    # kind's policy alone, so what stops the packets now is the store's ingress.
    chosen = selecting(policies, probe)
    assert chosen == ["default-deny", "meridian-sweep"]
    for name in chosen:
        assert RATE_STORE not in allowed_services(policies[name], "egress"), name
    selector = kind_policy()["spec"]["podSelector"]["matchLabels"]
    assert selector.items() <= probe.items()
    # And the store's ingress, which admits the gateway's name label alone, does
    # not admit it.
    assert allowed_services(policies[RATE_STORE], "ingress") == {"model-gateway"}
    assert probe["app.kubernetes.io/name"] != "model-gateway"


def test_with_the_gateways_label_the_chart_lets_the_probe_pod_reach_the_store() -> None:
    policies = network_policies(list(rendered_chart()))
    labelled = {
        "app.kubernetes.io/name": "model-gateway",
        "meridian-smoke": "network-probe",
    }

    # The gateway's own egress rule to the store and the store's ingress rule
    # (and kind's egress rule besides): the control reaches, by the same rules the
    # gateway's pods use.
    assert "model-gateway" in selecting(policies, labelled)
    assert RATE_STORE in allowed_services(policies["model-gateway"], "egress")
    assert "model-gateway" in allowed_services(policies[RATE_STORE], "ingress")


# ── the header, and where the line sits ──────────────────────────────────────


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


def test_the_header_says_the_store_line_proves_the_stores_ingress_and_how() -> None:
    flat = flat_header_of_check_eight()
    store = flat.split("the rate store (S066)")[1]

    assert "rate-store.meridian.svc:6379" in store
    assert "manifests/smoke-rate-store-networkpolicy.yaml" in store
    assert "egress" in store and "ingress" in store
    # The policy is read for its shape and not only for its existence.
    assert "a kind policy of the wrong shape" in store
    # The control and its fail branch.
    assert "app.kubernetes.io/name=model-gateway" in store
    assert "proves nothing" in store
    # What the label does while it is on, and the short window.
    assert "model-gateway Service" in store
    assert "labelled back" in store
    # A timeout alone also says a store that hangs, and where that is read.
    assert "a name that does not resolve" in store
    assert "check 5" in store
    # The old claim, that the line cannot tell the two, is gone.
    assert "cannot isolate" not in flat
    assert "no pod of the chart but the gateway's has an egress rule" not in flat


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
