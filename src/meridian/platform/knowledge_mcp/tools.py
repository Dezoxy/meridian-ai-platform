"""The knowledge server's one tool, ``wording_search`` (S046).

The kit has bound the call to the run's own policy: the ``product`` argument is
that policy's product, and ``call.binding`` carries it with the policy's wording
version (T-22, T-58). The tenant, the agent and the run the embedding call goes
out under are the run row's, never an argument (T-16, T-61).

No transaction is open while the server waits for the gateway: the handler has
only read so far, and ends that transaction before the call. Nothing of a query,
a vector or a hit is logged, and no exception text holds one (T-03, TB-7).

A call whose deadline has passed fails as ``timed-out`` before the gateway is
called, and the gateway is waited for no longer than the call has left; a gateway
that fails after the deadline is ``timed-out`` too, not unavailable. The
deadline is the server's: its clock starts when the call arrives, a little after
the runtime's, so a search that commits in the moment after the kit's last check
is ``completed`` although the runtime may have just stopped waiting.

The run's status is read once, before the gateway call; a run that ends while
the server waits still gets its answer (a read; T-22's residual).

A gateway that is busy (429) or says no (403) has answered about the call, and
the call is refused. A gateway that gave no vector for any other reason is the
platform's failure: the call fails and is audited as failed, so an operator
sees it. A scope with no corpus, or with vectors of another model, is an answer
about the call's scope and is refused, with a warning that names the scope.
"""

import logging
import math
from collections.abc import Sequence
from functools import partial
from typing import Literal

import httpx
import psycopg

from meridian.platform.knowledge_mcp.embedding_client import (
    EmbeddingBatch,
    EmbeddingCallError,
    EmbeddingClient,
)
from meridian.platform.knowledge_mcp.search import (
    Hit,
    QueryEmbedding,
    SearchRefused,
    corpus_exists,
    hybrid_search,
)
from meridian.platform.toolserver.handlers import (
    TIMED_OUT,
    Completed,
    Refused,
    ToolCall,
    ToolFailed,
    ToolHandler,
)

logger = logging.getLogger(__name__)

# Ten clauses unless the caller asks for fewer: at five the fused search of S012
# returned 3 of 8 labelled exclusion clauses and at ten 5 of 8, and ten clauses
# are under 1,000 tokens.
DEFAULT_TOP_K = 10
HTTP_TOO_MANY_REQUESTS = 429
HTTP_FORBIDDEN = 403


def _gateway_answer(error: EmbeddingCallError, call: ToolCall) -> Refused:
    """The refusal for a gateway that is busy or says no; any other gateway that
    gave no vector fails the call, as ``timed-out`` when the call's own deadline
    has passed (the wait was cut by the time the call had left, not by a gateway
    that is down). No retry: a busy gateway is the caller's to wait for, and the
    runtime's time for the call is short. The status is logged (0 is no usable
    answer), and for a 503 the fixed word of which refusal of the gateway's it
    was (S073); never the body or the query. The answer to an agent has no word:
    an agent is not told which part of the platform is away."""
    if error.gateway_word is None:
        logger.warning("embedding call failed: status %d", error.status_code)
    else:
        logger.warning(
            "embedding call failed: status %d, kind %s",
            error.status_code,
            error.gateway_word,
        )
    if error.status_code == HTTP_TOO_MANY_REQUESTS:
        return Refused("gateway-busy")
    if error.status_code == HTTP_FORBIDDEN:
        return Refused("gateway-refused")
    raise ToolFailed(TIMED_OUT if call.deadline.expired() else "gateway-unavailable")


def _scope_refusal(
    reason: Literal["no-corpus", "stale-vectors"], product: str, wording_version: str
) -> Refused:
    """A refusal about the call's scope, with a warning an operator can act on:
    the identifiers, not content."""
    logger.warning(
        "search refused: %s for product %r, wording version %r",
        reason,
        product,
        wording_version,
    )
    return Refused(reason)


def _request_timeout(http: httpx.Client, call: ToolCall) -> httpx.Timeout | None:
    """The client's own timeouts but for the read, when the time the call has
    left is shorter than the client's read timeout: the gateway is waited for
    no longer than the call has left. None when the client's own already holds."""
    configured, left = http.timeout, call.deadline.remaining()
    client_holds = configured.read is not None and configured.read <= left
    if client_holds or not math.isfinite(left):
        return None
    return httpx.Timeout(
        connect=configured.connect,
        read=left,
        write=configured.write,
        pool=configured.pool,
    )


def _embed_query(
    http: httpx.Client, call: ToolCall, query: str
) -> EmbeddingBatch | Refused:
    """The query's vector from the gateway, under the run's own identity, waited
    for no longer than the call has left; a refusal when the gateway has
    answered about the call."""
    binding = call.binding
    client = EmbeddingClient(
        http, tenant=binding.tenant, agent=binding.agent, run_id=binding.run_id
    )
    try:
        return client.embed([query], timeout=_request_timeout(http, call))
    except EmbeddingCallError as error:
        return _gateway_answer(error, call)


def _completed(product: str, wording_version: str, hits: Sequence[Hit]) -> Completed:
    return Completed(
        {
            "product": product,
            "wording_version": wording_version,
            "chunks": [
                {
                    "clause": hit.clause,
                    "section": hit.section,
                    "title": hit.title,
                    "body": hit.body,
                    "keyword_match": hit.lexical_rank is not None,
                }
                for hit in hits
            ],
        }
    )


def wording_search(
    http: httpx.Client, conn: psycopg.Connection, call: ToolCall
) -> Completed | Refused:
    query: str = call.arguments["query"]
    # The schema's minimum length lets a blank query through.
    if not query.strip():
        return Refused("invalid-arguments")
    binding = call.binding
    if binding.product is None or binding.wording_version is None:
        # The kit reads the policy's scope for a tool bound to the product.
        raise RuntimeError("the call has no policy scope")
    if not corpus_exists(
        conn, product=binding.product, wording_version=binding.wording_version
    ):
        # Before the gateway: the tenant is not charged for a search that cannot
        # answer. The statement below still refuses if the store is emptied now.
        return _scope_refusal("no-corpus", binding.product, binding.wording_version)
    conn.rollback()
    if call.deadline.expired():
        # The caller has stopped waiting: a search that is late costs the
        # tenant nothing. One already at the gateway is charged.
        raise ToolFailed(TIMED_OUT)
    batch = _embed_query(http, call, query)
    if isinstance(batch, Refused):
        return batch
    try:
        hits = hybrid_search(
            conn,
            product=binding.product,
            wording_version=binding.wording_version,
            query=query,
            embedding=QueryEmbedding(batch.deployment, batch.vectors[0]),
            top_k=call.arguments.get("top_k", DEFAULT_TOP_K),
        )
    except SearchRefused as error:
        # The schema and the blank check exclude the other reasons: one of them
        # is a bug, and is left to raise.
        if error.reason == "no-corpus":
            return _scope_refusal("no-corpus", binding.product, binding.wording_version)
        if error.reason == "stale-vectors":
            return _scope_refusal(
                "stale-vectors", binding.product, binding.wording_version
            )
        raise
    return _completed(binding.product, binding.wording_version, hits)


def handlers(http: httpx.Client) -> tuple[ToolHandler, ...]:
    """The handlers over the client the gateway is called with."""
    return (
        ToolHandler(
            tool="wording_search",
            scope="knowledge:search",
            bound_argument="product",
            bound_to="product",
            run=partial(wording_search, http),
        ),
    )
