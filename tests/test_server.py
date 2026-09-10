"""HTTP surface of the gateway service."""
import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMOBS_BACKEND", "local")
    monkeypatch.setenv("LLMOBS_SIM_SPEED", "1000")
    monkeypatch.setenv("LLMOBS_DB", str(tmp_path / "traces.db"))

    import importlib

    from llmobs import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as test_client:
        yield test_client
    server_module.gateway.close()


def test_health_is_always_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_checks_the_backend(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["up"] is True


def test_generate_returns_text_and_trace_id(client):
    response = client.post("/v1/generate", json={"prompt": "hello", "max_tokens": 32})
    assert response.status_code == 200
    body = response.json()
    assert body["text"]
    assert len(body["trace_id"]) == 32
    assert body["usage"]["completion_tokens"] > 0


def test_generate_reports_validation(client):
    response = client.post(
        "/v1/generate", json={"prompt": "give me json", "max_tokens": 32, "expect_json": True}
    )
    body = response.json()
    assert body["valid"] is False
    assert any(i["check"] == "valid_json" for i in body["validation"]["issues"])


def test_generate_includes_stage_breakdown(client):
    body = client.post("/v1/generate", json={"prompt": "hello"}).json()
    assert body["breakdown_ms"]
    assert "inference" in body["breakdown_ms"]


def test_metrics_endpoint_is_prometheus_text(client):
    client.post("/v1/generate", json={"prompt": "hello", "max_tokens": 16})
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE llm_requests_total counter" in response.text


def test_traces_are_listable_and_fetchable(client):
    trace_id = client.post("/v1/generate", json={"prompt": "hello"}).json()["trace_id"]
    listing = client.get("/v1/traces?limit=5").json()
    assert any(row["trace_id"] == trace_id for row in listing)

    detail = client.get(f"/v1/traces/{trace_id}").json()
    assert detail["trace_id"] == trace_id
    assert detail["spans"]


def test_unknown_trace_is_404(client):
    assert client.get("/v1/traces/does-not-exist").status_code == 404


def test_stages_endpoint(client):
    client.post("/v1/generate", json={"prompt": "hello"})
    stages = client.get("/v1/stages").json()
    assert {row["kind"] for row in stages} >= {"request", "inference"}


def test_slo_endpoint_reports_all_objectives(client):
    client.post("/v1/generate", json={"prompt": "hello"})
    body = client.get("/v1/slo").json()
    assert body["worst_severity"] == "ok"
    assert len(body["slos"]) == 3


def test_stats_endpoint_summarises(client):
    client.post("/v1/generate", json={"prompt": "hello"})
    body = client.get("/stats").json()
    assert body["backend"] == "local-sim"
    assert body["metrics"]["requests_total"] == 1


def test_streaming_endpoint_returns_chunks(client):
    with client.stream("POST", "/v1/generate/stream", json={"prompt": "hello", "max_tokens": 24}) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    assert text.strip()


def test_overloaded_returns_503_with_retry_after(client, monkeypatch):
    from llmobs import server as server_module
    from llmobs.gateway import OverloadedError

    def shed(*_args, **_kwargs):
        raise OverloadedError("too many in flight")

    monkeypatch.setattr(server_module.gateway, "generate", shed)
    response = client.post("/v1/generate", json={"prompt": "hello"})
    assert response.status_code == 503
    assert response.headers.get("retry-after") == "1"


def test_backend_error_returns_502(client, monkeypatch):
    from llmobs import server as server_module
    from llmobs.backends import BackendError

    def fail(*_args, **_kwargs):
        raise BackendError("backend down", retryable=True)

    monkeypatch.setattr(server_module.gateway, "generate", fail)
    assert client.post("/v1/generate", json={"prompt": "hello"}).status_code == 502
