"""Metrics registry with Prometheus text exposition.

Written against the exposition format directly rather than pulling in
``prometheus_client``, for two reasons: the dependency is one more thing to
pin in an inference image that is already several gigabytes, and the LLM-shaped
metrics here (TTFT, inter-token latency, queue depth, KV-cache utilisation)
need bucket boundaries that generic defaults get badly wrong.

Bucket choice matters more than it looks. Default Prometheus buckets top out at
10 seconds, which for LLM inference means every slow request lands in ``+Inf``
and your p99 becomes a straight line. The buckets here span 5 ms to 10 minutes.
"""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

Labels = tuple[tuple[str, str], ...]

# Latency buckets in seconds, spanning fast cache hits to long generations.
LATENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0,
    10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0,
)
# Time to first token: users notice anything past ~500 ms, so resolution is
# concentrated below one second.
TTFT_BUCKETS = (
    0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 5.0, 10.0,
)
TOKEN_BUCKETS = (16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)


def _labels(labels: dict[str, str] | None) -> Labels:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(labels: Labels, extra: tuple[tuple[str, str], ...] = ()) -> str:
    combined = list(labels) + list(extra)
    if not combined:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in combined)
    return "{" + inner + "}"


@dataclass
class Counter:
    name: str
    help: str
    values: dict[Labels, float] = field(default_factory=lambda: defaultdict(float))

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        self.values[_labels(labels)] += amount

    def get(self, **labels: str) -> float:
        """Exact lookup: the label set must match completely."""
        return self.values.get(_labels(labels), 0.0)

    def total(self, **filters: str) -> float:
        """Sum across every label set that *contains* ``filters``.

        Aggregation needs this: ``tokens{direction="completion"}`` is recorded
        per model and per backend, so an exact lookup on ``direction`` alone
        finds nothing.
        """
        wanted = set(_labels(filters))
        return sum(
            value for labels, value in self.values.items() if wanted <= set(labels)
        )

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for labels, value in sorted(self.values.items()):
            lines.append(f"{self.name}{_render_labels(labels)} {_fmt(value)}")
        return lines


