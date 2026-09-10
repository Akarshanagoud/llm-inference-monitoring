"""Adapters for real serving stacks: vLLM, Triton and any OpenAI-compatible API.

These use ``urllib`` from the standard library rather than ``requests`` or
``httpx``. An inference gateway sits on the hot path of every request, and the
HTTP client is not where the interesting engineering is — keeping the core
dependency-free means the image stays small and the library can be vendored
into an existing service without a dependency negotiation.

Each adapter's job is to translate: request shape in, normalised
:class:`BackendHealth` out. The Prometheus scrape of the backend's own
``/metrics`` is parsed here too, which is what lets queue depth and KV-cache
utilisation appear on the same dashboard regardless of who is serving.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from .base import Backend, BackendError, BackendHealth, GenerationRequest, GenerationResponse


def _post_json(url: str, payload: dict[str, Any], timeout: float, headers: dict[str, str]) -> dict:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=body, headers={"content-type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        # 429 and 5xx are worth retrying; a 400 means the request itself is wrong.
        raise BackendError(
            f"{url} returned {exc.code}: {detail}",
            retryable=exc.code == 429 or exc.code >= 500,
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise BackendError(f"{url} unreachable: {exc}", retryable=True) from exc


def _get_text(url: str, timeout: float) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode()
    except Exception as exc:  # noqa: BLE001 - health probes must not raise
        raise BackendError(f"{url} unreachable: {exc}", retryable=True) from exc


_METRIC_LINE = re.compile(r"^(?P<name>[a-zA-Z_:][\w:]*)(?P<labels>\{[^}]*\})?\s+(?P<value>[^\s]+)$")


def parse_prometheus(text: str) -> dict[str, float]:
    """Parse an exposition payload into ``{metric_name: last_value}``.

    Labels are dropped: for gateway-level health we want the aggregate, and a
    per-label breakdown is what the real Prometheus scrape is for.
    """
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _METRIC_LINE.match(line)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        name = match.group("name")
        out[name] = out.get(name, 0.0) + value if name in out else value
    return out


class OpenAICompatBackend(Backend):
    """Any server exposing ``/v1/completions`` — vLLM, TGI, llama.cpp, LocalAI.

    Free and self-hosted by default: point ``base_url`` at your own vLLM or
    llama.cpp server. ``api_key`` is optional and only sent when set.
    """

    name = "openai-compat"

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        model: str = "default",
        timeout: float = 120.0,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._headers = {"authorization": f"Bearer {api_key}"} if api_key else {}

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        payload = {
            "model": request.model or self.model,
            "prompt": request.prompt,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "top_p": request.top_p,
            **({"stop": request.stop} if request.stop else {}),
            **request.extra,
        }
        data = _post_json(
            f"{self.base_url}/v1/completions", payload, self.timeout, self._headers
        )
        choices = data.get("choices") or [{}]
        usage = data.get("usage") or {}
        cached = 0
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens", 0) or 0)
        return GenerationResponse(
            text=choices[0].get("text", ""),
            model=data.get("model", self.model),
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            cached_prompt_tokens=cached,
            finish_reason=choices[0].get("finish_reason", "stop"),
            raw=data,
        )

    def stream(self, request: GenerationRequest) -> Iterator[str]:
        payload = {
            "model": request.model or self.model,
            "prompt": request.prompt,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
            **request.extra,
        }
        body = json.dumps(payload).encode()
        http_request = urllib.request.Request(
            f"{self.base_url}/v1/completions",
            data=body,
            headers={"content-type": "application/json", **self._headers},
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode().strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        return
                    try:
                        parsed = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    for choice in parsed.get("choices", []):
                        text = choice.get("text")
                        if text:
                            yield text
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"stream failed: {exc}", retryable=True) from exc

    def health(self) -> BackendHealth:
        try:
            metrics = parse_prometheus(_get_text(f"{self.base_url}/metrics", 5.0))
        except BackendError as exc:
            return BackendHealth(up=False, model=self.model, detail=str(exc))
        return BackendHealth(
            up=True,
            model=self.model,
            queue_depth=metrics.get("vllm:num_requests_waiting", 0.0),
            running_batch_size=metrics.get("vllm:num_requests_running", 0.0),
            kv_cache_utilization=metrics.get("vllm:gpu_cache_usage_perc", 0.0),
            detail="ok",
        )


class VLLMBackend(OpenAICompatBackend):
    """vLLM, with its native metric names mapped onto the common health shape."""

    name = "vllm"

    def health(self) -> BackendHealth:
        try:
            metrics = parse_prometheus(_get_text(f"{self.base_url}/metrics", 5.0))
        except BackendError as exc:
            return BackendHealth(up=False, model=self.model, detail=str(exc))

        # vLLM reports cache usage as a 0..1 ratio; some builds emit percent.
        cache = metrics.get("vllm:gpu_cache_usage_perc", 0.0)
        if cache > 1.0:
            cache /= 100.0

        return BackendHealth(
            up=True,
            model=self.model,
            queue_depth=metrics.get("vllm:num_requests_waiting", 0.0),
            running_batch_size=metrics.get("vllm:num_requests_running", 0.0),
            kv_cache_utilization=cache,
            gpu_cache_usage_perc=cache * 100,
            detail="vllm",
        )


class TritonBackend(Backend):
    """NVIDIA Triton Inference Server via its KServe v2 HTTP protocol."""

    name = "triton"

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        model: str = "ensemble",
        timeout: float = 120.0,
        metrics_url: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        # Triton serves metrics on a separate port (8002) by convention.
        self.metrics_url = metrics_url or self.base_url.rsplit(":", 1)[0] + ":8002/metrics"

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        model = request.model or self.model
        payload = {
            "inputs": [
                {
                    "name": "text_input",
                    "shape": [1, 1],
                    "datatype": "BYTES",
                    "data": [request.prompt],
                },
                {
                    "name": "max_tokens",
                    "shape": [1, 1],
                    "datatype": "INT32",
                    "data": [request.max_tokens],
                },
            ],
            "outputs": [{"name": "text_output"}],
        }
        data = _post_json(
            f"{self.base_url}/v2/models/{model}/infer", payload, self.timeout, {}
        )
        outputs = data.get("outputs") or [{}]
        values = outputs[0].get("data") or [""]
        text = values[0] if isinstance(values[0], str) else str(values[0])
        return GenerationResponse(
            text=text,
            model=model,
            # Triton does not report token usage; estimate so the metric series
            # stays continuous when switching backends.
            prompt_tokens=max(1, len(request.prompt) // 4),
            completion_tokens=max(1, len(text) // 4),
            raw=data,
        )

    def health(self) -> BackendHealth:
        try:
            _get_text(f"{self.base_url}/v2/health/ready", 5.0)
        except BackendError as exc:
            return BackendHealth(up=False, model=self.model, detail=str(exc))

        try:
            metrics = parse_prometheus(_get_text(self.metrics_url, 5.0))
        except BackendError:
            metrics = {}

        return BackendHealth(
            up=True,
            model=self.model,
            queue_depth=metrics.get("nv_inference_pending_request_count", 0.0),
            running_batch_size=metrics.get("nv_inference_exec_count", 0.0),
            kv_cache_utilization=metrics.get("nv_gpu_memory_used_bytes", 0.0)
            / max(metrics.get("nv_gpu_memory_total_bytes", 1.0), 1.0),
            detail="triton",
        )


def build_backend(kind: str, **kwargs: Any) -> Backend:
    """Factory used by the gateway so backend choice stays configuration."""
    from .local import LocalSimBackend

    kinds = {
        "local": LocalSimBackend,
        "sim": LocalSimBackend,
        "vllm": VLLMBackend,
        "triton": TritonBackend,
        "openai-compat": OpenAICompatBackend,
        "tensorrt-llm": TritonBackend,  # served through Triton's backend
    }
    if kind not in kinds:
        raise ValueError(f"unknown backend {kind!r}; choose from {sorted(kinds)}")
    return kinds[kind](**kwargs)
