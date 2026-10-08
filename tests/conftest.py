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

``MERIDIAN_TEST_SHARD_REPORT`` names a file the shard writes when collection has
finished, as JSON: ``shard``, ``shards``, ``collected`` (the tests collected
before the selection), ``kept`` (the tests the run will run, counted after every
deselection, ``-k``, ``-m``, ``--deselect`` and ``--lf`` included) and ``digest``
(the SHA-256, in hex, of the sorted node ids of everything collected before the
selection, joined by newlines and encoded as UTF-8). CI's final job reads the
four reports and refuses unless every digest and every total is the same and the
kept counts add up to the total: that is the proof, on the runners, that the
shards together are the whole suite, and an option that drops a test breaks the
sum. The variable without a shard selection is a usage error.

This file is stdlib and pytest only: it is loaded before any fixture, and every
test under ``tests/meridian`` and ``tests/synthetic`` loads it.
``tests/meridian/test_test_sharding.py`` proves the selection on a synthetic
list and the hook and the report on a small file.
"""

import hashlib
import json
import os
import zlib
from collections.abc import Sequence
from pathlib import Path

import pytest

SHARD_ENV = "MERIDIAN_TEST_SHARD"
SHARDS_ENV = "MERIDIAN_TEST_SHARDS"
REPORT_ENV = "MERIDIAN_TEST_SHARD_REPORT"
# What the selection saw: (tests collected, digest of their ids), kept in the
# config from the selection to the end of collection, where the report is written.
FULL_LIST = pytest.StashKey[tuple[int, str]]()


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


def report_path(environ: dict[str, str] | os._Environ[str]) -> str | None:
    """The file the shard reports to, or None when no report is asked for."""
    if REPORT_ENV not in environ:
        return None
    path = environ[REPORT_ENV]
    if not path:
        raise pytest.UsageError(f"{REPORT_ENV} is set and names no file")
    if selection(environ) is None:
        raise pytest.UsageError(
            f"{REPORT_ENV} asks for a shard's report, and neither {SHARD_ENV} "
            f"nor {SHARDS_ENV} is set"
        )
    return path


def shard_of(node_id: str, shards: int) -> int:
    """The shard (from 1) a test belongs to."""
    return zlib.crc32(node_id.encode("utf-8")) % shards + 1


def in_shard(node_id: str, shard: int, shards: int) -> bool:
    """Whether the test is the shard's: exactly its own, never "up to" it."""
    return shard_of(node_id, shards) == shard


def digest_of(node_ids: Sequence[str]) -> str:
    """The SHA-256 (hex) of the sorted ids joined by newlines, as UTF-8."""
    return hashlib.sha256("\n".join(sorted(node_ids)).encode("utf-8")).hexdigest()


def write_report(
    path: str, shard: int, shards: int, collected: int, digest: str, kept: int
) -> None:
    """Write the shard's report; whole or not at all, whichever worker is last.

    Every pytest-xdist worker collects and selects, so each writes the same
    content. A temporary file of the worker's own, moved into place, leaves no
    half-written report to read.
    """
    report = {
        "shard": shard,
        "shards": shards,
        "collected": collected,
        "kept": kept,
        "digest": digest,
    }
    temporary = f"{path}.{os.getpid()}.tmp"
    Path(temporary).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def pytest_configure(config: pytest.Config) -> None:
    # Early, so a wrong value stops the run before 22,000 tests are collected.
    selection(os.environ)
    report_path(os.environ)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    chosen = selection(os.environ)
    if chosen is None:
        return
    shard, shards = chosen
    kept = [item for item in items if in_shard(item.nodeid, shard, shards)]
    dropped = [item for item in items if not in_shard(item.nodeid, shard, shards)]
    # The total and the digest are of everything collected, taken here, before
    # the selection; the report itself is written when collection has finished.
    ids = [item.nodeid for item in items]
    config.stash[FULL_LIST] = (len(ids), digest_of(ids))
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept


def pytest_collection_finish(session: pytest.Session) -> None:
    """Write the report, with the tests the run will really run as ``kept``.

    pytest's own ``-k``, ``-m``, ``--deselect`` and ``--lf`` deselect after this
    file's hook above, so a count taken there would still include the tests they
    drop. Here ``session.items`` is what is left after every one of them (and,
    under pytest-xdist, what this worker holds, which is what the run runs), so a
    run that drops a test adds up to less than the total and CI refuses it.
    """
    path = report_path(os.environ)
    chosen = selection(os.environ)
    full = session.config.stash.get(FULL_LIST, None)
    if path is None or chosen is None or full is None:
        return
    collected, digest = full
    write_report(path, chosen[0], chosen[1], collected, digest, len(session.items))
