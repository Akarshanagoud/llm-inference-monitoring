"""Backend adapters: the simulator's latency model and metric normalisation."""
import pytest

from llmobs import BackendError, LocalSimBackend, build_backend
from llmobs.backends import GenerationRequest, estimate_tokens, parse_prometheus


def test_local_backend_is_deterministic_for_the_same_prompt():
    backend = LocalSimBackend(speed=1000, jitter=0)
    a = backend.generate(GenerationRequest(prompt="same prompt", max_tokens=32))
    b = backend.generate(GenerationRequest(prompt="same prompt", max_tokens=32))
    assert a.text == b.text


def test_different_prompts_give_different_completions():
    backend = LocalSimBackend(speed=1000, jitter=0)
    a = backend.generate(GenerationRequest(prompt="one", max_tokens=32))
    b = backend.generate(GenerationRequest(prompt="two", max_tokens=32))
    assert a.text != b.text


def test_longer_output_takes_longer():
    # speed=20 keeps both generations well above the OS timer granularity
    # (~15 ms on Windows), which sub-millisecond sleeps cannot clear.
    backend = LocalSimBackend(speed=20, jitter=0)
    import time

    start = time.perf_counter()
    short_response = backend.generate(GenerationRequest(prompt="p", max_tokens=8))
    short = time.perf_counter() - start

    start = time.perf_counter()
    long_response = backend.generate(GenerationRequest(prompt="p", max_tokens=200))
    long = time.perf_counter() - start

    assert long_response.completion_tokens > short_response.completion_tokens
    assert long > short


def test_repeated_prefix_produces_a_cache_hit():
    backend = LocalSimBackend(speed=1000)
    prefix = "system preamble " * 30
    backend.generate(GenerationRequest(prompt=prefix + "a", max_tokens=8))
    second = backend.generate(GenerationRequest(prompt=prefix + "b", max_tokens=8))
    assert second.cached_prompt_tokens > 0


def test_failure_rate_raises_retryable_errors():
    backend = LocalSimBackend(speed=1000, failure_rate=1.0)
    with pytest.raises(BackendError) as excinfo:
        backend.generate(GenerationRequest(prompt="p"))
    assert excinfo.value.retryable


def test_closed_backend_refuses_work():
    backend = LocalSimBackend(speed=1000)
    backend.close()
    with pytest.raises(BackendError):
        backend.generate(GenerationRequest(prompt="p"))
    assert backend.health().up is False


def test_streaming_yields_multiple_chunks():
    backend = LocalSimBackend(speed=1000)
    chunks = list(backend.stream(GenerationRequest(prompt="p", max_tokens=20)))
    assert len(chunks) > 1


def test_health_reports_normalised_fields():
    health = LocalSimBackend(speed=1000).health()
    payload = health.to_dict()
    assert set(payload) >= {"up", "model", "queue_depth", "kv_cache_utilization"}


def test_token_estimate_scales_with_length():
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)
    assert estimate_tokens("") == 1


def test_build_backend_factory():
    assert isinstance(build_backend("local", speed=1000), LocalSimBackend)
    assert build_backend("vllm", base_url="http://x").name == "vllm"
    assert build_backend("triton", base_url="http://x:8000").name == "triton"


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="unknown backend"):
        build_backend("nope")


def test_prometheus_parser_reads_labelled_samples():
    text = """
# HELP vllm:num_requests_waiting Waiting
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{model="x"} 4
vllm:gpu_cache_usage_perc{model="x"} 0.73
garbage line without value
"""
    parsed = parse_prometheus(text)
    assert parsed["vllm:num_requests_waiting"] == 4.0
    assert parsed["vllm:gpu_cache_usage_perc"] == 0.73
    assert "garbage" not in parsed


def test_prometheus_parser_ignores_comments_and_blanks():
    assert parse_prometheus("# only a comment\n\n") == {}


def test_unreachable_backend_reports_down_rather_than_raising():
    backend = build_backend("vllm", base_url="http://127.0.0.1:1", model="m")
    health = backend.health()
    assert health.up is False
    assert health.detail
