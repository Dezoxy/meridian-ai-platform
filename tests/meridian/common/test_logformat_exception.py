"""The ``exception`` field is redacted, then bounded (S064, T-03, L1 of the
second review).

The field holds frames and class names and no message, but a frame prints its
source line, so a literal in the code (a message in a ``raise``) goes out as
written. The field is run through the same ``redact`` as the access path and cut
at ``EXCEPTION_MAX_CHARS`` with the path's marker. The records are made by the
real logger and read back from standard output; the conftest puts the root's and
uvicorn's handlers back after every test.
"""

import json
import logging
from typing import Any

import pytest

from meridian.platform.common.logformat import (
    EXCEPTION_MAX_CHARS,
    PATH_CUT_MARKER,
    configure_logging,
)
from meridian.platform.common.logredaction import install_log_redaction

SERVICE = "model-gateway"
EMAIL = "ana.kovacs@example.com"


@pytest.fixture(autouse=True)
def configured() -> None:
    install_log_redaction()
    configure_logging(SERVICE)


def _logged_class_named(
    capsys: pytest.CaptureFixture[str], name_length: int
) -> tuple[str, dict[str, Any]]:
    """One record whose exception is an instance of a class with a name
    ``name_length`` characters long (never raised, so the field is that class's
    qualified name alone). Returns the raw output and the parsed line."""
    error = type("E" * name_length, (Exception,), {})
    exc_info = (error, error(), None)

    logging.getLogger("meridian.test").error("failed", exc_info=exc_info)

    out = capsys.readouterr().out
    return out, json.loads(out)


def _qualified_prefix(capsys: pytest.CaptureFixture[str]) -> int:
    """The characters the module's name adds before a class's own name in the
    field."""
    _, probe = _logged_class_named(capsys, 1)
    return len(probe["exception"]) - 1


def test_a_source_line_holding_an_address_arrives_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> None:
        raise ValueError("write to ana.kovacs@example.com")  # synthetic address

    try:
        fail()
    except ValueError:
        logging.getLogger("meridian.test").exception("failed")

    out = capsys.readouterr().out
    text = json.loads(out)["exception"]
    assert EMAIL not in out
    assert 'raise ValueError("write to [email]")' in text
    assert text.splitlines()[-1] == "ValueError"


def test_a_field_over_the_bound_is_cut_with_the_marker_and_is_still_one_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out, line = _logged_class_named(capsys, EXCEPTION_MAX_CHARS * 2)

    assert out.count("\n") == 1
    text = line["exception"]
    assert len(text) == EXCEPTION_MAX_CHARS + len(PATH_CUT_MARKER)
    assert text.endswith(PATH_CUT_MARKER)
    assert line["message"] == "failed"


def test_a_field_exactly_at_the_bound_is_not_cut(
    capsys: pytest.CaptureFixture[str],
) -> None:
    prefix = _qualified_prefix(capsys)

    _, line = _logged_class_named(capsys, EXCEPTION_MAX_CHARS - prefix)

    assert len(line["exception"]) == EXCEPTION_MAX_CHARS
    assert PATH_CUT_MARKER not in line["exception"]


def test_a_field_one_over_the_bound_is_cut(
    capsys: pytest.CaptureFixture[str],
) -> None:
    prefix = _qualified_prefix(capsys)

    _, line = _logged_class_named(capsys, EXCEPTION_MAX_CHARS - prefix + 1)

    assert line["exception"].endswith(PATH_CUT_MARKER)
    assert len(line["exception"]) == EXCEPTION_MAX_CHARS + len(PATH_CUT_MARKER)


def test_an_address_that_crosses_the_bound_is_redacted_before_the_cut(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The class's name ends in an address that starts five characters before the
    # bound and ends after it: cutting first would leave the front of it.
    prefix = _qualified_prefix(capsys)
    name = "E" * (EXCEPTION_MAX_CHARS - prefix - 6) + f" {EMAIL}"
    error = type(name, (Exception,), {})

    logging.getLogger("meridian.test").error("failed", exc_info=(error, error(), None))

    out = capsys.readouterr().out
    assert "ana." not in out
    assert "@example" not in out
    assert json.loads(out)["exception"].endswith(PATH_CUT_MARKER)


def test_a_short_field_is_written_as_it_was(
    capsys: pytest.CaptureFixture[str],
) -> None:
    try:
        raise KeyError("canary-key-1a2b")
    except KeyError:
        logging.getLogger("meridian.test").exception("failed")

    text = json.loads(capsys.readouterr().out)["exception"]

    assert text.startswith("Traceback (most recent call last):\n")
    assert 'raise KeyError("canary-key-1a2b")' in text
    assert text.splitlines()[-1] == "KeyError"
    assert PATH_CUT_MARKER not in text
