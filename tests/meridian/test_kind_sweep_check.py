"""smoke.sh's sweep check, run in bash against stand-in commands (S041)."""

import re
from pathlib import Path

import pytest
from kindharness import (
    SWEEP_CREATED,
    SWEEP_FINISHED,
    SWEEP_TOLERANCE_SECONDS,
    epoch_of,
    other_job,
    run_sweep_check,
    seconds_after,
    sweep_cronjob_answer,
    sweep_job,
)
from kindsupport import (
    SMOKE_SH,
    SWEEP_PERIOD_SECONDS,
    function_body,
    requires_jq,
    sweep_cronjob,
)


def test_smoke_runs_the_sweep_check_after_the_adjuster_pages_check() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]
    body = function_body(SMOKE_SH, "check_sweep_job")

    assert calls[6] == "check_sweep"
    # Same skip rule as the tool check: only when no Meridian Deployment exists,
    # and the lookups are read-only kubectl through kctl.
    assert "$(deployed_services)" in body
    # The script reads the CronJob the manifest defines, by the one constant, and
    # the freshness rule counts in the schedule's own period.
    (name,) = re.findall(r"^readonly SWEEP_CRONJOB=(\S+)$", SMOKE_SH, re.MULTILINE)
    (period,) = re.findall(
        r"^readonly SWEEP_PERIOD_SECONDS=(\d+)$", SMOKE_SH, re.MULTILINE
    )
    assert name == sweep_cronjob()["metadata"]["name"]
    assert "${SWEEP_CRONJOB}" in body
    assert int(period) == SWEEP_PERIOD_SECONDS
    assert not re.search(r"kctl[^\n]*\b(create|apply|delete|patch|replace)\b", body)


@requires_jq
def test_the_sweep_check_skips_while_the_services_are_not_deployed(
    tmp_path: Path,
) -> None:
    lines, asked = run_sweep_check(tmp_path, deployed="")

    assert lines == [
        "SKIP  sweep: the Meridian services are not deployed (make deploy)"
    ]
    assert "cronjob" not in asked


@requires_jq
def test_the_sweep_check_fails_when_the_services_run_but_the_cronjob_does_not_exist(
    tmp_path: Path,
) -> None:
    (line,) = run_sweep_check(tmp_path, cronjob="")[0]

    assert line.startswith("FAIL  sweep: cronjob/meridian-sweep does not exist")


@requires_jq
def test_the_sweep_check_fails_when_the_cronjob_cannot_be_read(tmp_path: Path) -> None:
    (line,) = run_sweep_check(tmp_path, cronjob="FAIL")[0]

    assert line.startswith("FAIL  sweep: could not read cronjob/meridian-sweep")


@requires_jq
def test_the_sweep_check_skips_a_suspended_cronjob_with_a_line_of_its_own(
    tmp_path: Path,
) -> None:
    suspended = {"metadata": {"name": "meridian-sweep"}, "spec": {"suspend": True}}
    ok = [sweep_job("meridian-sweep-1", "2026-10-03T10:00:00Z")]

    # Even with a clock that would call the last success overdue.
    lines, asked = run_sweep_check(
        tmp_path,
        cronjob=suspended,
        jobs=ok,
        now=epoch_of("2026-10-03T10:00:00Z") + 10 * SWEEP_TOLERANCE_SECONDS,
    )

    assert lines == [
        "SKIP  sweep: cronjob/meridian-sweep is suspended (spec.suspend), "
        "so it makes no runs and none can be overdue"
    ]
    assert " exec " not in asked


@requires_jq
def test_the_sweep_check_skips_while_no_job_of_the_cronjob_has_finished(
    tmp_path: Path,
) -> None:
    other = {
        "metadata": {
            "name": "meridian-ingest-abc",
            "ownerReferences": [{"kind": "CronJob", "name": "another"}],
        },
        "status": {"conditions": [{"type": "Failed", "status": "True"}]},
    }
    jobs = [sweep_job("meridian-sweep-2", None), other]

    (line,) = run_sweep_check(tmp_path, jobs=jobs)[0]

    assert line.startswith("SKIP  sweep: no Job of cronjob/meridian-sweep has finished")


