"""``meridian gateway`` against PostgreSQL, as the role ``gateway_upkeep`` (S066).

Each command's happy path (its exact output lines, the audit row it leaves and
the role the row names), each refusal's line and exit code, and what a dry run
leaves alone. The functions' own rules are tested in
``tests/meridian/db/test_gateway_upkeep_*.py``; what needs no database is in
``test_upkeep_cli_usage.py``. The helpers that plant a ledger are in
``upkeepsupport``.
"""

import uuid
from datetime import date, timedelta

import psycopg
import pytest
from dbsupport import OWNER, UPKEEP_ROLE, DatabaseHandle
from typer.testing import CliRunner
from upkeepsupport import (
    COST_KIND,
    OTHER_TENANT,
    SERVICE,
    TENANT,
    TOKENS_KIND,
    assert_counters_reconcile,
    audit_rows,
    counters,
    ledger_snapshot,
    plant_ledger_of_a_month,
    plant_usage,
    previous_month,
    run,
    usage_row,
    utc_day,
    utc_month,
)

from meridian.platform.cli import app
from meridian.platform.cli import gateway as gateway_cli

REASON = "dead-process"
DEPLOYMENT = "aoai-sdc-gpt-4o"
runner = CliRunner()


@pytest.fixture
def db(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> DatabaseHandle:
    """A migrated database, and the command pointed at it as the upkeep role."""
    monkeypatch.setenv(
        gateway_cli.UPKEEP_DATABASE_URL_ENV, fresh_database.dsn(UPKEEP_ROLE)
    )
    return fresh_database


def refusal(db: DatabaseHandle, result, code: str, *, detail: str = "") -> None:
    """The command exited 1 with the table's one line for ``code`` on stderr and
    nothing else, and printed no password."""
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        f"ERROR {code} {gateway_cli.REFUSALS[code]}{detail}"
    ]
    assert db.passwords[UPKEEP_ROLE] not in result.output


def credit_ids(db: DatabaseHandle) -> list[uuid.UUID]:
    return [row[0] for row in run(db, OWNER, "SELECT credit_id FROM gateway.credits")]


def month_text(first: date) -> str:
    return f"{first:%Y-%m}"


# ── the connection ───────────────────────────────────────────────────────────
def test_the_command_connects_under_an_application_name_of_its_own(
    monkeypatch: pytest.MonkeyPatch, db: DatabaseHandle
) -> None:
    names: list[str] = []
    real_connect = gateway_cli.connect

    def spy(dsn: str, application_name: str) -> psycopg.Connection:
        names.append(application_name)
        return real_connect(dsn, application_name)

    monkeypatch.setattr(gateway_cli, "connect", spy)

    result = runner.invoke(app, ["gateway", "reservations", "--older-than", "10"])

    assert result.exit_code == 0, result.output
    assert names == ["meridian-gateway-upkeep"]


