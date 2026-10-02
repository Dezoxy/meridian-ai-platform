"""One pass over the allowed candidates of a model call (S042, S011, S045).

Routing keeps the candidates the data class may reach (T-44); this walk goes
through them in order under one deadline. A candidate the deadline leaves no
time for, or whose circuit is open, is skipped and reserves nothing. Before each
call the walk reserves the most the call can cost in the ledger (QA-12, T-14).
The one walk serves both purposes: an ``Operation`` says what to reserve, how to
call the provider and what the answer is, and nothing else differs.
A reservation the tenant's budget refuses ends the walk, because the token
reservation is the same for every candidate (the cost differs with the price;
both candidates have one price today). A deployment's own failure moves the
walk to the next candidate; a request the provider rejects ends it (T-45).
Every candidate touched leaves one audit row, and the audit write is part of the
answer (QA-05), unless the ledger's close fails: then the request answers 503
and the usage row, written before the call, is the record. There is no retry of
one deployment: the next candidate is the retry.

An attempt settles its circuit permit in ``_call_provider``, before the span
and the audit write, and a ``finally`` releases it on every other path (an
exception from the reservation, the span, the provider lookup, a
``BaseException``). Settling twice is harmless, so no failing write can leave a
probe held. The reservation is closed after the provider's ``except`` arms,
never inside one: ``_call_provider`` holds the arms and writes nothing to the
ledger or the audit log, so no database write happens inside an arm. The arm
keeps the exception, and the write follows, so a failed write carries none of
the provider's exception, which can hold prompt text, in its ``__context__``. A
``BaseException`` leaves the reservation open, which stays charged.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from opentelemetry.trace import Span, Tracer

from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.platform.gateway.budget import (
    BudgetRefusal,
    BudgetRefusalReason,
    Caller,
    Reservation,
    TokenEstimate,
)
from meridian.platform.gateway.meters import GatewayMeters
from meridian.platform.gateway.providers.base import (
    ModelProvider,
    ProviderError,
    ProviderErrorKind,
    Reply,
)
from meridian.platform.gateway.resilience import (
    CALL_DEADLINE_SECONDS,
    DEPLOYMENT_FAILURES,
    MIN_ATTEMPT_SECONDS,
    CircuitBreaker,
    Deadline,
    Permit,
)
from meridian.platform.gateway.routing import RouteDecision
from meridian.platform.gateway.settings import GatewayMode
from meridian.platform.registry.models import Deployment, TenantLimits

# The audit reason and span error type of a provider call that raised something
# other than a ProviderError.
INTERNAL_REASON = "internal"
# The audit reasons and span error types of a candidate that was not called.
SKIP_DEADLINE = "deadline"
SKIP_CIRCUIT_OPEN = "circuit-open"
# A 4xx means the provider refused the request before running it, a 408 apart:
# that is a timeout the provider ran into, and may have been billed.
CLIENT_ERRORS = range(400, 500)
REQUEST_TIMEOUT = 408


class AuditWriter(Protocol):
    """Writes one row and raises when it cannot."""

    def __call__(self, event: str, outcome: str, **fields: object) -> None: ...


class ReservationLedger(Protocol):
    """Reserves the cost of an attempt and closes the reservation; a database
    error propagates from every method."""

    def reserve(
        self,
        caller: Caller,
        deployment: Deployment,
        limits: TenantLimits,
        estimate: TokenEstimate,
    ) -> Reservation | BudgetRefusal: ...

    def settle(
        self,
        reservation: Reservation,
        deployment: Deployment,
        input_tokens: int,
        output_tokens: int,
    ) -> int: ...

    def release(self, reservation: Reservation) -> None: ...

    def keep(self, reservation: Reservation) -> None: ...


def caller_fields(caller: Caller) -> dict[str, object]:
    """The audit columns that say who made the call and which call it was."""
    return {
        "tenant": caller.tenant,
        "agent": caller.agent,
        "run_id": caller.run_id,
        "call_id": caller.call_id,
    }


def route_facts(
    deployment: Deployment | None, data_class: str | None
) -> dict[str, str | None]:
    """The audit columns that say where a call went and why it could go there."""
    facts: dict[str, str | None] = {"data_class": data_class}
    if deployment is not None:
        facts |= {
            "deployment": deployment.id,
            "provider": deployment.provider,
            "model": deployment.model,
            "sku": deployment.sku,
            "region": deployment.region,
            "residency": deployment.residency,
        }
    return facts


def route_attributes(
    deployment: Deployment, data_class: str | None = None
) -> dict[str, str]:
    """The span attributes for the deployment a call goes to. A replay
    deployment has no sku or region; before policy decides there is no data
    class to say."""
    attributes = {
        "meridian.deployment": deployment.id,
        "meridian.provider": deployment.provider,
        "gen_ai.request.model": deployment.model,
        "meridian.residency": deployment.residency,
    }
    if data_class is not None:
        attributes["meridian.data_class"] = data_class
    if deployment.sku is not None:
        attributes["meridian.sku"] = deployment.sku
    if deployment.region is not None:
        attributes["meridian.region"] = deployment.region
    return attributes


def closing_for(failure: Exception) -> Literal["release", "keep"]:
    """How to close the reservation of an attempt that raised ``failure``.

    Release only when the gateway knows the provider billed nothing: the
    request never left (``sent`` is false), or the provider answered with a 4xx
    other than 408, which means it refused the request before running it. Keep
    the reservation as the charge for everything else: a 5xx, a timeout, a lost
    connection, an unreadable reply and a completion the content filter
    withheld may all have been billed, and any other exception is no knowledge
    at all. Over-charging is the accepted side; under-counting is the worse
    error.
    """
    if not isinstance(failure, ProviderError):
        return "keep"
    status = failure.status_code
    refused_before_running = (
        status is not None and status in CLIENT_ERRORS and status != REQUEST_TIMEOUT
    )
    return "release" if not failure.sent or refused_before_running else "keep"


@dataclass(frozen=True, slots=True)
class Unanswered:
    """No candidate answered: how many were called and the last one's kind."""

    attempts: int
    last_attempt_kind: str | None


