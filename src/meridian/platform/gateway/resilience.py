"""What lets one model call survive the failure of one deployment (S042).

Pure and free of I/O: a per-deployment circuit breaker and a call deadline, both
on an injectable clock. The gateway walks the candidates that routing kept
(T-44), skips one whose circuit is open, and gives each attempt the time left
under one deadline. Only a deployment's own failures count toward a circuit;
a request the provider rejects says nothing about the deployment (T-45).
"""

import itertools
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from meridian.platform.gateway.providers.base import ProviderErrorKind

# What connecting and a default-length answer need. A first candidate that used
# its whole read limit therefore leaves the next one a ``deadline`` skip, never
# an attempt that is bound to time out and would count against a second circuit
# (T-45).
MIN_ATTEMPT_SECONDS = 10.0
CALL_DEADLINE_SECONDS = 25.0  # all attempts of one call; under the runtime's 30 s
FAILURE_THRESHOLD = 3  # counted failures in a row that open a circuit
OPEN_SECONDS = 30.0  # how long an open circuit sends nothing

# The deployment's own failures: the walk goes on to the next candidate and the
# failure counts toward the circuit. Every other kind (rejected, auth) ends the
# call and counts for nothing (T-45).
DEPLOYMENT_FAILURES: frozenset[ProviderErrorKind] = frozenset(
    {"timeout", "unavailable", "rate-limited", "bad-response"}
)

CircuitState = Literal["closed", "open", "half-open"]


@dataclass(frozen=True, slots=True)
class Permit:
    """What ``acquire`` hands a call: the circuit it may use and, when the call
    is the probe of a half-open circuit, the serial of that probe."""

    key: str
    probe: int | None


@dataclass(frozen=True, slots=True)
class _Circuit:
    state: CircuitState = "closed"
    failures: int = 0  # counted failures in a row while closed
    opened_at: float = 0.0
    probe: int | None = None  # the serial of the probe in flight


class CircuitBreaker:
    """One circuit per key (a deployment ID), all under one lock: the endpoint
    is sync and runs in a thread pool.

    Whoever gets a ``Permit`` from ``acquire`` settles it with ``success``,
    ``failure`` or ``release``; settling more than once is harmless. A permit
    acts only on the circuit state it was taken for:

    - A permit that is not the probe (``probe is None``) began while the circuit
      was closed. It changes the circuit only while it is still closed; on an
      open or half-open circuit it does nothing, because only the probe decides
      a half-open circuit.
    - The probe's permit acts only while the circuit is half-open and its serial
      is the one in flight: ``success`` closes, ``failure`` opens with a new
      cooldown, ``release`` frees the probe for the next caller. Otherwise it
      does nothing, so a late second settle cannot free a newer probe.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = FAILURE_THRESHOLD,
        open_seconds: float = OPEN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._circuits: dict[str, _Circuit] = {}
        self._serials = itertools.count(1)  # taken under the lock

    def _get(self, key: str) -> _Circuit:
        return self._circuits.get(key, _Circuit())

    def acquire(self, key: str) -> Permit | None:
        """May a call go to ``key``? ``None`` for an open circuit, and for a
        half-open one whose probe is still in flight."""
        with self._lock:
            circuit = self._get(key)
            if circuit.state == "closed":
                return Permit(key, None)
            if circuit.state == "open":
                if self._clock() - circuit.opened_at < self._open_seconds:
                    return None
            elif circuit.probe is not None:
                return None
            serial = next(self._serials)
            self._circuits[key] = replace(circuit, state="half-open", probe=serial)
            return Permit(key, serial)

    @staticmethod
    def _acts(permit: Permit, circuit: _Circuit) -> bool:
        """Does this permit still match the circuit it was taken for?"""
        if permit.probe is None:
            return circuit.state == "closed"
        return circuit.state == "half-open" and circuit.probe == permit.probe

    def success(self, permit: Permit) -> None:
        with self._lock:
            if self._acts(permit, self._get(permit.key)):
                self._circuits[permit.key] = _Circuit()

    def failure(self, permit: Permit) -> None:
        """Count a deployment failure; open the circuit at the threshold, or
        again (with a new cooldown) when the probe failed."""
        with self._lock:
            circuit = self._get(permit.key)
            if not self._acts(permit, circuit):
                return
            failures = circuit.failures + 1
            if permit.probe is not None or failures >= self._failure_threshold:
                self._circuits[permit.key] = _Circuit("open", failures, self._clock())
            else:
                self._circuits[permit.key] = replace(circuit, failures=failures)

    def release(self, permit: Permit) -> None:
        """The attempt said nothing about the deployment: keep the count and the
        state, and free a probe so the next caller probes."""
        with self._lock:
            circuit = self._get(permit.key)
            if permit.probe is not None and self._acts(permit, circuit):
                self._circuits[permit.key] = replace(circuit, probe=None)

    def state(self, key: str) -> CircuitState:
        """The stored state: an open circuit turns half-open at the first
        ``acquire`` after its cooldown, not before."""
        with self._lock:
            return self._get(key).state


class Deadline:
    """The time left for one call, on an injectable clock."""

    def __init__(
        self, seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._ends_at = clock() + seconds

    def remaining(self) -> float:
        return max(0.0, self._ends_at - self._clock())
