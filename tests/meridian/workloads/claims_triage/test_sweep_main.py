"""The scheduled sweep as a job (S052): its settings, and ``main``, which reads
them, runs a pass and exits with a code and a log that hold no secret. What a
pass does is in ``test_sweep.py`` and ``test_sweep_failures.py``."""

import logging
import re
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from sweepsupport import (
    DEADLINE_SECONDS,
    LOGGER,
    MINUTE,
    add_checkpoints,
    add_claim,
    add_run,
    breaking,
    claim_row,
    environ_of,
)

from meridian.workloads.claims_triage import sweep
from meridian.workloads.claims_triage.lifecycle import DOCUMENTS_DEADLINE_DAYS
from meridian.workloads.claims_triage.sweep import (
    DATABASE_URL_ENV,
    DOCUMENTS_DEADLINE_ENV,
    TRIAGE_LEASE_SECONDS,
    main,
    read_settings,
)

# ── the settings ────────────────────────────────────────────────────────────
DSN = "postgresql://claims_sweep:canary-secret@db.invalid:5432/meridian"


def test_the_settings_default_the_deadline_to_the_lifecycles() -> None:
    settings = read_settings({DATABASE_URL_ENV: DSN})

    assert settings.documents_deadline_days == DOCUMENTS_DEADLINE_DAYS
    assert settings.database_url == DSN
    assert "canary-secret" not in repr(settings)


@pytest.mark.parametrize("value", ["1", "14", "365"])
def test_a_deadline_from_one_to_365_whole_days_is_taken(value: str) -> None:
    settings = read_settings({DATABASE_URL_ENV: DSN, DOCUMENTS_DEADLINE_ENV: value})

    assert settings.documents_deadline_days == int(value)


@pytest.mark.parametrize(
    "value", ["0", "366", "-1", "1.5", "14 days", "", " 14", "fourteen", "١٤", "1e1"]
)
def test_a_deadline_that_is_not_a_whole_number_of_days_from_one_to_365_is_refused(
    value: str,
) -> None:
    with pytest.raises(sweep.SettingsError) as refused:
        read_settings({DATABASE_URL_ENV: DSN, DOCUMENTS_DEADLINE_ENV: value})

    assert str(refused.value) == (
        f"{DOCUMENTS_DEADLINE_ENV} must be a whole number of days from 1 to 365"
    )


@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_database_url_is_refused_by_name(value: str | None) -> None:
    environ = {} if value is None else {DATABASE_URL_ENV: value}

    with pytest.raises(sweep.SettingsError, match=DATABASE_URL_ENV):
        read_settings(environ)


# ── main ────────────────────────────────────────────────────────────────────
# The two shapes a log record of a pass may have: an ID with a class and a
# sqlstate, and the counts.
FAILURE_LINE = re.compile(
    r"^(claim|run|thread|step) \S+: the sweep could not finish it: "
    r"\w+ \(sqlstate \w+\)$"
)
SUMMARY_LINE = re.compile(
    r"^sweep pass: (\d+) claims referred as overdue, (\d+) claims failed as not "
    r"started, (\d+) claims failed as abandoned, (\d+) runs ended, "
    r"(\d+) threads cleaned, (\d+) failures$"
)


def test_main_exits_zero_after_a_clean_pass_and_logs_one_line_with_its_counts(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    add_claim(
        fresh_database,
        "CLM-5001",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5002",
        "triaging",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_checkpoints(fresh_database, "orphan")
    caplog.set_level(logging.INFO, logger=LOGGER)

    code = main(environ_of(fresh_database))

    assert code == 0
    lines = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert lines == [
        "sweep pass: 0 claims referred as overdue, 1 claims failed as not started, "
        "1 claims failed as abandoned, 0 runs ended, 1 threads cleaned, 0 failures"
    ]


def test_main_exits_one_when_an_item_failed_and_the_others_were_done(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_claim(
        fresh_database,
        "CLM-5001",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5002",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    monkeypatch.setattr(
        sweep, "move_claim", breaking(sweep.move_claim, "CLM-5001", "claim_id")
    )

    assert main(environ_of(fresh_database)) == 1
    assert claim_row(fresh_database, "CLM-5002")[0] == "triage_failed"


def test_main_exits_one_when_the_database_cannot_be_reached_and_logs_no_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    code = main(
        {DATABASE_URL_ENV: "postgresql://claims_sweep:canary-secret@127.0.0.1:1/x"}
    )

    assert code == 1
    assert "canary-secret" not in caplog.text
    assert "127.0.0.1" not in caplog.text
    assert "OperationalError" in caplog.text


@pytest.mark.parametrize(
    ("environ", "variable"),
    [
        ({}, DATABASE_URL_ENV),
        (
            {DATABASE_URL_ENV: DSN, DOCUMENTS_DEADLINE_ENV: "canary-days"},
            DOCUMENTS_DEADLINE_ENV,
        ),
        ({DATABASE_URL_ENV: DSN, DOCUMENTS_DEADLINE_ENV: "0"}, DOCUMENTS_DEADLINE_ENV),
    ],
    ids=["no-database", "not-a-number", "out-of-range"],
)
def test_main_exits_two_naming_the_variable_before_any_connection(
    environ: dict[str, str],
    variable: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def no_connection(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a connection was opened")

    monkeypatch.setattr(sweep, "connect", no_connection)
    caplog.set_level(logging.INFO)

    code = main(environ)

    assert code == 2
    assert variable in caplog.text
    assert "canary" not in caplog.text


def test_every_log_record_of_a_pass_holds_ids_counts_classes_and_sqlstates_only(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    add_claim(
        fresh_database,
        "CLM-5001",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5002",
        "triaging",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5003",
        "documents_requested",
        age_seconds=DEADLINE_SECONDS + MINUTE,
    )
    add_run(fresh_database, "Running")
    add_checkpoints(fresh_database, "orphan")
    monkeypatch.setattr(
        sweep, "move_claim", breaking(sweep.move_claim, "CLM-5002", "claim_id")
    )
    caplog.set_level(logging.DEBUG)

    assert main(environ_of(fresh_database)) == 1

    ours = [r for r in caplog.records if r.name == LOGGER]
    assert ours
    for record in ours:
        message = record.getMessage()
        assert FAILURE_LINE.match(message) or SUMMARY_LINE.match(message), message
        assert record.exc_info is None
