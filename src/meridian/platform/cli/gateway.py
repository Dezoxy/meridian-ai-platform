"""``meridian gateway``: the upkeep of the Model Gateway's ledger (S066).

A thin command over the three functions of migration 0020, run as the database
role ``gateway_upkeep``. That role can write no table: each function holds its
rule and writes its audit row in the transaction of its change (T-25, T-47), so
this module only parses what the operator typed, calls one function and prints
counts, IDs and amounts. It never prints the connection string, and it imports
nothing of ``meridian.platform.gateway``: that package reaches a provider SDK,
which hard rule 4 keeps out of the CLI.
"""

import os
import re
import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Annotated, NoReturn

import psycopg
import typer

from meridian.platform.common.db import connect

# The upkeep's own variable: no fallback to the services' or the owner's.
UPKEEP_DATABASE_URL_ENV = "MERIDIAN_GATEWAY_UPKEEP_DATABASE_URL"
APPLICATION_NAME = "meridian-gateway-upkeep"
# The floor of gateway.close_reservation (0020): a reservation younger than this
# may be a call in flight (the gateway's own deadline for a call is 25 seconds).
FLOOR_MINUTES = 10
INT4_MAX = 2**31 - 1
BIGINT_MAX = 2**63 - 1
SLUG = re.compile(r"[a-z0-9-]{1,64}")
# ASCII digits only: `\d` would match every script's digits.
EURO = re.compile(r"[0-9]+(\.[0-9]{1,6})?")
MONTH = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])")
TOKENS_KIND = "tokens-day"
COST_KIND = "cost-month"

# What an operator reads for each refusal of a function (the SQLSTATEs in the
# header of 0020), by code: what was refused and what to do. One line each.
REFUSALS = {
    "GU001": (
        "the reason is not a slug: use 1 to 64 lower-case letters, digits and hyphens"
    ),
    "GU002": "an argument was missing: give every argument the command asks for",
    "GU101": (
        "no reservation has this attempt ID: "
        "copy it from `meridian gateway reservations`"
    ),
    "GU102": (
        "the attempt is not reserved any more: it is closed already, "
        "nothing was changed"
    ),
    "GU103": (
        "the reservation is younger than ten minutes, so it may be a call "
        "in flight: try again later"
    ),
    "GU104": (
        "a release would take a counter below zero, or its counter row is gone: "
        "close it without --release to keep the charge"
    ),
    "GU201": "the kind is not tokens-day or cost-month: give --tokens or --eur",
    "GU202": "the amount must be above zero",
    "GU203": (
        "the tenant has no counter of the current period: check the tenant's "
        "name; only a tenant that has spent in this period can be credited"
    ),
    "GU204": "the amount is larger than the counter holds: credit no more than that",
    "GU301": "the month is not the first day of a month: give --before as YYYY-MM",
    "GU302": (
        "the current month is never removed: "
        "--before can be at most the current UTC month"
    ),
    "GU303": (
        "usage rows of those months are still reserved: "
        "close each with `meridian gateway close` first"
    ),
}

app = typer.Typer(
    no_args_is_help=True,
    help="Keep the Model Gateway's ledger: close, credit and expire. Each change "
    "leaves one audit row.",
)


def _fail(message: str) -> NoReturn:
    typer.echo(f"ERROR {message}", err=True)
    raise typer.Exit(code=1)


def _refuse(code: str, detail: str = "") -> NoReturn:
    _fail(f"{code} {REFUSALS[code]}{detail}")


def _eur(micro_eur: int) -> str:
    return f"{Decimal(micro_eur).scaleb(-6):.6f}"


def _amount(kind: str, amount: int) -> str:
    return f"{amount} tokens" if kind == TOKENS_KIND else f"{_eur(amount)} EUR"


def _detail(code: str, message: str, kind: str | None) -> str:
    """The number the server's own message gives for two refusals."""
    if code == "GU204" and kind and (held := re.search(r"holds \((\d+)\)$", message)):
        return f" (the counter holds {_amount(kind, int(held[1]))})"
    if code == "GU303" and (rows := re.search(r"^expire_ledger: (\d+) usage", message)):
        return f" (count: {rows[1]})"
    return ""


def _fail_on(exc: psycopg.Error, kind: str | None) -> NoReturn:
    message = exc.diag.message_primary or ""
    code = exc.diag.sqlstate or ""
    if code in REFUSALS:
        _refuse(code, _detail(code, message, kind))
    # Only the server's own message: libpq's text can echo part of a bad DSN.
    detail = message or "no server message (connection failed?)"
    _fail(f"gateway upkeep failed ({type(exc).__name__}): {detail}")


