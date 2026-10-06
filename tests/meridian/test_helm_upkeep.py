"""The chart can run the gateway's upkeep command as a Job (S066, decision 8).

``jobs.upkeep.enabled`` renders a ServiceAccount, a NetworkPolicy and one Job
that runs ``meridian gateway`` with the arguments of ``jobs.upkeep.args`` as the
database role ``gateway_upkeep``. It is off by default and is not part of the
release: ``infra/kind/upkeep.sh`` renders it for one run and applies it outside
the release, as ``deploy.sh`` applies the three Jobs, so ``make deploy`` neither
creates nor removes it, and no workload OF THE RELEASE holds the role's Secret.
The script's own tests are in ``test_kind_upkeep_script.py``.
"""

import json

import pytest
import yaml
from chartsupport import (
    NAME_LABEL,
    NAMESPACE,
    TEST_TAG,
    helm_arguments,
    network_policies,
    pod_labels,
    pod_spec,
    render,
    rendered_chart,
    run_helm,
)
from servicesupport import REPO_ROOT

APP = "meridian-upkeep"
SECRET = "gateway-upkeep-db"  # noqa: S105 (a Secret name, not a password)
VARIABLE = "MERIDIAN_GATEWAY_UPKEEP_DATABASE_URL"
ROLE = "gateway_upkeep"
SUFFIX = "261006-101500-4242"
ARGS = ["reservations", "--older-than", "15"]
PLATFORM_DB_POLICY = (
    REPO_ROOT / "infra" / "kind" / "manifests" / ("platform-db-networkpolicy.yaml")
)


def upkeep_arguments(
    args: list | None = None,
    suffix: str | None = SUFFIX,
    *,
    tag: str = TEST_TAG,
) -> list[str]:
    """``helm template`` arguments with the upkeep Job on beside the three
    others (``rendered_chart()``'s), the arguments as ``upkeep.sh`` passes them:
    JSON (``--set-json``), and the suffix as a string."""
    arguments = helm_arguments(tag=tag)
    arguments += ["--set", "jobs.upkeep.enabled=true"]
    if suffix is not None:
        arguments += ["--set-string", f"jobs.upkeep.runSuffix={suffix}"]
    if args is not None:
        arguments += ["--set-json", f"jobs.upkeep.args={json.dumps(args)}"]
    return arguments


def upkeep_documents(args: list | None = None, **kwargs) -> list[dict]:
    return render(upkeep_arguments(ARGS if args is None else args, **kwargs))


def upkeep_job(args: list | None = None, **kwargs) -> dict:
    (job,) = [
        d
        for d in upkeep_documents(args, **kwargs)
        if d["kind"] == "Job" and d["metadata"]["name"].startswith(APP)
    ]
    return job


def container_of(job: dict) -> dict:
    (container,) = pod_spec(job)["containers"]
    return container


def migrate_job() -> dict:
    (job,) = [
        d
        for d in render(helm_arguments(jobs=("migrate",)))
        if d["kind"] == "Job" and d["metadata"]["name"].startswith("meridian-migrate-")
    ]
    return job


# ── off by default, and not part of the release ──────────────────────────────
def test_the_default_render_holds_no_upkeep_object_and_no_reference_to_its_secret() -> (
    None
):
    documents = rendered_chart()

    names = {d["metadata"]["name"] for d in documents}
    assert not {n for n in names if n.startswith(APP)}
    rendered = yaml.dump(list(documents))
    assert SECRET not in rendered
    assert ROLE not in rendered
    assert VARIABLE not in rendered


def test_the_value_is_off_in_the_chart_and_in_kinds_values() -> None:
    chart = yaml.safe_load(
        (REPO_ROOT / "infra/helm/meridian/values.yaml").read_text(encoding="utf-8")
    )
    kind = yaml.safe_load(
        (REPO_ROOT / "infra/kind/values/meridian.yaml").read_text(encoding="utf-8")
    )

    assert chart["jobs"]["upkeep"]["enabled"] is False
    assert chart["jobs"]["upkeep"]["args"] == []
    assert chart["jobs"]["upkeep"]["runSuffix"] == ""
    assert "upkeep" not in kind.get("jobs", {})


