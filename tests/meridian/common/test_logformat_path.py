"""The access line's path: unquoted to a fixed point, and no userinfo (S069, B15, T-03).

A path is unquoted until it stops changing, at most ``PATH_UNQUOTE_ROUNDS``
times, so an address encoded twice is redacted as one encoded once is; a path
that a further round would still change is the fixed word and nothing of it. An
absolute-form request target (``scheme://user@host/...``) loses its userinfo
before anything is read. The records are made by the real logger and read back
from standard output.
"""

import json
import logging
from typing import Any
from urllib.parse import quote

import pytest

from meridian.platform.common import logformat
from meridian.platform.common.logformat import configure_logging
from meridian.platform.common.logredaction import install_log_redaction

SERVICE = "model-gateway"
UVICORN_TEMPLATE = '%s - "%s %s HTTP/%s" %d'
CLIENT = "203.0.113.7:51234"
OPERATOR = "operator-canary"
WORD = "word-canary"


@pytest.fixture(autouse=True)
def configured() -> None:
    install_log_redaction()
    configure_logging(SERVICE)


def _logged(
    capsys: pytest.CaptureFixture[str], target: str
) -> tuple[str, dict[str, Any]]:
    logging.getLogger("uvicorn.access").info(
        UVICORN_TEMPLATE, CLIENT, "GET", target, "1.1", 200
    )
    out = capsys.readouterr().out
    return out, json.loads(out)


def _encoded(text: str, rounds: int) -> str:
    for _ in range(rounds):
        text = quote(text, safe="")
    return text


def test_an_address_encoded_twice_is_redacted_as_one_encoded_once_is(
    capsys: pytest.CaptureFixture[str],
) -> None:
    once, line_once = _logged(capsys, "/u/anna.kovacs%40example.com")
    twice, line_twice = _logged(capsys, "/u/anna.kovacs%2540example.com")

    assert line_once["path"] == "/u/[email]"
    assert line_twice["path"] == line_once["path"]
    assert line_twice["message"] == "GET /u/[email] HTTP/1.1 200"
    assert "anna.kovacs" not in once + twice


