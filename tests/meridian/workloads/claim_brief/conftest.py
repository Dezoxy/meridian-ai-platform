"""The world of a claim-brief test: a migrated database of its own, the real tool
servers, a stub gateway and a ``Running`` run, as ``hostsupport`` builds them.

Teardown holds the workload to the rule of its contract: it moves no claim's
state and writes nothing to the claim, so the claim's whole row is what it was
when the test began, whatever legs the test ran.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from claimbriefsupport import claim_row
from dbsupport import DatabaseHandle
from hostsupport import BriefWorld, make_world


@pytest.fixture
def world(
    fresh_database: DatabaseHandle, plant: Callable[..., Path]
) -> Iterator[BriefWorld]:
    brief_world = make_world(fresh_database, plant)
    before = claim_row(brief_world)

    yield brief_world

    assert claim_row(brief_world) == before, "a leg changed the claim's row"
