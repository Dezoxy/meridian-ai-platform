"""The database roles of the seed and the ingestion Jobs on kind (S063).

Migration 0022 refuses to run unless ``policy_seed`` and ``knowledge_ingest``
exist and are plain login roles that are members of no role. On kind
CloudNativePG creates them from ``infra/kind/values/platform-db.yaml``, and
``make up`` makes their Secrets from ``DATABASE_ROLES`` in
``infra/kind/common.sh``: ``up.sh`` and ``deploy.sh`` follow that one list, and
these tests show it. The chart gives each Job its own role's Secret (the
manifests' tests); what each role may do is tested with the migration.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml
from chartsupport import rendered_chart
from servicesupport import REPO_ROOT

KIND_DIR = REPO_ROOT / "infra" / "kind"
COMMON_SH = KIND_DIR / "common.sh"
UP_SH = (KIND_DIR / "up.sh").read_text(encoding="utf-8")
DEPLOY_SH = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
PLATFORM_DB = yaml.safe_load((KIND_DIR / "values" / "platform-db.yaml").read_text())
# Each role, its Secret and the Job that holds it.
JOB_ROLES = {
    "policy_seed": ("policy-seed-db", "meridian-seed"),
    "knowledge_ingest": ("knowledge-ingest-db", "meridian-ingest"),
}
# What a managed role of the values file may say: no field that would give the
# role an attribute or a membership, which the migration refuses. The limit on
# connections is the one field it may add (the gateway's upkeep role has it too).
PLAIN_ROLE_FIELDS = {"name", "ensure", "login", "connectionLimit", "passwordSecret"}


def declared(role: str) -> dict:
    (found,) = [r for r in PLATFORM_DB["cluster"]["roles"] if r["name"] == role]
    return found


def function_text(script: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", script, re.MULTILINE | re.DOTALL)
    assert match, f"no function {name}"
    return match.group(0)


def bash(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize("role", JOB_ROLES)
def test_a_job_role_is_declared_with_login_and_nothing_else(role: str) -> None:
    secret, _ = JOB_ROLES[role]
    declaration = declared(role)

    assert set(declaration) == PLAIN_ROLE_FIELDS
    assert declaration["login"] is True
    assert declaration["ensure"] == "present"
    assert declaration["passwordSecret"] == {"name": secret}


@pytest.mark.parametrize("role", JOB_ROLES)
def test_a_job_role_may_hold_two_connections_and_no_more(role: str) -> None:
    # One Job pod runs at a time and opens one connection; the limit is that one
    # and a spare, as the upkeep role's is. A leaked credential cannot take more
    # than two of the services' shared hundred.
    limit = declared(role)["connectionLimit"]

    assert limit == 2
    assert limit == declared("gateway_upkeep")["connectionLimit"]


@pytest.mark.parametrize("job", [name for _, name in JOB_ROLES.values()])
def test_each_job_runs_one_pod_at_a_time_which_is_what_the_limit_assumes(
    job: str,
) -> None:
    (rendered,) = [
        d
        for d in rendered_chart()
        if d["kind"] == "Job" and d["metadata"]["name"].startswith(f"{job}-")
    ]

    # Neither field is set, so the Job runs one pod, and a retry (a new pod) only
    # after the failed one has ended.
    assert "parallelism" not in rendered["spec"]
    assert "completions" not in rendered["spec"]


@pytest.mark.parametrize("role", JOB_ROLES)
def test_a_job_role_is_one_of_the_roles_make_up_makes_a_secret_for(
    role: str, tmp_path: Path
) -> None:
    secret, _ = JOB_ROLES[role]
    script = "\n".join(
        [
            f'. "{COMMON_SH}"',
            'printf "%s\\n" "${DATABASE_ROLES[@]}"',
            f"role_secret_name {role}",
        ]
    )

    done = bash(script, tmp_path)

    assert done.returncode == 0, done.stderr
    *roles, name = done.stdout.splitlines()
    assert role in roles
    assert name == secret


def test_up_and_deploy_walk_the_one_list_of_roles() -> None:
    assert 'for role in "${DATABASE_ROLES[@]}"' in function_text(
        UP_SH, "ensure_database_secrets"
    )
    assert 'for role in "${DATABASE_ROLES[@]}"' in DEPLOY_SH
    # The reconciled check of `make up` and `make deploy` is common.sh's, over
    # the same list.
    assert "${DATABASE_ROLES[@]}" in function_text(
        COMMON_SH.read_text(encoding="utf-8"), "database_roles_reconciled"
    )


def run_ensure_database_secrets(
    tmp_path: Path, *, known: set[str]
) -> tuple[subprocess.CompletedProcess[str], str]:
    """``ensure_database_secrets`` of up.sh in bash against a stub ``kctl`` that
    knows the Secrets in ``known`` and no other, and writes what ``create`` reads
    to a file."""
    created = tmp_path / "created.yaml"
    script = "\n".join(
        [
            "set -euo pipefail",
            f'. "{COMMON_SH}"',
            "DATABASE_HOST=platform-db-rw; DATABASE_CA_PATH=/etc/ca/ca.crt",
            "kctl() {",
            '  case "$*" in',
            *(f'    *"get secret {name}") return 0 ;;' for name in sorted(known)),
            '    *"get secret"*) return 1 ;;',
            f'    *"create -f -"*) cat >>"{created}" ;;',
            '    *) echo "unexpected kctl $*" >&2; return 1 ;;',
            "  esac",
            "}",
            function_text(UP_SH, "ensure_database_secrets"),
            "ensure_database_secrets",
        ]
    )
    done = bash(script, tmp_path)
    return done, created.read_text(encoding="utf-8") if created.exists() else ""


@pytest.mark.parametrize("role", JOB_ROLES)
def test_make_up_creates_a_job_roles_secret_when_it_is_absent(
    role: str, tmp_path: Path
) -> None:
    secret, _ = JOB_ROLES[role]

    done, created = run_ensure_database_secrets(tmp_path, known=set())

    assert done.returncode == 0, done.stderr
    assert f"creating secret {secret}" in done.stdout
    assert f"name: {secret}\n" in created
    assert f"username: {role}\n" in created
    (password,) = re.findall(rf"postgresql://{role}:([0-9a-f]+)@", created)
    # The password goes to kubectl on stdin and never to the terminal.
    assert password not in done.stdout + done.stderr


def test_a_second_make_up_on_a_cluster_with_the_other_roles_creates_only_the_two(
    tmp_path: Path,
) -> None:
    existing = {
        "meridian-owner-db",
        "claims-api-db",
        "agent-runtime-db",
        "model-gateway-db",
        "policy-mcp-db",
        "claims-mcp-db",
        "knowledge-mcp-db",
        "claims-sweep-db",
        "gateway-upkeep-db",
    }

    done, created = run_ensure_database_secrets(tmp_path, known=existing)

    assert done.returncode == 0, done.stderr
    assert re.findall(r"creating secret (\S+)$", done.stdout, re.MULTILINE) == [
        "policy-seed-db",
        "knowledge-ingest-db",
    ]
    assert re.findall(r"^  name: (\S+)$", created, re.MULTILINE) == [
        "policy-seed-db",
        "knowledge-ingest-db",
    ]


@pytest.mark.parametrize("role", JOB_ROLES)
def test_a_job_pod_keeps_the_label_the_databases_ingress_admits(role: str) -> None:
    _, job_name = JOB_ROLES[role]
    documents = list(rendered_chart())
    (job,) = [
        d
        for d in documents
        if d["kind"] == "Job" and d["metadata"]["name"].startswith(f"{job_name}-")
    ]
    labels = job["spec"]["template"]["metadata"]["labels"]
    policy = yaml.safe_load(
        (KIND_DIR / "manifests" / "platform-db-networkpolicy.yaml").read_text()
    )
    (admitted,) = [
        rule["from"][0]["podSelector"]["matchLabels"]
        for rule in policy["spec"]["ingress"]
        if rule.get("ports", [{}])[0].get("port") == 5432
    ]

    assert admitted.items() <= labels.items()
    assert labels["app.kubernetes.io/name"] == job_name
