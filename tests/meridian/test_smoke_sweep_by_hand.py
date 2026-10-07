"""A Job made by hand does not stand for the schedule's (check 7, S073 K4).

``kubectl create job --from=cronjob/meridian-sweep`` makes a Job the CronJob
owns, so the sweep's verdict used to read a recent one as the schedule's own
success. Seen on kind (S073, R0): a Job made by hand carries the annotation
``cronjob.kubernetes.io/instantiate: manual``, and a scheduled one carries
``batch.kubernetes.io/cronjob-scheduled-timestamp`` instead. The rule these
tests pin: a Job is left out of the verdict only when it carries the first and
not the second; a Job with the second (the positive fact) or with neither (an
older cluster's) is the schedule's, and the line says so for the second case.
The harness is ``run_sweep_check`` of ``kindharness.py``.
"""

from pathlib import Path

from kindharness import (
    SWEEP_FINISHED,
    SWEEP_TOLERANCE_SECONDS,
    epoch_of,
    run_sweep_check,
    seconds_after,
    sweep_cronjob_answer,
    sweep_job,
)
from kindsupport import requires_jq

SCHEDULED_KEY = "batch.kubernetes.io/cronjob-scheduled-timestamp"
MANUAL_KEY = "cronjob.kubernetes.io/instantiate"
BY_HAND_NOTE = "was made by hand"
NOT_THE_SCHEDULES = "a by-hand run is not a run of the schedule"
ABSENT_NOTE = "the annotation was absent"


def annotated(job: dict, annotations: dict[str, str]) -> dict:
    job["metadata"]["annotations"] = annotations
    return job


def scheduled_job(name: str, finished: str, **options: str) -> dict:
    return annotated(sweep_job(name, finished, **options), {SCHEDULED_KEY: finished})


def by_hand_job(name: str, finished: str, **options: str) -> dict:
    return annotated(sweep_job(name, finished, **options), {MANUAL_KEY: "manual"})


def later(seconds: int) -> str:
    return seconds_after(SWEEP_FINISHED, seconds)


@requires_jq
def test_a_recent_by_hand_success_does_not_hide_a_schedule_that_makes_no_finished_runs(
    tmp_path: Path,
) -> None:
    by_hand_at = later(SWEEP_TOLERANCE_SECONDS + 30)
    cronjob = sweep_cronjob_answer(scheduled=later(SWEEP_TOLERANCE_SECONDS + 60))
    jobs = [
        scheduled_job("meridian-sweep-1", SWEEP_FINISHED),
        by_hand_job("meridian-sweep-by-hand-1", by_hand_at),
    ]

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=epoch_of(by_hand_at) + 60
    )[0]

    assert line.startswith("FAIL  sweep: the schedule is not producing finished runs")
    assert "meridian-sweep-by-hand-1" in line
    assert BY_HAND_NOTE in line and NOT_THE_SCHEDULES in line


@requires_jq
def test_a_recent_by_hand_success_does_not_hide_a_schedule_that_stopped(
    tmp_path: Path,
) -> None:
    by_hand_at = later(10 * SWEEP_TOLERANCE_SECONDS)
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_FINISHED)
    jobs = [
        scheduled_job("meridian-sweep-1", SWEEP_FINISHED),
        by_hand_job("meridian-sweep-by-hand-1", by_hand_at),
    ]

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=epoch_of(by_hand_at) + 60
    )[0]

    assert line.startswith("FAIL  sweep: the schedule stopped")
    assert "meridian-sweep-1," in line  # the schedule's own last Job is the one named
    assert BY_HAND_NOTE in line


@requires_jq
def test_a_by_hand_success_beside_no_scheduled_job_is_not_a_scheduled_success(
    tmp_path: Path,
) -> None:
    by_hand_at = later(60)
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_FINISHED)
    jobs = [by_hand_job("meridian-sweep-by-hand-1", by_hand_at)]

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=epoch_of(by_hand_at) + 60
    )[0]

    assert line.startswith("SKIP  sweep: no Job of cronjob/meridian-sweep has finished")
    assert BY_HAND_NOTE in line


