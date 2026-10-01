"""One pass over the allowed candidates of a model call (S042).

Routing keeps the candidates the data class may reach (T-44); this walk goes
through them in order under one deadline. A candidate the deadline leaves no
time for, or whose circuit is open, is skipped. A deployment's own failure moves
the walk to the next candidate; a request the provider rejects ends it (T-45).
Every candidate touched leaves one audit row, and the audit write is part of
the answer (QA-05). There is no retry of one deployment: the next candidate is
the retry.

An attempt settles its circuit permit in its own arm, before the span and the
audit write, and a ``finally`` releases it on every other path (an exception
from the span, the provider lookup, a ``BaseException``). Settling twice is
harmless, so no failing write can leave a probe held. No audit write happens
inside an ``except`` arm: the arm keeps the kind or the exception, and the write
follows, so a failed write carries none of the provider's exception, which can
hold prompt text, in its ``__context__``.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from opentelemetry.trace import Span, Tracer

from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.platform.gateway.models import (
    ChatOutput,
    ChatRequest,
    ChatResponse,
    Usage,
)
from meridian.platform.gateway.providers.base import (
    ChatProvider,
    ProviderError,
    ProviderErrorKind,
    ProviderReply,
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
from meridian.platform.registry.models import Deployment

# The audit reason and span error type of a provider call that raised something
# other than a ProviderError.
INTERNAL_REASON = "internal"
# The audit reasons and span error types of a candidate that was not called.
SKIP_DEADLINE = "deadline"
SKIP_CIRCUIT_OPEN = "circuit-open"


class AuditWriter(Protocol):
    """Writes one row and raises when it cannot."""

    def __call__(self, event: str, outcome: str, **fields: object) -> None: ...


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


@dataclass(frozen=True, slots=True)
class Unanswered:
    """No candidate answered: how many were called and the last one's kind."""

    attempts: int
    last_attempt_kind: str | None


@dataclass(slots=True)
class _Progress:
    """What one pass has done so far; one per request."""

    attempts: int = 0
    skipped: int = 0
    last_called: Deployment | None = None  # the last candidate that was called
    last_attempt_kind: str | None = None  # the kind that picks the status
    last_skip_reason: str | None = None  # why the last skipped one was skipped
    answered: bool = False


