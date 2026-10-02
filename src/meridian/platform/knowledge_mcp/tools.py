"""The knowledge server's one tool, ``wording_search`` (S046).

The kit has bound the call to the run's own policy: the ``product`` argument is
that policy's product, and ``call.binding`` carries it with the policy's wording
version (T-22, T-58). The tenant, the agent and the run the embedding call goes
out under are the run row's, never an argument (T-16, T-61).

No transaction is open while the server waits for the gateway: the handler has
only read so far, and ends that transaction before the call. Nothing of a query,
a vector or a hit is logged, and no exception text holds one (T-03, TB-7).
"""

from functools import partial

import httpx
import psycopg

from meridian.platform.knowledge_mcp.embedding_client import (
    EmbeddingCallError,
    EmbeddingClient,
)
from meridian.platform.knowledge_mcp.search import (
    QueryEmbedding,
    SearchRefused,
    hybrid_search,
)
from meridian.platform.toolserver.handlers import (
    Completed,
    Refused,
    ToolCall,
    ToolHandler,
)

DEFAULT_TOP_K = 5
HTTP_TOO_MANY_REQUESTS = 429
HTTP_FORBIDDEN = 403


def _gateway_refusal(error: EmbeddingCallError) -> Refused:
    """The refusal for a gateway that gave no vector. No retry: a busy gateway
    is the caller's to wait for, and the runtime's time for the call is short."""
    if error.status_code == HTTP_TOO_MANY_REQUESTS:
        return Refused("gateway-busy")
    if error.status_code == HTTP_FORBIDDEN:
        return Refused("gateway-refused")
    return Refused("gateway-unavailable")


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
    conn.rollback()
    client = EmbeddingClient(
        http, tenant=binding.tenant, agent=binding.agent, run_id=binding.run_id
    )
    try:
        batch = client.embed([query])
    except EmbeddingCallError as error:
        return _gateway_refusal(error)
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
            return Refused("no-corpus")
        if error.reason == "stale-vectors":
            return Refused("stale-vectors")
        raise
    return Completed(
        {
            "product": binding.product,
            "wording_version": binding.wording_version,
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