@requires_jq
def test_a_by_hand_failure_does_not_fail_a_schedule_that_is_healthy(
    tmp_path: Path,
) -> None:
    failed_at = later(60)
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_FINISHED)
    jobs = [
        scheduled_job("meridian-sweep-1", SWEEP_FINISHED),
        by_hand_job(
            "meridian-sweep-by-hand-1",
            failed_at,
            kind="Failed",
            reason="BackoffLimitExceeded",
        ),
    ]

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=epoch_of(failed_at) + 60
    )[0]

    assert line.startswith("PASS  sweep:")
    assert "meridian-sweep-1" in line and SWEEP_FINISHED in line
    assert "meridian-sweep-by-hand-1" in line
    assert BY_HAND_NOTE in line and NOT_THE_SCHEDULES in line


@requires_jq
def test_a_scheduled_failure_still_fails_beside_a_by_hand_success(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_FINISHED)
    jobs = [
        scheduled_job("meridian-sweep-1", SWEEP_FINISHED, kind="Failed", reason="Oops"),
        by_hand_job("meridian-sweep-by-hand-1", later(60)),
    ]

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=epoch_of(later(60)) + 60
    )[0]

    assert line.startswith("FAIL  sweep: the last finished Job of cronjob/")
    assert "meridian-sweep-1, failed" in line and "Oops" in line


@requires_jq
def test_an_older_by_hand_job_than_the_schedules_is_not_mentioned(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=later(300))
    jobs = [
        by_hand_job("meridian-sweep-by-hand-1", SWEEP_FINISHED),
        scheduled_job("meridian-sweep-2", later(300)),
    ]

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=epoch_of(later(360))
    )[0]

    assert line.startswith("PASS  sweep:")
    assert "meridian-sweep-2" in line
    assert "by hand" not in line and ABSENT_NOTE not in line


@requires_jq
def test_a_scheduled_success_alone_carries_no_note_of_any_kind(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=later(300))
    jobs = [scheduled_job("meridian-sweep-2", later(300))]

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=epoch_of(later(360))
    )[0]

    assert line.startswith("PASS  sweep:")
    assert BY_HAND_NOTE not in line and ABSENT_NOTE not in line


@requires_jq
def test_a_job_with_neither_annotation_is_read_as_the_schedules_and_the_line_says_so(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_FINISHED)
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=epoch_of(SWEEP_FINISHED) + 60
    )[0]

    assert line.startswith("PASS  sweep:")
    assert "meridian-sweep-1, succeeded at " + SWEEP_FINISHED in line
    assert ABSENT_NOTE in line
    assert BY_HAND_NOTE not in line


@requires_jq
def test_a_job_with_neither_annotation_that_failed_still_fails_as_it_did(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_FINISHED)
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED, kind="Failed", reason="X")]

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=epoch_of(SWEEP_FINISHED) + 60
    )[0]

    assert line.startswith("FAIL  sweep: the last finished Job of cronjob/")
    assert ABSENT_NOTE in line


@requires_jq
def test_a_job_with_the_scheduled_timestamp_counts_even_with_the_manual_one_too(
    tmp_path: Path,
) -> None:
    # The scheduled timestamp is the positive fact: it decides.
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_FINISHED)
    both = annotated(
        sweep_job("meridian-sweep-1", SWEEP_FINISHED),
        {SCHEDULED_KEY: SWEEP_FINISHED, MANUAL_KEY: "manual"},
    )

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=[both], now=epoch_of(SWEEP_FINISHED) + 60
    )[0]

    assert line.startswith("PASS  sweep:")
    assert BY_HAND_NOTE not in line and ABSENT_NOTE not in line


@requires_jq
def test_a_job_with_a_manual_value_that_is_not_manual_is_not_left_out(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_FINISHED)
    odd = annotated(
        sweep_job("meridian-sweep-1", SWEEP_FINISHED), {MANUAL_KEY: "automatic"}
    )

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=[odd], now=epoch_of(SWEEP_FINISHED) + 60
    )[0]

    assert line.startswith("PASS  sweep:")
    assert ABSENT_NOTE in line
