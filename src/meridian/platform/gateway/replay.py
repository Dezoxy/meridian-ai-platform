"""The replay provider: SIMULATED (hard rule 7).

No model is called and nothing is recorded from a real one; the text is built
from a fingerprint of the request, so the same messages always get the same
answer. It reads no fixture file and never the golden set's expected outcomes.
The request's ``max_output_tokens`` is accepted and ignored: the text has a
fixed length.

A replay embedding is a hashed bag of words: no model was called, and the
vector carries no meaning of the text. Two texts that share words get vectors
that point the same way, which is all a test of search needs (so the vector
half of a hybrid search ranks by word overlap in replay mode, not by noise);
two texts that mean the same in other words do not.
"""

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest, Message
from meridian.platform.gateway.providers.base import EmbeddingReply, ProviderReply
from meridian.platform.registry.models import Deployment

REPLAY_PREFIX = "Replay response (simulated; no model was called)."
FINGERPRINT_CHARS = 12
CHARS_PER_TOKEN = 4
WORD = re.compile(r"\w+")
# Bytes of a token's digest that pick its index; the lowest bit of the next
# byte picks its sign.
INDEX_BYTES = 8


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


def _slot(text: str, dimensions: int) -> tuple[int, int]:
    """The index and the sign (1 or -1) the SHA-256 of ``text`` picks: its first
    eight bytes as a big-endian integer, modulo the dimensions, and the lowest
    bit of the ninth byte."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    index = int.from_bytes(digest[:INDEX_BYTES], "big") % dimensions
    return index, -1 if digest[INDEX_BYTES] & 1 else 1


def replay_embedding(text: str, dimensions: int) -> tuple[float, ...]:
    """A unit vector of ``dimensions`` numbers for ``text``: SIMULATED.

    No model was called and the vector carries no meaning of the text, only its
    words. Each word (a run of letters, digits and underscores, casefolded) adds
    a signed 1 at the index its SHA-256 picks; the sum is scaled to length 1.
    Word order and repeats beyond their count change nothing. A text with no
    word, or whose words cancel each other out, has no direction of its own, so
    it gets the one-hot vector the SHA-256 of the whole text picks: never a zero
    vector, never NaN.
    """
    sums = [0] * dimensions
    for word in WORD.findall(text):
        index, sign = _slot(word.casefold(), dimensions)
        sums[index] += sign
    length = math.sqrt(sum(n * n for n in sums))  # exact: integers
    if length == 0.0:
        index, sign = _slot(text, dimensions)
        sums[index] = sign
        length = 1.0
    return tuple(n / length for n in sums)


class ReplayProvider:
    """``replay_chat`` and ``replay_embedding`` behind the provider protocols
    (SIMULATED, like the rest)."""

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        reply = replay_chat(request.messages)
        return ProviderReply(
            text=reply.text,
            finish_reason="stop",
            model=deployment.model,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
        )

    def embed(
        self,
        deployment: Deployment,
        request: EmbeddingRequest,
        *,
        timeout_seconds: float,
    ) -> EmbeddingReply:
        """The tokens are counted as ``replay_chat`` counts them, but per input:
        each input's characters over four, rounded up, then summed."""
        dimensions = deployment.dimensions
        if dimensions is None:
            raise ValueError(f"deployment {deployment.id} has no dimensions")
        return EmbeddingReply(
            embeddings=tuple(
                replay_embedding(text, dimensions) for text in request.inputs
            ),
            model=deployment.model,
            input_tokens=sum(_tokens(len(text)) for text in request.inputs),
        )