@dataclass(frozen=True, slots=True)
class BudgetRefused:
    """The tenant's budget refused a reservation before any candidate was
    called: why, and the deployment the reservation was for."""

    reason: BudgetRefusalReason
    candidate: Deployment


@dataclass(frozen=True, slots=True)
class Operation[ReplyT: Reply, ResponseT]:
    """What one request asks of the walk, built once and never changed: what to
    reserve (the number the rate limiter admitted too), how to call a provider
    within a budget of seconds, and how to turn the reply of the candidate that
    answered into the wire response."""

    estimate: TokenEstimate
    call: Callable[[ModelProvider, Deployment, float], ReplyT]
    respond: Callable[[uuid.UUID, GatewayMode, Deployment, ReplyT], ResponseT]


@dataclass(frozen=True, slots=True)
class _Call[ReplyT: Reply, ResponseT]:
    """What one request brings to the walk, built once and never changed."""

    caller: Caller
    limits: TenantLimits
    operation: Operation[ReplyT, ResponseT]
    decision: RouteDecision


@dataclass(frozen=True, slots=True)
class _Answered[ReplyT: Reply]:
    """An attempt the provider answered, and what the ledger charged for it."""

    reply: ReplyT
    charged_micro_eur: int


@dataclass(slots=True)
class _Progress:
    """What one pass has done so far; one per request."""

    attempts: int = 0
    skipped: int = 0
    last_called: Deployment | None = None  # the last candidate that was called
    last_attempt_kind: str | None = None  # the kind that picks the status
    last_skip_reason: str | None = None  # why the last skipped one was skipped
    answered: bool = False
    budget_refused: bool = False  # refused before any call: no error to name


