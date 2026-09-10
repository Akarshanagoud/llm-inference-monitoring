"""Trace persistence and the queries built on top of it."""
from llmobs import SpanKind, Tracer, TraceStore


def _write_traces(store, count=5, error_every=0):
    tracer = Tracer()
    tracer.add_exporter(store)
    for i in range(count):
        with tracer.trace("request") as root:
            root.model = "m1"
            root.backend = "local-sim"
            root.usage.prompt_tokens = 10
            root.usage.completion_tokens = 20
            root.set(valid=(i % 3 != 0))
            with tracer.span("infer", SpanKind.INFERENCE) as span:
                span.model = "m1"
            if error_every and i % error_every == 0:
                root.finish(__import__("llmobs").SpanStatus.ERROR, "boom")
    return tracer


def test_saved_trace_round_trips(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    tracer = _write_traces(store, 1)
    del tracer
    rows = store.recent()
    assert len(rows) == 1
    payload = store.get(rows[0]["trace_id"])
    assert payload["span_count"] == 2


def test_recent_orders_newest_first(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    _write_traces(store, 5)
    rows = store.recent(limit=5)
    timestamps = [r["started_at"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


def test_filter_by_invalid(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    _write_traces(store, 6)
    invalid = store.recent(invalid_only=True)
    assert invalid
    assert all(r["valid"] == 0 for r in invalid)


def test_filter_by_status(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    _write_traces(store, 6, error_every=2)
    errors = store.recent(status="error")
    assert errors
    assert all(r["status"] == "error" for r in errors)


def test_slowest_orders_by_duration(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    _write_traces(store, 5)
    rows = store.slowest(5)
    durations = [r["duration_ms"] for r in rows]
    assert durations == sorted(durations, reverse=True)


def test_stage_latency_groups_by_kind(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    _write_traces(store, 4)
    stages = {row["kind"] for row in store.stage_latency()}
    assert {"request", "inference"} <= stages


def test_summary_counts_errors_and_tokens(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    _write_traces(store, 6, error_every=3)
    summary = store.summary()
    assert summary["traces"] == 6
    assert summary["errors"] == 2
    assert summary["prompt_tokens"] == 60


def test_prune_keeps_the_most_recent(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    _write_traces(store, 10)
    removed = store.prune(keep_last=3)
    assert removed == 7
    assert len(store.recent(limit=100)) == 3


def test_prune_also_removes_orphaned_spans(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    _write_traces(store, 5)
    store.prune(keep_last=1)
    span_trace_ids = {row["kind"] for row in store.stage_latency()}
    remaining = store._conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    assert remaining == 2  # one trace, two spans
    assert span_trace_ids


def test_iter_traces_pages_through_everything(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    _write_traces(store, 7)
    assert len(list(store.iter_traces(batch_size=2))) == 7


def test_missing_trace_returns_none(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    assert store.get("does-not-exist") is None
