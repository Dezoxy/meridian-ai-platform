"""What a call may cost, reserved before it is made (S011, QA-12, T-14).

A tenant has a token budget per UTC day and a cost quota per UTC month. Before
a provider call the gateway reserves the most the call can use: the estimated
input plus the largest reply it allows (none for an embedding, whose estimate
is its input alone; a response schema counts as input, because the provider
bills it as prompt tokens). The reservation is written to PostgreSQL before
the call because a process that crashes mid-call must not leave a call that was
sent and never counted (T-14), and because the check and the increment must be
one statement, or two concurrent calls both pass the same check (QA-12). The
reservation is then closed in one of three ways:

- ``settle``: the provider answered; the charge is its own count of tokens and
  the cost of that count.
- ``release``: the gateway knows the provider billed nothing (a request that
  never left, or one the provider refused with a 4xx before running it); the
  charge is nothing.
- ``keep``: anything else (a timeout, a 5xx, a lost connection, an unreadable
  or filtered reply, a crash): the call may have been billed, so the
  reservation stays as the charge. Over-charging is the accepted side.

Each counter always equals the sum of the charged amounts of the usage rows of
its tenant and period. Statements use psycopg placeholders only (T-07). The
tables hold identifiers and numbers, never content (T-03, T-25). A database
error propagates: a call that cannot be reserved is not made.
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any, Literal

import psycopg

from meridian.platform.common.db import connect
from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest
from meridian.platform.registry.models import (
    Deployment,
    ExchangeRate,
    Price,
    TenantLimits,
)

BYTES_PER_TOKEN = 3  # an estimate, not a bound: English runs near 4
MESSAGE_OVERHEAD_TOKENS = 8  # role and separators of one message
REPLY_OVERHEAD_TOKENS = 8  # the priming of the reply
MICRO = 1_000_000
APPLICATION_NAME = "model-gateway"

TOKENS_KIND = "tokens-day"
COST_KIND = "cost-month"

BudgetRefusalReason = Literal["tenant-token-budget", "tenant-cost-budget"]
ClosingState = Literal["settled", "released", "kept"]

# A counter row is created at zero the first time a period is used; the
# conditional UPDATE below is the only place the limit is checked.
INSERT_COUNTER = """
INSERT INTO gateway.budget_counters (tenant, kind, period_start)
VALUES (%(tenant)s, %(kind)s, %(period_start)s)
ON CONFLICT DO NOTHING
"""
# The check and the increment in one statement: at READ COMMITTED a SELECT of
# the amount followed by a write would let two connections both read the same
# amount and both pass. A row that fails the condition is not updated.
CHARGE_COUNTER = """
UPDATE gateway.budget_counters
SET amount = amount + %(amount)s
WHERE tenant = %(tenant)s AND kind = %(kind)s AND period_start = %(period_start)s
  AND amount + %(amount)s <= %(limit)s
"""
ADJUST_COUNTER = """
UPDATE gateway.budget_counters
SET amount = amount + %(delta)s
WHERE tenant = %(tenant)s AND kind = %(kind)s AND period_start = %(period_start)s
"""
INSERT_USAGE = """
INSERT INTO gateway.usage (
    call_id, tenant, agent, run_id, deployment, provider, model, day, month,
    reserved_tokens, reserved_micro_eur, charged_tokens, charged_micro_eur
) VALUES (
    %(call_id)s, %(tenant)s, %(agent)s, %(run_id)s, %(deployment)s,
    %(provider)s, %(model)s, %(day)s, %(month)s,
    %(tokens)s, %(micro_eur)s, %(tokens)s, %(micro_eur)s
)
RETURNING attempt_id
"""
# A NULL charge keeps the charge the row already holds (the reservation).
CLOSE_USAGE = """
UPDATE gateway.usage
SET state = %(state)s,
    input_tokens = %(input_tokens)s,
    output_tokens = %(output_tokens)s,
    charged_tokens = COALESCE(%(charged_tokens)s, charged_tokens),
    charged_micro_eur = COALESCE(%(charged_micro_eur)s, charged_micro_eur),
    closed_at = now()
WHERE attempt_id = %(attempt_id)s AND state = 'reserved'
RETURNING tenant, day, month, reserved_tokens, reserved_micro_eur,
          charged_tokens, charged_micro_eur
