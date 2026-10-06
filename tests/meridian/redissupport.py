"""What the tests of the rate windows' Redis share (S066)."""

import secrets

import redis


class RateKeys:
    """Key prefixes no other test shares: the suite runs under xdist against
    one Redis, and every test and every limiter it builds gets its own."""

    def __init__(self, client: redis.Redis) -> None:
        self.client = client
        self._prefixes: list[str] = []
        self._clients: list[redis.Redis] = []

    def new_client(self) -> redis.Redis:
        """Another client of the same server: a second process's own."""
        kwargs = self.client.connection_pool.connection_kwargs
        other = redis.Redis(
            host=kwargs["host"], port=kwargs["port"], db=kwargs.get("db", 0)
        )
        self._clients.append(other)
        return other

    def new_prefix(self) -> str:
        prefix = f"meridian:test:{secrets.token_hex(8)}"
        self._prefixes.append(prefix)
        return prefix

    def keys(self, prefix: str) -> list[bytes]:
        return list(self.client.scan_iter(match=f"{prefix}:*"))

    def forget(self) -> None:
        for prefix in self._prefixes:
            for key in self.keys(prefix):
                self.client.delete(key)
        for other in self._clients:
            other.close()
