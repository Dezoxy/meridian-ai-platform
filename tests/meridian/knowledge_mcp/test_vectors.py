"""``usable_vector``: which vectors the database can compare (S012).

The unit tests pin the rule. The database tests are the evidence for it: what
pgvector 0.8.6 does with the vectors the rule refuses and with those just
inside it, so a change of image that changes the database shows here.
"""

import math

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.knowledge_mcp.store import (
    FLOAT32_MAX,
    FLOAT32_MIN_NORMAL,
    PGVECTOR_MAX_DIMENSIONS,
    usable_vector,
    vector_literal,
)

# Not parallel to any vector below, so a distance of 0 or 1 is plainly wrong.
REFERENCE = (1.0, 2.0)

USABLE = [
    pytest.param((1.0,), id="one-component"),
    pytest.param((0.25, -0.5, 1.0), id="ordinary"),
    pytest.param((1.0,) * PGVECTOR_MAX_DIMENSIONS, id="the-longest-pgvector-holds"),
    pytest.param((1.1e-19, 0.0), id="just-above-the-smallest-norm"),
    pytest.param((1.8e19, 0.0), id="just-below-the-largest-norm"),
    pytest.param((1e-50, 1.0), id="one-component-underflows-and-the-rest-is-fine"),
    pytest.param((0.0, 0.0, 1e-3), id="zeros-beside-a-component"),
    pytest.param((-3e-19, 0.0), id="negative"),
]
REFUSED = [
    pytest.param((), id="empty"),
    pytest.param((1.0,) * (PGVECTOR_MAX_DIMENSIONS + 1), id="longer-than-pgvector"),
    pytest.param((0.0, 0.0, 0.0), id="all-zero"),
    pytest.param((1e-50, 1e-50, 1e-50), id="every-component-underflows-to-zero"),
    pytest.param((1e-46, 0.0), id="below-the-smallest-float32"),
    pytest.param((1e-23, 1e-23), id="norm-underflows"),
    pytest.param((1e-20, 0.0), id="norm-is-a-denormal"),
    pytest.param((1.9e19, 0.0), id="norm-overflows"),
    pytest.param((3e38, 3e38), id="norm-overflows-by-a-lot"),
    pytest.param((1.0, 1e39), id="component-beyond-float32"),
    pytest.param((-1e39, 1.0), id="negative-component-beyond-float32"),
    pytest.param((1.0, math.nan), id="nan"),
    pytest.param((1.0, math.inf), id="infinity"),
    pytest.param((-math.inf, 1.0), id="negative-infinity"),
]


@pytest.mark.parametrize("vector", USABLE)
def test_a_vector_inside_the_rule_is_usable(vector: tuple[float, ...]) -> None:
    assert usable_vector(vector) is True


@pytest.mark.parametrize("vector", REFUSED)
def test_a_vector_outside_the_rule_is_not_usable(vector: tuple[float, ...]) -> None:
    assert usable_vector(vector) is False


def test_the_rule_is_the_float32_range_of_the_norm() -> None:
    assert pytest.approx(1.17549435e-38) == FLOAT32_MIN_NORMAL
    assert pytest.approx(3.40282347e38) == FLOAT32_MAX
    assert PGVECTOR_MAX_DIMENSIONS == 16_000
    # The rule is on the sum of squares: one component at the edge of each.
    assert usable_vector((math.sqrt(FLOAT32_MIN_NORMAL) * 1.0001,))
    assert not usable_vector((math.sqrt(FLOAT32_MIN_NORMAL) * 0.9999,))
    assert usable_vector((math.sqrt(FLOAT32_MAX) * 0.9999,))
    assert not usable_vector((math.sqrt(FLOAT32_MAX) * 1.0001,))


def _true_distance(vector: tuple[float, ...]) -> float | None:
    """The cosine distance of ``vector`` from ``REFERENCE`` in exact enough
    arithmetic (scaled first, so no square under- or overflows); None for a
    vector that points nowhere."""
    scale = max(abs(c) for c in vector)
    if scale == 0:
        return None
    unit = [c / scale for c in vector]
    dot = sum(a * b for a, b in zip(unit, REFERENCE, strict=True))
    norms = math.hypot(*unit) * math.hypot(*REFERENCE)
    return 1 - dot / norms


def _database_distance(db: DatabaseHandle, vector: tuple[float, ...]) -> float | None:
    """What the database answers for the cosine distance of ``vector`` from
    ``REFERENCE``; None when it refuses to read the literal."""
    with connect(db.dsn(OWNER), "test-vectors") as conn:
        try:
            ((value,),) = conn.execute(
                "SELECT %s::vector <=> %s::vector",
                (vector_literal(vector), vector_literal(REFERENCE)),
            ).fetchall()
        except psycopg.DataError:
            return None
        return value


@pytest.mark.parametrize(
    "vector",
    [
        pytest.param((1.1e-19, 1.1e-19), id="just-above-the-smallest-norm"),
        pytest.param((1.1e-19, 0.0), id="just-above-the-smallest-norm-on-an-axis"),
        pytest.param((1.3e19, 1.3e19), id="just-below-the-largest-norm"),
        pytest.param((1.8e19, 0.0), id="just-below-the-largest-norm-on-an-axis"),
        pytest.param((1.0, 1.0), id="ordinary"),
    ],
)
def test_the_database_compares_a_usable_vector_correctly(
    fresh_database: DatabaseHandle, vector: tuple[float, ...]
) -> None:
    assert usable_vector(vector)

    distance = _database_distance(fresh_database, vector)

    assert distance == pytest.approx(_true_distance(vector), abs=1e-6)


@pytest.mark.parametrize(
    "vector",
    [
        pytest.param((0.0, 0.0), id="all-zero"),
        pytest.param((1e-50, 1e-50), id="every-component-underflows-to-zero"),
        pytest.param((1e-23, 1e-23), id="norm-underflows"),
        pytest.param((1.9e19, 1.9e19), id="norm-overflows"),
        pytest.param((3e38, 3e38), id="norm-overflows-by-a-lot"),
        pytest.param((1e39, 1.0), id="component-beyond-float32"),
    ],
)
def test_the_database_cannot_compare_a_refused_vector(
    fresh_database: DatabaseHandle, vector: tuple[float, ...]
) -> None:
    assert not usable_vector(vector)

    distance = _database_distance(fresh_database, vector)
    truth = _true_distance(vector)

    # The literal is refused, the distance is NaN, or it is not the distance.
    assert (
        distance is None or math.isnan(distance) or abs(distance - truth) > 1e-3  # type: ignore[operator]
    )