# ── reservations ─────────────────────────────────────────────────────────────
def test_reservations_lists_the_open_ones_older_than_the_age_oldest_first(
    db: DatabaseHandle,
) -> None:
    older = plant_usage(
        db, tenant=OTHER_TENANT, tokens=7, micro_eur=3, age=timedelta(minutes=90)
    )
    old = plant_usage(db, tokens=100, micro_eur=40, age=timedelta(minutes=30))
    plant_usage(db, age=timedelta(minutes=9))
    plant_usage(db, age=timedelta(minutes=60), state="settled")

    result = runner.invoke(app, ["gateway", "reservations", "--older-than", "10"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"attempt {older} tenant {OTHER_TENANT} deployment {DEPLOYMENT} "
        "age 90 min reserved 7 tokens 0.000003 EUR",
        f"attempt {old} tenant {TENANT} deployment {DEPLOYMENT} "
        "age 30 min reserved 100 tokens 0.000040 EUR",
        "reservations: 2",
    ]


def test_reservations_leaves_out_what_is_younger_than_the_age_it_is_given(
    db: DatabaseHandle,
) -> None:
    older = plant_usage(db, age=timedelta(minutes=90))
    plant_usage(db, age=timedelta(minutes=30))

    result = runner.invoke(app, ["gateway", "reservations", "--older-than", "45"])

    assert result.exit_code == 0, result.output
    assert [line.split()[1] for line in result.stdout.splitlines()[:-1]] == [str(older)]
    assert result.stdout.splitlines()[-1] == "reservations: 1"


def test_reservations_says_none_when_none_is_open(db: DatabaseHandle) -> None:
    result = runner.invoke(app, ["gateway", "reservations", "--older-than", "10"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["reservations: 0"]


def test_reservations_changes_nothing_and_writes_no_audit_row(
    db: DatabaseHandle,
) -> None:
    plant_usage(db, age=timedelta(minutes=30))
    before = ledger_snapshot(db)

    result = runner.invoke(app, ["gateway", "reservations", "--older-than", "10"])

    assert result.exit_code == 0, result.output
    assert ledger_snapshot(db) == before
    assert audit_rows(db) == []


# ── close ────────────────────────────────────────────────────────────────────
def test_close_keeps_the_charge_by_default_and_audits_it_under_the_role(
    db: DatabaseHandle,
) -> None:
    attempt = plant_usage(db, tokens=100, micro_eur=40)
    before = counters(db)

    result = runner.invoke(app, ["gateway", "close", str(attempt), "--reason", REASON])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"closed {attempt} as kept: the charge stays at 100 tokens and 0.000040 EUR"
    ]
    assert usage_row(db, attempt) == ("kept", 100, 40, True)
    assert counters(db) == before
    (row,) = audit_rows(db)
    assert (row["service"], row["event"], row["outcome"]) == (
        SERVICE,
        "ledger.reservation-closed",
        "kept",
    )
    assert (row["tenant"], row["reference"], row["reason"]) == (
        TENANT,
        f"attempt={attempt} tokens=100 micro_eur=40",
        REASON,
    )
    assert row["db_role"] == UPKEEP_ROLE
    assert_counters_reconcile(db)
    assert db.passwords[UPKEEP_ROLE] not in result.output


def test_close_with_release_gives_back_what_was_reserved_and_audits_it(
    db: DatabaseHandle,
) -> None:
    attempt = plant_usage(db, tokens=100, micro_eur=40)
    plant_usage(db, tokens=7, micro_eur=3, state="settled")
    day, month = utc_day(db), utc_month(db)

    result = runner.invoke(
        app, ["gateway", "close", str(attempt), "--reason", REASON, "--release"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"closed {attempt} as released: 100 tokens and 0.000040 EUR "
        "went back to the counters"
    ]
    assert usage_row(db, attempt) == ("released", 0, 0, True)
    assert counters(db) == {
        (TENANT, TOKENS_KIND, day): 7,
        (TENANT, COST_KIND, month): 3,
    }
    (row,) = audit_rows(db)
    assert (row["event"], row["outcome"], row["reference"]) == (
        "ledger.reservation-closed",
        "released",
        f"attempt={attempt} tokens=100 micro_eur=40",
    )
    assert row["db_role"] == UPKEEP_ROLE
    assert_counters_reconcile(db)


def test_close_refuses_an_attempt_that_does_not_exist(db: DatabaseHandle) -> None:
    before = ledger_snapshot(db)

    result = runner.invoke(
        app, ["gateway", "close", str(uuid.uuid4()), "--reason", REASON]
    )

    refusal(db, result, "GU101")
    assert ledger_snapshot(db) == before


def test_close_refuses_a_reservation_that_is_closed_already(db: DatabaseHandle) -> None:
    attempt = plant_usage(db)
    first = runner.invoke(app, ["gateway", "close", str(attempt), "--reason", REASON])
    assert first.exit_code == 0, first.output
    before = ledger_snapshot(db)

    result = runner.invoke(
        app, ["gateway", "close", str(attempt), "--reason", REASON, "--release"]
    )

    refusal(db, result, "GU102")
    assert ledger_snapshot(db) == before
    assert usage_row(db, attempt) == ("kept", 100, 40, True)


def test_close_refuses_a_reservation_younger_than_the_floor(db: DatabaseHandle) -> None:
    # Nine minutes: one under the floor that `reservations --older-than` enforces.
    attempt = plant_usage(db, age=timedelta(minutes=9))
    before = ledger_snapshot(db)

    result = runner.invoke(app, ["gateway", "close", str(attempt), "--reason", REASON])

    refusal(db, result, "GU103")
    assert ledger_snapshot(db) == before


def test_close_closes_a_reservation_at_the_floor_that_reservations_lists(
    db: DatabaseHandle,
) -> None:
    attempt = plant_usage(db, age=timedelta(minutes=11))
    listed = runner.invoke(app, ["gateway", "reservations", "--older-than", "10"])

    result = runner.invoke(app, ["gateway", "close", str(attempt), "--reason", REASON])

    assert str(attempt) in listed.stdout
    assert result.exit_code == 0, result.output


def test_close_with_release_refuses_when_the_counter_row_is_not_there(
    db: DatabaseHandle,
) -> None:
    attempt = plant_usage(db, counted=False)
    before = ledger_snapshot(db)

    result = runner.invoke(
        app, ["gateway", "close", str(attempt), "--reason", REASON, "--release"]
    )

    refusal(db, result, "GU104")
    assert ledger_snapshot(db) == before
    assert usage_row(db, attempt) == ("reserved", 100, 40, False)


# ── credit ───────────────────────────────────────────────────────────────────
def test_credit_tokens_lowers_the_days_counter_and_audits_it_under_the_role(
    db: DatabaseHandle,
) -> None:
    plant_usage(db, tokens=100, micro_eur=40, state="settled")

    result = runner.invoke(
        app, ["gateway", "credit", TENANT, "--tokens", "30", "--reason", "goodwill"]
    )

    assert result.exit_code == 0, result.output
    (credit_id,) = credit_ids(db)
    assert result.stdout.splitlines() == [
        f"credited {TENANT}: 30 tokens (credit {credit_id})",
        "counter tokens-day now holds 70 tokens",
    ]
    assert counters(db)[(TENANT, TOKENS_KIND, utc_day(db))] == 70
    (row,) = audit_rows(db)
    assert (row["event"], row["outcome"], row["tenant"]) == (
        "budget.credited",
        "completed",
        TENANT,
    )
    assert (row["reference"], row["reason"]) == (
        f"credit={credit_id} kind={TOKENS_KIND} amount=30 period={utc_day(db)}",
        "goodwill",
    )
    assert row["db_role"] == UPKEEP_ROLE
    assert_counters_reconcile(db)
    assert db.passwords[UPKEEP_ROLE] not in result.output


@pytest.mark.parametrize(
    ("typed", "micro_eur", "credited", "holds"),
    [
        pytest.param("1", 1_000_000, "1.000000", "4.000000", id="one-euro"),
        pytest.param("0.000001", 1, "0.000001", "4.999999", id="one-micro-euro"),
        pytest.param("1.5", 1_500_000, "1.500000", "3.500000", id="one-and-a-half"),
    ],
)
def test_credit_eur_lowers_the_months_cost_counter_by_the_exact_micro_euro(
    db: DatabaseHandle, typed: str, micro_eur: int, credited: str, holds: str
) -> None:
    plant_usage(db, tokens=1, micro_eur=5_000_000, state="settled")

    result = runner.invoke(
        app, ["gateway", "credit", TENANT, "--eur", typed, "--reason", "goodwill"]
    )

    assert result.exit_code == 0, result.output
    (credit_id,) = credit_ids(db)
    assert result.stdout.splitlines() == [
        f"credited {TENANT}: {credited} EUR (credit {credit_id})",
        f"counter cost-month now holds {holds} EUR",
    ]
    assert counters(db)[(TENANT, COST_KIND, utc_month(db))] == 5_000_000 - micro_eur
    assert_counters_reconcile(db)


def test_credit_of_exactly_what_the_counter_holds_is_accepted_and_one_more_is_not(
    db: DatabaseHandle,
) -> None:
    plant_usage(db, tokens=100, micro_eur=40, state="settled")
    before = ledger_snapshot(db)

    over = runner.invoke(
        app, ["gateway", "credit", TENANT, "--tokens", "101", "--reason", "goodwill"]
    )
    refusal(db, over, "GU204", detail=" (at most 100 tokens can be credited now)")
    assert ledger_snapshot(db) == before
    exact = runner.invoke(
        app, ["gateway", "credit", TENANT, "--tokens", "100", "--reason", "goodwill"]
    )

    assert exact.exit_code == 0, exact.output
    assert exact.stdout.splitlines()[-1] == "counter tokens-day now holds 0 tokens"
    assert counters(db)[(TENANT, TOKENS_KIND, utc_day(db))] == 0
    assert_counters_reconcile(db)


def test_credit_eur_over_the_counter_names_what_it_holds_in_euro(
    db: DatabaseHandle,
) -> None:
    plant_usage(db, tokens=100, micro_eur=40, state="settled")
    before = ledger_snapshot(db)

    result = runner.invoke(
        app, ["gateway", "credit", TENANT, "--eur", "0.000041", "--reason", "goodwill"]
    )

    refusal(db, result, "GU204", detail=" (at most 0.000040 EUR can be credited now)")
    assert ledger_snapshot(db) == before


def test_credit_refuses_what_a_reservation_in_flight_holds_and_says_what_is_left(
    db: DatabaseHandle,
) -> None:
    # The counter holds 160: 100 settled and 60 in flight. Only 100 is creditable.
    plant_usage(db, tokens=100, micro_eur=40, state="settled")
    plant_usage(db, tokens=60, micro_eur=10)
    before = ledger_snapshot(db)

    over = runner.invoke(
        app, ["gateway", "credit", TENANT, "--tokens", "101", "--reason", "goodwill"]
    )
    refusal(db, over, "GU204", detail=" (at most 100 tokens can be credited now)")
    assert ledger_snapshot(db) == before
    exact = runner.invoke(
        app, ["gateway", "credit", TENANT, "--tokens", "100", "--reason", "goodwill"]
    )

    assert exact.exit_code == 0, exact.output
    assert exact.stdout.splitlines()[-1] == "counter tokens-day now holds 60 tokens"
    assert_counters_reconcile(db)


def test_credit_refuses_a_tenant_with_no_counter_of_the_period(
    db: DatabaseHandle,
) -> None:
    plant_usage(db, tenant=OTHER_TENANT, state="settled")
    before = ledger_snapshot(db)

    result = runner.invoke(
        app, ["gateway", "credit", TENANT, "--tokens", "1", "--reason", "goodwill"]
    )

    refusal(db, result, "GU203")
    assert ledger_snapshot(db) == before


# ── expire ───────────────────────────────────────────────────────────────────
def plant_two_months(db: DatabaseHandle) -> tuple[date, date]:
    """An old month's ledger (two usage rows, three counters, two credits) and
    one settled row of the current month; return (current month, old month)."""
    current = utc_month(db)
    old = previous_month(current)
    plant_ledger_of_a_month(db, old)
    plant_usage(db, state="settled")
    return current, old


def test_expire_without_confirm_says_what_it_would_remove_and_removes_nothing(
    db: DatabaseHandle,
) -> None:
    current, _ = plant_two_months(db)
    before = ledger_snapshot(db)

    result = runner.invoke(
        app,
        ["gateway", "expire", "--before", month_text(current), "--reason", REASON],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"would remove before {month_text(current)}: usage rows 2, "
        "counter rows 3, credits 2",
        "still reserved in those months: 0",
        "nothing removed: add --confirm to remove them",
        "this is a count at this moment: rows that arrive before --confirm are "
        "removed too; a reservation that arrives makes it refuse",
    ]
    assert ledger_snapshot(db) == before
    assert audit_rows(db) == []


def test_expire_dry_run_counts_only_the_months_before_the_one_given(
    db: DatabaseHandle,
) -> None:
    _, old = plant_two_months(db)

    result = runner.invoke(
        app, ["gateway", "expire", "--before", month_text(old), "--reason", REASON]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == (
        f"would remove before {month_text(old)}: usage rows 0, "
        "counter rows 0, credits 0"
    )


def test_expire_with_confirm_removes_the_old_months_and_audits_it_under_the_role(
    db: DatabaseHandle,
) -> None:
    current, _ = plant_two_months(db)

    result = runner.invoke(
        app,
        [
            "gateway",
            "expire",
            "--before",
            month_text(current),
            "--reason",
            "retention-test",
            "--confirm",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"removed before {month_text(current)}: usage rows 2, counter rows 3, credits 2"
    ]
    assert run(db, OWNER, "SELECT count(*) FROM gateway.usage") == [(1,)]
    assert run(db, OWNER, "SELECT count(*) FROM gateway.credits") == [(0,)]
    assert {period for (_, _, period) in counters(db)} == {utc_day(db), current}
    (row,) = audit_rows(db)
    assert (row["event"], row["outcome"], row["tenant"]) == (
        "ledger.expired",
        "completed",
        None,
    )
    assert row["reference"] == (
        f"before={month_text(current)} usage=2 counters=3 credits=2"
    )
    assert (row["reason"], row["db_role"]) == ("retention-test", UPKEEP_ROLE)
    assert_counters_reconcile(db)
    assert db.passwords[UPKEEP_ROLE] not in result.output


@pytest.mark.parametrize("confirm", [False, True], ids=["dry-run", "confirm"])
def test_expire_refuses_a_month_later_than_the_current_one(
    db: DatabaseHandle, confirm: bool
) -> None:
    current, _ = plant_two_months(db)
    later = (current + timedelta(days=32)).replace(day=1)
    before = ledger_snapshot(db)
    argv = ["gateway", "expire", "--before", month_text(later), "--reason", REASON]

    result = runner.invoke(app, [*argv, "--confirm"] if confirm else argv)

    refusal(db, result, "GU302")
    assert ledger_snapshot(db) == before


def test_expire_dry_run_warns_and_confirm_refuses_while_a_reservation_is_open(
    db: DatabaseHandle,
) -> None:
    current, old = plant_two_months(db)
    open_attempt = plant_usage(db, day=old.replace(day=15), month=old)
    before = ledger_snapshot(db)
    argv = ["gateway", "expire", "--before", month_text(current), "--reason", REASON]

    dry = runner.invoke(app, argv)
    confirmed = runner.invoke(app, [*argv, "--confirm"])

    assert dry.exit_code == 0, dry.output
    assert dry.stdout.splitlines()[1:3] == [
        "still reserved in those months: 1",
        "--confirm is refused until each is closed (see: close)",
    ]
    refusal(db, confirmed, "GU303", detail=" (count: 1)")
    assert ledger_snapshot(db) == before
    assert usage_row(db, open_attempt) == ("reserved", 100, 40, False)


def test_expire_succeeds_once_the_open_reservation_is_closed_by_the_command(
    db: DatabaseHandle,
) -> None:
    current, old = plant_two_months(db)
    open_attempt = plant_usage(db, day=old.replace(day=15), month=old)
    closed = runner.invoke(
        app, ["gateway", "close", str(open_attempt), "--reason", REASON]
    )
    assert closed.exit_code == 0, closed.output

    result = runner.invoke(
        app,
        [
            "gateway",
            "expire",
            "--before",
            month_text(current),
            "--reason",
            REASON,
            "--confirm",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0].startswith(
        f"removed before {month_text(current)}: usage rows 3,"
    )
    assert usage_row(db, open_attempt) is None
    assert [row["event"] for row in audit_rows(db)] == [
        "ledger.reservation-closed",
        "ledger.expired",
    ]
    assert_counters_reconcile(db)


def test_expire_with_confirm_and_nothing_to_remove_is_one_line_and_exit_1(
    db: DatabaseHandle,
) -> None:
    plant_usage(db, state="settled")  # the current month only
    before = ledger_snapshot(db)

    result = runner.invoke(
        app,
        [
            "gateway",
            "expire",
            "--before",
            month_text(utc_month(db)),
            "--reason",
            REASON,
            "--confirm",
        ],
    )

    refusal(db, result, "GU304")
    assert ledger_snapshot(db) == before
    assert audit_rows(db) == []


def test_expire_dry_run_of_nothing_still_prints_its_counts_and_exits_0(
    db: DatabaseHandle,
) -> None:
    result = runner.invoke(
        app,
        [
            "gateway",
            "expire",
            "--before",
            month_text(utc_month(db)),
            "--reason",
            REASON,
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0].endswith(
        "usage rows 0, counter rows 0, credits 0"
    )


def test_expire_dry_run_prints_a_year_before_1000_with_four_digits(
    db: DatabaseHandle,
) -> None:
    result = runner.invoke(
        app, ["gateway", "expire", "--before", "0001-01", "--reason", REASON]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0].startswith("would remove before 0001-01:")


# ── what the ledger holds is text from a database ───────────────────────────
PLACEHOLDER = "<not an ID>"


@pytest.mark.parametrize(
    "planted",
    ["\x1b[31mred\x1b[0m", "line\nbreak", "Upper", "a b", "\x07bell", ""],
    ids=["escape", "newline", "upper-case", "space", "bell", "empty"],
)
def test_reservations_prints_a_placeholder_for_a_tenant_that_is_not_an_id(
    db: DatabaseHandle, planted: str
) -> None:
    attempt = plant_usage(db, tenant=planted, counted=False)

    result = runner.invoke(app, ["gateway", "reservations", "--older-than", "10"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"attempt {attempt} tenant {PLACEHOLDER} deployment {DEPLOYMENT} "
        "age 30 min reserved 100 tokens 0.000040 EUR",
        "reservations: 1",
    ]
    assert "\x1b" not in result.stdout
    assert "\x07" not in result.stdout


def test_reservations_prints_a_placeholder_for_a_deployment_that_is_not_an_id(
    db: DatabaseHandle,
) -> None:
    attempt = plant_usage(db, counted=False, deployment="\x1b]0;title\x07")

    result = runner.invoke(app, ["gateway", "reservations", "--older-than", "10"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == (
        f"attempt {attempt} tenant {TENANT} deployment {PLACEHOLDER} "
        "age 30 min reserved 100 tokens 0.000040 EUR"
    )
    assert "\x1b" not in result.stdout


def test_reservations_prints_a_tenant_that_is_an_id_as_it_is(
    db: DatabaseHandle,
) -> None:
    attempt = plant_usage(db, tenant="a-1", counted=False)

    result = runner.invoke(app, ["gateway", "reservations", "--older-than", "10"])

    assert f"attempt {attempt} tenant a-1 deployment {DEPLOYMENT}" in result.stdout
