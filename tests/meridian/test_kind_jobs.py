"""The chart's Jobs and the sweep's CronJob.

Credentials, deadlines and hardening (S041, S063).
"""

import importlib
import importlib.util

import pytest
import typer.main
import yaml
from chartsupport import (
    CHART_DIR,
    IMAGE_REPOSITORY,
    TEST_TAG,
    helm_arguments,
    render,
)
from kindsupport import (
    DEPLOY_SH,
    INGEST_SECRET,
    INGEST_TENANT,
    OWNER_SECRET,
    SEED_SECRET,
    SWEEP_MODULE,
    SWEEP_PERIOD_SECONDS,
    SWEEP_ROLE,
    TLS_ENV,
    all_documents,
    containers,
    deployment,
    documents_of,
    env_of,
    function_body,
    job_named,
    pod_spec,
    secrets_referenced_by,
    sweep_cronjob,
    synthetic_destination,
)
from servicesupport import REGISTRY_DIR

from meridian.platform.cli import app as meridian_cli
from meridian.platform.cli.db import (
    INGEST_DATABASE_URL_ENV,
    MIGRATIONS_DATABASE_URL_ENV,
    SEED_DATABASE_URL_ENV,
)
from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.knowledge_mcp import INGESTION_AGENT
from meridian.platform.registry import load_registry
from meridian.runtime.settings import (
    GATEWAY_URL_ENV,
)
from meridian.workloads.claims_triage.sweep import (
    DOCUMENTS_DEADLINE_ENV as SWEEP_DEADLINE_ENV,
)


def only_container(job: dict) -> dict:
    (container,) = job["spec"]["template"]["spec"]["containers"]
    return container


@pytest.mark.parametrize(
    ("name", "variable", "secret"),
    [
        ("migrate", MIGRATIONS_DATABASE_URL_ENV, OWNER_SECRET),
        ("seed", SEED_DATABASE_URL_ENV, SEED_SECRET),
        ("ingest", INGEST_DATABASE_URL_ENV, INGEST_SECRET),
    ],
)
def test_each_job_runs_once_with_its_own_credentials_and_its_own_account(
    name: str, variable: str, secret: str
) -> None:
    job = job_named(name)
    reference = env_of(only_container(job))[variable]["valueFrom"]["secretKeyRef"]
    accounts = {
        d["metadata"]["name"]: d
        for d in documents_of("ServiceAccount")
        if d["metadata"]["name"] == f"meridian-{name}"
    }

    assert reference == {"name": secret, "key": "uri"}
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    assert job["spec"]["backoffLimit"] <= 3
    assert job["spec"]["activeDeadlineSeconds"] > 0
    assert set(accounts) == {f"meridian-{name}"}
    assert job["spec"]["template"]["spec"]["serviceAccountName"] == f"meridian-{name}"
    assert job["metadata"]["labels"]["app.kubernetes.io/name"] == f"meridian-{name}"


def cli_command_words(words: list[str]) -> list[str]:
    """The leading words of ``words`` that name commands of the ``meridian`` CLI,
    resolved through the Typer tree (a renamed command stops matching)."""
    command = typer.main.get_command(meridian_cli)
    found: list[str] = []
    for word in words:
        subcommands = getattr(command, "commands", None)
        if not subcommands or word not in subcommands:
            break
        found.append(word)
        command = subcommands[word]
    return found


def test_the_migration_job_runs_the_migrate_command() -> None:
    job = job_named("migrate")
    container = only_container(job)

    assert container["command"] == ["meridian", "db", "migrate"]
    assert set(env_of(container)) == {MIGRATIONS_DATABASE_URL_ENV}
    assert job["spec"]["ttlSecondsAfterFinished"] > 0


def test_the_seed_job_loads_the_policies_from_the_images_synthetic_data() -> None:
    job = job_named("seed")
    container = only_container(job)

    assert container["command"] == [
        "meridian",
        "db",
        "seed-policies",
        "--from",
        synthetic_destination(),
    ]
    assert cli_command_words(container["command"][1:]) == ["db", "seed-policies"]
    assert set(env_of(container)) == {SEED_DATABASE_URL_ENV}
    assert job["spec"]["ttlSecondsAfterFinished"] > 0


