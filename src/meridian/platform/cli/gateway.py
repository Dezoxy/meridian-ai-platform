"""``meridian gateway``: the upkeep of the Model Gateway's ledger (S066).

A thin command over the functions of migrations 0020, 0028 and 0030
(``expire-audit`` calls the audit expiry and its count, ``expire`` the ledger's
expiry in batches), run as the database role
``gateway_upkeep``. That role can write no table: each function holds its
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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, NoReturn

import psycopg
import typer

from meridian.platform.common.db import connect
from meridian.platform.registry.models import ENTITY_ID_PATTERN

# The upkeep's own variable: no fallback to the services' or the owner's.
UPKEEP_DATABASE_URL_ENV = "MERIDIAN_GATEWAY_UPKEEP_DATABASE_URL"
APPLICATION_NAME = "meridian-gateway-upkeep"
# The floor of gateway.close_reservation (0020): a reservation younger than this
# may be a call in flight (the gateway's own deadline for a call is 25 seconds).
FLOOR_MINUTES = 10
INT4_MAX = 2**31 - 1
BIGINT_MAX = 2**63 - 1
SLUG = re.compile(r"[a-z0-9-]{1,64}")
# The registry's pattern for a tenant's or a deployment's ID (a tenant is checked
# against it before the database is touched, and the ledger's text is printed only
# when it matches). `fullmatch`: its `$` would let a trailing newline through.
ENTITY_ID = re.compile(ENTITY_ID_PATTERN)
# ASCII digits only: `\d` would match every script's digits, and `int()` takes
# underscores, a sign and spaces. At most 19 digits keeps `int()` bounded.
TOKEN_AMOUNT = re.compile(r"[0-9]{1,19}")
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
    "GU204": (
        "the amount is larger than the counter holds beyond open reservations: "
        "credit no more than that, or wait until they close"
    ),
    "GU301": "the month is not the first day of a month: give --before as YYYY-MM",
    "GU302": (
        "the current month is never removed: "
        "--before can be at most the current UTC month"
    ),
    "GU303": (
        "usage rows of those months are still reserved: "
        "close each with `meridian gateway close` first"
    ),
    "GU304": (
        "there is nothing to remove before that month: "
        "run without --confirm to see the counts"
    ),
    "GU401": (
        "the cutoff is in the future: --before is at most now, and nothing "
        "newer than it is removed"
    ),
    "GU402": "the limit is from 1 to 10000 rows a batch: give --limit in that range",
    "GU305": "the limit is from 100 to 10000 rows a batch: give --limit in that range",
    "GU306": (
        "usage rows of those months are held by another session, or changed "
        "during the call: nothing was changed by this call, so run it again"
    ),
}
# What the dry run prints for a tenant or deployment that is not an ID: the ledger
# is text from a database, and an escape sequence in it must not reach a terminal.
NOT_AN_ID = "<not an ID>"
DRY_RUN_NOTE = (
    "this is a count at this moment: rows that arrive before --confirm are "
    "removed too; a reservation that arrives makes it refuse"
)

# No locals in a traceback, as the root app has it: they could hold a connection
# string. (Typer's default is False; it is set here so a change of it is not one.)
app = typer.Typer(
    no_args_is_help=True,
    help="Keep the Model Gateway's ledger: close, credit and expire. Each change "
    "leaves one audit row.",
    pretty_exceptions_show_locals=False,
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
    if code == "GU204" and kind and (left := re.search(r"\((\d+)\)$", message)):
        return f" (at most {_amount(kind, int(left[1]))} can be credited now)"
    if code == "GU303" and (
        rows := re.search(r"^expire_ledger_batch: (\d+) usage", message)
    ):
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


QUERY_CANCELED = "57014"


class CountCancelled(Exception):
    """A count that the statement timeout cancelled (only raised for a caller
    that asked for it: ``_upkeep(..., on_cancel=True)``)."""


def _upkeep[T](
    work: Callable[[psycopg.Connection], T],
    *,
    read_only: bool = False,
    kind: str | None = None,
    on_cancel: bool = False,
) -> T:
    """Run ``work`` in one transaction as the upkeep role. What it returns is
    printed by the caller after the commit, so no line says a change was made
    that was not. With ``on_cancel`` a statement cancelled by the timeout raises
    ``CountCancelled`` for the caller to word; any other failure exits as ever."""
    dsn = os.environ.get(UPKEEP_DATABASE_URL_ENV)
    if not dsn:
        _fail(f"{UPKEEP_DATABASE_URL_ENV} is not set")
    try:
        with connect(dsn, APPLICATION_NAME) as conn:
            conn.read_only = read_only
            return work(conn)
    except psycopg.Error as exc:
        if on_cancel and exc.diag.sqlstate == QUERY_CANCELED:
            raise CountCancelled from None
        _fail_on(exc, kind)
    except UnicodeError:
        # Text that cannot be encoded or decoded (a stray byte in an argument or
        # in the connection string): no detail, which could carry the string.
        _fail("gateway upkeep failed: text that is not valid UTF-8 was given")


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


def _tokens(text: str) -> int:
    amount = int(text) if TOKEN_AMOUNT.fullmatch(text) else 0
    if not 0 < amount <= BIGINT_MAX:
        raise typer.BadParameter(
            "an amount of tokens is a whole number above zero, in ASCII digits, "
            f"and at most {BIGINT_MAX}",
            param_hint="'--tokens'",
        )
    return amount


def _tenant(tenant: str) -> str:
    if not ENTITY_ID.fullmatch(tenant):
        raise typer.BadParameter(
            "a tenant is an ID of lower-case letters, digits and hyphens that "
            "starts with a letter or a digit",
            param_hint="TENANT",
        )
    return tenant


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
def _shown(text: str) -> str:
    """A tenant or a deployment from the ledger, as it is printed: only when it
    is an ID. Anything else (an escape sequence, a line break) is a placeholder;
    the attempt's ID, a UUID the database typed, is still printed beside it."""
    return text if ENTITY_ID.fullmatch(text) else NOT_AN_ID


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
            f"attempt {attempt_id} tenant {_shown(tenant)} "
            f"deployment {_shown(deployment)} "
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
        str | None,
        typer.Option(
            "--tokens",
            help="Tokens to credit to today's (UTC) counter: a whole number, "
            "in ASCII digits.",
        ),
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
    larger than what the counter holds beyond the reservations still open: a
    call in flight settles against the counter, so what it holds is not
    creditable until it closes. One audit row is written.
    """
    if (tokens is None) == (eur is None):
        raise typer.BadParameter("give exactly one of --tokens and --eur")
    tenant = _tenant(tenant)
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
EXPIRE_LEDGER_BATCH = "SELECT * FROM gateway.expire_ledger_batch(%s, %s, %s)"
# The function's own floor and maximum (0030); the default is a tenth of the
# maximum. Each batch writes an audit row the upkeep role can never remove, which
# is why a batch is never smaller than the floor.
MIN_LEDGER_BATCH = 100
MAX_LEDGER_BATCH = 10_000
DEFAULT_LEDGER_BATCH = 1_000
LEDGER_COUNT_CANCELLED = (
    "the count was cancelled (the statement timeout, or by an administrator), "
    "and nothing was changed: the real run removes in batches and needs no "
    "count, and a nearer --before counts faster"
)
# What the expiry would remove, by the same conditions as its DELETEs, and
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
    # Not `%Y`: it prints the year of 1 AD as `1`, and the database's own text for
    # the month (to_char YYYY) is `0001-01`.
    return (
        f"{prefix} before {month.year:04d}-{month.month:02d}: usage rows {usage}, "
        f"counter rows {counters}, credits {credits}"
    )


def _batches_line(usage: int, limit: int, batches: int | None = None) -> str:
    """What the run takes in calls: the batches of usage rows, then the call that
    removes the counters and credits. Before a run, the batches it will need."""
    taken = -(-usage // limit) if batches is None else batches
    return (
        f"in {taken} batch(es) of at most {limit} usage rows, "
        "then the counters and credits of those months"
    )


def _dry_run(conn: psycopg.Connection, before: date, limit: int) -> list[str]:
    current, usage, counters, credits, open_rows = conn.execute(
        WOULD_REMOVE, {"before": before}
    ).fetchone()
    if before > current:
        # The function refuses it too; say so now, not after --confirm.
        _refuse("GU302")
    lines = [
        _counts("would remove", before, usage, counters, credits),
        _batches_line(usage, limit),
        f"still reserved in those months: {open_rows}",
    ]
    if open_rows:
        lines.append("--confirm is refused until each is closed (see: close)")
    return [*lines, "nothing removed: add --confirm to remove them", DRY_RUN_NOTE]


def _ledger_limit(limit: int) -> int:
    if not MIN_LEDGER_BATCH <= limit <= MAX_LEDGER_BATCH:
        raise typer.BadParameter(
            f"a batch is from {MIN_LEDGER_BATCH} to {MAX_LEDGER_BATCH} usage rows",
            param_hint="'--limit'",
        )
    return limit


@dataclass
class Removed:
    """What the batches of a run have removed so far, and committed."""

    usage: int = 0
    batches: int = 0


def _call_until_closed(
    conn: psycopg.Connection, month: date, slug: str, limit: int, so_far: Removed
) -> tuple[int, int]:
    """Call the function on one connection until a call removes no usage row,
    one transaction for each call. That last call closed the periods (it returns
    their counters and credits) or found nothing to close."""
    while True:
        removed = conn.execute(EXPIRE_LEDGER_BATCH, (month, slug, limit)).fetchone()
        conn.commit()
        if removed[0] == 0:
            return removed[1], removed[2]
        so_far.usage += removed[0]
        so_far.batches += 1


def _expire_ledger_batches(
    month: date, slug: str, limit: int
) -> tuple[int, int, int, int]:
    """One connection for the run, a transaction for each call. Returns the usage
    rows, counters and credits removed and the batches that removed usage rows. A
    failure after a batch says what stays removed, then exits as any failure."""
    so_far = Removed()
    try:
        counters, credits = _upkeep(
            lambda conn: _call_until_closed(conn, month, slug, limit, so_far)
        )
    except typer.Exit:
        if so_far.batches:
            # A count, not a promise of the total: a batch is counted after its
            # commit returned, so a commit whose outcome is unknown (the
            # connection died inside it) may have removed one batch more. The
            # line starts "removed N usage rows in B batch(es) before the
            # failure" because infra/kind/upkeep.sh matches that start.
            typer.echo(
                f"removed {so_far.usage} usage rows in {so_far.batches} batch(es) "
                "before the failure (at least that many: a commit whose outcome "
                "is unknown may have removed one batch more): each batch is its "
                "own transaction with its own audit row, and what was removed "
                "stays removed; run the command again to continue",
                err=True,
            )
        raise
    return so_far.usage, counters, credits, so_far.batches


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
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help=f"Usage rows a batch removes, {MIN_LEDGER_BATCH} to "
            f"{MAX_LEDGER_BATCH}; each batch is its own transaction and leaves "
            "an audit row nobody can remove.",
        ),
    ] = DEFAULT_LEDGER_BATCH,
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
    refused. With --confirm it removes at most --limit usage rows at a time (100
    to 10,000: each batch leaves an audit row nobody can remove), on one
    connection, and then the counters and credits of those months in one more
    call. A run that stops half way leaves what it removed removed; running it
    again continues.
    """
    month = _month(before)
    slug = _slug(reason)
    batch = _ledger_limit(limit)
    if not confirm:
        try:
            lines = _upkeep(
                lambda conn: _dry_run(conn, month, batch),
                read_only=True,
                on_cancel=True,
            )
        except CountCancelled:
            _fail(LEDGER_COUNT_CANCELLED)
        for line in lines:
            typer.echo(line)
        return
    usage, counters, credits, batches = _expire_ledger_batches(month, slug, batch)
    if usage + counters + credits == 0:
        # Nothing before that month: the function returns zeros and writes no row.
        _refuse("GU304")
    typer.echo(_counts("removed", month, usage, counters, credits))
    typer.echo(_batches_line(usage, batch, batches))