def test_a_path_encoded_three_times_is_unquoted_and_one_encoded_four_times_is_withheld(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert logformat.PATH_UNQUOTE_ROUNDS == 3
    three = "/" + _encoded("a b", 3)
    four = "/" + _encoded("a b", 4)

    _, line_three = _logged(capsys, three)
    _, line_four = _logged(capsys, four)

    assert line_three["path"] == "/a b"
    assert line_four["path"] == logformat.PATH_OVER_ENCODED
    assert line_four["message"] == f"GET {logformat.PATH_OVER_ENCODED} HTTP/1.1 200"


def test_an_address_encoded_four_times_does_not_reach_the_line_in_any_form(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out, line = _logged(capsys, "/u/" + _encoded("anna.kovacs@example.com", 4))

    assert line["path"] == logformat.PATH_OVER_ENCODED
    assert "anna" not in out
    assert "kovacs" not in out


@pytest.mark.parametrize(
    ("target", "path"),
    [
        # A host with no dot is no address: the userinfo is cut at its "@".
        (f"http://{OPERATOR}@host/claims/CLM-0001", "http://host/claims/CLM-0001"),
        (
            f"http://{OPERATOR}:{WORD}@host:8080/claims/CLM-0001",
            "http://host:8080/claims/CLM-0001",
        ),
        # A dotted host after a word and an "@" was redacted as an address when
        # the record was made: the user name before the placeholder goes.
        (
            f"http://{OPERATOR}:{WORD}@host.example/claims/CLM-0001",
            "http://[email]/claims/CLM-0001",
        ),
        (
            f"https://{OPERATOR}%40mail:{WORD}@host.example/claims/CLM-0001?q=1",
            "https://[email]/claims/CLM-0001",
        ),
    ],
)
def test_an_absolute_form_target_loses_its_userinfo(
    capsys: pytest.CaptureFixture[str], target: str, path: str
) -> None:
    out, line = _logged(capsys, target)

    assert line["path"] == path
    assert line["message"] == f"GET {path} HTTP/1.1 200"
    assert OPERATOR not in out
    assert WORD not in out


def test_the_word_for_an_over_encoded_path_is_a_short_bracketed_word() -> None:
    assert logformat.PATH_OVER_ENCODED == "[encoded]"


@pytest.mark.parametrize("rounds", [1, 2])
def test_an_absolute_form_target_with_its_scheme_encoded_loses_its_userinfo(
    capsys: pytest.CaptureFixture[str], rounds: int
) -> None:
    target = (
        _encoded("http://", rounds)
        + _encoded(f"{OPERATOR}:{WORD}@", rounds)
        + "host.example/claims/CLM-0001"
    )

    out, line = _logged(capsys, target)

    assert line["path"] == "http://host.example/claims/CLM-0001"
    assert OPERATOR not in out
    assert WORD not in out


def test_a_user_name_without_a_password_behind_an_encoded_scheme_is_cut(
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = f"http%3A//{OPERATOR}%40host/claims/CLM-0001"

    out, line = _logged(capsys, target)

    assert line["path"] == "http://host/claims/CLM-0001"
    assert OPERATOR not in out


USERINFO_ANYWHERE = [
    # The security review's rows (S069 F1): the authority is not at the start.
    ("/go/http://alice:s3cret@host/x", "/go/http://host/x"),
    ("//alice:s3cret@host/x", "//host/x"),
    ("http:\\\\alice:pw@host\\x", "http:\\\\host\\x"),
    ("/x/https://alice:pw@host/y", "/x/https://host/y"),
    # The userinfo ends at the last "@" of the segment.
    ("/go/http://alice@bob:pw@host/x", "/go/http://host/x"),
]


@pytest.mark.parametrize(
    "quoted", [False, True], ids=["as sent by a client", "as uvicorn quotes it"]
)
@pytest.mark.parametrize(("target", "path"), USERINFO_ANYWHERE)
def test_a_user_name_does_not_survive_wherever_the_authority_sits(
    capsys: pytest.CaptureFixture[str], target: str, path: str, quoted: bool
) -> None:
    sent = quote(target, safe="") if quoted else target

    out, line = _logged(capsys, sent)

    assert line["path"] == path
    assert "alice" not in out
    assert "s3cret" not in out


@pytest.mark.parametrize(
    ("quoted", "path"),
    [
        # The record's redaction made a placeholder of the password and the
        # host, and the user name before it goes with the rest.
        (False, "/go/http://[email]/x"),
        # Quoted, the "@" is hidden from that redaction: the cut finds it.
        (True, "/go/http://host.example/x"),
    ],
)
def test_a_user_name_before_a_dotted_host_does_not_survive_either(
    capsys: pytest.CaptureFixture[str], quoted: bool, path: str
) -> None:
    target = "/go/http://alice:s3cret@host.example/x"
    sent = quote(target, safe="") if quoted else target

    out, line = _logged(capsys, sent)

    assert line["path"] == path
    assert "alice" not in out
    assert "s3cret" not in out


def test_an_authority_that_a_slash_ends_before_the_at_sign_is_left_as_it_is(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # What a pattern cannot tell from a path with an "@" in a segment: the
    # authority of this target is ``alice:pa`` and ``ss@host`` is a path.
    _, line = _logged(capsys, "http://alice:pa/ss@host/x")

    assert line["path"] == "http://alice:pa/ss@host/x"


@pytest.mark.parametrize(
    "target",
    ["/claims/a@b", "/claims/CLM-0001/a@b/c", "/files/a@b/c@d"],
)
def test_an_at_sign_in_a_plain_path_segment_stays(
    capsys: pytest.CaptureFixture[str], target: str
) -> None:
    _, line = _logged(capsys, target)

    assert line["path"] == target


def test_an_at_sign_in_the_path_of_an_absolute_form_target_is_not_userinfo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, line = _logged(capsys, "http://host.example/claims/a@b")

    assert line["path"] == "http://host.example/claims/a@b"


@pytest.mark.parametrize(
    ("target", "path"),
    [
        ("/claims/CLM-0001", "/claims/CLM-0001"),
        ("/claims/CLM-0001?q=1", "/claims/CLM-0001"),
        ("/files/a%20b", "/files/a b"),
        ("/files/a@b", "/files/a@b"),
    ],
)
def test_a_plain_path_and_a_path_with_one_legitimate_escape_log_as_before(
    capsys: pytest.CaptureFixture[str], target: str, path: str
) -> None:
    _, line = _logged(capsys, target)

    assert line["path"] == path
