"""Metric primitives and Prometheus exposition."""
from llmobs.metrics import LATENCY_BUCKETS, Counter, Gauge, Histogram, MetricsRegistry


def test_counter_accumulates_per_label_set():
    counter = Counter("c_total", "help")
    counter.inc(model="a")
    counter.inc(2, model="a")
    counter.inc(model="b")
    assert counter.get(model="a") == 3
    assert counter.get(model="b") == 1


def test_gauge_set_inc_dec():
    gauge = Gauge("g", "help")
    gauge.set(5)
    gauge.inc(2)
    gauge.dec(3)
    assert gauge.get() == 4


def test_histogram_buckets_are_cumulative():
    hist = Histogram("h", "help", (1.0, 2.0, 5.0))
    for value in (0.5, 1.5, 3.0):
        hist.observe(value)
    rendered = "\n".join(hist.render())
    assert 'h_bucket{le="1"} 1' in rendered
    assert 'h_bucket{le="2"} 2' in rendered
    assert 'h_bucket{le="5"} 3' in rendered
    assert 'h_bucket{le="+Inf"} 3' in rendered
    assert "h_count 3" in rendered


def test_histogram_quantiles_use_retained_samples():
    hist = Histogram("h", "help")
    for i in range(1, 101):
        hist.observe(i / 100)
    assert hist.quantile(0.5) == 0.5
    assert hist.quantile(0.95) == 0.95
    assert hist.count() == 100


def test_histogram_mean():
    hist = Histogram("h", "help")
    hist.observe(1.0)
    hist.observe(3.0)
    assert hist.mean() == 2.0


def test_quantile_of_empty_histogram_is_none():
    assert Histogram("h", "help").quantile(0.5) is None


def test_label_values_are_escaped():
    counter = Counter("c_total", "help")
    counter.inc(model='say "hi"')
    rendered = "\n".join(counter.render())
    assert '\\"hi\\"' in rendered


def test_registry_renders_valid_exposition_format(gateway):
    gateway.generate("hello world", max_tokens=32)
    text = gateway.prometheus()
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert lines, "expected at least one sample"
    for line in lines:
        # every sample line is "name{labels} value"
        assert " " in line
        float(line.rsplit(" ", 1)[1])
    assert "# TYPE llm_request_duration_seconds histogram" in text


def test_empty_metrics_are_omitted():
    registry = MetricsRegistry()
    text = registry.render()
    assert "llm_request_duration_seconds_bucket" not in text
    assert "llm_uptime_seconds" in text


def test_observe_span_records_latency_and_tokens(gateway):
    result = gateway.generate("hello", max_tokens=32)
    snapshot = gateway.metrics.snapshot()
    assert snapshot["requests_total"] == 1
    assert snapshot["tokens"]["completion"] == result.completion_tokens
    assert snapshot["latency_seconds"]["p50"] is not None


def test_latency_buckets_cover_slow_generations():
    # Default Prometheus buckets stop at 10s, which collapses every slow
    # generation into +Inf and flattens the p99.
    assert max(LATENCY_BUCKETS) >= 300


def test_snapshot_aggregates_across_label_sets(gateway):
    # Tokens are recorded per model and per backend; a service-level summary
    # must sum across those label sets rather than look for an exact match.
    gateway.generate("first prompt", max_tokens=32)
    gateway.generate("second prompt", max_tokens=32)
    snapshot = gateway.metrics.snapshot()
    assert snapshot["requests_total"] == 2
    assert snapshot["tokens"]["completion"] > 0
    assert snapshot["latency_seconds"]["p95"] is not None


def test_counter_total_matches_subset_labels():
    counter = Counter("c_total", "help")
    counter.inc(3, model="a", direction="prompt")
    counter.inc(4, model="b", direction="prompt")
    counter.inc(5, model="a", direction="completion")
    assert counter.total(direction="prompt") == 7
    assert counter.total(model="a") == 8
    assert counter.total() == 12
    assert counter.get(direction="prompt") == 0  # exact lookup still strict


def test_histogram_aggregates_quantiles_across_labels():
    hist = Histogram("h", "help")
    for i in range(50):
        hist.observe(i / 100, model="a")
    for i in range(50, 100):
        hist.observe(i / 100, model="b")
    assert hist.count() == 100
    assert hist.quantile(0.5) is not None
    assert hist.quantile(0.5, model="a") < hist.quantile(0.5, model="b")