def _upkeep[T](
    work: Callable[[psycopg.Connection], T],
    *,
    read_only: bool = False,
    kind: str | None = None,
) -> T:
    """Run ``work`` in one transaction as the upkeep role. What it returns is
    printed by the caller after the commit, so no line says a change was made
    that was not."""
    dsn = os.environ.get(UPKEEP_DATABASE_URL_ENV)
    if not dsn:
        _fail(f"{UPKEEP_DATABASE_URL_ENV} is not set")
    try:
        with connect(dsn, APPLICATION_NAME) as conn:
            conn.read_only = read_only
            return work(conn)
    except psycopg.Error as exc:
        _fail_on(exc, kind)


# ── what the operator typed, checked before the database is touched ─────────
def _slug(reason: str) -> str:
    if not SLUG.fullmatch(reason):
        raise typer.BadParameter(
            "a reason is a slug of 1 to 64 lower-case letters, digits and hyphens",
            param_hint="'--reason'",
        )
    return reason


def _month(text: str) -> date:
    found = MONTH.fullmatch(text)
    try:
        if found is None:
            raise ValueError(text)
        return date(int(found[1]), int(found[2]), 1)
    except ValueError:
        raise typer.BadParameter(
            "a month is written YYYY-MM, such as 2026-09", param_hint="'--before'"
        ) from None


def _tokens(amount: int) -> int:
    if not 0 < amount <= BIGINT_MAX:
        raise typer.BadParameter(
            f"an amount is above zero and at most {BIGINT_MAX}", param_hint="'--tokens'"
        )
    return amount


def micro_eur_from(text: str) -> int:
    """Euro to micro-euro, exactly: ASCII digits with up to six decimals, above
    zero. Never through a float; a seventh decimal is refused, not rounded."""
    micro_eur = int(Decimal(text).scaleb(6)) if EURO.fullmatch(text) else 0
    if not 0 < micro_eur <= BIGINT_MAX:
        raise typer.BadParameter(
            "an amount in euro is above zero, with at most six decimals "
            "(such as 12.5 or 0.000001)",
            param_hint="'--eur'",
        )
    return micro_eur


def _minutes(minutes: int) -> int:
    if not FLOOR_MINUTES <= minutes <= INT4_MAX:
        raise typer.BadParameter(
            f"the upkeep closes nothing younger than {FLOOR_MINUTES} minutes, "
            f"so the age is at least {FLOOR_MINUTES} minutes",
            param_hint="'--older-than'",
        )
    return minutes


# ── reservations ─────────────────────────────────────────────────────────────
LIST_RESERVED = (
    "SELECT attempt_id, tenant, deployment, "
    "floor(extract(epoch FROM now() - reserved_at) / 60)::bigint, "
    "reserved_tokens, reserved_micro_eur FROM gateway.usage "
    "WHERE state = 'reserved' "
    "AND reserved_at <= now() - make_interval(mins => %s::int) "
    "ORDER BY reserved_at, attempt_id"
)


@app.command()
def reservations(
    older_than: Annotated[
        int,
        typer.Option(
            "--older-than",
            help=f"Whole minutes, at least {FLOOR_MINUTES}: the floor below which "
            "a reservation may be a call in flight.",
        ),
    ],
) -> None:
    """List the reservations still open and older than --older-than.

    Read-only: a SELECT in a read-only transaction. It changes nothing and
    writes no audit row.
    """
    minutes = _minutes(older_than)
    rows = _upkeep(
        lambda conn: conn.execute(LIST_RESERVED, (minutes,)).fetchall(),
        read_only=True,
    )
    for attempt_id, tenant, deployment, age, tokens, micro_eur in rows:
        typer.echo(
            f"attempt {attempt_id} tenant {tenant} deployment {deployment} "
            f"age {age} min reserved {tokens} tokens {_eur(micro_eur)} EUR"
        )
    typer.echo(f"reservations: {len(rows)}")


# ── close ────────────────────────────────────────────────────────────────────
CLOSE_RESERVATION = "SELECT * FROM gateway.close_reservation(%s, %s, %s)"


@app.command()
def close(
    attempt_id: Annotated[
        uuid.UUID,
        typer.Argument(
            metavar="ATTEMPT_ID", help="The attempt ID `reservations` lists."
        ),
    ],
    reason: Annotated[
        str,
        typer.Option("--reason", help="Why: a slug, such as dead-process. Audited."),
    ],
    release: Annotated[
        bool,
        typer.Option(
            "--release",
            help="Only for a call you know was not billed: close it as released.",
        ),
    ] = False,
) -> None:
    """Close a reservation that a dead gateway process left open.

    Without --release the row is kept: the charge stays as it was reserved and
    no counter moves, because the call may have been billed. With --release the
    row is released: the charge is zero and the counters get back what was
    reserved; use it only for a call you know was not billed. Only a row older
    than ten minutes is closed, and only once. One audit row is written.
    """
    slug = _slug(reason)
    state, tokens, micro_eur = _upkeep(
        lambda conn: conn.execute(
            CLOSE_RESERVATION, (attempt_id, release, slug)
        ).fetchone()
    )
    spent = f"{tokens} tokens and {_eur(micro_eur)} EUR"
    if state == "kept":
        typer.echo(f"closed {attempt_id} as kept: the charge stays at {spent}")
    else:
        typer.echo(
            f"closed {attempt_id} as released: {spent} went back to the counters"
        )


