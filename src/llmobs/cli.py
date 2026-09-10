"""Command line interface.

    llmobs serve                       # run the instrumented gateway
    llmobs load --requests 500 -c 16   # drive traffic and print the report
    llmobs trace <trace_id>            # print one trace with its span tree
    llmobs traces --slow               # list recent or slowest traces
    llmobs stages                      # where request time is actually spent
    llmobs slo                         # objectives, burn rates, budget left
    llmobs health                      # probe the backend
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .backends import build_backend
from .gateway import GatewayConfig, InferenceGateway
from .loadgen import generate_load
from .store import TraceStore

EXIT_OK, EXIT_PROBLEM, EXIT_USAGE = 0, 1, 2


def _gateway(args: argparse.Namespace) -> InferenceGateway:
    backend_kwargs: dict[str, Any] = {}
    if args.backend in {"local", "sim"}:
        backend_kwargs = {"speed": args.speed, "failure_rate": args.failure_rate}
        if args.max_concurrency:
            backend_kwargs["max_concurrency"] = args.max_concurrency
    else:
        backend_kwargs = {"base_url": args.base_url, "model": args.model}

    backend = build_backend(args.backend, **backend_kwargs)
    config = GatewayConfig(
        trace_store_path=args.db,
        max_retries=args.max_retries,
        max_in_flight=args.max_in_flight,
    )
    return InferenceGateway(backend, config)


def cmd_load(args: argparse.Namespace) -> int:
    gateway = _gateway(args)
    try:
        result = generate_load(
            gateway,
            requests=args.requests,
            concurrency=args.concurrency,
            max_tokens=args.max_tokens,
            prefix_reuse=args.prefix_reuse,
        )
        if args.json:
            print(json.dumps(
                {"load": result.to_dict(), "gateway": gateway.snapshot()}, indent=2, default=str
            ))
        else:
            print(result.render())
            print()
            print("SLO status")
            for status in gateway.slo.all_status():
                budget = status.budget_remaining
                print(
                    f"  {status.slo:<16} objective {status.objective:.3%}  "
                    f"observed {status.observed:.3%}  budget left {budget:.1%}  "
                    f"[{status.severity.value}]"
                )
        return EXIT_PROBLEM if result.error_rate > 0.05 else EXIT_OK
    finally:
        gateway.close()


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "error: the server extra is required: pip install 'llmobs[server]'",
            file=sys.stderr,
        )
        return EXIT_USAGE

    import os

    os.environ.setdefault("LLMOBS_BACKEND", args.backend)
    os.environ.setdefault("LLMOBS_DB", args.db or "traces.db")
    os.environ.setdefault("LLMOBS_BASE_URL", args.base_url)
    os.environ.setdefault("LLMOBS_MODEL", args.model)
    uvicorn.run("llmobs.server:app", host=args.host, port=args.port, log_level="info")
    return EXIT_OK


def cmd_trace(args: argparse.Namespace) -> int:
    store = TraceStore(args.db or "traces.db")
    payload = store.get(args.trace_id)
    if payload is None:
        print(f"trace {args.trace_id} not found", file=sys.stderr)
        return EXIT_PROBLEM
    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK

    print(f"trace {payload['trace_id']}  {payload['duration_ms']} ms  [{payload['status']}]")
    usage = payload["usage"]
    print(
        f"tokens: {usage['prompt_tokens']} prompt / {usage['completion_tokens']} completion"
    )
    print("\nbreakdown by stage:")
    for kind, ms in payload["breakdown_ms"].items():
        share = ms / payload["duration_ms"] if payload["duration_ms"] else 0
        bar = "#" * int(share * 40)
        print(f"  {kind:<12} {ms:>9.2f} ms  {bar}")

    print("\nspan tree:")
    spans = payload["spans"]
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for span in spans:
        by_parent.setdefault(span["parent_id"], []).append(span)

    def walk(parent: str | None, depth: int) -> None:
        for span in by_parent.get(parent, []):
            marker = "" if span["status"] == "ok" else f"  <{span['status']}>"
            ttft = f"  ttft={span['ttft_ms']}ms" if span.get("ttft_ms") else ""
            print(
                f"  {'  ' * depth}{span['name']:<28} {span['duration_ms'] or 0:>8.2f} ms"
                f"{ttft}{marker}"
            )
            walk(span["span_id"], depth + 1)

    walk(None, 0)
    return EXIT_OK


def cmd_traces(args: argparse.Namespace) -> int:
    store = TraceStore(args.db or "traces.db")
    rows = (
        store.slowest(args.limit)
        if args.slow
        else store.recent(
            limit=args.limit,
            status=args.status,
            invalid_only=args.invalid,
            min_duration_ms=args.min_ms,
        )
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return EXIT_OK
    if not rows:
        print("no traces recorded")
        return EXIT_OK
    print(f"{'TRACE':<34}{'MS':>10}  {'TTFT':>8}  {'STATUS':<8} MODEL")
    for row in rows:
        print(
            f"{row['trace_id']:<34}{row.get('duration_ms') or 0:>10.2f}  "
            f"{row.get('ttft_ms') or 0:>8.2f}  {row.get('status', ''):<8} "
            f"{row.get('model') or ''}"
        )
    print(f"\n{json.dumps(store.summary())}")
    return EXIT_OK


def cmd_stages(args: argparse.Namespace) -> int:
    store = TraceStore(args.db or "traces.db")
    rows = store.stage_latency()
    if args.json:
        print(json.dumps(rows, indent=2))
        return EXIT_OK
    if not rows:
        print("no spans recorded")
        return EXIT_OK
    total = sum(r["total_ms"] for r in rows) or 1.0
    print(f"{'STAGE':<14}{'COUNT':>8}{'MEAN ms':>10}{'MAX ms':>10}{'SHARE':>8}")
    for row in rows:
        share = row["total_ms"] / total
        print(
            f"{row['kind']:<14}{row['count']:>8}{row['mean_ms']:>10.2f}"
            f"{row['max_ms']:>10.2f}{share:>7.1%}"
        )
    return EXIT_OK


def cmd_slo(args: argparse.Namespace) -> int:
    gateway = _gateway(args)
    try:
        store = gateway.store or TraceStore(args.db or "traces.db")
        # Replay stored traces so burn rates reflect recorded history rather
        # than an empty in-process window.
        replayed = 0
        for payload in store.iter_traces():
            gateway.slo.record(
                ok=payload["status"] == "ok",
                latency_seconds=(payload["duration_ms"] or 0) / 1000,
                timestamp=payload["spans"][0]["started_at"] if payload["spans"] else None,
            )
            replayed += 1

        statuses = [s.to_dict() for s in gateway.slo.all_status()]
        if args.json:
            print(json.dumps({"replayed_traces": replayed, "slos": statuses}, indent=2))
        else:
            print(f"replayed {replayed} traces\n")
            for status in statuses:
                print(f"{status['slo']}")
                print(f"  objective        : {status['objective']:.3%}")
                print(f"  observed         : {status['observed']:.3%}")
                print(f"  budget remaining : {status['budget_remaining']:.1%}")
                print(f"  burn rates       : {status['burn_rates']}")
                print(f"  severity         : {status['severity']}")
                print()
        worst = gateway.slo.worst_severity().value
        return EXIT_PROBLEM if worst != "ok" else EXIT_OK
    finally:
        gateway.close()


def cmd_health(args: argparse.Namespace) -> int:
    gateway = _gateway(args)
    try:
        health = gateway.refresh_backend_health()
        print(json.dumps(health, indent=2))
        return EXIT_OK if health["up"] else EXIT_PROBLEM
    finally:
        gateway.close()


def cmd_metrics(args: argparse.Namespace) -> int:
    gateway = _gateway(args)
    try:
        generate_load(gateway, requests=args.requests, concurrency=args.concurrency)
        print(gateway.prometheus())
        return EXIT_OK
    finally:
        gateway.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmobs", description="Observability and reliability tooling for LLM serving."
    )
    parser.add_argument("--version", action="store_true")
    sub = parser.add_subparsers(dest="command")

    def add_backend_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--backend", default="local",
            choices=["local", "sim", "vllm", "triton", "openai-compat", "tensorrt-llm"],
        )
        p.add_argument("--base-url", default="http://localhost:8000")
        p.add_argument("--model", default="sim-7b-instruct")
        p.add_argument("--db", default="traces.db", help="SQLite trace store path")
        p.add_argument("--speed", type=float, default=50.0,
                       help="simulator time compression (local backend only)")
        p.add_argument("--failure-rate", type=float, default=0.0)
        p.add_argument("--max-concurrency", type=int, default=0)
        p.add_argument("--max-retries", type=int, default=2)
        p.add_argument("--max-in-flight", type=int, default=0)

    load = sub.add_parser("load", help="drive traffic through the gateway")
    add_backend_args(load)
    load.add_argument("-n", "--requests", type=int, default=200)
    load.add_argument("-c", "--concurrency", type=int, default=8)
    load.add_argument("--max-tokens", type=int, default=96)
    load.add_argument("--prefix-reuse", type=float, default=0.8)
    load.add_argument("--json", action="store_true")
    load.set_defaults(func=cmd_load)

    serve = sub.add_parser("serve", help="run the FastAPI gateway")
    add_backend_args(serve)
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=cmd_serve)

    trace = sub.add_parser("trace", help="print one trace")
    trace.add_argument("trace_id")
    trace.add_argument("--db", default="traces.db")
    trace.add_argument("--json", action="store_true")
    trace.set_defaults(func=cmd_trace)

    traces = sub.add_parser("traces", help="list traces")
    traces.add_argument("--db", default="traces.db")
    traces.add_argument("--limit", type=int, default=20)
    traces.add_argument("--slow", action="store_true", help="order by duration")
    traces.add_argument("--status")
    traces.add_argument("--invalid", action="store_true")
    traces.add_argument("--min-ms", type=float)
    traces.add_argument("--json", action="store_true")
    traces.set_defaults(func=cmd_traces)

    stages = sub.add_parser("stages", help="latency by pipeline stage")
    stages.add_argument("--db", default="traces.db")
    stages.add_argument("--json", action="store_true")
    stages.set_defaults(func=cmd_stages)

    slo = sub.add_parser("slo", help="objectives, burn rates and error budget")
    add_backend_args(slo)
    slo.add_argument("--json", action="store_true")
    slo.set_defaults(func=cmd_slo)

    health = sub.add_parser("health", help="probe the backend")
    add_backend_args(health)
    health.set_defaults(func=cmd_health)

    metrics = sub.add_parser("metrics", help="print Prometheus exposition after a load run")
    add_backend_args(metrics)
    metrics.add_argument("-n", "--requests", type=int, default=50)
    metrics.add_argument("-c", "--concurrency", type=int, default=4)
    metrics.set_defaults(func=cmd_metrics)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "version", False):
        from . import __version__

        print(f"llmobs {__version__}")
        return EXIT_OK

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
