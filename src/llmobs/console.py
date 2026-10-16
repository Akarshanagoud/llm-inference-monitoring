"""Operations console: live traffic, SLO burn, traces and load tests.

Mounted by ``llmobs.server`` at ``/ui``. Every chart is computed from the
gateway's own trace store and SLO tracker, the same data the Prometheus
endpoint and the CLI read.

The traffic generator and incident scenarios exist so the console can be
explored without a production workload: they drive the real gateway, and the
scenarios change the simulator's latency and failure parameters at runtime.
"""
from __future__ import annotations

import contextlib
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .backends.base import BackendError
from .gateway import InferenceGateway, OverloadedError
from .loadgen import SYSTEM_PREFIXES, TASKS, generate_load

WEB_DIR = Path(__file__).parent / "web"

SCENARIOS: dict[str, dict[str, Any]] = {
    "healthy": {"label": "Healthy", "detail": "Nominal latency, no injected failures."},
    "flaky": {
        "label": "Flaky backend",
        "detail": "25% of backend calls fail. Most are recovered by retries; a few reach the caller.",
        "failure_rate": 0.25,
    },
    "degraded": {
        "label": "Degraded backend",
        "detail": "Prefill stalls for about 1.4 s and decoding slows: time to first token breaches its objective.",
        "base_overhead_ms": 1400.0,
        "decode_ms_per_token": 26.0,
    },
}
TUNABLE = ("failure_rate", "base_overhead_ms", "decode_ms_per_token")


class TrafficRequest(BaseModel):
    running: bool = True
    rps: float = Field(3.0, gt=0, le=40)


class ScenarioRequest(BaseModel):
    scenario: str


class LoadRequest(BaseModel):
    requests: int = Field(60, ge=1, le=2000)
    concurrency: int = Field(12, ge=1, le=64)
    max_tokens: int = Field(96, ge=8, le=512)
    stream: bool = True


class TrafficGenerator:
    """Poisson arrivals through the real gateway, with switchable incidents."""

    def __init__(self, gateway: InferenceGateway) -> None:
        self.gateway = gateway
        self.rps = 3.0
        self.scenario = "healthy"
        self.sent = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix="llmobs-traffic")
        self._rng = random.Random(11)
        backend = gateway.backend
        self._baseline = {k: getattr(backend, k) for k in TUNABLE if hasattr(backend, k)}
        self.load_history: list[dict[str, Any]] = []

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def simulated(self) -> bool:
        return len(self._baseline) == len(TUNABLE)

    def start(self, rps: float) -> None:
        self.rps = rps
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="llmobs-arrivals", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_scenario(self, name: str) -> None:
        if name not in SCENARIOS:
            raise HTTPException(status_code=422, detail=f"unknown scenario {name!r}")
        if not self.simulated:
            raise HTTPException(status_code=409, detail="incident scenarios need the local simulator backend")
        backend = self.gateway.backend
        for key, value in self._baseline.items():
            setattr(backend, key, value)
        for key in TUNABLE:
            if key in SCENARIOS[name]:
                setattr(backend, key, SCENARIOS[name][key])
        self.scenario = name

    def prompt(self) -> str:
        prefix = self._rng.choice(SYSTEM_PREFIXES) if self._rng.random() < 0.8 else f"Session {self._rng.randint(1, 10**9)}. "
        return prefix + self._rng.choice(TASKS)

    def _loop(self) -> None:
        next_at = time.monotonic()
        while not self._stop.is_set():
            next_at += self._rng.expovariate(self.rps)
            delay = next_at - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                break
            if delay < -2:  # fell behind (laptop slept): do not burst to catch up
                next_at = time.monotonic()
            self._pool.submit(self._one, self.prompt(), self._rng.choice((64, 96, 128, 160)))
            self.sent += 1

    def _one(self, prompt: str, max_tokens: int) -> None:
        # Chat traffic streams, which is what makes time to first token meaningful.
        # Failures are recorded by the gateway itself: metrics, trace and SLO event.
        with contextlib.suppress(BackendError, OverloadedError):
            self.gateway.generate(prompt, max_tokens=max_tokens, stream=True)

    def state(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "rps": self.rps,
            "sent": self.sent,
            "scenario": self.scenario,
            "simulated": self.simulated,
            "scenarios": [{"name": k, "label": v["label"], "detail": v["detail"]} for k, v in SCENARIOS.items()],
        }

    def load_test(self, body: LoadRequest) -> dict[str, Any]:
        started = time.time()
        result = generate_load(
            self.gateway,
            requests=body.requests,
            concurrency=body.concurrency,
            max_tokens=body.max_tokens,
            seed=len(self.load_history) + 7,
            stream=body.stream,
        ).to_dict()
        record = {"started_at": started, "scenario": self.scenario, **body.model_dump(), **result}
        self.load_history.insert(0, record)
        del self.load_history[20:]
        return record


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 2)


