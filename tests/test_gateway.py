"""End-to-end gateway behaviour: retries, shedding, validation, correlation."""
import pytest

from llmobs import (
    BackendError,
    GatewayConfig,
    InferenceGateway,
    LocalSimBackend,
    OverloadedError,
)
from llmobs.backends.base import Backend, BackendHealth, GenerationResponse


class FlakyBackend(Backend):
    """Fails ``failures`` times with a retryable error, then succeeds."""

    name = "flaky"

    def __init__(self, failures: int, retryable: bool = True):
        self.remaining = failures
        self.retryable = retryable
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise BackendError("transient", retryable=self.retryable)
        return GenerationResponse(
            text="recovered.", model="flaky-1", prompt_tokens=5, completion_tokens=2
        )

    def health(self):
        return BackendHealth(up=True, model="flaky-1")


def test_successful_request_returns_text_and_trace_id(gateway):
    result = gateway.generate("Summarise the report.", max_tokens=48)
    assert result.text
    assert len(result.trace_id) == 32
    assert result.attempts == 1
    assert result.completion_tokens > 0


def test_trace_is_persisted_and_retrievable(gateway):
    result = gateway.generate("hello", max_tokens=16)
    stored = gateway.store.get(result.trace_id)
    assert stored is not None
    assert stored["trace_id"] == result.trace_id
    assert stored["span_count"] >= 2


def test_trace_contains_inference_and_validation_spans(gateway):
    result = gateway.generate("hello", max_tokens=16)
    kinds = {s["kind"] for s in gateway.store.get(result.trace_id)["spans"]}
    assert {"request", "inference", "validation"} <= kinds


def test_breakdown_attributes_time_to_stages(gateway):
    result = gateway.generate("hello", max_tokens=32)
    breakdown = gateway.store.get(result.trace_id)["breakdown_ms"]
    assert "inference" in breakdown
    assert sum(breakdown.values()) == pytest.approx(result.duration_ms, rel=0.2)


def test_retryable_error_is_retried_then_succeeds():
    gateway = InferenceGateway(FlakyBackend(failures=2), GatewayConfig(retry_base_delay=0))
    result = gateway.generate("hi")
    assert result.text == "recovered."
    assert result.attempts == 3
    assert gateway.metrics.retries.get(model="unknown", backend="flaky") == 2


def test_non_retryable_error_fails_immediately():
    backend = FlakyBackend(failures=5, retryable=False)
    gateway = InferenceGateway(backend, GatewayConfig(retry_base_delay=0))
    with pytest.raises(BackendError):
        gateway.generate("hi")
    assert backend.calls == 1


def test_retries_are_capped():
    backend = FlakyBackend(failures=99)
    gateway = InferenceGateway(backend, GatewayConfig(max_retries=2, retry_base_delay=0))
    with pytest.raises(BackendError):
        gateway.generate("hi")
    assert backend.calls == 3  # initial attempt plus two retries


def test_retry_appears_as_its_own_span(tmp_path):
    gateway = InferenceGateway(
        FlakyBackend(failures=1),
        GatewayConfig(retry_base_delay=0, trace_store_path=str(tmp_path / "t.db")),
    )
    result = gateway.generate("hi")
    kinds = [s["kind"] for s in gateway.store.get(result.trace_id)["spans"]]
    assert "retry" in kinds
    gateway.close()


def test_failed_request_counts_against_availability_slo():
    gateway = InferenceGateway(
        FlakyBackend(failures=99, retryable=False), GatewayConfig(retry_base_delay=0)
    )
    with pytest.raises(BackendError):
        gateway.generate("hi")
    status = gateway.slo.status("availability")
    assert status.total_events == 1
    assert status.bad_events == 1


def test_load_shedding_rejects_beyond_the_limit(backend):
    gateway = InferenceGateway(backend, GatewayConfig(max_in_flight=1))
    gateway._in_flight = 1  # simulate a request already in flight
    with pytest.raises(OverloadedError):
        gateway.generate("hi")


def test_shedding_disabled_by_default(gateway):
    gateway._in_flight = 500
    result = gateway.generate("hi", max_tokens=8)
    assert result.text


def test_validation_runs_and_is_reported(gateway):
    result = gateway.generate("hello", max_tokens=32)
    assert result.validation is not None
    assert isinstance(result.valid, bool)


def test_expected_json_failure_is_surfaced(gateway):
    result = gateway.generate(
        "give me json", max_tokens=32, validation_context={"expect_json": True}
    )
    assert not result.valid
    assert "valid_json" in result.validation.checks_failed


def test_forbidden_phrase_marks_response_invalid(gateway):
    result = gateway.generate(
        "hello", max_tokens=32, validation_context={"must_not_contain": ["model"]}
    )
    assert not result.valid


def test_validation_can_be_disabled(backend):
    gateway = InferenceGateway(backend, GatewayConfig(validate_responses=False))
    result = gateway.generate("hello", max_tokens=16)
    assert result.validation is None
    assert result.valid


def test_prefix_cache_hits_are_recorded(gateway):
    prefix = "You are a helpful assistant. " * 12
    gateway.generate(prefix + "First question?", max_tokens=16)
    second = gateway.generate(prefix + "Second question?", max_tokens=16)
    assert second.cached_prompt_tokens > 0


def test_listener_is_notified(gateway):
    seen = []
    gateway.add_listener(seen.append)
    gateway.generate("hello", max_tokens=16)
    assert len(seen) == 1


def test_broken_listener_does_not_fail_the_request(gateway):
    def boom(_result):
        raise RuntimeError("listener down")

    gateway.add_listener(boom)
    assert gateway.generate("hello", max_tokens=16).text


def test_streaming_yields_chunks_and_records_a_trace(gateway):
    chunks = list(gateway.stream("hello", max_tokens=24))
    assert len(chunks) > 1
    assert gateway.store.summary()["traces"] >= 1


def test_streaming_records_ttft_before_completion(gateway):
    list(gateway.stream("hello", max_tokens=24))
    recent = gateway.store.recent(limit=1)[0]
    assert recent["ttft_ms"] is not None
    assert recent["ttft_ms"] < (recent["duration_ms"] or 0)


def test_backend_health_mirrors_into_metrics(gateway):
    health = gateway.refresh_backend_health()
    assert health["up"] is True
    assert gateway.metrics.model_up.get(backend="local-sim") == 1.0


def test_snapshot_reports_slos_and_traces(gateway):
    gateway.generate("hello", max_tokens=16)
    snapshot = gateway.snapshot()
    assert snapshot["backend"] == "local-sim"
    assert len(snapshot["slos"]) == 3
    assert snapshot["traces"]["traces"] == 1


def test_local_backend_queues_under_concurrency():
    from concurrent.futures import ThreadPoolExecutor

    backend = LocalSimBackend(speed=200, max_concurrency=2)
    gateway = InferenceGateway(backend, GatewayConfig())
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [pool.submit(gateway.generate, f"q{i}", max_tokens=24) for i in range(8)]
        durations = [f.result().duration_ms for f in results]
    # Queued requests wait for a batch slot, so the spread is wide.
    assert max(durations) > min(durations)


def test_breakdown_is_returned_to_the_caller(gateway):
    result = gateway.generate("hello", max_tokens=32)
    assert result.breakdown_ms, "stage attribution should reach the caller"
    assert "inference" in result.breakdown_ms
    assert sum(result.breakdown_ms.values()) == pytest.approx(result.duration_ms, rel=0.25)
