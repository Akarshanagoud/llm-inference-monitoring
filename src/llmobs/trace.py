"""Prompt tracing.

An LLM request is not one operation, it is a chain: retrieve, template, call
the model, validate, maybe retry. When p99 latency doubles, "the model is slow"
is almost never the answer — the retrieval step went from 20 ms to 900 ms, or
you are silently retrying once per request. A trace that only records total
latency cannot tell you which, so this module records spans.

Design notes:

* **Sampling decisions are made once, at the root, and inherited.** A trace
  with half its spans missing is worse than no trace.
* **Errors are always sampled.** The 0.1% of requests you actually need to look
  at are exactly the ones a uniform sampler throws away.
* **Prompts and completions are hashed by default.** Storing raw text turns a
  trace store into an uncontrolled copy of your customer data.
"""
from __future__ import annotations

import contextvars
import hashlib
import random
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_current_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "llmobs_current_span", default=None
)


def _restore(token: contextvars.Token, previous: Span | None) -> None:
    """Restore the ambient span, tolerating a context switch.

    A streaming response is a generator: Starlette may resume it on a
    different thread, and therefore in a different context, from the one that
    created the token. ``ContextVar.reset`` rejects a foreign token, so fall
    back to assigning the previous value directly.
    """
    try:
        _current_span.reset(token)
    except ValueError:
        _current_span.set(previous)


class SpanKind(str, Enum):
    REQUEST = "request"
    RETRIEVAL = "retrieval"
    TEMPLATE = "template"
    INFERENCE = "inference"
    VALIDATION = "validation"
    GUARDRAIL = "guardrail"
    TOOL = "tool"
    RETRY = "retry"


class SpanStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class TokenUsage:
    """Token accounting for one inference span."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_rate(self) -> float:
        if not self.prompt_tokens:
            return 0.0
        return self.cached_prompt_tokens / self.prompt_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class Span:
    """One timed step inside a request."""

    name: str
    kind: SpanKind
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: str | None = None
    started_at: float = field(default_factory=time.time)
    start_perf: float = field(default_factory=time.perf_counter)
    ended_at: float | None = None
    duration_ms: float | None = None
    status: SpanStatus = SpanStatus.OK
    error: str | None = None
    sampled: bool = True

    model: str | None = None
    backend: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    #: Time to first token. The number users actually feel on a streaming UI —
    #: total latency can look fine while TTFT is terrible.
    ttft_ms: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def set(self, **attributes: Any) -> Span:
        self.attributes.update(attributes)
        return self

    def add_event(self, name: str, **attributes: Any) -> Span:
        self.events.append(
            {"name": name, "timestamp": time.time(), "attributes": attributes}
        )
        return self

    def record_first_token(self) -> None:
        """Call when the first token arrives from a streaming backend."""
        if self.ttft_ms is None:
            self.ttft_ms = (time.perf_counter() - self.start_perf) * 1000

    def finish(self, status: SpanStatus = SpanStatus.OK, error: str | None = None) -> None:
        if self.ended_at is not None:
            return
        self.ended_at = time.time()
        self.duration_ms = (time.perf_counter() - self.start_perf) * 1000
        self.status = status
        self.error = error

    @property
    def tokens_per_second(self) -> float | None:
        """Decode throughput, excluding the prefill phase where TTFT is known."""
        if not self.duration_ms or not self.usage.completion_tokens:
            return None
        decode_ms = self.duration_ms - (self.ttft_ms or 0.0)
        if decode_ms <= 0:
            return None
        return self.usage.completion_tokens / (decode_ms / 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind.value,
            "started_at": self.started_at,
            "duration_ms": round(self.duration_ms, 3) if self.duration_ms else None,
            "status": self.status.value,
            "error": self.error,
            "model": self.model,
            "backend": self.backend,
            "ttft_ms": round(self.ttft_ms, 3) if self.ttft_ms else None,
            "tokens_per_second": round(self.tokens_per_second, 2)
            if self.tokens_per_second
            else None,
            "usage": self.usage.to_dict(),
            "attributes": self.attributes,
            "events": self.events,
        }


@dataclass
class Trace:
    """A complete request: the root span and everything beneath it."""

    trace_id: str
    spans: list[Span] = field(default_factory=list)

    @property
    def root(self) -> Span | None:
        return next((s for s in self.spans if s.parent_id is None), None)

    @property
    def duration_ms(self) -> float:
        root = self.root
        return root.duration_ms or 0.0 if root else 0.0

    @property
    def status(self) -> SpanStatus:
        if any(s.status is SpanStatus.ERROR for s in self.spans):
            return SpanStatus.ERROR
        if any(s.status is SpanStatus.TIMEOUT for s in self.spans):
            return SpanStatus.TIMEOUT
        return SpanStatus.OK

    def total_usage(self) -> TokenUsage:
        total = TokenUsage()
        for span in self.spans:
            total.prompt_tokens += span.usage.prompt_tokens
            total.completion_tokens += span.usage.completion_tokens
            total.cached_prompt_tokens += span.usage.cached_prompt_tokens
        return total

    def breakdown(self) -> dict[str, float]:
        """Milliseconds attributable to each span kind.

        Child durations are subtracted from their parent so the numbers sum to
        the request total instead of double-counting nested work.
        """
        self_time: dict[str, float] = {}
        for span in self.spans:
            if span.duration_ms is None:
                continue
            child_ms = sum(
                c.duration_ms or 0.0
                for c in self.spans
                if c.parent_id == span.span_id and c.duration_ms
            )
            exclusive = max(span.duration_ms - child_ms, 0.0)
            self_time[span.kind.value] = self_time.get(span.kind.value, 0.0) + exclusive
        return {k: round(v, 3) for k, v in sorted(self_time.items(), key=lambda kv: -kv[1])}

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "duration_ms": round(self.duration_ms, 3),
            "status": self.status.value,
            "span_count": len(self.spans),
            "usage": self.total_usage().to_dict(),
            "breakdown_ms": self.breakdown(),
            "spans": [s.to_dict() for s in self.spans],
        }


def hash_text(text: str, salt: str = "llmobs") -> str:
    """Stable pseudonym for a prompt or completion.

    Identical prompts hash identically, which is enough to spot a retry storm
    or a hot cache key without keeping the text itself.
    """
    return hashlib.sha256(f"{salt}:{text}".encode()).hexdigest()[:16]


class Tracer:
    """Creates spans and hands finished traces to registered exporters."""

    def __init__(
        self,
        service: str = "llm-gateway",
        sample_rate: float = 1.0,
        always_sample_errors: bool = True,
        capture_text: bool = False,
        hash_salt: str = "llmobs",
    ) -> None:
        self.service = service
        self.sample_rate = sample_rate
        self.always_sample_errors = always_sample_errors
        self.capture_text = capture_text
        self.hash_salt = hash_salt
        self._exporters: list[Any] = []
        self._active: dict[str, Trace] = {}
        self._lock = threading.Lock()

    def add_exporter(self, exporter: Any) -> None:
        """Register anything with ``export(trace: Trace) -> None``."""
        self._exporters.append(exporter)

    def _should_sample(self) -> bool:
        return self.sample_rate >= 1.0 or random.random() < self.sample_rate

    @contextmanager
    def trace(self, name: str, **attributes: Any) -> Iterator[Span]:
        """Start a root span. Exports on exit."""
        trace_id = uuid.uuid4().hex
        span = Span(
            name=name,
            kind=SpanKind.REQUEST,
            trace_id=trace_id,
            sampled=self._should_sample(),
        )
        span.set(service=self.service, **attributes)
        record = Trace(trace_id=trace_id, spans=[span])
        with self._lock:
            self._active[trace_id] = record

        previous = _current_span.get()
        token = _current_span.set(span)
        try:
            yield span
        except Exception as exc:
            span.finish(SpanStatus.ERROR, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            _restore(token, previous)
            if span.ended_at is None:
                span.finish()
            with self._lock:
                self._active.pop(trace_id, None)
            self._export(record)

    @contextmanager
    def span(self, name: str, kind: SpanKind = SpanKind.INFERENCE, **attributes: Any) -> Iterator[Span]:
        """Start a child of the current span, or a root if there is none."""
        parent = _current_span.get()
        if parent is None:
            with self.trace(name, **attributes) as root:
                root.kind = kind
                yield root
            return

        child = Span(
            name=name,
            kind=kind,
            trace_id=parent.trace_id,
            parent_id=parent.span_id,
            sampled=parent.sampled,
        )
        child.set(**attributes)
        with self._lock:
            record = self._active.get(parent.trace_id)
            if record is not None:
                record.spans.append(child)

        token = _current_span.set(child)
        try:
            yield child
        except Exception as exc:
            child.finish(SpanStatus.ERROR, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            _restore(token, parent)
            if child.ended_at is None:
                child.finish()

    def record_prompt(self, span: Span, prompt: str, completion: str | None = None) -> None:
        """Attach prompt/completion identity to a span, hashed unless opted in."""
        span.set(
            prompt_hash=hash_text(prompt, self.hash_salt),
            prompt_chars=len(prompt),
        )
        if completion is not None:
            span.set(
                completion_hash=hash_text(completion, self.hash_salt),
                completion_chars=len(completion),
            )
        if self.capture_text:
            span.set(prompt=prompt, completion=completion)

    def _export(self, record: Trace) -> None:
        root = record.root
        sampled = root.sampled if root else True
        if not sampled and not (
            self.always_sample_errors and record.status is not SpanStatus.OK
        ):
            return
        for exporter in self._exporters:
            try:
                exporter.export(record)
            except Exception:  # an exporter outage must not fail the request
                continue

    def current_span(self) -> Span | None:
        return _current_span.get()

    def current_trace(self) -> Trace | None:
        """The in-progress trace for the active span, if there is one.

        Available only while the root context manager is open; once it exits
        the trace has been exported and dropped from the active map.
        """
        span = _current_span.get()
        if span is None:
            return None
        with self._lock:
            return self._active.get(span.trace_id)
