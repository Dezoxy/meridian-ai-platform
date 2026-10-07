"""The edge for the adjuster's download, and the guard that keeps both file
switches local (S070 F4b, T-38).

``route.downloads.enabled`` (off by default; it needs ``route.uploads.enabled``)
adds a second, named rule to the uploads route for GET and HEAD of the download
path, a BackendTrafficPolicy of its own for that rule alone with a local rate
limit, and the variable that turns the Claims API's route on. Whatever turns a
file switch on needs a route host name that ends in ``.localhost`` (a name that
resolves to the machine itself), because until sign-in (S021) the routes take
and serve files for anyone who reaches them.

What the render cannot show (which of two matching routes Envoy picks, that the
policy of one rule takes effect over the route's own, the 429 past the limit)
needs a cluster; ``route.yaml`` says what is expected and why.
"""

import copy
import functools
import re
from pathlib import Path

import yaml
from chartsupport import (
    CHART_DIR,
    helm_arguments,
    network_policies,
    render,
    rendered_chart,
    run_helm,
)

from meridian.workloads.claims_triage.file_download import FILE_ID_PATTERN

UPLOADS_ON = ["--set", "route.uploads.enabled=true"]
DOWNLOADS_ON = ["--set", "route.downloads.enabled=true"]
BOTH_ON = [*UPLOADS_ON, *DOWNLOADS_ON]
FIRST = "claims-api"
UPLOADS = "claims-api-uploads"
DOWNLOADS = "claims-api-downloads"
RULE = "downloads"
SWITCH_ENV = "MERIDIAN_CLAIMS_DOWNLOADS"
UPLOADS_ENV = "MERIDIAN_CLAIMS_UPLOADS"
S021 = "S021"
GUARD = "does not end in .localhost"
CLAIM_PATH = "/adjuster/claims/CLM-0001/files/"
FILE = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def host(name: str) -> list[str]:
    return ["--set-string", f"route.hostname={name}"]


@functools.cache
def uploads_only() -> tuple[dict, ...]:
    return tuple(render([*helm_arguments(), *UPLOADS_ON]))


@functools.cache
def downloads_on() -> tuple[dict, ...]:
    return tuple(render([*helm_arguments(), *BOTH_ON]))


def of_kind(documents: tuple[dict, ...] | list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in documents if d["kind"] == kind}


def claims_api_env(documents: tuple[dict, ...] | list[dict]) -> list[str]:
    deployment = of_kind(documents, "Deployment")[FIRST]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    return [e["name"] for e in container["env"]]


def refusal(*arguments: str) -> str:
    done = run_helm([*helm_arguments(), *arguments])
    assert done.returncode != 0, "the render should have been refused"
    return done.stderr


def download_rule() -> dict:
    rules = of_kind(downloads_on(), "HTTPRoute")[UPLOADS]["spec"]["rules"]
    (rule,) = [r for r in rules if r.get("name") == RULE]
    return rule