# ── expire-audit ─────────────────────────────────────────────────────────────
EXPIRE_AUDIT_EVENTS = "SELECT gateway.expire_audit_events(%s, %s, %s)"
COUNT_AUDIT_EVENTS = "SELECT gateway.count_audit_events_before(%s)"
# The function's own maximum (0028); the default is a tenth of it.
MAX_AUDIT_BATCH = 10_000
DEFAULT_AUDIT_BATCH = 1_000
AUDIT_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# A timestamp in ASCII digits; the offset is optional here only so that its
# absence gets a sentence of its own.
AUDIT_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?"
    r"(Z|[+-][0-9]{2}(:?[0-9]{2})?)?"
)
AUDIT_DRY_RUN_NOTE = (
    "this is a count at this moment: a row another session holds is left for a "
    "later run"
)


def _audit_cutoff(text: str) -> datetime:
    """A bare date is 00:00 UTC of that day; a timestamp must carry its offset,
    or it would mean this machine's zone. Returned in UTC."""
    try:
        if AUDIT_DATE.fullmatch(text):
            return datetime.fromisoformat(text).replace(tzinfo=UTC)
        if AUDIT_TIMESTAMP.fullmatch(text):
            cutoff = datetime.fromisoformat(text)
            if cutoff.tzinfo is not None:
                return cutoff.astimezone(UTC)
            raise typer.BadParameter(
                "a timestamp needs its offset (Z or +hh:mm): without one it would "
                "mean this machine's zone",
                param_hint="'--before'",
            )
    except (ValueError, OverflowError):
        pass
    raise typer.BadParameter(
        "a cutoff is a date, such as 2026-09-01 (00:00 UTC), or a timestamp with "
        "an offset, such as 2026-09-01T12:00:00+02:00",
        param_hint="'--before'",
    )