def test_the_ingest_job_embeds_the_wordings_through_the_gateway_and_is_kept() -> None:
    job = job_named("ingest")
    container = only_container(job)
    gateway = env_of(containers(deployment("agent-runtime"))[0])[GATEWAY_URL_ENV]

    ingest = [
        "meridian",
        "knowledge",
        "ingest",
        "--tenant",
        INGEST_TENANT,
        "--from",
        synthetic_destination(),
    ]
    # Since S067 the same command line then runs `meridian knowledge verify`
    # (test_ingest_job_verify.py holds the second half and what a failure does).
    assert container["command"][:2] == ["sh", "-c"]
    assert container["command"][2].startswith(" ".join(ingest) + " && ")
    assert cli_command_words(ingest[1:]) == ["knowledge", "ingest"]
    assert set(env_of(container)) == {
        INGEST_DATABASE_URL_ENV,
        GATEWAY_URL_ENV,
        *TLS_ENV,
    }
    assert env_of(container)[GATEWAY_URL_ENV] == gateway
    # The finished Job is the record that this image's corpus is in the store:
    # deploy.sh skips the ingestion when it finds it, so it must not expire.
    assert "ttlSecondsAfterFinished" not in job["spec"]
    values = (CHART_DIR / "values.yaml").read_text(encoding="utf-8")
    assert "ttlSecondsAfterFinished" in values  # the comment that says why


def test_the_ingestions_tenant_may_run_the_ingestion_agent() -> None:
    registry = load_registry(REGISTRY_DIR)

    assert registry.tenant_may_run(INGEST_TENANT, INGESTION_AGENT)


def sweep_job_spec() -> dict:
    return sweep_cronjob()["spec"]["jobTemplate"]["spec"]


def sweep_container() -> dict:
    (container,) = pod_spec(sweep_cronjob())["containers"]
    return container


def test_the_sweep_cronjob_runs_the_sweep_module_and_nothing_else() -> None:
    container = sweep_container()

    assert container["command"] == ["python", "-m", SWEEP_MODULE]
    assert "args" not in container
    # The command is a real module of the image: the Python contract's file.
    assert importlib.util.find_spec(SWEEP_MODULE) is not None


def test_the_sweep_takes_its_own_roles_connection_string_and_the_deadline() -> None:
    environment = env_of(sweep_container())

    # The role's Secret is named as common.sh's role_secret_name names it.
    secret = SWEEP_ROLE.replace("_", "-") + "-db"
    # Beside them, the three variables of the collector (S064, C2): its address,
    # the file that verifies it and the bound on the send; and (C3) the instance
    # ID that keeps the pass's series the same from one pass to the next.
    assert set(environment) == {
        DATABASE_URL_ENV,
        SWEEP_DEADLINE_ENV,
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_TIMEOUT",
        "OTEL_RESOURCE_ATTRIBUTES",
    }
    assert environment[DATABASE_URL_ENV]["valueFrom"]["secretKeyRef"] == {
        "name": secret,
        "key": "uri",
    }
    assert environment[SWEEP_DEADLINE_ENV] == {
        "name": SWEEP_DEADLINE_ENV,
        "value": "14",
    }
    assert "envFrom" not in sweep_container()


def deadline_of_each_workload(documents: list[dict]) -> dict[str, str]:
    """The documents deadline the Claims API and the sweep are each given."""
    claims_api = [
        d
        for d in documents
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "claims-api"
    ]
    sweep = [
        d
        for d in documents
        if d["kind"] == "CronJob" and d["metadata"]["name"] == "meridian-sweep"
    ]
    (api,) = claims_api
    (cron,) = sweep
    (api_container,) = containers(api)
    (sweep_pod_container,) = containers(cron["spec"]["jobTemplate"])
    return {
        "claims-api": env_of(api_container)[SWEEP_DEADLINE_ENV]["value"],
        "sweep": env_of(sweep_pod_container)[SWEEP_DEADLINE_ENV]["value"],
    }


def test_the_claims_api_and_the_sweep_are_given_the_same_documents_deadline() -> None:
    deadlines = deadline_of_each_workload(all_documents())

    assert deadlines == {"claims-api": "14", "sweep": "14"}


def test_one_value_changes_the_documents_deadline_of_both_workloads() -> None:
    documents = render([*helm_arguments(), "--set", "sweep.documentsDeadlineDays=30"])

    deadlines = deadline_of_each_workload(documents)

    assert deadlines == {"claims-api": "30", "sweep": "30"}


def test_the_sweep_reads_its_own_secret_and_the_ca_and_never_the_owners() -> None:
    pod = pod_spec(sweep_cronjob())

    assert secrets_referenced_by(pod) == {"claims-sweep-db", "platform-db-ca"}
    assert OWNER_SECRET not in yaml.dump(sweep_cronjob())
    # Only the public certificate of the CA's Secret, as the Jobs mount it.
    (volume,) = [v for v in pod["volumes"] if v["name"] == "db-ca"]
    assert volume["secret"]["items"] == [{"key": "ca.crt", "path": "ca.crt"}]
    (mount,) = [m for m in sweep_container()["volumeMounts"] if m["name"] == "db-ca"]
    assert mount == {
        "name": volume["name"],
        "mountPath": "/etc/meridian/db-ca",
        "readOnly": True,
    }


