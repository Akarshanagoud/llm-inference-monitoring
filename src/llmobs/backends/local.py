"""Deterministic in-process backend.

This exists so the whole platform — gateway, metrics, SLO evaluation, load
generator, dashboards — runs end to end on a laptop with no GPU, no model
weights and no network. Everything in the observability path is exercised for
real; only the matrix multiplications are fake.

The simulator is not a stub that returns instantly. It models the two phases
that actually shape LLM latency:

* **prefill**, roughly linear in prompt length, which sets time to first token;
* **decode**, roughly linear in output length, which sets total duration.

It also simulates a prefix cache, queueing under concurrency, and a
configurable failure rate — so backpressure and error-budget code paths get
tested instead of being written blind.
"""
from __future__ import annotations

import hashlib
import random
import threading
import time
from collections.abc import Iterator

from .base import Backend, BackendError, BackendHealth, GenerationRequest, GenerationResponse

_LOREM = [
    "the", "model", "returns", "a", "deterministic", "completion", "so", "that",
    "traces", "metrics", "and", "validation", "logic", "can", "be", "exercised",
    "without", "a", "gpu", "attached", "to", "the", "machine", "which", "keeps",
    "the", "whole", "reference", "platform", "runnable", "in", "continuous",
    "integration", "and", "on", "a", "laptop", "during", "development", "or",
    "an", "interview", "walkthrough",
]


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 characters per token for English prose.

    Good enough for budgeting and metric buckets; swap in the real tokenizer
    when you attach a real backend.
    """
    return max(1, len(text) // 4)


class LocalSimBackend(Backend):
    """Simulated serving stack with realistic latency structure."""

    name = "local-sim"

    def __init__(
        self,
        model: str = "sim-7b-instruct",
        prefill_ms_per_1k_tokens: float = 45.0,
        decode_ms_per_token: float = 12.0,
        base_overhead_ms: float = 8.0,
        jitter: float = 0.15,
        failure_rate: float = 0.0,
        max_concurrency: int = 8,
        speed: float = 1.0,
        seed: int | None = 1337,
    ) -> None:
        self.model = model
        self.prefill_ms_per_1k = prefill_ms_per_1k_tokens
        self.decode_ms_per_token = decode_ms_per_token
        self.base_overhead_ms = base_overhead_ms
        self.jitter = jitter
        self.failure_rate = failure_rate
        self.max_concurrency = max_concurrency
        #: Wall-clock divisor. speed=100 runs a 3-second generation in 30 ms,
        #: which keeps the test suite fast without changing the shape of the
        #: reported numbers.
        self.speed = speed
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._in_flight = 0
        self._queue_depth = 0
        self._prefix_cache: dict[str, int] = {}
        self._cache_capacity = 128
        self._closed = False

    # -- latency model ------------------------------------------------------
    def _jittered(self, value: float) -> float:
        if self.jitter <= 0:
            return value
        return value * self._rng.uniform(1 - self.jitter, 1 + self.jitter)

    def _prefix_key(self, prompt: str) -> str:
        # Cache on the first 256 characters: system prompts and few-shot
        # preambles are the part that actually repeats across requests.
        return hashlib.sha256(prompt[:256].encode()).hexdigest()[:16]

    def _cached_tokens(self, prompt: str) -> int:
        key = self._prefix_key(prompt)
        with self._lock:
            hit = self._prefix_cache.get(key, 0)
            if len(self._prefix_cache) >= self._cache_capacity:
                self._prefix_cache.pop(next(iter(self._prefix_cache)))
            self._prefix_cache[key] = estimate_tokens(prompt[:256])
        return hit

    def _sleep(self, milliseconds: float) -> None:
        if milliseconds > 0:
            time.sleep(milliseconds / 1000 / max(self.speed, 1e-9))

    def _completion_for(self, request: GenerationRequest) -> str:
        """Deterministic text keyed on the prompt, so retries are comparable."""
        seed = int(hashlib.sha256(request.prompt.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        word_count = max(4, min(request.max_tokens, 200))
        words = [rng.choice(_LOREM) for _ in range(word_count)]
        return " ".join(words)

    # -- Backend interface --------------------------------------------------
    def generate(self, request: GenerationRequest) -> GenerationResponse:
        if self._closed:
            raise BackendError("backend is closed", retryable=False)

        with self._lock:
            if self._in_flight >= self.max_concurrency:
                self._queue_depth += 1
                queued = True
            else:
                queued = False
            self._in_flight += 1

        try:
            if queued:
                # Waiting for a batch slot: the dominant latency term once a
                # server is saturated, and the one teams forget to measure.
                self._sleep(self._jittered(self.decode_ms_per_token * 8))
                with self._lock:
                    self._queue_depth = max(0, self._queue_depth - 1)

            if self.failure_rate and self._rng.random() < self.failure_rate:
                self._sleep(self.base_overhead_ms)
                raise BackendError("simulated backend failure", retryable=True)

            prompt_tokens = estimate_tokens(request.prompt)
            cached = min(self._cached_tokens(request.prompt), prompt_tokens)
            billable_prefill = max(prompt_tokens - cached, 1)

            prefill_ms = self._jittered(
                self.base_overhead_ms + billable_prefill * self.prefill_ms_per_1k / 1000
            )
            self._sleep(prefill_ms)

            text = self._completion_for(request)
            completion_tokens = estimate_tokens(text)
            self._sleep(self._jittered(completion_tokens * self.decode_ms_per_token))

            return GenerationResponse(
                text=text,
                model=request.model or self.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_prompt_tokens=cached,
                finish_reason="stop",
                raw={"simulated": True, "queued": queued},
            )
        finally:
            with self._lock:
                self._in_flight = max(0, self._in_flight - 1)

    def stream(self, request: GenerationRequest) -> Iterator[str]:
        if self._closed:
            raise BackendError("backend is closed", retryable=False)

        prompt_tokens = estimate_tokens(request.prompt)
        cached = min(self._cached_tokens(request.prompt), prompt_tokens)
        self._sleep(
            self._jittered(
                self.base_overhead_ms + max(prompt_tokens - cached, 1) * self.prefill_ms_per_1k / 1000
            )
        )
        for word in self._completion_for(request).split():
            self._sleep(self._jittered(self.decode_ms_per_token))
            yield word + " "

    def health(self) -> BackendHealth:
        with self._lock:
            utilization = min(self._in_flight / max(self.max_concurrency, 1), 1.0)
            return BackendHealth(
                up=not self._closed,
                model=self.model,
                queue_depth=float(self._queue_depth),
                running_batch_size=float(self._in_flight),
                kv_cache_utilization=utilization,
                detail="simulated backend",
            )

    def close(self) -> None:
        self._closed = True
