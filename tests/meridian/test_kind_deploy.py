"""deploy.sh, run against stand-in commands.

The order, the Jobs, the ingestion and the token window.
"""

import os
import re
import subprocess

import pytest
from chartsupport import (
    JOBS,
    RATE_STORE,
    TEST_TAG,
    helm_arguments,
    render,
)
from kindsupport import (
    COMMON_SH,
    DEPLOY_SH,
    DOCKERFILE,
    DOCKERIGNORE,
    SERVICES,
    SMOKE_SH,
    documents_of,
    function_body,
    function_definition,
    job_named,
)

from meridian.platform.gateway.ratelimit import (
    TOKEN_WINDOW_SECONDS as GATEWAY_TOKEN_WINDOW_SECONDS,
)
from meridian.platform.knowledge_mcp.ingest import MAX_TOTAL_WAIT_SECONDS


def run_require_database(*, policy: bool) -> subprocess.CompletedProcess[str]:
    """``require_database`` from deploy.sh in bash against a stub ``kctl`` that
    knows the Database, no Secret to check and, when ``policy``, the NetworkPolicy
    ``platform-db``."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "NAMESPACE=meridian; DATABASE_ROLES=()",
            'die() { echo "error: $*" >&2; exit 1; }',
            "database_roles_reconciled() { return 0; }",
            # The address check has its own tests, with the real function.
            "api_server_matches_policy() { return 0; }",
            "kctl() {",
            '  case "$*" in',
            '    *"get database"*) printf true ;;',
            '    *"get networkpolicy platform-db"*)',
            '      [[ "${POLICY}" == yes ]] || return 1 ;;',
            "  esac",
            "}",
            function_definition(DEPLOY_SH, "require_database"),
            "require_database",
            "echo passed",
        ]
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "POLICY": "yes" if policy else "no"},
        check=False,
    )


def test_deploy_dies_with_make_up_when_the_database_policy_is_missing() -> None:
    missing = run_require_database(policy=False)
    present = run_require_database(policy=True)

    assert missing.returncode != 0
    assert "NetworkPolicy 'platform-db'" in missing.stderr
    assert "default-deny" in missing.stderr
    assert "run 'make up' first" in missing.stderr
    assert "passed" not in missing.stdout
    assert present.returncode == 0, present.stderr
    assert "passed" in present.stdout


def test_the_release_holds_no_job_a_flag_renders_one_and_deploy_knows_services() -> (
    None
):
    release = render(helm_arguments(jobs=()))
    (services,) = re.findall(r"^readonly SERVICES=\((.*)\)$", DEPLOY_SH, re.MULTILINE)

    # The Jobs are run by deploy.sh (a Job's spec cannot change), so the release
    # holds none by default.
    assert not [d for d in release if d["kind"] == "Job"]
    for name in JOBS:
        flagged = render(helm_arguments(jobs=(name,)))
        (job,) = [d for d in flagged if d["kind"] == "Job"]
        added = [d for d in flagged if d not in release]
        assert job["metadata"]["name"] == f"meridian-{name}-{TEST_TAG}"
        # The Job's NetworkPolicy comes with it: deploy.sh applies what its
        # template renders and nothing else of the chart.
        assert sorted(d["kind"] for d in added) == [
            "Job",
            "NetworkPolicy",
            "ServiceAccount",
        ], name
        (account,) = [d for d in added if d["kind"] == "ServiceAccount"]
        assert account["metadata"]["name"] == f"meridian-{name}"
    assert set(services.split()) == set(SERVICES)
    # deploy.sh's SERVICES are the six, and the store (kind turns it on) is its
    # own Deployment, which deploy.sh waits for apart.
    assert {d["metadata"]["name"] for d in documents_of("Deployment")} == {
        *SERVICES,
        RATE_STORE,
    }
    # Each service's database role, and so its Secret, is one deploy.sh checks.
    (roles,) = re.findall(
        r"^readonly DATABASE_ROLES=\((.*)\)$", COMMON_SH, re.MULTILINE
    )
    assert {s.replace("-", "_") for s in SERVICES} <= set(roles.split())


def test_deploy_installs_the_release_and_leaves_the_waiting_to_its_own_rollouts() -> (
    None
):
    body = function_body(DEPLOY_SH, "install_release")

    for flag in ("--install", "--take-ownership", "--server-side=true"):
        assert flag in body, flag
    assert "--force-conflicts" in body
    # The script's own rollout waits stay: Helm neither waits nor rolls back,
    # and it never creates the namespace (make up does).
    for flag in ("--wait", "--atomic", "--create-namespace"):
        assert flag not in body, flag
    assert ">/dev/null" in body
    assert "status ${RELEASE}" in body  # how the owner finds out why it failed


def main_sequence() -> list[str]:
    """The calls ``deploy.sh`` makes at its top level, from the first one."""
    lines = DEPLOY_SH.splitlines()
    return [
        line
        for line in lines[lines.index("require_database") :]
        if line and not line.startswith("log ")
    ]


def test_deploy_migrates_seeds_installs_ingests_and_then_waits_in_that_order() -> None:
    # The seed runs before the services start: a claim that met an empty policy
    # table would get a stored proposal "policy not found", which is final. The
    # ingestion calls the gateway, so it follows the gateway's rollout. The
    # certificates come right after the release: the ingestion Job mounts a
    # Secret that cert-manager makes from one of them (S055). The issuer is a
    # precondition like the database: it is checked before the image is built
    # and before any Job runs (S056); so is the approval of the Certificates:
    # approver-policy with its five policies. So is the rate store's Secret
    # (S066), which `make up` makes: a pod that cannot read it would not start,
    # after the Jobs had run. The store is waited for before the gateway, whose
    # every call (the ingestion's too) needs it.
    assert main_sequence() == [
        "require_database",
        "require_issuer",
        "require_approval",
        "require_rate_store_secret",
        "build_image",
        'run_job "meridian-migrate-${tag}" migrate',
        'run_job "meridian-seed-${tag}" seed',
        "install_release",
        "wait_for_certificates",
        'wait_for_deployment "${RATE_STORE_DEPLOYMENT}"',
        'wait_for_deployment "${GATEWAY_SERVICE}"',
        "ingest_corpus",
        "wait_for_other_rollouts",
        "wait_for_route",
        "wait_for_token_window",
        # S075: the record of who holds the cluster, written last, when it ended well
        # (`changing` was written before the first of these, right after the check).
        "record_cluster_holder ok",
    ]
    assert "run_migrations" not in DEPLOY_SH


def test_deploy_names_each_job_as_the_chart_does() -> None:
    for name in JOBS:
        chart_name = job_named(name)["metadata"]["name"]
        assert chart_name.replace(TEST_TAG, "${tag}") in DEPLOY_SH


def test_deploy_runs_a_job_from_a_clean_slate_to_completion_and_shows_its_log() -> None:
    body = function_body(DEPLOY_SH, "run_job")

    positions = [
        body.index(part)
        for part in (
            'delete "job/${job}" --ignore-not-found --wait',
            "render_job",
            "kctl apply --server-side",
            'job_state "${job}"',
            'logs "job/${job}"',
        )
    ]
    assert positions == sorted(positions)
    assert "failed)" in body
    assert "JOB_TIMEOUT" in body


def test_deploy_ingests_at_most_once_per_image_and_removes_the_other_ingestions() -> (
    None
):
    body = function_body(DEPLOY_SH, "ingest_corpus")
    label = job_named("ingest")["metadata"]["labels"]["app.kubernetes.io/name"]

    positions = [
        body.index(part)
        for part in (
            'job_state "${job}"',
            "already in the store",
            f"-l app.kubernetes.io/name={label}",
            'run_job "${job}" ingest',
            "ingested_at=${SECONDS}",
        )
    ]
    assert positions == sorted(positions)
    assert "succeeded" in body


def test_deploy_stops_on_a_failed_job_lookup_instead_of_ingesting_again() -> None:
    body = function_body(DEPLOY_SH, "ingest_corpus")
    (lookup,) = re.findall(r'^.*get job "\$\{job\}".*$', body, re.MULTILINE)
    (die_line,) = re.findall(r"^.*die .*could not look for.*$", body, re.MULTILINE)

    # An error is not "no such Job": the lookup asks for an empty answer when
    # the Job is absent, and a failure of it ends the deploy.
    assert "--ignore-not-found" in lookup
    assert "|| state=absent" not in body
    # A warning on stderr is not an answer: it must not read as "the Job
    # exists". kubectl's own stderr goes to the terminal, and the message does
    # not quote the answer.
    assert "2>&1" not in body
    assert "${found}" not in die_line


def test_deploy_skips_the_ingestion_only_when_the_store_holds_chunks() -> None:
    body = function_body(DEPLOY_SH, "ingest_corpus")
    reader = function_body(DEPLOY_SH, "stored_chunk_count")
    lines = [line.strip() for line in body.splitlines()]

    # A finished Job is not proof that the store holds a corpus: the rows are
    # counted in the database's primary pod, the way smoke.sh reaches psql.
    assert body.index("stored_chunk_count") < body.index("already in the store")
    assert "cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary" in reader
    assert "-c postgres" in reader
    assert "psql -d meridian" in reader
    assert "${CHUNK_COUNT_SQL}" in reader
    assert "SELECT count(*) FROM knowledge.chunks" in COMMON_SH
    # The skip is the branch that counted more than zero, and it returns.
    assert "> 0" in body
    skip = next(i for i, line in enumerate(lines) if "already in the store" in line)
    assert lines[skip + 1] == "return 0"


def run_ingest_corpus(count: str) -> tuple[list[str], str]:
    """``ingest_corpus`` from deploy.sh in bash, with the Job of this tag
    succeeded and ``count`` as the answer of the row count (``FAIL`` makes the
    query fail). The stdout lines and the final ``ingested_at``."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "NAMESPACE=meridian; tag=abc; image=meridian:abc; ingested_at=''",
            *re.findall(r"^readonly CHUNK_COUNT_SQL=.*$", COMMON_SH, re.M),
            'log() { echo "LOG $*"; }',
            'die() { echo "DIE $*"; exit 1; }',
            "job_state() { echo succeeded; }",
            'run_job() { echo "RUN $*"; }',
            "kctl() {",
            '  case "$*" in',
            '    *"delete jobs"*) echo DELETE ;;',
            '    *" exec "*) [[ "${COUNT}" != FAIL ]] || return 1; echo "${COUNT}" ;;',
            '    *"get pod"*) echo platform-db-1 ;;',
            '    *"get job"*) echo job.batch/meridian-ingest-abc ;;',
            "  esac",
            "}",
            function_definition(DEPLOY_SH, "stored_chunk_count"),
            function_definition(DEPLOY_SH, "ingest_corpus"),
            "ingest_corpus",
            'echo "ingested_at=${ingested_at}"',
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "COUNT": count},
        check=True,
    )
    *lines, last = done.stdout.splitlines()
    return lines, last


