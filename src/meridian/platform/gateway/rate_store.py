"""The gateway's rate windows from its settings: in the process, or in the shared
store (S066, T-45).

``limiter_from_settings`` is what the service's entry point asks for. Without the
store's address the windows stay in the process (``TenantRateLimiter``, on the
clock the app is given). With it they are in Redis (``RedisRateLimiter``, on the
server's clock), and the gateway uses nothing else: a store that cannot be reached
refuses the call (``app.py``), never a fall back to the process's own windows, and
no setting chooses between the two.

``rate_store_client`` is the one place the Redis client is made. Every argument
is set here and none is read from the address, because redis-py gives a query
string in a URL priority over its arguments (``?ssl_cert_reqs=none`` would turn
the check of the server's certificate off); the settings refuse a query, and this
function takes none. The connection:

* TLS only (``rediss``), with the repository's one client context
  (``ClientTls.ssl_context``): the server's certificate is verified against the
  services' CA (``MERIDIAN_TLS_CA_FILE``) alone, not the machine's CAs, and its
  name against the address's host, and the gateway presents its own certificate
  and key (``MERIDIAN_TLS_CERT_FILE`` and ``MERIDIAN_TLS_KEY_FILE``), because the
  store requires a client certificate. redis-py builds a context of its own and
  adds the CA it is given to the machine's (``SSL_CERT_FILE`` would then widen
  who is trusted), so ``_PinnedContextConnection`` wraps the socket with the
  given context instead. The context is built here, once: a file that cannot be
  used stops the start with a ``SettingsError`` that names the variables and no
  path, and is never a 503 on every call that looks like a store that is down.
  A certificate renewed on disk is read again at the next start only.
* The gateway's user and password from the address, percent-decoded, database 0.
  No ``CLIENT SETINFO``: ``driver_info=None``, redis-py's own switch for it (the
  ``lib_name`` and ``lib_version`` arguments are deprecated), and no ``CLIENT
  MAINT_NOTIFICATIONS`` (redis-py 8.1.0 sends it on every RESP3 connection unless
  its config says ``enabled=False``; Redis answers an error, which MONITOR does
  not show): three round trips less on a new connection and two entries less in
  the store's access list.
* A connect and a read timeout, and no retry: a retry after a read timeout could
  run a script that already ran, and a store that is down must be one quick
  answer. The timeouts are per socket operation, so a store that is slow, not
  dead, can hold a cold call for several of them: a cold call does a TLS
  handshake (a wait for the server per round of it: one under TLS 1.3, two under
  1.2), ``HELLO`` with the credentials, the script's ``EVALSHA`` and, when the
  server lost the script, ``SCRIPT LOAD`` and ``EVALSHA`` again, so at most
  ``COLD_CALL_READS`` reads; a warm connection makes one.
  ``worst_cold_call_seconds`` is the bound, and a test holds it well inside the
  attempt budget of a call (``resilience.MIN_ATTEMPT_SECONDS``). The bound does
  not cover name resolution, which has no timeout of its own here (the
  store's name is a cluster Service's, answered by the cluster's DNS), and
  counts one address per name: redis-py tries each address a name resolves to
  with a connect timeout of its own.

Nothing connects when the client or the limiter is built: a store that is down
when the gateway starts is refused per call, as a database that is down is.
Implemented and tested against a TLS test server (not a Redis) for who the
client trusts and presents, and against a plain Redis for the windows; no TLS
Redis has run, and nothing has run on a cluster.
"""

import ssl
from typing import Any

import redis
from redis.backoff import NoBackoff
from redis.connection import SSLConnection
from redis.maint_notifications import MaintNotificationsConfig
from redis.retry import Retry

from meridian.platform.common.env import SettingsError
from meridian.platform.common.tls import ClientTls
from meridian.platform.gateway.ratelimit import RateLimiter, TenantRateLimiter
from meridian.platform.gateway.ratelimit_redis import RedisRateLimiter
from meridian.platform.gateway.settings import (
    RATE_STORE_URL_ENV,
    GatewaySettings,
    parse_rate_store_url,
)

# A store on the same cluster answers in well under a millisecond, so a quarter
# of a second to read is generous, and a store that is down or stuck is one quick
# answer. One second to connect covers a TCP handshake across the cluster.
CONNECT_TIMEOUT_SECONDS = 1.0
READ_TIMEOUT_SECONDS = 0.25
# The commands of a cold call that finds the server without the script; a test
# counts what a real client sends. The reads are those and the TLS handshake's:
# one wait for the server's flight under TLS 1.3 (what the store negotiates), two
# under TLS 1.2, which the store also allows, so two are counted.
COLD_CALL_COMMANDS = ("HELLO", "EVALSHA", "SCRIPT", "EVALSHA")
HANDSHAKE_READS = 2
COLD_CALL_READS = HANDSHAKE_READS + len(COLD_CALL_COMMANDS)


def worst_cold_call_seconds(addresses: int = 1) -> float:
    """The longest a call on a new connection can wait for the store when it
    answers every operation just inside its timeout: a connect timeout for each
    address tried, then ``COLD_CALL_READS`` read timeouts. Name resolution is
    not counted."""
    return addresses * CONNECT_TIMEOUT_SECONDS + COLD_CALL_READS * READ_TIMEOUT_SECONDS


class _PinnedContextConnection(SSLConnection):
    """redis-py's TLS connection, wrapping its socket with the ``ssl.SSLContext``
    it is given and with nothing it builds itself.

    ``SSLConnection._wrap_socket_with_ssl`` (redis-py 8.1.0) makes a default
    context, which trusts the machine's CAs, and adds the CA file to them. Its
    own ``ssl_*`` arguments are not used here: the context carries the CA, the
    certificate and the checks, and the server's name is still checked against
    the host the connection was made for.
    """

    def __init__(self, *, ssl_context: ssl.SSLContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ssl_context = ssl_context

    def _wrap_socket_with_ssl(self, sock: Any) -> ssl.SSLSocket:
        return self._ssl_context.wrap_socket(sock, server_hostname=self.host)


def rate_store_client(url: str, tls: ClientTls) -> redis.Redis:
    """A client of the store at ``url`` that has not connected. Raise
    ``ValueError``, without the address, for one the settings would refuse, and
    ``SettingsError`` for a TLS file that cannot be used."""
    address = parse_rate_store_url(url)
    pool = redis.ConnectionPool(
        connection_class=_PinnedContextConnection,
        ssl_context=tls.ssl_context(),
        host=address.host,
        port=address.port,
        db=0,
        username=address.username,
        password=address.password,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=READ_TIMEOUT_SECONDS,
        socket_keepalive=True,
        retry=Retry(NoBackoff(), 0),
        driver_info=None,
        maint_notifications_config=MaintNotificationsConfig(enabled=False),
    )
    return redis.Redis(connection_pool=pool)


def limiter_from_settings(settings: GatewaySettings) -> RateLimiter:
    """The shared store's limiter when the settings carry its address, the
    process's own otherwise. Nothing connects."""
    if settings.rate_store_url is None:
        return TenantRateLimiter()
    if settings.client_tls is None:  # the settings refuse it; never a fall back
        raise SettingsError(f"{RATE_STORE_URL_ENV} needs the TLS files")
    client = rate_store_client(
        settings.rate_store_url.get_secret_value(), settings.client_tls
    )
    return RedisRateLimiter(client)
