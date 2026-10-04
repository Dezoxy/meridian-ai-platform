"""Read a small JSON file that is not to be trusted, and describe what is wrong
with it without quoting it (S050, T-78).

The evaluation's reports and manifests and the gateway's recordings are files
the platform reads at start or in a command. Each is bounded, UTF-8, free of a
duplicate key, and every error is a fixed sentence: a file's own text is never
quoted. They live here, in ``common``, so that the gateway, which serves a
deployed service, needs nothing from the evaluation package.
"""

import json
import re
from pathlib import Path
from typing import Annotated, Any

from pydantic import StringConstraints, ValidationError

MAX_JSON_FILE_BYTES = 5 * 1024 * 1024  # a report is a few KiB; this refuses a mistake
MAX_REPORTED_ERRORS = 5
MAX_LOC_PART_CHARS = 40
SAFE_LOC_PART = re.compile(r"[A-Za-z0-9_]+")

HexDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class JsonFileError(ValueError):
    """A file cannot be read; the message never quotes its content."""


def _loc_part(part: str | int) -> str:
    """One step of an error's location. A dict key or an extra field's name is
    the file's own text, so only a plain identifier is shown; an index stays."""
    if isinstance(part, int):
        return str(part)
    if len(part) > MAX_LOC_PART_CHARS or not SAFE_LOC_PART.fullmatch(part):
        return "?"
    return part


def describe_validation_error(error: ValidationError) -> str:
    """Field paths and error kinds only: Pydantic's text quotes the input, and
    a path can carry the file's own keys, so every part goes through
    ``_loc_part``."""
    problems = []
    for item in error.errors(include_input=False, include_url=False)[
        :MAX_REPORTED_ERRORS
    ]:
        path = ".".join(_loc_part(part) for part in item["loc"])
        kind = item["type"]
        if kind == "value_error":
            # Only this package's validators raise it, with a fixed sentence.
            kind = item["msg"].removeprefix("Value error, ")
        problems.append(f"{path or 'file'}: {kind}")
    hidden = error.error_count() - len(problems)
    if hidden > 0:
        problems.append(f"and {hidden} more")
    return "; ".join(problems)


def read_text_file(path: Path) -> str:
    """Read a small UTF-8 file; raise ``JsonFileError`` without quoting it.

    Only a regular file is opened (a named pipe or a directory is refused, not
    read), and the read stops one byte past the limit, whatever the size the
    file system reported.
    """
    try:
        if not path.is_file():
            raise JsonFileError(
                "not a regular file" if path.exists() else "file not found"
            )
        with path.open("rb") as stream:
            data = stream.read(MAX_JSON_FILE_BYTES + 1)
    except OSError as exc:
        raise JsonFileError(f"cannot read the file ({type(exc).__name__})") from None
    if len(data) > MAX_JSON_FILE_BYTES:
        raise JsonFileError(f"file too large (limit {MAX_JSON_FILE_BYTES} bytes)")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise JsonFileError("file is not valid UTF-8") from None


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json.loads`` keeps the last of two equal keys silently; refuse both,
    and never name the key: it is the file's own text."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise JsonFileError("duplicate key")
    return dict(pairs)


def parse_json(text: str) -> Any:
    """Parse JSON text with no duplicate key at any depth; raise
    ``JsonFileError`` without quoting the text."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates)
    except JsonFileError:
        raise
    except RecursionError:
        raise JsonFileError("the JSON is nested too deeply") from None
    except ValueError:  # a syntax error, or an integer too long to convert
        raise JsonFileError("the file is not valid JSON") from None


def read_json_file(path: Path) -> Any:
    """Read a small JSON file: bounded, UTF-8, no duplicate key, and errors that
    never quote the file."""
    return parse_json(read_text_file(path))
