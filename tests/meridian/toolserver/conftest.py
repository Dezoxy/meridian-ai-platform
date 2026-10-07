"""Fixtures of the tool servers' meter tests: the world a call is made in (the
three files ``test_toolserver_meter_*.py`` that run real calls take it)."""

import pytest
from dbsupport import DatabaseHandle
from toolsupport import World, seed_world


@pytest.fixture
def world(fresh_database: DatabaseHandle) -> World:
    return seed_world(fresh_database)