def _audit_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_AUDIT_BATCH:
        raise typer.BadParameter(
            f"a batch is from 1 to {MAX_AUDIT_BATCH} rows", param_hint="'--limit'"
        )
    return limit


AUDIT_COUNT_CANCELLED = (
    "the count did not finish inside the statement timeout, and nothing was "
    "changed: the real run removes in batches and needs no count, and a nearer "
    "--before counts faster"
)


def _count_audit(cutoff: datetime) -> int:
    """What the expiry would take for this cutoff, counted in a read-only
    transaction; raises ``CountCancelled`` when the timeout cancels the count."""
    return _upkeep(
        lambda conn: conn.execute(COUNT_AUDIT_EVENTS, (cutoff,)).fetchone()[0],
        read_only=True,
        on_cancel=True,
    )


def _say_what_remains(cutoff: datetime) -> None:
    """After a real run: count once what is still older than the cutoff. A row
    another session held, or one written by a transaction that began before the
    cutoff, is left for a later run. A count the timeout cancels is said, and the
    command still succeeds: the removal happened."""
    try:
        remaining = _count_audit(cutoff)
    except CountCancelled:
        typer.echo(
            "the count of what remains did not finish inside the statement "
            "timeout; the removal above happened"
        )
        return
    if remaining:
        typer.echo(
            f"{remaining} audit rows older than the cutoff remain (rows another "
            "session held, or written by a transaction that began before it): "
            "run it again"
        )


