"""What a tool server and its client agree on (S059): how long a call may take,
and the run it names.

The caller sends the time it has left in ``_meta``; the server reads it with
``call_budget_seconds`` and never trusts more than its own maximum. The run's
ID is read with ``run_id_of``.
"""

import math
import uuid
from typing import Any

import pytest

from meridian.platform.toolserver import wire
from meridian.platform.toolserver.wire import (
    MAX_CALL_SECONDS,
    META_RUN,
    META_TIMEOUT_MS,
    call_budget_seconds,
    run_id_of,
)

MAX_MILLISECONDS = 10_000


def test_the_most_a_server_works_on_one_call_is_ten_seconds() -> None:
    assert MAX_CALL_SECONDS == 10.0
    assert META_TIMEOUT_MS == "meridian/timeout-ms"


@pytest.mark.parametrize(
    ("sent", "seconds"),
    [(1, 0.001), (250, 0.25), (2_500, 2.5), (MAX_MILLISECONDS, MAX_CALL_SECONDS)],
)
def test_a_whole_number_of_milliseconds_up_to_the_maximum_is_the_budget(
    sent: int, seconds: float
) -> None:
    budget = call_budget_seconds({META_TIMEOUT_MS: sent})

    assert budget == seconds


@pytest.mark.parametrize(
    "sent",
    [
        MAX_MILLISECONDS + 1,
        60_000,
        10**15,
        # Too large for a float, so a division would raise: never raises.
        10**309,
        10**400,
    ],
)
def test_a_budget_over_the_maximum_is_the_maximum(sent: int) -> None:
    budget = call_budget_seconds({META_TIMEOUT_MS: sent})

    assert budget == MAX_CALL_SECONDS


@pytest.mark.parametrize(
    "meta",
    [
        pytest.param({}, id="absent"),
        pytest.param({"meridian/run": "x"}, id="another-key-only"),
        pytest.param({META_TIMEOUT_MS: None}, id="none"),
        pytest.param({META_TIMEOUT_MS: "2500"}, id="a-string"),
        pytest.param({META_TIMEOUT_MS: 2500.0}, id="a-float"),
        pytest.param({META_TIMEOUT_MS: math.nan}, id="nan"),
        pytest.param({META_TIMEOUT_MS: math.inf}, id="infinity"),
        pytest.param({META_TIMEOUT_MS: True}, id="true"),
        pytest.param({META_TIMEOUT_MS: False}, id="false"),
        pytest.param({META_TIMEOUT_MS: 0}, id="zero"),
        pytest.param({META_TIMEOUT_MS: -1}, id="negative"),
        pytest.param({META_TIMEOUT_MS: [2500]}, id="a-list"),
        pytest.param({META_TIMEOUT_MS: {"ms": 2500}}, id="a-mapping"),
    ],
)
def test_anything_that_is_not_a_positive_whole_number_is_the_maximum(
    meta: dict[str, Any],
) -> None:
    budget = call_budget_seconds(meta)

    assert budget == MAX_CALL_SECONDS


def test_the_run_a_call_names_is_read_when_it_is_a_uuid() -> None:
    run_id = uuid.uuid4()

    assert run_id_of({META_RUN: str(run_id)}) == run_id


@pytest.mark.parametrize(
    "meta",
    [
        pytest.param({}, id="absent"),
        pytest.param({META_RUN: None}, id="none"),
        pytest.param({META_RUN: "not-a-uuid"}, id="not-a-uuid"),
        pytest.param({META_RUN: ""}, id="empty"),
        pytest.param({META_RUN: uuid.uuid4()}, id="a-uuid-object"),
        pytest.param({META_RUN: 4900}, id="a-number"),
        pytest.param({META_RUN: ["x"]}, id="a-list"),
    ],
)
def test_anything_that_is_not_a_uuid_string_names_no_run(meta: dict[str, Any]) -> None:
    assert run_id_of(meta) is None


def test_the_maximum_is_read_when_the_budget_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wire, "MAX_CALL_SECONDS", 2.0)

    assert call_budget_seconds({META_TIMEOUT_MS: 1_500}) == 1.5
    assert call_budget_seconds({META_TIMEOUT_MS: 5_000}) == 2.0
    assert call_budget_seconds({}) == 2.0
