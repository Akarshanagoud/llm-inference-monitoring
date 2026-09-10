# LLM Inference Monitoring

An LLMOps reference platform: prompt tracing, response validation, latency
monitoring and model-serving observability for vLLM, Triton and TensorRT-LLM.

[![CI](https://github.com/akarshanamachanpally/llm-inference-monitoring/actions/workflows/ci.yml/badge.svg)](https://github.com/akarshanamachanpally/llm-inference-monitoring/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Dependencies](https://img.shields.io/badge/core%20dependencies-none-brightgreen)

**The whole stack runs on a laptop with no GPU.** A built-in simulator models
prefill/decode latency, prefix caching, queueing and failures, so the tracing,
metrics, SLO and dashboard code is exercised for real — only the matrix
multiplications are fake. Point it at a real vLLM or Triton server by changing
one environment variable.

```bash
pip install -e ".[dev]"
llmobs load -n 500 -c 16          # drive traffic, print latency + SLO report
llmobs stages                     # where request time actually goes
docker compose up                 # gateway + Prometheus + Grafana
```

---

## The problem it solves

When p99 latency doubles, "the model got slower" is almost never the answer.
The retrieval step went from 20 ms to 900 ms, or you are silently retrying
every third request, or the KV cache is full and vLLM is preempting. A metric
that only records total duration cannot tell you which, and three separately
bolted-on tools will disagree during the incident.

Here, a single request produces — from one code path — a span-level trace,
Prometheus metrics, a validation verdict, an SLO event and a stored trace, all
correlated by trace id:

```
$ llmobs stages
STAGE            COUNT   MEAN ms    MAX ms   SHARE
request            500      9.65    110.62  55.0%
inference          500      7.57     10.09  43.2%
validation         500      0.18      0.98   1.0%
retry                7      8.22      8.67   0.8%
```

That `retry` row is the point. Retries hide errors inside your availability
metric while tripling backend load, and they are invisible unless something
counts them as their own stage.

---

## What it does

| Area | Detail |
|---|---|
| **Tracing** | Span tree per request (retrieval, template, inference, validation, guardrail, tool, retry) with exclusive-time attribution, TTFT, decode throughput and token accounting |
| **Metrics** | Prometheus exposition written directly — counters, gauges and histograms with buckets sized for LLM latency (5 ms to 10 min, not the 10 s default) |
| **Validation** | Empty output, truncation, degenerate repetition, refusal rate, JSON parse, length bounds, required/forbidden content — exported as metrics so quality regressions appear next to latency ones |
| **SLOs** | Multi-window burn-rate evaluation with error budgets, availability and latency objectives, and a minimum-sample guard so low-traffic services don't page on noise |
| **Backends** | vLLM, Triton (KServe v2), any OpenAI-compatible server, plus the offline simulator. Their native metric names are normalised so dashboards are written once |
| **Storage** | SQLite trace store with the queries an on-call engineer actually runs: slowest, failed, invalid, stage breakdown |
| **Deployment** | Multi-stage Dockerfile (non-root, read-only rootfs), Kubernetes Deployment/Service/PDB/HPA, ServiceMonitor, Prometheus alert rules, provisioned Grafana dashboard |

---

## Quickstart

### Drive load and read the report

```bash
llmobs load -n 500 -c 16 --failure-rate 0.02
```

```
requests        : 500 in 4.21s (118.7 rps)
successes       : 496
failures        : 4 (error rate 0.80%)
invalid replies : 3
latency ms      : p50 118.4  p95 214.7  p99 302.1
ttft ms         : p50 41.2   p95 78.9
output tok/s    : 8214.0
prefix cache    : 69.2% of prompt tokens

SLO status
  availability     objective 99.500%  observed 99.200%  budget left 0.0%  [page]
  latency_p95_5s   objective 95.000%  observed 100.000% budget left 100.0% [ok]
  ttft_p95_1s      objective 95.000%  observed 100.000% budget left 100.0% [ok]
```

### Inspect one request

```bash
llmobs traces --slow --limit 5
llmobs trace 9f2a1c4e...
```

```
trace 9f2a1c4e...  312.44 ms  [ok]
tokens: 128 prompt / 96 completion

breakdown by stage:
  inference       268.10 ms  ##################################
  request          41.22 ms  #####
  validation        3.12 ms  #

span tree:
  inference_request              312.44 ms  ttft=41.2ms
    backend.generate#1            88.01 ms  <error>
    backend.generate#2           180.09 ms  ttft=41.2ms
    validate                       3.12 ms
```

### Library

```python
from llmobs import InferenceGateway, GatewayConfig, build_backend

gateway = InferenceGateway(
    build_backend("vllm", base_url="http://vllm:8000", model="llama-3.1-8b"),
    GatewayConfig(trace_store_path="traces.db", max_in_flight=128),
)

result = gateway.generate("Summarise this report.", max_tokens=256)
result.trace_id       # correlate with metrics, logs and the trace store
result.ttft_ms        # what the user actually feels
result.breakdown_ms   # {"inference": 268.1, "request": 41.2, "validation": 3.1}
result.valid          # response validation verdict
```

### HTTP gateway

```bash
pip install -e ".[server]"
uvicorn llmobs.server:app --port 8080
```

| Endpoint | Purpose |
|---|---|
| `POST /v1/generate` | Instrumented completion |
| `POST /v1/generate/stream` | Streaming, with TTFT recorded |
| `GET /metrics` | Prometheus scrape |
| `GET /v1/traces`, `/v1/traces/{id}` | Trace listing and detail |
| `GET /v1/stages` | Latency by pipeline stage |
| `GET /v1/slo` | Objectives, burn rates, budget |
| `GET /health`, `/ready` | Liveness and readiness (see below) |

---

## Decisions worth knowing

**Liveness and readiness are not the same probe.** `/health` reports only
whether the process is serving. `/ready` also checks the backend. If liveness
checked the backend, a model outage would restart every gateway pod — which
fixes nothing and destroys the traces that explain the outage. Readiness
failure removes the pod from the Service without killing it.

**Buckets, not defaults.** Prometheus' default histogram buckets stop at 10
seconds. For LLM inference that puts every slow generation into `+Inf` and
flattens the p99 into a straight line. Latency buckets here span 5 ms to 10
minutes, and TTFT gets its own finer-grained set below one second because that
is where users notice.

**TTFT is a separate objective from total latency.** A 3-second response that
starts streaming at 200 ms and a 3-second response that sits in silence are not
the same product, and one number cannot represent both.

**Alerts fire on burn rate, not thresholds.** A `p99 > 2s` alert pages someone
every time one slow request lands. Multi-window burn rate pages when the
service is actually going to miss its objective — 14.4x over an hour, 6x over
six hours, a slow leak files a ticket instead. A minimum-sample guard stops a
quiet service paging at 3am because three of its four requests failed.

**Autoscale on queue depth, not CPU.** The gateway spends its time waiting on
the model server, so CPU stays flat while requests pile up. Queue depth is the
signal that correlates with user-visible latency, so that is what the HPA uses.

**Prompts are hashed by default.** `capture_text` is opt-in. Identical prompts
hash identically, which is enough to spot a retry storm or a hot cache key
without turning the trace store into an uncontrolled copy of customer data.

**Sampling decides once, at the root, and errors always survive.** A trace
missing half its spans is worse than no trace, and the 0.1% of requests you
need to look at are exactly the ones a uniform sampler discards.

**Retries get their own span kind.** A request that took six seconds because it
was attempted three times and one that took six seconds in a single call have
completely different fixes.

---

## Deployment

```bash
docker compose up        # gateway + Prometheus + Grafana at :3000
kubectl apply -f deploy/k8s/
```

The Kubernetes manifests include what a production review asks for: non-root
with a read-only root filesystem and all capabilities dropped, `maxUnavailable:
0` rollouts, a PodDisruptionBudget, topology spread, separate startup/liveness/
readiness probes, and an HPA that scales on queue depth with asymmetric
behaviour (fast up, slow down — releasing a replica mid-generation costs a
request).

Alert rules in `deploy/alerts.yml` cover budget burn, TTFT degradation, queue
backlog, KV-cache saturation, backend down and retry storms. The Grafana
dashboard is provisioned automatically with 12 panels covering latency, TTFT,
throughput, queue depth, cache hit rate and inter-token latency.

---

## Switching to a real backend

```bash
export LLMOBS_BACKEND=vllm
export LLMOBS_BASE_URL=http://vllm:8000
export LLMOBS_MODEL=meta-llama/Llama-3.1-8B-Instruct
uvicorn llmobs.server:app --port 8080
```

Backend metric names are normalised into one shape, so nothing downstream
changes:

| Concept | vLLM | Triton |
|---|---|---|
| Queue depth | `vllm:num_requests_waiting` | `nv_inference_pending_request_count` |
| Running batch | `vllm:num_requests_running` | `nv_inference_exec_count` |
| Cache use | `vllm:gpu_cache_usage_perc` | derived from GPU memory |

`deploy/k8s/vllm.yaml` includes a working vLLM deployment (GPU node selector,
`/dev/shm` sizing for tensor parallelism, a 120 s readiness delay for weight
loading, prefix caching enabled).

---

## Project layout

```
src/llmobs/
  trace.py         Spans, traces, sampling, exclusive-time attribution
  metrics.py       Counter/Gauge/Histogram + Prometheus exposition
  validation.py    Response quality checks
  slo.py           Objectives, error budgets, multi-window burn rate
  store.py         SQLite trace store and on-call queries
  gateway.py       The instrumented request path
  loadgen.py       Concurrent traffic generator
  cli.py           load / trace / traces / stages / slo / health / metrics
  server.py        FastAPI service
  backends/        base, local simulator, vLLM, Triton, OpenAI-compatible
deploy/            Prometheus config + alerts, Grafana dashboard, k8s manifests
tests/             134 tests
```

## Testing

```bash
pytest                       # 134 tests, ~5 seconds, no GPU
pytest --cov=llmobs
ruff check src tests
```

The simulator makes the awkward paths testable: retry exhaustion, load
shedding, queueing under concurrency, burn-rate escalation, prefix-cache hits
and streaming TTFT are all covered by real assertions rather than mocks.

## Limitations

- Token counts from the simulator and the Triton adapter are estimated at ~4
  characters per token. Attach a real tokenizer when precision matters.
- The SLO tracker holds events in memory for the longest configured window.
  That suits a single gateway process; for a fleet, record the same events to
  Prometheus and run the equivalent rules there (`deploy/alerts.yml` does).
- SQLite is the right trace store for a reference platform and for development.
  At production trace volume, swap the driver for ClickHouse or Tempo — the
  schema is shaped for that move.

## License

MIT
