# Multi-stage: the wheel is built once, the runtime image carries no toolchain.
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel

FROM python:3.12-slim
LABEL org.opencontainers.image.title="llm-inference-gateway" \
      org.opencontainers.image.description="Instrumented gateway for LLM inference serving" \
      org.opencontainers.image.licenses="MIT"

# Run as a non-root user: the gateway needs no privileges, and a container
# that cannot write outside its data volume is one less thing to review.
RUN useradd --create-home --uid 10001 llmobs
WORKDIR /app

COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl "llmobs[server]" && rm /tmp/*.whl

USER llmobs
ENV LLMOBS_BACKEND=local \
    LLMOBS_DB=/data/traces.db \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080

# Readiness is checked by the orchestrator; this covers plain `docker run`.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=2).status==200 else 1)"

CMD ["uvicorn", "llmobs.server:app", "--host", "0.0.0.0", "--port", "8080"]
