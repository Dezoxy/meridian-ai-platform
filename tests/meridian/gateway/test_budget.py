"""The token and cost ledger (S011, QA-12): pure functions, then PostgreSQL."""

import math
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from psycopg.conninfo import make_conninfo
from servicesupport import REGISTRY_DIR, owner_rows
from upkeepsupport import CLOSE, CREDIT, plant_usage
from upkeepsupport import ROLE as UPKEEP_ROLE
from upkeepsupport import run as run_as

from meridian.platform.common.db import connect
from meridian.platform.gateway.budget import (
    MICRO,
    BudgetRefusal,
    Caller,
    Ledger,
    LedgerInconsistent,
    Reservation,
    TokenEstimate,
    chat_estimate,
    cost_micro_eur,
    embedding_estimate,
    estimate_input_tokens,
)
from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest, Message
from meridian.platform.registry import load_registry
from meridian.platform.registry.models import (
    Deployment,
    ExchangeRate,
    Price,
    TenantLimits,
)

TENANT = "development"
OTHER_TENANT = "evaluation"
AGENT = "claims-triage"
OCTOBER_FIRST = date(2026, 10, 1)
OCTOBER_LAST = date(2026, 10, 31)
RACERS = 12
EXCHANGE = ExchangeRate(
    usd_per_eur=Decimal("1.1355"), source="a test fixture", checked=date(2026, 9, 30)
)
GPT4O_PRICE = Price(
    currency="USD",
    input_per_million_tokens=Decimal("3.025"),
    output_per_million_tokens=Decimal("12.10"),
    source="a test fixture",
    checked=date(2026, 9, 30),
)
BIG_DAY = 10**9
BIG_MONTH_EUR = Decimal(1000)


def text_request(text: str = "x" * 30, max_output: int = 100) -> ChatRequest:
    return ChatRequest(
        messages=(Message(role="user", content=text),), max_output_tokens=max_output
    )


def limits(
    day: int = BIG_DAY, month_micro: int | None = None, eur: Decimal | None = None
) -> TenantLimits:
    cost = eur if eur is not None else Decimal(month_micro or 0) / MICRO
    return TenantLimits(
        requests_per_10_seconds=10,
        tokens_per_minute=10_000,
        tokens_per_day=day,
        cost_per_month_eur=cost if cost > 0 else BIG_MONTH_EUR,
    )


def caller(tenant: str = TENANT) -> Caller:
    return Caller(call_id=uuid.uuid4(), tenant=tenant, agent=AGENT, run_id=uuid.uuid4())


class Today:
    """A date a test moves by hand."""

    def __init__(self, day: date = OCTOBER_FIRST) -> None:
        self.day = day

    def __call__(self) -> date:
        return self.day


@pytest.fixture(scope="module")
def deployment() -> Deployment:
    found = load_registry(REGISTRY_DIR).deployment("aoai-sdc-gpt-4o")
    assert found is not None
    return found


@pytest.fixture
def today() -> Today:
    return Today()


@pytest.fixture
def ledger(fresh_database: DatabaseHandle, today: Today) -> Ledger:
    return make_ledger(fresh_database, today)


def make_ledger(db: DatabaseHandle, today: Today) -> Ledger:
    return Ledger(db.dsn("model_gateway"), exchange=EXCHANGE, today=today)


def reserved_tokens(request: ChatRequest | None = None) -> int:
    return chat_estimate(request or text_request()).tokens


def reserved_micro(deployment: Deployment, request: ChatRequest | None = None) -> int:
    request = request or text_request()
    return cost_micro_eur(
        deployment.price,
        EXCHANGE,
        estimate_input_tokens(request),
        request.max_output_tokens,
    )


def reserve(
    ledger: Ledger,
    deployment: Deployment,
    *,
    who: Caller | None = None,
    window: TenantLimits | None = None,
    request: ChatRequest | None = None,
) -> Reservation | BudgetRefusal:
    return ledger.reserve(
        who or caller(),
        deployment,
        window or limits(),
        chat_estimate(request or text_request()),
    )


def reserved(
    ledger: Ledger,
    deployment: Deployment,
    *,
    who: Caller | None = None,
    window: TenantLimits | None = None,
) -> Reservation:
    outcome = reserve(ledger, deployment, who=who, window=window)
    assert isinstance(outcome, Reservation), outcome
    return outcome


def counters(db: DatabaseHandle, tenant: str = TENANT) -> dict[tuple[str, date], int]:
    rows = owner_rows(
        db,
        "SELECT kind, period_start, amount FROM gateway.budget_counters "
        "WHERE tenant = %s",
        (tenant,),
    )
    return {(kind, period): amount for kind, period, amount in rows}


def usage_rows(db: DatabaseHandle, tenant: str = TENANT) -> list[tuple]:
    return owner_rows(
        db,
        "SELECT attempt_id, state, reserved_tokens, reserved_micro_eur, "
        "charged_tokens, charged_micro_eur, input_tokens, output_tokens "
        "FROM gateway.usage WHERE tenant = %s ORDER BY reserved_at, attempt_id",
        (tenant,),
    )


