"""FastAPI gateway.

This is the deployment shape: an instrumented proxy in front of your model
server. Applications call this instead of vLLM/Triton directly and get tracing,
metrics, validation and SLO accounting without changing their own code.

    pip install "llmobs[server]"
    uvicorn llmobs.server:app --port 8080

Environment:
    LLMOBS_BACKEND    local | vllm | triton | openai-compat   (default: local)
    LLMOBS_BASE_URL   backend URL for the HTTP adapters
    LLMOBS_MODEL      model name to request
    LLMOBS_DB         SQLite trace store path
    LLMOBS_MAX_IN_FLIGHT  shed load above this concurrency (0 disables)
"""
from __future__ import annotations

import os
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Response
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "llmobs.server requires the server extra: pip install 'llmobs[server]'"
    ) from exc

from . import __version__
from .backends import BackendError, build_backend
from .gateway import GatewayConfig, InferenceGateway, OverloadedError


def build_gateway() -> InferenceGateway:
    kind = os.getenv("LLMOBS_BACKEND", "local")
    if kind in {"local", "sim"}:
        backend = build_backend(
            kind,
            speed=float(os.getenv("LLMOBS_SIM_SPEED", "20")),
            failure_rate=float(os.getenv("LLMOBS_SIM_FAILURE_RATE", "0")),
        )
    else:
        backend = build_backend(
            kind,
            base_url=os.getenv("LLMOBS_BASE_URL", "http://localhost:8000"),
            model=os.getenv("LLMOBS_MODEL", "default"),
        )
    config = GatewayConfig(
        service=os.getenv("LLMOBS_SERVICE", "llm-gateway"),
        trace_store_path=os.getenv("LLMOBS_DB", "traces.db"),
        max_in_flight=int(os.getenv("LLMOBS_MAX_IN_FLIGHT", "0")),
        capture_text=os.getenv("LLMOBS_CAPTURE_TEXT", "").lower() in {"1", "true", "yes"},
    )
    return InferenceGateway(backend, config)


gateway = build_gateway()

app = FastAPI(
    title="LLM Inference Gateway",
    version=__version__,
    description="Instrumented proxy: tracing, metrics, response validation and SLOs.",
)


class GenerateRequest(BaseModel):
    prompt: str = Field(..., description="The prompt to send to the model")
    model: str | None = None
    max_tokens: int = 256
    temperature: float = 0.0
    stream: bool = False
    expect_json: bool = Field(False, description="Validate the response parses as JSON")
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness. Always 200 while the process is serving."""
    return {"status": "ok", "version": __version__, "backend": gateway.backend.name}


@app.get("/ready")
def ready() -> dict[str, Any]:
    """Readiness. Fails when the backend is down, so Kubernetes stops routing.

    Deliberately separate from /health: a gateway whose backend is unreachable
    should be pulled from the load balancer without being restarted, since
    restarting it fixes nothing.
    """
    status = gateway.refresh_backend_health()
    if not status["up"]:
        raise HTTPException(status_code=503, detail=status)
    return {"status": "ready", **status}


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(content=gateway.prometheus(), media_type="text/plain; version=0.0.4")


@app.get("/stats")
def stats() -> dict[str, Any]:
    return gateway.snapshot()


@app.post("/v1/generate")
def generate(request: GenerateRequest) -> dict[str, Any]:
    validation_context = {
        "expect_json": request.expect_json,
        "must_contain": request.must_contain,
        "must_not_contain": request.must_not_contain,
    }
    try:
        result = gateway.generate(
            request.prompt,
            model=request.model,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            validation_context=validation_context,
        )
    except OverloadedError as exc:
        # 503 with Retry-After is the honest answer to "we are shedding load".
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "1"}) from exc
    except BackendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result.to_dict()


@app.post("/v1/generate/stream")
def generate_stream(request: GenerateRequest) -> StreamingResponse:
    def chunks():
        try:
            yield from gateway.stream(
                request.prompt, model=request.model, max_tokens=request.max_tokens
            )
        except BackendError as exc:
            # The response body has already started, so a backend failure is
            # surfaced in the stream rather than as a status code.
            yield f"\n[backend error: {exc}]"

    return StreamingResponse(chunks(), media_type="text/plain")


@app.get("/v1/traces")
def list_traces(
    limit: int = 20, status: str | None = None, slow: bool = False, invalid: bool = False
) -> list[dict[str, Any]]:
    if gateway.store is None:
        raise HTTPException(status_code=409, detail="no trace store configured")
    if slow:
        return gateway.store.slowest(limit)
    return gateway.store.recent(limit=limit, status=status, invalid_only=invalid)


@app.get("/v1/traces/{trace_id}")
def get_trace(trace_id: str) -> dict[str, Any]:
    if gateway.store is None:
        raise HTTPException(status_code=409, detail="no trace store configured")
    payload = gateway.store.get(trace_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return payload


@app.get("/v1/stages")
def stage_latency() -> list[dict[str, Any]]:
    """Latency attributed to each pipeline stage across all stored traces."""
    if gateway.store is None:
        raise HTTPException(status_code=409, detail="no trace store configured")
    return gateway.store.stage_latency()


@app.get("/v1/slo")
def slo_status() -> dict[str, Any]:
    statuses = [s.to_dict() for s in gateway.slo.all_status()]
    return {"worst_severity": gateway.slo.worst_severity().value, "slos": statuses}