def test_with_the_job_on_only_its_own_objects_hold_the_roles_secret() -> None:
    # The release's workloads still hold none: only the Job, applied beside it.
    release = {d["metadata"]["name"] for d in rendered_chart()}
    documents = upkeep_documents()

    holders = [d for d in documents if SECRET in yaml.dump(d)]
    assert [(d["kind"], d["metadata"]["name"]) for d in holders] == [
        ("Job", f"{APP}-{SUFFIX}")
    ]
    assert {d["metadata"]["name"] for d in documents} - release == {
        APP,
        f"{APP}-{SUFFIX}",
    }


def test_turning_the_job_on_adds_three_objects_and_changes_no_other_name() -> None:
    release = rendered_chart()

    documents = upkeep_documents()

    added = [d for d in documents if d["metadata"]["name"].startswith(APP)]
    assert sorted(d["kind"] for d in added) == [
        "Job",
        "NetworkPolicy",
        "ServiceAccount",
    ]
    others = [d for d in documents if d not in added]
    assert others == list(release)


# ── what the Job is ──────────────────────────────────────────────────────────
def test_the_command_is_meridian_gateway_and_the_arguments_are_a_list_of_strings() -> (
    None
):
    container = container_of(upkeep_job())

    assert container["command"] == ["meridian", "gateway"]
    assert container["args"] == ["reservations", "--older-than", "15"]
    assert all(isinstance(a, str) for a in container["args"])


@pytest.mark.parametrize(
    "args",
    [
        ["credit", "tenant-a", "--eur", "1.5", "--reason", "retry-loop"],
        ["close", "0f8fad5b-d9cb-469f-a165-70867728950e", "--release"],
        ["expire", "--before", "2026-09", "--reason", "old-months", "--confirm"],
        ["true", "null", "15", "1e3", "0x10", "yes", "~", "2026-09"],
    ],
)
def test_every_argument_reaches_the_pod_as_the_string_it_was_given(
    args: list[str],
) -> None:
    container = container_of(upkeep_job(args))

    assert container["args"] == args
    assert all(isinstance(a, str) for a in container["args"])


def test_an_argument_with_shell_syntax_is_one_argument_and_is_never_a_shell_line() -> (
    None
):
    args = ["credit", "$(touch /tmp/x)", "; touch /tmp/y", "a b", "`id`", "*"]

    container = container_of(upkeep_job(args))

    assert container["args"] == args
    assert container["command"] == ["meridian", "gateway"]
    assert not {"sh", "bash", "-c"} & set(container["command"])


def test_the_database_url_comes_from_the_roles_secret_and_nothing_else_is_set() -> None:
    container = container_of(upkeep_job())

    assert container["env"] == [
        {
            "name": VARIABLE,
            "valueFrom": {"secretKeyRef": {"name": SECRET, "key": "uri"}},
        }
    ]
    assert "envFrom" not in container


def test_the_job_has_a_service_account_of_its_own_with_no_token() -> None:
    documents = upkeep_documents()

    (account,) = [
        d
        for d in documents
        if d["kind"] == "ServiceAccount" and d["metadata"]["name"] == APP
    ]
    assert account["metadata"]["name"] == APP
    assert account["automountServiceAccountToken"] is False
    spec = pod_spec(upkeep_job())
    assert spec["serviceAccountName"] == APP
    assert spec["automountServiceAccountToken"] is False


def test_the_pod_has_the_hardening_of_the_migration_job_and_the_same_volumes() -> None:
    upkeep = upkeep_job()
    migrate = migrate_job()

    assert pod_spec(upkeep)["securityContext"] == pod_spec(migrate)["securityContext"]
    assert (
        container_of(upkeep)["securityContext"]
        == container_of(migrate)["securityContext"]
    )
    assert container_of(upkeep)["image"] == container_of(migrate)["image"]
    assert container_of(upkeep)["resources"] == container_of(migrate)["resources"]
    assert pod_spec(upkeep)["volumes"] == pod_spec(migrate)["volumes"]
    assert container_of(upkeep)["volumeMounts"] == container_of(migrate)["volumeMounts"]
    assert pod_spec(upkeep)["restartPolicy"] == "Never"


def test_a_failed_change_is_not_retried_and_the_output_is_kept_for_a_day() -> None:
    spec = upkeep_job()["spec"]

    assert spec["backoffLimit"] == 0
    assert spec["ttlSecondsAfterFinished"] == 86400
    assert 0 < spec["activeDeadlineSeconds"] <= 300


