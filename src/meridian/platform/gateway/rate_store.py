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

* TLS only (``rediss``): the server's certificate is verified against the
  services' CA (``MERIDIAN_TLS_CA_FILE``) and its name against the address's host,
  and the gateway presents its own certificate and key (``MERIDIAN_TLS_CERT_FILE``
  and ``MERIDIAN_TLS_KEY_FILE``), because the store requires a client certificate.
  The files are read when the first connection is made, not here.
* The gateway's user and password from the address, percent-decoded, database 0.
* One second to connect and one to read, and no retry: a retry after a read
  timeout could run a script that already ran, and a store that is down must be
  one quick answer. These are far inside the attempt budget of a call
  (``resilience.MIN_ATTEMPT_SECONDS``) and its deadline.

Nothing connects when the client or the limiter is built: a store that is down
when the gateway starts is refused per call, as a database that is down is.
Implemented and tested without a server for the arguments and against a plain
Redis for the windows; no TLS Redis has run, and nothing has run on a cluster.
"""

import redis
from redis.backoff import NoBackoff
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

# What a store on the same cluster answers in well under a millisecond. A store
# that is not there is one answer after at most this long, so a dead store never
# stands in for a slow one.
CONNECT_TIMEOUT_SECONDS = 1.0
READ_TIMEOUT_SECONDS = 1.0


def rate_store_client(url: str, tls: ClientTls) -> redis.Redis:
    """A client of the store at ``url`` that has not connected. Raise
    ``ValueError``, without the address, for one the settings would refuse."""
    address = parse_rate_store_url(url)
    return redis.Redis(
        host=address.host,
        port=address.port,
        db=0,
        username=address.username,
        password=address.password,
        ssl=True,
        ssl_cert_reqs="required",
        ssl_check_hostname=True,
        ssl_ca_certs=str(tls.ca_file),
        ssl_certfile=str(tls.cert_file),
        ssl_keyfile=str(tls.key_file),
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=READ_TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), 0),
    )


def limiter_from_settings(settings: GatewaySettings) -> RateLimiter:
    """The shared store's limiter when the settings carry its address, the
    process's own otherwise. Nothing connects."""
    if settings.rate_store_url is None:
        return TenantRateLimiter()
    if settings.client_tls is None:  # the settings refuse it; never a fall back
        raise SettingsError(f"{RATE_STORE_URL_ENV} needs the TLS files")
    client = rate_store_client(settings.rate_store_url, settings.client_tls)
    return RedisRateLimiter(client)
