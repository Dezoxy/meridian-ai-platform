"""The JSON file reader the gateway and the evaluation share (S050, T-76).

It lives in ``common`` so that the gateway needs nothing from the evaluation
package. Its errors are fixed sentences that quote nothing of the file.
"""

import ast
import os
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from meridian.platform.common import jsonfile
from meridian.platform.common.jsonfile import (
    JsonFileError,
    describe_validation_error,
    parse_json,
    read_json_file,
    read_text_file,
)
from meridian.platform.evaluation.report import ReportError
from meridian.platform.gateway.providers.recorded import RecordingError

MARKER = "SECRET-MARKER-31c8"


def test_both_readers_of_a_file_raise_an_error_that_is_a_json_file_error() -> None:
    assert issubclass(ReportError, JsonFileError)
    assert issubclass(RecordingError, JsonFileError)
    assert issubclass(JsonFileError, ValueError)
    # Neither is the other: a caller that catches one does not catch the other.
    assert not issubclass(RecordingError, ReportError)
    assert not issubclass(ReportError, RecordingError)


def test_a_valid_document_is_parsed(tmp_path: Path) -> None:
    path = tmp_path / "any.json"
    path.write_text('{"a": [1, {"b": null}]}', encoding="utf-8")

    assert read_json_file(path) == {"a": [1, {"b": None}]}


def test_a_duplicate_key_at_any_depth_is_refused_without_naming_it() -> None:
    with pytest.raises(JsonFileError) as raised:
        parse_json(f'{{"a": {{"{MARKER}": 1, "{MARKER}": 2}}}}')

    assert str(raised.value) == "duplicate key"


@pytest.mark.parametrize(
    ("text", "sentence"),
    [
        ("{not json", "the file is not valid JSON"),
        ("", "the file is not valid JSON"),
        ("1" * 5000, "the file is not valid JSON"),
        ('{"a": ' + "[" * 100_000, "the JSON is nested too deeply"),
    ],
)
def test_a_text_that_is_not_json_is_refused_with_a_fixed_sentence(
    text: str, sentence: str
) -> None:
    with pytest.raises(JsonFileError) as raised:
        parse_json(text)

    assert str(raised.value) == sentence


def test_a_missing_file_a_directory_and_a_non_utf8_file_are_named_without_content(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'{"a": "\xff"}')

    with pytest.raises(JsonFileError, match="file not found"):
        read_text_file(tmp_path / "nope.json")
    with pytest.raises(JsonFileError, match="not a regular file"):
        read_text_file(tmp_path)
    with pytest.raises(JsonFileError, match="not valid UTF-8"):
        read_text_file(bad)


def test_a_named_pipe_is_refused_not_read(tmp_path: Path) -> None:
    pipe = tmp_path / "pipe.json"
    os.mkfifo(pipe)

    with pytest.raises(JsonFileError, match="not a regular file"):
        read_text_file(pipe)


def test_the_limit_is_five_mebibytes_and_a_file_at_it_is_read_but_one_byte_more_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "any.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    size = path.stat().st_size
    assert jsonfile.MAX_JSON_FILE_BYTES == 5 * 1024 * 1024

    monkeypatch.setattr(jsonfile, "MAX_JSON_FILE_BYTES", size)
    assert read_json_file(path) == {"a": 1}
    monkeypatch.setattr(jsonfile, "MAX_JSON_FILE_BYTES", size - 1)
    with pytest.raises(JsonFileError, match="too large"):
        read_json_file(path)


class Shape(BaseModel):
    count: int


def test_a_validation_error_is_described_by_paths_and_kinds_only() -> None:
    with pytest.raises(ValidationError) as raised:
        Shape.model_validate({"count": MARKER})

    text = describe_validation_error(raised.value)

    assert text == "count: int_parsing"
    assert MARKER not in text


def test_the_key_of_an_unknown_field_is_not_shown() -> None:
    class Closed(BaseModel):
        model_config = {"extra": "forbid"}

    with pytest.raises(ValidationError) as raised:
        Closed.model_validate({"EVIL\n::error::x": 1})

    text = describe_validation_error(raised.value)

    assert text == "?: extra_forbidden"
    assert "EVIL" not in text


def test_the_gateway_reads_a_recording_through_common_and_not_the_evaluation() -> None:
    platform = Path(jsonfile.__file__).resolve().parents[1]
    source = platform / "gateway" / "providers" / "recorded.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "meridian.platform.common.jsonfile" in imported
    assert not {m for m in imported if m.startswith("meridian.platform.evaluation")}
