"""What a paid run may write, and how (S071, L3).

The repository is public and a recording is a model's own text, derived from
attack sentences. The model cannot know an endpoint, an account or a token, so
the checks below should never fire, which is why they are cheap: before any file
of a paid run is written, refuse a recording or a report that holds a shape that
only a leak would put there, or a recording entry too large to be an answer this
run asked for. The refusal names the file and the kind of thing found, never the
text. The owner's reading of the diff before the commit stays the other barrier.

``write_all_or_none`` writes a run's files to a staging directory beside the
target and moves them into place in sequence only when every one is written, so
a failure between two files leaves none of them.
"""

import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from meridian.platform.gateway.providers.recorded import Recording

# Each kind and the pattern that finds it. Case-insensitive except the bearer
# marker, which is matched as an HTTP header writes it (English words such as
# "bearer of the policy" must pass).
KINDS: dict[str, re.Pattern[str]] = {
    "a GUID shape": re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.IGNORECASE,
    ),
    "an Azure OpenAI host name": re.compile(r"openai\.azure\.com", re.IGNORECASE),
    "an account name prefix": re.compile(r"oai-meridian-", re.IGNORECASE),
    "an e-mail address": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "a bearer-token marker": re.compile(r"Bearer "),
    "a JWT opening": re.compile(r"eyJ[A-Za-z0-9_-]{4,}"),
    "a URL scheme": re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]{1,15}://"),
}
# One recording entry's text, in characters. The wire's output cap is 1,024
# tokens (about 4,000 characters of English) and the triage request asks for 400
# (about 1,600): an entry over 4,000 characters is not an answer this run asked
# for, and a larger one is more text to read in a diff than a person will.
MAX_ENTRY_CHARS = 4000


# For the tests: one sample of each kind. None is a real identifier: the GUID
# is the RFC's example, the host and account are made up, the token is not one.
SAMPLES = {
    "a GUID shape": "the id is 123e4567-e89b-12d3-a456-426614174000 here",
    "an Azure OpenAI host name": "see example-account.openai.azure.com for it",
    "an account name prefix": "the account oai-meridian-example answered",
    "an e-mail address": "write to someone@example.org about it",
    "a bearer-token marker": "send Authorization: Bearer abcdef",
    "a JWT opening": "the token eyJhbGciOiJIUzI1NiJ9 opens",
    "a URL scheme": "follow https://example.org/path for more",
}


class UnsafeFile(AssertionError):
    """A file a paid run would write holds what only a leak puts there, or an
    entry too large. The message names the file and the kind, never the text."""


def refuse_identifier_shapes(name: str, text: str) -> None:
    """``UnsafeFile`` naming ``name`` and the kinds found, none of their text."""
    found = sorted(kind for kind, pattern in KINDS.items() if pattern.search(text))
    if found:
        raise UnsafeFile(f"nothing was written: {name} holds {', '.join(found)}")


def refuse_large_entries(name: str, recording: Recording) -> None:
    """``UnsafeFile`` when one entry's text is over ``MAX_ENTRY_CHARS``."""
    over = sum(
        1 for entry in recording.entries.values() if len(entry.text) > MAX_ENTRY_CHARS
    )
    if over:
        raise UnsafeFile(
            f"nothing was written: {name} holds {over} entries over "
            f"{MAX_ENTRY_CHARS} characters"
        )


def refuse_unsafe_recording(name: str, recording: Recording, text: str) -> None:
    """The two checks for a recording, with ``text`` its serialised form."""
    refuse_identifier_shapes(name, text)
    refuse_large_entries(name, recording)


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def write_all_or_none(base: Path, files: Mapping[str, str]) -> tuple[Path, ...]:
    """Write ``files`` (a path under ``base``, and its text) so that either all
    are in place or none is. They are written to a staging directory under
    ``base`` (a dot-name ending in ``.tmp``, which ``.gitignore`` holds), then
    moved into place in sequence, and the staging directory is removed on every
    exit. A write that raises leaves none of them. Returns the final paths."""
    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".injection-run-", suffix=".tmp", dir=base))
    try:
        staged = []
        for index, (relative, text) in enumerate(files.items()):
            path = staging / f"{index}-{Path(relative).name}"
            _write_text(path, text)
            staged.append((path, base / relative))
        for path, final in staged:
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return tuple(final for _, final in staged)
