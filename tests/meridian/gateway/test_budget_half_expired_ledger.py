"""The ledger's expiry in batches leaves the gateway's decisions alone (S068).

Between two calls of ``gateway.expire_ledger_batch`` the counters of the past
months stand without all their usage rows. Nothing the gateway decides may read a
past period: a call in the current period is admitted, charged and settled the
same whether the tenant's past is whole, half expired or absent.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from dbsupport import OWNER, DatabaseHandle
from ledgerbatchsupport import batch, plant_ledger
from servicesupport import REGISTRY_DIR
from upkeepsupport import previous_month, run, utc_day, utc_month

from meridian.platform.gateway.budget import (
    MICRO,
    Caller,
    Ledger,
    Reservation,
    chat_estimate,
)
from meridian.platform.gateway.models import ChatRequest, Message
from meridian.platform.registry import load_registry
from meridian.platform.registry.models import Deployment, ExchangeRate, TenantLimits

HALF_EXPIRED_TENANT = "ledger-tenant-1"
UNTOUCHED_TENANT = "ledger-tenant-9"
EXCHANGE = ExchangeRate(
    usd_per_eur=Decimal("1.1355"), source="a test fixture", checked=date(2026, 9, 30)
)
A_HUGE_PAST_COUNTER = 10**12
CURRENT_COUNTERS = (
    "SELECT kind, amount FROM gateway.budget_counters "
    "WHERE tenant = %s AND period_start = ANY(%s) ORDER BY kind"
)
PAST_COUNTERS = (
    "SELECT kind, period_start, amount FROM gateway.budget_counters "
    "WHERE tenant = %s AND period_start < %s ORDER BY kind, period_start"
)


@pytest.fixture(scope="module")
def deployment() -> Deployment:
    found = load_registry(REGISTRY_DIR).deployment("aoai-sdc-gpt-4o")
    assert found is not None
    return found


def caller(tenant: str, number: int) -> Caller:
    return Caller(
        call_id=uuid.UUID(int=number),
        tenant=tenant,
        agent="claims-triage",
        run_id=uuid.UUID(int=1),
    )


def test_a_call_is_admitted_and_charged_the_same_with_a_half_expired_past_period(
    fresh_database: DatabaseHandle, deployment: Deployment
) -> None:
    db = fresh_database
    current, today = utc_month(db), utc_day(db)
    old = previous_month(current)
    plant_ledger(db, [old], rows=6)
    # The past counter of the first tenant is far above any limit: a decision that
    # read it would refuse the call.
    run(
        db,
        OWNER,
        "UPDATE gateway.budget_counters SET amount = amount + %s "
        "WHERE tenant = %s AND period_start = %s",
        (A_HUGE_PAST_COUNTER, HALF_EXPIRED_TENANT, old),
    )
    half = batch(db, current, 3)
    assert half == (3, 0, 0)
    past_before = run(db, OWNER, PAST_COUNTERS, (HALF_EXPIRED_TENANT, current))
    request = ChatRequest(
        messages=(Message(role="user", content="x" * 30),), max_output_tokens=100
    )
    estimate = chat_estimate(request)
    # The limits equal what the call reserves: admitted only if the current
    # period's counters start at zero.
    limits = TenantLimits(
        requests_per_10_seconds=10,
        tokens_per_minute=10_000,
        tokens_per_day=estimate.tokens,
        cost_per_month_eur=Decimal(10**6) / MICRO,
    )
    ledger = Ledger(db.dsn("model_gateway"), exchange=EXCHANGE, today=lambda: today)

    outcomes = []
    for number, tenant in enumerate((HALF_EXPIRED_TENANT, UNTOUCHED_TENANT), 1):
        reservation = ledger.reserve(
            caller(tenant, number), deployment, limits, estimate
        )
        assert isinstance(reservation, Reservation), (tenant, reservation)
        settled = ledger.settle(reservation, deployment, 10, 20)
        counters = run(db, OWNER, CURRENT_COUNTERS, (tenant, [today, current]))
        outcomes.append((reservation.tokens, reservation.micro_eur, settled, counters))

    assert outcomes[0] == outcomes[1]
    assert run(db, OWNER, PAST_COUNTERS, (HALF_EXPIRED_TENANT, current)) == past_before
