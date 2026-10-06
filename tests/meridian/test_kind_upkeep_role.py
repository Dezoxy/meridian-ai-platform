"""The database role of the gateway's ledger upkeep on kind (S066).

Migration 0020 refuses to run unless the role ``gateway_upkeep`` exists and is
a plain login role that is a member of no role. On kind CloudNativePG creates
it from ``infra/kind/values/platform-db.yaml``, and ``make up`` makes its
Secret from ``DATABASE_ROLES`` in ``infra/kind/common.sh``. These tests pin
that declaration, that ``make up`` makes the Secret, and that no workload of
the chart is given it: the command that uses the role is run by an operator,
not by the cluster. What the role may do is tested with the migration.
"""

import re
import subprocess
from pathlib import Path

import yaml
from chartsupport import rendered_chart
from servicesupport import REPO_ROOT

KIND_DIR = REPO_ROOT / "infra" / "kind"
COMMON_SH = KIND_DIR / "common.sh"
UP_SH = (KIND_DIR / "up.sh").read_text(encoding="utf-8")
PLATFORM_DB = yaml.safe_load((KIND_DIR / "values" / "platform-db.yaml").read_text())
ROLE = "gateway_upkeep"
SECRET = "gateway-upkeep-db"  # noqa: S105 (a Secret name, not a password)
# What a managed role of the values file may say. Every other field of
# CloudNativePG's RoleConfiguration (superuser, createdb, createrole, bypassrls,
# replication, inRoles, inherit, validUntil, comment, disablePassword) is
# absent, so the operator's defaults apply: no attribute beyond LOGIN and no
# membership, which is what the migration checks.
PLAIN_ROLE_FIELDS = {"name", "ensure", "login", "connectionLimit", "passwordSecret"}


def upkeep_role() -> dict:
    (role,) = [r for r in PLATFORM_DB["cluster"]["roles"] if r["name"] == ROLE]
    return role


def hba_lists() -> tuple[list[str], list[str]]:
    rules = PLATFORM_DB["cluster"]["postgresql"]["pg_hba"]
    (accept,) = [r for r in rules if r.startswith("hostssl meridian ") and "scram" in r]
    (refuse,) = [
        r for r in rules if r.startswith("hostssl all ") and r.endswith("reject")
    ]
    return accept.split()[2].split(","), refuse.split()[2].split(",")


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


def test_the_upkeep_role_is_declared_with_login_and_nothing_else_but_a_limit() -> None:
    role = upkeep_role()

    assert set(role) <= PLAIN_ROLE_FIELDS
    assert role["login"] is True
    assert role["ensure"] == "present"
    assert role["passwordSecret"] == {"name": SECRET}


def test_the_upkeep_role_may_hold_two_connections_and_no_more() -> None:
    role = upkeep_role()

    # The command opens one connection for milliseconds. The limit bounds what
    # a holder of the credential can hold open; it does not stop it.
    assert role["connectionLimit"] == 2


def test_the_upkeep_role_is_in_both_pg_hba_lines_of_the_meridian_roles() -> None:
    accepted, refused = hba_lists()

    assert ROLE in accepted
    assert ROLE in refused
    # A role in the first line only could reach other databases; in the second
    # only, it could not log in to meridian.
    assert set(accepted) == set(refused)


def test_the_upkeep_role_is_one_of_the_roles_make_up_makes_a_secret_for() -> None:
    script = "\n".join(
        [
            f'. "{COMMON_SH}"',
            'printf "%s\\n" "${DATABASE_ROLES[@]}"',
            f"role_secret_name {ROLE}",
        ]
    )

    done = bash(script, Path("/"))

    assert done.returncode == 0, done.stderr
    *roles, secret = done.stdout.splitlines()
    assert ROLE in roles
    assert secret == SECRET


def run_ensure_database_secrets(
    tmp_path: Path, *, secret_exists: bool
) -> tuple[subprocess.CompletedProcess[str], str]:
    """``ensure_database_secrets`` of up.sh in bash against a stub ``kctl`` that
    knows every Secret or none, and writes what ``create`` reads to a file."""
    created = tmp_path / "created.yaml"
    script = "\n".join(
        [
            "set -euo pipefail",
            f'. "{COMMON_SH}"',
            "DATABASE_HOST=platform-db-rw; DATABASE_CA_PATH=/etc/ca/ca.crt",
            "kctl() {",
            '  case "$*" in',
            f'    *"get secret"*) {"return 0" if secret_exists else "return 1"} ;;',
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


def test_make_up_creates_the_upkeep_secret_when_it_is_absent(tmp_path: Path) -> None:
    done, created = run_ensure_database_secrets(tmp_path, secret_exists=False)

    assert done.returncode == 0, done.stderr
    assert f"creating secret {SECRET}" in done.stdout
    assert f"name: {SECRET}\n" in created
    assert f"username: {ROLE}\n" in created
    assert f"postgresql://{ROLE}:" in created
    # The password goes to kubectl on stdin and never to the terminal.
    (password,) = re.findall(rf"postgresql://{ROLE}:([0-9a-f]+)@", created)
    assert password not in done.stdout + done.stderr


def test_make_up_leaves_an_existing_upkeep_secret_alone(tmp_path: Path) -> None:
    done, created = run_ensure_database_secrets(tmp_path, secret_exists=True)

    assert done.returncode == 0, done.stderr
    assert f"secret {SECRET} exists" in done.stdout
    assert created == ""


def test_no_workload_of_the_chart_is_given_the_upkeep_secret() -> None:
    documents = rendered_chart()

    rendered = yaml.dump(list(documents))
    # The check reads the whole render, so a Secret named in an env, a volume,
    # an envFrom or a Role's resourceNames would all be seen. The sweep's own
    # Secret proves the text holds role Secret names at all.
    assert "claims-sweep-db" in rendered
    assert SECRET not in rendered
    assert ROLE not in rendered
