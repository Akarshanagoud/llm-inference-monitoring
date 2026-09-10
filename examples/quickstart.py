"""Runnable tour of the platform. No GPU, no network, no API keys.

    python examples/quickstart.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llmobs import GatewayConfig, InferenceGateway, LocalSimBackend  # noqa: E402
from llmobs.loadgen import generate_load  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> None:
    db = Path(tempfile.mkdtemp()) / "traces.db"
    gateway = InferenceGateway(
        LocalSimBackend(speed=60, failure_rate=0.05),
        GatewayConfig(trace_store_path=str(db), max_retries=2),
    )

    rule("1. One instrumented request")
    result = gateway.generate("Summarise the Q3 report in three bullets.", max_tokens=64)
    print(f"trace_id      : {result.trace_id}")
    print(f"duration      : {result.duration_ms:.1f} ms")
    print(f"ttft          : {result.ttft_ms:.1f} ms")
    print(f"tokens        : {result.prompt_tokens} prompt / {result.completion_tokens} completion")
    print(f"attempts      : {result.attempts}")
    print(f"valid         : {result.valid}")
    print(f"breakdown     : {result.breakdown_ms}")

    rule("2. Response validation catches quality regressions")
    bad = gateway.generate(
        "produce json please", max_tokens=48, validation_context={"expect_json": True}
    )
    print(f"valid: {bad.valid}")
    for issue in bad.validation.issues:
        print(f"  [{issue.severity}] {issue.check}: {issue.message}")

    rule("3. Prefix caching shows up in the metrics")
    prefix = "You are a support assistant. Cite the policy section. " * 6
    gateway.generate(prefix + "First question?", max_tokens=32)
    second = gateway.generate(prefix + "Second question?", max_tokens=32)
    print(f"cached prompt tokens on the second call: {second.cached_prompt_tokens}")

    rule("4. Concurrent load, with retries and queueing")
    load = generate_load(gateway, requests=300, concurrency=16, max_tokens=64)
    print(load.render())

    rule("5. Where the time actually goes")
    total = sum(row["total_ms"] for row in gateway.store.stage_latency()) or 1
    for row in gateway.store.stage_latency():
        share = row["total_ms"] / total
        print(f"  {row['kind']:<12} {row['mean_ms']:>8.2f} ms mean  "
              f"{'#' * int(share * 40)} {share:.1%}")

    rule("6. The slowest requests, and why")
    for row in gateway.store.slowest(3):
        payload = gateway.store.get(row["trace_id"])
        print(f"  {row['trace_id'][:12]}  {row['duration_ms']:.1f} ms  {payload['breakdown_ms']}")

    rule("7. SLO status and error budget")
    for status in gateway.slo.all_status():
        print(
            f"  {status.slo:<16} observed {status.observed:.3%}  "
            f"budget left {status.budget_remaining:>6.1%}  [{status.severity.value}]"
        )
        print(f"     burn rates: {({k: round(v, 2) for k, v in status.burn_rates.items()})}")

    rule("8. Prometheus exposition (excerpt)")
    lines = gateway.prometheus().splitlines()
    for line in [line for line in lines if "duration_seconds" in line][:6]:
        print("  " + line)

    rule("9. Backend health, normalised across serving stacks")
    print(json.dumps(gateway.refresh_backend_health(), indent=2))

    gateway.close()
    print("\nDone. No GPU was harmed in the making of this demo.")


if __name__ == "__main__":
    main()