def test_the_sweep_has_an_account_of_its_own_with_no_service_account_token() -> None:
    accounts = {
        d["metadata"]["name"]: d
        for d in documents_of("ServiceAccount")
        if d["metadata"]["name"] == "meridian-sweep"
    }
    pod = pod_spec(sweep_cronjob())

    assert set(accounts) == {"meridian-sweep"}
    assert accounts["meridian-sweep"]["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == "meridian-sweep"
    assert pod["automountServiceAccountToken"] is False
    # Nothing in the chart grants an account a right, and the objects the sweep
    # owns are its account, its CronJob and its NetworkPolicy.
    assert not [d["kind"] for d in all_documents() if "Role" in d["kind"]]
    assert {
        d["kind"]
        for d in all_documents()
        if d["metadata"].get("labels", {}).get("app.kubernetes.io/name")
        == "meridian-sweep"
    } == {"ServiceAccount", "CronJob", "NetworkPolicy"}


def test_the_sweeps_pod_is_hardened_like_the_jobs_pods() -> None:
    pod = pod_spec(sweep_cronjob())
    context = sweep_container()["securityContext"]
    resources = sweep_container()["resources"]

    assert context["runAsNonRoot"] is True
    assert context["allowPrivilegeEscalation"] is False
    assert context["capabilities"]["drop"] == ["ALL"]
    assert context["seccompProfile"]["type"] == "RuntimeDefault"
    assert pod["restartPolicy"] == "Never"
    assert {"cpu", "memory"} <= set(resources["requests"])
    assert "memory" in resources["limits"]
    assert sweep_container()["image"] == f"{IMAGE_REPOSITORY}:{TEST_TAG}"
    assert sweep_container()["imagePullPolicy"] == "Never"
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        assert not pod.get(flag), flag


def test_the_sweep_never_runs_two_passes_at_once_and_a_failed_pass_is_not_retried() -> (
    None
):
    spec = sweep_cronjob()["spec"]

    assert spec["schedule"] == "*/5 * * * *"
    assert spec["concurrencyPolicy"] == "Forbid"
    assert not spec.get("suspend")
    # The next run is the retry: a pod that failed is not started again.
    assert sweep_job_spec()["backoffLimit"] == 0


def test_a_sweep_pass_ends_well_before_the_next_one_is_due() -> None:
    deadline = sweep_job_spec()["activeDeadlineSeconds"]

    # Forbid skips a run while the last one is alive, so a hung pass would
    # silence the sweep: the deadline ends it, with room to spare.
    assert 0 < deadline <= SWEEP_PERIOD_SECONDS // 2


def test_the_sweep_keeps_three_successes_three_failures_and_a_day_of_history() -> None:
    spec = sweep_cronjob()["spec"]

    # A run the controller cannot start within two minutes is skipped; the next
    # one is five minutes away.
    assert spec["startingDeadlineSeconds"] == 120
    # The last success is the one `make smoke` reads. A Job made by hand is
    # owned by the CronJob and counts: with one kept, a by-hand success evicted
    # the schedule's own (seen on kind on 2026-10-07), so three are kept, and a
    # by-hand run or two leave the schedule's last success in the history. A
    # failure (three kept) is what the TTL, one day, leaves to read in the
    # morning, and the last success of a suspended CronJob.
    assert spec["successfulJobsHistoryLimit"] == 3
    assert spec["failedJobsHistoryLimit"] == 3
    assert sweep_job_spec()["ttlSecondsAfterFinished"] == 24 * 60 * 60


def test_the_sweep_cronjob_carries_the_labels_of_the_other_manifests() -> None:
    labels = {
        "app.kubernetes.io/name": "meridian-sweep",
        "app.kubernetes.io/part-of": "meridian",
    }
    cronjob = sweep_cronjob()

    assert cronjob["metadata"]["labels"] == labels
    assert cronjob["spec"]["jobTemplate"]["metadata"]["labels"] == labels
    assert (
        cronjob["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
        == labels
    )


def test_the_release_holds_the_sweep_with_the_image_and_no_tag_in_its_name() -> None:
    release = render(helm_arguments(jobs=()))
    (cronjob,) = [d for d in release if d["kind"] == "CronJob"]

    # Part of the release, not run by run_job; a CronJob's name is fixed, so a
    # new image changes its spec in place (a Job's spec cannot change).
    assert cronjob["metadata"]["name"] == "meridian-sweep"
    assert TEST_TAG not in cronjob["metadata"]["name"]
    assert pod_spec(cronjob)["containers"][0]["image"].endswith(f":{TEST_TAG}")
    assert "meridian-sweep" not in function_body(DEPLOY_SH, "run_job")