def test_the_name_ends_in_the_run_suffix_and_not_in_the_image_tag() -> None:
    first = upkeep_job(suffix="run-one", tag="aaaaaaaaaaaa")
    second = upkeep_job(suffix="run-two", tag="aaaaaaaaaaaa")
    same_run_other_image = upkeep_job(suffix="run-one", tag="bbbbbbbbbbbb")

    assert first["metadata"]["name"] == f"{APP}-run-one"
    assert second["metadata"]["name"] == f"{APP}-run-two"
    assert same_run_other_image["metadata"]["name"] == first["metadata"]["name"]


def test_the_pod_and_the_job_carry_the_apps_labels_the_policy_selects() -> None:
    job = upkeep_job()

    assert pod_labels(job) == {
        NAME_LABEL: APP,
        "app.kubernetes.io/part-of": "meridian",
    }
    assert job["metadata"]["namespace"] == NAMESPACE


# ── what it may reach ────────────────────────────────────────────────────────
def test_the_policy_allows_dns_and_the_database_and_no_ingress_and_no_collector() -> (
    None
):
    policies = network_policies(upkeep_documents())

    policy = policies[APP]
    assert policy["spec"]["podSelector"] == {"matchLabels": {NAME_LABEL: APP}}
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in policy["spec"]
    ports = [[p["port"] for p in rule["ports"]] for rule in policy["spec"]["egress"]]
    assert ports == [[53, 53], [5432]]


def test_the_upkeep_policy_has_the_egress_of_the_migration_jobs_policy() -> None:
    policies = network_policies(upkeep_documents())

    upkeep = policies[APP]["spec"]["egress"]
    assert upkeep == policies["meridian-migrate"]["spec"]["egress"]


def test_the_databases_policy_admits_the_jobs_pod_by_its_labels() -> None:
    admitted = [
        rule
        for rule in yaml.safe_load(PLATFORM_DB_POLICY.read_text(encoding="utf-8"))[
            "spec"
        ]["ingress"]
        if any(p.get("port") == 5432 for p in rule.get("ports", []))
    ]
    (rule,) = admitted
    (peer,) = rule["from"]
    selector = peer["podSelector"]["matchLabels"]

    labels = pod_labels(upkeep_job())

    assert "namespaceSelector" not in peer
    assert selector.items() <= labels.items()


# ── what the chart refuses ───────────────────────────────────────────────────
def refused(arguments: list[str]) -> str:
    done = run_helm(arguments)
    assert done.returncode != 0, "the chart rendered what it should refuse"
    return done.stderr


def test_enabled_with_no_arguments_the_chart_refuses_and_names_the_value() -> None:
    no_value = refused(upkeep_arguments(None))
    empty_list = refused(upkeep_arguments([]))

    for error in (no_value, empty_list):
        assert "jobs.upkeep.args" in error


def test_the_chart_refuses_an_argument_that_is_not_a_string() -> None:
    for args in (["reservations", 15], ["credit", True], [["nested"]], ["a", None]):
        assert "jobs.upkeep.args" in refused(upkeep_arguments(args))


@pytest.mark.parametrize(
    "suffix",
    [
        None,
        "",
        "Run1",
        "run_1",
        "-run",
        "run-",
        "run.1",
        "run 1",
        "a" * 33,
        "$(id)",
    ],
)
def test_the_chart_refuses_a_run_suffix_that_is_not_a_short_dns_label(
    suffix: str | None,
) -> None:
    error = refused(upkeep_arguments(ARGS, suffix))

    assert "jobs.upkeep.runSuffix" in error


@pytest.mark.parametrize("suffix", ["a", "1", "261006-101500-4242", "a" * 32, "0-0"])
def test_the_chart_accepts_a_suffix_that_is_a_dns_label_of_32_characters_or_fewer(
    suffix: str,
) -> None:
    job = upkeep_job(suffix=suffix)

    assert job["metadata"]["name"] == f"{APP}-{suffix}"
    assert len(job["metadata"]["name"]) + len("-xxxxx") <= 63


def test_the_chart_refuses_the_job_without_the_roles_secret_named() -> None:
    arguments = [*upkeep_arguments(ARGS), "--set-string", "database.upkeepSecret="]

    assert "database.upkeepSecret" in refused(arguments)


def test_with_the_policy_off_the_job_renders_with_no_policy() -> None:
    arguments = [*upkeep_arguments(ARGS), "--set", "networkPolicy.enabled=false"]

    documents = render(arguments)

    assert [d["kind"] for d in documents if d["metadata"]["name"].startswith(APP)] == [
        "ServiceAccount",
        "Job",
    ]