@requires_jq
def test_the_sweep_check_passes_on_the_newest_finished_job_that_succeeded(
    tmp_path: Path,
) -> None:
    jobs = [
        sweep_job("meridian-sweep-1", "2026-10-03T10:00:00Z", kind="Failed"),
        sweep_job("meridian-sweep-3", "2026-10-03T10:10:00Z"),
        sweep_job("meridian-sweep-2", "2026-10-03T10:05:00Z", kind="Failed"),
        sweep_job("meridian-sweep-4", None),  # running: not finished yet
    ]

    (line,) = run_sweep_check(tmp_path, jobs=jobs)[0]

    assert line.startswith("PASS  sweep:")
    assert "meridian-sweep-3" in line
    assert "succeeded" in line
    assert "2026-10-03T10:10:00Z" in line  # when it finished, as the API says


@requires_jq
def test_the_sweep_check_prints_the_completion_time_of_a_job_that_completed(
    tmp_path: Path,
) -> None:
    jobs = [
        sweep_job(
            "meridian-sweep-1", "2026-10-03T10:10:09Z", completed="2026-10-03T10:10:07Z"
        )
    ]

    (line,) = run_sweep_check(tmp_path, jobs=jobs)[0]

    assert line.startswith("PASS  sweep:")
    assert "2026-10-03T10:10:07Z" in line


@requires_jq
def test_the_sweep_check_fails_when_the_newest_finished_job_failed(
    tmp_path: Path,
) -> None:
    jobs = [
        sweep_job("meridian-sweep-1", "2026-10-03T10:00:00Z"),
        sweep_job(
            "meridian-sweep-2",
            "2026-10-03T10:05:00Z",
            kind="Failed",
            reason="DeadlineExceeded",
        ),
    ]

    (line,) = run_sweep_check(tmp_path, jobs=jobs)[0]

    assert line.startswith("FAIL  sweep:")
    assert "meridian-sweep-2" in line
    assert "failed" in line
    assert "2026-10-03T10:05:00Z" in line
    # A Job that hit its deadline, or whose pod never started, has no pod log:
    # the reason and `describe` are what show why.
    assert "DeadlineExceeded" in line
    assert "kubectl -n meridian describe job/meridian-sweep-2" in line
    assert "logs job/meridian-sweep-2" in line


@requires_jq
def test_the_sweep_check_says_so_when_a_failed_job_gives_no_reason(
    tmp_path: Path,
) -> None:
    jobs = [sweep_job("meridian-sweep-2", "2026-10-03T10:05:00Z", kind="Failed")]

    (line,) = run_sweep_check(tmp_path, jobs=jobs)[0]

    assert line.startswith("FAIL  sweep:")
    assert "no reason" in line


@requires_jq
def test_the_sweep_check_counts_a_job_made_by_hand_from_the_cronjob(
    tmp_path: Path,
) -> None:
    # `kubectl create job --from=cronjob/meridian-sweep` sets the same owner.
    jobs = [
        sweep_job("meridian-sweep-1", "2026-10-03T10:00:00Z"),
        sweep_job("meridian-sweep-manual", "2026-10-03T10:02:00Z", kind="Failed"),
    ]

    (line,) = run_sweep_check(tmp_path, jobs=jobs)[0]

    assert line.startswith("FAIL  sweep:")
    assert "meridian-sweep-manual" in line


@requires_jq
def test_the_sweep_check_fails_when_the_jobs_cannot_be_read(tmp_path: Path) -> None:
    (line,) = run_sweep_check(tmp_path, jobs="FAIL")[0]

    assert line.startswith("FAIL  sweep: could not read the Jobs")


@requires_jq
def test_the_sweep_check_fails_when_the_schedule_ran_on_without_a_run_finishing(
    tmp_path: Path,
) -> None:
    # Last scheduled more than three periods after the newest finished Job
    # finished, and nothing running: the schedule makes Jobs that never finish.
    scheduled = seconds_after(SWEEP_FINISHED, SWEEP_TOLERANCE_SECONDS + 1)
    cronjob = sweep_cronjob_answer(scheduled=scheduled)

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=[sweep_job("meridian-sweep-1", SWEEP_FINISHED)]
    )[0]

    assert line.startswith("FAIL  sweep: the schedule is not producing finished runs")
    assert scheduled in line
    assert SWEEP_FINISHED in line