def assert_counters_equal_charges(db: DatabaseHandle) -> None:
    """The invariant: each counter is the sum of its period's charged amounts,
    less the credits of that period (S066, ``gateway.credits``)."""
    tokens = owner_rows(
        db,
        "SELECT c.tenant, c.period_start, c.amount, "
        "COALESCE((SELECT sum(u.charged_tokens) FROM gateway.usage u "
        "WHERE u.tenant = c.tenant AND u.day = c.period_start), 0) "
        "- COALESCE((SELECT sum(r.amount) FROM gateway.credits r "
        "WHERE r.tenant = c.tenant AND r.kind = c.kind "
        "AND r.period_start = c.period_start), 0) "
        "FROM gateway.budget_counters c WHERE c.kind = 'tokens-day'",
    )
    cost = owner_rows(
        db,
        "SELECT c.tenant, c.period_start, c.amount, "
        "COALESCE((SELECT sum(u.charged_micro_eur) FROM gateway.usage u "
        "WHERE u.tenant = c.tenant AND u.month = c.period_start), 0) "
        "- COALESCE((SELECT sum(r.amount) FROM gateway.credits r "
        "WHERE r.tenant = c.tenant AND r.kind = c.kind "
        "AND r.period_start = c.period_start), 0) "
        "FROM gateway.budget_counters c WHERE c.kind = 'cost-month'",
    )
    for tenant, period, amount, charged in (*tokens, *cost):
        assert amount == charged, (tenant, period)
    uncounted = owner_rows(
        db,
        "SELECT count(*) FROM gateway.usage u WHERE u.charged_tokens > 0 AND NOT "
        "EXISTS (SELECT 1 FROM gateway.budget_counters c WHERE c.tenant = u.tenant "
        "AND c.kind = 'tokens-day' AND c.period_start = u.day)",
    )
    assert uncounted == [(0,)]


# ── estimate and cost: pure ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        pytest.param(["hi"], 1 + 8 + 8, id="two-bytes-is-one-token"),
        pytest.param(["abc"], 1 + 8 + 8, id="three-bytes-is-one-token"),
        pytest.param(["abcd"], 2 + 8 + 8, id="four-bytes-round-up-to-two"),
        pytest.param(["é" * 10], 7 + 8 + 8, id="twenty-bytes-not-ten-characters"),
        pytest.param(
            ["abc", "abcd", "x"], (1 + 2 + 1) + 3 * 8 + 8, id="three-messages"
        ),
    ],
)
def test_the_estimate_counts_bytes_and_overhead(
    messages: list[str], expected: int
) -> None:
    request = ChatRequest(
        messages=tuple(Message(role="user", content=m) for m in messages)
    )

    assert estimate_input_tokens(request) == expected


def test_the_reservation_adds_the_largest_reply(deployment: Deployment) -> None:
    request = text_request("x" * 30, max_output=250)

    assert estimate_input_tokens(request) == 10 + 8 + 8
    assert chat_estimate(request).tokens == 26 + 250


def test_the_chat_estimate_is_the_input_estimate_and_the_largest_reply() -> None:
    request = text_request("x" * 30, max_output=250)

    estimate = chat_estimate(request)

    assert estimate == TokenEstimate(input_tokens=26, max_output_tokens=250)
    assert estimate.tokens == 276


# A response schema is billed as prompt tokens, so the estimate counts it (S051).
SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["applies", "none"]}},
    "required": ["verdict"],
    "additionalProperties": False,
}
COMPACT_SCHEMA = (
    '{"additionalProperties":false,"properties":{"verdict":{"enum":'
    '["applies","none"],"type":"string"}},"required":["verdict"],"type":"object"}'
)


def test_the_estimate_adds_the_compact_json_of_a_response_schema() -> None:
    plain = text_request("x" * 30)
    asking = plain.model_copy(update={"response_schema": SCHEMA})

    extra = estimate_input_tokens(asking) - estimate_input_tokens(plain)

    assert extra == -(-len(COMPACT_SCHEMA.encode("utf-8")) // 3)
    assert chat_estimate(asking).tokens == chat_estimate(plain).tokens + extra


def test_a_request_without_a_schema_estimates_exactly_as_before() -> None:
    request = text_request("x" * 30, max_output=250)

    assert request.response_schema is None
    assert estimate_input_tokens(request) == 10 + 8 + 8
    assert chat_estimate(request) == TokenEstimate(26, 250)


def test_the_schema_estimate_does_not_depend_on_the_key_order() -> None:
    plain = text_request("x")
    reordered = dict(reversed(SCHEMA.items()))

    one = plain.model_copy(update={"response_schema": SCHEMA})
    other = plain.model_copy(update={"response_schema": reordered})

    assert estimate_input_tokens(one) == estimate_input_tokens(other)
    assert estimate_input_tokens(one) > estimate_input_tokens(plain)


def embedding_request(*inputs: str) -> EmbeddingRequest:
    return EmbeddingRequest(inputs=inputs)


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        pytest.param(["abc"], 1, id="three-bytes-is-one-token"),
        pytest.param(["abcd"], 2, id="four-bytes-round-up-to-two"),
        pytest.param(["é" * 10], 7, id="twenty-bytes-not-ten-characters"),
        # Each input rounds up on its own, then the tokens add up: pooling the
        # bytes first would make this 5 bytes -> 2.
        pytest.param(["abcd", "x"], 3, id="each-input-rounds-up-on-its-own"),
        # No overhead per input and none for a reply, as chat has.
        pytest.param(["abc"] * 16, 16, id="no-overhead-per-input"),
    ],
)
def test_the_embedding_estimate_counts_bytes_per_input_and_no_output(
    inputs: list[str], expected: int
) -> None:
    estimate = embedding_estimate(embedding_request(*inputs))

    assert estimate == TokenEstimate(input_tokens=expected, max_output_tokens=0)
    assert estimate.tokens == expected


