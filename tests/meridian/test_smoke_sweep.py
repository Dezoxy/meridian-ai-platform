"""The smoke check of the sweep, the cases of S062's review (check 7).

``check_sweep`` in ``infra/kind/smoke.sh`` reads the CronJob ``meridian-sweep``
and its Jobs. The sweep keeps one success and the cluster removes a finished
Job a day after it finished, so a schedule that stopped days ago leaves a
CronJob with a last schedule time and no Job at all: that is a stopped
schedule, not a CronJob that has finished nothing yet. These tests use the
harness of ``test_kind_manifests.py`` (``run_sweep_check``: ``check_sweep`` in
bash against a stub ``kctl``).
"""

from pathlib import Path

from test_kind_manifests import (
    SWEEP_CREATED,
    SWEEP_FINISHED,
    SWEEP_SCHEDULED,
    SWEEP_TOLERANCE_SECONDS,
    epoch_of,
    other_job,
    requires_jq,
    run_sweep_check,
    seconds_after,
    sweep_cronjob_answer,
    sweep_job,
)

SCHEDULED_AT = epoch_of(SWEEP_SCHEDULED)


@requires_jq
def test_the_sweep_check_fails_a_schedule_that_stopped_with_no_job_left_at_all(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_SCHEDULED)

    (line,) = run_sweep_check(
        tmp_path,
        cronjob=cronjob,
        jobs=[],
        now=SCHEDULED_AT + SWEEP_TOLERANCE_SECONDS + 1,
    )[0]

    assert line.startswith("FAIL  sweep: the schedule stopped")
    assert SWEEP_SCHEDULED in line
    assert f"{SWEEP_TOLERANCE_SECONDS + 1} s" in line
    assert "database's clock" in line
    assert f"{SWEEP_TOLERANCE_SECONDS} s (three periods of 300 s)" in line
    assert "no Job of cronjob/meridian-sweep is left" in line
    assert "none is running" in line


@requires_jq
def test_the_sweep_check_skips_with_no_job_at_exactly_three_periods_after_the_schedule(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_SCHEDULED)

    (line,) = run_sweep_check(
        tmp_path,
        cronjob=cronjob,
        jobs=[],
        now=SCHEDULED_AT + SWEEP_TOLERANCE_SECONDS,
    )[0]

    assert line.startswith("SKIP  sweep: no Job of cronjob/meridian-sweep has finished")
    assert SWEEP_SCHEDULED in line


@requires_jq
def test_a_job_of_another_owner_does_not_hide_a_schedule_that_stopped(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_SCHEDULED)
    jobs = [other_job(SWEEP_SCHEDULED)]

    (line,) = run_sweep_check(
        tmp_path,
        cronjob=cronjob,
        jobs=jobs,
        now=SCHEDULED_AT + 10 * SWEEP_TOLERANCE_SECONDS,
    )[0]

    assert line.startswith("FAIL  sweep: the schedule stopped")


@requires_jq
def test_an_old_schedule_time_with_a_job_still_running_is_not_a_stopped_schedule(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_SCHEDULED, active=1)

    (line,) = run_sweep_check(
        tmp_path,
        cronjob=cronjob,
        jobs=[sweep_job("meridian-sweep-1", None)],
        now=SCHEDULED_AT + 10 * SWEEP_TOLERANCE_SECONDS,
    )[0]

    assert line.startswith("SKIP  sweep: no Job of cronjob/meridian-sweep has finished")
    assert "running" in line


@requires_jq
def test_a_cronjob_that_never_scheduled_is_still_the_never_verdict_with_no_job(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=None)
    now = epoch_of(SWEEP_CREATED) + 10 * SWEEP_TOLERANCE_SECONDS

    (line,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=[], now=now)[0]

    assert line.startswith("FAIL  sweep: cronjob/meridian-sweep has never been")


@requires_jq
def test_a_cronjob_just_deployed_with_no_job_and_no_schedule_still_skips(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=None)
    now = epoch_of(SWEEP_CREATED) + 60

    (line,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=[], now=now)[0]

    assert line.startswith("SKIP  sweep: cronjob/meridian-sweep has not been scheduled")


@requires_jq
def test_a_suspended_cronjob_with_an_old_schedule_time_and_no_job_still_skips(
    tmp_path: Path,
) -> None:
    suspended = sweep_cronjob_answer(scheduled=SWEEP_SCHEDULED)
    suspended["spec"] = {"suspend": True}

    (line,) = run_sweep_check(
        tmp_path,
        cronjob=suspended,
        jobs=[],
        now=SCHEDULED_AT + 10 * SWEEP_TOLERANCE_SECONDS,
    )[0]

    assert line.startswith("SKIP  sweep: cronjob/meridian-sweep is suspended")


@requires_jq
def test_a_success_that_finished_in_time_still_passes_beside_the_new_rule(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_FINISHED)
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]
    now = epoch_of(seconds_after(SWEEP_FINISHED, SWEEP_TOLERANCE_SECONDS))

    (line,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=jobs, now=now)[0]

    assert line.startswith("PASS  sweep:")


@requires_jq
def test_the_clock_failure_line_keeps_the_reason_psql_gave(
    tmp_path: Path,
) -> None:
    # The stub's psql writes "psql failed" on stderr when the clock query fails.
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]

    (line,) = run_sweep_check(tmp_path, jobs=jobs, now="FAIL")[0]

    assert line.startswith("FAIL  sweep: could not read the database's clock")
    assert "psql failed" in line


@requires_jq
def test_the_clock_failure_line_does_not_print_an_answer_that_is_no_clock(
    tmp_path: Path,
) -> None:
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]

    (line,) = run_sweep_check(tmp_path, jobs=jobs, now="soon")[0]

    assert line.startswith("FAIL  sweep: could not read the database's clock")
    assert "not a whole number of seconds" in line
    assert "soon" not in line