@requires_jq
def test_the_sweep_check_passes_at_exactly_three_periods_after_the_newest_finish(
    tmp_path: Path,
) -> None:
    scheduled = seconds_after(SWEEP_FINISHED, SWEEP_TOLERANCE_SECONDS)
    cronjob = sweep_cronjob_answer(scheduled=scheduled)

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=[sweep_job("meridian-sweep-1", SWEEP_FINISHED)]
    )[0]

    assert line.startswith("PASS  sweep:")


@requires_jq
def test_the_sweep_check_passes_past_three_periods_while_a_job_is_running(
    tmp_path: Path,
) -> None:
    scheduled = seconds_after(SWEEP_FINISHED, SWEEP_TOLERANCE_SECONDS + 1)
    cronjob = sweep_cronjob_answer(scheduled=scheduled, active=1)

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=[sweep_job("meridian-sweep-1", SWEEP_FINISHED)]
    )[0]

    assert line.startswith("PASS  sweep:")


@requires_jq
def test_the_sweep_check_skips_a_cronjob_that_has_not_been_scheduled_yet(
    tmp_path: Path,
) -> None:
    # Created at 09:00:00; the newest timestamp the API holds is 600 s later:
    # fewer than three periods, so it cannot be told from a young CronJob.
    cronjob = sweep_cronjob_answer(scheduled=None)
    jobs = [other_job(seconds_after(SWEEP_CREATED, 600))]

    (line,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=jobs)[0]

    assert line.startswith("SKIP  sweep: cronjob/meridian-sweep has not been scheduled")
    assert SWEEP_CREATED in line
    assert "600 s" in line


@requires_jq
def test_the_sweep_check_says_which_clock_it_asked_for_a_cronjob_not_scheduled_yet(
    tmp_path: Path,
) -> None:
    created = epoch_of(SWEEP_CREATED)

    lines, asked = run_sweep_check(
        tmp_path,
        cronjob=sweep_cronjob_answer(scheduled=None),
        jobs=[],
        now=created + 120,
    )

    (line,) = lines
    assert line.startswith("SKIP  sweep: cronjob/meridian-sweep has not been scheduled")
    assert "120 s ago by the database's clock" in line
    assert "no server-side clock" not in line
    # The clock is the primary's `now()`, read as a whole number of seconds.
    options = re.search(r"^readonly PSQL_OPTIONS='(.*)'$", SMOKE_SH, re.M)
    assert options
    exec_psql = f"exec platform-db-1 -c postgres -- env PGOPTIONS={options.group(1)} "
    assert exec_psql + "psql -d meridian -tAc " in asked
    assert "-tAc SELECT floor(extract(epoch FROM now()))::bigint" in asked


@requires_jq
def test_the_sweep_check_fails_a_cronjob_never_scheduled_by_the_databases_clock(
    tmp_path: Path,
) -> None:
    created = epoch_of(SWEEP_CREATED)
    cronjob = sweep_cronjob_answer(scheduled=None)

    # Nothing in the API is newer than the CronJob: only the clock knows.
    (kept,) = run_sweep_check(
        tmp_path, cronjob=cronjob, now=created + SWEEP_TOLERANCE_SECONDS
    )[0]
    (late,) = run_sweep_check(
        tmp_path, cronjob=cronjob, now=created + SWEEP_TOLERANCE_SECONDS + 1
    )[0]

    assert kept.startswith("SKIP  sweep:")
    assert late.startswith(
        "FAIL  sweep: cronjob/meridian-sweep has never been scheduled"
    )
    assert f"{SWEEP_TOLERANCE_SECONDS + 1} s" in late


@requires_jq
def test_the_sweep_check_skips_at_exactly_three_periods_and_fails_one_second_after(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=None)
    at = seconds_after(SWEEP_CREATED, SWEEP_TOLERANCE_SECONDS)
    past = seconds_after(SWEEP_CREATED, SWEEP_TOLERANCE_SECONDS + 1)

    (kept,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=[other_job(at)])[0]
    (late,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=[other_job(past)])[0]

    assert kept.startswith("SKIP  sweep:")
    assert late.startswith(
        "FAIL  sweep: cronjob/meridian-sweep has never been scheduled"
    )
    assert f"{SWEEP_TOLERANCE_SECONDS + 1} s" in late