@dataclass
class Gauge:
    name: str
    help: str
    values: dict[Labels, float] = field(default_factory=lambda: defaultdict(float))

    def set(self, value: float, **labels: str) -> None:
        self.values[_labels(labels)] = value

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        self.values[_labels(labels)] += amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.values[_labels(labels)] -= amount

    def get(self, **labels: str) -> float:
        return self.values.get(_labels(labels), 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        for labels, value in sorted(self.values.items()):
            lines.append(f"{self.name}{_render_labels(labels)} {_fmt(value)}")
        return lines


@dataclass
class Histogram:
    name: str
    help: str
    buckets: tuple[float, ...] = LATENCY_BUCKETS
    counts: dict[Labels, list[int]] = field(default_factory=dict)
    sums: dict[Labels, float] = field(default_factory=lambda: defaultdict(float))
    totals: dict[Labels, int] = field(default_factory=lambda: defaultdict(int))
    #: Raw observations, capped, so quantiles can be computed exactly for
    #: dashboards without relying on bucket interpolation.
    samples: dict[Labels, list[float]] = field(default_factory=dict)
    max_samples: int = 4096

    def observe(self, value: float, **labels: str) -> None:
        key = _labels(labels)
        if key not in self.counts:
            self.counts[key] = [0] * len(self.buckets)
            self.samples[key] = []
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[key][i] += 1
        self.sums[key] += value
        self.totals[key] += 1
        pool = self.samples[key]
        if len(pool) < self.max_samples:
            pool.append(value)

    def _pool(self, filters: dict[str, str]) -> list[float]:
        """Samples across every label set containing ``filters``.

        An empty filter therefore aggregates the whole metric, which is what
        a service-level summary wants.
        """
        wanted = set(_labels(filters))
        pool: list[float] = []
        for labels, samples in self.samples.items():
            if wanted <= set(labels):
                pool.extend(samples)
        return pool

    def quantile(self, q: float, **labels: str) -> float | None:
        """Exact quantile over retained samples (not bucket-interpolated)."""
        pool = sorted(self._pool(labels))
        if not pool:
            return None
        index = min(int(math.ceil(q * len(pool))) - 1, len(pool) - 1)
        return pool[max(index, 0)]

    def count(self, **labels: str) -> int:
        wanted = set(_labels(labels))
        return sum(
            total for key, total in self.totals.items() if wanted <= set(key)
        )

    def mean(self, **labels: str) -> float | None:
        wanted = set(_labels(labels))
        total = 0
        summed = 0.0
        for key, count in self.totals.items():
            if wanted <= set(key):
                total += count
                summed += self.sums[key]
        return summed / total if total else None

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for key in sorted(self.counts):
            cumulative = self.counts[key]
            for bound, count in zip(self.buckets, cumulative, strict=True):
                le = "+Inf" if math.isinf(bound) else _fmt(bound)
                lines.append(f"{self.name}_bucket{_render_labels(key, (('le', le),))} {count}")
            lines.append(
                f"{self.name}_bucket{_render_labels(key, (('le', '+Inf'),))} {self.totals[key]}"
            )
            lines.append(f"{self.name}_sum{_render_labels(key)} {_fmt(self.sums[key])}")
            lines.append(f"{self.name}_count{_render_labels(key)} {self.totals[key]}")
        return lines


def _fmt(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(round(value, 6))


class MetricsRegistry:
    """Holds the standard LLM-serving metric set and renders it for Prometheus."""

    def __init__(self, namespace: str = "llm") -> None:
        self.namespace = namespace
        self._lock = threading.Lock()
        self.started_at = time.time()

        n = namespace
        self.requests = Counter(f"{n}_requests_total", "Inference requests received")
        self.errors = Counter(f"{n}_request_errors_total", "Inference requests that failed")
        self.retries = Counter(f"{n}_retries_total", "Backend calls retried")
        self.tokens = Counter(f"{n}_tokens_total", "Tokens processed, by direction")
        self.cache_hits = Counter(f"{n}_prefix_cache_hits_total", "Prompt prefix cache hits")

        self.latency = Histogram(
            f"{n}_request_duration_seconds", "End-to-end request latency", LATENCY_BUCKETS
        )
        self.ttft = Histogram(
            f"{n}_time_to_first_token_seconds", "Time to first token", TTFT_BUCKETS
        )
        self.inter_token = Histogram(
            f"{n}_inter_token_latency_seconds",
            "Mean gap between generated tokens",
            (0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0),
        )
        self.prompt_tokens = Histogram(
            f"{n}_prompt_tokens", "Prompt length in tokens", TOKEN_BUCKETS
        )
        self.completion_tokens = Histogram(
            f"{n}_completion_tokens", "Completion length in tokens", TOKEN_BUCKETS
        )

        self.in_flight = Gauge(f"{n}_requests_in_flight", "Requests currently being served")
        self.queue_depth = Gauge(f"{n}_queue_depth", "Requests waiting for a batch slot")
        self.kv_cache_utilization = Gauge(
            f"{n}_kv_cache_utilization_ratio", "Fraction of KV cache blocks in use"
        )
        self.batch_size = Gauge(f"{n}_running_batch_size", "Sequences in the running batch")
        self.model_up = Gauge(f"{n}_backend_up", "1 when the backend responded to its last probe")

        self._collections: list[Counter | Gauge | Histogram] = [
            self.requests, self.errors, self.retries, self.tokens, self.cache_hits,
            self.latency, self.ttft, self.inter_token, self.prompt_tokens,
            self.completion_tokens, self.in_flight, self.queue_depth,
            self.kv_cache_utilization, self.batch_size, self.model_up,
        ]

    def observe_span(self, span: Any) -> None:
        """Record one finished inference span into every relevant metric."""
        labels = {
            "model": span.model or "unknown",
            "backend": span.backend or "unknown",
        }
        with self._lock:
            self.requests.inc(**labels, status=span.status.value)
            if span.status.value != "ok":
                self.errors.inc(**labels, status=span.status.value)
            if span.duration_ms is not None:
                self.latency.observe(span.duration_ms / 1000, **labels)
            if span.ttft_ms is not None:
                self.ttft.observe(span.ttft_ms / 1000, **labels)
                completion = span.usage.completion_tokens
                if completion > 1 and span.duration_ms:
                    decode_ms = max(span.duration_ms - span.ttft_ms, 0.0)
                    self.inter_token.observe(decode_ms / 1000 / (completion - 1), **labels)
            if span.usage.prompt_tokens:
                self.prompt_tokens.observe(span.usage.prompt_tokens, **labels)
                self.tokens.inc(span.usage.prompt_tokens, **labels, direction="prompt")
            if span.usage.completion_tokens:
                self.completion_tokens.observe(span.usage.completion_tokens, **labels)
                self.tokens.inc(span.usage.completion_tokens, **labels, direction="completion")
            if span.usage.cached_prompt_tokens:
                self.cache_hits.inc(span.usage.cached_prompt_tokens, **labels)

    def render(self) -> str:
        """Prometheus text exposition format (version 0.0.4)."""
        with self._lock:
            lines: list[str] = []
            for collection in self._collections:
                rendered = collection.render()
                if len(rendered) > 2:  # skip metrics with no observations
                    lines.extend(rendered)
            lines.append(f"# HELP {self.namespace}_uptime_seconds Process uptime")
            lines.append(f"# TYPE {self.namespace}_uptime_seconds gauge")
            lines.append(f"{self.namespace}_uptime_seconds {_fmt(time.time() - self.started_at)}")
        return "\n".join(lines) + "\n"

    def snapshot(self, model: str = "", backend: str = "") -> dict[str, Any]:
        """Human-readable summary for the /stats endpoint and the CLI."""
        labels = {}
        if model:
            labels["model"] = model
        if backend:
            labels["backend"] = backend

        def q(hist: Histogram, quantile: float) -> float | None:
            value = hist.quantile(quantile, **labels)
            return round(value, 4) if value is not None else None

        total = sum(self.requests.values.values())
        failed = sum(self.errors.values.values())
        return {
            "requests_total": total,
            "errors_total": failed,
            "error_rate": round(failed / total, 4) if total else 0.0,
            "in_flight": self.in_flight.get(),
            "latency_seconds": {
                "p50": q(self.latency, 0.5),
                "p95": q(self.latency, 0.95),
                "p99": q(self.latency, 0.99),
                "mean": round(self.latency.mean(**labels) or 0.0, 4),
            },
            "ttft_seconds": {
                "p50": q(self.ttft, 0.5),
                "p95": q(self.ttft, 0.95),
                "p99": q(self.ttft, 0.99),
            },
            "tokens": {
                "prompt": self.tokens.total(**labels, direction="prompt"),
                "completion": self.tokens.total(**labels, direction="completion"),
                "cached": self.cache_hits.total(**labels),
            },
            "kv_cache_utilization": self.kv_cache_utilization.get(),
            "queue_depth": self.queue_depth.get(),
        }