def _utc_text(cutoff: datetime) -> str:
    return cutoff.isoformat().replace("+00:00", "Z")


def _expire_audit_batches(cutoff: datetime, slug: str, limit: int) -> tuple[int, int]:
    """Call the function until a call removes nothing; each call is its own
    transaction. Returns the rows removed and the batches that removed some. A
    failure after a batch says what stays removed, then exits as any failure."""
    total = batches = 0
    while True:
        try:
            removed = _upkeep(
                lambda conn: conn.execute(
                    EXPIRE_AUDIT_EVENTS, (cutoff, slug, limit)
                ).fetchone()[0]
            )
        except typer.Exit:
            if batches:
                typer.echo(
                    f"removed {total} audit rows in {batches} batch(es) before the "
                    "failure: each batch is its own transaction with its own audit "
                    "row, and what was removed stays removed",
                    err=True,
                )
            raise
        if removed == 0:
            return total, batches
        total += removed
        batches += 1


@app.command("expire-audit")
def expire_audit(
    before: Annotated[
        str,
        typer.Option(
            "--before",
            help="The cutoff: every audit row recorded before it goes. A date "
            "(00:00 UTC of that day) or a timestamp with an offset, such as "
            "2026-09-01T12:00:00+02:00. No default. Through `make gateway-upkeep` "
            "only a date works (its words hold no colon or plus sign).",
        ),
    ],
    reason: Annotated[
        str,
        typer.Option("--reason", help="Why: a slug, such as retention-2026. Audited."),
    ],
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help=f"Rows a batch removes, 1 to {MAX_AUDIT_BATCH}; each batch is its "
            "own transaction.",
        ),
    ] = DEFAULT_AUDIT_BATCH,
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Remove what the dry run counts. Without it nothing is removed.",
        ),
    ] = False,
) -> None:
    """Remove audit rows older than --before, in batches.

    Without --confirm it counts what would be removed and removes nothing. The
    cutoff is the operator's decision: there is no default, nothing runs this on
    a schedule, and the retention period is the owner's to choose. The function
    looks at a row's age only, not at whether its claim still exists. With
    --confirm it removes at most --limit rows at a time until a call removes
    nothing, one audit row for each batch, and prints the total and the number
    of batches.
    """
    cutoff = _audit_cutoff(before)
    slug = _slug(reason)
    batch = _audit_limit(limit)
    text = _utc_text(cutoff)
    if not confirm:
        try:
            counted = _count_audit(cutoff)
        except CountCancelled:
            _fail(AUDIT_COUNT_CANCELLED)
        typer.echo(f"would remove audit rows before {text}: {counted}")
        typer.echo(f"in {-(-counted // batch)} batch(es) of at most {batch}")
        typer.echo("nothing removed: add --confirm to remove them")
        typer.echo(AUDIT_DRY_RUN_NOTE)
        return
    total, batches = _expire_audit_batches(cutoff, slug, batch)
    typer.echo(
        f"removed {total} audit rows before {text} in {batches} batch(es) "
        f"of at most {batch}"
    )
    _say_what_remains(cutoff)