def test_a_succeeded_job_with_chunks_in_the_store_is_not_ingested_again() -> None:
    lines, ingested_at = run_ingest_corpus("12")

    assert [line for line in lines if "already in the store" in line]
    assert not [line for line in lines if line.startswith(("RUN", "DELETE"))]
    assert ingested_at == "ingested_at="


@pytest.mark.parametrize("count", ["0", "FAIL", "", "not-a-number"])
def test_a_succeeded_job_with_no_chunks_or_no_answer_ingests_again(count: str) -> None:
    lines, ingested_at = run_ingest_corpus(count)

    assert not [line for line in lines if "already in the store" in line]
    (reason,) = [line for line in lines if "ingesting again" in line]
    assert "not-a-number" not in reason  # an answer is never quoted
    assert [line for line in lines if line.startswith("RUN")]
    assert re.fullmatch(r"ingested_at=\d+", ingested_at)  # the wait is armed


def test_deploy_prints_a_jobs_log_through_the_printable_ascii_filter() -> None:
    body = function_body(DEPLOY_SH, "run_job")
    filter_body = function_body(COMMON_SH, "printable_ascii")
    log_reads = re.findall(r"^.*logs \"job/\$\{job\}\".*$", body, re.MULTILINE)

    # The success line and both failure paths (a verdict and the timeout): a
    # Job's log can quote data of a checkout, and an escape sequence must not
    # reach the terminal.
    assert len(log_reads) == 3
    for line in log_reads:
        assert "| printable_ascii" in line
    assert "tr -cd" in filter_body


