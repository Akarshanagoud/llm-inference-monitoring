"""Backend adapter interface.

Every serving stack reports the same facts under different names: vLLM calls it
``num_requests_waiting``, Triton calls it ``nv_inference_pending_request_count``,
TensorRT-LLM exposes it through the batch manager. Normalising them here means
the dashboards, SLO rules and alerts are written once, and swapping backends is
a config change rather than a rewrite of every panel.
"""
from __future__ import annotations

import abc
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GenerationRequest:
    prompt: str
    model: str | None = None
    max_tokens: int = 256
    temperature: float = 0.0
    top_p: float = 1.0
    stop: list[str] = field(default_factory=list)
    stream: bool = False
    request_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class GenerationResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BackendHealth:
    """Normalised serving-layer telemetry, shared across backends."""

    up: bool
    model: str | None = None
    queue_depth: float = 0.0
    running_batch_size: float = 0.0
    kv_cache_utilization: float = 0.0
    gpu_cache_usage_perc: float | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "up": self.up,
            "model": self.model,
            "queue_depth": self.queue_depth,
            "running_batch_size": self.running_batch_size,
            "kv_cache_utilization": round(self.kv_cache_utilization, 4),
            "detail": self.detail,
        }


class Backend(abc.ABC):
    """A model-serving stack the gateway can talk to."""

    name: str = "backend"

    @abc.abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Run a non-streaming completion."""

    def stream(self, request: GenerationRequest) -> Iterator[str]:
        """Yield text deltas. Defaults to a single chunk from :meth:`generate`."""
        yield self.generate(request).text

    @abc.abstractmethod
    def health(self) -> BackendHealth:
        """Probe the serving layer and return normalised telemetry."""

    def close(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release any connections. Safe to call more than once.

        Deliberately concrete and empty: most backends hold no resources, and
        forcing every adapter to implement a no-op is noise.
        """


class BackendError(RuntimeError):
    """Raised when a backend fails in a way the gateway should surface."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