"""


def utc_today() -> date:
    return datetime.now(UTC).date()


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """What a call may use, as both purposes estimate it from the text as sent:
    the input tokens and the largest reply. The rate limiter admits ``tokens``
    and the ledger reserves it, so a request is held to one number."""

    input_tokens: int
    max_output_tokens: int

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.max_output_tokens


def _utf8_tokens(text: str) -> int:
    """The UTF-8 bytes over three, rounded up."""
    return -(-len(text.encode("utf-8")) // BYTES_PER_TOKEN)


def _schema_tokens(schema: dict[str, Any] | None) -> int:
    """The tokens of a response schema, which the provider bills as prompt:
    its compact JSON, keys sorted, counted as a message's text is (S051)."""
    if schema is None:
        return 0
    return _utf8_tokens(
        json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )


def estimate_input_tokens(request: ChatRequest) -> int:
    """An estimate of the input tokens: per message the UTF-8 bytes over three,
    rounded up, plus the message overhead; plus the reply's priming; plus the
    response schema's compact JSON when the request carries one (S051)."""
    return (
        REPLY_OVERHEAD_TOKENS
        + sum(
            _utf8_tokens(message.content) + MESSAGE_OVERHEAD_TOKENS
            for message in request.messages
        )
        + _schema_tokens(request.response_schema)
    )


def chat_estimate(request: ChatRequest) -> TokenEstimate:
    """The estimated input and the largest reply the request allows."""
    return TokenEstimate(estimate_input_tokens(request), request.max_output_tokens)


def embedding_estimate(request: EmbeddingRequest) -> TokenEstimate:
    """Per input the UTF-8 bytes over three, rounded up, summed; no overhead
    and no reply, because an embedding has no output."""
    return TokenEstimate(sum(_utf8_tokens(text) for text in request.inputs), 0)


def cost_micro_eur(
    price: Price, exchange: ExchangeRate, input_tokens: int, output_tokens: int
) -> int:
    """The cost in micro-EUR, rounded up to a whole one.

    A price is USD per million tokens, so tokens times price is micro-USD, and
    micro-USD over USD per EUR is micro-EUR. Decimal arithmetic, never float. A
    price without an output price charges output tokens nothing.
    """
    micro_usd = Decimal(input_tokens) * price.input_per_million_tokens
    if price.output_per_million_tokens is not None:
        micro_usd += Decimal(output_tokens) * price.output_per_million_tokens
    return int(
        (micro_usd / exchange.usd_per_eur).to_integral_value(rounding=ROUND_CEILING)
    )


@dataclass(frozen=True, slots=True)
class Caller:
    call_id: uuid.UUID
    tenant: str
    agent: str
    run_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Reservation:
    attempt_id: uuid.UUID
    tokens: int
    micro_eur: int


@dataclass(frozen=True, slots=True)
class BudgetRefusal:
    reason: BudgetRefusalReason


class LedgerInconsistent(Exception):
    """A counter row a closing must move is not there exactly once. The closing
    is rolled back and the usage row stays reserved, so the charge stands. The
    message names no value."""

    def __init__(self) -> None:
        super().__init__("a budget counter was not found exactly once")


def _charge(
    conn: psycopg.Connection,
    tenant: str,
    kind: str,
    period: date,
    amount: int,
    limit: int,
) -> bool:
    """Add ``amount`` to the counter if that keeps it within ``limit``."""
    cursor = conn.execute(
        CHARGE_COUNTER,
        {
            "tenant": tenant,
            "kind": kind,
            "period_start": period,
            "amount": amount,
            "limit": limit,
        },
    )
    return cursor.rowcount == 1


def _usage_values(
    caller: Caller,
    deployment: Deployment,
    period: tuple[date, date],
    tokens: int,
    micro_eur: int,
) -> dict[str, object]:
    """The parameters of ``INSERT_USAGE``: who, what served it, the day and the
    month it is charged to, how much."""
    day, month = period
    return {
        "call_id": caller.call_id,
        "tenant": caller.tenant,
        "agent": caller.agent,
        "run_id": caller.run_id,
        "deployment": deployment.id,
        "provider": deployment.provider,
        "model": deployment.model,
        "day": day,
        "month": month,
        "tokens": tokens,
        "micro_eur": micro_eur,
    }


