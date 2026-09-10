"""llmobs - observability and reliability tooling for LLM inference serving."""
from .backends import (
    Backend,
    BackendError,
    BackendHealth,
    GenerationRequest,
    GenerationResponse,
    LocalSimBackend,
    build_backend,
)
from .gateway import GatewayConfig, GatewayResult, InferenceGateway, OverloadedError
from .metrics import Counter, Gauge, Histogram, MetricsRegistry
from .slo import SLO, Severity, SLOStatus, SLOTracker, default_slos
from .store import TraceStore
from .trace import Span, SpanKind, SpanStatus, TokenUsage, Trace, Tracer
from .validation import ResponseValidator, ValidationIssue, ValidationResult

__version__ = "0.1.0"

__all__ = [
    "SLO",
    "Backend",
    "BackendError",
    "BackendHealth",
    "Counter",
    "Gauge",
    "GatewayConfig",
    "GatewayResult",
    "GenerationRequest",
    "GenerationResponse",
    "Histogram",
    "InferenceGateway",
    "LocalSimBackend",
    "MetricsRegistry",
    "OverloadedError",
    "ResponseValidator",
    "SLOStatus",
    "SLOTracker",
    "Severity",
    "Span",
    "SpanKind",
    "SpanStatus",
    "TokenUsage",
    "Trace",
    "TraceStore",
    "Tracer",
    "ValidationIssue",
    "ValidationResult",
    "build_backend",
    "default_slos",
    "__version__",
]