def test_the_cost_of_gpt_4o_is_rounded_up_to_a_whole_micro_euro() -> None:
    # (1000 x 3.025 + 500 x 12.10) / 1.1355 = 9075 / 1.1355 = 7992.07... micro-EUR
    assert cost_micro_eur(GPT4O_PRICE, EXCHANGE, 1000, 500) == 7993


def test_a_cost_that_is_exactly_whole_is_not_rounded_up() -> None:
    price = GPT4O_PRICE.model_copy(
        update={
            "input_per_million_tokens": Decimal("2"),
            "output_per_million_tokens": Decimal(0),
        }
    )
    rate = EXCHANGE.model_copy(update={"usd_per_eur": Decimal("2")})

    assert cost_micro_eur(price, rate, 1000, 0) == 1000


def test_a_price_without_an_output_price_charges_output_tokens_nothing() -> None:
    price = GPT4O_PRICE.model_copy(update={"output_per_million_tokens": None})

    assert cost_micro_eur(price, EXCHANGE, 1000, 0) == cost_micro_eur(
        price, EXCHANGE, 1000, 500_000
    )
    assert (
        cost_micro_eur(price, EXCHANGE, 1000, 500) == 2665
    )  # 3025 / 1.1355 = 2664.02...


def test_zero_prices_cost_nothing() -> None:
    price = GPT4O_PRICE.model_copy(
        update={
            "input_per_million_tokens": Decimal(0),
            "output_per_million_tokens": Decimal(0),
        }
    )

    assert cost_micro_eur(price, EXCHANGE, 10**6, 10**6) == 0


def test_the_cost_is_exact_decimal_arithmetic_never_float() -> None:
    exact = Fraction(10**9) * Fraction("15.125") / Fraction("1.1355")

    cost = cost_micro_eur(GPT4O_PRICE, EXCHANGE, 10**9, 10**9)

    assert isinstance(cost, int)
    assert cost == math.ceil(exact)


