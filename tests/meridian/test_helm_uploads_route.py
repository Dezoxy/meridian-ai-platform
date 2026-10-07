"""The edge for the upload route is a second route with limits of its own (S070).

``route.uploads.enabled`` (off by default, apart from ``route.enabled``) adds a
second HTTPRoute for exactly the upload paths, a BackendTrafficPolicy of its own
with a larger request buffer and a local rate limit, and the variable that
turns the Claims API's route on. The first route and its policy, the 64 Ki
buffer for everything else, do not change by one byte, and neither does any
NetworkPolicy: the Claims API already admits the edge's pods on its port, and a
second route to the same pod and port is the same traffic to the policy.

What the render cannot show (a 413 at the edge over the limit, a 429 past the
rate, the first route still at 64 Ki, which of two matching routes Envoy picks)
needs a cluster; ``route.yaml`` says what is expected and why.
"""

import copy
import functools
import re
from pathlib import Path

import yaml
from chartsupport import (
    CHART_DIR,
    NAMESPACE,
    helm_arguments,
    network_policies,
    render,
    rendered_chart,
    run_helm,
)

ENABLED = "route.uploads.enabled"
UPLOADS_ON = ["--set", f"{ENABLED}=true"]
FIRST = "claims-api"
SECOND = "claims-api-uploads"
SWITCH_ENV = "MERIDIAN_CLAIMS_UPLOADS"
# 1 MiB for the file and 76 KiB for the multipart envelope and the `kind` field:
# the app's UPLOAD_BODY_LIMIT_BYTES, which a test in the app's contract holds
# equal to the chart's default (the two are written once in each place).
UPLOAD_LIMIT = str(1024 * 1024 + 76 * 1024)
FILES_PATHS = ("/claims/CLM-0001/files", "/claimant/claims/CLM-0001/files")


@functools.cache
def uploads_on() -> tuple[dict, ...]:
    return tuple(render([*helm_arguments(), *UPLOADS_ON]))


def of_kind(documents: tuple[dict, ...] | list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in documents if d["kind"] == kind}


def claims_api_env(documents: tuple[dict, ...] | list[dict]) -> dict[str, str]:
    deployment = of_kind(documents, "Deployment")[FIRST]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value") for e in container["env"]}


