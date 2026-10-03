"""Redaction of a request's text before the gateway does anything with it (T-20).

Every chat message and every embedding input has its personal identifiers
replaced (``meridian.platform.guardrails.redact``), whatever the request's data
class: the class decides where a request may go, never whether an e-mail address
or a card number is sent. It runs when the operation is built, after policy has
let the request through and before the rate limiter, so the estimate, the
limiter, the ledger, the provider and the replay provider all see the redacted
text and nothing below the route decision sees the original. A request policy
refuses is never redacted: it costs time in proportion to its text.

Roles, and the number and order of messages and inputs, are kept. The new
request is copied, not validated again: a placeholder can be one character
longer than the shortest value it replaces, and the bounds of the wire contract
are the caller's, not a limit the provider needs. Only a count leaves this
module: never the kinds found and never the text (T-03, T-18).
"""

from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest
from meridian.platform.guardrails import redact


def redact_chat(body: ChatRequest) -> tuple[ChatRequest, int]:
    """The request with every message's content redacted, and how many values
    were replaced in all."""
    results = tuple(redact(message.content) for message in body.messages)
    messages = tuple(
        message.model_copy(update={"content": result.text})
        for message, result in zip(body.messages, results, strict=True)
    )
    count = sum(sum(result.found.values()) for result in results)
    return body.model_copy(update={"messages": messages}), count


def redact_embeddings(body: EmbeddingRequest) -> tuple[EmbeddingRequest, int]:
    """The request with every input redacted, and how many values were replaced
    in all."""
    results = tuple(redact(text) for text in body.inputs)
    inputs = tuple(result.text for result in results)
    count = sum(sum(result.found.values()) for result in results)
    return body.model_copy(update={"inputs": inputs}), count
