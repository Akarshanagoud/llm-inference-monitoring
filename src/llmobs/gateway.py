"""The instrumented request path.

Everything else in this package is a component; this is the thing that makes
them one system. A request through the gateway produces, from a single code
path: a trace with per-stage spans, Prometheus metrics, a validation verdict,
an SLO event, and a stored trace — all correlated by trace id.

That correlation is the whole point. Metrics tell you *something* regressed,
the SLO tells you whether to care, and the trace tells you *which stage* did
it. Systems that bolt these on separately end up with three sources of truth
that disagree during an incident.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .backends.base import Backend, BackendError, GenerationRequest, GenerationResponse
from .metrics import MetricsRegistry
from .slo import SLOTracker
from .store import TraceStore
from .trace import SpanKind, SpanStatus, Tracer
from .validation import ResponseValidator, ValidationResult


@dataclass
class GatewayConfig:
    service: str = "llm-gateway"
    max_retries: int = 2
    retry_base_delay: float = 0.1
    timeout_seconds: float = 120.0
    sample_rate: float = 1.0
    capture_text: bool = False
    validate_responses: bool = True
    #: Reject requests once this many are already in flight. Shedding load at
    #: the edge beats letting the queue grow until every request times out.
    max_in_flight: int = 0  # 0 disables shedding
    trace_store_path: str | None = None
    metrics_namespace: str = "llm"
    default_validation_context: dict[str, Any] = field(default_factory=dict)


class OverloadedError(RuntimeError):
    """Raised when the gateway sheds load rather than queueing indefinitely."""


@dataclass
class GatewayResult:
    text: str
    trace_id: str
    model: str
    duration_ms: float
    ttft_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int
    attempts: int
    validation: ValidationResult | None
    breakdown_ms: dict[str, float] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.validation is None or self.validation.valid

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "trace_id": self.trace_id,
            "model": self.model,
            "duration_ms": round(self.duration_ms, 3),
            "ttft_ms": round(self.ttft_ms, 3) if self.ttft_ms else None,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "cached_prompt_tokens": self.cached_prompt_tokens,
            },
            "attempts": self.attempts,
            "valid": self.valid,
            "validation": self.validation.to_dict() if self.validation else None,
            "breakdown_ms": self.breakdown_ms,
        }


class InferenceGateway:
    """Wraps a backend with tracing, metrics, validation and SLO accounting."""

    def __init__(
        self,
        backend: Backend,
        config: GatewayConfig | None = None,
        *,
        tracer: Tracer | None = None,
        metrics: MetricsRegistry | None = None,
        validator: ResponseValidator | None = None,
        slo_tracker: SLOTracker | None = None,
        store: TraceStore | None = None,
    ) -> None:
        self.backend = backend
        self.config = config or GatewayConfig()
        self.metrics = metrics or MetricsRegistry(self.config.metrics_namespace)
        self.validator = validator or ResponseValidator()
        self.slo = slo_tracker or SLOTracker()
        self.tracer = tracer or Tracer(
            service=self.config.service,
            sample_rate=self.config.sample_rate,
            capture_text=self.config.capture_text,
        )
        self.store = store
        if self.store is None and self.config.trace_store_path:
            self.store = TraceStore(self.config.trace_store_path)
        if self.store is not None:
            self.tracer.add_exporter(self.store)

        self._in_flight = 0
        #: Hooks fired after each request, e.g. to push to an external sink.
        self._listeners: list[Callable[[GatewayResult], None]] = []

    def add_listener(self, listener: Callable[[GatewayResult], None]) -> None:
        self._listeners.append(listener)

    # -- request path -------------------------------------------------------
    def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_tokens: int = 256,
        temperature: float = 0.0,
        stream: bool = False,
        validation_context: dict[str, Any] | None = None,
        **extra: Any,
    ) -> GatewayResult:
        if self.config.max_in_flight and self._in_flight >= self.config.max_in_flight:
            self.metrics.errors.inc(
                model=model or "unknown", backend=self.backend.name, status="shed"
            )
            raise OverloadedError(
                f"{self._in_flight} requests in flight, limit is {self.config.max_in_flight}"
            )

        request = GenerationRequest(
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=stream,
            extra=extra,
        )

        self._in_flight += 1
        self.metrics.in_flight.set(self._in_flight)
        started = time.perf_counter()

        try:
            with self.tracer.trace("inference_request", model=model or "default") as root:
                response, attempts, ttft_ms = self._call_with_retries(request, root)

                validation: ValidationResult | None = None
                if self.config.validate_responses:
                    with self.tracer.span("validate", SpanKind.VALIDATION) as span:
                        context = {
                            **self.config.default_validation_context,
                            **(validation_context or {}),
                            "finish_reason": response.finish_reason,
                            "max_tokens": max_tokens,
                        }
                        validation = self.validator.validate(response.text, **context)
                        span.set(valid=validation.valid, failed=validation.checks_failed)

                self.tracer.record_prompt(root, prompt, response.text)
                root.set(
                    valid=validation.valid if validation else True,
                    attempts=attempts,
                    backend=self.backend.name,
                )
                root.model = response.model
                root.backend = self.backend.name
                root.usage.prompt_tokens = response.prompt_tokens
                root.usage.completion_tokens = response.completion_tokens
                root.usage.cached_prompt_tokens = response.cached_prompt_tokens
                root.ttft_ms = ttft_ms
                root.finish(SpanStatus.OK)

                duration_ms = (time.perf_counter() - started) * 1000
                # Computed inside the trace context: once the root span exits,
                # the trace has been exported and is no longer reachable.
                active = self.tracer.current_trace()
                breakdown = active.breakdown() if active else {}
                result = GatewayResult(
                    text=response.text,
                    trace_id=root.trace_id,
                    model=response.model,
                    duration_ms=duration_ms,
                    ttft_ms=ttft_ms,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    cached_prompt_tokens=response.cached_prompt_tokens,
                    attempts=attempts,
                    validation=validation,
                    breakdown_ms=breakdown,
                )

                self.metrics.observe_span(root)
                self.slo.record(ok=True, latency_seconds=duration_ms / 1000)
                self._notify(result)
                return result

        except BackendError:
            duration_ms = (time.perf_counter() - started) * 1000
            self.metrics.errors.inc(
                model=model or "unknown", backend=self.backend.name, status="error"
            )
            self.metrics.requests.inc(
                model=model or "unknown", backend=self.backend.name, status="error"
            )
            self.metrics.latency.observe(
                duration_ms / 1000, model=model or "unknown", backend=self.backend.name
            )
            self.slo.record(ok=False, latency_seconds=duration_ms / 1000)
            raise
        finally:
            self._in_flight -= 1
            self.metrics.in_flight.set(self._in_flight)

    def _call_with_retries(
        self, request: GenerationRequest, root: Any
    ) -> tuple[GenerationResponse, int, float | None]:
        """Call the backend, retrying only errors the backend marked retryable.

        Retries are counted into their own span so a request that took 6
        seconds because it was attempted three times is distinguishable from
        one slow call — those have completely different fixes.
        """
        last_error: BackendError | None = None
        for attempt in range(1, self.config.max_retries + 2):
            kind = SpanKind.INFERENCE if attempt == 1 else SpanKind.RETRY
            with self.tracer.span(f"backend.generate#{attempt}", kind) as span:
                span.backend = self.backend.name
                span.model = request.model
                span.set(attempt=attempt)
                try:
                    if request.stream:
                        chunks: list[str] = []
                        for chunk in self.backend.stream(request):
                            if not chunks:
                                span.record_first_token()
                            chunks.append(chunk)
                        text = "".join(chunks)
                        response = GenerationResponse(
                            text=text,
                            model=request.model or getattr(self.backend, "model", "unknown"),
                            prompt_tokens=max(1, len(request.prompt) // 4),
                            completion_tokens=max(1, len(text) // 4),
                        )
                    else:
                        response = self.backend.generate(request)
                        # Non-streaming: first token and last token arrive
                        # together, so TTFT equals the call duration.
                        span.record_first_token()

                    span.usage.prompt_tokens = response.prompt_tokens
                    span.usage.completion_tokens = response.completion_tokens
                    span.usage.cached_prompt_tokens = response.cached_prompt_tokens
                    return response, attempt, span.ttft_ms

                except BackendError as exc:
                    last_error = exc
                    span.finish(SpanStatus.ERROR, str(exc))
                    span.add_event("backend_error", retryable=exc.retryable)
                    if not exc.retryable or attempt > self.config.max_retries:
                        raise
                    self.metrics.retries.inc(
                        model=request.model or "unknown", backend=self.backend.name
                    )
                    root.add_event("retry_scheduled", attempt=attempt)
                    time.sleep(self.config.retry_base_delay * (2 ** (attempt - 1)))

        raise last_error or BackendError("exhausted retries")

    def stream(self, prompt: str, **kwargs: Any) -> Iterator[str]:
        """Stream deltas while still recording a full trace on completion."""
        request = GenerationRequest(prompt=prompt, stream=True, **_stream_kwargs(kwargs))
        with self.tracer.trace("inference_stream") as root:
            root.backend = self.backend.name
            root.model = request.model
            chunks: list[str] = []
            try:
                for chunk in self.backend.stream(request):
                    if not chunks:
                        root.record_first_token()
                    chunks.append(chunk)
                    yield chunk
            except BackendError as exc:
                root.finish(SpanStatus.ERROR, str(exc))
                self.slo.record(ok=False, latency_seconds=(root.duration_ms or 0) / 1000)
                raise
            text = "".join(chunks)
            root.usage.prompt_tokens = max(1, len(prompt) // 4)
            root.usage.completion_tokens = max(1, len(text) // 4)
            root.finish()
            self.metrics.observe_span(root)
            self.slo.record(ok=True, latency_seconds=(root.duration_ms or 0) / 1000)

    # -- observability surface ---------------------------------------------
    def refresh_backend_health(self) -> dict[str, Any]:
        """Probe the backend and mirror its telemetry into our metric set."""
        health = self.backend.health()
        self.metrics.model_up.set(1.0 if health.up else 0.0, backend=self.backend.name)
        self.metrics.queue_depth.set(health.queue_depth, backend=self.backend.name)
        self.metrics.batch_size.set(health.running_batch_size, backend=self.backend.name)
        self.metrics.kv_cache_utilization.set(
            health.kv_cache_utilization, backend=self.backend.name
        )
        return health.to_dict()

    def prometheus(self) -> str:
        self.refresh_backend_health()
        return self.metrics.render()

    def snapshot(self) -> dict[str, Any]:
        return {
            "service": self.config.service,
            "backend": self.backend.name,
            "metrics": self.metrics.snapshot(),
            "slos": [s.to_dict() for s in self.slo.all_status()],
            "worst_severity": self.slo.worst_severity().value,
            "traces": self.store.summary() if self.store else None,
        }

    def _notify(self, result: GatewayResult) -> None:
        for listener in self._listeners:
            try:
                listener(result)
            except Exception:  # a listener must not fail the request
                continue

    def close(self) -> None:
        self.backend.close()
        if self.store is not None:
            self.store.close()


def _stream_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    known = {"model", "max_tokens", "temperature", "top_p", "stop", "request_id"}
    out = {k: v for k, v in kwargs.items() if k in known}
    extra = {k: v for k, v in kwargs.items() if k not in known}
    if extra:
        out["extra"] = extra
    return out
