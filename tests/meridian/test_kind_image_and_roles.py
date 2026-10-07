"""The Dockerfile's pins and the database's roles agree with the chart (S041)."""

import re

import pytest
from kindsupport import (
    COMMON_SH,
    DOCKERIGNORE,
    INGEST_ROLE,
    PLATFORM_DB,
    SEED_ROLE,
    SWEEP_ROLE,
    TOOL_SERVERS,
    UPKEEP_ROLE,
    dockerfile_instructions,
)

from meridian.platform.toolserver.server import MAX_CONCURRENT_CALLS


def test_every_from_line_of_the_dockerfile_is_pinned_by_digest() -> None:
    stages = dockerfile_instructions("FROM")

    assert len(stages) >= 2
    for stage in stages:
        assert re.search(r"@sha256:[0-9a-f]{64}\b", stage), stage


def test_the_image_runs_as_a_numeric_user_other_than_root() -> None:
    users = dockerfile_instructions("USER")

    assert users, "no USER instruction"
    last = users[-1]
    assert re.fullmatch(r"\d+(:\d+)?", last), last
    assert int(last.split(":")[0]) > 0
    # The unit starts a service by its manifest's command, not by default.
    assert not dockerfile_instructions("CMD")
    assert not dockerfile_instructions("ENTRYPOINT")


def test_dockerignore_excludes_everything_and_then_allows_a_short_list() -> None:
    lines = [
        line.strip()
        for line in DOCKERIGNORE.splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert lines[0] == "*"
    assert "!infra" not in lines
    assert not any(line.startswith(("!.", "!infra", "!docs")) for line in lines)


def test_the_database_declares_its_roles_with_login_only() -> None:
    roles = {r["name"]: r for r in PLATFORM_DB["cluster"]["roles"]}

    assert set(roles) == {
        "meridian_owner",
        "claims_api",
        "agent_runtime",
        "model_gateway",
        "policy_mcp",
        "claims_mcp",
        "knowledge_mcp",
        SWEEP_ROLE,
        UPKEEP_ROLE,
        SEED_ROLE,
        INGEST_ROLE,
    }
    for name, role in roles.items():
        assert role["login"] is True
        assert role["ensure"] == "present"
        assert not {"superuser", "createdb", "createrole"} & {
            key for key, value in role.items() if value is True
        }
        # up.sh creates this Secret before the release installs, naming it the
        # same way.
        assert role["passwordSecret"]["name"] == name.replace("_", "-") + "-db"
    (listed,) = re.findall(
        r"^readonly DATABASE_ROLES=\((.*)\)$", COMMON_SH, re.MULTILINE
    )
    assert set(listed.split()) == set(roles)
    assert """printf '%s-db' "${1//_/-}\"""" in COMMON_SH  # role_secret_name


def test_only_the_tool_server_roles_have_a_connection_limit_above_their_pool() -> None:
    limits = {
        r["name"]: r["connectionLimit"]
        for r in PLATFORM_DB["cluster"]["roles"]
        if "connectionLimit" in r
        and r["name"]
        not in (SWEEP_ROLE, UPKEEP_ROLE, SEED_ROLE, INGEST_ROLE, "claims_api")
    }

    # A tool server runs MAX_CONCURRENT_CALLS calls in worker threads, one
    # connection each, and writes a failure's audit row on one more. During a
    # rollout two pods of a server run side by side.
    assert set(limits) == {name.replace("-", "_") for name in TOOL_SERVERS}
    for name, limit in limits.items():
        assert limit >= 2 * (MAX_CONCURRENT_CALLS + 1), name


def test_the_claims_api_role_may_hold_its_threads_and_a_few_more_and_no_more() -> None:
    (role,) = [r for r in PLATFORM_DB["cluster"]["roles"] if r["name"] == "claims_api"]

    # One connection a request, in a worker thread: anyio's 40 (S070's review, M-3)
    # and ten spare for an audit row's own connection and a replacement. The
    # limit is under the shared hundred and over the threads, so it never refuses
    # a request the pod can serve.
    assert role["connectionLimit"] == 50
    assert 40 < role["connectionLimit"] < 100


def test_the_sweep_role_may_hold_a_few_connections_and_no_more() -> None:
    (role,) = [r for r in PLATFORM_DB["cluster"]["roles"] if r["name"] == SWEEP_ROLE]

    # One pod at a time (concurrencyPolicy: Forbid), one connection at a time;
    # a run by hand beside the scheduled one makes two pods. Anything near the
    # tool servers' 20 would not be a bound on a one-connection job.
    assert 2 <= role["connectionLimit"] <= 5


def test_the_sweep_role_is_in_both_pg_hba_lines_of_the_meridian_roles() -> None:
    rules = PLATFORM_DB["cluster"]["postgresql"]["pg_hba"]
    accepted = [r for r in rules if r.startswith("hostssl meridian ") and "scram" in r]
    refused = [
        r for r in rules if r.startswith("hostssl all ") and r.endswith("reject")
    ]

    (accept,) = accepted
    (refuse,) = refused
    assert SWEEP_ROLE in accept.split()[2].split(",")
    assert SWEEP_ROLE in refuse.split()[2].split(",")
    # Both lines list the same roles as the roles' own list.
    listed = {r["name"] for r in PLATFORM_DB["cluster"]["roles"]}
    assert set(accept.split()[2].split(",")) == listed
    assert set(refuse.split()[2].split(",")) == listed


@pytest.mark.parametrize("role", [SEED_ROLE, INGEST_ROLE])
def test_a_job_role_is_in_both_pg_hba_lines_and_carries_a_connection_limit_of_two(
    role: str,
) -> None:
    rules = PLATFORM_DB["cluster"]["postgresql"]["pg_hba"]
    (accept,) = [r for r in rules if r.startswith("hostssl meridian ") and "scram" in r]
    (refuse,) = [
        r for r in rules if r.startswith("hostssl all ") and r.endswith("reject")
    ]
    (declared,) = [r for r in PLATFORM_DB["cluster"]["roles"] if r["name"] == role]

    assert role in accept.split()[2].split(",")
    assert role in refuse.split()[2].split(",")
    # Two, as the upkeep role has: one Job pod runs at a time and opens one
    # connection, so a leaked credential cannot take more than two of the
    # services' shared hundred (the reviews of S063, all three).
    assert declared["connectionLimit"] == 2


def test_the_meridian_database_is_owned_by_the_owner_role_and_keeps_app() -> None:
    databases = {d["name"]: d for d in PLATFORM_DB["databases"]}

    assert databases["meridian"]["owner"] == "meridian_owner"
    # pgvector for the knowledge store (S012): the owner cannot create it.
    assert databases["meridian"]["extensions"] == [{"name": "vector"}]
    assert databases["app"] == {
        "name": "app",
        "owner": "app",
        "extensions": [{"name": "vector"}],
    }
    assert PLATFORM_DB["cluster"]["initdb"] == {"database": "app", "owner": "app"}