# ── reserve ─────────────────────────────────────────────────────────────────
def test_reserve_writes_one_reserved_row_and_raises_both_counters(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    who = caller()

    reservation = reserved(ledger, deployment, who=who)

    tokens, micro = reserved_tokens(), reserved_micro(deployment)
    assert reservation.tokens == tokens
    assert reservation.micro_eur == micro
    assert counters(fresh_database) == {
        ("tokens-day", OCTOBER_FIRST): tokens,
        ("cost-month", OCTOBER_FIRST): micro,
    }
    ((attempt_id, state, r_tokens, r_micro, c_tokens, c_micro, i_tok, o_tok),) = (
        usage_rows(fresh_database)
    )
    assert attempt_id == reservation.attempt_id
    assert (state, r_tokens, r_micro) == ("reserved", tokens, micro)
    assert (c_tokens, c_micro, i_tok, o_tok) == (tokens, micro, None, None)


@pytest.fixture(scope="module")
def embedding_deployment() -> Deployment:
    found = load_registry(REGISTRY_DIR).deployment("aoai-sdc-text-embedding-3-large")
    assert found is not None
    return found


def test_an_embedding_reservation_is_the_estimated_input_alone(
    ledger: Ledger, fresh_database: DatabaseHandle, embedding_deployment: Deployment
) -> None:
    estimate = embedding_estimate(embedding_request("x" * 30, "y" * 9))  # 10 + 3

    outcome = ledger.reserve(caller(), embedding_deployment, limits(), estimate)

    assert isinstance(outcome, Reservation)
    micro = cost_micro_eur(embedding_deployment.price, EXCHANGE, 13, 0)
    assert micro > 0
    assert (outcome.tokens, outcome.micro_eur) == (13, micro)
    assert counters(fresh_database) == {
        ("tokens-day", OCTOBER_FIRST): 13,
        ("cost-month", OCTOBER_FIRST): micro,
    }


def test_settling_an_embedding_charges_its_input_tokens_at_the_embedding_price(
    ledger: Ledger, fresh_database: DatabaseHandle, embedding_deployment: Deployment
) -> None:
    estimate = embedding_estimate(embedding_request("x" * 30))
    reservation = ledger.reserve(caller(), embedding_deployment, limits(), estimate)
    assert isinstance(reservation, Reservation)

    charged = ledger.settle(reservation, embedding_deployment, 8, 0)

    micro = cost_micro_eur(embedding_deployment.price, EXCHANGE, 8, 0)
    assert charged == micro > 0
    ((_, state, r_tokens, _, c_tokens, c_micro, i_tok, o_tok),) = usage_rows(
        fresh_database
    )
    assert (state, r_tokens, c_tokens, c_micro) == ("settled", 10, 8, micro)
    assert (i_tok, o_tok) == (8, 0)
    assert counters(fresh_database) == {
        ("tokens-day", OCTOBER_FIRST): 8,
        ("cost-month", OCTOBER_FIRST): micro,
    }
    assert_counters_equal_charges(fresh_database)


def test_an_embedding_over_the_daily_budget_is_refused_like_a_chat_call(
    ledger: Ledger, fresh_database: DatabaseHandle, embedding_deployment: Deployment
) -> None:
    estimate = embedding_estimate(embedding_request("x" * 30))  # 10 tokens

    fits = ledger.reserve(caller(), embedding_deployment, limits(day=10), estimate)
    over = ledger.reserve(caller(), embedding_deployment, limits(day=10), estimate)

    assert isinstance(fits, Reservation)
    assert over == BudgetRefusal("tenant-token-budget")
    assert len(usage_rows(fresh_database)) == 1


def test_the_usage_row_names_the_call_and_what_served_it(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    who = caller()

    reserved(ledger, deployment, who=who)

    assert owner_rows(
        fresh_database,
        "SELECT call_id, tenant, agent, run_id, deployment, provider, model, "
        "day, month, closed_at FROM gateway.usage",
    ) == [
        (
            who.call_id,
            TENANT,
            AGENT,
            who.run_id,
            "aoai-sdc-gpt-4o",
            "azure-openai",
            "gpt-4o",
            OCTOBER_FIRST,
            OCTOBER_FIRST,
            None,
        )
    ]


def test_the_month_row_is_dated_the_first_of_the_month(
    ledger: Ledger,
    fresh_database: DatabaseHandle,
    deployment: Deployment,
    today: Today,
) -> None:
    today.day = date(2026, 10, 17)

    reserved(ledger, deployment)

    assert set(counters(fresh_database)) == {
        ("tokens-day", date(2026, 10, 17)),
        ("cost-month", OCTOBER_FIRST),
    }


def test_today_is_asked_once_per_reserve(
    fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    asked: list[int] = []

    def today() -> date:
        asked.append(1)
        return OCTOBER_FIRST

    ledger = Ledger(fresh_database.dsn("model_gateway"), exchange=EXCHANGE, today=today)
    reserved(ledger, deployment)

    assert len(asked) == 1


def test_reserving_a_call_twice_on_one_deployment_is_refused_by_the_database(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    who = caller()
    reserved(ledger, deployment, who=who)
    before = counters(fresh_database)

    with pytest.raises(psycopg.errors.UniqueViolation):
        reserve(ledger, deployment, who=who)

    assert counters(fresh_database) == before
    assert len(usage_rows(fresh_database)) == 1


# ── closing ─────────────────────────────────────────────────────────────────
def test_settle_writes_the_real_tokens_and_cost_and_moves_both_counters(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    reservation = reserved(ledger, deployment)

    charged = ledger.settle(reservation, deployment, 60, 40)

    micro = cost_micro_eur(deployment.price, EXCHANGE, 60, 40)
    assert charged == micro > 0  # the charge, for the metrics and the span
    assert counters(fresh_database) == {
        ("tokens-day", OCTOBER_FIRST): 100,
        ("cost-month", OCTOBER_FIRST): micro,
    }
    ((_, state, r_tokens, r_micro, c_tokens, c_micro, i_tok, o_tok),) = usage_rows(
        fresh_database
    )
    assert (state, c_tokens, c_micro, i_tok, o_tok) == ("settled", 100, micro, 60, 40)
    assert (r_tokens, r_micro) == (reserved_tokens(), reserved_micro(deployment))
    assert_counters_equal_charges(fresh_database)


def test_settle_closes_the_row(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    ledger.settle(reserved(ledger, deployment), deployment, 1, 1)

    assert owner_rows(
        fresh_database, "SELECT closed_at IS NOT NULL FROM gateway.usage"
    ) == [(True,)]


def test_release_returns_both_counters_to_what_they_were(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    first = reserved(ledger, deployment)
    ledger.settle(first, deployment, 60, 40)
    before = counters(fresh_database)
    second = reserved(ledger, deployment)
    assert counters(fresh_database) != before

    ledger.release(second)

    assert counters(fresh_database) == before
    (_, released) = usage_rows(fresh_database)
    assert released[1:] == (
        "released",
        reserved_tokens(),
        reserved_micro(deployment),
        0,
        0,
        None,
        None,
    )
    assert_counters_equal_charges(fresh_database)


def test_keep_leaves_the_reservation_as_the_charge(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    reservation = reserved(ledger, deployment)
    before = counters(fresh_database)

    ledger.keep(reservation)

    assert counters(fresh_database) == before
    ((_, state, _, _, c_tokens, c_micro, i_tok, o_tok),) = usage_rows(fresh_database)
    assert (state, c_tokens, c_micro) == (
        "kept",
        reserved_tokens(),
        reserved_micro(deployment),
    )
    assert (i_tok, o_tok) == (None, None)
    assert_counters_equal_charges(fresh_database)


def test_a_settled_charge_above_the_reservation_is_written_as_it_is(
    fresh_database: DatabaseHandle, deployment: Deployment, today: Today
) -> None:
    ledger = make_ledger(fresh_database, today)
    tokens = reserved_tokens()
    window = limits(day=tokens)  # the reservation fills the day exactly
    reservation = reserved(ledger, deployment, window=window)

    ledger.settle(reservation, deployment, tokens, 500)  # the provider's count

    assert counters(fresh_database)[("tokens-day", OCTOBER_FIRST)] == tokens + 500
    assert_counters_equal_charges(fresh_database)
    assert isinstance(reserve(ledger, deployment, window=window), BudgetRefusal)


CLOSERS = ["settle", "release", "keep"]


def close(
    ledger: Ledger, how: str, reservation: Reservation, deployment: Deployment
) -> None:
    if how == "settle":
        ledger.settle(reservation, deployment, 7, 3)
    else:
        getattr(ledger, how)(reservation)


@pytest.mark.parametrize("second", CLOSERS)
@pytest.mark.parametrize("first", CLOSERS)
def test_closing_twice_changes_nothing_after_the_first(
    ledger: Ledger,
    fresh_database: DatabaseHandle,
    deployment: Deployment,
    first: str,
    second: str,
) -> None:
    reservation = reserved(ledger, deployment)
    close(ledger, first, reservation, deployment)
    counters_after_first = counters(fresh_database)
    usage_after_first = owner_rows(fresh_database, "SELECT * FROM gateway.usage")

    close(ledger, second, reservation, deployment)

    assert counters(fresh_database) == counters_after_first
    assert (
        owner_rows(fresh_database, "SELECT * FROM gateway.usage") == usage_after_first
    )
    assert_counters_equal_charges(fresh_database)


# ── refusals ────────────────────────────────────────────────────────────────
def test_one_token_over_the_daily_limit_is_refused_and_leaves_no_trace(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    tokens = reserved_tokens()

    outcome = reserve(ledger, deployment, window=limits(day=tokens - 1))

    assert outcome == BudgetRefusal("tenant-token-budget")
    assert usage_rows(fresh_database) == []
    assert counters(fresh_database) == {}  # the counter rows rolled back too


def test_a_reservation_exactly_at_the_daily_limit_passes(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    tokens = reserved_tokens()

    outcome = reserve(ledger, deployment, window=limits(day=tokens))

    assert isinstance(outcome, Reservation)
    assert counters(fresh_database)[("tokens-day", OCTOBER_FIRST)] == tokens


def test_the_second_call_that_would_pass_the_limit_is_refused_untouched(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    tokens = reserved_tokens()
    window = limits(day=2 * tokens)
    reserved(ledger, deployment, window=window)
    reserved(ledger, deployment, window=window)
    before = counters(fresh_database)

    outcome = reserve(ledger, deployment, window=window)

    assert outcome == BudgetRefusal("tenant-token-budget")
    assert counters(fresh_database) == before
    assert len(usage_rows(fresh_database)) == 2


def test_one_micro_euro_over_the_monthly_cost_is_refused(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    micro = reserved_micro(deployment)

    outcome = reserve(ledger, deployment, window=limits(month_micro=micro - 1))

    assert outcome == BudgetRefusal("tenant-cost-budget")
    assert usage_rows(fresh_database) == []


def test_a_cost_exactly_at_the_monthly_limit_passes(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    micro = reserved_micro(deployment)

    outcome = reserve(ledger, deployment, window=limits(month_micro=micro))

    assert isinstance(outcome, Reservation)
    assert counters(fresh_database)[("cost-month", OCTOBER_FIRST)] == micro


def test_a_cost_refusal_rolls_back_the_token_increment_that_passed(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    window = limits(month_micro=reserved_micro(deployment) - 1)
    assert isinstance(reserve(ledger, deployment, window=window), BudgetRefusal)

    # The token update passed (the daily limit is huge) and was rolled back.
    assert counters(fresh_database).get(("tokens-day", OCTOBER_FIRST), 0) == 0
    assert_counters_equal_charges(fresh_database)


def test_a_cost_refusal_after_earlier_use_leaves_the_token_counter_as_it_was(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    micro = reserved_micro(deployment)
    window = limits(month_micro=micro)
    reserved(ledger, deployment, window=window)
    before = counters(fresh_database)

    outcome = reserve(ledger, deployment, window=window)

    assert outcome == BudgetRefusal("tenant-cost-budget")
    assert counters(fresh_database) == before


# ── the race (QA-12) ────────────────────────────────────────────────────────
def race(
    db: DatabaseHandle,
    deployment: Deployment,
    window: TenantLimits,
    today: Today,
    racers: int = RACERS,
) -> list[Reservation | BudgetRefusal]:
    """``racers`` threads, each with a Ledger and a connection of its own, call
    ``reserve`` at the same moment."""
    start = threading.Barrier(racers)

    def call() -> Reservation | BudgetRefusal:
        own = make_ledger(db, today)
        start.wait(timeout=30)
        return reserve(own, deployment, window=window)

    with ThreadPoolExecutor(max_workers=racers) as pool:
        futures = [pool.submit(call) for _ in range(racers)]
        return [future.result(timeout=60) for future in futures]


def seeded(db: DatabaseHandle, deployment: Deployment, today: Today) -> tuple[int, int]:
    """One earlier reservation, so the counter rows exist and the racers meet at
    the UPDATE, not at the creation of the row (where PostgreSQL would queue
    them behind the first insert). Returns what it reserved: tokens, micro-EUR."""
    reserved(make_ledger(db, today), deployment)
    return reserved_tokens(), reserved_micro(deployment)


def test_with_room_for_exactly_one_more_only_one_racer_reserves(
    fresh_database: DatabaseHandle, deployment: Deployment, today: Today
) -> None:
    tokens, _ = seeded(fresh_database, deployment, today)

    outcomes = race(fresh_database, deployment, limits(day=2 * tokens), today)

    won = [o for o in outcomes if isinstance(o, Reservation)]
    assert len(won) == 1
    assert outcomes.count(BudgetRefusal("tenant-token-budget")) == RACERS - 1
    assert counters(fresh_database)[("tokens-day", OCTOBER_FIRST)] == 2 * tokens
    assert len(usage_rows(fresh_database)) == 2
    assert_counters_equal_charges(fresh_database)


@pytest.mark.parametrize("room", [2, 5, 8])
def test_with_room_for_k_more_exactly_k_racers_reserve(
    fresh_database: DatabaseHandle, deployment: Deployment, today: Today, room: int
) -> None:
    tokens, _ = seeded(fresh_database, deployment, today)

    outcomes = race(fresh_database, deployment, limits(day=(1 + room) * tokens), today)

    assert sum(isinstance(o, Reservation) for o in outcomes) == room
    assert counters(fresh_database)[("tokens-day", OCTOBER_FIRST)] == (
        (1 + room) * tokens
    )
    assert len(usage_rows(fresh_database)) == 1 + room
    assert_counters_equal_charges(fresh_database)


def test_the_monthly_cost_limit_holds_under_the_same_race(
    fresh_database: DatabaseHandle, deployment: Deployment, today: Today
) -> None:
    _, micro = seeded(fresh_database, deployment, today)

    outcomes = race(fresh_database, deployment, limits(month_micro=4 * micro), today)

    assert sum(isinstance(o, Reservation) for o in outcomes) == 3
    assert outcomes.count(BudgetRefusal("tenant-cost-budget")) == RACERS - 3
    assert counters(fresh_database)[("cost-month", OCTOBER_FIRST)] == 4 * micro
    assert_counters_equal_charges(fresh_database)


def test_racers_on_a_period_with_no_row_yet_still_cannot_pass_the_limit(
    fresh_database: DatabaseHandle, deployment: Deployment, today: Today
) -> None:
    tokens = reserved_tokens()

    outcomes = race(fresh_database, deployment, limits(day=3 * tokens), today)

    assert sum(isinstance(o, Reservation) for o in outcomes) == 3
    assert counters(fresh_database)[("tokens-day", OCTOBER_FIRST)] == 3 * tokens
    assert_counters_equal_charges(fresh_database)


def test_racers_refused_on_either_counter_leave_counters_that_add_up(
    fresh_database: DatabaseHandle, deployment: Deployment, today: Today
) -> None:
    tokens, micro = seeded(fresh_database, deployment, today)
    window = limits(day=5 * tokens, month_micro=3 * micro)

    outcomes = race(fresh_database, deployment, window, today)

    assert sum(isinstance(o, Reservation) for o in outcomes) == 2
    assert_counters_equal_charges(fresh_database)


# ── periods ─────────────────────────────────────────────────────────────────
def test_a_new_day_gives_the_tenant_a_new_day_row_and_the_month_carries_on(
    ledger: Ledger,
    fresh_database: DatabaseHandle,
    deployment: Deployment,
    today: Today,
) -> None:
    tokens, micro = reserved_tokens(), reserved_micro(deployment)
    window = limits(day=tokens)
    reserved(ledger, deployment, window=window)
    assert isinstance(reserve(ledger, deployment, window=window), BudgetRefusal)

    today.day = OCTOBER_FIRST + timedelta(days=1)
    reserved(ledger, deployment, window=window)

    assert counters(fresh_database) == {
        ("tokens-day", OCTOBER_FIRST): tokens,
        ("tokens-day", date(2026, 10, 2)): tokens,
        ("cost-month", OCTOBER_FIRST): 2 * micro,
    }
    assert_counters_equal_charges(fresh_database)


def test_a_new_month_starts_both_counters_at_zero(
    ledger: Ledger,
    fresh_database: DatabaseHandle,
    deployment: Deployment,
    today: Today,
) -> None:
    tokens, micro = reserved_tokens(), reserved_micro(deployment)
    window = limits(day=tokens, month_micro=micro)
    today.day = OCTOBER_LAST
    reserved(ledger, deployment, window=window)
    assert isinstance(reserve(ledger, deployment, window=window), BudgetRefusal)

    today.day = date(2026, 11, 1)
    reserved(ledger, deployment, window=window)

    assert counters(fresh_database) == {
        ("tokens-day", OCTOBER_LAST): tokens,
        ("cost-month", OCTOBER_FIRST): micro,
        ("tokens-day", date(2026, 11, 1)): tokens,
        ("cost-month", date(2026, 11, 1)): micro,
    }
    assert_counters_equal_charges(fresh_database)


def test_settling_after_midnight_corrects_the_day_the_call_was_charged_to(
    ledger: Ledger,
    fresh_database: DatabaseHandle,
    deployment: Deployment,
    today: Today,
) -> None:
    reservation = reserved(ledger, deployment)
    today.day = OCTOBER_FIRST + timedelta(days=1)

    ledger.settle(reservation, deployment, 60, 40)

    assert counters(fresh_database) == {
        ("tokens-day", OCTOBER_FIRST): 100,
        ("cost-month", OCTOBER_FIRST): cost_micro_eur(
            deployment.price, EXCHANGE, 60, 40
        ),
    }
    assert_counters_equal_charges(fresh_database)


def test_releasing_after_the_month_turned_corrects_the_month_it_was_charged_to(
    ledger: Ledger,
    fresh_database: DatabaseHandle,
    deployment: Deployment,
    today: Today,
) -> None:
    today.day = OCTOBER_LAST
    reservation = reserved(ledger, deployment)
    today.day = date(2026, 11, 1)

    ledger.release(reservation)

    assert counters(fresh_database) == {
        ("tokens-day", OCTOBER_LAST): 0,
        ("cost-month", OCTOBER_FIRST): 0,
    }
    assert_counters_equal_charges(fresh_database)


# ── tenants ─────────────────────────────────────────────────────────────────
def test_two_tenants_do_not_share_counters(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    window = limits(day=reserved_tokens())
    reserved(ledger, deployment, window=window, who=caller(TENANT))
    assert isinstance(
        reserve(ledger, deployment, window=window, who=caller(TENANT)), BudgetRefusal
    )

    reserved(ledger, deployment, window=window, who=caller(OTHER_TENANT))

    assert counters(fresh_database, TENANT) == counters(fresh_database, OTHER_TENANT)
    assert len(usage_rows(fresh_database, TENANT)) == 1
    assert len(usage_rows(fresh_database, OTHER_TENANT)) == 1


# ── the invariant ───────────────────────────────────────────────────────────
def test_counters_equal_charges_after_a_mixed_sequence(
    ledger: Ledger,
    fresh_database: DatabaseHandle,
    deployment: Deployment,
    today: Today,
) -> None:
    window = limits(day=reserved_tokens() * 4)
    held = [reserved(ledger, deployment, window=window) for _ in range(4)]
    assert isinstance(reserve(ledger, deployment, window=window), BudgetRefusal)
    ledger.settle(held[0], deployment, 11, 5)
    ledger.release(held[1])
    ledger.keep(held[2])
    ledger.settle(held[2], deployment, 1, 1)  # closed already: nothing
    today.day = OCTOBER_FIRST + timedelta(days=1)
    reserved(ledger, deployment, window=window, who=caller(OTHER_TENANT))
    ledger.settle(held[3], deployment, 2, 2)  # after midnight
    reserved(ledger, deployment, window=window)

    assert_counters_equal_charges(fresh_database)
    states = [row[1] for row in usage_rows(fresh_database)]
    assert sorted(states) == ["kept", "released", "reserved", "settled", "settled"]


# ── the database is down, or unusable ───────────────────────────────────────
def test_a_database_that_is_down_makes_reserve_raise_and_return_nothing(
    fresh_database: DatabaseHandle, deployment: Deployment, today: Today
) -> None:
    closed_port = make_conninfo(fresh_database.dsn("model_gateway"), port=1)
    ledger = Ledger(closed_port, exchange=EXCHANGE, today=today)

    with pytest.raises(psycopg.Error):
        reserve(ledger, deployment)

    assert usage_rows(fresh_database) == []
    assert counters(fresh_database) == {}


def test_a_database_without_the_tables_makes_reserve_raise(
    empty_database: DatabaseHandle, deployment: Deployment, today: Today
) -> None:
    ledger = make_ledger(empty_database, today)

    with pytest.raises(psycopg.Error):
        reserve(ledger, deployment)

    assert owner_rows(
        empty_database,
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'gateway'",
    ) == [(0,)]


@pytest.mark.parametrize("how", CLOSERS)
def test_a_database_that_is_down_makes_closing_raise(
    ledger: Ledger,
    fresh_database: DatabaseHandle,
    deployment: Deployment,
    today: Today,
    how: str,
) -> None:
    reservation = reserved(ledger, deployment)
    closed_port = make_conninfo(fresh_database.dsn("model_gateway"), port=1)
    down = Ledger(closed_port, exchange=EXCHANGE, today=today)

    with pytest.raises(psycopg.Error):
        close(down, how, reservation, deployment)

    assert usage_rows(fresh_database)[0][1] == "reserved"


# ── a counter that is not there, or not one ─────────────────────────────────
def owner_executes(db: DatabaseHandle, statement: str) -> None:
    with connect(db.dsn(OWNER), "test-write") as conn:
        conn.execute(statement)


@pytest.mark.parametrize("how", ["settle", "release"])
def test_closing_with_a_missing_counter_row_raises_and_leaves_the_row_reserved(
    ledger: Ledger,
    fresh_database: DatabaseHandle,
    deployment: Deployment,
    how: str,
) -> None:
    reservation = reserved(ledger, deployment)
    owner_executes(fresh_database, "DELETE FROM gateway.budget_counters")

    with pytest.raises(LedgerInconsistent) as raised:
        close(ledger, how, reservation, deployment)

    assert usage_rows(fresh_database)[0][1] == "reserved"
    assert owner_rows(
        fresh_database, "SELECT closed_at IS NULL FROM gateway.usage"
    ) == [(True,)]
    assert str(raised.value) == str(LedgerInconsistent())  # a fixed message
    assert TENANT not in str(raised.value)


def test_keep_touches_no_counter_so_a_missing_row_does_not_stop_it(
    ledger: Ledger, fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    reservation = reserved(ledger, deployment)
    owner_executes(fresh_database, "DELETE FROM gateway.budget_counters")

    ledger.keep(reservation)

    assert usage_rows(fresh_database)[0][1] == "kept"


def test_the_counters_equal_charges_less_credits_after_gateway_calls_and_upkeep(
    ledger: Ledger,
    fresh_database: DatabaseHandle,
    deployment: Deployment,
    today: Today,
) -> None:
    # The upkeep credits by the database's clock, so the gateway's day is that.
    ((today.day,),) = owner_rows(
        fresh_database, "SELECT (now() AT TIME ZONE 'UTC')::date"
    )
    settled, kept, released = (reserved(ledger, deployment) for _ in range(3))
    ledger.settle(settled, deployment, 10, 5)
    ledger.keep(kept)
    ledger.release(released)
    # A reservation a dead process left, found thirty minutes later: released.
    stale = plant_usage(fresh_database, tenant=TENANT, tokens=40, micro_eur=9)
    run_as(fresh_database, UPKEEP_ROLE, CLOSE, (stale, True, "dead-process"))
    still_open = reserved(ledger, deployment)
    run_as(fresh_database, UPKEEP_ROLE, CREDIT, (TENANT, "tokens-day", 3, "goodwill"))
    run_as(fresh_database, UPKEEP_ROLE, CREDIT, (TENANT, "cost-month", 2, "goodwill"))
    ledger.settle(still_open, deployment, 20, 10)
    # Another one, kept: the charge stays and no counter moves.
    stale_kept = plant_usage(fresh_database, tenant=TENANT, tokens=11, micro_eur=4)
    run_as(fresh_database, UPKEEP_ROLE, CLOSE, (stale_kept, False, "dead-process"))

    assert_counters_equal_charges(fresh_database)

    assert owner_rows(
        fresh_database, "SELECT kind, amount FROM gateway.credits ORDER BY kind"
    ) == [("cost-month", 2), ("tokens-day", 3)]
    states = {row[0]: row[1] for row in usage_rows(fresh_database)}
    assert states[stale] == "released"
    assert states[stale_kept] == "kept"


def test_a_settle_equal_to_the_reservation_adjusts_nothing(
    ledger: Ledger,
    fresh_database: DatabaseHandle,
    deployment: Deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.platform.gateway import budget

    statements: list[str] = []
    real = budget.connect

    def spying(dsn: str, application_name: str):
        conn = real(dsn, application_name)
        original = conn.execute

        def execute(query, params=None, **kwargs):
            statements.append(str(query))
            return original(query, params, **kwargs)

        conn.execute = execute  # type: ignore[method-assign]
        return conn

    reservation = reserved(ledger, deployment)
    request = text_request()
    monkeypatch.setattr(budget, "connect", spying)

    ledger.settle(
        reservation,
        deployment,
        estimate_input_tokens(request),
        request.max_output_tokens,
    )

    assert not any(statement == budget.ADJUST_COUNTER for statement in statements)
    assert any(statement == budget.CLOSE_USAGE for statement in statements)