class CandidateWalker:
    def __init__(
        self,
        *,
        tracer: Tracer,
        providers: Mapping[str, ChatProvider],
        kinds: Mapping[str, str],
        breaker: CircuitBreaker,
        audit: AuditWriter,
        clock: Callable[[], float],
        mode: GatewayMode,
    ) -> None:
        self._tracer = tracer
        self._providers = providers
        self._kinds = kinds
        self._breaker = breaker
        self._audit = audit
        self._clock = clock
        self._mode = mode

    def run(
        self,
        span: Span,
        who: dict[str, object],
        body: ChatRequest,
        decision: RouteDecision,
    ) -> ChatResponse | Unanswered:
        """Walk ``decision.candidates``; the response of the first to answer."""
        deadline = Deadline(CALL_DEADLINE_SECONDS, self._clock)
        progress = _Progress()
        try:
            for deployment in decision.candidates:
                remaining = deadline.remaining()
                if remaining < MIN_ATTEMPT_SECONDS:
                    self._skip(who, progress, deployment, decision, SKIP_DEADLINE)
                elif (permit := self._breaker.acquire(deployment.id)) is None:
                    self._skip(who, progress, deployment, decision, SKIP_CIRCUIT_OPEN)
                else:
                    outcome = self._attempt(
                        who, progress, deployment, decision, body, permit, remaining
                    )
                    if isinstance(outcome, ProviderReply):
                        progress.answered = True
                        return self._answer(span, who, deployment, decision, outcome)
                    if outcome not in DEPLOYMENT_FAILURES:
                        break  # the request itself was refused: no other will do
            return Unanswered(progress.attempts, progress.last_attempt_kind)
        finally:
            self._finish_span(span, progress, decision.data_class)

    def _skip(
        self,
        who: dict[str, object],
        progress: _Progress,
        deployment: Deployment,
        decision: RouteDecision,
        reason: str,
    ) -> None:
        progress.skipped += 1
        progress.last_skip_reason = reason
        self._audit(
            "model.call",
            "skipped",
            **who,
            **route_facts(deployment, decision.data_class),
            reason=reason,
        )

    def _attempt(
        self,
        who: dict[str, object],
        progress: _Progress,
        deployment: Deployment,
        decision: RouteDecision,
        body: ChatRequest,
        permit: Permit,
        timeout_seconds: float,
    ) -> ProviderReply | ProviderErrorKind:
        """One call to one deployment, in its own span: the reply, or the kind
        of the ``ProviderError`` it raised. Any other exception is re-raised
        after its row is written."""
        try:
            progress.attempts += 1
            progress.last_called = deployment
            provider = self._providers[self._kinds[deployment.id]]
            with start_span(self._tracer, "gateway.attempt") as span:
                set_span_attributes(
                    span,
                    route_attributes(deployment, decision.data_class)
                    | {"meridian.attempt": progress.attempts},
                )
                failure: Exception
                try:
                    reply = provider.chat(
                        deployment, body, timeout_seconds=timeout_seconds
                    )
                except ProviderError as error:
                    if error.kind in DEPLOYMENT_FAILURES:
                        self._breaker.failure(permit)
                    else:
                        self._breaker.release(permit)
                    failure = error
                except Exception as error:
                    # Whatever it was, the call may have reached the provider,
                    # so it leaves a row. Nothing of the exception is kept in
                    # the row or on a span (T-03, T-18); it is re-raised for the
                    # unexpected-error middleware's 500.
                    self._breaker.release(permit)
                    failure = error
                else:
                    self._breaker.success(permit)
                    return reply
                return self._failed(span, who, progress, deployment, decision, failure)
        finally:
            # After success or failure this changes nothing; on every other path
            # (an exception before the call, a BaseException) it frees the probe.
            self._breaker.release(permit)

    def _failed(
        self,
        span: Span,
        who: dict[str, object],
        progress: _Progress,
        deployment: Deployment,
        decision: RouteDecision,
        failure: Exception,
    ) -> ProviderErrorKind:
        """The span and the row of a failed attempt, written outside the
        ``except`` arm that caught it: the kind to walk on with, or the
        exception re-raised."""
        reason = failure.kind if isinstance(failure, ProviderError) else INTERNAL_REASON
        progress.last_attempt_kind = reason
        set_span_attributes(span, {"error.type": reason})
        if isinstance(failure, ProviderError):
            mark_error(span, failure)
        self._audit(
            "model.call",
            "failed",
            **who,
            **route_facts(deployment, decision.data_class),
            reason=reason,
        )
        if isinstance(failure, ProviderError):
            return failure.kind
        raise failure

    def _answer(
        self,
        span: Span,
        who: dict[str, object],
        deployment: Deployment,
        decision: RouteDecision,
        reply: ProviderReply,
    ) -> ChatResponse:
        response = ChatResponse(
            call_id=uuid.uuid4(),
            mode=self._mode,
            deployment=deployment.id,
            provider=deployment.provider,
            model=deployment.model,
            output=ChatOutput(text=reply.text, finish_reason=reply.finish_reason),
            usage=Usage(
                input_tokens=reply.input_tokens, output_tokens=reply.output_tokens
            ),
        )
        set_span_attributes(
            span,
            route_attributes(deployment, decision.data_class)
            | {
                "gen_ai.usage.input_tokens": reply.input_tokens,
                "gen_ai.usage.output_tokens": reply.output_tokens,
                "gen_ai.response.model": reply.model,
            },
        )
        # The last thing that can fail: nothing after the audit write may.
        self._audit(
            "model.call",
            "completed",
            **who,
            **route_facts(deployment, decision.data_class),
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
        )
        return response

    @staticmethod
    def _finish_span(span: Span, progress: _Progress, data_class: str | None) -> None:
        """The parent span's closing attributes: the counts always, and, when
        nothing answered, the last candidate that was called and its kind, or,
        when none was called, the last reason for skipping."""
        set_span_attributes(
            span,
            {
                "meridian.attempts": progress.attempts,
                "meridian.skipped": progress.skipped,
            },
        )
        if progress.answered:
            return
        called = progress.last_called
        if called is not None:
            kind = progress.last_attempt_kind or INTERNAL_REASON
            set_span_attributes(
                span, route_attributes(called, data_class) | {"error.type": kind}
            )
        elif progress.last_skip_reason is not None:
            set_span_attributes(span, {"error.type": progress.last_skip_reason})