@requires_jq
def test_a_finished_job_made_by_hand_does_not_hide_a_cronjob_that_never_fired(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=None)
    jobs = [sweep_job("meridian-sweep-manual", "2026-10-03T10:00:00Z")]

    (line,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=jobs)[0]

    assert line.startswith(
        "FAIL  sweep: cronjob/meridian-sweep has never been scheduled"
    )


@requires_jq
def test_the_sweep_check_skips_while_the_first_job_is_running(tmp_path: Path) -> None:
    cronjob = sweep_cronjob_answer(active=1)

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=[sweep_job("meridian-sweep-1", None)]
    )[0]

    assert line.startswith("SKIP  sweep: no Job of cronjob/meridian-sweep has finished")
    assert "running" in line


SWEEP_FINISHED_AT = epoch_of(SWEEP_FINISHED)


def run_after_a_success(
    tmp_path: Path, *, ago: int, active: int = 0, schedule: str | None = None
) -> list[str]:
    """The sweep check when the newest finished Job (a success) finished
    ``ago`` seconds before the database's clock and the CronJob was last
    scheduled at that moment, with ``active`` Jobs running."""
    cronjob = sweep_cronjob_answer(
        scheduled=SWEEP_FINISHED, active=active, schedule=schedule
    )
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]

    return run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=SWEEP_FINISHED_AT + ago
    )[0]


@requires_jq
def test_the_sweep_check_fails_a_schedule_that_stopped_after_a_success(
    tmp_path: Path,
) -> None:
    (line,) = run_after_a_success(tmp_path, ago=SWEEP_TOLERANCE_SECONDS + 1)

    assert line.startswith("FAIL  sweep: the schedule stopped")
    assert "meridian-sweep-1" in line
    assert SWEEP_FINISHED in line
    # How long ago by the database's clock, and what the bound is.
    assert f"{SWEEP_TOLERANCE_SECONDS + 1} s" in line
    assert "database's clock" in line
    assert f"{SWEEP_TOLERANCE_SECONDS} s (three periods of 300 s)" in line
    assert "no Job is running" in line


@requires_jq
def test_the_sweep_check_passes_a_success_exactly_three_periods_old(
    tmp_path: Path,
) -> None:
    (line,) = run_after_a_success(tmp_path, ago=SWEEP_TOLERANCE_SECONDS)

    assert line.startswith("PASS  sweep:")


@requires_jq
def test_the_sweep_check_passes_an_old_success_while_a_job_is_running(
    tmp_path: Path,
) -> None:
    (line,) = run_after_a_success(tmp_path, ago=SWEEP_TOLERANCE_SECONDS * 4, active=1)

    assert line.startswith("PASS  sweep:")


@requires_jq
def test_the_sweep_check_counts_in_the_period_of_the_cronjobs_own_schedule(
    tmp_path: Path,
) -> None:
    ten_minutes = "*/10 * * * *"
    bound = 3 * 10 * 60

    (kept,) = run_after_a_success(tmp_path, ago=bound, schedule=ten_minutes)
    (late,) = run_after_a_success(tmp_path, ago=bound + 1, schedule=ten_minutes)

    assert kept.startswith("PASS  sweep:")
    assert late.startswith("FAIL  sweep: the schedule stopped")
    assert f"{bound} s (three periods of 600 s)" in late


@requires_jq
@pytest.mark.parametrize(
    "schedule", ["0 * * * *", "*/0 * * * *", "*/90 * * * *", "*/5 * * * * *", "x"]
)
def test_the_sweep_check_uses_its_own_constant_for_a_schedule_it_cannot_read(
    tmp_path: Path, schedule: str
) -> None:
    (kept,) = run_after_a_success(
        tmp_path, ago=SWEEP_TOLERANCE_SECONDS, schedule=schedule
    )
    (late,) = run_after_a_success(
        tmp_path, ago=SWEEP_TOLERANCE_SECONDS + 1, schedule=schedule
    )

    assert kept.startswith("PASS  sweep:")
    assert f"{SWEEP_TOLERANCE_SECONDS} s (three periods of 300 s)" in late