def test_the_printable_ascii_filter_drops_escapes_and_other_bytes() -> None:
    hostile = "ok \\033[31mred\\033[0m\\tcaf\\303\\251\\r\\nnext\\n"
    script = (
        function_definition(COMMON_SH, "printable_ascii")
        + f"printf '{hostile}' | printable_ascii"
    )

    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )

    assert done.stdout == "ok [31mred[0mcaf\nnext\n"


def test_the_printable_ascii_filter_redacts_a_postgresql_connection_string() -> None:
    # Synthetic: the host is under .invalid and the password says what it is.
    lines = (
        "connect failed: postgresql://role:not-a-secret@db.invalid/x refused",
        "also postgres://role:not-a-secret@db.invalid:5432/x?sslmode=require",
        "plain line",
    )
    script = (
        function_definition(COMMON_SH, "printable_ascii")
        + "printf '%s\\n' "
        + " ".join(f"'{line}'" for line in lines)
        + " | printable_ascii"
    )

    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )

    assert "not-a-secret" not in done.stdout
    assert "db.invalid" not in done.stdout
    assert done.stdout.splitlines() == [
        "connect failed: postgresql://[redacted] refused",
        "also postgresql://[redacted]",
        "plain line",
    ]


def test_the_smoke_probe_reads_server_names_from_stdout_only() -> None:
    body = function_body(SMOKE_SH, "check_tools")
    (probe_call,) = re.findall(r"^.*toolprobe.*$", body, re.MULTILINE)

    # Stderr stays out of the names (a warning is not a server) but is shown
    # when the probe fails.
    assert "2>&1" not in probe_call
    assert '2>"${err_file}"' in probe_call
    (failure,) = re.findall(r"^\s*fail .*probe in deployment.*$", body, re.MULTILINE)
    assert '"${err_file}"' in failure


