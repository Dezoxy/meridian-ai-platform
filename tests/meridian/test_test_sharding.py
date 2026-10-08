"""The shard hook of ``tests/conftest.py`` (S074): CI runs the suite in shards.

``MERIDIAN_TEST_SHARD`` (1-based) out of ``MERIDIAN_TEST_SHARDS`` keeps the
tests whose node id hashes to it. The point of the design is that the shards
are disjoint and together are exactly the whole suite: a test that ran in no
shard would be a test CI never ran. No test here collects the whole suite. The
function that decides is proved on a synthetic list of some thousands of node
ids, the hook on a small real file, and the real suite is checked on every CI
run by ``MERIDIAN_TEST_SHARD_REPORT``: each shard writes the number of tests it
collected, the number it kept and a digest of the full list, and the final job
refuses a set of reports that do not agree (``scripts/ci_python_verdict.py``).

Each run is a subprocess with the sharding variables set for it alone, and with
coverage's and make's variables taken out, so the run that executes this test
(a shard itself, under coverage, with a report of its own to write) cannot
change what a collection holds or overwrite its own report.
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

import pytest
from servicesupport import REPO_ROOT

SHARD = "MERIDIAN_TEST_SHARD"
SHARDS = "MERIDIAN_TEST_SHARDS"
REPORT = "MERIDIAN_TEST_SHARD_REPORT"
# A small, quick collection for the usage errors.
SMALL = "tests/synthetic/test_oracle.py"
USAGE_ERROR = 4
NO_TESTS = 5


def load_conftest() -> ModuleType:
    # The module the hook lives in, loaded under a name of its own: it is stdlib
    # and pytest only, so a second copy beside the one pytest loaded is harmless.
    spec = importlib.util.spec_from_file_location(
        "sharding_conftest", REPO_ROOT / "tests" / "conftest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_environment(extra: Mapping[str, str]) -> dict[str, str]:
    kept = {
        key: value
        for key, value in os.environ.items()
        if key
        not in (SHARD, SHARDS, REPORT, "COVERAGE", "PYTEST_ARGS", "COVERAGE_FILE")
        and not key.startswith(("COV_CORE", "MAKEFLAGS", "MFLAGS", "PYTEST_XDIST"))
    }
    return {**kept, **extra}


def collect(
    environment: Mapping[str, str], *paths: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *paths,
        ],
        cwd=REPO_ROOT,
        env=clean_environment(environment),
        capture_output=True,
        text=True,
        check=False,
    )


def node_ids(done: subprocess.CompletedProcess[str]) -> list[str]:
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    return [line for line in done.stdout.splitlines() if "::" in line]


def test_without_the_variables_everything_is_collected() -> None:
    assert len(node_ids(collect({}, SMALL))) > 0


@pytest.mark.parametrize(
    ("environment", "why"),
    [
        ({SHARD: "1"}, "a shard without a count"),
        ({SHARDS: "4"}, "a count without a shard"),
        ({SHARD: "0", SHARDS: "4"}, "shards are numbered from 1"),
        ({SHARD: "5", SHARDS: "4"}, "a shard above the count"),
        ({SHARD: "1", SHARDS: "0"}, "no shards"),
        ({SHARD: "x", SHARDS: "4"}, "a shard that is no number"),
        ({SHARD: "1", SHARDS: "four"}, "a count that is no number"),
        ({SHARD: "", SHARDS: "4"}, "an empty shard"),
        ({SHARD: "1", SHARDS: ""}, "an empty count"),
        ({SHARD: "-1", SHARDS: "4"}, "a negative shard"),
        ({SHARD: "1.5", SHARDS: "4"}, "a shard that is not whole"),
        ({SHARD: chr(0x661), SHARDS: "4"}, "a digit that is not ASCII"),
    ],
)
def test_a_wrong_selection_stops_the_run_as_a_usage_error(
    environment: dict[str, str], why: str
) -> None:
    done = collect(environment, SMALL)

    assert done.returncode == USAGE_ERROR, why + done.stdout + done.stderr
    assert "tests collected" not in done.stdout
    assert SHARD in done.stderr + done.stdout


def test_a_shard_that_holds_no_test_is_a_failed_run_not_a_passing_one() -> None:
    # Fewer tests than shards: the test_oracle file has more than one test, so
    # a count far above it leaves the shard empty with a very high probability;
    # the number is fixed, so this is not a flake. pytest's exit status 5 is
    # what makes the shard's job fail.
    environment = {SHARD: "99999", SHARDS: "100000"}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", SMALL],
        cwd=REPO_ROOT,
        env=clean_environment(environment),
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == NO_TESTS, done.stdout + done.stderr


def test_deselected_tests_are_reported_as_deselected() -> None:
    whole = node_ids(collect({}, SMALL))
    done = collect({SHARD: "1", SHARDS: "2"}, SMALL)
    kept = node_ids(done)

    assert 0 < len(kept) < len(whole)
    assert f"{len(whole) - len(kept)} deselected" in done.stdout


def test_the_same_test_lands_in_the_same_shard_in_every_run() -> None:
    first = node_ids(collect({SHARD: "2", SHARDS: "3"}, SMALL))
    second = node_ids(collect({SHARD: "2", SHARDS: "3"}, SMALL))

    assert first == second


def synthetic_node_ids(count: int) -> list[str]:
    # The shape of the suite's ids: a file, a test, sometimes a parameter.
    return [
        f"tests/meridian/area_{number % 83}/test_unit_{number % 211}.py"
        f"::test_case_{number}" + (f"[param-{number}]" if number % 3 else "")
        for number in range(count)
    ]


@pytest.mark.parametrize("shards", range(1, 9))
def test_the_selection_makes_disjoint_shards_that_are_the_whole_list(
    shards: int,
) -> None:
    # The function the hook decides with, on a list the size of a real
    # collection's thousands, for every count from 1 to 8. A shard that kept
    # more than its own (a comparison that is not an equality) overlaps.
    conftest = load_conftest()
    ids = synthetic_node_ids(6000)
    assert len(ids) == len(set(ids))

    chosen = [
        [i for i in ids if conftest.in_shard(i, number, shards)]
        for number in range(1, shards + 1)
    ]

    assert sum(len(part) for part in chosen) == len(ids)
    assert set().union(*chosen) == set(ids)
    for number, part in enumerate(chosen, start=1):
        assert all(conftest.shard_of(i, shards) == number for i in part)


def test_no_shard_of_four_is_empty_and_none_is_far_from_a_fair_share() -> None:
    conftest = load_conftest()
    ids = synthetic_node_ids(6000)

    sizes = [
        sum(conftest.in_shard(i, number, 4) for i in ids) for number in range(1, 5)
    ]

    assert all(sizes), sizes
    # Balance by count is the design; a shard far from a fair share would mean
    # the hash is not spreading the ids.
    assert all(0.9 * 1500 < size < 1.1 * 1500 for size in sizes), sizes


def test_the_shards_of_a_real_file_are_disjoint_and_together_are_the_file() -> None:
    # The hook itself, on one small file and three shards: the last shard is the
    # one a comparison that keeps "up to" its number would get wrong.
    whole = node_ids(collect({}, SMALL))
    shards = [
        node_ids(collect({SHARD: str(number), SHARDS: "3"}, SMALL))
        for number in (1, 2, 3)
    ]

    assert len(whole) == len(set(whole)), "a node id is collected twice"
    assert sum(len(ids) for ids in shards) == len(whole)
    assert set().union(*map(set, shards)) == set(whole)


def digest_of(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def test_the_report_holds_the_shard_the_counts_and_the_digest_of_the_full_list(
    tmp_path: Path,
) -> None:
    whole = node_ids(collect({}, SMALL))
    report = tmp_path / "shard-2.report.json"
    done = collect({SHARD: "2", SHARDS: "3", REPORT: str(report)}, SMALL)
    kept = node_ids(done)

    written = json.loads(report.read_text(encoding="utf-8"))
    assert written == {
        "shard": 2,
        "shards": 3,
        "collected": len(whole),
        "kept": len(kept),
        "digest": digest_of(whole),
    }
    assert 0 < written["kept"] < written["collected"]
    assert not list(tmp_path.glob("*.tmp")), "a temporary file was left"


def test_the_report_is_the_same_under_parallel_workers(tmp_path: Path) -> None:
    # CI runs the shard with four workers, each of which collects and selects,
    # so each writes the file. This is a real run, not a collection: xdist
    # starts no worker under --collect-only. The content is the same as a
    # single process gives, the run passes, and no temporary file is left.
    whole = node_ids(collect({}, SMALL))
    alone = tmp_path / "alone.report.json"
    kept = node_ids(collect({SHARD: "2", SHARDS: "3", REPORT: str(alone)}, SMALL))
    report = tmp_path / "shard-2.report.json"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n",
            "2",
            "-p",
            "no:cacheprovider",
            SMALL,
        ],
        cwd=REPO_ROOT,
        env=clean_environment({SHARD: "2", SHARDS: "3", REPORT: str(report)}),
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "bringing up nodes" in done.stdout
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written == json.loads(alone.read_text(encoding="utf-8"))
    assert written["digest"] == digest_of(whole)
    assert written["collected"] == len(whole)
    assert written["kept"] == len(kept)
    assert f"{len(kept)} passed" in done.stdout
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "alone.report.json",
        "shard-2.report.json",
    ]


@pytest.mark.parametrize(
    "environment",
    [
        {REPORT: "report.json"},
        {REPORT: ""},
    ],
)
def test_a_report_without_a_shard_selection_or_without_a_name_is_a_usage_error(
    environment: dict[str, str],
) -> None:
    # A report that nothing writes would be missing at the end, but the run that
    # asked for it should not spend its time first.
    done = collect(environment, SMALL)

    assert done.returncode == USAGE_ERROR, done.stdout + done.stderr
    assert REPORT in done.stderr + done.stdout


def test_a_report_that_cannot_be_written_fails_the_run(tmp_path: Path) -> None:
    report = tmp_path / "absent" / "shard-1.report.json"
    done = collect({SHARD: "1", SHARDS: "2", REPORT: str(report)}, SMALL)

    assert done.returncode not in (0, NO_TESTS), done.stdout + done.stderr


# ── an option that drops tests after the shard selection (the re-check's M-1) ──
VERDICT = REPO_ROOT / "scripts" / "ci_python_verdict.py"


def one_test_to_drop(shard_ids: list[str], whole: list[str]) -> tuple[str, str]:
    """A test of the shard that a `-k` of its name removes alone, and its name."""
    for node_id in shard_ids:
        name = node_id.split("::")[-1]
        if "[" not in name and sum(name in other for other in whole) == 1:
            return node_id, name
    raise AssertionError("no test of the shard has a name of its own")


def write_two_reports(folder: Path, second_shard_options: list[str]) -> list[str]:
    """Reports of shard 1 and shard 2 of 2; the second run gets the options.

    Returns the whole list of node ids, the total the reports must add up to.
    """
    for number in (1, 2):
        options = second_shard_options if number == 2 else []
        report = folder / f"shard-{number}.report.json"
        done = collect(
            {SHARD: str(number), SHARDS: "2", REPORT: str(report)}, *options, SMALL
        )
        assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
        (folder / f"shard-{number}.coverage").write_text("data", encoding="utf-8")
    return node_ids(collect({}, SMALL))


def run_verdict(folder: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERDICT), "coverage-files", str(folder), "--shards", "2"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_unreduced_reports_of_two_shards_pass_the_final_check(
    tmp_path: Path,
) -> None:
    whole = write_two_reports(tmp_path, [])

    done = run_verdict(tmp_path)

    assert done.returncode == 0, done.stdout + done.stderr
    assert f"{len(whole)} tests collected" in done.stdout


def test_a_k_that_drops_a_test_lowers_kept_and_the_final_check_names_the_sum(
    tmp_path: Path,
) -> None:
    whole = node_ids(collect({}, SMALL))
    share = node_ids(collect({SHARD: "2", SHARDS: "2"}, SMALL))
    _, name = one_test_to_drop(share, whole)

    write_two_reports(tmp_path, ["-k", f"not {name}"])
    written = json.loads((tmp_path / "shard-2.report.json").read_text("utf-8"))
    done = run_verdict(tmp_path)

    # The share of the shard is one higher, and the total is still the whole.
    assert written["kept"] == len(share) - 1
    assert written["collected"] == len(whole)
    assert done.returncode == 1, done.stdout + done.stderr
    assert (
        f"the shards kept {len(whole) - 1} tests in all, "
        f"and {len(whole)} were collected"
    ) in done.stdout


def test_a_deselect_that_drops_a_test_lowers_kept_and_the_final_check_refuses(
    tmp_path: Path,
) -> None:
    whole = node_ids(collect({}, SMALL))
    share = node_ids(collect({SHARD: "2", SHARDS: "2"}, SMALL))

    write_two_reports(tmp_path, ["--deselect", share[0]])
    written = json.loads((tmp_path / "shard-2.report.json").read_text("utf-8"))
    done = run_verdict(tmp_path)

    assert written["kept"] == len(share) - 1
    assert done.returncode == 1, done.stdout + done.stderr
    assert (
        f"the shards kept {len(whole) - 1} tests in all, "
        f"and {len(whole)} were collected"
    ) in done.stdout


def test_a_k_under_parallel_workers_writes_the_count_the_workers_run(
    tmp_path: Path,
) -> None:
    # The count is the items each worker holds after every deselection, which is
    # what xdist then runs: the passed tests are the report's kept.
    whole = node_ids(collect({}, SMALL))
    share = node_ids(collect({SHARD: "2", SHARDS: "3"}, SMALL))
    _, name = one_test_to_drop(share, whole)
    report = tmp_path / "shard-2.report.json"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-n",
        "2",
        "-p",
        "no:cacheprovider",
    ]
    done = subprocess.run(
        [*command, "-k", f"not {name}", SMALL],
        cwd=REPO_ROOT,
        env=clean_environment({SHARD: "2", SHARDS: "3", REPORT: str(report)}),
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["kept"] == len(share) - 1
    assert f"{len(share) - 1} passed" in done.stdout
