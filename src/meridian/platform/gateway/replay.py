"""The replay provider: SIMULATED (hard rule 7).

No model is called and nothing is recorded from a real one; the text is built
from a fingerprint of the request, so the same messages always get the same
answer. It reads no fixture file and never the golden set's expected outcomes.
The request's ``max_output_tokens`` is accepted and ignored: the text has a
fixed length.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from meridian.platform.gateway.models import Message

REPLAY_PREFIX = "Replay response (simulated; no model was called)."
FINGERPRINT_CHARS = 12
CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class ReplayReply:
    text: str
    input_tokens: int
    output_tokens: int


def _tokens(characters: int) -> int:
    """Characters divided by four, rounded up."""
    return -(-characters // CHARS_PER_TOKEN)


def _fingerprint(messages: Sequence[Message]) -> str:
    """First 12 hex characters of the SHA-256 of the canonical JSON: a list of
    ``{content, role}`` objects, keys sorted, no spaces, UTF-8 not escaped."""
    canonical = json.dumps(
        [{"role": m.role, "content": m.content} for m in messages],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def replay_chat(messages: Sequence[Message]) -> ReplayReply:
    text = f"{REPLAY_PREFIX} Request fingerprint: {_fingerprint(messages)}."
    return ReplayReply(
        text=text,
        input_tokens=_tokens(sum(len(m.content) for m in messages)),
        output_tokens=_tokens(len(text)),
    )
