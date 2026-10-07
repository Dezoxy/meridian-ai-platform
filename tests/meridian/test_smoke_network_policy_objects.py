"""Check 8 reads every service's NetworkPolicy object (S073 K4, B14's second half).

``check_network_policy`` in ``infra/kind/smoke.sh`` looked for the Claims API's
Deployment alone before it probed, and for the policy ``default-deny``. It now
lists the Meridian Deployments as checks 3, 5 and 7 do (``deployed_services``,
by the label) and one FAIL line names every service whose NetworkPolicy object
is not there. The probes are as they were: no probe Pod per service. The harness
is ``run_network_policy_check`` of ``test_smoke_network_policy.py``.
"""

from pathlib import Path

from chartsupport import RATE_STORE, SERVICES, network_policies, rendered_chart
from kindsupport import requires_jq
from test_smoke_network_policy import (
    DEPLOYED,
    POLICY,
    execs,
    run_network_policy_check,
    verbs,
)

pytestmark = requires_jq

SEVEN = (*SERVICES, RATE_STORE)
ALL_DEPLOYED = "\n".join(f"deployment.apps/{name}" for name in SEVEN)
DEFAULT_DENY = POLICY.rsplit("/", 1)[0]
LINE = "FAIL  network policy: no networkpolicy object for"


def policies(*names: str) -> str:
    return "\n".join([POLICY, *(f"{DEFAULT_DENY}/{name}" for name in names)])


def lines_of(tmp_path: Path, **options: object) -> tuple[list[str], str]:
    return run_network_policy_check(tmp_path, **options)  # type: ignore[arg-type]


def test_every_service_with_its_policy_prints_what_it_printed_before(
    tmp_path: Path,
) -> None:
    lines, _ = lines_of(tmp_path, deployed=ALL_DEPLOYED, policy_list=policies(*SEVEN))

    assert [line.split()[0] for line in lines] == ["PASS"] * 4
    assert not any(LINE in line for line in lines)
    # The probes were read, not skipped: each line names its own target.
    targets = (
        "agent-runtime.meridian.svc:8000",
        "model-gateway.meridian.svc:8000",
        "kubernetes.default.svc:443",
        "platform-db-rw.meridian.svc:5432",
    )
    for line, target in zip(lines, targets, strict=True):
        assert line.startswith("PASS  network policy: ")
        assert f"({target})" in line


def test_a_service_without_its_policy_object_is_named_and_only_it(
    tmp_path: Path,
) -> None:
    others = [name for name in SEVEN if name != "model-gateway"]

    lines, _ = lines_of(tmp_path, deployed=ALL_DEPLOYED, policy_list=policies(*others))

    (line,) = [line for line in lines if line.startswith(LINE)]
    assert line.startswith(f"{LINE} model-gateway ")
    assert "claims-api" not in line and "rate-store" not in line
    assert "make deploy" in line
    # The probes still ran, and the other lines are still PASS lines.
    assert [line.split()[0] for line in lines].count("PASS") == 4


def test_every_service_that_lacks_its_policy_is_named_in_the_one_line(
    tmp_path: Path,
) -> None:
    present = [name for name in SEVEN if name not in ("policy-mcp", RATE_STORE)]

    lines, _ = lines_of(tmp_path, deployed=ALL_DEPLOYED, policy_list=policies(*present))

    (line,) = [line for line in lines if line.startswith(LINE)]
    assert line.startswith(f"{LINE} policy-mcp, rate-store ")


def test_a_policy_of_a_service_that_is_not_deployed_is_not_named(
    tmp_path: Path,
) -> None:
    lines, _ = lines_of(
        tmp_path,
        deployed=DEPLOYED,
        policy_list=policies("claims-api", "model-gateway"),
    )

    assert not any(LINE in line for line in lines)


def test_the_probes_and_the_pods_are_the_same_with_seven_services_as_with_one(
    tmp_path: Path,
) -> None:
    one = tmp_path / "one"
    seven = tmp_path / "seven"
    one.mkdir()
    seven.mkdir()

    _, asked_one = lines_of(one)
    _, asked_seven = lines_of(
        seven, deployed=ALL_DEPLOYED, policy_list=policies(*SEVEN)
    )

    assert execs(asked_seven) == execs(asked_one)
    assert verbs(asked_seven).count("create") == verbs(asked_one).count("create")
    assert verbs(asked_seven).count("create") == 1  # one probe Pod, not one each


def test_the_policies_are_listed_once_not_once_a_service(tmp_path: Path) -> None:
    _, asked = lines_of(tmp_path, deployed=ALL_DEPLOYED, policy_list=policies(*SEVEN))

    listings = [call for call in asked.splitlines() if "get networkpolicy" in call]
    assert len([c for c in listings if "-o name" in c and "default-deny" not in c]) == 1


def test_a_listing_of_the_policies_that_fails_is_a_fail_that_says_so(
    tmp_path: Path,
) -> None:
    lines, _ = lines_of(tmp_path, deployed=ALL_DEPLOYED, policy_list="FAIL")

    (line,) = [line for line in lines if "could not list the NetworkPolicies" in line]
    assert line.startswith(
        "FAIL  network policy: could not list the NetworkPolicies in meridian"
    )
    assert not any(LINE in line for line in lines)


def test_services_without_the_claims_api_cannot_be_probed_and_say_so(
    tmp_path: Path,
) -> None:
    lines, asked = lines_of(
        tmp_path,
        deployed="deployment.apps/model-gateway",
        policy_list=policies("model-gateway"),
    )

    assert lines == [
        "FAIL  network policy: deployment/claims-api is not deployed, and the probes "
        "run in its pod"
    ]
    assert execs(asked) == []


def test_each_labelled_deployment_of_the_chart_has_a_policy_of_its_name() -> None:
    documents = list(rendered_chart())
    deployments = sorted(
        d["metadata"]["name"]
        for d in documents
        if d["kind"] == "Deployment"
        and d["metadata"]["labels"].get("app.kubernetes.io/part-of") == "meridian"
    )

    assert deployments == sorted(SEVEN)
    assert set(deployments) <= set(network_policies(documents))
