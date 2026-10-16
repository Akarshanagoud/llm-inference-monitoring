"""The operations console API. Its numbers must agree with the gateway's own records."""
import importlib
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMOBS_BACKEND", "local")
    monkeypatch.setenv("LLMOBS_SIM_SPEED", "1000")
    monkeypatch.setenv("LLMOBS_DB", str(tmp_path / "traces.db"))
    monkeypatch.delenv("LLMOBS_DEMO_TRAFFIC", raising=False)
    from llmobs import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        yield server_module, client
    server_module.traffic.stop()
    server_module.gateway.close()


def test_root_redirects_to_the_console(server):
    _, client = server
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui/"
    assert "Inference Observatory" in client.get("/ui/").text


def test_overview_separates_failures_from_retried_successes(server):
    _, client = server
    assert client.post("/v1/ui/scenario", json={"scenario": "flaky"}).status_code == 200
    statuses = [client.post("/v1/generate", json={"prompt": f"ticket {i}", "max_tokens": 16}).status_code for i in range(30)]
    kpis = client.get("/v1/ui/overview").json()["kpis"]
    assert kpis["requests"] == 30
    assert kpis["failed"] == statuses.count(502)
    assert kpis["retried"] > 0
    failed = client.get("/v1/ui/traces?view=failed").json()
    retried = client.get("/v1/ui/traces?view=retried").json()
    assert all(r["outcome"] == "error" for r in failed)
    assert all(r["outcome"] == "ok" and r["status"] == "error" for r in retried)
    assert len(retried) == kpis["retried"]


def test_scenarios_change_the_simulator_and_healthy_restores_it(server):
    module, client = server
    backend = module.gateway.backend
    baseline = backend.base_overhead_ms
    client.post("/v1/ui/scenario", json={"scenario": "degraded"})
    assert backend.base_overhead_ms == 1400.0
    client.post("/v1/ui/scenario", json={"scenario": "flaky"})
    assert backend.base_overhead_ms == baseline and backend.failure_rate == 0.25
    client.post("/v1/ui/scenario", json={"scenario": "healthy"})
    assert backend.failure_rate == 0.0
    assert client.post("/v1/ui/scenario", json={"scenario": "meteor"}).status_code == 422


def test_overview_reports_slos_with_their_alerting_rules(server):
    _, client = server
    client.post("/v1/generate", json={"prompt": "hello", "max_tokens": 16, "stream": True})
    data = client.get("/v1/ui/overview").json()
    names = {s["slo"] for s in data["slos"]}
    assert names == {c["name"] for c in data["slo_config"]}
    ttft = next(c for c in data["slo_config"] if c["name"] == "ttft_p95_1s")
    assert ttft["indicator"] == "ttft"
    assert {w["name"] for w in ttft["windows"]} == {"fast", "medium", "slow"}
    assert abs(sum(s["share"] for s in data["stages"]) - 1.0) < 0.01


def test_streamed_requests_measure_the_first_chunk(server):
    _, client = server
    body = client.post("/v1/generate", json={"prompt": "summarise the incident", "max_tokens": 64, "stream": True}).json()
    assert body["ttft_ms"] < body["duration_ms"]


def test_load_test_runs_through_the_gateway_and_is_recorded(server):
    _, client = server
    result = client.post("/v1/ui/load", json={"requests": 12, "concurrency": 4, "max_tokens": 16}).json()
    assert result["requests"] == 12 and result["stream"] is True
    assert client.get("/v1/ui/load").json()[0]["requests"] == 12
    assert client.get("/v1/ui/overview").json()["kpis"]["requests"] >= 12


def test_background_traffic_starts_and_stops(server):
    _, client = server
    state = client.post("/v1/ui/traffic", json={"running": True, "rps": 30}).json()
    assert state["running"] is True
    time.sleep(0.6)
    assert client.get("/v1/ui/traffic").json()["sent"] > 0
    client.post("/v1/ui/traffic", json={"running": False})
    time.sleep(0.3)
    assert client.get("/v1/ui/traffic").json()["running"] is False