class CandidateWalker:
    def __init__(
        self,
        *,
        tracer: Tracer,
        providers: Mapping[str, ModelProvider],
        kinds: Mapping[str, str],
        breaker: CircuitBreaker,
        audit: AuditWriter,
        ledger: ReservationLedger,
        meters: GatewayMeters,
        clock: Callable[[], float],
        mode: GatewayMode,
    ) -> None:
        self._tracer = tracer
        self._providers = providers
        self._kinds = kinds
        self._breaker = breaker
        self._audit = audit
        self._ledger = ledger
        self._meters = meters
        self._clock = clock
        self._mode = mode

    def run[ReplyT: Reply, ResponseT](
        self,
        span: Span,
        caller: Caller,
        limits: TenantLimits,
        operation: Operation[ReplyT, ResponseT],
        decision: RouteDecision,
    ) -> ResponseT | Unanswered | BudgetRefused:
        """Walk ``decision.candidates``; the response of the first to answer."""
        call = _Call(caller, limits, operation, decision)
        deadline = Deadline(CALL_DEADLINE_SECONDS, self._clock)
        progress = _Progress()
        try:
            for deployment in decision.candidates:
                if deadline.remaining() < MIN_ATTEMPT_SECONDS:
                    self._skip(call, progress, deployment, SKIP_DEADLINE)
                    continue
                permit = self._breaker.acquire(deployment.id)
                if permit is None:
                    self._skip(call, progress, deployment, SKIP_CIRCUIT_OPEN)
                    continue
                outcome = self._attempt(call, progress, deployment, permit, deadline)
                if isinstance(outcome, _Answered):
                    response = self._answer(span, call, deployment, outcome)
                    progress.answered = True  # only once the answer is recorded
                    return response
                if isinstance(outcome, BudgetRefusal):
                    return self._budget_refused(call, progress, deployment, outcome)
                if outcome is not None and outcome not in DEPLOYMENT_FAILURES:
                    break  # the request itself was refused: no other will do
            return Unanswered(progress.attempts, progress.last_attempt_kind)
        finally:
            self._finish_span(span, progress, decision.data_class)

    def _skip(
        self,
        call: _Call,
        progress: _Progress,
        deployment: Deployment,
        reason: str,
    ) -> None:
        progress.skipped += 1
        progress.last_skip_reason = reason
        self._audit(
            "model.call",
            "skipped",
            **caller_fields(call.caller),
            **route_facts(deployment, call.decision.data_class),
            reason=reason,
        )

    def _budget_refused(
        self,
        call: _Call,
        progress: _Progress,
        deployment: Deployment,
        refusal: BudgetRefusal,
    ) -> Unanswered | BudgetRefused:
        """The walk ends here. With no attempt made the request is refused for
        the budget, and the caller answers it (and audits it, throttled).
        After an earlier attempt that one decides the answer, and this
        candidate leaves a skipped row that says why."""
        if progress.attempts == 0:
            progress.budget_refused = True
            return BudgetRefused(refusal.reason, deployment)
        self._skip(call, progress, deployment, refusal.reason)
        return Unanswered(progress.attempts, progress.last_attempt_kind)

    def _attempt[ReplyT: Reply](
        self,
        call: _Call[ReplyT, object],
        progress: _Progress,
        deployment: Deployment,
        permit: Permit,
        deadline: Deadline,
    ) -> _Answered[ReplyT] | BudgetRefusal | ProviderErrorKind | None:
        """One call to one deployment, in its own span: the answer, the
        refusal of its reservation (the candidate was not called), the kind of
        the ``ProviderError`` it raised, or ``None`` when the reservation took
        so long that the deadline left no time to call (released, skipped, no
        attempt counted). Any other exception is re-raised after its row is
        written."""
        try:
            provider = self._providers[self._kinds[deployment.id]]
            reservation = self._ledger.reserve(
                call.caller, deployment, call.limits, call.operation.estimate
            )
            if isinstance(reservation, BudgetRefusal):
                return reservation  # not called: the finally frees the permit
            remaining = deadline.remaining()  # the reservation took time
            if remaining < MIN_ATTEMPT_SECONDS:
                self._ledger.release(reservation)
                self._skip(call, progress, deployment, SKIP_DEADLINE)
                return None
            progress.attempts += 1
            progress.last_called = deployment
            with start_span(self._tracer, "gateway.attempt") as span:
                set_span_attributes(
                    span,
                    route_attributes(deployment, call.decision.data_class)
                    | {"meridian.attempt": progress.attempts},
                )
                result = self._call_provider(
                    provider, deployment, call.operation.call, permit, remaining
                )
                charged = self._close(call.caller, reservation, deployment, result)
                if not isinstance(result, Exception):
                    return _Answered(result, charged)
                return self._failed(span, call, progress, deployment, result)
        finally:
            # After success or failure this changes nothing; on every other path
            # (an exception before the call, a BaseException) it frees the probe.
            self._breaker.release(permit)

    def _call_provider[ReplyT: Reply](
        self,
        provider: ModelProvider,
        deployment: Deployment,
        call: Callable[[ModelProvider, Deployment, float], ReplyT],
        permit: Permit,
        timeout_seconds: float,
    ) -> ReplyT | Exception:
        """The provider's reply, or the exception it raised, with the circuit
        settled. This method writes nothing to the ledger or the audit log: the
        arms below hold no database write."""
        result: ReplyT | Exception
        try:
            result = call(provider, deployment, timeout_seconds)
        except ProviderError as error:
            if error.kind in DEPLOYMENT_FAILURES:
                self._breaker.failure(permit)
            else:
                self._breaker.release(permit)
            result = error
        except Exception as error:
            # Whatever it was, the call may have reached the provider, so it
            # leaves a row. Nothing of the exception is kept in the row or on a
            # span (T-03, T-18); it is re-raised for the unexpected-error
            # middleware's 500.
            self._breaker.release(permit)
            result = error
        else:
            self._breaker.success(permit)
        return result

    def _close(
        self,
        caller: Caller,
        reservation: Reservation,
        deployment: Deployment,
        result: Reply | Exception,
    ) -> int:
        """Close the reservation outside the arms; the micro-EUR charged to an
        answer, and nothing for a failure. A failing write propagates and the
        row stays reserved, so the charge stands."""
        if not isinstance(result, Exception):
            charged = self._ledger.settle(
                reservation, deployment, result.input_tokens, result.output_tokens
            )
            self._meters.settled(
                caller.tenant,
                caller.agent,
                deployment,
                result.input_tokens,
                result.output_tokens,
                charged,
            )
            return charged
        if closing_for(result) == "release":
            self._ledger.release(reservation)
        else:
            self._ledger.keep(reservation)
        return 0

    def _failed(
        self,
        span: Span,
        call: _Call,
        progress: _Progress,
        deployment: Deployment,
        failure: Exception,
    ) -> ProviderErrorKind:
        """The span and the row of a failed attempt, written outside the
        ``except`` arm that caught it: the kind to walk on with, or the
        exception re-raised."""
        reason = failure.kind if isinstance(failure, ProviderError) else INTERNAL_REASON
        progress.last_attempt_kind = reason
        set_span_attributes(span, {"error.type": reason})
        status = None
        if isinstance(failure, ProviderError):
            mark_error(span, failure)
            status = failure.status_code
        self._audit(
            "model.call",
            "failed",
            **caller_fields(call.caller),
            **route_facts(deployment, call.decision.data_class),
            reason=reason,
            http_status=status,
        )
        if isinstance(failure, ProviderError):
            return failure.kind
        raise failure

    def _answer[ReplyT: Reply, ResponseT](
        self,
        span: Span,
        call: _Call[ReplyT, ResponseT],
        deployment: Deployment,
        answered: _Answered[ReplyT],
    ) -> ResponseT:
        reply = answered.reply
        response = call.operation.respond(
            call.caller.call_id, self._mode, deployment, reply
        )
        data_class = call.decision.data_class
        set_span_attributes(
            span,
            route_attributes(deployment, data_class)
            | {
                "gen_ai.usage.input_tokens": reply.input_tokens,
                "gen_ai.usage.output_tokens": reply.output_tokens,
                "gen_ai.response.model": reply.model,
                "meridian.cost_micro_eur": answered.charged_micro_eur,
            },
        )
        # The last thing that can fail: nothing after the audit write may.
        self._audit(
            "model.call",
            "completed",
            **caller_fields(call.caller),
            **route_facts(deployment, data_class),
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            provider_model=reply.model,
        )
        return response

    @staticmethod
    def _finish_span(span: Span, progress: _Progress, data_class: str | None) -> None:
        """The parent span's closing attributes: the counts always, and, when
        nothing answered, the last candidate that was called and its kind, or,
        when none was called, the last reason for skipping. A request refused
        for the budget names no error: the refusal is the answer."""
        set_span_attributes(
            span,
            {
                "meridian.attempts": progress.attempts,
                "meridian.skipped": progress.skipped,
            },
        )
        if progress.answered or progress.budget_refused:
            return
        called = progress.last_called
        if called is not None:
            kind = progress.last_attempt_kind or INTERNAL_REASON
            set_span_attributes(
                span, route_attributes(called, data_class) | {"error.type": kind}
            )
        elif progress.last_skip_reason is not None:
            set_span_attributes(span, {"error.type": progress.last_skip_reason})
