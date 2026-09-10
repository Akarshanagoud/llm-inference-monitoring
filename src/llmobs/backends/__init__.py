"""Backend adapters: local simulator, vLLM, Triton, OpenAI-compatible servers."""
from .base import (
    Backend,
    BackendError,
    BackendHealth,
    GenerationRequest,
    GenerationResponse,
)
from .http_backends import (
    OpenAICompatBackend,
    TritonBackend,
    VLLMBackend,
    build_backend,
    parse_prometheus,
)
from .local import LocalSimBackend, estimate_tokens

__all__ = [
    "Backend",
    "BackendError",
    "BackendHealth",
    "GenerationRequest",
    "GenerationResponse",
    "LocalSimBackend",
    "OpenAICompatBackend",
    "TritonBackend",
    "VLLMBackend",
    "build_backend",
    "estimate_tokens",
    "parse_prometheus",
]
