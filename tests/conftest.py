"""Shards of the suite for CI (S074).

``MERIDIAN_TEST_SHARDS`` is the number of shards and ``MERIDIAN_TEST_SHARD`` the
one this run keeps, counted from 1. A test belongs to the shard its node id
hashes to (``zlib.crc32``, which does not change from one process to the next as
Python's own ``hash`` does), so a run on any machine, and every pytest-xdist
worker of it, splits the suite the same way. Neither variable set means the
whole suite, as a developer runs it. One set without the other, a count of zero,
a shard above the count or a value that is no whole number stops the run as a
usage error (exit status 4): a shard that silently kept the wrong tests, or all
of them, is the failure this must not allow. A shard that keeps nothing ends
with pytest's exit status 5.

This file is stdlib and pytest only: it is loaded before any fixture, and every
test under ``tests/meridian`` and ``tests/synthetic`` loads it.
``tests/meridian/test_test_sharding.py`` proves the shards are disjoint and
together are the whole collection.
"""

import os
import zlib

import pytest

SHARD_ENV = "MERIDIAN_TEST_SHARD"
SHARDS_ENV = "MERIDIAN_TEST_SHARDS"


def _whole_number(name: str, value: str) -> int:
    if not (value.isascii() and value.isdigit()):
        raise pytest.UsageError(f"{name}={value!r} is not a whole number")
    return int(value)


def selection(environ: dict[str, str] | os._Environ[str]) -> tuple[int, int] | None:
    """The (shard, shards) the environment names, or None for the whole suite."""
    have_shard, have_shards = SHARD_ENV in environ, SHARDS_ENV in environ
    if not have_shard and not have_shards:
        return None
    if have_shard != have_shards:
        missing = SHARDS_ENV if have_shard else SHARD_ENV
        raise pytest.UsageError(
            f"{SHARD_ENV} and {SHARDS_ENV} are set together or not at all: "
            f"{missing} is missing"
        )
    shard = _whole_number(SHARD_ENV, environ[SHARD_ENV])
    shards = _whole_number(SHARDS_ENV, environ[SHARDS_ENV])
    if shards < 1:
        raise pytest.UsageError(f"{SHARDS_ENV}={shards} must be at least 1")
    if not 1 <= shard <= shards:
        raise pytest.UsageError(
            f"{SHARD_ENV}={shard} must be from 1 to {SHARDS_ENV}={shards}"
        )
    return shard, shards


def shard_of(node_id: str, shards: int) -> int:
    """The shard (from 1) a test belongs to."""
    return zlib.crc32(node_id.encode("utf-8")) % shards + 1


def pytest_configure(config: pytest.Config) -> None:
    # Early, so a wrong value stops the run before 22,000 tests are collected.
    selection(os.environ)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    chosen = selection(os.environ)
    if chosen is None:
        return
    shard, shards = chosen
    kept = [item for item in items if shard_of(item.nodeid, shards) == shard]
    dropped = [item for item in items if shard_of(item.nodeid, shards) != shard]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
