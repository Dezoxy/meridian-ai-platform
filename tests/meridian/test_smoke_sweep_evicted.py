"""The sweep verdict when the schedule's last run is not in the history (S073 K4b).

Seen on kind on 2026-10-07 (R4): the chart's CronJob kept ONE successful Job, so
a Job made by hand that succeeded evicted the schedule's own last success. The
history then held the by-hand Job and the failed scheduled Job of the evening
before; the verdict left the by-hand Job out and read the failed one as the
newest run of the schedule: a FAIL on a healthy schedule. The fixtures below are
that state, as the API returned it. When the schedule's newest finished Job
finished before the CronJob's ``lastScheduleTime`` and a Job made by hand
finished after it, the schedule's latest run is not in the history, and the
verdict is decided from ``lastScheduleTime`` alone: older than the bound, the
schedule stopped (FAIL); within it, a Job of the schedule running, or its
outcome not read (SKIP, with the reason). The harness is ``run_sweep_check``.
"""

from pathlib import Path

from kindharness import (
    SWEEP_TOLERANCE_SECONDS,
    epoch_of,
    run_sweep_check,
    sweep_cronjob_answer,
)
from kindsupport import requires_jq
from test_smoke_sweep_by_hand import (
    BY_HAND_NOTE,
    by_hand_job,
    scheduled_job,
)

LAST_SCHEDULED = "2026-10-07T03:50:00Z"
CREATED = "2026-10-06T18:09:00Z"
BY_HAND_AT = "2026-10-07T03:51:24Z"
SCHEDULES_SUCCESS_AT = "2026-10-07T03:50:03Z"
FAILED_AT = "2026-10-06T18:26:15Z"
FAILED = "meridian-sweep-29855185"
BY_HAND = "meridian-sweep-r4-by-hand"
SUCCESS = "meridian-sweep-29855750"
# The clock a minute after the by-hand Job finished, as smoke read it.
NOW = epoch_of(BY_HAND_AT) + 36


def cronjob(*, active: int = 0) -> dict:
    return sweep_cronjob_answer(
        scheduled=LAST_SCHEDULED, created=CREATED, active=active
    )


def evicted_history() -> list[dict]:
    """What the CronJob held after the by-hand success evicted the schedule's:
    the by-hand Job and yesterday's failed scheduled Job (limit 1 and 3)."""
    return [
        scheduled_job(FAILED, FAILED_AT, kind="Failed", reason="BackoffLimitExceeded"),
        by_hand_job(BY_HAND, BY_HAND_AT),
    ]


@requires_jq
def test_the_eviction_seen_on_kind_is_a_skip_that_says_why_not_a_fail(
    tmp_path: Path,
) -> None:
    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob(), jobs=evicted_history(), now=NOW
    )[0]

    assert line.startswith(
        f"SKIP  sweep: cronjob/meridian-sweep fired at {LAST_SCHEDULED}"
    )
    assert "no longer in the history" in line
    assert "was not read" in line
    assert "run smoke again after the next scheduled run" in line
    assert FAILED in line and BY_HAND in line
    assert BY_HAND_NOTE in line


@requires_jq
def test_the_same_history_with_a_stale_last_schedule_is_the_stopped_fail(
    tmp_path: Path,
) -> None:
    now = epoch_of(LAST_SCHEDULED) + SWEEP_TOLERANCE_SECONDS + 1

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob(), jobs=evicted_history(), now=now
    )[0]

    assert line.startswith("FAIL  sweep: the schedule stopped")
    assert LAST_SCHEDULED in line
    assert f"{SWEEP_TOLERANCE_SECONDS + 1} s" in line
    assert BY_HAND_NOTE in line


@requires_jq
def test_at_exactly_the_bound_the_schedule_has_not_yet_stopped(tmp_path: Path) -> None:
    now = epoch_of(LAST_SCHEDULED) + SWEEP_TOLERANCE_SECONDS

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob(), jobs=evicted_history(), now=now
    )[0]

    assert line.startswith("SKIP  sweep:")


@requires_jq
def test_a_job_of_the_schedule_running_is_a_skip_and_not_a_fail_on_the_old_one(
    tmp_path: Path,
) -> None:
    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob(active=1), jobs=evicted_history(), now=NOW
    )[0]

    assert line.startswith(
        f"SKIP  sweep: cronjob/meridian-sweep was last scheduled at {LAST_SCHEDULED}"
    )
    assert "is running" in line


@requires_jq
def test_with_the_schedules_success_still_in_the_history_the_line_passes_on_it(
    tmp_path: Path,
) -> None:
    # The limit at 3: the by-hand success did not evict the schedule's.
    jobs = [
        *evicted_history(),
        scheduled_job(SUCCESS, SCHEDULES_SUCCESS_AT),
    ]

    (line,) = run_sweep_check(tmp_path, cronjob=cronjob(), jobs=jobs, now=NOW)[0]

    assert line.startswith("PASS  sweep:")
    assert f"{SUCCESS}, succeeded at {SCHEDULES_SUCCESS_AT}" in line
    assert BY_HAND_NOTE in line and BY_HAND in line


@requires_jq
def test_a_by_hand_job_that_finished_before_the_last_schedule_does_not_hide_it(
    tmp_path: Path,
) -> None:
    # The schedule fired after the by-hand run and made no finished Job in
    # three periods: still the stale verdict, not "not read".
    last = "2026-10-07T04:10:00Z"
    now = epoch_of(last) + 60
    jobs = [
        scheduled_job(SUCCESS, SCHEDULES_SUCCESS_AT),
        by_hand_job(BY_HAND, BY_HAND_AT),
    ]

    (line,) = run_sweep_check(
        tmp_path,
        cronjob=sweep_cronjob_answer(scheduled=last, created=CREATED),
        jobs=jobs,
        now=now,
    )[0]

    assert line.startswith("FAIL  sweep: the schedule is not producing finished runs")


@requires_jq
def test_the_failure_of_the_schedules_newest_run_is_still_the_failure(
    tmp_path: Path,
) -> None:
    # The Job finished after the last schedule time: it is that run's outcome,
    # and there is nothing to excuse it, as before.
    jobs = [
        scheduled_job(FAILED, "2026-10-07T03:50:20Z", kind="Failed", reason="Oops"),
        by_hand_job(BY_HAND, BY_HAND_AT),
    ]

    (line,) = run_sweep_check(tmp_path, cronjob=cronjob(), jobs=jobs, now=NOW)[0]

    assert line.startswith("FAIL  sweep: the last finished Job of cronjob/")
    assert f"{FAILED}, failed" in line


@requires_jq
def test_a_by_hand_job_and_no_job_of_the_schedule_is_still_none_finished(
    tmp_path: Path,
) -> None:
    (line,) = run_sweep_check(
        tmp_path,
        cronjob=cronjob(),
        jobs=[by_hand_job(BY_HAND, BY_HAND_AT)],
        now=NOW,
    )[0]

    assert line.startswith("SKIP  sweep: no Job of cronjob/meridian-sweep has finished")