# ── off by default, and nothing changes while it is off ─────────────────────
def test_downloads_are_off_by_default_and_by_kinds_values() -> None:
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    kind = yaml.safe_load(
        (CHART_DIR.parents[1] / "kind" / "values" / "meridian.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert values["route"]["downloads"]["enabled"] is False
    assert "downloads" not in kind["route"]
    assert "uploads" not in kind["route"]


def test_with_downloads_off_nothing_of_the_download_is_rendered() -> None:
    for documents in (rendered_chart(), uploads_only()):
        assert DOWNLOADS not in of_kind(documents, "BackendTrafficPolicy")
        for workload in documents:
            if workload["kind"] in {"Deployment", "Job", "CronJob"}:
                spec = workload["spec"]
                if workload["kind"] == "CronJob":
                    spec = spec["jobTemplate"]["spec"]
                names = [
                    e["name"]
                    for c in spec["template"]["spec"]["containers"]
                    for e in c.get("env", [])
                ]
                assert SWITCH_ENV not in names, workload["metadata"]["name"]
    (rule,) = of_kind(uploads_only(), "HTTPRoute")[UPLOADS]["spec"]["rules"]
    assert "name" not in rule


def test_downloads_change_the_render_by_one_rule_one_policy_and_one_variable() -> None:
    before, after = uploads_only(), downloads_on()

    assert len(after) == len(before) + 1
    assert sorted(of_kind(after, "BackendTrafficPolicy")) == sorted(
        [FIRST, UPLOADS, DOWNLOADS]
    )

    def without_download(documents: tuple[dict, ...]) -> list[dict]:
        kept = [
            copy.deepcopy(d) for d in documents if d["metadata"]["name"] != DOWNLOADS
        ]
        for document in kept:
            if document["metadata"]["name"] == UPLOADS and document["kind"] == (
                "HTTPRoute"
            ):
                document["spec"]["rules"] = [
                    r for r in document["spec"]["rules"] if r.get("name") != RULE
                ]
            if document["kind"] == "Deployment" and document["metadata"]["name"] == (
                FIRST
            ):
                (container,) = document["spec"]["template"]["spec"]["containers"]
                container["env"] = [
                    e for e in container["env"] if e["name"] != SWITCH_ENV
                ]
        return kept

    assert without_download(after) == list(before)


def test_the_other_routes_and_policies_are_the_same_with_downloads_on() -> None:
    before, after = uploads_only(), downloads_on()

    assert (
        of_kind(after, "BackendTrafficPolicy")[UPLOADS]
        == of_kind(before, "BackendTrafficPolicy")[UPLOADS]
    )
    assert (
        of_kind(after, "BackendTrafficPolicy")[FIRST]
        == of_kind(before, "BackendTrafficPolicy")[FIRST]
    )
    assert of_kind(after, "HTTPRoute")[FIRST] == of_kind(before, "HTTPRoute")[FIRST]
    first_rule = of_kind(after, "HTTPRoute")[UPLOADS]["spec"]["rules"][0]
    assert first_rule == of_kind(before, "HTTPRoute")[UPLOADS]["spec"]["rules"][0]


# ── the rule: GET and HEAD of one shape, and nothing else ───────────────────
def test_the_rule_matches_get_and_head_of_the_download_path_and_nothing_else() -> None:
    rule = download_rule()

    assert [m["method"] for m in rule["matches"]] == ["GET", "HEAD"]
    assert {m["path"]["type"] for m in rule["matches"]} == {"RegularExpression"}
    expressions = {m["path"]["value"] for m in rule["matches"]}
    assert len(expressions) == 1
    (expression,) = expressions
    pattern = re.compile(expression)
    assert pattern.fullmatch(f"{CLAIM_PATH}{FILE}")
    for path in (
        "/",
        "/adjuster/claims",
        "/adjuster/claims/CLM-0001",
        "/adjuster/claims/CLM-0001/files",
        f"{CLAIM_PATH}",
        f"{CLAIM_PATH}{FILE}/",
        f"{CLAIM_PATH}{FILE}x",
        f"{CLAIM_PATH}{FILE.upper()}",
        f"{CLAIM_PATH}{FILE[:-1]}",
        f"{CLAIM_PATH}{FILE.replace('-', '')}",
        f"{CLAIM_PATH}../{FILE}",
        f"{CLAIM_PATH}{FILE}\n",
        f"/adjuster/claims/CLM-00011/files/{FILE}",
        f"/adjuster/claims/CLM-001/files/{FILE}",
        f"/adjuster/claims/clm-0001/files/{FILE}",
        f"/adjuster/claims//files/{FILE}",
        f"/x/adjuster/claims/CLM-0001/files/{FILE}",
        f"/claimant/claims/CLM-0001/files/{FILE}",
        f"/claims/CLM-0001/files/{FILE}",
        "/adjuster/claims/CLM-0001/proposal",
    ):
        assert not pattern.fullmatch(path), path


def test_the_edges_file_identifier_is_the_apps() -> None:
    (expression,) = {m["path"]["value"] for m in download_rule()["matches"]}

    tail = expression.removeprefix("^/adjuster/claims/CLM-[0-9]{4}/files/")
    assert tail.removesuffix("$") == FILE_ID_PATTERN.removeprefix("^").removesuffix("$")


def test_the_rule_serves_the_same_backend_as_the_other_routes() -> None:
    route = of_kind(downloads_on(), "HTTPRoute")[UPLOADS]
    first = of_kind(downloads_on(), "HTTPRoute")[FIRST]

    assert download_rule()["backendRefs"] == first["spec"]["rules"][0]["backendRefs"]
    assert route["spec"]["hostnames"] == first["spec"]["hostnames"]
    # The upload rule is still the first, with its two POST matches.
    assert [m["method"] for m in route["spec"]["rules"][0]["matches"]] == [
        "POST",
        "POST",
    ]


# ── the policy: its own, on the rule alone, 30 a minute, no larger buffer ───
def test_the_policy_targets_the_download_rule_alone_by_its_name() -> None:
    policy = of_kind(downloads_on(), "BackendTrafficPolicy")[DOWNLOADS]

    assert policy["spec"]["targetRefs"] == [
        {
            "group": "gateway.networking.k8s.io",
            "kind": "HTTPRoute",
            "name": UPLOADS,
            "sectionName": RULE,
        }
    ]
    assert download_rule()["name"] == RULE


def test_the_policy_limits_the_rule_to_thirty_requests_a_minute() -> None:
    policy = of_kind(downloads_on(), "BackendTrafficPolicy")[DOWNLOADS]

    assert policy["spec"]["rateLimit"] == {
        "local": {"rules": [{"limit": {"requests": 30, "unit": "Minute"}}]}
    }


def test_the_policy_buffers_no_more_than_the_first_route_does() -> None:
    policy = of_kind(downloads_on(), "BackendTrafficPolicy")[DOWNLOADS]

    assert policy["spec"]["requestBuffer"] == {"limit": "64Ki"}
    assert (
        policy["spec"]["requestBuffer"]
        == (
            of_kind(downloads_on(), "BackendTrafficPolicy")[FIRST]["spec"][
                "requestBuffer"
            ]
        )
    )
    assert "1126400" not in yaml.safe_dump(policy)


def test_the_limits_are_the_values() -> None:
    documents = render(
        [
            *helm_arguments(),
            *BOTH_ON,
            "--set",
            "route.downloads.rateLimit.requests=7",
            "--set-string",
            "route.downloads.rateLimit.unit=Hour",
        ]
    )

    spec = of_kind(documents, "BackendTrafficPolicy")[DOWNLOADS]["spec"]
    assert spec["rateLimit"]["local"]["rules"] == [
        {"limit": {"requests": 7, "unit": "Hour"}}
    ]
    assert (
        of_kind(documents, "BackendTrafficPolicy")[UPLOADS]
        == of_kind(uploads_only(), "BackendTrafficPolicy")[UPLOADS]
    )


def test_the_network_policies_are_identical_with_downloads_on_and_off() -> None:
    assert network_policies(downloads_on()) == network_policies(uploads_only())


# ── the variable ────────────────────────────────────────────────────────────
def test_the_switch_reaches_the_claims_api_and_only_it() -> None:
    on = downloads_on()

    assert claims_api_env(on).count(SWITCH_ENV) == 1
    deployment = of_kind(on, "Deployment")[FIRST]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    assert {e["name"]: e.get("value") for e in container["env"]}[SWITCH_ENV] == "on"
    for workload in on:
        if workload["kind"] in {"Deployment", "Job", "CronJob"} and (
            workload["metadata"]["name"] != FIRST
        ):
            spec = workload["spec"]
            if workload["kind"] == "CronJob":
                spec = spec["jobTemplate"]["spec"]
            names = [
                e["name"]
                for c in spec["template"]["spec"]["containers"]
                for e in c.get("env", [])
            ]
            assert SWITCH_ENV not in names, workload["metadata"]["name"]


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
    assert "route.downloads.enabled" in stderr


# ── downloads need uploads ──────────────────────────────────────────────────
def test_downloads_on_with_uploads_off_is_refused_with_a_sentence() -> None:
    stderr = refusal(*DOWNLOADS_ON)

    assert "route.downloads.enabled is true, but route.uploads.enabled is false" in (
        stderr
    )


# ── the guard: both switches only on a host name that ends in .localhost ────
def test_both_switches_on_a_localhost_name_render() -> None:
    for name in ("claims.meridian.localhost", "a.b.localhost", "x.localhost"):
        documents = render([*helm_arguments(), *BOTH_ON, *host(name)])

        hostnames = of_kind(documents, "HTTPRoute")[UPLOADS]["spec"]["hostnames"]
        assert hostnames == [name]
        assert SWITCH_ENV in claims_api_env(documents)


def test_the_default_values_render() -> None:
    documents = rendered_chart()

    assert sorted(of_kind(documents, "HTTPRoute")) == [FIRST]
    assert UPLOADS_ENV not in claims_api_env(documents)


def test_a_route_on_another_name_renders_while_both_switches_are_off() -> None:
    documents = render([*helm_arguments(), *host("claims.example.com")])

    assert of_kind(documents, "HTTPRoute")[FIRST]["spec"]["hostnames"] == [
        "claims.example.com"
    ]


def test_each_switch_on_another_name_is_refused_with_a_sentence_naming_s021() -> None:
    for switches in (UPLOADS_ON, BOTH_ON):
        stderr = refusal(*switches, *host("claims.example.com"))

        assert GUARD in stderr
        assert S021 in stderr
        assert "claims.example.com" in stderr


def test_names_that_only_look_local_are_refused() -> None:
    for name in (
        "localhost",
        "claims.localhost.example.com",
        "claims-localhost",
        "claims.meridian.localhost.",
        "claims.meridian.LOCALHOST",
        "claims.meridian.localhost.evil.test",
        "127.0.0.1",
        "",
    ):
        for switches in (UPLOADS_ON, BOTH_ON):
            done = run_helm([*helm_arguments(), *switches, *host(name)])
            assert done.returncode != 0, (name, switches)


def test_uploads_without_the_route_is_still_refused_first() -> None:
    stderr = refusal("--set", "route.enabled=false", *UPLOADS_ON)

    assert "route.uploads.enabled is true, but route.enabled is false" in stderr