@requires_jq
def test_the_sweep_check_does_not_fail_a_cronjob_deployed_less_than_a_period_ago(
    tmp_path: Path,
) -> None:
    # A Job of an earlier CronJob of the same name is old; this one is new.
    created = seconds_after(SWEEP_FINISHED, 4 * SWEEP_TOLERANCE_SECONDS)
    cronjob = sweep_cronjob_answer(scheduled=None, created=created)
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]
    created_at = epoch_of(created)

    (young,) = run_sweep_check(
        tmp_path,
        cronjob=cronjob,
        jobs=jobs,
        now=created_at + SWEEP_PERIOD_SECONDS - 1,
    )[0]
    (grown,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=created_at + SWEEP_PERIOD_SECONDS
    )[0]

    assert young.startswith("SKIP  sweep: cronjob/meridian-sweep was deployed")
    assert "299 s ago" in young
    assert grown.startswith("FAIL  sweep: the schedule stopped")


@requires_jq
def test_the_sweep_check_still_fails_a_failed_job_whatever_the_clock_says(
    tmp_path: Path,
) -> None:
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED, kind="Failed")]

    (line,) = run_sweep_check(
        tmp_path,
        cronjob=sweep_cronjob_answer(scheduled=SWEEP_FINISHED),
        jobs=jobs,
        now=SWEEP_FINISHED_AT + 10 * SWEEP_TOLERANCE_SECONDS,
    )[0]

    assert line.startswith("FAIL  sweep: the last finished Job")


@requires_jq
@pytest.mark.parametrize("clock", ["FAIL", "", "soon", "12.5", "-3"])
def test_the_sweep_check_fails_when_the_databases_clock_cannot_be_read(
    tmp_path: Path, clock: str
) -> None:
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]

    (line,) = run_sweep_check(tmp_path, jobs=jobs, now=clock)[0]

    assert line.startswith("FAIL  sweep: could not read the database's clock")


def test_the_sweep_check_says_why_it_asks_the_database_and_not_the_laptop() -> None:
    header = SMOKE_SH.split("set -euo pipefail")[0]
    section = header.split("7. sweep:")[1].split("8. network policy:")[0]
    body = " ".join(section.replace("#", " ").split())

    assert "database's clock" in body
    assert "laptop" in body
    assert "Lease" in body
    assert "cannot be seen" not in body
    assert "no clock the script trusts" not in body
    assert "Only the API server's timestamps are compared" not in body
    (clock_sql,) = re.findall(r"^readonly SWEEP_CLOCK_SQL=(.*)$", SMOKE_SH, re.M)
    assert clock_sql.strip("'") == "SELECT floor(extract(epoch FROM now()))::bigint"


@requires_jq
@pytest.mark.parametrize(
    ("outcome", "cronjob", "jobs"),
    [
        (
            "PASS",
            sweep_cronjob_answer(),
            [sweep_job("meridian-sweep-1", SWEEP_FINISHED)],
        ),
        (
            "FAIL",
            sweep_cronjob_answer(),
            [sweep_job("meridian-sweep-1", SWEEP_FINISHED, kind="Failed")],
        ),
        (
            "FAIL",
            sweep_cronjob_answer(scheduled="2026-10-03T11:00:00Z"),
            [sweep_job("meridian-sweep-1", SWEEP_FINISHED)],
        ),
        ("SKIP", sweep_cronjob_answer(), []),
    ],
    ids=["pass", "failed-job", "stale-schedule", "skip"],
)
def test_the_sweep_check_only_reads(
    tmp_path: Path, outcome: str, cronjob: dict, jobs: list[dict]
) -> None:
    lines, asked = run_sweep_check(tmp_path, cronjob=cronjob, jobs=jobs)

    # Every path gets its verdict from `get` calls and one SELECT of the
    # database's clock.
    assert lines[0].startswith(outcome), lines
    assert asked.splitlines(), "kubectl was never asked"
    for call in asked.splitlines():
        if " exec " in call:
            assert call.endswith("-tAc SELECT floor(extract(epoch FROM now()))::bigint")
            continue
        assert " get " in call, call
        assert not re.search(r"\b(create|apply|delete|patch|replace|exec)\b", call)