def env_names(workload: dict) -> list[str]:
    spec = workload["spec"]
    if workload["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    template = spec["template"]
    return [e["name"] for c in template["spec"]["containers"] for e in c.get("env", [])]


def refusal(*arguments: str) -> str:
    done = run_helm([*helm_arguments(), *arguments])
    assert done.returncode != 0, "the render should have been refused"
    return done.stderr


def test_uploads_are_off_by_default_and_by_kinds_values() -> None:
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    kind = yaml.safe_load(
        (CHART_DIR.parents[1] / "kind" / "values" / "meridian.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert values["route"]["uploads"]["enabled"] is False
    assert "uploads" not in kind["route"]


def test_with_uploads_off_there_is_one_route_one_policy_and_no_variable() -> None:
    documents = rendered_chart()

    assert list(of_kind(documents, "HTTPRoute")) == [FIRST]
    assert list(of_kind(documents, "BackendTrafficPolicy")) == [FIRST]
    for workload in documents:
        if workload["kind"] in {"Deployment", "Job", "CronJob"}:
            assert SWITCH_ENV not in env_names(workload), workload["metadata"]["name"]


def test_with_uploads_on_there_is_one_more_route_and_one_more_policy() -> None:
    off, on = rendered_chart(), uploads_on()

    assert sorted(of_kind(on, "HTTPRoute")) == sorted([FIRST, SECOND])
    assert sorted(of_kind(on, "BackendTrafficPolicy")) == sorted([FIRST, SECOND])
    assert len(on) == len(off) + 2


def test_the_second_route_serves_the_same_host_gateway_and_backend() -> None:
    routes = of_kind(uploads_on(), "HTTPRoute")
    first, second = routes[FIRST], routes[SECOND]

    assert second["spec"]["parentRefs"] == first["spec"]["parentRefs"]
    assert second["spec"]["hostnames"] == first["spec"]["hostnames"]
    assert second["metadata"]["namespace"] == NAMESPACE
    (rule,) = second["spec"]["rules"]
    assert rule["backendRefs"] == first["spec"]["rules"][0]["backendRefs"]


def test_the_second_route_matches_post_to_the_two_files_paths_and_nothing_else() -> (
    None
):
    (rule,) = of_kind(uploads_on(), "HTTPRoute")[SECOND]["spec"]["rules"]

    matches = rule["matches"]
    assert [m["method"] for m in matches] == ["POST", "POST"]
    assert {m["path"]["type"] for m in matches} == {"RegularExpression"}
    patterns = [re.compile(m["path"]["value"]) for m in matches]
    for path in FILES_PATHS:
        assert any(p.fullmatch(path) for p in patterns), path
    for path in (
        "/",
        "/claims",
        "/claims/CLM-0001",
        "/claims/CLM-0001/documents",
        "/claims/CLM-0001/files/",
        "/claims/CLM-0001/files/x",
        "/claims/CLM-0001/files\n",
        "/claims/CLM-0001/filesx",
        "/claims/CLM-00011/files",
        "/claims/CLM-001/files",
        "/claims/clm-0001/files",
        "/claims/CLM-0001/../files",
        "/claims//files",
        "/x/claims/CLM-0001/files",
        "/claimant/claims/CLM-0001/documents",
        "/claimant/claims/CLM-0001/files/",
        "/claimant/claims",
        "/claimant/claims/CLM-0001",
    ):
        assert not any(p.fullmatch(path) for p in patterns), path


def test_the_render_shape_is_a_first_route_with_no_match_and_a_second_of_regexes() -> (
    None
):
    # This shows the render's SHAPE and nothing about which route serves a
    # request: precedence is a cluster's to show. No `matches` is the Gateway
    # API's default, PathPrefix "/", which every upload path also satisfies.
    # Envoy Gateway is understood to order path matches Exact, then
    # RegularExpression, then PathPrefix, so the regular expressions of the
    # second route should win over the prefix "/" of the first; the kind run
    # reads it (a post to the upload path is counted by the second route's
    # rate limit), and no test here can.
    routes = of_kind(uploads_on(), "HTTPRoute")
    (first,) = routes[FIRST]["spec"]["rules"]
    (second,) = routes[SECOND]["spec"]["rules"]

    assert "matches" not in first
    assert all(m["path"]["type"] == "RegularExpression" for m in second["matches"])


def test_the_first_route_and_its_policy_are_the_same_with_uploads_on_and_off() -> None:
    off, on = rendered_chart(), uploads_on()

    for kind in ("HTTPRoute", "BackendTrafficPolicy"):
        assert of_kind(on, kind)[FIRST] == of_kind(off, kind)[FIRST]
    policy = of_kind(on, "BackendTrafficPolicy")[FIRST]
    assert policy["spec"]["requestBuffer"] == {"limit": "64Ki"}
    assert "rateLimit" not in policy["spec"]


def test_the_second_policy_targets_only_the_second_route() -> None:
    policy = of_kind(uploads_on(), "BackendTrafficPolicy")[SECOND]

    assert policy["spec"]["targetRefs"] == [
        {"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": SECOND}
    ]


def test_the_second_policy_buffers_one_mib_and_the_envelope_by_default() -> None:
    policy = of_kind(uploads_on(), "BackendTrafficPolicy")[SECOND]

    assert policy["spec"]["requestBuffer"] == {"limit": UPLOAD_LIMIT}
    assert UPLOAD_LIMIT == "1126400"


def test_the_second_policy_limits_the_whole_route_to_six_requests_a_minute() -> None:
    # CRD at Envoy Gateway v1.9.2: spec.rateLimit.local.rules[].limit takes
    # `requests` and `unit`; a rule with no clientSelectors applies to all
    # traffic of the targeted route, in one bucket.
    policy = of_kind(uploads_on(), "BackendTrafficPolicy")[SECOND]

    assert policy["spec"]["rateLimit"] == {
        "local": {"rules": [{"limit": {"requests": 6, "unit": "Minute"}}]}
    }


def test_the_limits_are_the_values() -> None:
    documents = render(
        [
            *helm_arguments(),
            *UPLOADS_ON,
            "--set-string",
            "route.uploads.requestBufferLimit=2Mi",
            "--set",
            "route.uploads.rateLimit.requests=3",
            "--set-string",
            "route.uploads.rateLimit.unit=Hour",
        ]
    )

    spec = of_kind(documents, "BackendTrafficPolicy")[SECOND]["spec"]
    assert spec["requestBuffer"] == {"limit": "2Mi"}
    assert spec["rateLimit"]["local"]["rules"] == [
        {"limit": {"requests": 3, "unit": "Hour"}}
    ]
    # The first route's policy does not follow them.
    assert (
        of_kind(documents, "BackendTrafficPolicy")[FIRST]
        == of_kind(rendered_chart(), "BackendTrafficPolicy")[FIRST]
    )


def test_the_switch_reaches_the_claims_api_and_only_it() -> None:
    on = uploads_on()

    assert claims_api_env(on)[SWITCH_ENV] == "on"
    assert env_names(of_kind(on, "Deployment")[FIRST]).count(SWITCH_ENV) == 1
    for workload in on:
        if workload["kind"] in {"Deployment", "Job", "CronJob"} and (
            workload["metadata"]["name"] != FIRST
        ):
            assert SWITCH_ENV not in env_names(workload), workload["metadata"]["name"]


def test_uploads_change_nothing_else_in_the_render() -> None:
    off, on = rendered_chart(), uploads_on()

    def unchanged(documents: tuple[dict, ...]) -> list[dict]:
        # A copy: the cached documents are shared and must not be changed.
        kept = [copy.deepcopy(d) for d in documents if d["metadata"]["name"] != SECOND]
        for document in kept:
            if document["kind"] == "Deployment" and document["metadata"]["name"] == (
                FIRST
            ):
                (container,) = document["spec"]["template"]["spec"]["containers"]
                container["env"] = [
                    e for e in container["env"] if e["name"] != SWITCH_ENV
                ]
        return kept

    assert unchanged(on) == list(off)


def test_the_network_policies_are_identical_with_uploads_on_and_off() -> None:
    # The Claims API's ingress from the edge names the edge's pods and the
    # chart's port, not a route: a second route to the same pod and port is the
    # same traffic to the policy. The policy is gated on route.enabled only.
    off, on = network_policies(rendered_chart()), network_policies(uploads_on())

    assert on == off
    assert FIRST in on


def test_uploads_on_with_the_route_off_is_refused_with_a_sentence() -> None:
    stderr = refusal("--set", "route.enabled=false", *UPLOADS_ON)

    assert "route.uploads.enabled is true, but route.enabled is false" in stderr
    assert "turn the route on" in stderr


def test_the_route_off_without_uploads_still_renders() -> None:
    documents = render([*helm_arguments(), "--set", "route.enabled=false"])

    assert not {"HTTPRoute", "BackendTrafficPolicy"} & {d["kind"] for d in documents}


def test_a_values_env_item_cannot_set_the_switch(tmp_path: Path) -> None:
    values = tmp_path / "values.yaml"
    values.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "claims-api": {
                        "env": [
                            {
                                "name": "MERIDIAN_RUNTIME_URL",
                                "serviceUrl": "agent-runtime",
                            },
                            {"name": SWITCH_ENV, "value": "on"},
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    stderr = refusal("-f", str(values))

    assert SWITCH_ENV in stderr
    assert "route.uploads.enabled" in stderr