# ── credit ───────────────────────────────────────────────────────────────────
CREDIT_TENANT = "SELECT * FROM gateway.credit_tenant(%s, %s, %s, %s)"


@app.command()
def credit(
    tenant: Annotated[
        str, typer.Argument(metavar="TENANT", help="The tenant to credit.")
    ],
    reason: Annotated[
        str, typer.Option("--reason", help="Why: a slug, such as goodwill. Audited.")
    ],
    tokens: Annotated[
        int | None,
        typer.Option("--tokens", help="Tokens to credit to today's (UTC) counter."),
    ] = None,
    eur: Annotated[
        str | None,
        typer.Option(
            "--eur",
            help="Euro to credit to this month's (UTC) counter, up to six decimals.",
        ),
    ] = None,
) -> None:
    """Credit a tenant's counter of the current period.

    Give exactly one of --tokens and --eur. A credit is a row of its own, so
    the counter stays equal to the charges less the credits, and it is never
    larger than what the counter holds. One audit row is written.
    """
    if (tokens is None) == (eur is None):
        raise typer.BadParameter("give exactly one of --tokens and --eur")
    slug = _slug(reason)
    kind, amount = (
        (TOKENS_KIND, _tokens(tokens))
        if tokens is not None
        else (COST_KIND, micro_eur_from(eur or ""))
    )
    credit_id, now_holds = _upkeep(
        lambda conn: conn.execute(
            CREDIT_TENANT, (tenant, kind, amount, slug)
        ).fetchone(),
        kind=kind,
    )
    typer.echo(f"credited {tenant}: {_amount(kind, amount)} (credit {credit_id})")
    typer.echo(f"counter {kind} now holds {_amount(kind, now_holds)}")


# ── expire ───────────────────────────────────────────────────────────────────
EXPIRE_LEDGER = "SELECT * FROM gateway.expire_ledger(%s, %s)"
# What expire_ledger would remove, by the same conditions as its DELETEs, and
# the database's own current UTC month (the function's clock, not this host's).
WOULD_REMOVE = (
    "SELECT date_trunc('month', now() AT TIME ZONE 'UTC')::date, "
    "(SELECT count(*) FROM gateway.usage WHERE month < %(before)s), "
    "(SELECT count(*) FROM gateway.budget_counters WHERE period_start < %(before)s), "
    "(SELECT count(*) FROM gateway.credits WHERE period_start < %(before)s), "
    "(SELECT count(*) FROM gateway.usage WHERE month < %(before)s "
    "AND state = 'reserved')"
)


def _counts(prefix: str, month: date, usage: int, counters: int, credits: int) -> str:
    return (
        f"{prefix} before {month:%Y-%m}: usage rows {usage}, "
        f"counter rows {counters}, credits {credits}"
    )


def _dry_run(conn: psycopg.Connection, before: date) -> list[str]:
    current, usage, counters, credits, open_rows = conn.execute(
        WOULD_REMOVE, {"before": before}
    ).fetchone()
    if before > current:
        # The function refuses it too; say so now, not after --confirm.
        _refuse("GU302")
    lines = [
        _counts("would remove", before, usage, counters, credits),
        f"still reserved in those months: {open_rows}",
    ]
    if open_rows:
        lines.append("--confirm is refused until each is closed (see: close)")
    return [*lines, "nothing removed: add --confirm to remove them"]


@app.command()
def expire(
    before: Annotated[
        str,
        typer.Option(
            "--before",
            help="YYYY-MM: the first month to keep; every month before it goes. "
            "No default.",
        ),
    ],
    reason: Annotated[
        str,
        typer.Option("--reason", help="Why: a slug, such as retention-2026. Audited."),
    ],
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Remove what the dry run lists. Without it nothing is removed.",
        ),
    ] = False,
) -> None:
    """Remove the ledger rows of whole past months: usage, counters and credits.

    Without --confirm it prints what would be removed and removes nothing. The
    month is the operator's decision: there is no default, nothing runs this on
    a schedule, and the retention period is the owner's to choose. The current
    month is never removed, and months with a reservation still open are
    refused. One audit row is written.
    """
    month = _month(before)
    slug = _slug(reason)
    if not confirm:
        for line in _upkeep(lambda conn: _dry_run(conn, month), read_only=True):
            typer.echo(line)
        return
    removed = _upkeep(
        lambda conn: conn.execute(EXPIRE_LEDGER, (month, slug)).fetchone()
    )
    typer.echo(_counts("removed", month, *removed))
