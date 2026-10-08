"""`make demo-seed`: the refusals before the first post, the script's facts and
the Makefile's target (S098).

Run whole against stand-ins, with the rig of ``demoseedsupport.py``; the claims it
posts and what it prints are in ``test_kind_demo_seed.py``.
"""

import json
import re
from pathlib import Path

import pytest
from demoseedsupport import (
    SEED_SH,
    post_ids,
    posts_of,
    requires_tools,
    run_seed,
)
from kindsupport import KIND_DIR, REPO_ROOT
from test_kind_kubectl_bounds import raw_calls

from meridian.workloads.claims_triage.triaging import (
    BEING_TRIAGED_DETAIL,
    DIFFERENT_SUBMISSION_DETAIL,
    HAS_PROPOSAL_DETAIL,
    TRIAGE_CAP_DETAIL,
)

MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
REAL_CLAIMS = json.loads(
    (REPO_ROOT / "data" / "synthetic" / "claims.json").read_text(encoding="utf-8")
)


@requires_tools
def test_the_edge_that_does_not_answer_stops_the_run_before_the_first_post(
    tmp_path: Path,
) -> None:
    done, calls = run_seed(tmp_path, count="2", health="down")

    assert done.returncode != 0
    assert "did not answer" in done.stderr
    assert posts_of(calls) == []


@requires_tools
def test_a_post_curl_cannot_make_stops_the_run_non_zero(tmp_path: Path) -> None:
    done, calls = run_seed(tmp_path, count="3", posts={"CLM-0002": ("curlfail", "")})

    assert done.returncode != 0
    assert "could not reach" in done.stderr
    assert post_ids(calls) == ["CLM-0001", "CLM-0002"]


@requires_tools
def test_a_docker_engine_that_is_not_local_is_refused_and_nothing_is_called(
    tmp_path: Path,
) -> None:
    done, calls = run_seed(tmp_path, docker_host="tcp://203.0.113.9:2375")

    assert done.returncode != 0
    assert "not local" in done.stderr
    assert calls == []


@requires_tools
def test_no_credentials_file_and_an_unreachable_cluster_are_each_refused(
    tmp_path: Path,
) -> None:
    missing, missing_calls = run_seed(tmp_path / "a", kubeconfig=False)
    down, down_calls = run_seed(tmp_path / "b", nodes="down")

    assert missing.returncode != 0 and "make up" in missing.stderr
    assert down.returncode != 0 and "not reachable" in down.stderr
    assert missing_calls == down_calls == []


@requires_tools
def test_a_cluster_held_by_another_checkout_is_refused_unless_it_is_taken(
    tmp_path: Path,
) -> None:
    record = "s075-f1|abc1234|2026-10-06T12:00:00Z|ok"
    refused, refused_calls = run_seed(tmp_path / "a", record=record)
    taken, taken_calls = run_seed(
        tmp_path / "b", record=record, environment={"TAKE_CLUSTER": "1"}, count="2"
    )
    mine, mine_calls = run_seed(
        tmp_path / "c",
        record="s098-demo-seed|abc1234|2026-10-06T12:00:00Z|ok",
        count="2",
    )

    assert refused.returncode != 0
    assert (
        "s075-f1" in refused.stderr
        and "TAKE_CLUSTER=1 make demo-seed" in refused.stderr
    )
    assert refused_calls == []
    assert taken.returncode == 0, taken.stderr
    assert mine.returncode == 0, mine.stderr
    assert len(post_ids(taken_calls)) == len(post_ids(mine_calls)) == 2


@requires_tools
def test_the_seeding_only_reads_the_cluster_it_never_writes_the_holders_record(
    tmp_path: Path,
) -> None:
    done, _ = run_seed(tmp_path, count="1")

    assert done.returncode == 0, done.stdout + done.stderr
    calls = (tmp_path / "kubectl-calls").read_text()
    assert " get nodes" in calls and " get configmap meridian-cluster-holder" in calls
    for changing in (" apply", " create", " delete", " patch", " replace", " label"):
        assert changing not in calls


@requires_tools
@pytest.mark.parametrize(
    ("available", "allowed"), [(2499, False), (2500, True), (2501, True), (100, False)]
)
def test_under_2500_mb_available_stops_the_run_and_prints_the_figure(
    tmp_path: Path, available: int, allowed: bool
) -> None:
    done, calls = run_seed(tmp_path, available_mb=available, count="1")

    assert (done.returncode == 0) is allowed, done.stdout + done.stderr
    if not allowed:
        assert f"{available} MB" in done.stderr
        assert "2500 MB" in done.stderr
        assert calls == []