def overview(gateway: InferenceGateway, traffic: TrafficGenerator, window: int) -> dict[str, Any]:
    if gateway.store is None:
        raise HTTPException(status_code=409, detail="no trace store configured")
    now = time.time()
    since = now - window
    rows = gateway.store.outcomes(since=since)
    bucket = max(2, window // 60)
    first = int(since // bucket) * bucket
    count = int((now - first) // bucket) + 1
    buckets: list[dict[str, Any]] = [
        {"t": first + i * bucket, "requests": 0, "failed": 0, "retried": 0, "invalid": 0, "latency": [], "ttft": [], "tokens": 0}
        for i in range(count)
    ]
    for row in rows:
        b = buckets[min(count - 1, int((row["started_at"] - first) // bucket))]
        b["requests"] += 1
        if row["outcome"] != "ok":
            b["failed"] += 1
            continue
        if row["status"] != "ok":
            b["retried"] += 1
        if not row["valid"]:
            b["invalid"] += 1
        b["latency"].append(row["duration_ms"] or 0.0)
        if row["ttft_ms"] is not None:
            b["ttft"].append(row["ttft_ms"])
        b["tokens"] += row["completion_tokens"] or 0
    series = [
        {
            "t": b["t"],
            "rps": round(b["requests"] / bucket, 3),
            "failed": b["failed"],
            "retried": b["retried"],
            "requests": b["requests"],
            "p50": _percentile(b["latency"], 0.5),
            "p95": _percentile(b["latency"], 0.95),
            "ttft_p95": _percentile(b["ttft"], 0.95),
            "tokens_per_s": round(b["tokens"] / bucket, 1),
        }
        for b in buckets
    ]
    served = [r for r in rows if r["outcome"] == "ok"]
    # Rates over the time actually observed, so a fresh session is not diluted
    # by the empty part of the window.
    observed = max(1.0, min(window, now - rows[0]["started_at"])) if rows else float(window)
    latencies = [r["duration_ms"] or 0.0 for r in served]
    ttfts = [r["ttft_ms"] for r in served if r["ttft_ms"] is not None]
    metrics = gateway.metrics.snapshot()
    tokens = metrics["tokens"]
    return {
        "window_seconds": window,
        "bucket_seconds": bucket,
        "kpis": {
            "requests": len(rows),
            "rps": round(len(rows) / observed, 2),
            "failed": len(rows) - len(served),
            "retried": sum(1 for r in served if r["status"] != "ok"),
            "invalid": sum(1 for r in served if not r["valid"]),
            "error_rate": round((len(rows) - len(served)) / len(rows), 4) if rows else 0.0,
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
            "ttft_p95": _percentile(ttfts, 0.95),
            "tokens_per_s": round(sum(r["completion_tokens"] or 0 for r in served) / observed, 1),
            "cache_hit_rate": round(tokens["cached"] / tokens["prompt"], 4) if tokens["prompt"] else 0.0,
            "in_flight": metrics["in_flight"],
        },
        "series": series,
        "stages": gateway.store.breakdown(since=since),
        "slos": [s.to_dict() for s in gateway.slo.all_status()],
        "slo_config": [
            {
                "name": slo.name,
                "objective": slo.objective,
                "kind": slo.kind,
                "indicator": slo.indicator,
                "threshold_seconds": slo.latency_threshold_seconds,
                "windows": [
                    {"name": w.name, "window_seconds": w.window_seconds, "threshold": w.threshold, "severity": w.severity.value}
                    for w in slo.burn_windows
                ],
            }
            for slo in gateway.slo.slos
        ],
        "worst_severity": gateway.slo.worst_severity().value,
        "backend": gateway.refresh_backend_health(),
        "traffic": traffic.state(),
    }


def build_router(gateway: InferenceGateway, traffic: TrafficGenerator) -> APIRouter:
    router = APIRouter(prefix="/v1/ui", tags=["console"])

    @router.get("/overview")
    def get_overview(window: int = 300) -> dict[str, Any]:
        return overview(gateway, traffic, max(60, min(window, 3600)))

    @router.get("/traffic")
    def get_traffic() -> dict[str, Any]:
        return traffic.state()

    @router.post("/traffic")
    def set_traffic(body: TrafficRequest) -> dict[str, Any]:
        if body.running:
            traffic.start(body.rps)
        else:
            traffic.stop()
        time.sleep(0.05)
        return traffic.state()

    @router.post("/scenario")
    def set_scenario(body: ScenarioRequest) -> dict[str, Any]:
        traffic.set_scenario(body.scenario)
        return traffic.state()

    @router.post("/load")
    def run_load(body: LoadRequest) -> dict[str, Any]:
        return traffic.load_test(body)

    @router.get("/load")
    def load_history() -> list[dict[str, Any]]:
        return traffic.load_history

    @router.get("/traces")
    def traces(limit: int = 100, view: str = "recent", window: int = 900) -> list[dict[str, Any]]:
        if gateway.store is None:
            raise HTTPException(status_code=409, detail="no trace store configured")
        rows = gateway.store.outcomes(since=time.time() - max(60, min(window, 86_400)))
        if view == "failed":
            rows = [r for r in rows if r["outcome"] != "ok"]
        elif view == "retried":
            rows = [r for r in rows if r["outcome"] == "ok" and r["status"] != "ok"]
        elif view == "invalid":
            rows = [r for r in rows if not r["valid"]]
        if view == "slowest":
            rows = sorted(rows, key=lambda r: -(r["duration_ms"] or 0))
        else:
            rows = list(reversed(rows))
        return rows[: max(1, min(limit, 500))]

    return router


def mount_console(app: FastAPI, gateway: InferenceGateway) -> TrafficGenerator:
    """Attach the console API, static front end and a ``/`` redirect to ``app``."""
    traffic = TrafficGenerator(gateway)
    app.include_router(build_router(gateway, traffic))
    app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=True), name="console")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/")

    return traffic
