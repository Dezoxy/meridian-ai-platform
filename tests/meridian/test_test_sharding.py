"""The shard hook of ``tests/conftest.py`` (S074): CI runs the suite in shards.

``MERIDIAN_TEST_SHARD`` (1-based) out of ``MERIDIAN_TEST_SHARDS`` keeps the
tests whose node id hashes to it. The point of the design is that the shards
are disjoint and together are exactly the whole suite: a test that ran in no
shard would be a test CI never ran, and the required check would say so only if
something looked. The partition test below is that look, over the whole
collection and with the shard count the workflow holds.

Each run is a subprocess with the sharding variables set for it alone, and with
coverage's and make's variables taken out, so the run that executes this test
(a shard itself, under coverage) cannot change what a collection holds.
"""

import os
import subprocess
import sys
from collections.abc import Mapping

import pytest
from ciworkflowsupport import WORKFLOW
from servicesupport import REPO_ROOT

SHARD = "MERIDIAN_TEST_SHARD"
SHARDS = "MERIDIAN_TEST_SHARDS"
# A small, quick collection for the usage errors.
SMALL = "tests/synthetic/test_oracle.py"
USAGE_ERROR = 4
NO_TESTS = 5
# The one number of shards, held by the workflow (its top-level env): the
# partition below is proved for the number CI runs.
SHARD_COUNT = int(WORKFLOW["env"]["TEST_SHARD_COUNT"])


def clean_environment(extra: Mapping[str, str]) -> dict[str, str]:
    kept = {
        key: value
        for key, value in os.environ.items()
        if key not in (SHARD, SHARDS, "COVERAGE", "PYTEST_ARGS", "COVERAGE_FILE")
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


def test_the_shards_are_disjoint_and_together_are_the_whole_suite() -> None:
    # About 90 s: five collections of the suite. Its name was chosen so that its
    # hash puts it in shard 1 with four shards, not in shard 3 with the slowest
    # tests (S074); only the node id decides, and a rename moves it.
    whole = node_ids(collect({}))
    shards = [
        node_ids(collect({SHARD: str(number), SHARDS: str(SHARD_COUNT)}))
        for number in range(1, SHARD_COUNT + 1)
    ]

    assert len(whole) == len(set(whole)), "a node id is collected twice"
    seen: set[str] = set()
    for number, ids in enumerate(shards, start=1):
        assert ids, f"shard {number} is empty"
        assert not seen & set(ids), f"shard {number} repeats a test of another shard"
        seen |= set(ids)
    assert sum(len(ids) for ids in shards) == len(whole)
    assert seen == set(whole)
    # Balance by count is the design; a shard far from a fair share would mean
    # the hash is not spreading the ids.
    fair = len(whole) / SHARD_COUNT
    for number, ids in enumerate(shards, start=1):
        assert 0.8 * fair < len(ids) < 1.2 * fair, (number, len(ids), fair)
