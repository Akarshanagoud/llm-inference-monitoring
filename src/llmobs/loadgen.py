"""Load generator.

Reference platforms that only serve a single hand-typed request never exercise
the interesting behaviour: queueing, prefix-cache hits, retry storms, burn-rate
alerts. This generates concurrent traffic with a realistic mix — repeated
system prefixes, varied prompt lengths, an optional failure rate — so the
dashboards have something to show and the SLO logic gets tested against data
rather than assertions.
"""
from __future__ import annotations

import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from .backends.base import BackendError
from .gateway import InferenceGateway, OverloadedError

SYSTEM_PREFIXES = [
    "You are a helpful support assistant for an enterprise SaaS product. "
    "Answer concisely and cite the relevant policy section.\n\n",
    "You are a clinical documentation assistant. Extract structured fields "
    "and never invent values that are not present in the source.\n\n",
    "You are a code review assistant. Point out correctness issues first.\n\n",
]

TASKS = [
    "Summarise the attached quarterly report in three bullet points.",
    "What is the refund window for annual subscriptions?",
    "Extract the patient's medication list as JSON.",
    "Explain why this query is doing a full table scan.",
    "Draft a release note for the caching change.",
    "Classify this ticket as billing, technical or account.",
    "List the top three risks in this architecture document.",
    "Rewrite this paragraph for a non-technical audience.",
]


@dataclass
class LoadResult:
    requests: int
    successes: int
    failures: int
    shed: int
    duration_seconds: float
    latencies_ms: list[float] = field(default_factory=list)
    ttfts_ms: list[float] = field(default_factory=list)
    completion_tokens: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    invalid_responses: int = 0

    @property
    def throughput(self) -> float:
        return self.requests / self.duration_seconds if self.duration_seconds else 0.0

    @property
    def error_rate(self) -> float:
        return self.failures / self.requests if self.requests else 0.0

    def percentile(self, values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(int(q * len(ordered)), len(ordered) - 1)
        return ordered[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "shed": self.shed,
            "invalid_responses": self.invalid_responses,
            "duration_seconds": round(self.duration_seconds, 3),
            "throughput_rps": round(self.throughput, 2),
            "error_rate": round(self.error_rate, 4),
            "latency_ms": {
                "p50": round(self.percentile(self.latencies_ms, 0.50), 2),
                "p95": round(self.percentile(self.latencies_ms, 0.95), 2),
                "p99": round(self.percentile(self.latencies_ms, 0.99), 2),
                "mean": round(statistics.fmean(self.latencies_ms), 2)
                if self.latencies_ms
                else 0.0,
            },
            "ttft_ms": {
                "p50": round(self.percentile(self.ttfts_ms, 0.50), 2),
                "p95": round(self.percentile(self.ttfts_ms, 0.95), 2),
            },
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "cached": self.cached_tokens,
                "cache_hit_rate": round(self.cached_tokens / self.prompt_tokens, 4)
                if self.prompt_tokens
                else 0.0,
            },
            "output_tokens_per_second": round(
                self.completion_tokens / self.duration_seconds, 1
            )
            if self.duration_seconds
            else 0.0,
        }

    def render(self) -> str:
        data = self.to_dict()
        return "\n".join(
            [
                f"requests        : {data['requests']} in {data['duration_seconds']}s "
                f"({data['throughput_rps']} rps)",
                f"successes       : {data['successes']}",
                f"failures        : {data['failures']} (error rate {data['error_rate']:.2%})",
                f"shed            : {data['shed']}",
                f"invalid replies : {data['invalid_responses']}",
                f"latency ms      : p50 {data['latency_ms']['p50']}  "
                f"p95 {data['latency_ms']['p95']}  p99 {data['latency_ms']['p99']}",
                f"ttft ms         : p50 {data['ttft_ms']['p50']}  p95 {data['ttft_ms']['p95']}",
                f"output tok/s    : {data['output_tokens_per_second']}",
                f"prefix cache    : {data['tokens']['cache_hit_rate']:.1%} of prompt tokens",
            ]
        )


def generate_load(
    gateway: InferenceGateway,
    requests: int = 200,
    concurrency: int = 8,
    max_tokens: int = 96,
    prefix_reuse: float = 0.8,
    seed: int = 7,
) -> LoadResult:
    """Drive ``requests`` prompts through the gateway at ``concurrency``.

    ``prefix_reuse`` controls how often a request reuses one of the shared
    system prefixes, which is what makes the prefix-cache metrics meaningful.
    """
    rng = random.Random(seed)
    prompts = []
    for _ in range(requests):
        prefix = (
            rng.choice(SYSTEM_PREFIXES)
            if rng.random() < prefix_reuse
            else f"Session {rng.randint(1, 10**9)}. "
        )
        prompts.append(prefix + rng.choice(TASKS))

    result = LoadResult(
        requests=requests, successes=0, failures=0, shed=0, duration_seconds=0.0
    )
    lock = threading.Lock()
    started = time.perf_counter()

    def run(prompt: str) -> None:
        try:
            outcome = gateway.generate(prompt, max_tokens=max_tokens)
        except OverloadedError:
            with lock:
                result.shed += 1
                result.failures += 1
            return
        except BackendError:
            with lock:
                result.failures += 1
            return
        with lock:
            result.successes += 1
            result.latencies_ms.append(outcome.duration_ms)
            if outcome.ttft_ms is not None:
                result.ttfts_ms.append(outcome.ttft_ms)
            result.prompt_tokens += outcome.prompt_tokens
            result.completion_tokens += outcome.completion_tokens
            result.cached_tokens += outcome.cached_prompt_tokens
            if not outcome.valid:
                result.invalid_responses += 1

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(run, prompt) for prompt in prompts]
        for future in as_completed(futures):
            future.result()

    result.duration_seconds = time.perf_counter() - started
    return result
