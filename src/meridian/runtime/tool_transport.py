"""One HTTP client for each tool server, kept across calls (S059).

Before this module every call drove the SDK with ``anyio.run``: a new event
loop, and in it a new HTTP client, so a new TCP connection and a new mutual-TLS
handshake for each call. An async client cannot outlive the loop it was used
in, so a client that outlives a call needs a loop that does too.

``ToolTransport`` owns that loop, in one daemon thread, and one
``httpx2.AsyncClient`` for each tool server address, made on the loop at the
first call to the address. The runtime is synchronous (a graph runs in a worker
thread), so ``exchange`` is called from a worker thread and blocks until the
answer. The SDK's ``Client`` and its session stay one per call, opened over the
kept HTTP client: the tool servers are stateless, so nothing but the connection
is shared between calls, and a run's calls carry the run's identity in each
call's ``_meta``, never in the connection.

The thread starts at the first call, so an app that never calls a tool owns
none, and ends in ``close``: the app closes the transport at shutdown, and a
thread left behind is a daemon, which does not hold the process open.
"""

import ssl
import threading
from contextlib import AbstractContextManager, AsyncExitStack
from typing import Any

import anyio
import httpx2
import mcp_types as types
from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from meridian.platform.toolserver.wire import MCP_PATH
from meridian.runtime import tool_client

# How long an idle connection of ours is kept. Below the 5 s a tool server
# keeps one (uvicorn's default ``timeout_keep_alive``, which no tool server's
# command overrides), so a call rarely meets a connection the server is closing.
KEEPALIVE_SECONDS = 2.0
THREAD_NAME = "meridian-tool-transport"


class ToolTransport:
    """The kept clients of the tool servers and the loop they run on.

    ``verify`` is how a call is made over TLS: the context of the runtime's own
    certificate and CA, or the default verification, never off. Safe to call
    from any thread. After ``close`` a call raises ``RuntimeError``."""

    def __init__(self, verify: ssl.SSLContext | bool = True) -> None:
        self._verify = verify
        self._lock = threading.Lock()
        self._manager: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._closed = False
        # Read and written only on the loop's thread, with no ``await`` between
        # the read and the write, so two calls never make two clients.
        self._clients: dict[str, httpx2.AsyncClient] = {}

    def exchange(
        self, target: str, tool: str, arguments: dict[str, Any], meta: dict[str, Any]
    ) -> types.CallToolResult:
        """One call to the tool server at ``target``, on the kept client; blocks
        until the answer. The failure is the exception of the call, which the
        caller turns into ``ToolUnavailable``: it can quote an argument."""
        return self._started().call(self._exchange, target, tool, arguments, meta)

    def close(self) -> None:
        """Close every client and end the loop's thread; safe to call twice."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            portal, manager = self._portal, self._manager
            self._portal = self._manager = None
        if portal is None or manager is None:
            return
        try:
            portal.call(self._close_clients)
        finally:
            manager.__exit__(None, None, None)

    def _started(self) -> BlockingPortal:
        with self._lock:
            if self._closed:
                raise RuntimeError("the tool transport is closed")
            if self._portal is None:
                manager = start_blocking_portal(name=THREAD_NAME)
                self._portal = manager.__enter__()
                self._manager = manager
            return self._portal

    def _client_for(self, target: str) -> httpx2.AsyncClient:
        client = self._clients.get(target)
        if client is None:
            # trust_env=False: a proxy variable must not reroute claimant data.
            client = httpx2.AsyncClient(
                trust_env=False,
                timeout=tool_client.TOOL_TIMEOUT_SECONDS,
                verify=self._verify,
                limits=httpx2.Limits(keepalive_expiry=KEEPALIVE_SECONDS),
            )
            self._clients[target] = client
        return client

    async def _exchange(
        self, target: str, tool: str, arguments: dict[str, Any], meta: dict[str, Any]
    ) -> types.CallToolResult:
        # Read at call time, so the bound is the constant's current value.
        with anyio.fail_after(tool_client.TOOL_TIMEOUT_SECONDS):
            # A client given to the SDK's transport is not closed by it.
            transport = streamable_http_client(
                target.rstrip("/") + MCP_PATH, http_client=self._client_for(target)
            )
            async with Client(
                transport, mode=tool_client.CONNECT_MODE, cache=None
            ) as client:
                return await tool_client.send_call(client, tool, arguments, meta)

    async def _close_clients(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        async with AsyncExitStack() as stack:
            for client in clients:
                stack.push_async_callback(client.aclose)