class Ledger:
    """Reserves and closes charges in PostgreSQL, one connection and one
    transaction per method, so it keeps no state and is safe to share between
    threads. A ``psycopg.Error`` propagates from every method."""

    def __init__(
        self,
        dsn: str,
        *,
        exchange: ExchangeRate,
        today: Callable[[], date] = utc_today,
    ) -> None:
        self._dsn = dsn
        self._exchange = exchange
        self._today = today

    def reserve(
        self,
        caller: Caller,
        deployment: Deployment,
        limits: TenantLimits,
        estimate: TokenEstimate,
    ) -> Reservation | BudgetRefusal:
        """Charge the most the call can cost, or refuse it: the day's tokens
        first, then the month's cost, always in that order so two calls cannot
        deadlock. A refusal leaves nothing behind."""
        day = self._today()
        month = day.replace(day=1)
        tokens = estimate.tokens
        micro_eur = cost_micro_eur(
            deployment.price,
            self._exchange,
            estimate.input_tokens,
            estimate.max_output_tokens,
        )
        with connect(self._dsn, APPLICATION_NAME) as conn:
            for kind, period in ((TOKENS_KIND, day), (COST_KIND, month)):
                conn.execute(
                    INSERT_COUNTER,
                    {"tenant": caller.tenant, "kind": kind, "period_start": period},
                )
            token_limit = limits.tokens_per_day
            if not _charge(conn, caller.tenant, TOKENS_KIND, day, tokens, token_limit):
                conn.rollback()
                return BudgetRefusal("tenant-token-budget")
            cost_limit = int(limits.cost_per_month_eur * MICRO)
            if not _charge(
                conn, caller.tenant, COST_KIND, month, micro_eur, cost_limit
            ):
                conn.rollback()
                return BudgetRefusal("tenant-cost-budget")
            ((attempt_id,),) = conn.execute(
                INSERT_USAGE,
                _usage_values(caller, deployment, (day, month), tokens, micro_eur),
            ).fetchall()
            conn.commit()
        return Reservation(attempt_id=attempt_id, tokens=tokens, micro_eur=micro_eur)

    def settle(
        self,
        reservation: Reservation,
        deployment: Deployment,
        input_tokens: int,
        output_tokens: int,
    ) -> int:
        """The provider answered: charge its own count and what it costs, and
        return that cost in micro-EUR. A charge above the reservation is
        written as it is."""
        micro_eur = cost_micro_eur(
            deployment.price, self._exchange, input_tokens, output_tokens
        )
        self._close(
            reservation,
            "settled",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            charged_tokens=input_tokens + output_tokens,
            charged_micro_eur=micro_eur,
        )
        return micro_eur

    def release(self, reservation: Reservation) -> None:
        """The gateway knows the provider billed nothing: the request never
        left, or the provider refused it with a 4xx before running it."""
        self._close(reservation, "released", charged_tokens=0, charged_micro_eur=0)

    def keep(self, reservation: Reservation) -> None:
        """It may have been billed: the reservation stays as the charge."""
        self._close(reservation, "kept")

    def _close(
        self,
        reservation: Reservation,
        state: ClosingState,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        charged_tokens: int | None = None,
        charged_micro_eur: int | None = None,
    ) -> None:
        """Close the attempt and move the counters by charged minus reserved,
        in the day and month the attempt was charged to. Closing an attempt
        that is no longer reserved changes nothing."""
        with connect(self._dsn, APPLICATION_NAME) as conn:
            row = conn.execute(
                CLOSE_USAGE,
                {
                    "attempt_id": reservation.attempt_id,
                    "state": state,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "charged_tokens": charged_tokens,
                    "charged_micro_eur": charged_micro_eur,
                },
            ).fetchone()
            if row is not None:
                tenant, day, month, r_tokens, r_micro, c_tokens, c_micro = row
                for kind, period, delta in (
                    (TOKENS_KIND, day, c_tokens - r_tokens),
                    (COST_KIND, month, c_micro - r_micro),
                ):
                    if delta == 0:
                        continue
                    cursor = conn.execute(
                        ADJUST_COUNTER,
                        {
                            "tenant": tenant,
                            "kind": kind,
                            "period_start": period,
                            "delta": delta,
                        },
                    )
                    if cursor.rowcount != 1:
                        # Leaving the block rolls the closing back.
                        raise LedgerInconsistent
            conn.commit()