def test_deploy_waits_out_the_token_window_the_ingestion_opens() -> None:
    (window,) = re.findall(
        r"^readonly TOKEN_WINDOW_SECONDS=(\d+)$", DEPLOY_SH, re.MULTILINE
    )
    body = function_body(DEPLOY_SH, "wait_for_token_window")

    # Longer than the gateway's sliding window, or the wait ends with part of
    # the ingestion's reservation still counted.
    assert int(window) > GATEWAY_TOKEN_WINDOW_SECONDS
    assert "TOKEN_WINDOW_SECONDS" in body
    assert 'sleep "${remaining}"' in body  # and it does wait
    assert "- SECONDS" in body  # the script's own clock
    assert "${ingested_at}" in body  # no ingestion in this deploy: no wait
    assert "claims-triage" in body
    # The node's clock is not the laptop's: no Kubernetes timestamp is read.
    assert "Timestamp" not in DEPLOY_SH
    assert "completionTime" not in DEPLOY_SH


def run_ingest_then_token_window(
    *, job: str, chunks: str
) -> subprocess.CompletedProcess[str]:
    """``ingest_corpus`` and then ``wait_for_token_window`` of deploy.sh in bash
    against stubs: ``kctl`` finds the ingestion Job unless ``job`` is
    ``absent``, ``job_state`` says ``job`` (``succeeded`` or ``absent``),
    ``stored_chunk_count`` prints ``chunks``, ``run_job`` and ``sleep`` only
    say that they ran, and ``log`` prints its words on stdout."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "die() { printf 'error: %s\\n' \"$*\" >&2; exit 1; }",
            'log() { printf "log: %s\\n" "$*"; }',
            'sleep() { printf "sleep %s\\n" "$1"; }',
            'run_job() { printf "run_job %s\\n" "$1"; }',
            f'job_state() {{ printf "%s" "{job}"; }}',
            f'stored_chunk_count() {{ printf "%s" "{chunks}"; }}',
            "kctl() {",
            '  case "$*" in',
            f'    *"get job"*) [[ "{job}" == absent ]] || echo job.batch/stub ;;',
            "  esac",
            "}",
            "NAMESPACE=meridian tag=abc image=stub:abc",
            *re.findall(r"^readonly TOKEN_WINDOW_SECONDS=\d+$", DEPLOY_SH, re.M),
            'ingested_at=""',
            function_definition(DEPLOY_SH, "ingest_corpus"),
            function_definition(DEPLOY_SH, "wait_for_token_window"),
            "ingest_corpus",
            "wait_for_token_window",
        ]
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_deploy_says_it_skipped_the_token_window_when_the_ingestion_was_not_run() -> (
    None
):
    done = run_ingest_then_token_window(job="succeeded", chunks="85")

    assert done.returncode == 0, done.stderr
    message = " ".join(done.stdout.split())
    assert "run_job" not in done.stdout
    # No wait, and one line that says so and why: this run did not run it.
    assert "sleep" not in done.stdout
    assert "did not run the ingestion" in message
    assert "skipped" in message
    # What the reader may meet, and the remedy.
    assert "token" in message
    assert "wait a minute" in message
    assert "run it again" in message
    # One line, not a stack of them.
    assert len([line for line in done.stdout.splitlines() if "skipped" in line]) == 1


@pytest.mark.parametrize(
    ("job", "chunks"), [("succeeded", "0"), ("succeeded", ""), ("absent", "")]
)
def test_deploy_waits_as_before_and_prints_no_skip_when_this_run_ran_the_ingestion(
    job: str, chunks: str
) -> None:
    done = run_ingest_then_token_window(job=job, chunks=chunks)

    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert any(line.startswith("run_job meridian-ingest-abc") for line in lines)
    assert any(line.startswith("log: waiting ") for line in lines)
    assert any(line.startswith("sleep ") for line in lines)
    assert "skipped" not in done.stdout
    assert "did not run the ingestion" not in done.stdout


def test_the_ingest_job_is_not_retried_and_ends_after_the_ingestions_longest_wait() -> (
    None
):
    job = job_named("ingest")
    (timeout,) = re.findall(r"^readonly JOB_TIMEOUT=(\d+)$", DEPLOY_SH, re.MULTILINE)

    # A second pod straight after a failure would meet the token window the
    # first one filled; the next deploy is the retry.
    assert job["spec"]["backoffLimit"] == 0
    # The command waits up to MAX_TOTAL_WAIT_SECONDS for the gateway and then
    # needs time to audit its refusal: the deadline must not cut it short, and
    # deploy.sh must not give up on the Job before the deadline ends it.
    assert job["spec"]["activeDeadlineSeconds"] > MAX_TOTAL_WAIT_SECONDS
    assert int(timeout) > job["spec"]["activeDeadlineSeconds"]


def test_smoke_runs_the_tool_check_after_the_database_check() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]

    assert calls[:3] == ["check_edge", "check_database", "check_tools"]


def test_the_tool_check_skips_only_when_no_meridian_deployment_exists() -> None:
    body = function_body(SMOKE_SH, "check_tools")
    # The lookup is deployed_services, which the cost check shares.
    found = re.search(
        r"get deployment.*?--ignore-not-found",
        function_body(SMOKE_SH, "deployed_services"),
        re.DOTALL,
    )
    assert found, "no lookup of the Deployments"
    lookup = found.group(0)
    skips = re.findall(r"^\s*skip .*$", body, re.MULTILINE)

    # Any Meridian Deployment makes the probe required: a missing or renamed
    # agent-runtime fails the probe's exec instead of skipping the check.
    assert "$(deployed_services)" in body
    assert "get deployment" not in body
    assert "-l app.kubernetes.io/part-of=meridian" in lookup
    assert "agent-runtime" not in lookup
    assert "--ignore-not-found" in lookup
    assert len(skips) == 1
    assert "not deployed" in skips[0]
    assert "deploy/agent-runtime" in body  # the probe still runs there
    # A warning on stderr is not an answer (the probe's own call is checked by
    # test_the_smoke_probe_reads_server_names_from_stdout_only).
    assert "2>&1" not in lookup


def test_the_dockerfile_declares_no_secret_looking_variable() -> None:
    joined = re.sub(r"\\\n", " ", DOCKERFILE)
    declared: list[str] = []
    for instruction in re.findall(r"^(?:ENV|ARG)\s+(.*)$", joined, re.MULTILINE):
        declared += re.findall(r"([A-Za-z_][A-Za-z0-9_]*)(?:=|\s|$)", instruction)

    assert declared, "the pattern found no ENV or ARG names"
    assert not [n for n in declared if re.search(r"KEY|TOKEN|SECRET|PASSWORD", n, re.I)]


def test_dockerignore_keeps_secret_files_out_even_inside_allowed_folders() -> None:
    lines = [
        line.strip()
        for line in DOCKERIGNORE.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    last_allowed = max(i for i, line in enumerate(lines) if line.startswith("!"))

    for pattern in ("**/.env*", "**/*.pem", "**/*.key"):
        assert lines.index(pattern) > last_allowed, pattern