@requires_tools
def test_a_missing_meminfo_is_said_and_does_not_stop_the_run(tmp_path: Path) -> None:
    done, calls = run_seed(
        tmp_path, count="1", environment={"MEMINFO_FILE": str(tmp_path / "absent")}
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "not checked" in done.stdout
    assert post_ids(calls) == ["CLM-0001"]


@requires_tools
@pytest.mark.parametrize(
    "names",
    [
        "meridian-control-plane\nmeridian-pytest-db\n",
        "agent-s098-pytest-redis\n",
        "meridian-pytest-db-s099\nmeridian-pytest-redis-s099\n",
    ],
)
def test_a_running_test_database_container_stops_the_run(
    tmp_path: Path, names: str
) -> None:
    done, calls = run_seed(tmp_path, docker_names=names, count="1")

    assert done.returncode != 0
    assert "test database" in done.stderr
    assert "pytest" in done.stderr
    assert calls == []


@requires_tools
def test_other_containers_do_not_stop_the_run(tmp_path: Path) -> None:
    done, _ = run_seed(
        tmp_path,
        docker_names="meridian-control-plane\nmeridian-worker\nredis-cache\n",
        count="1",
    )

    assert done.returncode == 0, done.stdout + done.stderr


# ── the script, the data it reads and the Makefile ───────────────────────────


def seed_constant(name: str) -> str:
    (value,) = re.findall(rf'^readonly {name}="(.*)"$', SEED_SH, re.MULTILINE)
    return value


def test_the_script_says_the_409_sentences_the_claims_api_gives() -> None:
    assert seed_constant("ALREADY_TRIAGED") == HAS_PROPOSAL_DETAIL
    assert seed_constant("BEING_TRIAGED") == BEING_TRIAGED_DETAIL
    assert seed_constant("DIFFERENT_SUBMISSION") == DIFFERENT_SUBMISSION_DETAIL
    assert seed_constant("TRIAGE_CAP") == TRIAGE_CAP_DETAIL


def test_the_script_knows_the_47_claims_and_the_first_40_are_the_golden_set() -> None:
    assert re.search(r"^readonly MAX_CLAIMS=47$", SEED_SH, re.MULTILINE)
    assert re.search(r"^readonly DEFAULT_COUNT=40$", SEED_SH, re.MULTILINE)
    ids = [claim["claim_id"] for claim in REAL_CLAIMS]
    assert len(ids) == 47
    assert ids == sorted(ids)
    assert ids[:40] == [f"CLM-{number:04d}" for number in range(1, 41)]


def test_the_script_is_executable_and_runs_no_kubectl_or_helm_of_its_own() -> None:
    assert (KIND_DIR / "demo-seed.sh").stat().st_mode & 0o111
    assert [name for name, _ in raw_calls() if name == "demo-seed.sh"] == []


def test_the_script_reads_the_cluster_and_posts_through_the_api_only() -> None:
    code = "\n".join(
        line for line in SEED_SH.splitlines() if not line.lstrip().startswith("#")
    )

    for changing in (
        "apply",
        "create",
        "delete",
        "patch",
        "exec",
        "psql",
        "port-forward",
    ):
        assert not re.search(rf"\bkctl\b[^\n]*\b{changing}\b", code), changing
    assert "record_cluster_holder" not in code
    assert 'check_cluster_holder "make demo-seed"' in code


def test_the_makefile_has_the_target_with_its_defaults_and_its_help_line() -> None:
    phony = next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    ).split()
    (help_line,) = re.findall(r"^## demo-seed\s+(.+)$", MAKEFILE, re.MULTILINE)

    assert "demo-seed" in phony
    assert re.search(
        r"^## demo-seed {7}\S", MAKEFILE, re.MULTILINE
    )  # make help's column
    assert re.search(r"^COUNT\s+\?= 40$", MAKEFILE, re.MULTILINE)
    assert re.search(r"^PACE_SECONDS\s+\?= 2$", MAKEFILE, re.MULTILINE)
    # No dependency: it does not deploy, build or touch the cluster first.
    assert "\ndemo-seed:\n\t" in MAKEFILE
    recipe = MAKEFILE.split("\ndemo-seed:\n", 1)[1].split("\n\n", 1)[0]
    assert "infra/kind/demo-seed.sh" in recipe
    assert 'COUNT="$(COUNT)"' in recipe and 'PACE_SECONDS="$(PACE_SECONDS)"' in recipe
    for words in ("kind only", "synthetic", "simulated", "costs nothing", "COUNT"):
        assert words in help_line, words
