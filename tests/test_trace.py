"""Span hierarchy, timing attribution and sampling."""
import pytest

from llmobs import SpanKind, SpanStatus, Tracer
from llmobs.trace import Span, Trace, hash_text


class Collector:
    def __init__(self):
        self.traces = []

    def export(self, trace):
        self.traces.append(trace)


def _tracer(**kwargs):
    tracer = Tracer(**kwargs)
    sink = Collector()
    tracer.add_exporter(sink)
    return tracer, sink


def test_root_span_is_exported():
    tracer, sink = _tracer()
    with tracer.trace("request"):
        pass
    assert len(sink.traces) == 1
    assert sink.traces[0].root.name == "request"


def test_child_spans_attach_to_the_parent():
    tracer, sink = _tracer()
    with tracer.trace("request") as root:
        with tracer.span("retrieve", SpanKind.RETRIEVAL):
            pass
        with tracer.span("infer", SpanKind.INFERENCE):
            pass
    trace = sink.traces[0]
    assert len(trace.spans) == 3
    assert all(s.parent_id == root.span_id for s in trace.spans if s is not root)
    assert all(s.trace_id == root.trace_id for s in trace.spans)


def test_nested_spans_record_depth():
    tracer, _ = _tracer()
    with (
        tracer.trace("request") as root,
        tracer.span("outer", SpanKind.TOOL) as outer,
        tracer.span("inner", SpanKind.INFERENCE) as inner,
    ):
        pass
    assert inner.parent_id == outer.span_id
    assert outer.parent_id == root.span_id


def test_exception_marks_the_span_as_error():
    tracer, sink = _tracer()
    with pytest.raises(ValueError), tracer.trace("request"):
        raise ValueError("boom")
    trace = sink.traces[0]
    assert trace.status is SpanStatus.ERROR
    assert "boom" in trace.root.error


def test_breakdown_excludes_child_time_from_the_parent():
    trace = Trace("t1")
    root = Span("request", SpanKind.REQUEST, "t1")
    root.duration_ms = 100.0
    child = Span("infer", SpanKind.INFERENCE, "t1", parent_id=root.span_id)
    child.duration_ms = 70.0
    trace.spans = [root, child]

    breakdown = trace.breakdown()
    assert breakdown["request"] == 30.0
    assert breakdown["inference"] == 70.0
    assert sum(breakdown.values()) == 100.0


def test_ttft_recorded_once():
    tracer, _ = _tracer()
    with tracer.trace("r") as span:
        span.record_first_token()
        first = span.ttft_ms
        span.record_first_token()
    assert span.ttft_ms == first


def test_tokens_per_second_excludes_prefill():
    span = Span("infer", SpanKind.INFERENCE, "t")
    span.duration_ms = 1100.0
    span.ttft_ms = 100.0
    span.usage.completion_tokens = 100
    # 100 tokens over the 1000 ms decode phase, not the full 1100 ms.
    assert span.tokens_per_second == pytest.approx(100.0)


def test_zero_sampling_drops_clean_traces():
    tracer, sink = _tracer(sample_rate=0.0)
    with tracer.trace("request"):
        pass
    assert sink.traces == []


def test_errors_are_sampled_even_at_zero_rate():
    tracer, sink = _tracer(sample_rate=0.0, always_sample_errors=True)
    with pytest.raises(RuntimeError), tracer.trace("request"):
        raise RuntimeError("fail")
    assert len(sink.traces) == 1


def test_child_inherits_the_sampling_decision():
    tracer, _ = _tracer(sample_rate=0.0)
    with tracer.trace("request") as root, tracer.span("child") as child:
        pass
    assert child.sampled == root.sampled


def test_prompt_text_is_hashed_by_default():
    tracer, _ = _tracer(capture_text=False)
    with tracer.trace("r") as span:
        tracer.record_prompt(span, "secret prompt", "secret completion")
    assert "prompt" not in span.attributes
    assert span.attributes["prompt_hash"] == hash_text("secret prompt")
    assert span.attributes["prompt_chars"] == len("secret prompt")


def test_capture_text_opts_in_to_storing_payloads():
    tracer, _ = _tracer(capture_text=True)
    with tracer.trace("r") as span:
        tracer.record_prompt(span, "hello", "world")
    assert span.attributes["prompt"] == "hello"


def test_identical_prompts_hash_identically():
    assert hash_text("same") == hash_text("same")
    assert hash_text("same") != hash_text("different")


def test_broken_exporter_does_not_break_the_request():
    class Broken:
        def export(self, trace):
            raise RuntimeError("sink down")

    tracer = Tracer()
    tracer.add_exporter(Broken())
    good = Collector()
    tracer.add_exporter(good)
    with tracer.trace("request"):
        pass
    assert len(good.traces) == 1


def test_usage_aggregates_across_spans():
    tracer, sink = _tracer()
    with tracer.trace("request") as root:
        root.usage.prompt_tokens = 10
        with tracer.span("infer") as span:
            span.usage.completion_tokens = 25
    usage = sink.traces[0].total_usage()
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 25
    assert usage.total_tokens == 35


def test_span_events_are_recorded():
    tracer, _ = _tracer()
    with tracer.trace("r") as span:
        span.add_event("retry_scheduled", attempt=1)
    assert span.events[0]["name"] == "retry_scheduled"
    assert span.events[0]["attributes"]["attempt"] == 1
